"""Platform-scoped probability and binding validation; no runtime data."""
from copy import deepcopy
import unittest
from plugins.astrbot_plugin_roleplay_gateway.native_settings import settings_from_config, bridge_token
from plugins.astrbot_plugin_roleplay_gateway.gate import ConfigurationError, GateSettings
from plugins.astrbot_plugin_roleplay_gateway.weixin_adapter import settings_from_config as weixin_settings


def config():
    return dict(enabled=True, platform_id="qq-sakura", bot_self_id="12345",
                admin_private_ids=["23456"], allowed_group_ids=["34567"],
                memory_admin_ids=["23456"], wake_names=["三叶"], wake_regex=[],
                mode="roleplay_stream", persona_id="fixture", persona_name="fixture",
                persona_filename="fixture.txt", persona_sha256="a"*64,
                reply_pacing={"enabled": True, "probability": .6},
                wechat_enabled=True, wechat_platform_id="wechat-sakura",
                wechat_allowed_private_ids=["peer@im.wechat"], wechat_admin_ids=["peer@im.wechat"],
                native_enabled=True, native_self_id="wxid_bot",
                native_allowed_group_ids=["123@chatroom"], native_admin_ids=[],
                native_bridge_token="b"*64)


class NativeSettingsTests(unittest.TestCase):
    def test_probability_one_and_memory_namespace_do_not_change_other_platforms(self):
        value = config()
        before = deepcopy(value)
        native = settings_from_config(value)
        self.assertEqual(native.reply_pacing.probability, 1.0)
        self.assertEqual(native.reply_pacing.cooldown_seconds, 15)
        self.assertEqual(native.reply_pacing.max_rounds, 3)
        self.assertEqual(native.memory_admin_ids, frozenset())
        self.assertEqual(native.admin_private_ids, frozenset())
        self.assertEqual(native.platform_id, "wechat-native-sakura")
        self.assertEqual(native.bot_self_id, "wxid_bot")
        self.assertEqual(GateSettings.from_mapping(value).reply_pacing.probability, .6)
        self.assertEqual(weixin_settings(value).reply_pacing.probability, .6)
        self.assertEqual(value, before)

    def test_legacy_config_remains_disabled(self):
        value = {k:v for k,v in config().items() if not k.startswith("native_")}
        settings = settings_from_config(value)
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.allowed_group_ids)

    def test_enabled_requires_exact_single_group_token_and_persona(self):
        for changes in ({"native_allowed_group_ids": []},
                        {"native_allowed_group_ids": ["123@chatroom", "124@chatroom"]},
                        {"native_bridge_token": ""}, {"mode": "neutral_receipt"},
                        {"native_self_id": ""}, {"native_enabled": 1},
                        {"native_platform_id": "wechat-sakura"},
                        {"native_reply_probability": True}, {"native_reply_probability": 1.1}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(ConfigurationError):
                    settings_from_config({**config(), **changes})

    def test_native_wake_overrides_are_independent(self):
        value = config()
        value.update(native_wake_names=["三叶酱"], native_wake_regex=["^叫你$"], native_reply_probability=.9)
        actual = settings_from_config(value)
        self.assertEqual(actual.wake_names, ("三叶酱",))
        self.assertEqual(actual.wake_regex, ("^叫你$",))
        self.assertEqual(actual.reply_pacing.probability, .9)
        self.assertEqual(GateSettings.from_mapping(value).wake_names, ("三叶",))

    def test_token_errors_are_static(self):
        with self.assertRaisesRegex(ConfigurationError, "^invalid_native_bridge_token$"):
            bridge_token({"native_bridge_token": "do-not-echo-this"})
