"""Unnamed group voice admission; synthetic accounts/providers only."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace
import unittest

import test_roleplay_stream as fixture
from test_provider_gate import FakeStream, response
from test_reply_pacing import fast_policy
from test_wechat_native_gateway import settings, event
from plugins.astrbot_plugin_roleplay_gateway.gate import ApiAccepted
from plugins.astrbot_plugin_roleplay_gateway.memory_store import MemoryInput, MemoryReply
from plugins.astrbot_plugin_roleplay_gateway.roleplay_stream import RoleplayStreamService
from plugins.astrbot_plugin_roleplay_gateway.voice_intent import VoiceIntent


def voice(incoming="1", **changes):
    return event(incoming, **{"text": "请用语音告诉我，你今天心情怎么样？",
                             "mentions": (), "input_modality": "voice", **changes})


def decision(addressed=True, mode="voice", **kwargs):
    body = json.dumps(dict(addressed=addressed, mode=mode))
    return FakeStream([response(body), response(body, chunk=False)], **kwargs)


class AddresseeServiceTests(unittest.IsolatedAsyncioTestCase):
    open_stores = fixture.RoleplayStreamTests.open_stores
    factory = fixture.RoleplayStreamTests.factory
    asyncTearDown = fixture.RoleplayStreamTests.asyncTearDown
    dispatch = fixture.RoleplayStreamTests.dispatch
    payload = fixture.RoleplayStreamTests.payload

    async def asyncSetUp(self):
        self.prepared = []
        await fixture.RoleplayStreamTests.asyncSetUp(self)

    def make_service(self):
        self.settings = replace(settings(admin_private_ids=["wxid_member"]),
                                reply_pacing=fast_policy(), send_timeout_seconds=.4)
        return RoleplayStreamService(self.settings, self.receipts, self.segments, self.factory,
            persona=self.persona, memory=self.memory, guard=self.guard, limits=self.limits,
            segment_interval=.001, emit=self.logs.append, enabled=lambda: self.enabled,
            voice_enabled=True, group_voice_ai_wake=True)

    async def send(self, incoming, text):
        self.outputs.append((incoming.incoming_message_id, text))
        return ApiAccepted()

    async def deliver(self, incoming=None):
        async def sender(item, text):
            return await self.send(item, text)
        async def prepare(item, text):
            self.prepared.append((item.incoming_message_id, text))
        sender.prepare_voice = prepare
        return await self.dispatch(incoming or voice(), sender=sender)

    def pacing_count(self):
        with closing(sqlite3.connect(self.base / "receipts.sqlite3")) as db:
            return db.execute("SELECT COUNT(*) FROM pacing_decisions WHERE selected=1").fetchone()[0]

    async def test_affirmative_reuses_mode_sends_once_and_deduplicates(self):
        self.backend.streams.extend([decision(), fixture.reply_stream("心情不错。\n\n谢谢关心。")])
        self.assertEqual(await self.deliver(), "ACCEPTED")
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(len(self.prepared), 1)
        self.assertEqual(len(self.outputs), 1)
        snapshot = await self.memory.snapshot(self.service.namespace(voice()))
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(len(snapshot.turns[0].replies), 1)
        self.assertEqual(self.pacing_count(), 1)
        self.assertEqual(await self.deliver(), "DUPLICATE")
        self.assertEqual(len(self.backend.calls), 2)

    async def test_negative_uncertain_and_malformed_do_not_consume_slot_or_memory(self):
        self.service.settings = replace(self.settings, reply_pacing=fast_policy(cooldown_seconds=15, window_seconds=60, max_rounds=3))
        for index, stream in enumerate([decision(False), decision(None),
                FakeStream([response('broken'), response('broken', chunk=False)]), decision("true")], 1):
            with self.subTest(index=index):
                self.backend.streams.append(stream)
                self.assertEqual(await self.deliver(voice(str(index))), "SKIPPED")
        self.assertEqual(len(self.backend.calls), 4)
        self.assertEqual(self.pacing_count(), 0)
        self.assertFalse(self.outputs)
        self.assertFalse((await self.memory.snapshot(self.service.namespace(voice()))).turns)
        self.assertFalse(self.service._queued_epochs)
        self.backend.streams.append(fixture.reply_stream())
        self.assertEqual(await self.deliver(voice("5", text="三叶请用语音回答")), "ACCEPTED")
        self.assertEqual(len(self.backend.calls), 5)
        self.assertEqual(self.pacing_count(), 1)

    async def test_explicit_name_mention_regex_and_private_bypass_addressee(self):
        from plugins.astrbot_plugin_roleplay_gateway.gate import WakeMatcher
        self.service.matcher = WakeMatcher(("三叶",), (r"^测试唤醒",), lambda _: None)
        for item in [voice("1", text="三叶请用文字回答"),
                     voice("2", text="请用文字回答", mentions=("wxid_bot",)),
                     voice("3", text="测试唤醒：请用文字回答"),
                     voice("4", kind="private", group_id=None, text="请用文字回答")]:
            self.backend.streams.append(fixture.reply_stream())
            self.assertEqual(await self.deliver(item), "ACCEPTED")
        self.assertEqual(len(self.backend.calls), 4)
        self.assertFalse(any(row.get("event") == "voice_addressee" for row in self.logs))
        self.assertFalse(self.prepared)

    async def test_unauthorized_and_unwoken_text_make_zero_calls(self):
        for item in [voice("1", input_modality="text"), voice("2", group_id="999@chatroom"),
                     voice("3", sender_id="wxid_bot"), voice("4", platform_id="other"),
                     voice("5", kind="private", group_id=None, sender_id="wxid_other")]:
            self.assertEqual(await self.deliver(item), "IGNORED")
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.outputs)

    async def test_configured_display_mention_wakes_text_and_prepares_voice_once(self):
        from plugins.astrbot_plugin_roleplay_gateway.gate import WakeMatcher
        self.service.matcher = WakeMatcher(('三叶',), (r'^\s*@78percent(?:\s|[，,:：]|$)',), lambda _: None)
        self.backend.streams.append(fixture.reply_stream('早上好，今天吃什么'))
        incoming = voice(text='@78percent\u2005测试一下语音功能，用indextts的示例声线说“早上好，今天吃什么”',
                         input_modality='text')
        self.assertEqual(await self.deliver(incoming), 'ACCEPTED')
        self.assertEqual(len(self.backend.calls), 1)  # Speech generation only.
        self.assertEqual(self.prepared, [('1', '早上好，今天吃什么')])
        self.assertEqual(self.outputs, [('1', '早上好，今天吃什么')])
        for text in ('@78percent_other 测试一下语音功能', '@other 测试一下语音功能',
                     '他说 @78percent 测试一下语音功能'):
            self.assertEqual(await self.deliver(voice('2', text=text, input_modality='text')), 'IGNORED')
        self.assertEqual(await self.deliver(voice('3', text=incoming.text, input_modality='text',
                                                 group_id='999@chatroom')), 'IGNORED')
        self.assertEqual(len(self.backend.calls), 1)

    async def test_pacing_probability_cooldown_and_quota_prevent_classification(self):
        self.service.settings = replace(self.settings, reply_pacing=fast_policy(probability=0))
        self.assertEqual(await self.deliver(), "SKIPPED")
        policy = fast_policy(cooldown_seconds=15, window_seconds=60, max_rounds=1)
        self.service.settings = replace(self.settings, reply_pacing=policy)
        self.backend.streams.append(fixture.reply_stream())
        self.assertEqual(await self.deliver(voice("2", text="三叶请用文字回答")), "ACCEPTED")
        self.assertEqual(await self.deliver(voice("3")), "SKIPPED")
        self.service.settings = replace(self.settings, reply_pacing=replace(policy, cooldown_seconds=0))
        self.assertEqual(await self.deliver(voice("4")), "SKIPPED")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual((await self.receipts.get("fixture-native", "3"))["reason"], "pacing_cooldown")
        self.assertEqual((await self.receipts.get("fixture-native", "4"))["reason"], "pacing_quota")

    async def test_timeout_and_provider_error_are_silent(self):
        self.service.voice_intent.timeout = .03
        slow = decision(block=True)
        self.backend.streams.append(slow)
        self.assertEqual(await self.deliver(), "SKIPPED")
        slow.release.set()
        self.backend.error = RuntimeError("provider unavailable")
        self.assertEqual(await self.deliver(voice("2")), "SKIPPED")
        self.assertEqual(len(self.backend.calls), 2)
        self.assertFalse(self.outputs)
        self.assertEqual(self.pacing_count(), 0)

    async def test_context_is_only_last_two_confirmed_same_group_turns(self):
        async def seed(item, label, batch):
            await self.memory.commit_turn(self.service.namespace(item), expected_epoch=0,
                batch_id=batch, persona_revision=self.persona.revision,
                inputs=(MemoryInput(batch, item.sender_id, "fixture", label),),
                replies=(MemoryReply(0, "100"+batch, "已确认回答"),), complete=True)
        await seed(voice(), "old group turn", "1")
        await seed(voice(), "recent group one", "2")
        await seed(voice(), "recent group two", "3")
        await seed(voice(kind="private", group_id=None), "private secret", "4")
        await seed(voice(group_id="201@chatroom"), "other group secret", "5")
        self.backend.streams.extend([decision(False), decision(False)])
        await self.deliver(voice("10", text="denied utterance"))
        await self.deliver(voice("11", text="下一句"))
        prompt = json.dumps(self.payload(), ensure_ascii=False)
        for text in ("recent group one", "recent group two"):
            self.assertIn(text, prompt)
        for text in ("old group turn", "private secret", "other group secret", "denied utterance"):
            self.assertNotIn(text, prompt)

    async def test_clear_or_disable_during_classifier_rejects_affirmative(self):
        for index, action in enumerate(("clear", "disable"), 1):
            slow = decision(block=True)
            self.backend.streams.append(slow)
            task = asyncio.create_task(self.deliver(voice(str(index))))
            await asyncio.wait_for(slow.entered.wait(), 1)
            if action == "clear":
                await self.service.clear_memory(self.service.namespace(voice()))
            else:
                self.enabled = False
            slow.release.set()
            self.assertEqual(await task, "SKIPPED")
        self.assertFalse(self.outputs)
        self.assertEqual(self.pacing_count(), 0)

    async def test_close_during_classifier_does_not_start_reply(self):
        slow = decision(block=True)
        self.backend.streams.append(slow)
        admitted = asyncio.create_task(self.service.accept(voice(), self.send))
        await asyncio.wait_for(slow.entered.wait(), 1)
        closing_task = asyncio.create_task(self.service.close())
        await asyncio.sleep(.02)
        slow.release.set()
        self.assertEqual(await admitted, "SKIPPED")
        await closing_task
        self.assertEqual(len(self.backend.calls), 1)
        self.assertFalse(self.outputs)

    async def test_pacing_preview_has_no_writes_and_reservation_rechecks(self):
        policy = fast_policy(cooldown_seconds=15, window_seconds=60)
        first, second = voice(), voice("2")
        for item in (first, second):
            await self.receipts.claim(item, purpose="roleplay")
        self.assertTrue((await self.receipts.decide_pacing(first, policy, now=100, reserve=False)).selected)
        self.assertEqual(self.pacing_count(), 0)
        await self.receipts.decide_pacing(second, policy, now=101)
        self.assertEqual((await self.receipts.decide_pacing(first, policy, now=102, reserve=False)).reason, "cooldown")
        self.assertEqual((await self.receipts.get(first.platform_id, "1"))["status"], "ACCEPTED")
        self.assertFalse((await self.receipts.decide_pacing(first, policy, now=102)).selected)
        self.assertEqual((await self.receipts.get(first.platform_id, "1"))["status"], "SKIPPED")


class AddresseeIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_affirmative_cannot_override_timeout(self):
        release = asyncio.Event()
        class Adapter:
            pending_tasks = 0
            reusable = True
            async def consume(self, parts, delta, **kwargs):
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                await delta('{"addressed":true,"mode":"voice"}')
                return SimpleNamespace(final_seen=True)
        selector = VoiceIntent(Adapter, timeout=.02)
        result = await selector.addressed(voice(), bot_name="三叶")
        self.assertEqual(result, (False, None, "timeout", None))
        release.set()
        await asyncio.sleep(.01)
        await selector.close()
        self.assertEqual(result, (False, None, "timeout", None))

    async def test_explicit_mode_overrides_combined_result(self):
        class Adapter:
            pending_tasks = 0
            reusable = True
            async def consume(self, parts, delta, **kwargs):
                await delta('{"addressed":true,"mode":"voice"}')
                return SimpleNamespace(final_seen=True)
        selector = VoiceIntent(Adapter)
        result = await selector.addressed(voice(text="不要语音，请用文字回复"), bot_name="三叶")
        self.assertEqual(result, (True, "text", "model", "explicit"))
        await selector.close()
