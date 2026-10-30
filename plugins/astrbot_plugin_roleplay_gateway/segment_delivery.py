"""Metadata-only segmented delivery for the explicit neutral_stream mode.

One SegmentSender belongs to one attempt. The future coordinator owns session
serialization, generation changes, stream buffering, fallback and history. This
module neither reads credentials nor calls a particular platform. ``send_call``
is the injected platform boundary and must return protocol-specific evidence.

``commit_allowed`` is a snapshot, never a history transaction authorization:
the history owner MUST check token/epoch again in its own commit transaction.
An attribution says which input a segment addresses; it does not prove that the
segment substantively answers that input (the acceptance harness owns that).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Awaitable, Callable
import uuid

from .gate import DefiniteSendFailure, _offload, incoming_message_id, message_id, send_evidence


def _identifier(value: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(
        ord(char) < 32 for char in value
    ):
        raise ValueError("invalid_delivery_identifier")
    return value


@dataclass(frozen=True)
class AttemptToken:
    platform_id: str
    session_id: str
    batch_id: str
    attempt_generation: int
    memory_epoch: int

    def __post_init__(self) -> None:
        for value in (self.platform_id, self.batch_id):
            _identifier(value)
        _identifier(self.session_id, maximum=384)
        for value in (self.attempt_generation, self.memory_epoch):
            if type(value) is not int or value < 0:
                raise ValueError("invalid_delivery_version")

    @property
    def batch_key(self) -> tuple[str, str, str]:
        return self.platform_id, self.session_id, self.batch_id


@dataclass(frozen=True)
class InputRef:
    message_id: str
    speaker_id: str
    id_kind: str = "onebot"

    def __post_init__(self) -> None:
        normalized = incoming_message_id(self.message_id, self.id_kind)
        if normalized is None:
            raise ValueError("invalid_input_message_id")
        object.__setattr__(self, "message_id", normalized)
        _identifier(self.speaker_id)


@dataclass(frozen=True)
class DeliveryOutcome:
    sequence: int
    status: str
    outgoing_message_id: str | None = None
    commit_allowed: bool = False
    reason: str = ""
    duplicate: bool = False
    delivery_ref: str | None = None

    @property
    def has_success_evidence(self) -> bool:
        if self.status == "SENT":
            return message_id(self.outgoing_message_id) is not None
        return (self.status == "API_ACCEPTED" and self.outgoing_message_id is None
                and isinstance(self.delivery_ref, str) and bool(self.delivery_ref)
                and len(self.delivery_ref) <= 160 and self.delivery_ref.isprintable()
                and not any(char.isspace() for char in self.delivery_ref))


class SegmentLedger:
    """A dedicated SQLite file, with no request/response body or error text.

    A single live owner is required. Opening the ledger is a process-recovery
    boundary: unfinished dispatches become UNKNOWN and unsent claims FAILED.
    Reopening does not authorize retrying an existing attempt/sequence number.
    An explicit newer generation may restart numbering only when every older
    attempt's claim was definitely unsent/failed; the coordinator owns that
    generation change and still needs to settle the old model request.
    """

    _APPLICATION_ID = 0x5347444C

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    async def open(self) -> None:
        def work() -> None:
            with self._lock:
                if self._connection is not None:
                    raise RuntimeError("segment_ledger_already_open")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
                db.row_factory = sqlite3.Row
                try:
                    app = db.execute("PRAGMA application_id").fetchone()[0]
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    if (app not in (0, self._APPLICATION_ID) or version not in (0, 1, 2)
                            or (app == 0 and tables)):
                        raise ValueError("unsupported_segment_ledger")
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("PRAGMA busy_timeout=5000")
                    db.execute("BEGIN IMMEDIATE")
                    with db:
                        if version == 1:
                            db.execute("ALTER TABLE segments RENAME TO segments_v1")
                        db.execute("""CREATE TABLE IF NOT EXISTS segments (
                            platform_id TEXT NOT NULL, session_id TEXT NOT NULL,
                            batch_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                            attempt_generation INTEGER NOT NULL, memory_epoch INTEGER NOT NULL,
                            attribution_json TEXT NOT NULL, claim_id TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN
                                ('UNSENT','IN_FLIGHT','SENT','API_ACCEPTED','UNKNOWN','FAILED')),
                            reason TEXT NOT NULL, outgoing_message_id TEXT,
                            created_at REAL NOT NULL, updated_at REAL NOT NULL,
                            PRIMARY KEY(platform_id, session_id, batch_id, attempt_generation, sequence)
                        )""")
                        if version == 1:
                            db.execute("INSERT INTO segments SELECT * FROM segments_v1")
                            db.execute("DROP TABLE segments_v1")
                        db.execute(f"PRAGMA application_id={self._APPLICATION_ID}")
                        db.execute("PRAGMA user_version=2")
                        now = time.time()
                        db.execute("""UPDATE segments SET status='UNKNOWN',
                            reason='restart_in_flight', updated_at=? WHERE status='IN_FLIGHT'""", (now,))
                        db.execute("""UPDATE segments SET status='FAILED',
                            reason='restart_unsent', updated_at=? WHERE status='UNSENT'""", (now,))
                    self._connection = db
                except BaseException:
                    db.close()
                    raise
        await _offload(work)

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("segment_ledger_closed")
        return self._connection

    async def _reserve(
        self, token: AttemptToken, sequence: int, refs: tuple[InputRef, ...], claim_id: str,
    ) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            with self._lock:
                db = self._db()
                now = time.time()
                with db:
                    existing = db.execute("""SELECT * FROM segments WHERE platform_id=?
                        AND session_id=? AND batch_id=? AND attempt_generation=? AND sequence=?""",
                        (*token.batch_key, token.attempt_generation, sequence)).fetchone()
                    if existing is not None:
                        return dict(existing)
                    blocked = db.execute("""SELECT * FROM segments WHERE platform_id=?
                        AND session_id=? AND batch_id=? AND (status IN ('IN_FLIGHT','UNKNOWN')
                        OR (status IN ('UNSENT','SENT','API_ACCEPTED') AND
                        (attempt_generation<>? OR memory_epoch<>?))) LIMIT 1""",
                        (*token.batch_key, token.attempt_generation, token.memory_epoch)).fetchone()
                    if blocked is not None:
                        return {**dict(blocked), "blocked": True}
                    cursor = db.execute("""INSERT OR IGNORE INTO segments VALUES
                        (?,?,?,?,?,?,?,?, 'UNSENT','reserved',NULL,?,?)""",
                        (*token.batch_key, sequence, token.attempt_generation, token.memory_epoch,
                         json.dumps([[ref.message_id, ref.speaker_id] for ref in refs]),
                         claim_id, now, now))
                if cursor.rowcount:
                    return None
                return dict(db.execute("""SELECT * FROM segments WHERE platform_id=?
                    AND session_id=? AND batch_id=? AND attempt_generation=? AND sequence=?""",
                    (*token.batch_key, token.attempt_generation, sequence)).fetchone())
        return await _offload(work)

    async def _transition(
        self, token: AttemptToken, sequence: int, claim_id: str, expected: tuple[str, ...],
        status: str, reason: str, receipt: str | None = None,
    ) -> bool:
        if status == "SENT" and message_id(receipt) is None:
            raise ValueError("sent_requires_message_id")
        if status == "API_ACCEPTED" and (receipt is not None or not claim_id):
            raise ValueError("api_accepted_has_no_platform_message_id")
        observed_at = time.time()
        def work() -> bool:
            with self._lock:
                db = self._db()
                placeholders = ",".join("?" for _ in expected)
                with db:
                    cursor = db.execute(f"""UPDATE segments SET status=?, reason=?,
                        outgoing_message_id=?, updated_at=? WHERE platform_id=? AND session_id=?
                        AND batch_id=? AND attempt_generation=? AND sequence=? AND claim_id=?
                        AND status IN ({placeholders})""",
                        (status, reason, receipt, observed_at, *token.batch_key, token.attempt_generation, sequence,
                         claim_id, *expected))
                return bool(cursor.rowcount)
        return await _offload(work)

    async def records(self, token: AttemptToken) -> tuple[dict[str, Any], ...]:
        """All attempts in this batch; bodies are deliberately unavailable."""
        def work() -> tuple[dict[str, Any], ...]:
            with self._lock:
                rows = self._db().execute("""SELECT platform_id,session_id,batch_id,sequence,
                    attempt_generation,memory_epoch,attribution_json,status,reason,
                    outgoing_message_id,claim_id AS delivery_ref,created_at,updated_at FROM segments WHERE platform_id=?
                    AND session_id=? AND batch_id=? ORDER BY attempt_generation,sequence""", token.batch_key)
                return tuple(dict(row) for row in rows)
        return await _offload(work)

    async def close(self) -> None:
        def work() -> None:
            with self._lock:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
        await _offload(work)


class _NotDispatched(Exception):
    pass


@dataclass
class _Invocation:
    dispatched: bool = False


class SegmentSender:
    """Serial delivery for one attempt; no retry, fallback or history writes.

    The final synchronous is_current check and send_call invocation have no
    intervening await. A send_call adapter must treat its invocation as its
    submission boundary; it must not defer submission in an untracked task.
    Uncooperative timed-out calls stay tracked until they actually settle.
    close() cancels and waits a bounded interval, but cannot force-kill them.
    """

    def __init__(
        self, ledger: SegmentLedger, token: AttemptToken,
        send_call: Callable[[str], Awaitable[Any]],
        is_current: Callable[[AttemptToken], bool], *, segment_interval: float = 1.5,
        send_timeout: float = 8.0, close_timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        first_not_before: float = 0.0,
        interval_samples: tuple[float, ...] | None = None,
        id_kind: str = "onebot",
        prepare_call=None,
    ) -> None:
        for value, zero_ok in ((segment_interval, True), (send_timeout, False), (close_timeout, True)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("invalid_delivery_timing")
            if value < 0 or (not zero_ok and value == 0):
                raise ValueError("invalid_delivery_timing")
        self.ledger, self.token = ledger, token
        incoming_message_id(None, id_kind)  # Reject an unknown protocol before any dispatch.
        self.id_kind = id_kind
        self._send_call, self._is_current = send_call, is_current
        self._prepare_call = prepare_call
        self._interval, self._timeout, self._close_timeout = segment_interval, send_timeout, close_timeout
        self._clock, self._sleep = clock, sleep
        if (isinstance(first_not_before, bool) or not isinstance(first_not_before, (int, float))
                or not math.isfinite(first_not_before)):
            raise ValueError("invalid_first_send_target")
        if interval_samples is not None and (not isinstance(interval_samples, tuple) or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 for value in interval_samples
        )):
            raise ValueError("invalid_interval_samples")
        self._first_not_before, self._interval_samples = first_not_before, interval_samples
        self._next_sequence = 0
        self._last_ack: float | None = None
        self._stopped = False
        self._closing = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._workers: set[asyncio.Task[Any]] = set()
        self._sends: set[asyncio.Task[Any]] = set()

    @property
    def pending_sends(self) -> int:
        return sum(not task.done() for task in self._sends)

    def _current(self) -> bool:
        result = self._is_current(self.token)
        if type(result) is not bool:
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("is_current_must_be_synchronous_bool")
        return result

    @staticmethod
    def _done(task: asyncio.Task[Any], tasks: set[asyncio.Task[Any]]) -> None:
        tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Consume only; late completion never changes the ledger.

    async def can_fallback(self) -> bool:
        """Necessary condition only; the coordinator must also settle the model.

        This is not a lock or permission to launch an overlapping attempt. Only
        a coordinator that has stopped this attempt may act on a true result.
        """
        if self._closing or self._workers or self.pending_sends or not self._current():
            return False
        records = await self.ledger.records(self.token)
        return (not self._closing and not self._workers and not self.pending_sends
                and self._current() and not any(row["status"] in ("UNSENT", "IN_FLIGHT", "SENT", "API_ACCEPTED", "UNKNOWN")
                                               for row in records))

    async def send(
        self, text: str, *, attribution: tuple[InputRef, ...], sequence: int | None = None,
    ) -> DeliveryOutcome:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty_delivery_segment")
        if not isinstance(attribution, tuple) or not attribution or not all(
            isinstance(ref, InputRef) for ref in attribution
        ) or len({ref.message_id for ref in attribution}) != len(attribution):
            raise ValueError("invalid_segment_attribution")
        if sequence is not None and (type(sequence) is not int or sequence < 0):
            raise ValueError("invalid_segment_sequence")
        if self._closing:
            raise RuntimeError("segment_sender_closed")
        seq = self._next_sequence if sequence is None else sequence
        self._next_sequence = max(self._next_sequence, seq + 1)
        worker = asyncio.create_task(self._send(text, attribution, seq), name="roleplay-segment-worker")
        self._workers.add(worker)
        worker.add_done_callback(lambda done: self._done(done, self._workers))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            worker.cancel()
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                pass
            raise

    async def _invoke(self, text: str, invocation: _Invocation) -> Any:
        if self._closing or not self._current():
            raise _NotDispatched()
        invocation.dispatched = True
        # No await between checking the versions and invoking the adapter.
        return await self._send_call(text)

    async def _send(self, text: str, refs: tuple[InputRef, ...], seq: int) -> DeliveryOutcome:
        claim = uuid.uuid4().hex
        invocation = _Invocation()
        call: asyncio.Task[Any] | None = None
        try:
            async with self._lock:
                if self._stopped or self._closing:
                    return DeliveryOutcome(seq, "FAILED", reason="sender_stopped")
                existing = await self.ledger._reserve(self.token, seq, refs, claim)
                if existing is not None:
                    self._stopped = True
                    if existing.get("blocked"):
                        return DeliveryOutcome(seq, "UNKNOWN" if existing["status"] in ("UNKNOWN", "IN_FLIGHT")
                                               else "FAILED", reason="batch_blocked")
                    return DeliveryOutcome(seq, existing["status"], existing["outgoing_message_id"],
                                           reason="duplicate_sequence", duplicate=True,
                                           delivery_ref=existing["claim_id"])
                if not self._current():
                    return await self._finish(seq, claim, "FAILED", "stale_before_dispatch")
                if self._last_ack is None:
                    target = self._first_not_before
                else:
                    interval = self._interval
                    if self._interval_samples is not None:
                        if not 1 <= seq <= len(self._interval_samples):
                            return await self._finish(seq, claim, "FAILED", "pacing_segment_limit")
                        interval = self._interval_samples[seq - 1]
                    target = self._last_ack + interval
                while (remaining := target - self._clock()) > 0:
                    if self._closing or not self._current():
                        return await self._finish(seq, claim, "FAILED", "stale_before_dispatch")
                    await self._sleep(min(remaining, .1) if self._interval_samples is not None
                                      or self._first_not_before else remaining)
                if self._prepare_call is not None:
                    if self._closing or not self._current():
                        return await self._finish(seq, claim, "FAILED", "stale_before_prepare")
                    await self._prepare_call(text)
                    if self._closing or not self._current():
                        return await self._finish(seq, claim, "FAILED", "stale_after_prepare")
                if not await self.ledger._transition(self.token, seq, claim, ("UNSENT",),
                                                     "IN_FLIGHT", "dispatching"):
                    self._stopped = True
                    return DeliveryOutcome(seq, "UNKNOWN", reason="claim_lost")
                call = asyncio.create_task(self._invoke(text, invocation), name="roleplay-segment-send")
                self._sends.add(call)
                call.add_done_callback(lambda done: self._done(done, self._sends))
                done, _ = await asyncio.wait({call}, timeout=self._timeout)
                if not done:
                    outcome = await self._finish(seq, claim, "UNKNOWN", "send_timeout")
                    call.cancel()
                    return outcome
                try:
                    response = call.result()
                except _NotDispatched:
                    return await self._finish(seq, claim, "FAILED", "stale_before_dispatch")
                except DefiniteSendFailure:
                    return await self._finish(seq, claim, "FAILED", "platform_rejected")
                except asyncio.CancelledError:
                    return await self._finish(seq, claim, "UNKNOWN", "send_cancelled")
                except Exception:
                    return await self._finish(seq, claim, "UNKNOWN", "transport_error")
                status, receipt = send_evidence(response, self.id_kind)
                if status == "UNKNOWN":
                    return await self._finish(seq, claim, "UNKNOWN", "missing_receipt")
                self._last_ack = self._clock()
                # Actual delivery may belong to an old epoch; record the fact only.
                current = self._current()
                evidence_reason = "api_accepted" if status == "API_ACCEPTED" else "acknowledged"
                outcome = await self._finish(seq, claim, status,
                                             evidence_reason if current else "stale_" + evidence_reason, receipt)
                return DeliveryOutcome(seq, outcome.status, outcome.outgoing_message_id,
                                       outcome.status in {"SENT", "API_ACCEPTED"} and current and not self._closing
                                       and self._current(), outcome.reason, delivery_ref=outcome.delivery_ref)
        except asyncio.CancelledError:
            self._stopped = True
            if call is not None:
                call.cancel()
            await self.ledger._transition(self.token, seq, claim, ("UNSENT", "IN_FLIGHT"),
                                         "UNKNOWN" if invocation.dispatched else "FAILED",
                                         "cancelled_in_flight" if invocation.dispatched else "cancelled_unsent")
            raise
        except Exception:
            self._stopped = True
            if call is not None:
                call.cancel()
            await self.ledger._transition(self.token, seq, claim, ("UNSENT", "IN_FLIGHT"),
                                         "UNKNOWN" if invocation.dispatched else "FAILED", "worker_error")
            raise

    async def _finish(
        self, seq: int, claim: str, status: str, reason: str, receipt: str | None = None,
    ) -> DeliveryOutcome:
        if status not in {"SENT", "API_ACCEPTED"}:
            self._stopped = True
        changed = await self.ledger._transition(self.token, seq, claim, ("UNSENT", "IN_FLIGHT"),
                                                status, reason, receipt)
        if not changed:
            self._stopped = True
            return DeliveryOutcome(seq, "UNKNOWN", reason="claim_lost")
        return DeliveryOutcome(seq, status, receipt, reason=reason,
                               delivery_ref=claim if status in {"SENT", "API_ACCEPTED"} else None)

    async def close(self) -> None:
        """Stop new sends and settle workers; the caller owns ledger.close()."""
        self._closing = True
        async with self._close_lock:
            if self._closed:
                return
            workers = tuple(self._workers)
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            sends = tuple(self._sends)
            for call in sends:
                call.cancel()
            if sends:
                await asyncio.wait(sends, timeout=self._close_timeout)
            self._closed = True
