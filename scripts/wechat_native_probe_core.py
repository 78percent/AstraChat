"""Pure, single-message native probe policy; no Win32, I/O, or upstream import.

The transport may receive unrelated native events. This module rejects their
metadata before accessing text where possible, and never reports their content
or identifiers. A completion callback is not a recipient delivery receipt.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
import re


_UINT64_MAX = 2**64 - 1
_ROOM = re.compile(r"[1-9][0-9]{0,117}@chatroom")
_PERSON = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}")
_SYSTEM = frozenset({"filehelper", "weixin", "fmessage", "newsapp", "floatbottle"})
_TERMINAL = frozenset({"NATIVE_COMPLETED", "UNKNOWN", "STOPPED"})
_REASONS = frozenset({
    "deadline", "timeout", "native_timeout", "native_error", "transport_error",
    "account_changed", "process_changed", "module_changed", "queue_dropped",
    "user_stop", "interrupted", "stop_requested", "cancelled", "preflight_failed",
    "send_error", "read_error", "cleanup_failed", "report_write_failed",
})


def _person(value: object) -> bool:
    return (isinstance(value, str) and _PERSON.fullmatch(value) is not None
            and value not in _SYSTEM and not value.startswith("gh_"))


def _native_identifier(value: object) -> str | None:
    if not isinstance(value, bytes) or not 1 <= len(value) <= 127:
        return None
    try:
        return value.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return None


class NativeProbe:
    """Bind one actual room with a fresh command, then permit one dispatch.

    The caller supplies a monotonic clock and persists its dispatch intent
    before performing a native side effect. ``room`` and ``incoming_id`` are
    private binding data; ``summary()`` deliberately excludes them.
    """

    def __init__(self, self_id: str, nonce: str, allowed_room: str | None = None):
        if not _person(self_id):
            raise ValueError("invalid_self_id")
        if not isinstance(nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{6,64}", nonce) is None:
            raise ValueError("invalid_nonce")
        if allowed_room is not None and (
                not isinstance(allowed_room, str) or _ROOM.fullmatch(allowed_room) is None):
            raise ValueError("invalid_allowed_room")
        self.self_id = self_id
        self.command = "三叶 原生测试 " + nonce
        self.reply = "原生连通验证：已收到本次唤醒。[" + nonce + "]"
        self.room: str | None = allowed_room
        self.incoming_id: str | None = None
        self.state = "WAITING"
        self.reason = "awaiting_exact_command"
        self._seen_ids: set[int] = set()
        self._started_at: float | None = None
        self._last_at: float | None = None
        self._admitted_at: float | None = None
        self._dispatched_at: float | None = None
        self._completed_at: float | None = None
        self._ended_at: float | None = None
        self._counts = {
            "observations": 0, "metadata_rejected": 0, "text_rejected": 0,
            "self_rejected": 0, "unmatched_commands": 0, "duplicate_ids": 0,
            "admissions": 0, "dispatches": 0,
        }

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def _end(self, reason: str) -> None:
        if self.terminal:
            return
        self.state = "UNKNOWN" if self._dispatched_at is not None else "STOPPED"
        self.reason = reason
        self._ended_at = self._last_at

    def _tick(self, now: float) -> bool:
        if (isinstance(now, bool) or not isinstance(now, (int, float))
                or not math.isfinite(now) or now < 0
                or (self._last_at is not None and now < self._last_at)):
            self._end("invalid_clock")
            return False
        self._last_at = float(now)
        if self._started_at is None:
            self._started_at = self._last_at
        return True

    def observe(self, raw: object, now: float) -> bool:
        """Admit one exact command; rejected metadata never requires ``raw.text``."""
        if self.state != "WAITING":
            return False
        if not self._tick(now):
            return False
        self._counts["observations"] += 1
        try:
            kind = getattr(raw, "type", None)
            if type(kind) is not int or kind != 1:
                self._counts["metadata_rejected"] += 1
                return False
            receiver = _native_identifier(getattr(raw, "receiver", None))
            if receiver != self.self_id:
                self._counts["metadata_rejected"] += 1
                return False
            room = _native_identifier(getattr(raw, "sender", None))
            if (room is None or _ROOM.fullmatch(room) is None
                    or (self.room is not None and room != self.room)):
                self._counts["metadata_rejected"] += 1
                return False
            native_id = getattr(raw, "native_message_id", None)
            sequence = getattr(raw, "sequence", None)
            if (type(native_id) is not int or not 1 <= native_id <= _UINT64_MAX
                    or type(sequence) is not int or not 1 <= sequence <= _UINT64_MAX):
                self._counts["metadata_rejected"] += 1
                return False
        except Exception:
            self._counts["metadata_rejected"] += 1
            return False
        if native_id in self._seen_ids:
            self._counts["duplicate_ids"] += 1
            return False
        self._seen_ids.add(native_id)
        try:
            encoded = getattr(raw, "text", None)
            if not isinstance(encoded, bytes) or not 1 <= len(encoded) <= 1023 or b"\0" in encoded:
                self._counts["text_rejected"] += 1
                return False
            text = encoded.decode("utf-8", errors="strict")
        except Exception:
            self._counts["text_rejected"] += 1
            return False
        member, separator, body = text.partition(":\n")
        if not separator or not _person(member):
            self._counts["text_rejected"] += 1
            return False
        if member == self.self_id:
            self._counts["self_rejected"] += 1
            return False
        if body != self.command:
            self._counts["unmatched_commands"] += 1
            return False
        self.room = room
        # Decimal text preserves the exact uint64 when a caller uses JSON/JS.
        self.incoming_id = str(native_id)
        self.state = "ADMITTED"
        self.reason = "new_command_observed"
        self._admitted_at = self._last_at
        self._counts["admissions"] += 1
        return True

    def mark_dispatched(self, now: float) -> None:
        if self.state != "ADMITTED":
            raise ValueError("dispatch_requires_admission")
        if not self._tick(now):
            raise ValueError("invalid_clock")
        self.state = "DISPATCHED"
        self.reason = "awaiting_native_completion"
        self._dispatched_at = self._last_at
        self._counts["dispatches"] += 1

    def complete(self, outcome: object, now: float) -> bool:
        """Verify full local completion evidence; a late result never revives a run."""
        if self.terminal:
            return False
        if self.state != "DISPATCHED":
            raise ValueError("completion_requires_dispatch")
        if not self._tick(now):
            return False
        valid = False
        try:
            if isinstance(outcome, Mapping):
                report = outcome.get("report")
                valid = (
                    outcome.get("success") is True
                    and type(outcome.get("code")) is int and outcome["code"] == 0
                    and type(outcome.get("cleanup_code")) is int and outcome["cleanup_code"] == 0
                    and isinstance(report, Mapping)
                    and report.get("completion_success") is True
                    and report.get("target") == self.room
                    and report.get("text") == self.reply
                )
        except Exception:
            valid = False
        if not valid:
            self._end("completion_not_verified")
            return False
        self.state = "NATIVE_COMPLETED"
        self.reason = "matching_native_completion"
        self._completed_at = self._last_at
        self._ended_at = self._last_at
        return True

    def fail(self, reason: str, now: float) -> None:
        if self.terminal:
            return
        if self._tick(now):
            self._end(reason if isinstance(reason, str) and reason in _REASONS else "external_failure")

    def stop(self, reason: str, now: float) -> None:
        self.fail(reason, now)

    def summary(self) -> dict:
        def difference(later: float | None, earlier: float | None) -> float | None:
            return None if later is None or earlier is None else round(later - earlier, 6)

        return {
            "state": self.state, "reason": self.reason, "terminal": self.terminal,
            "binding_established_by_command": self.incoming_id is not None,
            "evidence": "native_completion_only",
            "recipient_delivery_confirmed": False,
            "outgoing_platform_message_id": None,
            "counts": dict(self._counts),
            "timings_seconds": {
                "command_after_start": difference(self._admitted_at, self._started_at),
                "command_observed_to_dispatch": difference(self._dispatched_at, self._admitted_at),
                "dispatch_to_native_completion": difference(self._completed_at, self._dispatched_at),
                "command_observed_to_native_completion": difference(self._completed_at, self._admitted_at),
                "run_elapsed": difference(self._ended_at if self.terminal else self._last_at,
                                          self._started_at),
            },
        }
