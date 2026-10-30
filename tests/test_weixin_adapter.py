"""Synthetic native Weixin contracts; no AstrBot process or network calls."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway import weixin_adapter as wa
from plugins.astrbot_plugin_roleplay_gateway.gate import ApiAccepted, DefiniteSendFailure

PRIVATE = "FriendMessage"


class FakeNativePlatform:
    def __init__(self, platform_id="test-wechat"):
        self.platform_id = platform_id
        self.calls = []
        self.result = True
        self.failure = None

    def meta(self):
        return types.SimpleNamespace(name="weixin_oc", id=self.platform_id)

    async def _send_items_to_session(self, user_id, item_list, *, cache_components=None,
                                     cache_message_str=None):
        self.calls.append((user_id, item_list))
        if self.failure:
            raise self.failure
        return self.result


class FakeWeixinEvent:
    def __init__(self, *, platform_id="test-wechat", sender="user-abc@im.wechat",
                 incoming="wx:message-123", text="你好", platform=None):
        self.platform = platform or FakeNativePlatform(platform_id)
        self.platform_id, self.sender = platform_id, sender
        self.message_obj = types.SimpleNamespace(
            self_id=platform_id, sender=types.SimpleNamespace(user_id=sender),
            type=PRIVATE, group_id="", session_id=sender, message_id=incoming,
            message_str=text,
            raw_message={"message_type": 1, "from_user_id": sender, "message_id": incoming,
                         "item_list": [{"type": 1, "text_item": {"text": text}}]},
        )
        self.session = types.SimpleNamespace(platform_name=platform_id,
                                             message_type=PRIVATE, session_id=sender)
        self.created_at = time.time()
        self.stopped = False

    def get_platform_name(self):
        return "weixin_oc"

    def get_platform_id(self):
        return self.platform_id

    def get_self_id(self):
        return self.message_obj.self_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_message_type(self):
        return self.message_obj.type

    def get_group_id(self):
        return self.message_obj.group_id

    def get_session_id(self):
        return self.session.session_id

    def stop_event(self):
        self.stopped = True


def native_contract():
    return wa.ASTRBOT_VERSION, FakeNativePlatform, FakeWeixinEvent, PRIVATE


class WeixinSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.contract = patch.object(wa, "_runtime_contract", native_contract)
        self.contract.start()
        self.addCleanup(self.contract.stop)

    def snapshot(self, event):
        return wa.snapshot_weixin_event(event, platform_id="test-wechat")

    def test_original_stable_id_and_text_not_wake_modified_or_quoted(self):
        event = FakeWeixinEvent(text="三叶，早上好。")
        event.message_str = "早上好。"
        event.message_obj.raw_message["item_list"][0]["ref_msg"] = {
            "item_list": [{"type": 1, "text_item": {"text": "引用不入本轮"}}]}
        envelope = self.snapshot(event)
        self.assertEqual(envelope.text, "三叶，早上好。")
        self.assertEqual(envelope.incoming_message_id, "wx:message-123")
        self.assertEqual(envelope.session, "test-wechat:private:user-abc@im.wechat")
        self.assertEqual(envelope.received_at, event.created_at)

    def test_msg_id_fallback_is_original_and_stable(self):
        event = FakeWeixinEvent()
        raw = event.message_obj.raw_message
        raw["msg_id"] = raw.pop("message_id")
        self.assertEqual(self.snapshot(event).incoming_message_id, "wx:message-123")

    def test_framework_generated_id_does_not_replace_missing_raw_id(self):
        event = FakeWeixinEvent(incoming="framework-random-uuid")
        event.message_obj.raw_message.pop("message_id")
        with self.assertRaisesRegex(ValueError, "stable_message_id"):
            self.snapshot(event)

    def test_invalid_or_changed_stable_ids_rejected(self):
        for value in (True, None, "", "bad id", "\0", "x" * 129, "different"):
            event = FakeWeixinEvent()
            event.message_obj.raw_message["message_id"] = value
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                self.snapshot(event)

    def test_identity_and_session_mismatches_rejected(self):
        mutations = (
            lambda e: setattr(e, "platform_id", "other"),
            lambda e: setattr(e.platform, "platform_id", "other"),
            lambda e: setattr(e.message_obj, "self_id", "account-is-not-instance"),
            lambda e: setattr(e.message_obj.sender, "user_id", "other"),
            lambda e: setattr(e.session, "session_id", "other"),
            lambda e: setattr(e.message_obj, "session_id", "other"),
            lambda e: setattr(e.session, "platform_name", "other"),
            lambda e: e.message_obj.raw_message.update(from_user_id="bad:user"),
        )
        for mutate in mutations:
            event = FakeWeixinEvent()
            mutate(event)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.snapshot(event)

    def test_groups_outgoing_nontext_and_empty_events_rejected(self):
        mutations = (
            lambda e: setattr(e.message_obj, "type", "GroupMessage"),
            lambda e: setattr(e.session, "message_type", "GroupMessage"),
            lambda e: setattr(e.message_obj, "group_id", "group-1"),
            lambda e: e.message_obj.raw_message.update(group_id="group-1"),
            lambda e: e.message_obj.raw_message.update(message_type=2),
            lambda e: e.message_obj.raw_message.update(item_list=[]),
            lambda e: e.message_obj.raw_message.update(item_list=[{"type": 2}]),
        )
        for mutate in mutations:
            event = FakeWeixinEvent()
            mutate(event)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.snapshot(event)

    def test_contract_rejects_version_and_signature_drift(self):
        with patch.object(wa, "_runtime_contract", return_value=(
                "9.0.0", FakeNativePlatform, FakeWeixinEvent, PRIVATE)):
            with self.assertRaisesRegex(ValueError, "version"):
                wa.check_runtime_contract()
        class Changed(FakeNativePlatform):
            async def _send_items_to_session(self, user_id, item_list):
                return True
        with patch.object(wa, "_runtime_contract", return_value=(
                wa.ASTRBOT_VERSION, Changed, FakeWeixinEvent, PRIVATE)):
            with self.assertRaisesRegex(ValueError, "signature"):
                wa.check_runtime_contract()

    def test_default_closed_and_identity_replaces_qq_without_inheriting_groups(self):
        config = {"platform_id": "test-qq", "bot_self_id": "900", "enabled": True,
                  "admin_private_ids": ["100"], "allowed_group_ids": ["200"]}
        settings = wa.settings_from_config(config)
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.platform_id, "wechat-sakura")
        self.assertEqual(settings.bot_self_id, settings.platform_id)
        self.assertFalse(settings.admin_private_ids)
        self.assertFalse(settings.allowed_group_ids)
        self.assertEqual(settings.id_kind, "weixin")

    def test_wechat_settings_strict_identity_lists_even_when_disabled(self):
        base = {"platform_id": "test-qq", "bot_self_id": "900"}
        for value in (True, 123, None, " x", "x ", "x y", "x:y", "x\n", "x" * 129, ""):
            with self.subTest(platform=value), self.assertRaises(ValueError):
                wa.settings_from_config({**base, "wechat_platform_id": value})
            with self.subTest(peer=value), self.assertRaises(ValueError):
                wa.settings_from_config({**base, "wechat_allowed_private_ids": [value]})
        for value in (None, "peer", ["peer", "peer"], ["x" + str(i) for i in range(33)], [{}]):
            with self.subTest(ids=value), self.assertRaises(ValueError):
                wa.settings_from_config({**base, "wechat_allowed_private_ids": value})
        with self.assertRaisesRegex(ValueError, "admin_not_allowed"):
            wa.settings_from_config({**base, "wechat_admin_ids": ["unallowed"]})
        with self.assertRaisesRegex(ValueError, "conflicts"):
            wa.settings_from_config({**base, "wechat_platform_id": "test-qq"})
        with self.assertRaisesRegex(ValueError, "invalid_wechat_enabled"):
            wa.settings_from_config({**base, "wechat_enabled": "true"})

    def test_enabled_requires_bound_admin_subset_and_roleplay_mode(self):
        base = {"platform_id": "test-qq", "bot_self_id": "900", "wechat_enabled": True,
                "wechat_allowed_private_ids": ["peer"], "wechat_admin_ids": ["peer"]}
        for mode in ("neutral_receipt", "neutral_stream"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "roleplay_binding"):
                wa.settings_from_config({**base, "mode": mode})
        for field in ("wechat_allowed_private_ids", "wechat_admin_ids"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                wa.settings_from_config({**base, "mode": "roleplay_stream", field: []})


class WeixinSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.contract = patch.object(wa, "_runtime_contract", native_contract)
        self.contract.start()
        self.addCleanup(self.contract.stop)
        self.event = FakeWeixinEvent()
        self.item = wa.snapshot_weixin_event(self.event, platform_id="test-wechat")

    async def test_strict_true_returns_api_evidence_without_platform_id(self):
        result = await wa.send_weixin_text(self.event.platform, self.item, "完整一句。")
        self.assertIsInstance(result, ApiAccepted)
        self.assertEqual(self.event.platform.calls, [
            (self.item.sender_id, [{"type": 1, "text_item": {"text": "完整一句。"}}])])

    async def test_false_is_definite_rejection_without_retry(self):
        self.event.platform.result = False
        with self.assertRaises(DefiniteSendFailure):
            await wa.send_weixin_text(self.event.platform, self.item, "test")
        self.assertEqual(len(self.event.platform.calls), 1)

    async def test_none_truthy_mapping_and_one_are_unknown_not_success(self):
        for result in (None, 1, {"message_id": "fake"}):
            self.event.platform.result = result
            with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, "unknown"):
                await wa.send_weixin_text(self.event.platform, self.item, "test")
        self.assertEqual(len(self.event.platform.calls), 3)

    async def test_exception_and_cancel_are_preserved_without_retry(self):
        for error in (TimeoutError(), RuntimeError(), asyncio.CancelledError()):
            self.event.platform.failure = error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                await wa.send_weixin_text(self.event.platform, self.item, "test")
        self.assertEqual(len(self.event.platform.calls), 3)

    async def test_changed_platform_or_instance_method_cannot_send(self):
        self.event.platform.platform_id = "other"
        with self.assertRaises(DefiniteSendFailure):
            await wa.send_weixin_text(self.event.platform, self.item, "test")
        self.event.platform.platform_id = "test-wechat"
        async def replacement(*args, **kwargs):
            return True
        self.event.platform._send_items_to_session = replacement
        with self.assertRaises(DefiniteSendFailure):
            await wa.send_weixin_text(self.event.platform, self.item, "test")
        self.assertEqual(self.event.platform.calls, [])


if __name__ == "__main__":
    unittest.main()
