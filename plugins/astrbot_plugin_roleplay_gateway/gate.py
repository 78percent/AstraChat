"""Neutral integration gate. No model calls, persona reads, or chat history.

The SQLite ledger contains identifiers and delivery states only. An uncertain
send is never automatically retried, including after a process restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
import importlib
import json
import math
import re
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping

from .reply_pacing import PacingDecision, ReplyPacing, stable_uniform


NEUTRAL_REPLY = "中性连通验证：已收到本次唤醒。"
MODE = "neutral_receipt"
STREAM_MODE = "neutral_stream"
ROLEPLAY_MODE = "roleplay_stream"
STREAM_PROVIDER_ID = "deepseek-flash-official"
_NATIVE_PERSON = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}")
_NATIVE_GROUP = re.compile(r"[1-9][0-9]{0,117}@chatroom")
_NATIVE_SYSTEM_IDS = frozenset({"filehelper", "weixin", "fmessage", "newsapp", "floatbottle"})
_UINT64_MAX = 2**64 - 1


class ConfigurationError(ValueError):
    """Invalid configuration; messages deliberately do not include its values."""


class DefiniteSendFailure(Exception):
    """The platform explicitly rejected the action (not a transport timeout)."""


class AdmissionResult(str):
    """Preserve the string result contract with a static diagnostic reason."""
    def __new__(cls, result: str, reason: str):
        value = super().__new__(cls, result)
        value.reason = reason
        return value


def message_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    digits = value[1:] if value.startswith("-") else value
    if len(value) > 32 or not digits.isascii() or not digits.isdecimal():
        return None
    return str(int(value)) if int(value) != 0 else None


def opaque_id(value: Any, *, allow_colon: bool = False) -> str | None:
    """A bounded native WeChat identifier; never a replacement for QQ IDs."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    if (not value or len(value) > 128 or not value.isprintable()
            or any(char.isspace() for char in value) or (not allow_colon and ":" in value)):
        return None
    return value


def native_person_id(value: Any) -> str | None:
    """Exact desktop WeChat account/member ID; never a display name or room."""
    if (not isinstance(value, str) or _NATIVE_PERSON.fullmatch(value) is None
            or value in _NATIVE_SYSTEM_IDS or value.startswith("gh_")):
        return None
    return value


def native_group_id(value: Any) -> str | None:
    """Exact native room ID, isolated from QQ and ClawBot identity policies."""
    return value if isinstance(value, str) and _NATIVE_GROUP.fullmatch(value) else None


def incoming_message_id(value: Any, id_kind: str = "onebot") -> str | None:
    if id_kind == "onebot":
        return message_id(value)
    if id_kind == "weixin":
        return opaque_id(value, allow_colon=True)
    if id_kind == "wechat_native":
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        value = str(value)
        if re.fullmatch(r"[1-9][0-9]{0,19}", value) and int(value) <= _UINT64_MAX:
            return value
        return None
    raise ConfigurationError("unsupported_identity_kind")


@dataclass(frozen=True)
class ApiAccepted:
    """Adapter-verified local acceptance; no message ID or recipient proof.

    ClawBot requires its native API's explicit acceptance. The desktop bridge
    requires a verified native completion callback before creating this value.
    Both are recorded as API_ACCEPTED, never as recipient delivery.
    """


def send_evidence(result: Any, id_kind: str = "onebot") -> tuple[str, str | None]:
    """Only the selected protocol may produce its explicit success evidence."""
    if id_kind in {"weixin", "wechat_native"}:
        return ("API_ACCEPTED", None) if type(result) is ApiAccepted else ("UNKNOWN", None)
    if id_kind != "onebot":
        raise ConfigurationError("unsupported_identity_kind")
    receipt = message_id(result.get("message_id")) if isinstance(result, Mapping) else None
    return ("SENT", receipt) if receipt is not None else ("UNKNOWN", None)


async def _offload(work: Callable[[], Any]) -> Any:
    """Settle a submitted SQLite operation before delivering cancellation."""
    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling to_thread's await does not stop its thread. Fence that work
        # before the caller records shutdown states or closes the connection.
        try:
            await task
        except Exception:
            pass
        raise


@dataclass(frozen=True)
class GateSettings:
    platform_id: str = ""
    bot_self_id: str = ""
    admin_private_ids: frozenset[str] = frozenset()
    memory_admin_ids: frozenset[str] = frozenset()
    allowed_group_ids: frozenset[str] = frozenset()
    wake_names: tuple[str, ...] = ()
    wake_regex: tuple[str, ...] = ()
    enabled: bool = False
    mode: str = MODE
    provider_id: str = STREAM_PROVIDER_ID
    persona_id: str = ""
    persona_name: str = ""
    persona_filename: str = ""
    persona_sha256: str = ""
    send_timeout_seconds: float = 8.0
    max_pending: int = 16
    max_parallel_sends: int = 3
    reply_pacing: ReplyPacing = ReplyPacing()
    id_kind: str = "onebot"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, id_kind: str = "onebot") -> "GateSettings":
        # Protocol comes from the adapter, never a config switch on a QQ entry.
        if id_kind not in {"onebot", "weixin", "wechat_native"}:
            raise ConfigurationError("unsupported_identity_kind")
        def strings(key: str, *, ids: bool = False) -> tuple[str, ...]:
            value = raw.get(key, [])
            if not isinstance(value, list) or len(value) > 32:
                raise ConfigurationError(f"invalid_{key}")
            out = []
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise ConfigurationError(f"invalid_{key}")
                if ids and id_kind == "wechat_native" and item != item.strip():
                    raise ConfigurationError(f"invalid_{key}")
                item = item.strip()
                if len(item) > (512 if key == "wake_regex" else 128):
                    raise ConfigurationError(f"invalid_{key}")
                if ids:
                    native_validator = native_group_id if key == "allowed_group_ids" else native_person_id
                    if ((id_kind == "onebot" and (not item.isdecimal() or int(item) <= 0))
                            or (id_kind == "weixin" and opaque_id(item) is None)
                            or (id_kind == "wechat_native" and native_validator(item) is None)):
                        raise ConfigurationError(f"invalid_{key}")
                out.append(item)
            return tuple(dict.fromkeys(out))

        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigurationError("invalid_enabled")
        platform = raw.get("platform_id", "")
        bot = raw.get("bot_self_id", "")
        if not isinstance(platform, str) or not isinstance(bot, str):
            raise ConfigurationError("invalid_identity")
        if id_kind == "wechat_native" and bot != bot.strip():
            raise ConfigurationError("invalid_bot_self_id")
        platform, bot = platform.strip(), bot.strip()
        if len(platform) > 128 or ":" in platform:
            raise ConfigurationError("invalid_platform_id")
        if bot and ((id_kind == "onebot" and (not bot.isdecimal() or int(bot) <= 0 or len(bot) > 32))
                    or (id_kind == "weixin" and opaque_id(bot) is None)
                    or (id_kind == "wechat_native" and native_person_id(bot) is None)):
            raise ConfigurationError("invalid_bot_self_id")
        if id_kind == "weixin":
            if platform and opaque_id(platform) is None:
                raise ConfigurationError("invalid_platform_id")
            if bot and bot != platform:
                raise ConfigurationError("invalid_weixin_self_id")
            if strings("allowed_group_ids", ids=True):
                raise ConfigurationError("weixin_private_only")
        if id_kind == "wechat_native":
            if platform and opaque_id(platform) is None:
                raise ConfigurationError("invalid_platform_id")
            if len(strings("admin_private_ids", ids=True)) > 1:
                raise ConfigurationError("wechat_native_single_private_binding")
        if enabled and (not platform or not bot):
            raise ConfigurationError("missing_identity")
        mode = raw.get("mode", MODE)
        if mode not in (MODE, STREAM_MODE, ROLEPLAY_MODE):
            raise ConfigurationError("unsupported_mode")
        provider_id = raw.get("provider_id", STREAM_PROVIDER_ID)
        if provider_id != STREAM_PROVIDER_ID:
            raise ConfigurationError("unsupported_stream_provider")
        persona_fields = {key: raw.get(key, "") for key in (
            "persona_id", "persona_name", "persona_filename", "persona_sha256")}
        if mode == ROLEPLAY_MODE:
            if (any(not isinstance(value, str) for value in persona_fields.values())
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", persona_fields["persona_id"])
                    or not persona_fields["persona_name"].strip()
                    or len(persona_fields["persona_name"]) > 100
                    or any(ch in persona_fields["persona_name"] for ch in "\r\n\0")
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(?:txt|md|json|png)", persona_fields["persona_filename"])
                    or not re.fullmatch(r"[a-f0-9]{64}", persona_fields["persona_sha256"])):
                raise ConfigurationError("invalid_roleplay_persona_config")
        timeout = raw.get("send_timeout_seconds", 8.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigurationError("invalid_send_timeout")
        if not 0.1 <= timeout <= 30:
            raise ConfigurationError("invalid_send_timeout")
        try:
            pacing = ReplyPacing.from_mapping(raw.get("reply_pacing", {}))
            pacing.validate_deadlines(first=4, generation=25, total=30, send_timeout=timeout)
        except (ValueError, TypeError):
            raise ConfigurationError("invalid_reply_pacing") from None
        return cls(
            platform_id=platform,
            bot_self_id=bot,
            admin_private_ids=frozenset(strings("admin_private_ids", ids=True)),
            memory_admin_ids=(frozenset(strings("memory_admin_ids", ids=True))
                              if mode == ROLEPLAY_MODE else frozenset()),
            allowed_group_ids=frozenset(strings("allowed_group_ids", ids=True)),
            wake_names=strings("wake_names"),
            wake_regex=strings("wake_regex"),
            enabled=enabled,
            mode=mode,
            provider_id=provider_id,
            **persona_fields,
            send_timeout_seconds=float(timeout),
            reply_pacing=pacing,
            id_kind=id_kind,
        )


@dataclass(frozen=True)
class Envelope:
    platform_id: str
    bot_self_id: str
    sender_id: str
    incoming_message_id: str | None
    kind: str
    group_id: str | None = None
    text: str = ""
    mentions: tuple[str, ...] = ()
    received_at: float = 0.0
    post_type: str = "message"
    input_modality: str = "text"

    @property
    def session(self) -> str:
        target = self.group_id if self.kind == "group" else self.sender_id
        return f"{self.platform_id}:{self.kind}:{target}"


class WakeMatcher:
    """Use regex's interruptible timeout, never stdlib re in a worker thread."""

    def __init__(
        self,
        names: tuple[str, ...],
        patterns: tuple[str, ...],
        report: Callable[[str], None],
        regex_backend: Any = None,
    ) -> None:
        self.names = names
        self.report = report
        self.disabled: set[int] = set()
        self.patterns = []
        if patterns:
            try:
                backend = regex_backend or importlib.import_module("regex")
                self.patterns = [backend.compile(pattern) for pattern in patterns]
            except ImportError:
                raise ConfigurationError("regex_dependency_missing") from None
            except Exception:
                raise ConfigurationError("invalid_wake_regex") from None

    def matches(self, text: str) -> bool:
        # Bound matching input as well as per-pattern and total execution time.
        if len(text) > 4096:
            self.report("wake_text_too_long")
            return False
        if any(name in text for name in self.names):
            return True
        deadline = time.perf_counter() + 0.020
        for index, pattern in enumerate(self.patterns):
            if index in self.disabled:
                continue
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                self.report("regex_total_budget")
                break
            try:
                if pattern.search(text, timeout=min(0.005, remaining)):
                    return True
            except TimeoutError:
                self.disabled.add(index)
                self.report(f"regex_timeout_{index}")
        return False


class ReceiptStore:
    """Small, metadata-only ledger. All SQLite work runs outside the event loop."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    async def open(self) -> None:
        await _offload(self._open)

    def _open(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3):
                    raise ConfigurationError("unsupported_ledger_version")
                with conn:
                    conn.execute("""CREATE TABLE IF NOT EXISTS receipts (
                        platform_id TEXT NOT NULL, incoming_message_id TEXT NOT NULL,
                        bot_self_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                        group_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
                        reason TEXT NOT NULL, received_at REAL NOT NULL,
                        updated_at REAL NOT NULL, outgoing_message_id TEXT,
                        PRIMARY KEY (platform_id, incoming_message_id)
                    )""")
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(receipts)")}
                    for name, definition in (
                        ("purpose", "TEXT NOT NULL DEFAULT 'legacy'"),
                        ("started_at", "REAL"), ("first_ack_at", "REAL"),
                        ("confirmed_segments", "INTEGER NOT NULL DEFAULT 0"),
                        ("delivery_ref", "TEXT"),
                    ):
                        if name not in columns:
                            conn.execute(f"ALTER TABLE receipts ADD COLUMN {name} {definition}")
                    conn.execute("""CREATE TABLE IF NOT EXISTS pacing_decisions (
                        platform_id TEXT NOT NULL, incoming_message_id TEXT NOT NULL,
                        session_id TEXT NOT NULL, selected INTEGER NOT NULL,
                        reason TEXT NOT NULL, decided_at REAL NOT NULL, score REAL NOT NULL,
                        first_delay REAL NOT NULL, intervals_json TEXT NOT NULL,
                        policy_json TEXT NOT NULL,
                        PRIMARY KEY (platform_id,incoming_message_id)
                    )""")
                    conn.execute("""CREATE INDEX IF NOT EXISTS pacing_session_admissions
                        ON pacing_decisions(session_id,selected,decided_at)""")
                    conn.execute("PRAGMA user_version=3")
                    conn.execute("""UPDATE receipts SET status='UNKNOWN',
                        reason='restart_in_flight', updated_at=? WHERE status='IN_FLIGHT'""",
                        (time.time(),))
                    conn.execute("""UPDATE receipts SET status='ABORTED',
                        reason='restart_pending', updated_at=? WHERE status='ACCEPTED'""",
                        (time.time(),))
                self._connection = conn
            except BaseException:
                conn.close()
                raise

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("ledger_closed")
        return self._connection

    async def claim(self, event: Envelope, *, purpose: str = "neutral") -> bool:
        if purpose not in {"neutral", "roleplay", "management"}:
            raise ValueError("invalid_receipt_purpose")
        def work() -> bool:
            with self._lock:
                conn = self._db()
                with conn:
                    cursor = conn.execute("""INSERT OR IGNORE INTO receipts
                        (platform_id,incoming_message_id,bot_self_id,sender_id,group_id,
                         kind,status,reason,received_at,updated_at,purpose,delivery_ref)
                        VALUES (?,?,?,?,?,?,'ACCEPTED','accepted',?,?,?,?)""",
                        (event.platform_id, event.incoming_message_id, event.bot_self_id,
                         event.sender_id, event.group_id, event.kind,
                         event.received_at or time.time(), time.time(), purpose, uuid.uuid4().hex))
                return cursor.rowcount == 1
        return await _offload(work)

    async def decide_pacing(self, event: Envelope, policy: ReplyPacing, *,
                            now: float | None = None, reserve: bool = True) -> PacingDecision:
        if type(reserve) is not bool:
            raise ValueError("invalid_pacing_reservation")
        now = time.time() if now is None else now
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("invalid_pacing_clock")

        def work():
            with self._lock:
                conn = self._db()
                # The write transaction serializes quota reads and reservations,
                # including across separate connections to this same ledger.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    key = (event.platform_id, str(event.incoming_message_id))
                    prior = conn.execute("""SELECT * FROM pacing_decisions
                        WHERE platform_id=? AND incoming_message_id=?""", key).fetchone()
                    if prior is not None:
                        result = PacingDecision(bool(prior["selected"]), prior["reason"],
                                                prior["decided_at"], prior["score"], prior["first_delay"],
                                                tuple(json.loads(prior["intervals_json"])))
                    else:
                        receipt = conn.execute("""SELECT status FROM receipts
                            WHERE platform_id=? AND incoming_message_id=?""", key).fetchone()
                        if receipt is None or receipt["status"] != "ACCEPTED" or not policy.enabled:
                            raise RuntimeError("pacing_requires_live_claim")
                        seed = event.platform_id + "\0" + str(event.incoming_message_id)
                        score = stable_uniform(seed + "\0probability")
                        reason = "selected"
                        if score >= policy.probability:
                            reason = "probability"
                        else:
                            recent = conn.execute("""SELECT decided_at FROM pacing_decisions
                                WHERE session_id=? AND selected=1 AND decided_at>?
                                ORDER BY decided_at DESC""",
                                (event.session, now - policy.window_seconds)).fetchall()
                            if recent and now - recent[0][0] < policy.cooldown_seconds:
                                reason = "cooldown"
                            elif len(recent) >= policy.max_rounds:
                                reason = "quota"
                        result = PacingDecision(
                            reason == "selected", reason, now, score,
                            policy.sample(seed + "\0first", "first_delay"),
                            tuple(policy.sample(seed + f"\0interval\0{i}", "interval")
                                  for i in range(policy.max_bubbles - 1)),
                        )
                        if reserve:
                            conn.execute("""INSERT INTO pacing_decisions VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                         (*key, event.session, int(result.selected), result.reason, now,
                                          result.score, result.first_delay, json.dumps(result.intervals),
                                          json.dumps(asdict(policy), sort_keys=True)))
                        if reserve and not result.selected:
                            conn.execute("""UPDATE receipts SET status='SKIPPED',reason=?,updated_at=?
                                WHERE platform_id=? AND incoming_message_id=? AND status='ACCEPTED'""",
                                ("pacing_" + reason, now, *key))
                    conn.commit()
                    return result
                except BaseException:
                    conn.rollback()
                    raise
        return await _offload(work)

    async def record_delivery_metrics(self, event: Envelope, first_ack_at: float | None,
                                      confirmed_segments: int) -> None:
        def work():
            with self._lock:
                conn = self._db()
                with conn:
                    conn.execute("""UPDATE receipts SET first_ack_at=?,confirmed_segments=?
                        WHERE platform_id=? AND incoming_message_id=?""",
                        (first_ack_at, confirmed_segments, event.platform_id, event.incoming_message_id))
        await _offload(work)

    async def transition(
        self, event: Envelope, expected: str, status: str, reason: str,
        outgoing_message_id: str | None = None,
        *, delivery_ref: str | None = None,
    ) -> bool:
        def work() -> bool:
            with self._lock:
                conn = self._db()
                with conn:
                    cursor = conn.execute("""UPDATE receipts SET status=?,reason=?,
                        updated_at=?,outgoing_message_id=?,delivery_ref=COALESCE(?,delivery_ref),
                        started_at=CASE WHEN ?='IN_FLIGHT' THEN COALESCE(started_at,?) ELSE started_at END
                        WHERE platform_id=? AND incoming_message_id=? AND status=?""",
                        (status, reason, time.time(), outgoing_message_id, delivery_ref, status, time.time(),
                         event.platform_id, event.incoming_message_id, expected))
                return cursor.rowcount == 1
        return await _offload(work)

    async def get(self, platform: str, incoming_id: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            with self._lock:
                row = self._db().execute("""SELECT * FROM receipts
                    WHERE platform_id=? AND incoming_message_id=?""",
                    (platform, incoming_id)).fetchone()
                return dict(row) if row else None
        return await _offload(work)

    async def settle_shutdown(self) -> None:
        """Also covers workers cancelled before their coroutine first ran."""
        def work() -> None:
            with self._lock:
                conn = self._db()
                with conn:
                    conn.execute("""UPDATE receipts SET status='ABORTED',
                        reason='shutdown_pending', updated_at=? WHERE status='ACCEPTED'""",
                        (time.time(),))
                    conn.execute("""UPDATE receipts SET status='UNKNOWN',
                        reason='shutdown_in_flight', updated_at=? WHERE status='IN_FLIGHT'""",
                        (time.time(),))
        await _offload(work)

    async def close(self) -> None:
        def work() -> None:
            with self._lock:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
        await _offload(work)


Sender = Callable[[Envelope, str], Awaitable[Any]]


class GateService:
    def __init__(
        self, settings: GateSettings, store: ReceiptStore,
        emit: Callable[[dict[str, Any]], None] | None = None,
        enabled: Callable[[], bool] | None = None,
        regex_backend: Any = None,
        shared_parallel: asyncio.Semaphore | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.emit = emit or (lambda record: None)
        self.enabled = enabled or (lambda: settings.enabled)
        self._closing = False
        self._admission_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._workers: set[asyncio.Task] = set()
        self._sends: set[asyncio.Task] = set()
        self._sessions: dict[str, asyncio.Lock] = {}
        self._parallel = shared_parallel if shared_parallel is not None else asyncio.Semaphore(settings.max_parallel_sends)
        self.matcher = WakeMatcher(settings.wake_names, settings.wake_regex,
                                   lambda code: self._log(code), regex_backend)

    def _log(self, code: str, event: Envelope | None = None, **fields: Any) -> None:
        record = {"component": "roleplay_gateway", "code": code, **fields}
        if event is not None:
            record.update(platform_id=event.platform_id,
                          incoming_message_id=event.incoming_message_id,
                          sender_id=event.sender_id, group_id=event.group_id)
        try:
            self.emit(record)
        except Exception:
            pass  # Observability must not turn an acknowledged send into a retry.

    def owns(self, event: Envelope) -> bool:
        return bool(self.settings.platform_id and self.settings.bot_self_id
                    and event.platform_id == self.settings.platform_id
                    and event.bot_self_id == self.settings.bot_self_id)

    def eligibility_reason(self, event: Envelope) -> str | None:
        """Return fixed codes only; never copy message text or identities."""
        if self._closing:
            return "closing"
        if not self.enabled():
            return "disabled"
        if not self.owns(event):
            return "wrong_identity"
        if event.post_type != "message":
            return "not_message"
        if event.sender_id == event.bot_self_id:
            return "self_message"
        if self.settings.id_kind == "weixin" and (event.kind != "private" or opaque_id(event.sender_id) is None):
            return "invalid_sender_or_kind"
        if self.settings.id_kind == "wechat_native" and (
                event.kind not in ("group", "private") or (event.kind == "group" and native_group_id(event.group_id) is None)
                or native_person_id(event.sender_id) is None):
            return "invalid_sender_or_kind"
        if event.kind == "private":
            return None if event.sender_id in self.settings.admin_private_ids else "private_not_allowed"
        if event.kind != "group" or event.group_id not in self.settings.allowed_group_ids:
            return "group_not_allowed"
        if event.bot_self_id not in event.mentions and not self.matcher.matches(event.text):
            return "wake_not_matched"
        return None

    def eligible(self, event: Envelope) -> bool:
        return self.eligibility_reason(event) is None

    async def accept(self, event: Envelope, sender: Sender) -> str:
        reason = self.eligibility_reason(event)
        if reason is not None:
            return AdmissionResult("IGNORED", reason)
        incoming_id = incoming_message_id(event.incoming_message_id, self.settings.id_kind)
        if incoming_id is None:
            self._log("missing_message_id", event)
            return "INVALID"
        received = event.received_at
        if (isinstance(received, bool) or not isinstance(received, (int, float))
                or not math.isfinite(received) or received <= 0):
            received = time.time()
        event = replace(event, incoming_message_id=incoming_id, received_at=received)
        async with self._admission_lock:
            if self._closing or not self.enabled():
                return AdmissionResult("IGNORED", "closing" if self._closing else "disabled")
            return await self._admit(event, sender)

    async def _admit(self, event: Envelope, sender: Sender) -> str:
        if not await self.store.claim(event):
            self._log("duplicate_ignored", event)
            return "DUPLICATE"
        # Re-check after the asynchronous claim: shutdown/config disable may race it.
        if self._closing or not self.enabled():
            await self.store.transition(event, "ACCEPTED", "ABORTED", "gate_disabled")
            return "ABORTED"
        if len(self._workers) >= self.settings.max_pending:
            await self.store.transition(event, "ACCEPTED", "REJECTED", "pending_limit")
            self._log("rejected", event, reason="pending_limit")
            return "REJECTED"
        task = asyncio.create_task(self._run(event, sender), name="roleplay-gate-worker")
        self._workers.add(task)
        task.add_done_callback(lambda done: self._done(done, self._workers))
        return "ACCEPTED"

    def _done(self, task: asyncio.Task, owned: set[asyncio.Task]) -> None:
        owned.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._log("background_error", error_type=type(error).__name__)

    async def _finish(self, event: Envelope, status: str, reason: str,
                      outgoing_id: str | None = None, *, delivery_ref: str | None = None) -> None:
        if await self.store.transition(event, "IN_FLIGHT", status, reason, outgoing_id, delivery_ref=delivery_ref):
            self._log("delivery_result", event, status=status, reason=reason,
                      outgoing_message_id=outgoing_id, delivery_ref=delivery_ref)

    async def _run(self, event: Envelope, sender: Sender) -> None:
        dispatched = False
        try:
            lock = self._sessions.setdefault(event.session, asyncio.Lock())
            async with lock, self._parallel:
                if self._closing or not self.enabled():
                    await self.store.transition(event, "ACCEPTED", "ABORTED", "gate_disabled")
                    return
                if not await self.store.transition(event, "ACCEPTED", "IN_FLIGHT", "dispatch"):
                    return
                if self._closing or not self.enabled():
                    await self.store.transition(event, "IN_FLIGHT", "ABORTED", "before_dispatch")
                    return
                call = asyncio.create_task(sender(event, NEUTRAL_REPLY), name="roleplay-gate-send")
                dispatched = True
                self._sends.add(call)
                call.add_done_callback(lambda done: self._done(done, self._sends))
                done, _ = await asyncio.wait({call}, timeout=self.settings.send_timeout_seconds)
                if not done:
                    await self._finish(event, "UNKNOWN", "send_timeout")
                    call.cancel()
                    return
                try:
                    result = call.result()
                except DefiniteSendFailure:
                    await self._finish(event, "FAILED", "platform_rejected")
                    return
                except asyncio.CancelledError:
                    await self._finish(event, "UNKNOWN", "send_cancelled")
                    return
                except Exception:
                    await self._finish(event, "UNKNOWN", "transport_error")
                    return
                status, receipt = send_evidence(result, self.settings.id_kind)
                if status == "UNKNOWN":
                    await self._finish(event, "UNKNOWN", "missing_receipt")
                else:
                    await self._finish(event, status, "api_accepted" if status == "API_ACCEPTED" else "acknowledged", receipt)
        except asyncio.CancelledError:
            if dispatched:
                await self._finish(event, "UNKNOWN", "shutdown_in_flight")
            else:
                changed = await self.store.transition(event, "ACCEPTED", "ABORTED", "shutdown_pending")
                if not changed:
                    await self.store.transition(event, "IN_FLIGHT", "ABORTED", "shutdown_before_dispatch")
            raise
        except Exception as error:
            if dispatched:
                await self._finish(event, "UNKNOWN", "worker_error")
            else:
                await self.store.transition(event, "ACCEPTED", "ABORTED", "worker_error")
            self._log("worker_error", event, error_type=type(error).__name__)

    async def wait_idle(self) -> None:
        """Test/maintenance barrier; event handlers must not wait here."""
        while self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)

    async def close(self) -> None:
        self._closing = True
        async with self._close_lock:
            if self._closed:
                return
            # A claim can have been submitted before closing was requested.
            async with self._admission_lock:
                pass
            await self._close()
            self._closed = True

    async def _close(self) -> None:
        for task in tuple(self._workers):
            task.cancel()
        await self.wait_idle()
        for task in tuple(self._sends):
            task.cancel()
        if self._sends:
            done, pending = await asyncio.wait(tuple(self._sends), timeout=2.0)
            for task in done:
                self._done(task, self._sends)
            # A non-cooperative third-party coroutine cannot be force-killed by
            # asyncio. Keep it tracked and never accept its late result as SENT.
            for task in pending:
                task.cancel()
            if pending:
                self._log("shutdown_uncooperative_send", pending=len(pending))
        await self.store.settle_shutdown()
        await self.store.close()
