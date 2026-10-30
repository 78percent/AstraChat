"""Synthetic streaming contracts; no AstrBot runtime, keys, QQ or paid API."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import AttemptResult
from plugins.astrbot_plugin_roleplay_gateway.segment_delivery import (
    AttemptToken, DeliveryOutcome, InputRef, SegmentLedger, SegmentSender,
)
from plugins.astrbot_plugin_roleplay_gateway.stream_reply import ReplyLimits, ReplyStopped, StreamReply


def final(chars=10, seen=True):
    return AttemptResult(1, chars, seen, 'fixture', ())


def ack(sequence, status='SENT', commit=True):
    return DeliveryOutcome(sequence, status, str(100 + sequence) if status == 'SENT' else None,
                           commit, 'fixture')


def limits(**changes):
    return replace(ReplyLimits(first_receipt_seconds=.4, generation_seconds=.8,
                               total_seconds=1.2, cleanup_seconds=.15, flush_seconds=.02), **changes)


class StreamReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Windows asyncio debug stack capture can exceed synthetic 20ms clocks.
        # Lifecycle is asserted explicitly; normal runtime uses debug=False.
        asyncio.get_running_loop().set_debug(False)

    async def test_api_accepted_segments_preserve_evidence_without_platform_ids(self):
        output = []
        async def produce(delta):
            await delta("第一句。")
            await delta("第二句。")
            return final()
        async def send(text):
            output.append(text)
            return DeliveryOutcome(len(output) - 1, "API_ACCEPTED", None, True,
                                   delivery_ref=f"local-{len(output)}")
        result = await StreamReply(limits(), presegmented=True).run(produce, send, is_current=lambda: True)
        self.assertTrue(result.complete)
        self.assertEqual([value.delivery_status for value in result.sent], ["API_ACCEPTED", "API_ACCEPTED"])
        self.assertTrue(all(value.outgoing_message_id is None for value in result.sent))
        self.assertEqual([value.delivery_ref for value in result.sent], ["local-1", "local-2"])

    async def test_api_acceptance_requires_local_ref_and_empty_platform_id(self):
        for malformed in (DeliveryOutcome(0, "API_ACCEPTED", None, True),
                          DeliveryOutcome(0, "API_ACCEPTED", "123", True, delivery_ref="local")):
            async def produce(delta):
                await delta("中性句子。")
                return final()
            async def send(text):
                return malformed
            result = await StreamReply(limits()).run(produce, send, is_current=lambda: True)
            self.assertFalse(result.complete)
            self.assertEqual(result.sent, ())
            self.assertEqual(result.reason, "send_unconfirmed")

    async def test_sentence_sent_before_full_generation_and_no_terminal_duplicate(self):
        sent_first = asyncio.Event()
        output = []
        async def produce(delta):
            await delta('第一句。')
            await asyncio.wait_for(sent_first.wait(), .3)
            await delta('第二句。')
            return final()
        async def send(text):
            output.append(text)
            sent_first.set()
            return ack(len(output))
        runner = StreamReply(limits())
        result = await runner.run(produce, send, is_current=lambda: True)
        self.assertTrue(result.complete)
        self.assertEqual(''.join(output), '第一句。第二句。')
        self.assertEqual(''.join(x.text for x in result.sent), ''.join(output))
        self.assertEqual(runner.pending_tasks, 0)

    async def test_timer_flush_without_further_provider_delta(self):
        sent = asyncio.Event()
        output = []
        async def produce(delta):
            await delta('无标点但已有正文')
            await asyncio.wait_for(sent.wait(), .3)
            return final()
        async def send(text):
            output.append(text)
            sent.set()
            return ack(len(output))
        result = await StreamReply(limits()).run(produce, send, is_current=lambda: True)
        self.assertTrue(result.complete)
        self.assertEqual(output, ['无标点但已有正文'])

    async def test_provider_disconnect_keeps_only_confirmed_segments(self):
        sent = asyncio.Event()
        output = []
        async def produce(delta):
            await delta('已经送达。')
            await sent.wait()
            await delta('未完整续文')
            raise OSError('private provider detail')
        async def send(text):
            output.append(text)
            sent.set()
            return ack(len(output))
        result = await StreamReply(limits()).run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'provider_failed')
        self.assertTrue(result.partial)
        self.assertEqual(output, ['已经送达。'])
        self.assertNotIn('private', repr(result))

    async def test_unknown_stops_all_later_segments(self):
        output = []
        async def produce(delta):
            await delta('第一句。第二句。第三句。')
            return final()
        async def send(text):
            output.append(text)
            return ack(len(output), 'UNKNOWN', False)
        result = await StreamReply(limits()).run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'send_unknown')
        self.assertEqual(len(output), 1)
        self.assertEqual(result.sent, ())

    async def test_first_deadline_cancels_silent_provider_and_never_sends_placeholder(self):
        stopped = asyncio.Event()
        async def produce(delta):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def send(text):
            self.fail('no placeholder allowed')
        runner = StreamReply(limits(first_receipt_seconds=.25))
        result = await runner.run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'first_receipt_deadline')
        self.assertTrue(stopped.is_set())
        self.assertFalse(result.complete)
        self.assertEqual(runner.pending_tasks, 0)

    async def test_first_deadline_during_send_cancels_sink(self):
        cancelled = asyncio.Event()
        async def produce(delta):
            await delta('正文。')
            return final()
        async def send(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        result = await StreamReply(limits(first_receipt_seconds=.25)).run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'send_deadline_unknown')
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.sent, ())

    async def test_generation_deadline_does_not_flush_incomplete_tail(self):
        sent = asyncio.Event()
        output = []
        async def produce(delta):
            await delta('完整句。')
            await sent.wait()
            await delta('应丢弃的不完整尾巴')
            await asyncio.Event().wait()
        async def send(text):
            output.append(text)
            sent.set()
            return ack(len(output))
        result = await StreamReply(limits(first_receipt_seconds=.25, generation_seconds=.5,
                                         flush_seconds=1)).run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'generation_deadline')
        self.assertEqual(output, ['完整句。'])
        self.assertTrue(result.partial)

    async def test_generation_deadline_runs_while_second_send_waits(self):
        stopped = asyncio.Event()
        started_second = asyncio.Event()
        calls = []
        async def produce(delta):
            try:
                await delta('第一句。第二句。')
                await started_second.wait()
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def send(text):
            calls.append(text)
            if len(calls) == 2:
                started_second.set()
                await stopped.wait()
            return ack(len(calls))
        result = await StreamReply(limits(first_receipt_seconds=.2, generation_seconds=.4)).run(
            produce, send, is_current=lambda: True)
        self.assertTrue(stopped.is_set())
        self.assertEqual(result.reason, 'generation_deadline')
        self.assertEqual(len(result.sent), 2)

    async def test_stale_epoch_inflight_keeps_delivery_fact_not_history_candidate(self):
        current = True
        async def produce(delta):
            await delta('先前开始的正文。后续内容。')
            return final()
        async def send(text):
            nonlocal current
            current = False
            return ack(1, commit=False)
        result = await StreamReply(limits()).run(produce, send, is_current=lambda: current)
        self.assertEqual(result.reason, 'stale_after_send')
        self.assertEqual(len(result.sent), 1)
        self.assertFalse(result.sent[0].commit_allowed)
        self.assertFalse(result.complete)

    async def test_oversized_delta_fails_without_sending(self):
        async def produce(delta):
            await delta('过长内容')
            return final()
        async def send(text):
            self.fail('oversized delta must not be sent')
        result = await StreamReply(limits(max_delta_characters=2)).run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'provider_failed')
        self.assertFalse(result.sent)

    async def test_eof_race_drains_all_queued_deltas_exactly_once(self):
        source = '中性测试文本没有句末符号' * 40
        output = []
        async def produce(delta):
            for char in source:
                await delta(char)
            return final(len(source))
        async def send(text):
            output.append(text)
            await asyncio.sleep(0)
            return ack(len(output))
        result = await StreamReply(limits(queue_items=2, segment_characters=12)).run(produce, send, is_current=lambda: True)
        self.assertTrue(result.complete)
        self.assertEqual(''.join(output), source)

    async def test_uncooperative_provider_blocks_coordinator_reuse(self):
        release = asyncio.Event()
        late_delta_blocked = asyncio.Event()
        async def produce(delta):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            try:
                await delta('不得迟发。')
            except ReplyStopped:
                late_delta_blocked.set()
            return final()
        async def send(text):
            self.fail('late data cannot send')
        runner = StreamReply(limits(first_receipt_seconds=.25, cleanup_seconds=.1))
        result = await runner.run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'cleanup_unsettled')
        self.assertFalse(result.coordinator_tasks_settled)
        with self.assertRaises(ReplyStopped):
            await runner.run(produce, send, is_current=lambda: True)
        release.set()
        await asyncio.wait_for(late_delta_blocked.wait(), .3)
        await asyncio.sleep(0)
        self.assertEqual(runner.pending_tasks, 0)

    async def test_external_cancel_settles_provider(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def produce(delta):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        async def send(text):
            self.fail('no output')
        runner = StreamReply(limits())
        task = asyncio.create_task(runner.run(produce, send, is_current=lambda: True))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(runner.pending_tasks, 0)

    async def test_incomplete_provider_final_does_not_flush_draft(self):
        async def produce(delta):
            await delta('未完成草稿')
            return final(seen=False)
        async def send(text):
            self.fail('draft cannot be considered complete')
        result = await StreamReply(limits()).run(produce, send, is_current=lambda: True)
        self.assertEqual(result.reason, 'provider_incomplete')
        self.assertFalse(result.sent)

    async def test_stale_before_start_does_not_call_provider(self):
        async def produce(delta):
            self.fail('stale request')
        async def send(text):
            self.fail('stale request')
        result = await StreamReply(limits()).run(produce, send, is_current=lambda: False)
        self.assertEqual(result.reason, 'stale_before_start')




class LedgerPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = SegmentLedger(Path(self.temp.name) / 'segments.sqlite3')
        await self.ledger.open()
        self.token = AttemptToken('fixture', 'group:200', 'batch', 0, 0)
        self.refs = (InputRef('1', '100'),)
        self.senders = []

    async def asyncTearDown(self):
        for sender in self.senders:
            await sender.close()
        await self.ledger.close()
        self.temp.cleanup()

    def sender(self, call, **kwargs):
        item = SegmentSender(self.ledger, self.token, call, lambda token: True,
                             segment_interval=.01, **kwargs)
        self.senders.append(item)
        return item

    async def test_stream_with_real_ledger_keeps_receipt_per_segment(self):
        output = []
        async def call(text):
            output.append(text)
            return {'message_id': 200 + len(output)}
        sender = self.sender(call)
        async def produce(delta):
            await delta('一个完整短句。')
            await asyncio.sleep(.005)
            await delta('另一个完整短句。')
            return final()
        result = await StreamReply(limits(first_receipt_seconds=1, generation_seconds=2, total_seconds=3)).run(
            produce, lambda text: sender.send(text, attribution=self.refs), is_current=lambda: True)
        self.assertTrue(result.complete)
        rows = await self.ledger.records(self.token)
        self.assertEqual(len(rows), len(output))
        self.assertTrue(all(row['status'] == 'SENT' for row in rows))
        self.assertEqual([s.outgoing_message_id for s in result.sent],
                         [row['outgoing_message_id'] for row in rows])
        self.assertFalse(await sender.can_fallback())
        self.assertEqual(''.join(output), '一个完整短句。另一个完整短句。')

    async def test_late_ack_stays_unknown_and_never_releases_next_segment(self):
        release = asyncio.Event()
        late_finished = asyncio.Event()
        calls = []
        async def call(text):
            calls.append(text)
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            late_finished.set()
            return {'message_id': 300}
        sender = self.sender(call, send_timeout=.1, close_timeout=.5)
        async def produce(delta):
            await delta('第一句。第二句。')
            return final()
        result = await StreamReply(limits(first_receipt_seconds=2, generation_seconds=3, total_seconds=4,
                                         cleanup_seconds=1)).run(
            produce, lambda text: sender.send(text, attribution=self.refs), is_current=lambda: True)
        self.assertEqual(result.reason, 'send_unknown')
        self.assertEqual(len(calls), 1)
        self.assertFalse(await sender.can_fallback())
        release.set()
        await asyncio.wait_for(late_finished.wait(), .3)
        await asyncio.sleep(0)
        rows = await self.ledger.records(self.token)
        self.assertEqual(rows[0]['status'], 'UNKNOWN')
        self.assertIsNone(rows[0]['outgoing_message_id'])
        self.assertEqual(result.sent, ())

    async def test_duplicate_sent_receipt_not_counted_as_new_response(self):
        calls = []
        async def call(text):
            calls.append(text)
            return {'message_id': 400}
        first = self.sender(call)
        await first.send('已送达的旧段。', attribution=self.refs, sequence=0)
        second = self.sender(call)
        async def produce(delta):
            await delta('不能重复发送。')
            return final()
        result = await StreamReply(limits()).run(
            produce, lambda text: second.send(text, attribution=self.refs, sequence=0), is_current=lambda: True)
        self.assertEqual(result.reason, 'duplicate_delivery')
        self.assertEqual(calls, ['已送达的旧段。'])
        self.assertEqual(result.sent, ())


    async def test_existing_provider_adapter_to_buffer_and_ledger(self):
        from unittest.mock import patch
        from test_provider_gate import Backend, FakeClient, FakeProvider, FakeStream, response
        from plugins.astrbot_plugin_roleplay_gateway import provider_gate as pg
        backend = Backend(FakeStream([
            response("第一句。"), response("第二句。"),
            response("第一句。第二句。", chunk=False),
        ]))
        provider = FakeProvider(backend)
        output = []
        async def call(text):
            output.append(text)
            return {'message_id': 500 + len(output)}
        sender = self.sender(call)
        parts = pg.PromptParts(runtime_boundary='Neutral integration test only.', persona='',
                               current=(pg.SpeakerInput('100', 'fixture', '1', 'neutral input'),))
        with patch.object(pg, '_runtime_contract', return_value=(
                pg.ASTRBOT_VERSION, pg.OPENAI_VERSION, FakeProvider, FakeClient)):
            adapter = pg.FailFastOpenAI(provider)
            async def produce(delta):
                return await adapter.consume(parts, delta, timeout_seconds=2)
            result = await StreamReply(limits(first_receipt_seconds=1, generation_seconds=2,
                                             total_seconds=3)).run(
                produce, lambda text: sender.send(text, attribution=self.refs), is_current=lambda: True)
        self.assertTrue(result.complete)
        self.assertEqual(''.join(output), '第一句。第二句。')
        self.assertEqual(len(backend.calls), 1)
        self.assertTrue(backend.stream.close_completed)
        self.assertFalse(provider.client.closed)
        self.assertTrue(all(row['status'] == 'SENT' for row in await self.ledger.records(self.token)))


if __name__ == '__main__':
    unittest.main()
