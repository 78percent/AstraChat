"""Offline neutral-gate registration and rollback tests; no real runtime or network."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_gateway", PROJECT / "scripts/configure_gateway.py")
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class ConfigureGatewayTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / "work/test-configure-gateway"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "astrbot").mkdir(parents=True)
        (self.root / "config").mkdir()
        self.inputs = {"qq": {"bot_account_id": "123456789", "admin_account_ids": ["234567890"],
                               "private_contact_allowlist": ["234567890"], "test_group_ids": ["345678901"]}}
        self.config = {"provider_settings": {"enable": False, "web_search": False},
                       "platform": [{"id": "qq-sakura", "type": "aiocqhttp", "enable": True}],
                       "admins_id": ["234567890"], "plugin_set": [], "disable_builtin_commands": True,
                       "platform_settings": {"enable_id_white_list": True,
                                             "id_whitelist": ["qq-sakura:GroupMessage:345678901", "qq-sakura:FriendMessage:234567890"]},
                       "unrelated": {"retained": True}}
        self.config_path = self.runtime / "astrbot/cmd_config.json"
        self.plugin_path = self.runtime / gateway.PLUGIN_FILE
        self.write_inputs()

    def write_inputs(self):
        (self.root / "config/connection-inputs.local.json").write_text(json.dumps(self.inputs), "utf-8")
        self.config_path.write_text(json.dumps(self.config), "utf-8")

    def test_dryrun_leaves_existing_and_absent_files_untouched(self):
        before = self.config_path.read_bytes()
        result = gateway.execute(self.root)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.plugin_path.exists())
        self.assertFalse((self.runtime / "backups").exists())
        self.assertNotIn("123456789", json.dumps(result))

    def test_apply_generates_schema_settings_and_preserves_unrelated_config(self):
        before = self.config_path.read_bytes()
        self.assertEqual(gateway.execute(self.root, apply=True)["status"], "applied")
        config = json.loads(self.config_path.read_text("utf-8"))
        plugin = json.loads(self.plugin_path.read_text("utf-8"))
        self.assertEqual(config["plugin_set"], [gateway.PLUGIN])
        self.assertFalse(config["provider_settings"]["enable"])
        self.assertEqual(config["unrelated"], self.config["unrelated"])
        self.assertEqual(plugin["mode"], "neutral_receipt")
        self.assertEqual(plugin["bot_self_id"], self.inputs["qq"]["bot_account_id"])
        self.assertEqual(plugin["admin_private_ids"], self.inputs["qq"]["private_contact_allowlist"])
        self.assertEqual(plugin["wake_names"], ["三叶", "佐仓三叶"])
        self.assertEqual(plugin["wake_regex"], ["^测试唤醒[：:]?"])
        self.assertEqual(plugin["send_timeout_seconds"], 8)
        backup = next((self.runtime / "backups").iterdir())
        self.assertEqual((backup / "cmd_config.json").read_bytes(), before)
        self.assertFalse((backup / self.plugin_path.name).exists())

    def test_repeat_apply_preserves_byte_format_and_adds_no_backup(self):
        gateway.execute(self.root, apply=True)
        plugin = json.loads(self.plugin_path.read_text("utf-8"))
        self.plugin_path.write_text(json.dumps(plugin, separators=(",", ":")), "utf-8")
        before = (self.config_path.read_bytes(), self.plugin_path.read_bytes())
        backups = list((self.runtime / "backups").iterdir())
        self.assertEqual(gateway.execute(self.root, apply=True)["status"], "already_configured")
        self.assertEqual((self.config_path.read_bytes(), self.plugin_path.read_bytes()), before)
        self.assertEqual(list((self.runtime / "backups").iterdir()), backups)

    def test_rate_limit_count_is_disabled_without_changing_other_fields(self):
        self.config["platform_settings"]["rate_limit"] = {
            "count": 17, "time": 45, "strategy": "discard", "future_setting": "retain"}
        self.write_inputs()
        gateway.execute(self.root, apply=True)
        config = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual(config["platform_settings"]["rate_limit"], {
            "count": 0, "time": 45, "strategy": "discard", "future_setting": "retain"})
        self.assertEqual(gateway.execute(self.root, apply=True)["status"], "already_configured")

    def test_modified_gateway_config_requires_manual_configuration(self):
        gateway.execute(self.root, apply=True)
        plugin = json.loads(self.plugin_path.read_text("utf-8"))
        plugin["wake_names"] = ["用户别名"]
        self.plugin_path.write_text(json.dumps(plugin), "utf-8")
        before = self.plugin_path.read_bytes()
        with self.assertRaisesRegex(gateway.ConfigurationError, "manual_configuration_required"):
            gateway.execute(self.root, apply=True)
        self.assertEqual(self.plugin_path.read_bytes(), before)

    def test_mismatched_settings_and_other_plugins_are_refused(self):
        original = copy.deepcopy(self.config)
        mutations = [lambda c: c["provider_settings"].update(enable=True),
                     lambda c: c.update(plugin_set=["other-plugin"]),
                     lambda c: c.update(admins_id=["456789012"]),
                     lambda c: c["platform"][0].update(enable=False),
                     lambda c: c["platform_settings"].update(id_whitelist=[])]
        for mutate in mutations:
            self.config = copy.deepcopy(original)
            mutate(self.config)
            self.write_inputs()
            before = self.config_path.read_bytes()
            with self.assertRaises(gateway.ConfigurationError):
                gateway.execute(self.root, apply=True)
            self.assertEqual(self.config_path.read_bytes(), before)
            self.assertFalse(self.plugin_path.exists())

    def test_second_replace_failure_restores_new_file_absence(self):
        before = self.config_path.read_bytes()
        original_replace = gateway.os.replace
        count = 0
        def fail_once(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated replacement failure")
            return original_replace(source, target)
        with patch.object(gateway.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(gateway.ConfigurationError, "apply_failed_rolled_back"):
                gateway.execute(self.root, apply=True)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.plugin_path.exists())
        self.assertFalse((self.runtime / ".configure-gateway.lock").exists())
        self.assertEqual(list(self.runtime.rglob(".gateway-*.tmp")), [])

    def test_first_replace_failure_removes_reserved_new_file(self):
        before = self.config_path.read_bytes()
        with patch.object(gateway.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaisesRegex(gateway.ConfigurationError, "apply_failed_rolled_back"):
                gateway.execute(self.root, apply=True)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.plugin_path.exists())

    def test_concurrent_new_file_is_not_overwritten(self):
        plan = gateway.make_plan(self.root)
        self.plugin_path.parent.mkdir(parents=True)
        self.plugin_path.write_text('{"user_owned":true}', "utf-8")
        before = self.plugin_path.read_bytes()
        with self.assertRaisesRegex(gateway.ConfigurationError, "runtime_changed_since_plan"):
            gateway.apply_plan(plan)
        self.assertEqual(self.plugin_path.read_bytes(), before)

    def test_read_only_input_change_before_lock_prevents_output_writes(self):
        plan = gateway.make_plan(self.root)
        inputs_path = self.root / "config/connection-inputs.local.json"
        changed = inputs_path.read_bytes() + b"\n"
        original_open = Path.open
        lock = self.runtime / ".configure-gateway.lock"
        def mutate_before_lock(path, *args, **kwargs):
            if path == lock and args and args[0] == "x":
                inputs_path.write_bytes(changed)
            return original_open(path, *args, **kwargs)
        before = self.config_path.read_bytes()
        with patch.object(Path, "open", mutate_before_lock):
            with self.assertRaisesRegex(gateway.ConfigurationError, "read_only_guard_changed_since_plan"):
                gateway.apply_plan(plan)
        self.assertEqual(inputs_path.read_bytes(), changed)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.plugin_path.exists())
        self.assertFalse((self.runtime / "backups").exists())
        self.assertFalse(lock.exists())

    def test_read_only_guard_change_during_first_or_last_replace_rolls_back_outputs(self):
        source = self.root / "external-source.txt"
        source.write_bytes(b"synthetic source before")
        original_replace = gateway.atomic_replace
        for change_number in (1, 2):
            with self.subTest(replacement=change_number):
                source.write_bytes(b"synthetic source before")
                plan = gateway.make_plan(self.root)
                plan["read_only_guards"][source] = source.read_bytes()
                before = self.config_path.read_bytes()
                count = 0
                changed = b"synthetic source concurrently edited"
                def replace_and_edit(path, data, expected):
                    nonlocal count
                    self.assertNotIn(path, plan["read_only_guards"])
                    original_replace(path, data, expected)
                    count += 1
                    if count == change_number:
                        source.write_bytes(changed)
                with patch.object(gateway, "atomic_replace", side_effect=replace_and_edit):
                    with self.assertRaisesRegex(gateway.ConfigurationError, "apply_failed_rolled_back"):
                        gateway.apply_plan(plan)
                self.assertEqual(self.config_path.read_bytes(), before)
                self.assertFalse(self.plugin_path.exists())
                self.assertEqual(source.read_bytes(), changed)
                self.assertFalse((self.runtime / ".configure-gateway.lock").exists())
                self.assertEqual(list(self.runtime.rglob(".gateway-*.tmp")), [])
                for backup in (self.runtime / "backups").iterdir():
                    self.assertFalse((backup / source.name).exists())
                    self.assertEqual(json.loads((backup / "status.json").read_text("utf-8"))["status"],
                                     "apply_failed_rolled_back")

    def test_read_only_guards_cannot_escape_root_overlap_outputs_or_omit_root(self):
        outside = self.root.parent / (self.root.name + "-outside.txt")
        outside.write_bytes(b"synthetic outside fixture")
        self.addCleanup(outside.unlink)
        cases = (({outside: outside.read_bytes()}, "read_only_guard_outside_root"),
                 ({self.config_path: self.config_path.read_bytes()}, "read_only_guard_conflicts_with_output"),
                 ({self.root / "missing.txt": b"expected"}, "read_only_guard_changed_since_plan"),
                 ({self.root / "bad.txt": None}, "invalid_read_only_guards"))
        for guards, code in cases:
            with self.subTest(code=code):
                plan = gateway.make_plan(self.root)
                plan["read_only_guards"] = guards
                with self.assertRaisesRegex(gateway.ConfigurationError, code):
                    gateway.apply_plan(plan)
                self.assertFalse(self.plugin_path.exists())
                self.assertFalse((self.runtime / "backups").exists())
        plan = gateway.make_plan(self.root)
        plan.pop("root")
        with self.assertRaisesRegex(gateway.ConfigurationError, "invalid_read_only_guard_root"):
            gateway.apply_plan(plan)

    def test_unchanged_plan_still_checks_read_only_guards(self):
        gateway.execute(self.root, apply=True)
        plan = gateway.make_plan(self.root)
        self.assertTrue(all(before == after for _, before, after in plan["files"]))
        backups = list((self.runtime / "backups").iterdir())
        inputs_path = self.root / "config/connection-inputs.local.json"
        inputs_path.write_bytes(inputs_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(gateway.ConfigurationError, "read_only_guard_changed_since_plan"):
            gateway.apply_plan(plan)
        self.assertEqual(list((self.runtime / "backups").iterdir()), backups)
        self.assertFalse((self.runtime / ".configure-gateway.lock").exists())

    def test_path_escape_is_rejected(self):
        with self.assertRaisesRegex(gateway.ConfigurationError, "path_outside_root"):
            gateway.checked_path(self.runtime, "../outside.json")


if __name__ == "__main__":
    unittest.main()
