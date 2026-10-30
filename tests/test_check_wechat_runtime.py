"""Synthetic WeChat preflight fixtures; no production files, APIs or tokens."""

from contextlib import redirect_stdout
import copy
import io
import json
import unittest

import test_check_runtime as fixtures

checker = fixtures.checker
GATE_FILE = f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json"
CONFIG_FILE = "runtime/astrbot/cmd_config.json"
WECHAT_ID = "opaque-user_测试@im"
WECHAT_TOKEN = "synthetic-wechat-token-must-not-appear-in-output"
ACCOUNT_ID = "synthetic-account-must-not-appear-in-output"


class WechatRuntimeTests(unittest.TestCase):
    write_json = fixtures.CheckRuntimeTests.write_json
    runtime_bytes = fixtures.CheckRuntimeTests.runtime_bytes
    assert_code = fixtures.CheckRuntimeTests.assert_code
    enable_gateway_fixture = fixtures.CheckRuntimeTests.enable_gateway_fixture
    enable_stream_fixture = fixtures.CheckRuntimeTests.enable_stream_fixture
    enable_roleplay_fixture = fixtures.CheckRuntimeTests.enable_roleplay_fixture

    def setUp(self):
        fixtures.CheckRuntimeTests.setUp(self)
        self.gate, self.card = self.enable_roleplay_fixture()
        self.qq_whitelist = list(self.config["platform_settings"]["id_whitelist"])

    def node(self, **overrides):
        result = {"id": checker.WECHAT_PLATFORM_ID, "type": "weixin_oc", "enable": True,
                  "weixin_oc_token": WECHAT_TOKEN, "weixin_oc_account_id": ACCOUNT_ID,
                  "weixin_oc_base_url": "https://synthetic.invalid",
                  "weixin_oc_context_tokens": {WECHAT_ID: "synthetic-context-secret"}}
        result.update(overrides)
        return result

    def save(self):
        self.write_json(CONFIG_FILE, self.config)
        self.write_json(GATE_FILE, self.gate)

    def enabled(self):
        self.config["platform"].append(self.node())
        self.gate.update(wechat_enabled=True, wechat_platform_id=checker.WECHAT_PLATFORM_ID,
                         wechat_allowed_private_ids=[WECHAT_ID], wechat_admin_ids=[WECHAT_ID])
        self.config["platform_settings"]["id_whitelist"].append(
            f"{checker.WECHAT_PLATFORM_ID}:FriendMessage:{WECHAT_ID}")
        self.save()

    def test_legacy_qq_config_without_wechat_fields_is_unchanged(self):
        before = self.runtime_bytes()
        result = checker.check_runtime(self.root)
        self.assertTrue(result["ok"])
        self.assertFalse(result["wechat_platform_configured"])
        self.assertFalse(result["live_wechat_login_verified"])
        self.assertEqual(before, self.runtime_bytes())

    def test_unbound_node_can_log_in_with_plugin_disabled_and_no_new_whitelist(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.config["platform"] = [self.config["platform"][0], self.node(
                    enable=enabled, weixin_oc_token="", weixin_oc_account_id="")]
                self.gate.update(wechat_enabled=False, wechat_platform_id=checker.WECHAT_PLATFORM_ID,
                                 wechat_allowed_private_ids=[], wechat_admin_ids=[])
                self.save()
                result = checker.check_runtime(self.root)
                self.assertTrue(result["wechat_platform_configured"])
                self.assertEqual(self.config["platform_settings"]["id_whitelist"], self.qq_whitelist)

    def test_prepared_inert_config_allows_native_node_without_whitelist_expansion(self):
        self.config["plugin_set"] = []
        self.config["platform"].append(self.node(weixin_oc_token="", weixin_oc_account_id=""))
        self.save()
        self.assertEqual(checker.check_runtime(self.root)["runtime_profile"], "prepared-inert")
        self.config["platform_settings"]["id_whitelist"].append(
            f"{checker.WECHAT_PLATFORM_ID}:FriendMessage:{WECHAT_ID}")
        self.save()
        self.assert_code("WHITELIST_CHANGED")

    def test_enabled_scope_passes_without_exposing_or_mutating_credentials(self):
        self.enabled()
        before = self.runtime_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(checker.main(["--root", str(self.root), "--json"]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["wechat_private_scope_verified"])
        self.assertFalse(result["live_wechat_login_verified"])
        for secret in (WECHAT_TOKEN, ACCOUNT_ID, WECHAT_ID, "synthetic-context-secret", self.token):
            self.assertNotIn(secret, output.getvalue())
        self.assertEqual(before, self.runtime_bytes())

    def test_wechat_can_remain_enabled_when_only_qq_gateway_is_disabled(self):
        self.enabled()
        self.gate["enabled"] = False
        self.save()
        self.assertTrue(checker.check_runtime(self.root)["ok"])

    def test_platform_order_does_not_change_qq_binding(self):
        self.enabled()
        self.config["platform"].reverse()
        self.save()
        self.assertTrue(checker.check_runtime(self.root)["ok"])

    def test_platform_list_rejects_missing_duplicate_or_foreign_nodes(self):
        qq = self.config["platform"][0]
        for nodes in ([], [self.node()], [qq, copy.deepcopy(qq)],
                      [qq, self.node(), self.node()], [qq, {"id": "foreign", "type": "weixin_oc", "enable": False}],
                      [qq, self.node(type="aiocqhttp")], [qq, self.node(enable=1)],
                      [qq, "invalid"]):
            with self.subTest(nodes=len(nodes)):
                self.config["platform"] = copy.deepcopy(nodes)
                self.save()
                self.assert_code("PLATFORM_CONFIG_INVALID")

    def test_wechat_does_not_weaken_qq_platform_checks(self):
        self.enabled()
        qq = copy.deepcopy(self.config["platform"][0])
        for field, value in (("enable", False), ("type", "weixin_oc"),
                             ("ws_reverse_host", "127.0.0.1"), ("ws_reverse_port", 3001)):
            with self.subTest(field=field):
                self.config["platform"][0] = {**qq, field: value}
                self.save()
                self.assert_code("PLATFORM_CONFIG_INVALID")

    def test_active_wechat_requires_native_node_and_roleplay_mode(self):
        self.enabled()
        node = self.config["platform"].pop()
        self.save()
        self.assert_code("WECHAT_NOT_READY")
        self.config["platform"].append({**node, "enable": False})
        self.save()
        self.assert_code("WECHAT_NOT_READY")
        self.config["platform"][-1] = node
        for mode in ("neutral_receipt", "neutral_stream"):
            self.gate["mode"] = mode
            self.save()
            self.assert_code("WECHAT_NOT_READY")

    def test_active_wechat_requires_nonempty_native_account_and_token(self):
        self.enabled()
        node = copy.deepcopy(self.config["platform"][-1])
        for field in ("weixin_oc_token", "weixin_oc_account_id"):
            for invalid in (None, "", "  ", False, 123):
                with self.subTest(field=field, invalid=invalid):
                    self.config["platform"][-1] = {**node, field: invalid}
                    self.save()
                    self.assert_code("WECHAT_ACCOUNT_NOT_CONFIGURED")

    def test_failed_cli_never_emits_native_credentials_or_user_ids(self):
        self.enabled()
        self.config["platform"][-1]["weixin_oc_account_id"] = ""
        self.save()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(checker.main(["--root", str(self.root), "--json"]), 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "WECHAT_ACCOUNT_NOT_CONFIGURED")
        for value in (WECHAT_ID, WECHAT_TOKEN, self.password, self.token):
            self.assertNotIn(value, output.getvalue())

    def test_native_llm_stays_disabled_with_wechat(self):
        self.enabled()
        self.config["provider_settings"]["enable"] = True
        self.save()
        self.assert_code("NATIVE_PROVIDER_ENABLED")

    def test_enabled_requires_nonempty_allowed_and_admin_subset(self):
        self.enabled()
        for allowed, admins in (([], []), ([WECHAT_ID], []), ([], [WECHAT_ID]),
                                ([WECHAT_ID], ["different-user"])):
            with self.subTest(allowed=len(allowed), admins=len(admins)):
                self.gate.update(wechat_allowed_private_ids=allowed, wechat_admin_ids=admins)
                self.save()
                self.assert_code("WECHAT_SCOPE_INVALID")

    def test_disabled_keeps_binding_metadata_but_no_wechat_framework_whitelist(self):
        self.enabled()
        self.gate["wechat_enabled"] = False
        self.save()
        self.assert_code("WHITELIST_CHANGED")
        self.config["platform_settings"]["id_whitelist"] = self.qq_whitelist
        self.save()
        self.assertTrue(checker.check_runtime(self.root)["ok"])

    def test_opaque_identity_validation_including_bounds_and_separators(self):
        invalid_lists = (None, {}, WECHAT_ID, [123], [True], [None], [""], [" "],
                         ["a b"], ["a\tb"], ["a\nb"], ["a:b"], ["a\u200bb"],
                         ["a\x7fb"], ["a\u0085b"], ["a" * 129],
                         [WECHAT_ID, WECHAT_ID], [str(i) for i in range(33)])
        for field in ("wechat_allowed_private_ids", "wechat_admin_ids"):
            for values in invalid_lists:
                with self.subTest(field=field, value_type=type(values).__name__):
                    self.gate.update(wechat_enabled=False, wechat_allowed_private_ids=[], wechat_admin_ids=[])
                    self.gate[field] = values
                    self.save()
                    self.assert_code("WECHAT_SCOPE_INVALID")

    def test_opaque_identity_maximum_length_and_multiple_contacts(self):
        self.enabled()
        allowed = ["u" * 128, WECHAT_ID]
        self.gate.update(wechat_allowed_private_ids=allowed, wechat_admin_ids=[WECHAT_ID])
        self.config["platform_settings"]["id_whitelist"] = self.qq_whitelist + [
            f"{checker.WECHAT_PLATFORM_ID}:FriendMessage:{value}" for value in allowed]
        self.save()
        self.assertTrue(checker.check_runtime(self.root)["ok"])

    def test_wechat_enable_and_platform_id_are_strict_even_when_disabled(self):
        for field, value in (("wechat_enabled", 0), ("wechat_enabled", "false"),
                             ("wechat_enabled", None), ("wechat_platform_id", "other"),
                             ("wechat_platform_id", ""), ("wechat_platform_id", None)):
            with self.subTest(field=field, value=value):
                self.gate.update(wechat_enabled=False, wechat_platform_id=checker.WECHAT_PLATFORM_ID)
                self.gate[field] = value
                self.save()
                self.assert_code("WECHAT_SCOPE_INVALID")

    def test_exact_whitelist_rejects_extra_qq_or_wechat_or_missing_qq(self):
        self.enabled()
        expected = copy.deepcopy(self.config["platform_settings"]["id_whitelist"])
        mutations = (expected + ["qq-sakura:FriendMessage:999999999"],
                     expected + ["wechat-sakura:FriendMessage:unapproved"],
                     expected + [f"wechat-sakura:GroupMessage:{WECHAT_ID}"],
                     expected[1:], expected[:-1], expected + [expected[-1]],
                     self.qq_whitelist + [f"other-wechat:FriendMessage:{WECHAT_ID}"])
        for values in mutations:
            self.config["platform_settings"]["id_whitelist"] = values
            self.save()
            self.assert_code("WHITELIST_CHANGED")

    def test_wechat_admin_cannot_be_added_to_qq_global_admins(self):
        self.enabled()
        self.config["admins_id"].append(WECHAT_ID)
        self.save()
        self.assert_code("WHITELIST_CHANGED")

    def test_qq_gateway_scope_and_snapshot_are_still_strict(self):
        self.enabled()
        old = self.gate["admin_private_ids"]
        self.gate["admin_private_ids"] = [WECHAT_ID]
        self.save()
        self.assert_code("GATEWAY_SCOPE_CHANGED")
        self.gate["admin_private_ids"] = old
        self.save()
        self.inputs["qq"]["test_group_ids"] = ["999999999"]
        self.write_json("config/connection-inputs.local.json", self.inputs)
        self.assert_code("DEPLOYMENT_ACCOUNT_CHANGED")

    def test_two_overrides_validate_planned_enable_without_mutating_stored_files(self):
        before = self.runtime_bytes()
        proposed_config = copy.deepcopy(self.config)
        proposed_gate = copy.deepcopy(self.gate)
        proposed_config["platform"].append(self.node())
        proposed_config["platform_settings"]["id_whitelist"].append(
            f"wechat-sakura:FriendMessage:{WECHAT_ID}")
        proposed_gate.update(wechat_enabled=True, wechat_allowed_private_ids=[WECHAT_ID], wechat_admin_ids=[WECHAT_ID])
        desired_config, desired_gate = copy.deepcopy(proposed_config), copy.deepcopy(proposed_gate)
        result = checker.check_runtime(self.root, gateway_override=proposed_gate, config_override=proposed_config)
        self.assertTrue(result["wechat_platform_configured"])
        self.assertEqual(before, self.runtime_bytes())
        self.assertEqual((desired_config, desired_gate), (proposed_config, proposed_gate))
        self.assertFalse(checker.check_runtime(self.root)["wechat_platform_configured"])

    def test_one_sided_enable_proposal_is_rejected(self):
        proposed_gate = {**self.gate, "wechat_enabled": True,
                         "wechat_allowed_private_ids": [WECHAT_ID], "wechat_admin_ids": [WECHAT_ID]}
        with self.assertRaises(checker.RuntimeCheckError) as raised:
            checker.check_runtime(self.root, gateway_override=proposed_gate)
        self.assertEqual(raised.exception.code, "WECHAT_NOT_READY")

    def test_two_overrides_allow_disable_with_preserved_binding(self):
        self.enabled()
        before = self.runtime_bytes()
        proposed_gate = {**self.gate, "wechat_enabled": False}
        proposed_config = copy.deepcopy(self.config)
        proposed_config["platform_settings"]["id_whitelist"] = self.qq_whitelist
        self.assertTrue(checker.check_runtime(self.root, gateway_override=proposed_gate,
                                            config_override=proposed_config)["ok"])
        self.assertEqual(before, self.runtime_bytes())

    def test_overrides_do_not_bypass_qq_or_native_model_boundaries(self):
        self.enabled()
        for change, code in (({"admins_id": [WECHAT_ID]}, "WHITELIST_CHANGED"),
                             ({"provider_settings": {"enable": True}}, "NATIVE_PROVIDER_ENABLED")):
            with self.assertRaises(checker.RuntimeCheckError) as raised:
                checker.check_runtime(self.root, config_override={**self.config, **change})
            self.assertEqual(raised.exception.code, code)

    def test_overrides_still_require_readable_existing_configuration(self):
        for filename in (CONFIG_FILE, GATE_FILE):
            path = self.root / filename
            before = path.read_bytes()
            path.write_text("{", "utf-8")
            try:
                with self.assertRaises(checker.RuntimeCheckError) as raised:
                    checker.check_runtime(self.root, gateway_override=self.gate, config_override=self.config)
                self.assertEqual(raised.exception.code, "CONFIG_INVALID")
                self.assertEqual(path.read_text("utf-8"), "{")
            finally:
                path.write_bytes(before)

    def test_invalid_config_override_is_rejected_without_writes(self):
        before = self.runtime_bytes()
        for value in ([], "invalid", True):
            with self.assertRaises(checker.RuntimeCheckError) as raised:
                checker.check_runtime(self.root, config_override=value)
            self.assertEqual(raised.exception.code, "CONFIG_INVALID")
        self.assertEqual(before, self.runtime_bytes())


if __name__ == "__main__":
    unittest.main()
