"""Roleplay/memory integration with neutral fixtures and temporary SQLite only."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.astrbot_plugin_roleplay_gateway.gate import ApiAccepted, Envelope, GateSettings, ReceiptStore
from plugins.astrbot_plugin_roleplay_gateway.memory_store import MemoryStore, MemoryInput, MemoryReply
from plugins.astrbot_plugin_roleplay_gateway.neutral_stream import StreamSafetyGuard
from plugins.astrbot_plugin_roleplay_gateway.persona import PersonaSnapshot
from plugins.astrbot_plugin_roleplay_gateway import provider_gate as pg
from plugins.astrbot_plugin_roleplay_gateway.qq_text import format_qq_bubble
from plugins.astrbot_plugin_roleplay_gateway.roleplay_stream import ROLEPLAY_BOUNDARY, RoleplayStreamService
from plugins.astrbot_plugin_roleplay_gateway.segment_delivery import SegmentLedger
from plugins.astrbot_plugin_roleplay_gateway.stream_reply import ReplyLimits
from test_provider_gate import Backend, FakeClient, FakeProvider, FakeStream, response


def fixture_event(incoming="1", *, sender="100", kind="group", group="200", text="机器人中性问题"):
    return Envelope("fixture-qq", "900", sender, incoming, kind,
                    group_id=group if kind == "group" else None, text=text,
                    mentions=("900",) if kind == "group" else ())


def reply_stream(text="中性回复。", **kwargs):
    encoded = json.dumps({"speech": [part for part in text.split("\n\n") if part.strip()]}, ensure_ascii=False)
    return FakeStream([response(encoded), response(encoded, chunk=False)], **kwargs)


class QueuedBackend(Backend):
    def __init__(self):
        super().__init__()
        self.streams = deque()
        self.used_streams = []

    async def create(self, client, kwargs):
        self.stream = self.streams.popleft() if self.streams else reply_stream()
        self.used_streams.append(self.stream)
        return await super().create(client, kwargs)


class RoleplayStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="roleplay-stream-", dir=ROOT / "tests")
        self.base = Path(self.temp.name)
        self.backend = QueuedBackend()
        self.provider = FakeProvider(self.backend)
        self.contract = patch.object(pg, "_runtime_contract", return_value=(
            pg.ASTRBOT_VERSION, pg.OPENAI_VERSION, FakeProvider, FakeClient))
        self.contract.start()
        self.persona = PersonaSnapshot("neutral-guide", "revision-one", "中性向导",
                                       "你是中性向导，回答清晰自然。")
        self.settings = GateSettings(enabled=True, mode="roleplay_stream", platform_id="fixture-qq",
                                     bot_self_id="900", admin_private_ids=frozenset({"100", "101"}),
                                     allowed_group_ids=frozenset({"100", "200", "201"}),
                                     wake_names=("机器人",), send_timeout_seconds=.4)
        self.limits = ReplyLimits(first_receipt_seconds=2, generation_seconds=3, total_seconds=4,
                                  cleanup_seconds=.25, flush_seconds=.02)
        self.guard = StreamSafetyGuard()
        self.enabled = True
        self.logs, self.outputs, self.adapters = [], [], []
        self.release_events = []
        self.expected_close_error = False
        await self.open_stores()
        self.service = self.make_service()

    async def open_stores(self):
        self.receipts = ReceiptStore(self.base / "receipts.sqlite3")
        self.segments = SegmentLedger(self.base / "segments.sqlite3")
        self.memory = MemoryStore(self.base / "memory.sqlite3")
        await self.receipts.open()
        await self.segments.open()
        await self.memory.open()

    def factory(self):
        adapter = pg.FailFastOpenAI(self.provider, cleanup_timeout_seconds=.3)
        self.adapters.append(adapter)
        return adapter

    def make_service(self):
        return RoleplayStreamService(self.settings, self.receipts, self.segments, self.factory,
                                     persona=self.persona, memory=self.memory, guard=self.guard,
                                     limits=self.limits, segment_interval=.001,
                                     emit=self.logs.append, enabled=lambda: self.enabled)

    async def asyncTearDown(self):
        for event in self.release_events:
            event.set()
        self.backend.create_release.set()
        for stream in self.backend.used_streams:
            stream.release.set()
            stream.close_release.set()
        await asyncio.sleep(.03)
        try:
            try:
                await self.service.close()
            except RuntimeError:
                if not self.expected_close_error:
                    raise
            for adapter in self.adapters:
                if adapter._tasks:
                    _, pending = await asyncio.wait(tuple(adapter._tasks), timeout=2)
                    self.assertFalse(pending, "fixture left provider tasks pending")
            if self.service._workers:
                _, pending = await asyncio.wait(tuple(self.service._workers), timeout=2)
                self.assertFalse(pending, "fixture left stream workers pending")
        finally:
            await self.memory.close()
            await self.segments.close()
            await self.receipts.close()
            self.contract.stop()
            self.temp.cleanup()

    async def test_voice_coalesces_and_commits_body_once(self):
        from plugins.astrbot_plugin_roleplay_gateway.voice_intent import VoiceIntent
        self.service.voice_intent = VoiceIntent(self.factory)
        self.backend.streams.append(reply_stream("第一句。\n\n第二句。"))
        prepared = []
        async def prepare(event, text):prepared.append(text)
        async def sender(event, text):return await self.send(event, text)
        sender.prepare_voice = prepare
        event = fixture_event(text="机器人请用语音回答")
        self.assertEqual(await self.dispatch(event, sender=sender), "ACCEPTED")
        self.assertEqual(len(prepared), 1)
        self.assertEqual(len(self.outputs), 1)
        self.assertIn("本轮回复形式已确定为微信语音", self.payload()[0]["content"])
        snapshot = await self.memory.snapshot(self.service.namespace(event))
        self.assertEqual(len(snapshot.turns), 1)

    async def test_voice_does_not_read_out_false_capability_denial(self):
        from plugins.astrbot_plugin_roleplay_gateway.voice_intent import VoiceIntent
        self.service.voice_intent = VoiceIntent(self.factory)
        self.backend.streams.append(reply_stream("我这边只能发文字消息，语音暂时没法给你呢。"))
        prepared = []
        async def prepare(event, text): prepared.append(text)
        async def sender(event, text): return await self.send(event, text)
        sender.prepare_voice = prepare
        event = fixture_event(text="机器人请用语音回答")
        self.assertEqual(await self.dispatch(event, sender=sender), "ACCEPTED")
        self.assertEqual(prepared, ["我在听，你接着说。"])
        self.assertEqual(self.outputs[-1][1], "我在听，你接着说。")
        self.assertTrue(any(row["code"] == "voice_capability_denial_replaced" for row in self.logs))

    async def test_ignored_voice_request_never_calls_intent(self):
        from plugins.astrbot_plugin_roleplay_gateway.voice_intent import VoiceIntent
        self.service.voice_intent = VoiceIntent(lambda: self.fail("ignored input reached classifier"))
        event = fixture_event(text="你好", sender="999", kind="private")
        self.assertEqual(await self.dispatch(event), "IGNORED")
        self.assertFalse(self.adapters)

    async def send(self, event, text):
        # main.py applies this exact display formatter at the OneBot boundary.
        self.outputs.append((event.incoming_message_id, format_qq_bubble(text)))
        return {"message_id": 10000 + len(self.outputs)}

    async def dispatch(self, event=None, *, sender=None):
        admitted = await self.service.accept(event or fixture_event(), sender or self.send)
        await asyncio.wait_for(self.service.wait_idle(), 7)
        return admitted

    async def restart(self, persona=None):
        await self.service.close()
        if persona is not None:
            self.persona = persona
        await self.open_stores()
        self.service = self.make_service()

    def payload(self, index=-1):
        return self.backend.calls[index][2]["messages"]

    async def open_weixin(self, *, shared_parallel=None):
        receipts = ReceiptStore(self.base / "weixin" / "receipts.sqlite3")
        segments = SegmentLedger(self.base / "weixin" / "segments.sqlite3")
        memory = MemoryStore(self.base / "weixin" / "memory.sqlite3")
        await receipts.open()
        await segments.open()
        await memory.open()
        configured = replace(self.settings, id_kind="weixin", platform_id="fixture-wechat",
                             bot_self_id="fixture-wechat", admin_private_ids=frozenset({"wx-user", "wx-other"}),
                             memory_admin_ids=frozenset({"wx-user"}), allowed_group_ids=frozenset())
        return RoleplayStreamService(configured, receipts, segments, self.factory,
                                     persona=self.persona, memory=memory,
                                     limits=self.limits, segment_interval=.001,
                                     emit=self.logs.append, shared_parallel=shared_parallel)

    async def test_weixin_history_and_acceptance_are_persistent_and_separate_from_qq(self):
        qq_event = fixture_event("1", kind="private", text="QQ私聊独有代号是青柚灯塔。")
        await self.dispatch(qq_event)
        before = await self.memory.snapshot(self.service.namespace(qq_event))
        service = await self.open_weixin()
        event = Envelope("fixture-wechat", "fixture-wechat", "wx-user", "raw:0001", "private",
                         text="微信标记是银杏。")
        sent = []
        async def accepted(item, text):
            sent.append(text)
            return ApiAccepted()
        try:
            self.assertEqual(await service.accept(event, accepted), "ACCEPTED")
            self.assertEqual(await service.accept(event, accepted), "DUPLICATE")
            await service.wait_idle()
            self.assertNotIn("青柚灯塔", json.dumps(self.payload(), ensure_ascii=False))
            snapshot = await service.memory.snapshot(service.namespace(event))
            self.assertEqual(len(snapshot.turns), 1)
            reply = snapshot.turns[0].replies[0]
            self.assertEqual(reply.delivery_status, "API_ACCEPTED")
            self.assertIsNone(reply.message_id)
            self.assertTrue(reply.delivery_ref)
            row = await service.store.get(event.platform_id, event.incoming_message_id)
            self.assertEqual(row["status"], "API_ACCEPTED")
            self.assertIsNone(row["outgoing_message_id"])
            self.assertEqual(row["confirmed_segments"], len(sent))
            self.assertEqual(row["delivery_ref"], reply.delivery_ref)
        finally:
            await service.close()
        service = await self.open_weixin()
        try:
            self.assertEqual(await service.accept(event, accepted), "DUPLICATE")
            self.assertEqual((await service.memory.snapshot(service.namespace(event))), snapshot)
            before_calls = len(self.backend.calls)
            await service.accept(replace(event, incoming_message_id="raw:0002", text="微信标记是什么？"), accepted)
            await service.wait_idle()
            self.assertEqual(len(self.backend.calls), before_calls + 1)
            self.assertEqual(len((await service.memory.snapshot(service.namespace(event))).turns), 2)
            payload = json.dumps(self.payload(), ensure_ascii=False)
            self.assertIn("银杏", payload)
            self.assertNotIn("青柚灯塔", payload)
            self.assertEqual(await self.memory.snapshot(self.service.namespace(qq_event)), before)
        finally:
            await service.close()

    async def test_weixin_memory_commands_and_close_do_not_touch_qq(self):
        qq_event = fixture_event("1", kind="private")
        await self.dispatch(qq_event)
        before = await self.memory.snapshot(self.service.namespace(qq_event))
        pending = fixture_event("99", kind="private")
        await self.receipts.claim(pending)
        await self.receipts.transition(pending, "ACCEPTED", "IN_FLIGHT", "fixture_pending")
        service = await self.open_weixin()
        event = Envelope("fixture-wechat", "fixture-wechat", "wx-user", "cmd:1", "private", text="/记忆 编辑 微信摘要")
        sent = []
        async def accepted(item, text):
            sent.append(text)
            return ApiAccepted()
        model_calls = len(self.backend.calls)
        try:
            await service.accept(event, accepted)
            await service.wait_idle()
            namespace = service.namespace(event)
            self.assertEqual((await service.memory.snapshot(namespace)).summary, "微信摘要")
            row = await service.store.get(event.platform_id, event.incoming_message_id)
            self.assertEqual(row["status"], "API_ACCEPTED")
            self.assertIsNone(row["outgoing_message_id"])
            self.assertTrue(row["delivery_ref"])
            await service.accept(replace(event, incoming_message_id="cmd:2", text="/记忆 私聊 100 查看"), accepted)
            await service.wait_idle()
            self.assertIn("没有权限", sent[-1])
            await service.accept(replace(event, incoming_message_id="cmd:3", text="/记忆 清空全部"), accepted)
            await service.wait_idle()
            self.assertEqual((await service.memory.snapshot(namespace)).summary, "")
            self.assertEqual(await self.memory.snapshot(self.service.namespace(qq_event)), before)
            self.assertEqual(len(self.backend.calls), model_calls)
        finally:
            await service.close()
        self.assertEqual((await self.receipts.get(pending.platform_id, pending.incoming_message_id))["status"], "IN_FLIGHT")
        self.assertEqual(await self.memory.snapshot(self.service.namespace(qq_event)), before)

    async def test_weixin_unknown_send_never_enters_history(self):
        service = await self.open_weixin()
        event = Envelope("fixture-wechat", "fixture-wechat", "wx-user", "msg:unknown", "private", text="中性问题")
        calls = []
        async def unknown(item, text):
            calls.append(text)
            raise ConnectionError("synthetic transport failure")
        try:
            await service.accept(event, unknown)
            await service.wait_idle()
            self.assertEqual((await service.store.get(event.platform_id, event.incoming_message_id))["status"], "UNKNOWN")
            self.assertEqual((await service.memory.snapshot(service.namespace(event))).turns, ())
            self.assertEqual(await service.accept(event, unknown), "DUPLICATE")
            self.assertEqual(len(calls), 1)
        finally:
            await service.close()

    async def test_persona_and_second_turn_only_sent_history_enter_payload(self):
        first = fixture_event("1", text="机器人请记住中性标记甲")
        self.backend.streams.append(reply_stream("已确认标记甲。"))
        await self.dispatch(first)
        first_payload = self.payload()
        self.assertEqual(first_payload[0]["content"], ROLEPLAY_BOUNDARY)
        self.assertEqual(first_payload[1]["content"], self.persona.prompt)
        self.assertEqual([row["role"] for row in first_payload], ["system", "system", "user"])
        self.assertEqual(await self.dispatch(first), "DUPLICATE")
        self.assertEqual(len(self.backend.calls), 1)
        await self.dispatch(fixture_event("2", text="机器人刚才的标记是什么"))
        messages = self.payload()
        self.assertEqual([row["role"] for row in messages], ["system", "system", "user", "assistant", "user"])
        self.assertEqual(json.loads(messages[2]["content"])["current_speaker_batch"][0]["text"], first.text)
        self.assertEqual(json.loads(messages[3]["content"]), {"speech": ["已确认标记甲。"]})
        snapshot = await self.memory.snapshot(self.service.namespace(first))
        self.assertEqual(len(snapshot.turns), 2)
        self.assertEqual(snapshot.turns[0].persona_revision, "revision-one")
        self.assertTrue(snapshot.turns[0].complete)
        self.assertNotIn(first.text, repr(self.logs))
        self.assertNotIn("已确认标记甲", repr(self.logs))

    async def test_same_group_shared_but_other_group_private_and_friend_isolated(self):
        self.backend.streams.append(reply_stream("群内共同标记。"))
        await self.dispatch(fixture_event("1", sender="100", group="100", text="机器人记住群内标记"))
        await self.dispatch(fixture_event("2", sender="101", group="100"))
        self.assertIn("群内共同标记", repr(self.payload()))
        for event in (fixture_event("3", group="201"),
                      fixture_event("4", kind="private", sender="100"),
                      fixture_event("5", kind="private", sender="101")):
            await self.dispatch(event)
            self.assertEqual(len(self.payload()), 3)
            self.assertNotIn("群内共同标记", repr(self.payload()))
        private = fixture_event("6", kind="private", sender="100")
        await self.dispatch(private)
        self.assertEqual(len(self.payload()), 5)
        self.assertEqual(len((await self.memory.snapshot(self.service.namespace(private))).turns), 2)

    async def test_normal_restart_restores_history_and_dedup_without_replaying(self):
        first = fixture_event("1", text="机器人重启前信息")
        self.backend.streams.append(reply_stream("重启前确认回答。"))
        await self.dispatch(first)
        await self.restart()
        self.assertEqual(await self.dispatch(first), "DUPLICATE")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.outputs), 1)
        await self.dispatch(fixture_event("2"))
        self.assertIn("重启前确认回答", repr(self.payload()))
        self.assertEqual(len((await self.memory.snapshot(self.service.namespace(first))).turns), 2)

    async def test_persona_revision_keeps_history_but_other_identity_isolated(self):
        self.backend.streams.append(reply_stream("旧修订的确认内容。"))
        await self.dispatch(fixture_event("1"))
        updated = replace(self.persona, revision="revision-two", prompt="修订后的中性向导。")
        await self.restart(updated)
        await self.dispatch(fixture_event("2"))
        self.assertEqual(self.payload()[1]["content"], updated.prompt)
        self.assertIn("旧修订的确认内容", repr(self.payload()))
        namespace = self.service.namespace(fixture_event())
        snapshot = await self.memory.snapshot(namespace)
        self.assertEqual([turn.persona_revision for turn in snapshot.turns], ["revision-one", "revision-two"])
        await self.restart(replace(updated, persona_id="another-neutral", revision="other-revision"))
        await self.dispatch(fixture_event("3"))
        self.assertEqual(len(self.payload()), 3)
        self.assertEqual(len((await self.memory.snapshot(namespace)).turns), 2)

    async def test_dialogue_projection_preserves_database_and_removes_legacy_stage_prose_from_request(self):
        event = fixture_event()
        namespace = self.service.namespace(event)
        legacy = '“累了就歇会儿。”\n$我顿了一下，抬眼看你。\n$“散步代号是青柚灯塔。”'
        await self.memory.commit_turn(namespace, expected_epoch=0, batch_id="legacy",
            persona_revision="old", inputs=(MemoryInput("old", "100", "甲", "记住散步代号"),),
            replies=(MemoryReply(0, "confirmed-old", legacy),), complete=True)
        await self.dispatch(event)
        request = repr(self.payload())
        self.assertIn("青柚灯塔", request)
        self.assertNotIn("我顿了一下，抬眼看你", request)
        stored = await self.memory.snapshot(namespace)
        self.assertEqual(stored.turns[0].replies[0].text, legacy)

    async def test_chunked_dialogue_is_clean_in_both_delivery_and_memory(self):
        raw = '{"speech":["“累了就歇会儿。$”","我陪你聊。"]}'
        self.backend.streams.append(FakeStream([*(response(char) for char in raw), response("", chunk=False)]))
        event = fixture_event()
        await self.dispatch(event)
        shown = [text for _, text in self.outputs]
        self.assertEqual(shown, ["累了就歇会儿。", "我陪你聊。"])
        snapshot = await self.memory.snapshot(self.service.namespace(event))
        self.assertEqual([reply.text for reply in snapshot.turns[0].replies], shown)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_narration_or_invalid_protocol_is_never_sent_or_saved_as_raw_text(self):
        for i, raw in enumerate(('“你好。”$我抬眼看你。', '{"action":"抬眼","speech":["你好。"]}',
                                 '{"speech":["你好。（抬眼看你）"]}', '{"speech":["未闭合'), 1):
            self.backend.streams.append(FakeStream([response(raw), response("", chunk=False)]))
            event = fixture_event(str(i))
            await self.dispatch(event)
            self.assertEqual((await self.receipts.get("fixture-qq", str(i)))["status"], "FAILED")
        self.assertEqual(self.outputs, [])
        self.assertEqual((await self.memory.snapshot(self.service.namespace(event))).turns, ())
        self.assertEqual(len(self.backend.calls), 4)

    async def test_first_closed_speech_is_sent_while_later_record_is_still_generating(self):
        tail_entered, tail_release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.release_events.append(tail_release)

        class SlowTail(FakeStream):
            step = 0

            async def __anext__(self):
                self.step += 1
                if self.step == 1:
                    return response('{"speech":["先歇一会儿。",')
                if self.step == 2:
                    tail_entered.set()
                    await tail_release.wait()
                    return response('"后续未闭合')
                if self.step == 3:
                    return response("", chunk=False)
                raise StopAsyncIteration

        self.backend.streams.append(SlowTail())
        async def sender(event, text):
            result = await self.send(event, text)
            sent.set()
            return result
        await self.service.accept(fixture_event(), sender)
        await asyncio.wait_for(tail_entered.wait(), 1)
        await asyncio.wait_for(sent.wait(), 1)
        self.assertFalse(tail_release.is_set())
        tail_release.set()
        await asyncio.wait_for(self.service.wait_idle(), 3)
        self.assertEqual([text for _, text in self.outputs], ["先歇一会儿。"])
        snapshot = await self.memory.snapshot(self.service.namespace(fixture_event()))
        self.assertFalse(snapshot.turns[0].complete)
        self.assertEqual([reply.text for reply in snapshot.turns[0].replies], ["先歇一会儿。"])

    async def test_provider_failure_and_unknown_send_do_not_save_drafts(self):
        first = fixture_event("1")
        self.backend.error = OSError("private fixture provider detail")
        await self.dispatch(first)
        self.assertEqual((await self.receipts.get("fixture-qq", "1"))["status"], "FAILED")
        self.assertEqual((await self.memory.snapshot(self.service.namespace(first))).turns, ())
        self.backend.error = None
        self.backend.streams.append(reply_stream("生成但回执未知的内容。"))

        async def unknown(event, text):
            raise ConnectionError("private fixture connection detail")

        await self.dispatch(fixture_event("2"), sender=unknown)
        self.assertEqual((await self.receipts.get("fixture-qq", "2"))["status"], "UNKNOWN")
        self.assertEqual((await self.memory.snapshot(self.service.namespace(first))).turns, ())
        self.assertNotIn("private fixture", repr(self.logs))

    async def test_partial_send_commits_only_confirmed_visible_prefix(self):
        self.backend.streams.append(reply_stream("第一段已确认。\n\n第二段回执未知。\n\n第三段未发送。"))
        calls = 0

        async def partial(event, text):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ConnectionError("uncertain fixture send")
            return await self.send(event, text)

        event = fixture_event()
        await self.dispatch(event, sender=partial)
        snapshot = await self.memory.snapshot(self.service.namespace(event))
        self.assertEqual(len(snapshot.turns), 1)
        self.assertFalse(snapshot.turns[0].complete)
        self.assertEqual([reply.text for reply in snapshot.turns[0].replies], ["第一段已确认。"])
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.outputs), 1)
        await self.dispatch(fixture_event("2"))
        payload = self.payload()
        self.assertEqual(json.loads(payload[3]["content"]), {"speech": ["第一段已确认。"]})
        self.assertFalse(json.loads(payload[2]["content"])["reply_delivery"]["complete"])
        self.assertNotIn("第二段回执未知", repr(payload))
        self.assertNotIn("第三段未发送", repr(payload))

    async def test_memory_records_formatted_qq_bubbles_not_raw_edge_blank_lines(self):
        self.backend.streams.append(FakeStream([
            response('{"speech":["第一段。",'), response('\n\n"  第二段。"]}'),
            response("", chunk=False),
        ]))
        event = fixture_event()
        await self.dispatch(event)
        snapshot = await self.memory.snapshot(self.service.namespace(event))
        shown = [text for _, text in self.outputs]
        stored = [reply.text for reply in snapshot.turns[0].replies]
        self.assertEqual(shown, ["第一段。", "第二段。"])
        self.assertEqual(stored, shown)

    async def test_clear_memory_invalidates_running_generation_and_allows_fresh_input(self):
        blocked = reply_stream("清空前的旧输出。", block=True)
        self.backend.streams.append(blocked)
        self.release_events.append(blocked.release)
        old = fixture_event("1")
        await self.service.accept(old, self.send)
        await asyncio.wait_for(blocked.entered.wait(), 2)
        cleared = await self.service.clear_memory(self.service.namespace(old))
        self.assertEqual(cleared.epoch, 1)
        blocked.release.set()
        await asyncio.wait_for(self.service.wait_idle(), 5)
        self.assertEqual(self.outputs, [])
        self.assertEqual((await self.memory.snapshot(self.service.namespace(old))).turns, ())
        await self.dispatch(fixture_event("2"))
        self.assertEqual(len(self.payload()), 3)
        self.assertEqual(len((await self.memory.snapshot(self.service.namespace(old))).turns), 1)

    async def test_clear_memory_also_invalidates_preclear_queued_work(self):
        blocked = reply_stream("清空前活动输出。", block=True)
        self.backend.streams.append(blocked)
        self.release_events.append(blocked.release)
        first = fixture_event("1")
        await self.service.accept(first, self.send)
        await asyncio.wait_for(blocked.entered.wait(), 2)
        await self.service.accept(fixture_event("2", text="机器人清空前排队问题"), self.send)
        await self.service.clear_memory(self.service.namespace(first))
        blocked.release.set()
        await asyncio.wait_for(self.service.wait_idle(), 5)
        self.assertEqual(self.outputs, [])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual((await self.memory.snapshot(self.service.namespace(first))).turns, ())
        await self.dispatch(fixture_event("3", text="机器人清空后新问题"))
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(len(self.outputs), 1)

    async def test_cancelled_clear_waits_for_committed_epoch_refresh_and_fences_old_reply(self):
        blocked = reply_stream("清空提交后不得发送旧回复。", block=True)
        self.backend.streams.append(blocked)
        committed, release_clear = asyncio.Event(), asyncio.Event()
        self.release_events.extend((blocked.release, release_clear))
        first = fixture_event("1")
        namespace = self.service.namespace(first)
        await self.service.accept(first, self.send)
        await asyncio.wait_for(blocked.entered.wait(), 2)
        original_clear = self.memory.clear_all

        async def pause_after_commit(target):
            snapshot = await original_clear(target)
            committed.set()
            await release_clear.wait()
            return snapshot

        with patch.object(self.memory, "clear_all", pause_after_commit):
            clearing = asyncio.create_task(self.service.clear_memory(namespace))
            try:
                await asyncio.wait_for(committed.wait(), 2)
                self.assertEqual((await self.memory.snapshot(namespace)).epoch, 1)
                self.assertEqual(self.service._memory_epochs[namespace], 0)
                clearing.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                # Caller cancellation must wait for DB/cache settlement instead
                # of abandoning the already committed clear operation.
                self.assertFalse(clearing.done())
                self.assertTrue(self.service._clearing_namespaces.get(namespace))
                self.assertTrue(self.service._memory_clears)
                blocked.release.set()
                await asyncio.wait_for(self.service.wait_idle(), 5)
                self.assertEqual(self.outputs, [])
                self.assertEqual((await self.memory.snapshot(namespace)).turns, ())
                release_clear.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(clearing, 2)
                self.assertEqual(self.service._memory_epochs[namespace], 1)
                self.assertNotIn(namespace, self.service._clearing_namespaces)
                self.assertFalse(self.service._memory_clears)
            finally:
                release_clear.set()
                blocked.release.set()
                await asyncio.gather(clearing, return_exceptions=True)
        await self.dispatch(fixture_event("2", text="机器人清空后新问题"))
        snapshot = await self.memory.snapshot(namespace)
        self.assertEqual(snapshot.epoch, 1)
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(snapshot.turns[0].inputs[0].message_id, "2")
        self.assertNotIn("清空提交后不得发送旧回复", repr(self.payload()))

    async def test_queue_expiry_and_shutdown_release_epochs_before_model_dispatch(self):
        blocked = reply_stream("关闭前阻塞的中性回复。", block=True)
        self.backend.streams.append(blocked)
        self.release_events.append(blocked.release)
        await self.service.accept(fixture_event("1"), self.send)
        await asyncio.wait_for(blocked.entered.wait(), 2)
        released = []
        expired = asyncio.Event()
        original_release = self.service._release_input

        def track_release(event):
            original_release(event)
            key = (event.platform_id, str(event.incoming_message_id))
            released.append((str(event.incoming_message_id), key in self.service._queued_epochs))
            if event.incoming_message_id == "2":
                expired.set()

        with patch.object(self.service, "_release_input", track_release):
            self.service.queue_timeout = .15
            await self.service.accept(fixture_event("2", text="机器人将过期的排队输入"), self.send)
            await asyncio.wait_for(expired.wait(), 2)
            self.assertEqual((await self.receipts.get("fixture-qq", "2"))["reason"], "queue_expired")
            self.assertIn(("2", False), released)
            self.assertNotIn(("fixture-qq", "2"), self.service._queued_epochs)
            self.assertIn(("fixture-qq", "1"), self.service._queued_epochs)
            self.assertEqual(len(self.backend.calls), 1)

            self.service.queue_timeout = 30
            await self.service.accept(fixture_event("3", text="机器人关闭前的排队输入"), self.send)
            await asyncio.sleep(0)  # Let the worker begin waiting for the held session lock.
            self.assertIn(("fixture-qq", "3"), self.service._queued_epochs)
            self.assertEqual((await self.receipts.get("fixture-qq", "3"))["status"], "ACCEPTED")
            await self.service.close()
            self.assertIn(("3", False), released)
            self.assertFalse(self.service._queued_epochs)
            self.assertFalse(self.service._workers)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.outputs, [])
        self.assertIsNone(self.memory._connection)
        with closing(sqlite3.connect(self.base / "receipts.sqlite3")) as database:
            row = database.execute("SELECT status FROM receipts WHERE incoming_message_id='3'").fetchone()
        self.assertEqual(row[0], "ABORTED")

    async def test_shutdown_settles_generation_before_all_three_databases_close(self):
        blocked = reply_stream("停止后不应发送。", block=True)
        self.backend.streams.append(blocked)
        await self.service.accept(fixture_event(), self.send)
        await asyncio.wait_for(blocked.entered.wait(), 2)
        await self.service.close()
        self.assertFalse(self.service._workers)
        self.assertEqual(self.outputs, [])
        self.assertIsNone(self.receipts._connection)
        self.assertIsNone(self.segments._connection)
        self.assertIsNone(self.memory._connection)
        self.assertTrue(blocked.close_completed)
        self.assertTrue(all(adapter.pending_tasks == 0 for adapter in self.adapters))

    async def test_uncooperative_send_retains_memory_database_and_blocks_reload(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.release_events.append(release)

        async def uncooperative(event, text):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            finished.set()
            return {"message_id": 55555}

        await self.dispatch(sender=uncooperative)
        self.assertTrue(entered.is_set())
        self.assertTrue(self.service.blocked)
        self.assertIn(self.service, self.guard.retained)
        self.assertEqual((await self.memory.snapshot(self.service.namespace(fixture_event()))).turns, ())
        self.expected_close_error = True
        with self.assertRaisesRegex(RuntimeError, "cleanup_unsettled"):
            await self.service.close()
        self.assertIsNotNone(self.memory._connection)
        with self.assertRaisesRegex(RuntimeError, "restart_required"):
            self.make_service()
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(.03)
        self.assertEqual((await self.receipts.get("fixture-qq", "1"))["status"], "UNKNOWN")
        self.assertEqual((await self.memory.snapshot(self.service.namespace(fixture_event()))).turns, ())


if __name__ == "__main__":
    unittest.main()
