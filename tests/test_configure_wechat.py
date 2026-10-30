"""微信配置事务测试；全部账号、凭据和人设均为临时合成资料。"""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest

import test_check_runtime as fixture

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_wechat", PROJECT / "scripts/configure_wechat.py")
assert SPEC and SPEC.loader
wechat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wechat)


class ConfigureWechatTests(unittest.TestCase):
    write_json = fixture.CheckRuntimeTests.write_json
    runtime_bytes = fixture.CheckRuntimeTests.runtime_bytes
    enable_gateway_fixture = fixture.CheckRuntimeTests.enable_gateway_fixture
    enable_stream_fixture = fixture.CheckRuntimeTests.enable_stream_fixture
    enable_roleplay_fixture = fixture.CheckRuntimeTests.enable_roleplay_fixture

    def setUp(self):
        fixture.CheckRuntimeTests.setUp(self)
        self.gate, _ = self.enable_roleplay_fixture()
        self.peer = "synthetic-user@im.wechat"
        self.node = {"id": "wechat-sakura", "type": "weixin_oc", "enable": True,
                     "weixin_oc_token": "synthetic-token-must-never-export",
                     "weixin_oc_account_id": "synthetic-bot",
                     "weixin_oc_context_tokens": {self.peer: "synthetic-context-secret"},
                     "weixin_oc_sync_buf": "synthetic-polling-state"}
        self.config["platform"].append(self.node)
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.plugin_path = self.root / ("runtime/" + wechat.gateway.PLUGIN_FILE)
        self.config_path = self.root / "runtime/astrbot/cmd_config.json"

    def load(self):
        return json.loads(self.plugin_path.read_text("utf-8")), json.loads(self.config_path.read_text("utf-8"))

    def test_dry_run_keeps_files_and_hides_credentials(self):
        before = self.runtime_bytes()
        result = wechat.execute(self.root, operation="enable", peer_id=self.peer)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.runtime_bytes(), before)
        self.assertFalse((self.root / "runtime/backups").exists())
        encoded = json.dumps(result)
        for secret in (self.password, self.token, self.peer, self.node["weixin_oc_token"],
                       self.node["weixin_oc_context_tokens"][self.peer]):
            self.assertNotIn(secret, encoded)

    def test_enable_only_changes_four_fields_and_one_whitelist_entry(self):
        wechat.execute(self.root, operation="enable", peer_id=self.peer, apply=True)
        gate, config = self.load()
        self.assertEqual(gate, {**self.gate, "wechat_enabled": True, "wechat_platform_id": "wechat-sakura",
                               "wechat_allowed_private_ids": [self.peer], "wechat_admin_ids": [self.peer]})
        expected = copy.deepcopy(self.config)
        expected["platform_settings"]["id_whitelist"].append("wechat-sakura:FriendMessage:" + self.peer)
        self.assertEqual(config, expected)
        self.assertTrue(wechat.checker.check_runtime(self.root)["ok"])
        backup = next((self.root / "runtime/backups").iterdir())
        self.assertEqual(json.loads((backup / "cmd_config.json").read_text("utf-8")), self.config)

    def test_idempotent_enable_preserves_bytes(self):
        wechat.execute(self.root, operation="enable", peer_id=self.peer, apply=True)
        before = self.runtime_bytes()
        result = wechat.execute(self.root, operation="enable", peer_id=self.peer, apply=True)
        self.assertEqual(result["status"], "already_configured")
        self.assertEqual(self.runtime_bytes(), before)

    def test_disable_preserves_native_state_and_binding_but_removes_access(self):
        wechat.execute(self.root, operation="enable", peer_id=self.peer, apply=True)
        wechat.execute(self.root, operation="disable", apply=True)
        gate, config = self.load()
        self.assertFalse(gate["wechat_enabled"])
        self.assertEqual(gate["wechat_allowed_private_ids"], [self.peer])
        self.assertEqual(config, self.config)

    def test_prepare_can_run_before_native_node_exists(self):
        self.config["platform"].pop()
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        wechat.execute(self.root, apply=True)
        gate, config = self.load()
        self.assertFalse(gate["wechat_enabled"])
        self.assertEqual(gate["wechat_allowed_private_ids"], [])
        self.assertEqual(config, self.config)

    def test_unknown_peer_or_absent_native_context_cannot_be_bound(self):
        before = self.runtime_bytes()
        with self.assertRaisesRegex(wechat.ConfigurationError, "peer_must_send"):
            wechat.execute(self.root, operation="enable", peer_id="someone-else", apply=True)
        self.assertEqual(self.runtime_bytes(), before)

    def test_invalid_identity_and_operation_rejected_without_writes(self):
        before = self.runtime_bytes()
        for value in (None, "", "x:y", "line\nbreak", "x y", "x" * 129):
            with self.subTest(peer=value), self.assertRaises(wechat.ConfigurationError):
                wechat.execute(self.root, operation="enable", peer_id=value, apply=True)
        for operation in ("prepare", "disable", "other"):
            with self.subTest(operation=operation), self.assertRaises(wechat.ConfigurationError):
                wechat.execute(self.root, operation=operation, peer_id=self.peer, apply=True)
        self.assertEqual(self.runtime_bytes(), before)

    def test_native_login_required_and_not_reset(self):
        self.node["weixin_oc_token"] = ""
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        before = self.runtime_bytes()
        with self.assertRaisesRegex(wechat.ConfigurationError, "native_wechat_login_required"):
            wechat.execute(self.root, operation="enable", peer_id=self.peer, apply=True)
        self.assertEqual(self.runtime_bytes(), before)

    def test_stale_native_poll_state_prevents_apply(self):
        plan = wechat.make_plan(self.root, operation="enable", peer_id=self.peer)
        self.node["weixin_oc_sync_buf"] = "new-native-state"
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        gate_before = self.plugin_path.read_bytes()
        with self.assertRaises(wechat.gateway.ConfigurationError):
            wechat.gateway.apply_plan(plan)
        self.assertEqual(self.plugin_path.read_bytes(), gate_before)
        self.assertEqual(self.load()[1]["platform"][1]["weixin_oc_sync_buf"], "new-native-state")

    def test_cli_errors_do_not_leak_supplied_identity(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = wechat.main(["--root", str(self.root), "--operation", "enable",
                                  "--peer-id", "not-a-confirmed-peer"])
        self.assertEqual(result, 1)
        self.assertNotIn("not-a-confirmed-peer", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "peer_must_send_to_native_bot_first")


if __name__ == "__main__":
    unittest.main()
