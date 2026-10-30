"""Mode switching against temporary runtime fixtures; no Docker, APIs or QQ."""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_check_runtime as runtime_fixture

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_stream", PROJECT / "scripts/configure_stream.py")
assert SPEC and SPEC.loader
stream = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stream)


class ConfigureStreamTests(unittest.TestCase):
    write_json = runtime_fixture.CheckRuntimeTests.write_json
    runtime_bytes = runtime_fixture.CheckRuntimeTests.runtime_bytes
    enable_gateway_fixture = runtime_fixture.CheckRuntimeTests.enable_gateway_fixture
    enable_stream_fixture = runtime_fixture.CheckRuntimeTests.enable_stream_fixture

    def setUp(self):
        runtime_fixture.CheckRuntimeTests.setUp(self)
        self.gate = self.enable_stream_fixture()
        self.gate["mode"] = "neutral_receipt"
        del self.gate["provider_id"]
        self.gate.update(wake_names=["用户自选名字"], wake_regex=["^custom"], send_timeout_seconds=7,
                         retained_setting={"future": "preserve"})
        self.plugin_path = self.root / ("runtime/" + stream.gateway.PLUGIN_FILE)
        self.config_path = self.root / "runtime/astrbot/cmd_config.json"
        self.env_path = self.root / "runtime/astrbot.env"
        self.write_gate()

    def write_gate(self):
        self.plugin_path.write_text(json.dumps(self.gate, ensure_ascii=False), "utf-8")

    def test_dryrun_has_no_mutations_no_backup_and_no_secrets(self):
        before = self.runtime_bytes()
        result = stream.execute(self.root)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["changed_fields"], ["mode", "provider_id"])
        self.assertEqual(before, self.runtime_bytes())
        self.assertFalse((self.root / "runtime/backups").exists())
        for value in (self.password, self.token, self.web_token, "sk-synthetic-stream-key-1234"):
            self.assertNotIn(value, json.dumps(result))

    def test_apply_changes_only_mode_and_provider_and_creates_exact_backup(self):
        before = self.runtime_bytes()
        result = stream.execute(self.root, apply=True)
        self.assertEqual(result["status"], "applied")
        actual = json.loads(self.plugin_path.read_text("utf-8"))
        expected = {**self.gate, "mode": "neutral_stream", "provider_id": stream.PROVIDER_ID}
        self.assertEqual(actual, expected)
        self.assertEqual(self.config_path.read_bytes(), before[str(self.config_path.relative_to(self.root))])
        self.assertEqual(self.env_path.read_bytes(), before[str(self.env_path.relative_to(self.root))])
        self.assertEqual(stream.checker.check_runtime(self.root)["neutral_runtime_profile"], "neutral-stream-enabled")
        backup = next((self.root / "runtime/backups").iterdir())
        self.assertEqual((backup / self.plugin_path.name).read_bytes(), before[str(self.plugin_path.relative_to(self.root))])
        self.assertEqual((backup / "astrbot.env").read_bytes(), before[str(self.env_path.relative_to(self.root))])
        self.assertEqual((backup / "cmd_config.json").read_bytes(), before[str(self.config_path.relative_to(self.root))])

    def test_repeat_apply_preserves_bytes_and_does_not_add_backup(self):
        stream.execute(self.root, apply=True)
        before = self.runtime_bytes()
        self.assertEqual(stream.execute(self.root, apply=True)["status"], "already_configured")
        self.assertEqual(before, self.runtime_bytes())

    def test_disabled_gateway_stays_disabled(self):
        self.gate["enabled"] = False
        self.write_gate()
        result = stream.execute(self.root, apply=True)
        self.assertFalse(result["enabled"])
        self.assertEqual(stream.checker.check_runtime(self.root)["neutral_runtime_profile"], "neutral-stream-disabled")

    def test_receipt_rollback_does_not_need_valid_stream_provider(self):
        stream.execute(self.root, apply=True)
        self.config["provider_sources"][0]["api_base"] = "http://unapproved.invalid"
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.env_path.write_text("ASTRBOT_DASHBOARD_INITIAL_PASSWORD=" + self.password + "\n", "utf-8")
        before = (self.config_path.read_bytes(), self.env_path.read_bytes())
        result = stream.execute(self.root, mode="neutral_receipt", apply=True)
        self.assertEqual(result["changed_fields"], ["mode"])
        self.assertEqual((self.config_path.read_bytes(), self.env_path.read_bytes()), before)
        actual = json.loads(self.plugin_path.read_text("utf-8"))
        self.assertEqual(actual["provider_id"], stream.PROVIDER_ID)
        self.assertEqual(actual["mode"], "neutral_receipt")
        self.assertTrue(stream.checker.check_runtime(self.root)["ok"])

    def test_receipt_noop_does_not_insert_provider_id(self):
        before = self.runtime_bytes()
        result = stream.execute(self.root, mode="neutral_receipt", apply=True)
        self.assertEqual(result["status"], "already_configured")
        self.assertEqual(result["changed_fields"], [])
        self.assertEqual(before, self.runtime_bytes())

    def test_unknown_mode_and_wrong_provider_are_not_overwritten(self):
        for field, value in (("mode", "unreviewed"), ("provider_id", "other-provider")):
            with self.subTest(field=field):
                current = dict(self.gate)
                self.gate[field] = value
                self.write_gate()
                before = self.runtime_bytes()
                with self.assertRaises(stream.ConfigurationError):
                    stream.execute(self.root, apply=True)
                self.assertEqual(before, self.runtime_bytes())
                self.gate = current
        with self.assertRaises(stream.ConfigurationError):
            stream.execute(self.root, mode="unreviewed")

    def test_inert_or_foreign_plugin_cannot_be_enabled_by_switch(self):
        for plugins in ([], ["unreviewed"], [stream.gateway.PLUGIN, "another"]):
            self.config["plugin_set"] = plugins
            self.write_json("runtime/astrbot/cmd_config.json", self.config)
            before = self.runtime_bytes()
            with self.assertRaisesRegex(stream.ConfigurationError, "existing_reviewed_gateway_required"):
                stream.execute(self.root, apply=True)
            self.assertEqual(before, self.runtime_bytes())

    def test_invalid_boundary_and_provider_fail_before_backup(self):
        original = copy.deepcopy(self.config)
        cases = [lambda c: c["provider_settings"].update(enable=True),
                 lambda c: c.update(disable_builtin_commands=False),
                 lambda c: c["platform_settings"]["rate_limit"].update(count=9),
                 lambda c: c["provider"][0].update(enable=False),
                 lambda c: c["provider_sources"][0].update(max_retries=3),
                 lambda c: c["provider_sources"][0].update(key=["raw-synthetic-secret"])]
        for mutate in cases:
            self.config = copy.deepcopy(original)
            mutate(self.config)
            self.write_json("runtime/astrbot/cmd_config.json", self.config)
            before = self.runtime_bytes()
            with self.assertRaises(stream.checker.RuntimeCheckError):
                stream.execute(self.root, apply=True)
            self.assertEqual(before, self.runtime_bytes())
            self.assertFalse((self.root / "runtime/backups").exists())

    def test_concurrent_plugin_config_or_environment_changes_abort_plan(self):
        for path in (self.plugin_path, self.config_path, self.env_path):
            with self.subTest(file=path.name):
                plan = stream.make_plan(self.root)
                original = path.read_bytes()
                changed = original + b"\n"
                path.write_bytes(changed)
                with self.assertRaisesRegex(stream.ConfigurationError, "runtime_changed_since_plan"):
                    stream.apply_plan(plan)
                self.assertEqual(path.read_bytes(), changed)
                path.write_bytes(original)

    def test_replace_failure_preserves_original_files(self):
        before = self.runtime_bytes()
        with patch.object(stream.gateway.os, "replace", side_effect=OSError("private fixture detail")):
            with self.assertRaisesRegex(stream.ConfigurationError, "apply_failed_rolled_back"):
                stream.execute(self.root, apply=True)
        for relative, data in before.items():
            self.assertEqual((self.root / relative).read_bytes(), data)
        self.assertFalse((self.root / "runtime/.configure-gateway.lock").exists())
        self.assertEqual(list((self.root / "runtime").rglob(".gateway-*.tmp")), [])

    def test_cli_reports_only_static_error_not_credentials_or_json_body(self):
        for bad in (False, True):
            if bad:
                self.config_path.write_text('{"key": "raw-synthetic-secret",', "utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                status = stream.main(["--root", str(self.root)])
            self.assertEqual(status, 1 if bad else 0)
            parsed = json.loads(output.getvalue())
            self.assertEqual(parsed["status"], "failed" if bad else "dry_run")
            for value in (self.password, self.token, self.web_token, "sk-synthetic-stream-key-1234", "raw-synthetic-secret"):
                self.assertNotIn(value, output.getvalue())

    def test_neither_persona_nor_model_access_file_is_read(self):
        source = self.root / self.inputs["persona"]["source_file"]
        access = self.root / "config/model-access.local.json"
        source.write_text("do not open fixture persona", "utf-8")
        access.write_text("not valid model access; do not use", "utf-8")
        original_text, original_bytes = Path.read_text, Path.read_bytes
        def text_guard(path, *args, **kwargs):
            if path in (source, access):
                self.fail("Unrelated private source opened")
            return original_text(path, *args, **kwargs)
        def byte_guard(path, *args, **kwargs):
            if path in (source, access):
                self.fail("Unrelated private source opened")
            return original_bytes(path, *args, **kwargs)
        with patch.object(Path, "read_text", text_guard), patch.object(Path, "read_bytes", byte_guard):
            self.assertEqual(stream.execute(self.root)["status"], "dry_run")


if __name__ == "__main__":
    unittest.main()
