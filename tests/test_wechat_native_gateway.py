"""Native group identity, receipt and shared-memory contracts; no desktop I/O."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import test_roleplay_stream as fixture
from plugins.astrbot_plugin_roleplay_gateway.gate import (
    ApiAccepted, ConfigurationError, Envelope, GateService, GateSettings,
    ReceiptStore, WakeMatcher, incoming_message_id, native_group_id,
    native_person_id, send_evidence,
)
from plugins.astrbot_plugin_roleplay_gateway.memory_commands import (
    CommandError, DENIED, MemoryCommand, command_namespace,
)
from plugins.astrbot_plugin_roleplay_gateway.memory_store import Namespace
from plugins.astrbot_plugin_roleplay_gateway.reply_pacing import ReplyPacing


def configuration(**changes):
    return {
        "enabled": True, "platform_id": "fixture-native", "bot_self_id": "wxid_bot",
        "admin_private_ids": [], "allowed_group_ids": ["200@chatroom", "201@chatroom"],
        "memory_admin_ids": ["wxid_admin"], "wake_names": ["三叶"],
        "mode": "roleplay_stream", "persona_id": "neutral-guide",
        "persona_name": "中性向导", "persona_filename": "neutral.txt",
        "persona_sha256": "a" * 64, **changes,
    }


def settings(**changes):
    return GateSettings.from_mapping(configuration(**changes), id_kind="wechat_native")


def event(incoming="1", **changes):
    return replace(Envelope("fixture-native", "wxid_bot", "wxid_member", incoming,
                            "group", group_id="200@chatroom", text="三叶你好",
                            mentions=("wxid_bot",)), **changes)


class NativeIdentityTests(unittest.TestCase):
    def test_native_person_and_group_ids_are_exact_ascii_types(self):
        for value in ("wxid_member", "legacy.name-1", "a" * 127):
            self.assertEqual(native_person_id(value), value)
        for value in (None, True, 1, "", "a" * 128, "成员", "wx id", "wx:id",
                      "wxid\n", " wxid", "wxid ", "wxid\0", "200@chatroom",
                      "filehelper", "weixin", "fmessage", "newsapp", "floatbottle", "gh_public"):
            with self.subTest(value=value):
                self.assertIsNone(native_person_id(value))
        for value in ("1@chatroom", "9" * 118 + "@chatroom"):
            self.assertEqual(native_group_id(value), value)
        for value in (None, True, 200, "test", "200", "wxid_member", "0@chatroom",
                      "01@chatroom", "２００@chatroom", "9" * 119 + "@chatroom",
                      "200@chatroom\n", " 200@chatroom", "200@chatroom:member"):
            with self.subTest(value=value):
                self.assertIsNone(native_group_id(value))

    def test_native_message_ids_preserve_uint64_without_aliases(self):
        for value in (1, "1", 2**64 - 1, str(2**64 - 1)):
            self.assertEqual(incoming_message_id(value, "wechat_native"), str(value))
        for value in (None, True, False, 0, "0", -1, "-1", "01", " 1", "1 ",
                      "1\n", "１", 1.0, 2**64, str(2**64), "msg:1"):
            with self.subTest(value=value):
                self.assertIsNone(incoming_message_id(value, "wechat_native"))
        self.assertEqual(incoming_message_id(" 001 ", "onebot"), "1")
        self.assertEqual(incoming_message_id("msg:0001", "weixin"), "msg:0001")

    def test_native_configuration_binds_one_private_peer_and_is_separate_from_clawbot(self):
        configured = settings()
        self.assertEqual(configured.id_kind, "wechat_native")
        self.assertNotEqual(configured.bot_self_id, configured.platform_id)
        self.assertEqual(configured.memory_admin_ids, frozenset({"wxid_admin"}))
        self.assertEqual(configured.admin_private_ids, frozenset())
        self.assertEqual(settings(admin_private_ids=["wxid_member"]).admin_private_ids, frozenset({"wxid_member"}))
        for change in (
            {"allowed_group_ids": ["test"]}, {"allowed_group_ids": ["200"]},
            {"allowed_group_ids": [" 200@chatroom"]},
            {"bot_self_id": "200@chatroom"}, {"bot_self_id": "wxid_bot "},
            {"memory_admin_ids": ["wxid_admin "]}, {"memory_admin_ids": ["gh_public"]},
            {"admin_private_ids": ["wxid_member", "wxid_other"]}, {"platform_id": "native:group"},
            {"platform_id": "native group"},
        ):
            with self.subTest(change=change), self.assertRaises(ConfigurationError):
                settings(**change)
        with self.assertRaises(ConfigurationError):
            GateSettings.from_mapping(configuration())
        with self.assertRaisesRegex(ConfigurationError, "weixin_private_only"):
            GateSettings.from_mapping({"platform_id": "clawbot", "bot_self_id": "clawbot",
                                       "allowed_group_ids": ["200@chatroom"]}, id_kind="weixin")
        with self.assertRaisesRegex(ConfigurationError, "invalid_weixin_self_id"):
            GateSettings.from_mapping({"platform_id": "clawbot", "bot_self_id": "wxid_bot"},
                                      id_kind="weixin")

    def test_config_cannot_change_protocol_or_other_platform_pacing(self):
        native = settings(reply_pacing={"enabled": True, "probability": 1.0})
        qq = GateSettings.from_mapping({"enabled": True, "platform_id": "qq", "bot_self_id": "900",
                                       "id_kind": "wechat_native",
                                       "reply_pacing": {"enabled": True, "probability": .6}})
        self.assertEqual(native.reply_pacing.probability, 1.0)
        self.assertEqual(qq.id_kind, "onebot")
        self.assertEqual(qq.reply_pacing.probability, .6)

    def test_native_receipt_requires_verified_marker_and_never_fabricates_id(self):
        class DerivedAcceptance(ApiAccepted):
            pass
        self.assertEqual(send_evidence(ApiAccepted(), "wechat_native"), ("API_ACCEPTED", None))
        for result in (True, False, None, {"success": True}, {"message_id": "123"},
                       {"status": "NATIVE_COMPLETED"}, DerivedAcceptance()):
            with self.subTest(result=result):
                self.assertEqual(send_evidence(result, "wechat_native"), ("UNKNOWN", None))
        self.assertEqual(send_evidence(ApiAccepted(), "onebot"), ("UNKNOWN", None))
        self.assertEqual(send_evidence({"message_id": "123"}, "onebot"), ("SENT", "123"))

    def test_eligible_only_accepts_real_group_members_in_bound_rooms(self):
        configured = settings()
        service = GateService(configured, None)
        self.assertTrue(service.eligible(event()))
        self.assertTrue(service.eligible(event(sender_id="legacy.member", mentions=())))
        self.assertTrue(service.eligible(event(text="hello")))
        for change in (
            {"kind": "private", "group_id": None}, {"group_id": "202@chatroom"},
            {"group_id": "test"}, {"sender_id": "wxid_bot"}, {"sender_id": "gh_public"},
            {"sender_id": "other:member"}, {"sender_id": "200@chatroom"},
            {"sender_id": "成员昵称"}, {"post_type": "notice"},
            {"platform_id": "clawbot"}, {"bot_self_id": "other"},
            {"text": "hello", "mentions": ()}, {"text": "@bot hello", "mentions": ()},
        ):
            with self.subTest(change=change):
                self.assertFalse(service.eligible(event(**change)))
        # Explicit binding admits just that private peer, independently of group wakes.
        service.settings = replace(configured, admin_private_ids=frozenset({"wxid_member"}))
        self.assertTrue(service.eligible(event(kind="private", group_id=None)))
        self.assertFalse(service.eligible(event(kind="private", group_id=None, sender_id="wxid_other")))

    def test_native_command_namespace_is_current_group_only(self):
        item = event(sender_id="wxid_admin")
        configured = settings()
        namespace = command_namespace(MemoryCommand("view"), item, configured, "neutral-guide")
        self.assertEqual(namespace, Namespace("fixture-native", "group", "200@chatroom", "neutral-guide"))
        for command, current in (
            (MemoryCommand("view", "group", "201@chatroom"), item),
            (MemoryCommand("view", "private", "wxid_admin"), item),
            (MemoryCommand("view"), replace(item, kind="private", group_id=None)),
            (MemoryCommand("view"), replace(item, sender_id="wxid_member")),
            (MemoryCommand("view"), replace(item, group_id="202@chatroom")),
        ):
            with self.subTest(command=command, current=current), self.assertRaises(CommandError):
                command_namespace(command, current, configured, "neutral-guide")


class NativePacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_probability_one_retains_cooldown_and_quota_and_does_not_change_qq(self):
        with tempfile.TemporaryDirectory(prefix="native-policy-", dir=ROOT / "work") as temporary:
            store = ReceiptStore(Path(temporary) / "receipts.sqlite3")
            await store.open()
            policy = ReplyPacing(enabled=True, probability=1.0)
            try:
                with patch("plugins.astrbot_plugin_roleplay_gateway.gate.stable_uniform", return_value=.99):
                    for incoming, when, reason in (("1", 1000, "selected"), ("2", 1001, "cooldown"),
                                                   ("3", 1016, "selected"), ("4", 1032, "selected"),
                                                   ("5", 1048, "quota")):
                        item = event(incoming)
                        self.assertTrue(await store.claim(item, purpose="roleplay"))
                        decision = await store.decide_pacing(item, policy, now=when)
                        self.assertEqual(decision.reason, reason)
                        self.assertEqual(decision.selected, reason == "selected")
                    qq = Envelope("qq", "900", "100", "1", "group", group_id="200")
                    self.assertTrue(await store.claim(qq, purpose="roleplay"))
                    decision = await store.decide_pacing(qq, replace(policy, probability=.6), now=1000)
                    self.assertEqual(decision.reason, "probability")
            finally:
                await store.close()


class NativeMemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    open_stores = fixture.RoleplayStreamTests.open_stores
    factory = fixture.RoleplayStreamTests.factory
    make_service = fixture.RoleplayStreamTests.make_service
    asyncTearDown = fixture.RoleplayStreamTests.asyncTearDown
    dispatch = fixture.RoleplayStreamTests.dispatch
    restart = fixture.RoleplayStreamTests.restart
    payload = fixture.RoleplayStreamTests.payload

    async def asyncSetUp(self):
        await fixture.RoleplayStreamTests.asyncSetUp(self)
        self.settings = replace(settings(), send_timeout_seconds=.4)
        self.service.settings = self.settings
        self.service.matcher = WakeMatcher(self.settings.wake_names, (), lambda code: None)

    async def send(self, item, text):
        self.outputs.append((item.incoming_message_id, text))
        return ApiAccepted()

    async def test_bound_private_voice_history_is_separate_from_group_and_other_contact(self):
        self.service.settings = replace(self.settings, admin_private_ids=frozenset({"wxid_peer"}))
        private = event(kind="private", group_id=None, sender_id="wxid_peer", input_modality="voice",
                        text="私聊里的代号是雨后银杏。", mentions=())
        self.assertEqual(await self.dispatch(private), "ACCEPTED")
        snapshot = await self.memory.snapshot(self.service.namespace(private))
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(snapshot.turns[0].inputs[0].text, private.text)
        self.assertEqual(await self.dispatch(event("2")), "ACCEPTED")
        self.assertNotIn("雨后银杏", json.dumps(self.payload(), ensure_ascii=False))
        self.assertEqual(await self.dispatch(replace(private, incoming_message_id="3", sender_id="wxid_other")), "IGNORED")

    async def test_success_persists_honest_receipt_and_restart_deduplication(self):
        item = event(str(2**64 - 1))
        self.assertEqual(await self.dispatch(item), "ACCEPTED")
        namespace = self.service.namespace(item)
        snapshot = await self.memory.snapshot(namespace)
        self.assertEqual(len(snapshot.turns), 1)
        reply = snapshot.turns[0].replies[0]
        self.assertEqual(reply.delivery_status, "API_ACCEPTED")
        self.assertIsNone(reply.message_id)
        self.assertTrue(reply.delivery_ref)
        row = await self.receipts.get(item.platform_id, item.incoming_message_id)
        self.assertEqual(row["status"], "API_ACCEPTED")
        self.assertIsNone(row["outgoing_message_id"])
        await self.restart()
        self.assertEqual(await self.dispatch(item), "DUPLICATE")
        self.assertEqual(await self.memory.snapshot(namespace), snapshot)
        self.assertEqual(len(self.outputs), 1)

    async def test_group_members_share_history_but_other_rooms_and_platforms_do_not(self):
        first = event(text="三叶，群内代号是青柚灯塔。")
        second = event("2", sender_id="wxid_other", text="三叶，刚才群内代号是什么？")
        self.assertEqual(self.service.namespace(first), self.service.namespace(second))
        await self.dispatch(first)
        await self.dispatch(second)
        self.assertIn("青柚灯塔", json.dumps(self.payload(), ensure_ascii=False))
        snapshot = await self.memory.snapshot(self.service.namespace(first))
        self.assertEqual([turn.inputs[0].sender_id for turn in snapshot.turns],
                         ["wxid_member", "wxid_other"])
        await self.dispatch(event("3", group_id="201@chatroom", text="三叶，另一个群说过什么？"))
        self.assertNotIn("青柚灯塔", json.dumps(self.payload(), ensure_ascii=False))
        for platform, kind, peer in (("qq", "group", "200@chatroom"),
                                     ("clawbot", "private", "wxid_member"),
                                     ("fixture-native", "private", "wxid_member")):
            snapshot = await self.memory.snapshot(Namespace(platform, kind, peer, "neutral-guide"))
            self.assertEqual(snapshot.turns, ())

    async def test_native_commands_require_real_mention_and_admin_without_model_calls(self):
        item = event(text="/记忆 编辑 合成群摘要", sender_id="wxid_admin")
        self.assertEqual(await self.dispatch(replace(item, mentions=())), "IGNORED")
        self.assertEqual(await self.dispatch(replace(item, mentions=("all",))), "IGNORED")
        self.assertEqual(await self.dispatch(item), "ACCEPTED")
        namespace = self.service.namespace(item)
        self.assertEqual((await self.memory.snapshot(namespace)).summary, "合成群摘要")
        self.assertEqual(self.backend.calls, [])
        row = await self.receipts.get(item.platform_id, item.incoming_message_id)
        self.assertEqual(row["status"], "API_ACCEPTED")
        self.assertIsNone(row["outgoing_message_id"])
        await self.dispatch(event("2", text="/记忆 清空全部"))
        self.assertEqual(self.outputs[-1][1], DENIED)
        self.assertEqual((await self.memory.snapshot(namespace)).summary, "合成群摘要")
        await self.dispatch(event("3", text="/记忆 查看", sender_id="wxid_admin"))
        self.assertIn("合成群摘要", self.outputs[-1][1])

    async def test_empty_admin_list_never_grants_group_memory_management(self):
        self.service.settings = replace(self.settings, memory_admin_ids=frozenset())
        await self.dispatch(event(text="/记忆 清空全部", sender_id="wxid_admin"))
        self.assertEqual(self.outputs[-1][1], DENIED)
        self.assertEqual(self.backend.calls, [])

    async def test_command_and_chat_reject_bad_members_private_events_and_invalid_message_ids(self):
        for text in ("三叶你好", "/记忆 查看"):
            for change in ({"sender_id": "gh_public"}, {"sender_id": "bad:member"},
                           {"kind": "private", "group_id": None}, {"group_id": "202@chatroom"}):
                self.assertEqual(await self.dispatch(event(text=text, **change)), "IGNORED")
            for incoming in (None, "01", str(2**64)):
                self.assertEqual(await self.dispatch(event(incoming, text=text)), "INVALID")
        self.assertEqual(self.outputs, [])
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
