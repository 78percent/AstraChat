"""Neutral fixtures, temporary SQLite and fake network boundaries only."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.gate import (
    DefiniteSendFailure, Envelope, GateSettings, ReceiptStore,
    ApiAccepted,
)
from plugins.astrbot_plugin_roleplay_gateway.neutral_stream import (
    NEUTRAL_SYSTEM_PROMPT, NeutralStreamService, StreamSafetyGuard,
)
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import AttemptResult
from plugins.astrbot_plugin_roleplay_gateway.segment_delivery import SegmentLedger
from plugins.astrbot_plugin_roleplay_gateway.stream_reply import ReplyLimits


class FakeAdapter:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.calls = []
        self.pending_tasks = 0
        self.reusable = True

    async def consume(self, parts, on_delta, *, timeout_seconds):
        self.calls.append((parts, timeout_seconds))
        if self.behavior:
            return await self.behavior(parts, on_delta)
        await on_delta('中性答案。')
        return AttemptResult(1, 5, True, 'fixture', ())


def event(incoming='1', *, group='200', text='机器人你好', **changes):
    return replace(Envelope('fixture-qq', '900', '100', incoming, 'group',
                            group_id=group, text=text, mentions=('900',)), **changes)


class NeutralStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix='neutral-stream-', dir=ROOT / 'tests')
        self.base = Path(self.temp.name)
        self.store = ReceiptStore(self.base / 'receipts.sqlite3')
        self.segments = SegmentLedger(self.base / 'segments.sqlite3')
        await self.store.open()
        await self.segments.open()
        self.config = GateSettings(enabled=True, mode='neutral_stream', platform_id='fixture-qq',
                                   bot_self_id='900', admin_private_ids=frozenset({'100'}),
                                   allowed_group_ids=frozenset({'200', '201', '202', '203'}),
                                   wake_names=('机器人',), send_timeout_seconds=.3)
        self.limits = ReplyLimits(first_receipt_seconds=1, generation_seconds=2,
                                 total_seconds=3, cleanup_seconds=.25, flush_seconds=.05)
        self.adapters = []
        self.behavior = None
        self.enabled = True
        self.outputs = []
        self.logs = []
        self.guard = StreamSafetyGuard()
        self.service = self.make_service()
        self.releases = []

    def factory(self):
        adapter = FakeAdapter(self.behavior)
        self.adapters.append(adapter)
        return adapter

    def make_service(self, **changes):
        options = dict(emit=self.logs.append, enabled=lambda: self.enabled,
                       limits=self.limits, segment_interval=.001, guard=self.guard)
        options.update(changes)
        return NeutralStreamService(self.config, self.store, self.segments, self.factory, **options)

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        await asyncio.sleep(.02)
        try:
            await self.service.close()
        finally:
            # Tests intentionally exercising retained guards close their fake
            # work before deleting temporary files; no production ledger exists.
            await self.store.close()
            await self.segments.close()
            self.temp.cleanup()

    async def send(self, item, text):
        self.outputs.append((item.incoming_message_id, text))
        return {'message_id': 1000 + len(self.outputs)}

    async def dispatch(self, item=None, sender=None):
        admitted = await self.service.accept(item or event(), sender or self.send)
        await asyncio.wait_for(self.service.wait_idle(), 5)
        return admitted

    async def row(self, incoming='1'):
        return await self.store.get('fixture-qq', incoming)

    async def test_single_input_fixed_neutral_prompt_and_metadata_only(self):
        await self.dispatch()
        parts, timeout = self.adapters[0].calls[0]
        self.assertEqual(parts.runtime_boundary, NEUTRAL_SYSTEM_PROMPT)
        self.assertEqual(parts.persona, '')
        self.assertEqual((parts.summary, parts.recent, parts.examples,
                          parts.before_worldbook, parts.after_worldbook), ('', (), (), (), ()))
        self.assertEqual(parts.current[0].display_name, '100')
        self.assertEqual(parts.current[0].message_id, '1')
        self.assertEqual(timeout, 2)
        self.assertEqual(self.outputs, [('1', '中性答案。')])
        row = await self.row()
        self.assertEqual((row['status'], row['outgoing_message_id']), ('SENT', '1001'))
        rows = await self.segments.records(self.service._token(event()))
        self.assertEqual(rows[0]['attribution_json'], '[["1", "100"]]')
        self.assertNotIn('机器人你好', repr(self.logs))
        self.assertNotIn('中性答案', repr(self.logs))
        self.assertNotIn('中性答案', repr(rows))

    async def test_private_uses_same_pipeline_and_dedup(self):
        item = event(kind='private', group_id=None, mentions=(), text='中性问题')
        self.assertEqual(await self.dispatch(item), 'ACCEPTED')
        self.assertEqual(await self.dispatch(item), 'DUPLICATE')
        self.assertEqual(len(self.outputs), 1)
        self.assertEqual(sum(len(x.calls) for x in self.adapters), 1)

    async def test_no_wake_and_unauthorized_never_request(self):
        self.assertEqual(await self.dispatch(event(mentions=(), text='普通闲聊')), 'IGNORED')
        self.assertEqual(await self.dispatch(event(group='999')), 'IGNORED')
        self.assertEqual(await self.dispatch(event(kind='private', sender_id='101')), 'IGNORED')
        self.assertEqual(self.adapters, [])
        self.assertEqual(self.outputs, [])

    async def test_empty_and_oversized_input_rejected_without_model(self):
        for mid, text in [('1', ' \n'), ('2', '字' * 2001)]:
            self.assertEqual(await self.dispatch(event(mid, text=text)), 'REJECTED')
            row = await self.row(mid)
            self.assertEqual((row['status'], row['reason']), ('REJECTED', 'invalid_stream_input'))
        self.assertFalse(self.adapters)

    async def test_provider_failure_no_placeholder_or_retry(self):
        async def fail(parts, delta):
            raise OSError('must-not-leak-private-detail')
        self.behavior = fail
        await self.dispatch()
        self.assertEqual((await self.row())['status'], 'FAILED')
        self.assertEqual(self.outputs, [])
        self.assertEqual(sum(len(a.calls) for a in self.adapters), 1)
        self.assertNotIn('must-not-leak', repr(self.logs))
        self.assertFalse(self.service.blocked)

    async def test_partial_stream_records_only_confirmed_segments(self):
        first = asyncio.Event()
        async def produce(parts, delta):
            await delta('已发送第一段。')
            await first.wait()
            raise OSError('stream broke')
        async def send(item, text):
            result = await self.send(item, text)
            first.set()
            return result
        self.behavior = produce
        await self.dispatch(sender=send)
        self.assertEqual((await self.row())['status'], 'PARTIAL')
        self.assertEqual(self.outputs, [('1', '已发送第一段。')])
        self.assertEqual(sum(len(a.calls) for a in self.adapters), 1)

    async def test_definite_send_rejection_is_failed_no_retry(self):
        async def reject(item, text):
            raise DefiniteSendFailure()
        await self.dispatch(sender=reject)
        self.assertEqual((await self.row())['status'], 'FAILED')
        self.assertFalse(self.service.blocked)

    async def test_transport_failure_is_unknown_even_without_sent_segment(self):
        async def broken(item, text):
            raise ConnectionError('private network string')
        await self.dispatch(sender=broken)
        self.assertEqual((await self.row())['status'], 'UNKNOWN')
        self.assertNotIn('private network', repr(self.logs))
        self.assertEqual(await self.dispatch(), 'DUPLICATE')

    async def test_disabled_while_model_running_does_not_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        async def produce(parts, delta):
            entered.set()
            await release.wait()
            await delta('不应发送。')
            return AttemptResult(1, 5, True, 'fixture', ())
        self.behavior = produce
        await self.service.accept(event(), self.send)
        await entered.wait()
        self.enabled = False
        release.set()
        await self.service.wait_idle()
        self.assertEqual(self.outputs, [])
        self.assertEqual((await self.row())['status'], 'FAILED')

    async def test_same_session_serial_and_other_session_can_run(self):
        first, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        observed = []
        async def produce(parts, delta):
            mid = parts.current[0].message_id
            observed.append(mid)
            if mid == '1':
                first.set()
                await release.wait()
            await delta('回答。')
            return AttemptResult(1, 3, True, 'fixture', ())
        self.behavior = produce
        await self.service.accept(event('1'), self.send)
        await first.wait()
        await self.service.accept(event('2'), self.send)
        await self.service.accept(event('3', group='201'), self.send)
        for _ in range(50):
            if '3' in observed:
                break
            await asyncio.sleep(.005)
        self.assertIn('3', observed)
        self.assertNotIn('2', observed)
        release.set()
        await self.service.wait_idle()
        self.assertEqual(observed, ['1', '3', '2'])
        self.assertEqual(len(self.adapters), 2)

    async def test_cross_session_concurrency_capped_at_three(self):
        release = asyncio.Event()
        self.releases.append(release)
        entered = []
        async def produce(parts, delta):
            entered.append(parts.current[0].message_id)
            await release.wait()
            await delta('回答。')
            return AttemptResult(1, 3, True, 'fixture', ())
        self.behavior = produce
        for n in range(4):
            await self.service.accept(event(str(n+1), group=str(200+n)), self.send)
        for _ in range(50):
            if len(entered) >= 3:
                break
            await asyncio.sleep(.005)
        self.assertEqual(len(entered), 3)
        self.assertEqual(len(self.adapters), 3)
        release.set()
        await self.service.wait_idle()
        self.assertEqual(len(entered), 4)
        self.assertEqual(len(self.adapters), 3)

    async def test_qq_and_weixin_share_three_slots_through_send_wait(self):
        shared = asyncio.Semaphore(3)
        self.config = replace(self.config, send_timeout_seconds=2)
        self.limits = replace(self.limits, first_receipt_seconds=3, generation_seconds=4, total_seconds=5)
        self.service = self.make_service(shared_parallel=shared)
        store = ReceiptStore(self.base / "weixin" / "receipts.sqlite3")
        segments = SegmentLedger(self.base / "weixin" / "segments.sqlite3")
        await store.open()
        await segments.open()
        configured = replace(self.config, id_kind="weixin", platform_id="fixture-wechat", bot_self_id="fixture-wechat",
                             admin_private_ids=frozenset({"wx-a", "wx-b"}), allowed_group_ids=frozenset())
        release, three = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        entered = []
        async def produce(parts, delta):
            entered.append(parts.current[0].message_id)
            if len(entered) == 3:
                three.set()
            await delta("中性回复。")
            return AttemptResult(1, 5, True, "fixture", ())
        self.behavior = produce
        service = NeutralStreamService(configured, store, segments, self.factory,
                                       limits=self.limits, segment_interval=.001, shared_parallel=shared)
        self.assertIs(service._parallel, self.service._parallel)
        async def send_wait(item, text):
            await release.wait()
            return ApiAccepted() if item.platform_id == "fixture-wechat" else {"message_id": 51}
        try:
            for item in (event("1", group="200"), event("2", group="201")):
                await self.service.accept(item, send_wait)
            for user in ("wx-a", "wx-b"):
                await service.accept(Envelope("fixture-wechat", "fixture-wechat", user, user + ":msg", "private", text="中性问题"), send_wait)
            await asyncio.wait_for(three.wait(), 2)
            await asyncio.sleep(.03)
            self.assertEqual(len(entered), 3)
            release.set()
            await asyncio.gather(self.service.wait_idle(), service.wait_idle())
            self.assertEqual(len(entered), 4)
            self.assertEqual(shared._value, 3)
        finally:
            release.set()
            await service.close()

    async def test_old_message_expires_without_request(self):
        await self.dispatch(event(received_at=time.time()-31))
        self.assertEqual((await self.row())['reason'], 'queue_expired')
        self.assertEqual(self.adapters, [])

    async def test_waiting_for_session_lock_expires(self):
        self.service.queue_timeout = .08
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        async def produce(parts, delta):
            entered.set()
            await release.wait()
            await delta('回答。')
            return AttemptResult(1, 3, True, 'fixture', ())
        self.behavior = produce
        await self.service.accept(event('1'), self.send)
        await entered.wait()
        await self.service.accept(event('2'), self.send)
        await asyncio.sleep(.15)
        self.assertEqual((await self.row('2'))['reason'], 'queue_expired')
        self.assertEqual(sum(len(a.calls) for a in self.adapters), 1)
        release.set()
        await self.service.wait_idle()

    async def test_shutdown_cancels_provider_before_closing_databases(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def produce(parts, delta):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.behavior = produce
        await self.service.accept(event(), self.send)
        await entered.wait()
        await self.service.close()
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.service._workers)
        self.assertEqual(self.outputs, [])
        self.assertIsNone(self.store._connection)
        self.assertIsNone(self.segments._connection)

    async def test_reopened_input_ledger_does_not_generate_again(self):
        await self.dispatch()
        await self.service.close()
        self.store = ReceiptStore(self.base / 'receipts.sqlite3')
        self.segments = SegmentLedger(self.base / 'segments.sqlite3')
        await self.store.open()
        await self.segments.open()
        self.service = self.make_service()
        self.assertEqual(await self.dispatch(), 'DUPLICATE')
        self.assertEqual(len(self.outputs), 1)

    async def test_unsettled_adapter_blocks_factory_replacement_and_shared_guard(self):
        async def produce(parts, delta):
            await delta('有效正文。')
            self.adapters[0].pending_tasks = 1
            self.adapters[0].reusable = False
            return AttemptResult(1, 5, True, 'fixture', ())
        self.behavior = produce
        await self.dispatch()
        self.assertTrue(self.service.blocked)
        self.assertIn(self.service, self.guard.retained)
        self.assertEqual(await self.dispatch(event('2')), 'REJECTED')
        self.assertEqual(len(self.adapters), 1)
        with self.assertRaisesRegex(RuntimeError, 'restart_required'):
            self.make_service()
        self.adapters[0].pending_tasks = 0

    async def test_uncooperative_send_retained_unknown_and_late_ack_ignored(self):
        entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        async def send(item, text):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            done.set()
            return {'message_id': 7777}
        await self.dispatch(sender=send)
        self.assertTrue(entered.is_set())
        self.assertEqual((await self.row())['status'], 'UNKNOWN')
        self.assertTrue(self.service.blocked)
        self.assertEqual(await self.dispatch(event('2')), 'REJECTED')
        with self.assertRaisesRegex(RuntimeError, 'cleanup_unsettled'):
            await self.service.close()
        self.assertIsNotNone(self.segments._connection)
        release.set()
        await asyncio.wait_for(done.wait(), 1)
        await asyncio.sleep(.01)
        rows = await self.segments.records(self.service._token(event()))
        self.assertEqual(rows[0]['status'], 'UNKNOWN')
        self.assertIsNone(rows[0]['outgoing_message_id'])
        await self.service.close()

    async def test_uncooperative_weixin_send_keeps_shared_slot_across_platforms(self):
        shared = asyncio.Semaphore(3)
        self.limits = replace(self.limits, first_receipt_seconds=3, generation_seconds=4, total_seconds=5)
        self.service = self.make_service(shared_parallel=shared)
        store = ReceiptStore(self.base / "weixin" / "receipts.sqlite3")
        segments = SegmentLedger(self.base / "weixin" / "segments.sqlite3")
        await store.open()
        await segments.open()
        configured = replace(self.config, id_kind="weixin", platform_id="fixture-wechat", bot_self_id="fixture-wechat",
                             admin_private_ids=frozenset({"wx-user"}), allowed_group_ids=frozenset(),
                             send_timeout_seconds=.05)
        service = NeutralStreamService(configured, store, segments, self.factory,
                                       limits=replace(self.limits, cleanup_seconds=.03),
                                       segment_interval=.001, shared_parallel=shared)
        wx_release, wx_entered, wx_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        qq_release, two_qq = asyncio.Event(), asyncio.Event()
        self.releases.extend((wx_release, qq_release))
        wx_event = Envelope("fixture-wechat", "fixture-wechat", "wx-user", "msg:pending", "private", text="中性问题")
        async def uncooperative(item, text):
            wx_entered.set()
            while not wx_release.is_set():
                try:
                    await wx_release.wait()
                except asyncio.CancelledError:
                    pass
            wx_done.set()
            return ApiAccepted()
        entered = []
        async def qq_produce(parts, delta):
            entered.append(parts.current[0].message_id)
            if len(entered) == 2:
                two_qq.set()
            await qq_release.wait()
            await delta("QQ中性回复。")
            return AttemptResult(1, 7, True, "fixture", ())
        try:
            await service.accept(wx_event, uncooperative)
            await asyncio.wait_for(wx_entered.wait(), 2)
            await asyncio.wait_for(service.wait_idle(), 2)
            self.assertTrue(service.blocked)
            self.assertFalse(self.service.blocked)
            self.assertEqual(service._retained_parallel_permits, 1)
            self.assertEqual(shared._value, 2)
            self.assertEqual((await store.get(wx_event.platform_id, wx_event.incoming_message_id))["status"], "UNKNOWN")
            self.behavior = qq_produce
            for index in range(3):
                await self.service.accept(event(str(index + 1), group=str(200 + index)), self.send)
            await asyncio.wait_for(two_qq.wait(), 2)
            await asyncio.sleep(.03)
            self.assertEqual(len(entered), 2)
            self.assertEqual(shared._value, 0)
            # Late success does not release the quarantined slot or turn UNKNOWN
            # into API_ACCEPTED. A new plugin instance cannot reset this budget.
            wx_release.set()
            await asyncio.wait_for(wx_done.wait(), 1)
            await asyncio.sleep(.02)
            self.assertEqual(len(entered), 2)
            self.assertEqual(shared._value, 0)
            rows = await segments.records(service._token(wx_event))
            self.assertEqual(rows[0]["status"], "UNKNOWN")
            qq_release.set()
            await asyncio.wait_for(self.service.wait_idle(), 3)
            self.assertEqual(len(entered), 3)
            self.assertEqual(shared._value, 2)
        finally:
            wx_release.set()
            qq_release.set()
            await asyncio.sleep(.02)
            await service.close()
        self.assertEqual(shared._value, 2)

    async def test_provider_pool_cannot_receive_same_busy_adapter_twice(self):
        shared = FakeAdapter()
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        async def produce(parts, delta):
            entered.set()
            await release.wait()
            await delta('正文。')
            return AttemptResult(1, 3, True, 'fixture', ())
        shared.behavior = produce
        self.service.provider_factory = lambda: shared
        await self.service.accept(event('1'), self.send)
        await entered.wait()
        await self.service.accept(event('2', group='201'), self.send)
        for _ in range(50):
            if self.service.blocked:
                break
            await asyncio.sleep(.005)
        self.assertTrue(self.service.blocked)
        self.assertEqual(len(shared.calls), 1)
        release.set()
        await self.service.wait_idle()


if __name__ == '__main__':
    unittest.main()
