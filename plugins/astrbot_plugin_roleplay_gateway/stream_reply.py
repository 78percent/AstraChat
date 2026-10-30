"""Bounded provider-to-segment pipeline used by explicit neutral_stream mode.

No file reads, personas, history writes, network creation or fallback routing.
The producer is normally a closure over FailFastOpenAI.consume. The sink is a
closure over SegmentSender.send with explicit input attribution. A successful
OneBot receipt is a server acknowledgement, never a client-visible latency proof.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import math

from .provider_gate import AttemptResult
from .segment_buffer import TextSegmenter
from .segment_delivery import DeliveryOutcome


class ReplyStopped(RuntimeError):
    """Static diagnostic only; never includes prompt or response text."""


@dataclass(frozen=True)
class ReplyLimits:
    first_receipt_seconds: float = 4.0
    generation_seconds: float = 25.0
    total_seconds: float = 30.0
    cleanup_seconds: float = 2.0
    flush_seconds: float = 0.8
    segment_characters: int = 120
    queue_items: int = 16
    max_delta_characters: int = 8192
    max_reply_characters: int = 24000

    def __post_init__(self):
        for value in (self.first_receipt_seconds, self.generation_seconds,
                      self.total_seconds, self.cleanup_seconds, self.flush_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('invalid_reply_limit')
        for value in (self.segment_characters, self.queue_items,
                      self.max_delta_characters, self.max_reply_characters):
            if type(value) is not int or value < 1:
                raise ValueError('invalid_reply_limit')
        if not self.first_receipt_seconds <= self.generation_seconds <= self.total_seconds:
            raise ValueError('invalid_deadline_order')


@dataclass(frozen=True)
class ConfirmedSegment:
    sequence: int
    text: str
    outgoing_message_id: str | None
    commit_allowed: bool
    delivery_status: str = "SENT"
    delivery_ref: str | None = None


@dataclass(frozen=True)
class ReplyResult:
    reason: str
    complete: bool
    sent: tuple[ConfirmedSegment, ...]
    provider_result: AttemptResult | None
    first_receipt_seconds: float | None
    elapsed_seconds: float
    coordinator_tasks_settled: bool = True

    @property
    def partial(self) -> bool:
        return bool(self.sent) and not self.complete


Producer = Callable[[Callable[[str], Awaitable[None]]], Awaitable[AttemptResult]]
Sink = Callable[[str], Awaitable[DeliveryOutcome]]


class StreamReply:
    """One active reply per coordinator; unresolved cleanup blocks reuse.

    The first-receipt deadline is conservative: it covers model and send, not
    just TTFT. A timed-out in-flight send is UNKNOWN in the sink and cannot be
    retried. This class does not select alternate providers or claim fallback
    is safe. coordinator_tasks_settled covers only tasks owned here, not
    lower-level provider transports or the sink's pending_sends. Callers must retain the SegmentSender and check its ledger.
    """

    def __init__(self, limits: ReplyLimits | None = None, *, presegmented: bool = False):
        self.limits = limits or ReplyLimits()
        self.presegmented = presegmented
        self._busy = False
        self._blocked = False
        self._owned: set[asyncio.Task] = set()

    @property
    def pending_tasks(self) -> int:
        return sum(not task.done() for task in self._owned)

    def _task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._owned.add(task)
        def done(item):
            self._owned.discard(item)
            if not item.cancelled():
                item.exception()
        task.add_done_callback(done)
        return task

    async def run(self, produce: Producer, send: Sink, *,
                  is_current: Callable[[], bool]) -> ReplyResult:
        if self._busy or self._blocked:
            raise ReplyStopped('coordinator_busy_or_cleanup_unsettled')
        self._busy = True
        loop = asyncio.get_running_loop()
        start = loop.time()
        limits = self.limits
        queue: asyncio.Queue[str] = asyncio.Queue(limits.queue_items)
        active = True
        generation_expired = False
        total_characters = 0
        confirmed: list[ConfirmedSegment] = []
        first_receipt: float | None = None
        provider_result: AttemptResult | None = None
        result: ReplyResult | None = None

        def current() -> bool:
            try:
                return is_current() is True
            except Exception:
                return False

        async def delta(text: str) -> None:
            nonlocal total_characters
            if not active or not current():
                raise ReplyStopped('stale_delta')
            if not isinstance(text, str):
                raise ReplyStopped('invalid_delta')
            if len(text) > limits.max_delta_characters:
                raise ReplyStopped('delta_too_large')
            total_characters += len(text)
            if total_characters > limits.max_reply_characters:
                raise ReplyStopped('reply_too_large')
            if text:
                await queue.put(text)
                if not active or not current():
                    raise ReplyStopped('stale_delta')

        def outcome(reason: str, complete: bool = False) -> ReplyResult:
            # Returned text is only a candidate for history: the transaction
            # that commits it MUST recheck its epoch, including after return.
            valid = current()
            sent = tuple(replace(s, commit_allowed=s.commit_allowed and valid) for s in confirmed)
            return ReplyResult(reason, complete and valid, sent, provider_result,
                               first_receipt, loop.time() - start)

        producer: asyncio.Task | None = None
        reader: asyncio.Task | None = None
        send_task: asyncio.Task | None = None
        try:
            if not current():
                result = outcome('stale_before_start')
            else:
                producer = self._task(produce(delta))
                async def generation_watchdog():
                    nonlocal active, generation_expired
                    await asyncio.sleep(max(0.0, start + limits.generation_seconds - loop.time()))
                    if not producer.done():
                        active = False
                        generation_expired = True
                        if producer.cancelling() == 0:
                            producer.cancel()
                self._task(generation_watchdog())
                buffer = TextSegmenter(max_characters=limits.segment_characters,
                                       max_wait_seconds=limits.flush_seconds)
                ready: deque[str] = deque()
                eof = False
                winding_down = False
                while result is None:
                    now = loop.time()
                    if not current():
                        result = outcome('stale_attempt_or_epoch')
                        break
                    if now >= start + limits.total_seconds:
                        result = outcome('total_deadline')
                        break
                    if first_receipt is None and now >= start + limits.first_receipt_seconds:
                        result = outcome('first_receipt_deadline')
                        break
                    if generation_expired and not eof and not winding_down:
                        winding_down = True
                        if reader is not None:
                            reader.cancel()
                            reader = None
                    if not eof and not winding_down and producer.done():
                        active = False
                        if producer.cancelled():
                            result = outcome('provider_cancelled')
                            break
                        if producer.exception() is not None:
                            result = outcome('provider_failed')
                            break
                        value = producer.result()
                        if (not isinstance(value, AttemptResult)
                                or (not value.final_seen and value.stop_reason != 'policy_limit')):
                            result = outcome('provider_incomplete')
                            break
                        provider_result = value
                        eof = True
                    if not eof and not winding_down and now >= start + limits.generation_seconds:
                        # Stop generation at 25s. Only already formed segments
                        # drain until 30s; no incomplete tail is invented/flushed.
                        active = False
                        winding_down = True
                        if producer.cancelling() == 0:
                            producer.cancel()
                        if reader is not None:
                            reader.cancel()
                            reader = None
                    if ready:
                        text = ready.popleft()
                        send_task = self._task(send(text))
                        deadline = start + limits.total_seconds
                        if first_receipt is None:
                            deadline = min(deadline, start + limits.first_receipt_seconds)
                        done, _ = await asyncio.wait({send_task}, timeout=max(0.0, deadline - loop.time()))
                        if not done:
                            send_task.cancel()
                            result = outcome('send_deadline_unknown')
                            break
                        if send_task.cancelled() or send_task.exception() is not None:
                            result = outcome('send_failed_or_unknown')
                            break
                        receipt = send_task.result()
                        if not isinstance(receipt, DeliveryOutcome):
                            result = outcome('invalid_delivery_outcome')
                            break
                        if receipt.duplicate:
                            result = outcome('duplicate_delivery')
                            break
                        if not receipt.has_success_evidence:
                            result = outcome('send_' + (receipt.status.lower() if receipt.status in {'FAILED', 'UNKNOWN', 'ABORTED', 'DUPLICATE', 'BLOCKED'} else 'unconfirmed'))
                            break
                        confirmed.append(ConfirmedSegment(receipt.sequence, text,
                                                          receipt.outgoing_message_id,
                                                          receipt.commit_allowed,
                                                          receipt.status, receipt.delivery_ref))
                        if first_receipt is None:
                            first_receipt = loop.time() - start
                        if not receipt.commit_allowed or not current():
                            result = outcome('stale_after_send')
                            break
                        send_task = None
                        continue
                    if winding_down:
                        result = outcome('generation_deadline')
                        break
                    # Keep one queue reader alive across timer flushes; cancelling
                    # and recreating it at each tick can lose a raced delta.
                    if reader is None and not (eof and queue.empty()):
                        reader = self._task(queue.get())
                    if reader is not None and reader.done():
                        if self.presegmented:
                            ready.append(reader.result())
                        else:
                            ready.extend(buffer.feed(reader.result(), now))
                        reader = None
                        continue
                    if eof and queue.empty():
                        if reader is not None:
                            reader.cancel()
                            reader = None
                        if not self.presegmented:
                            ready.extend(buffer.finish())
                        if ready:
                            continue
                        policy_limited = provider_result is not None and provider_result.stop_reason == 'policy_limit'
                        result = outcome('policy_limit' if policy_limited else
                                         ('complete' if confirmed else 'empty_output'),
                                         bool(confirmed) and not policy_limited)
                        break
                    if not self.presegmented:
                        ready.extend(buffer.poll(now))
                    if ready:
                        continue
                    deadline = start + limits.total_seconds
                    if not eof:
                        deadline = min(deadline, start + limits.generation_seconds)
                    if first_receipt is None:
                        deadline = min(deadline, start + limits.first_receipt_seconds)
                    if buffer.next_due is not None:
                        deadline = min(deadline, buffer.next_due)
                    waiting = {t for t in (reader, producer if not eof else None) if t is not None}
                    await asyncio.wait(waiting, timeout=max(0.0, deadline - loop.time()), return_when=asyncio.FIRST_COMPLETED)
        finally:
            active = False
            pending = [task for task in tuple(self._owned) if not task.done()]
            for task in pending:
                # Cancellation is delivered once; repeated cancellation can
                # interrupt the provider/sender's own cleanup and ledger fence.
                if task.cancelling() == 0:
                    task.cancel()
            try:
                if pending:
                    _, unresolved = await asyncio.wait(pending, timeout=limits.cleanup_seconds)
                    if unresolved:
                        self._blocked = True
                        if result is not None:
                            result = replace(result, complete=False, reason='cleanup_unsettled', coordinator_tasks_settled=False)
            except asyncio.CancelledError:
                self._blocked = True
                raise
            finally:
                self._busy = False
        assert result is not None
        return replace(result, elapsed_seconds=loop.time() - start)
