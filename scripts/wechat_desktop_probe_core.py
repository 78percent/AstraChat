"""Pure single-run state machine for a visible-message desktop experiment.

This module does not import wxauto, touch a desktop, perform I/O, or call a model.
UI identifiers are ephemeral observation labels, never WeChat message IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Iterable


@dataclass(frozen=True)
class ObservedMessage:
    ui_id: str
    attr: str
    type: str
    sender: str
    content: str


_TERMINAL = frozenset({"UI_OBSERVED", "FAILED_BEFORE_DISPATCH", "UNKNOWN"})
_EXTERNAL_REASONS = frozenset({
    "timeout", "deadline", "user_stop", "interrupted", "stop_requested",
    "ui_error", "read_error", "send_error", "wrapper_error", "preflight_failed",
    "target_mismatch", "desktop_unavailable", "chat_info_error", "observe_error",
    "send_returned_false", "poll_timeout", "live_deadline", "cancelled",
})


class ProbeState:
    """Admit one new exact command and observe, but never certify, its reply.

    ``observe`` returns True once, when the wrapper may prepare a send. The
    wrapper must persist its dispatch marker and call ``mark_dispatched`` before
    touching the SendMsg API. A terminal state never resumes or authorizes retry.
    All ``now`` values use one caller-supplied monotonic clock.
    """

    def __init__(self, target: str, nonce: str):
        if (not isinstance(target, str) or not target.strip() or len(target) > 128
                or any(ord(char) < 32 for char in target)):
            raise ValueError("invalid_target")
        if not isinstance(nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{6,64}", nonce) is None:
            raise ValueError("invalid_nonce")
        self.target = target
        self.command = "三叶 桌面测试 " + nonce
        self.reply = "桌面连通验证：已收到本次唤醒。[" + nonce + "]"
        self.state = "BASELINE"
        self.reason = "awaiting_baseline"
        self._seen: set[tuple[str, str, str, str, str]] = set()
        self._started_at: float | None = None
        self._last_at: float | None = None
        self._admitted_at: float | None = None
        self._dispatched_at: float | None = None
        self._observed_at: float | None = None
        self._ended_at: float | None = None
        self._counts = {
            "snapshots": 0, "baseline_messages": 0, "new_observations": 0,
            "repeated_observations": 0, "invalid_observations": 0,
            "ignored_observations": 0, "member_text_observations": 0,
            "self_text_observations": 0, "admissions": 0, "dispatches": 0,
        }

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def _tick(self, now: float) -> bool:
        if (isinstance(now, bool) or not isinstance(now, (int, float))
                or not math.isfinite(now) or now < 0
                or (self._last_at is not None and now < self._last_at)):
            self._finish("invalid_clock")
            return False
        value = float(now)
        if self._started_at is None:
            self._started_at = value
        self._last_at = value
        return True

    def _finish(self, reason: str) -> None:
        if self.terminal:
            return
        self.state = "UNKNOWN" if self._dispatched_at is not None else "FAILED_BEFORE_DISPATCH"
        self.reason = reason
        self._ended_at = self._last_at

    @staticmethod
    def _key(message: object) -> tuple[str, str, str, str, str] | None:
        if not isinstance(message, ObservedMessage):
            return None
        fields = (message.ui_id, message.attr, message.type, message.sender, message.content)
        if not all(isinstance(value, str) for value in fields):
            return None
        if (not message.ui_id or len(message.ui_id) > 256
                or not message.ui_id.isprintable()
                or any(char.isspace() for char in message.ui_id)
                or not message.attr or len(message.attr) > 32
                or not message.type or len(message.type) > 32
                or len(message.sender) > 256 or "\0" in message.sender
                or len(message.content) > 100_000 or "\0" in message.content):
            return None
        return fields

    def observe(self, chat_name: str, chat_type: str,
                messages: Iterable[ObservedMessage], now: float) -> bool:
        """Inspect only the confirmed target; return a one-time send admission.

        Target validation happens before consuming ``messages``. The UI wrapper
        must also perform this check *before* obtaining the underlying snapshot.
        A startup command or reply with this nonce invalidates the entire run.
        """
        if self.terminal:
            return False
        if not self._tick(now):
            return False
        if chat_name != self.target or chat_type != "group":
            self._finish("target_mismatch")
            return False
        try:
            snapshot = list(messages)
        except Exception:
            self._finish("read_error")
            return False
        self._counts["snapshots"] += 1
        baseline = self.state == "BASELINE"
        # Inspect the whole baseline before admitting anything. Even malformed
        # metadata cannot make a visibly reused nonce safe to replay.
        if baseline and any(isinstance(message, ObservedMessage)
                            and isinstance(message.content, str)
                            and message.content in (self.command, self.reply)
                            for message in snapshot):
            self._finish("nonce_already_visible")
            return False
        admitted = False
        for message in snapshot:
            key = self._key(message)
            if key is None:
                self._counts["invalid_observations"] += 1
                continue
            if key in self._seen:
                self._counts["repeated_observations"] += 1
                continue
            self._seen.add(key)
            if baseline:
                self._counts["baseline_messages"] += 1
                continue
            self._counts["new_observations"] += 1
            if message.type != "text" or message.attr not in {"friend", "self"}:
                self._counts["ignored_observations"] += 1
                continue
            if message.attr == "self":
                self._counts["self_text_observations"] += 1
                if message.content == self.reply:
                    if self.state != "DISPATCHED":
                        self._finish("reply_seen_before_dispatch")
                        return False
                    self.state = "UI_OBSERVED"
                    self.reason = "matching_self_bubble_observed"
                    self._observed_at = self._last_at
                    self._ended_at = self._last_at
                    return False
                continue
            if not message.sender.strip() or any(ord(char) < 32 for char in message.sender):
                self._counts["invalid_observations"] += 1
                continue
            self._counts["member_text_observations"] += 1
            if self.state == "WAITING" and message.content == self.command:
                self.state = "ADMITTED"
                self.reason = "new_command_observed"
                self._admitted_at = self._last_at
                self._counts["admissions"] += 1
                admitted = True
        if baseline:
            self.state = "WAITING"
            self.reason = "waiting_for_new_command"
        return admitted and self.state == "ADMITTED"

    def mark_dispatched(self, now: float) -> None:
        """Record exactly one attempted UI send, before calling external code."""
        if self.state != "ADMITTED":
            raise ValueError("dispatch_requires_admission")
        if not self._tick(now):
            raise ValueError("invalid_clock")
        self.state = "DISPATCHED"
        self.reason = "awaiting_matching_self_bubble"
        self._dispatched_at = self._last_at
        self._counts["dispatches"] += 1

    def fail(self, reason: str, now: float) -> None:
        """End a run; arbitrary exception text never enters diagnostics."""
        if self.terminal:
            return
        if self._tick(now):
            self._finish(reason if reason in _EXTERNAL_REASONS else "external_failure")

    def stop(self, reason: str, now: float) -> None:
        self.fail(reason, now)

    def summary(self) -> dict:
        """Return counters and relative times only; no chat, identity or nonce."""
        def difference(later: float | None, earlier: float | None) -> float | None:
            return None if later is None or earlier is None else round(later - earlier, 6)

        return {
            "state": self.state,
            "reason": self.reason,
            "terminal": self.terminal,
            "evidence": "desktop_ui_observation_only",
            "counts": dict(self._counts),
            "timings_seconds": {
                "first_command_after_start": difference(self._admitted_at, self._started_at),
                "command_observed_to_dispatch": difference(self._dispatched_at, self._admitted_at),
                "dispatch_to_ui_observed": difference(self._observed_at, self._dispatched_at),
                "command_observed_to_ui_observed": difference(self._observed_at, self._admitted_at),
                "run_elapsed": difference(self._ended_at if self.terminal else self._last_at,
                                          self._started_at),
            },
        }
