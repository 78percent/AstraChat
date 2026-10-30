"""Offline fixture-only configuration, snapshot, and rollback tests."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import configure_native_wechat as configure
from scripts.wechat_native_bridge_core import COMMIT as UPSTREAM_COMMIT


class ConfigureNativeWechatTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "work/test-configure-native-wechat"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="fixture-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.probe_path = self.root / configure.PROBE_FILE
        self.plugin_path = self.root / configure.PLUGIN_FILE
        self.host_path = self.root / configure.HOST_FILE
        self.probe_path.parent.mkdir(parents=True)
        self.plugin_path.parent.mkdir(parents=True)
        self.probe = {"native_trial_authorized": True, "target_group_label": "test",
                      "expected_self": "wxid_synthetic_bot", "allowed_room": "12345@chatroom",
                      "upstream_commit": UPSTREAM_COMMIT}
        self.plugin = {"enabled": True, "platform_id": "fixture-qq", "bot_self_id": "900",
                       "allowed_group_ids": ["200"], "admin_private_ids": ["100"],
                       "wechat_enabled": True, "wechat_platform_id": "fixture-clawbot",
                       "reply_pacing": {"enabled": True, "probability": .6},
                       "mode": "roleplay_stream", "provider_id": "deepseek-flash-official",
                       "persona_id": "neutral-guide", "persona_name": "中性向导",
                       "persona_filename": "never-read-this-card.txt", "persona_sha256": "b" * 64,
                       "wake_names": ["三叶", "合成别名"], "wake_regex": ["^合成唤醒"],
                       "unrelated": {"retained": [1, 2, 3]}}
        self.write_fixtures()

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_fixtures(self):
        self.write_json(self.probe_path, self.probe)
        self.write_json(self.plugin_path, self.plugin)

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def backups(self):
        return list((self.root / "runtime/backups").glob("native-wechat-*"))

    def test_dry_run_never_writes_token_backups_or_existing_files(self):
        before = (self.probe_path.read_bytes(), self.plugin_path.read_bytes())
        with patch.object(configure.secrets, "token_hex", return_value="c" * 64) as generate:
            result = configure.execute(self.root)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["changed_file_count"], 2)
        self.assertEqual(result["admin_count"], 0)
        generate.assert_called_once_with(32)
        self.assertEqual((self.probe_path.read_bytes(), self.plugin_path.read_bytes()), before)
        self.assertFalse(self.host_path.exists())
        self.assertEqual(self.backups(), [])
        for sensitive in ("c" * 64, self.probe["expected_self"], self.probe["allowed_room"]):
            self.assertNotIn(sensitive, json.dumps(result))

    def test_apply_changes_only_native_fields_and_backs_up_original_bytes(self):
        before = self.plugin_path.read_bytes()
        result = configure.execute(self.root, apply=True)
        self.assertEqual(result["status"], "applied")
        plugin, host = self.read(self.plugin_path), self.read(self.host_path)
        self.assertEqual({key: value for key, value in plugin.items() if not key.startswith("native_")}, self.plugin)
        self.assertEqual(plugin["native_allowed_group_ids"], [self.probe["allowed_room"]])
        self.assertEqual(plugin["native_self_id"], self.probe["expected_self"])
        self.assertEqual(plugin["native_admin_ids"], [])
        self.assertEqual(plugin["native_wake_names"], self.plugin["wake_names"])
        self.assertEqual(plugin["native_wake_regex"], self.plugin["wake_regex"])
        self.assertEqual(plugin["native_reply_probability"], 1.0)
        self.assertRegex(plugin["native_bridge_token"], r"^[a-f0-9]{64}$")
        self.assertEqual(host, {"enabled": True, "platform_id": configure.PLATFORM,
                               "expected_self": self.probe["expected_self"],
                               "allowed_room": self.probe["allowed_room"], "url": configure.URL,
                               "upstream_commit": self.probe["upstream_commit"],
                               "token": plugin["native_bridge_token"]})
        backup = self.backups()[0]
        self.assertEqual((backup / self.plugin_path.name).read_bytes(), before)
        self.assertEqual(self.read(backup / "status.json")["status"], "applied")
        self.assertEqual(self.read(self.probe_path), self.probe)

    def test_existing_token_admins_and_unknown_native_plugin_fields_are_preserved(self):
        token = "d" * 64
        self.plugin.update(native_bridge_token=token, native_admin_ids=["wxid_real_admin"],
                           native_future_option={"keep": True})
        self.write_fixtures()
        self.write_json(self.host_path, {"token": token})
        configure.execute(self.root, apply=True)
        plugin, host = self.read(self.plugin_path), self.read(self.host_path)
        self.assertEqual(plugin["native_admin_ids"], ["wxid_real_admin"])
        self.assertEqual(plugin["native_future_option"], {"keep": True})
        self.assertEqual(plugin["native_bridge_token"], token)
        self.assertEqual(host["token"], token)

    def test_host_only_token_is_reused_without_rotation(self):
        self.write_json(self.host_path, {"token": "e" * 64})
        with patch.object(configure.secrets, "token_hex", side_effect=AssertionError("must reuse")):
            plan = configure.make_plan(self.root)
        desired = json.loads(plan["files"][0][2])
        self.assertEqual(desired["native_bridge_token"], "e" * 64)

    def test_repeat_apply_preserves_original_format_and_creates_no_extra_backup(self):
        configure.execute(self.root, apply=True)
        for path in (self.plugin_path, self.host_path):
            path.write_text(json.dumps(self.read(path), separators=(",", ":")), encoding="utf-8")
        before = (self.plugin_path.read_bytes(), self.host_path.read_bytes(), self.backups())
        self.assertEqual(configure.execute(self.root, apply=True)["status"], "already_configured")
        self.assertEqual((self.plugin_path.read_bytes(), self.host_path.read_bytes(), self.backups()), before)

    def test_missing_wake_fields_have_defaults_but_explicit_empty_lists_are_retained(self):
        self.plugin.pop("wake_names")
        self.plugin.pop("wake_regex")
        self.write_fixtures()
        desired = json.loads(configure.make_plan(self.root)["files"][0][2])
        self.assertEqual(desired["native_wake_names"], ["三叶", "佐仓三叶"])
        self.assertEqual(desired["native_wake_regex"], [])
        self.plugin.update(wake_names=[], wake_regex=[])
        self.write_fixtures()
        desired = json.loads(configure.make_plan(self.root)["files"][0][2])
        self.assertEqual(desired["native_wake_names"], [])

    def test_repeat_apply_preserves_independent_native_wake_customization(self):
        configure.execute(self.root, apply=True)
        plugin = self.read(self.plugin_path)
        plugin.update(native_wake_names=["原生独立别名"], native_wake_regex=["^独立唤醒"],
                      wake_names=["QQ已改名"], wake_regex=["^QQ新唤醒"])
        self.write_json(self.plugin_path, plugin)
        before = self.plugin_path.read_bytes()
        self.assertEqual(configure.execute(self.root, apply=True)["status"], "already_configured")
        self.assertEqual(self.plugin_path.read_bytes(), before)
        plugin.update(native_wake_names=[], native_wake_regex=[])
        self.write_json(self.plugin_path, plugin)
        desired = json.loads(configure.make_plan(self.root)["files"][0][2])
        self.assertEqual(desired["native_wake_names"], [])
        self.assertEqual(desired["native_wake_regex"], [])

    def test_nonpinned_commit_and_extra_host_fields_fail_during_dry_run(self):
        self.probe["upstream_commit"] = "a" * 40
        self.write_fixtures()
        with self.assertRaisesRegex(configure.ConfigurationError, "invalid_bridge_runtime_settings"):
            configure.execute(self.root)
        self.probe["upstream_commit"] = UPSTREAM_COMMIT
        self.write_fixtures()
        self.write_json(self.host_path, {"token": "e" * 64, "unknown_host_option": True})
        before = self.host_path.read_bytes()
        with self.assertRaisesRegex(configure.ConfigurationError, "invalid_bridge_runtime_settings"):
            configure.execute(self.root)
        self.assertEqual(self.host_path.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_invalid_roleplay_settings_fail_during_dry_run_without_reading_persona(self):
        baseline = copy.deepcopy(self.plugin)
        for mutation in ({"mode": "neutral_receipt"}, {"provider_id": "unsupported-provider"},
                         {"persona_sha256": "invalid"}, {"persona_name": ""},
                         {"reply_pacing": {"enabled": True, "probability": .6, "max_bubbles": 16}}):
            self.plugin = {**baseline, **mutation}
            self.write_fixtures()
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(configure.ConfigurationError, "invalid_native_runtime_settings"):
                    configure.execute(self.root)
        self.assertFalse(self.host_path.exists())
        self.assertEqual(self.backups(), [])

    def test_invalid_or_mismatched_existing_tokens_are_never_silently_rotated(self):
        for token in (None, True, "short", "A" * 64, "f" * 63):
            with self.subTest(token=token):
                self.plugin["native_bridge_token"] = token
                self.write_fixtures()
                with self.assertRaisesRegex(configure.ConfigurationError, "invalid_existing_bridge_token"):
                    configure.make_plan(self.root)
        self.plugin["native_bridge_token"] = "a" * 64
        self.write_fixtures()
        self.write_json(self.host_path, {"token": "b" * 64})
        with self.assertRaisesRegex(configure.ConfigurationError, "bridge_token_mismatch"):
            configure.execute(self.root, apply=True)
        self.assertEqual(self.backups(), [])

    def test_probe_must_have_explicit_authorization_and_exact_bound_identities(self):
        baseline = copy.deepcopy(self.probe)
        for mutation in ({"native_trial_authorized": False}, {"target_group_label": "other"},
                         {"expected_self": None}, {"expected_self": "gh_public"},
                         {"allowed_room": "test"}, {"allowed_room": "123"},
                         {"upstream_commit": "main"}):
            self.probe = {**baseline, **mutation}
            self.write_fixtures()
            with self.subTest(mutation=mutation), self.assertRaises(configure.ConfigurationError):
                configure.execute(self.root, apply=True)
        self.assertFalse(self.host_path.exists())
        self.assertEqual(self.backups(), [])

    def test_invalid_admin_and_wake_settings_fail_without_guessing(self):
        baseline = copy.deepcopy(self.plugin)
        for mutation in ({"native_admin_ids": ["gh_public"]}, {"native_admin_ids": ["wxid_admin "]},
                         {"native_admin_ids": ["wxid_admin", "wxid_admin"]},
                         {"wake_names": "三叶"}, {"wake_names": [None]},
                         {"wake_regex": ["x" * 513]}):
            self.plugin = {**baseline, **mutation}
            self.write_fixtures()
            with self.subTest(mutation=mutation), self.assertRaises(configure.ConfigurationError):
                configure.make_plan(self.root)

    def test_stale_probe_or_output_snapshots_prevent_all_writes(self):
        for target in (self.probe_path, self.plugin_path, self.host_path):
            with self.subTest(target=target.name):
                plan = configure.make_plan(self.root)
                original = target.read_bytes() if target.exists() else None
                target.write_bytes(b'{"concurrent_edit":true}')
                with self.assertRaises(configure.ConfigurationError):
                    configure.apply_plan(plan)
                self.assertEqual(target.read_bytes(), b'{"concurrent_edit":true}')
                self.assertEqual(self.backups(), [])
                if original is None:
                    target.unlink()
                else:
                    target.write_bytes(original)

    def test_second_write_failure_rolls_back_first_and_removes_new_host_reservation(self):
        before = self.plugin_path.read_bytes()
        actual_replace = configure.os.replace
        count = 0
        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic failure")
            return actual_replace(source, destination)
        with patch.object(configure.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(configure.ConfigurationError, "apply_failed_rolled_back"):
                configure.execute(self.root, apply=True)
        self.assertEqual(self.plugin_path.read_bytes(), before)
        self.assertFalse(self.host_path.exists())
        self.assertFalse((self.root / "runtime/.configure-gateway.lock").exists())
        self.assertEqual(list(self.root.rglob(".gateway-*.tmp")), [])
        self.assertEqual(self.read(self.backups()[0] / "status.json")["status"], "apply_failed_rolled_back")

    def test_probe_edit_during_apply_rolls_back_outputs_and_preserves_source_edit(self):
        before = self.plugin_path.read_bytes()
        actual = configure.atomic_replace
        def replace_and_edit(path, data, expected):
            actual(path, data, expected)
            if path == self.plugin_path:
                self.probe_path.write_bytes(b'{"concurrent_probe_edit":true}')
        with patch.object(configure, "atomic_replace", side_effect=replace_and_edit):
            with self.assertRaisesRegex(configure.ConfigurationError, "apply_failed_rolled_back"):
                configure.execute(self.root, apply=True)
        self.assertEqual(self.plugin_path.read_bytes(), before)
        self.assertFalse(self.host_path.exists())
        self.assertEqual(self.probe_path.read_bytes(), b'{"concurrent_probe_edit":true}')

    def test_late_source_change_restores_both_existing_output_files(self):
        self.write_json(self.host_path, {"token": "f" * 64, "enabled": False})
        before = (self.plugin_path.read_bytes(), self.host_path.read_bytes())
        actual = configure.atomic_replace
        calls = 0
        def replace_and_edit_last(path, data, expected):
            nonlocal calls
            actual(path, data, expected)
            calls += 1
            if calls == 2:
                self.probe_path.write_bytes(b'{"late_probe_edit":true}')
        with patch.object(configure, "atomic_replace", side_effect=replace_and_edit_last):
            with self.assertRaisesRegex(configure.ConfigurationError, "apply_failed_rolled_back"):
                configure.execute(self.root, apply=True)
        self.assertEqual((self.plugin_path.read_bytes(), self.host_path.read_bytes()), before)
        backup = self.backups()[0]
        self.assertEqual((backup / self.plugin_path.name).read_bytes(), before[0])
        self.assertEqual((backup / self.host_path.name).read_bytes(), before[1])
        self.assertEqual(self.probe_path.read_bytes(), b'{"late_probe_edit":true}')

    def test_keyboard_interrupt_during_second_write_rolls_back_first(self):
        before = self.plugin_path.read_bytes()
        actual = configure.atomic_replace
        calls = 0
        def interrupt_second(path, data, expected):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            actual(path, data, expected)
        with patch.object(configure, "atomic_replace", side_effect=interrupt_second):
            with self.assertRaisesRegex(configure.ConfigurationError, "apply_failed_rolled_back"):
                configure.execute(self.root, apply=True)
        self.assertEqual(self.plugin_path.read_bytes(), before)
        self.assertFalse(self.host_path.exists())
        self.assertFalse((self.root / "runtime/.configure-gateway.lock").exists())

    def test_concurrent_user_edit_is_not_overwritten_by_rollback(self):
        actual = configure.atomic_replace
        calls = 0
        def alter_and_fail(path, data, expected):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.plugin_path.write_bytes(b'{"concurrent_plugin_edit":true}')
                raise OSError("synthetic failure")
            actual(path, data, expected)
        with patch.object(configure, "atomic_replace", side_effect=alter_and_fail):
            with self.assertRaisesRegex(configure.ConfigurationError, "apply_failed_rollback_incomplete"):
                configure.execute(self.root, apply=True)
        self.assertEqual(self.plugin_path.read_bytes(), b'{"concurrent_plugin_edit":true}')
        self.assertFalse(self.host_path.exists())

    def test_existing_shared_lock_and_tampered_output_path_fail_closed(self):
        lock = self.root / "runtime/.configure-gateway.lock"
        lock.write_text("another configuration operation", encoding="utf-8")
        with self.assertRaisesRegex(configure.ConfigurationError, "configuration_locked"):
            configure.execute(self.root, apply=True)
        self.assertTrue(lock.exists())
        lock.unlink()
        plan = configure.make_plan(self.root)
        _, before, after = plan["files"][1]
        plan["files"][1] = (self.root / "other.json", before, after)
        with self.assertRaisesRegex(configure.ConfigurationError, "invalid_plan_outputs"):
            configure.apply_plan(plan)
        self.assertFalse((self.root / "other.json").exists())

    def test_cli_prints_only_redacted_summary_and_static_errors(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(configure.main(["--root", str(self.root)]), 0)
        value = json.loads(stream.getvalue())
        self.assertEqual(value["status"], "dry_run")
        self.assertTrue(value["service_stop_required"])
        self.assertFalse(self.host_path.exists())
        for sensitive in (self.probe["expected_self"], self.probe["allowed_room"], self.probe["upstream_commit"]):
            self.assertNotIn(sensitive, stream.getvalue())
        self.plugin["native_bridge_token"] = "SYNTHETIC_SECRET_DO_NOT_PRINT"
        self.write_fixtures()
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(configure.main(["--root", str(self.root)]), 1)
        self.assertNotIn("SYNTHETIC_SECRET", stream.getvalue())
        self.assertEqual(json.loads(stream.getvalue())["code"], "invalid_existing_bridge_token")


if __name__ == "__main__":
    unittest.main()
