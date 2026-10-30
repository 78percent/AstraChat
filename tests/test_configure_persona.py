"""Persona selection uses synthetic local fixtures, never live files or APIs."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_check_runtime as runtime_fixture
import test_configure_stream as stream_fixture

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_persona", PROJECT / "scripts/configure_persona.py")
assert SPEC and SPEC.loader
persona = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(persona)


class ConfigurePersonaTests(unittest.TestCase):
    write_json = runtime_fixture.CheckRuntimeTests.write_json
    runtime_bytes = runtime_fixture.CheckRuntimeTests.runtime_bytes
    enable_gateway_fixture = runtime_fixture.CheckRuntimeTests.enable_gateway_fixture
    enable_stream_fixture = runtime_fixture.CheckRuntimeTests.enable_stream_fixture

    def setUp(self):
        runtime_fixture.CheckRuntimeTests.setUp(self)
        self.gate = self.enable_stream_fixture()
        self.gate.update(wake_names=["测试角色"], retained_setting={"preserve": True})
        self.plugin_path = self.root / ("runtime/" + persona.gateway.PLUGIN_FILE)
        self.config_path = self.root / "runtime/astrbot/cmd_config.json"
        self.env_path = self.root / "runtime/astrbot.env"
        self.source = self.root / "fixture-persona.txt"
        self.source_bytes = "\ufeff测试角色是热爱气象的朋友。\r\n回答简短，保持友善。\r\n".encode("utf-8")
        self.source.write_bytes(self.source_bytes)
        self.write_gate()

    def write_gate(self):
        self.plugin_path.write_text(json.dumps(self.gate, ensure_ascii=False), "utf-8")

    def make_plan(self):
        return persona.make_plan(self.root, self.source, persona_id="weather_friend", name="测试角色")

    def execute(self, **kwargs):
        return persona.execute(self.root, self.source, persona_id="weather_friend", name="测试角色", **kwargs)

    def test_dryrun_has_no_writes_and_reports_only_metadata(self):
        before = self.runtime_bytes()
        result = self.execute()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["mode"], "roleplay_stream")
        self.assertEqual(result["source_format"], "text")
        self.assertFalse(result["content_safety_verified"])
        self.assertFalse(result["runtime_import_applied"])
        self.assertEqual(result["memory_admin_ids"], self.config["admins_id"])
        self.assertNotEqual(result["memory_admin_ids"], self.gate["admin_private_ids"])
        self.assertIn("memory_admin_ids", result["changed_fields"])
        self.assertGreater(result["budget_report"]["estimated_tokens"]["persona"], 0)
        self.assertEqual(before, self.runtime_bytes())
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertFalse((self.root / "runtime/backups").exists())
        self.assertFalse((self.root / "runtime/roleplay/personas").exists())
        for text in ("热爱气象", self.password, self.token, self.web_token, "sk-synthetic-stream-key-1234"):
            self.assertNotIn(text, json.dumps(result, ensure_ascii=False))

    def test_apply_copies_exact_bytes_preserves_credentials_and_backups(self):
        before = self.runtime_bytes()
        result = self.execute(apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["runtime_import_applied"])
        card = self.root / "runtime/roleplay/personas" / result["persona_filename"]
        self.assertEqual(card.read_bytes(), self.source_bytes)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        expected = {**self.gate, "mode": "roleplay_stream", "persona_id": "weather_friend",
                    "memory_admin_ids": self.config["admins_id"],
                    "persona_name": "测试角色", "persona_filename": result["persona_filename"],
                    "persona_sha256": result["persona_sha256"]}
        self.assertEqual(json.loads(self.plugin_path.read_text("utf-8")), expected)
        for path in (self.config_path, self.env_path):
            self.assertEqual(path.read_bytes(), before[str(path.relative_to(self.root))])
        backup = next((self.root / "runtime/backups").iterdir())
        for path in (self.plugin_path, self.config_path, self.env_path):
            self.assertEqual((backup / path.name).read_bytes(), before[str(path.relative_to(self.root))])
        manifest = json.loads((backup / "status.json").read_text("utf-8"))
        self.assertEqual(manifest["status"], "applied")
        self.assertIn({"name": card.name, "existed": False}, manifest["files"])
        report = persona.checker.check_runtime(self.root)
        self.assertEqual(report["runtime_profile"], "roleplay-stream-enabled")
        self.assertTrue(report["runtime_persona_hash_verified"])
        self.assertFalse(report["persona_source_read"])
        self.assertTrue(report["memory_admin_scope_verified"])

    def test_untrusted_memory_admin_field_is_replaced_from_explicit_sources(self):
        self.gate["memory_admin_ids"] = self.gate["admin_private_ids"]
        self.write_gate()
        result = self.execute(apply=True)
        actual = json.loads(self.plugin_path.read_text("utf-8"))
        self.assertEqual(actual["memory_admin_ids"], self.config["admins_id"])
        self.assertIn("memory_admin_ids", result["changed_fields"])

    def test_inconsistent_admin_source_fails_before_persona_read_or_mutation(self):
        self.config["admins_id"] = self.gate["admin_private_ids"]
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        before = self.runtime_bytes()
        with patch.object(persona, "load_persona", side_effect=AssertionError("must not read persona")):
            with self.assertRaises(persona.checker.RuntimeCheckError) as raised:
                self.execute(apply=True)
        self.assertEqual(raised.exception.code, "WHITELIST_CHANGED")
        self.assertEqual(before, self.runtime_bytes())

    def test_duplicate_explicit_admin_sources_are_rejected_before_persona_read(self):
        self.config["admins_id"] *= 2
        self.inputs["qq"]["admin_account_ids"] = self.config["admins_id"]
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.write_json("config/connection-inputs.local.json", self.inputs)
        self.write_json("runtime/inputs.snapshot.json", self.inputs)
        before = self.runtime_bytes()
        with patch.object(persona, "load_persona", side_effect=AssertionError("must not read persona")):
            with self.assertRaises(persona.checker.RuntimeCheckError) as raised:
                self.execute(apply=True)
        self.assertEqual(raised.exception.code, "MEMORY_ADMIN_SOURCE_INVALID")
        self.assertEqual(before, self.runtime_bytes())

    def test_repeat_apply_is_byte_stable_and_has_no_new_backup(self):
        self.execute(apply=True)
        before = self.runtime_bytes()
        self.assertEqual(self.execute(apply=True)["status"], "already_configured")
        self.assertEqual(before, self.runtime_bytes())

    def test_reimport_preserves_enabled_wechat_and_its_credentials(self):
        self.execute(apply=True)
        self.gate = json.loads(self.plugin_path.read_text("utf-8"))
        peer = "synthetic-private-peer"
        self.gate.update(wechat_enabled=True, wechat_platform_id="wechat-sakura",
                         wechat_allowed_private_ids=[peer], wechat_admin_ids=[peer],
                         native_enabled=True, native_reply_probability=1.0)
        self.config["platform"].append({"id": "wechat-sakura", "type": "weixin_oc",
            "enable": True, "weixin_oc_token": "synthetic-token-never-export",
            "weixin_oc_account_id": "synthetic-account"})
        self.config["platform_settings"]["id_whitelist"].append("wechat-sakura:FriendMessage:" + peer)
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.write_gate()
        original_config = self.config_path.read_bytes()
        self.source.write_bytes(self.source_bytes + "只说直接对话。".encode("utf-8"))
        before = self.runtime_bytes()
        self.assertEqual(self.execute()["status"], "dry_run")
        self.assertEqual(self.runtime_bytes(), before)
        result = self.execute(apply=True)
        self.assertEqual(result["status"], "applied")
        actual = json.loads(self.plugin_path.read_text("utf-8"))
        self.assertEqual(actual, {**self.gate, "persona_filename": result["persona_filename"],
                                  "persona_sha256": result["persona_sha256"]})
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertTrue(persona.checker.check_runtime(self.root)["ok"])

    def test_reimport_does_not_bypass_disabled_wechat_node(self):
        self.execute(apply=True)
        self.gate = json.loads(self.plugin_path.read_text("utf-8"))
        peer = "synthetic-private-peer"
        self.gate.update(wechat_enabled=True, wechat_platform_id="wechat-sakura",
                         wechat_allowed_private_ids=[peer], wechat_admin_ids=[peer])
        self.config["platform"].append({"id": "wechat-sakura", "type": "weixin_oc",
            "enable": False, "weixin_oc_token": "synthetic-token-never-export",
            "weixin_oc_account_id": "synthetic-account"})
        self.config["platform_settings"]["id_whitelist"].append("wechat-sakura:FriendMessage:" + peer)
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.write_gate()
        before = self.runtime_bytes()
        with self.assertRaises(persona.checker.RuntimeCheckError) as raised:
            self.execute(apply=True)
        self.assertEqual(raised.exception.code, "WECHAT_NOT_READY")
        self.assertEqual(self.runtime_bytes(), before)

    def test_disabled_gateway_stays_disabled(self):
        self.gate["enabled"] = False
        self.write_gate()
        result = self.execute(apply=True)
        self.assertFalse(result["enabled"])
        self.assertEqual(persona.checker.check_runtime(self.root)["runtime_profile"], "roleplay-stream-disabled")

    def test_changed_source_selects_new_copy_without_overwriting_previous(self):
        first = self.execute(apply=True)
        self.source.write_bytes("另一个合成人设。".encode("utf-8"))
        second = self.execute(apply=True)
        self.assertNotEqual(first["persona_filename"], second["persona_filename"])
        directory = self.root / "runtime/roleplay/personas"
        self.assertEqual((directory / first["persona_filename"]).read_bytes(), self.source_bytes)
        self.assertEqual((directory / second["persona_filename"]).read_bytes(), self.source.read_bytes())

    def test_source_must_be_supported_file_inside_project(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("synthetic outside source", "utf-8")
        unsupported = self.root / "source.py"
        unsupported.write_text("synthetic unsupported source", "utf-8")
        for source, code in ((outside, "persona_source_outside_project"),
                             (unsupported, "unsupported_persona_source_type"),
                             (self.root / "missing.txt", "persona_source_missing")):
            with self.subTest(code=code):
                before = self.runtime_bytes()
                with self.assertRaisesRegex(persona.ConfigurationError, code):
                    persona.execute(self.root, source, persona_id="weather_friend", name="测试角色", apply=True)
                self.assertEqual(before, self.runtime_bytes())

    def test_invalid_id_and_name_are_rejected_before_writes(self):
        for identity, name in (("../escape", "测试角色"), ("中文", "测试角色"),
                               ("weather_friend", ""), ("weather_friend", "line\nbreak")):
            with self.subTest(identity=identity, name=name):
                before = self.runtime_bytes()
                with self.assertRaises(persona.checker.RuntimeCheckError):
                    persona.execute(self.root, self.source, persona_id=identity, name=name, apply=True)
                self.assertEqual(before, self.runtime_bytes())

    def test_excessive_persona_budget_is_rejected_without_truncation_or_copy(self):
        large = ("气" * 2100).encode("utf-8")
        self.source.write_bytes(large)
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "persona_budget_exceeded"):
            self.execute(apply=True)
        self.assertEqual(before, self.runtime_bytes())
        self.assertEqual(self.source.read_bytes(), large)

    def test_invalid_source_is_not_copied(self):
        self.source.write_bytes(b"\xff\x00")
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "invalid_utf8"):
            self.execute(apply=True)
        self.assertEqual(before, self.runtime_bytes())

    def test_existing_hash_named_copy_is_not_silently_overwritten(self):
        plan = self.make_plan()
        target = plan["files"][0][0]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"synthetic mismatched contents")
        before = self.runtime_bytes()
        with self.assertRaisesRegex(persona.ConfigurationError, "existing_persona_copy_hash_mismatch"):
            self.execute(apply=True)
        self.assertEqual(before, self.runtime_bytes())

    def test_source_change_during_parsing_aborts_preflight(self):
        original = persona.load_persona
        def changed_source(path, **kwargs):
            result = original(path, **kwargs)
            path.write_bytes(b"source changed by synthetic concurrent writer")
            return result
        before = self.runtime_bytes()
        with patch.object(persona, "load_persona", side_effect=changed_source):
            with self.assertRaisesRegex(persona.ConfigurationError, "persona_source_changed_during_preflight"):
                self.execute(apply=True)
        self.assertEqual(before, self.runtime_bytes())

    def test_runtime_change_during_parsing_aborts_preflight(self):
        original = persona.load_persona
        changed = self.env_path.read_bytes() + b"\n"
        def changed_runtime(path, **kwargs):
            result = original(path, **kwargs)
            self.env_path.write_bytes(changed)
            return result
        with patch.object(persona, "load_persona", side_effect=changed_runtime):
            with self.assertRaisesRegex(persona.ConfigurationError, "runtime_changed_during_preflight"):
                self.execute(apply=True)
        self.assertEqual(self.env_path.read_bytes(), changed)
        self.assertFalse((self.root / "runtime/backups").exists())

    def test_admin_source_change_during_parsing_aborts_preflight(self):
        original = persona.load_persona
        for relative in ("runtime/astrbot/cmd_config.json", "config/connection-inputs.local.json",
                         "runtime/inputs.snapshot.json"):
            with self.subTest(relative=relative):
                path = self.root / relative
                before = path.read_bytes()
                changed = before + b"\n"
                def changed_authorization(source, **kwargs):
                    result = original(source, **kwargs)
                    path.write_bytes(changed)
                    return result
                with patch.object(persona, "load_persona", side_effect=changed_authorization):
                    with self.assertRaisesRegex(persona.ConfigurationError, "runtime_changed_during_preflight"):
                        self.execute(apply=True)
                self.assertEqual(path.read_bytes(), changed)
                self.assertFalse((self.root / "runtime/backups").exists())
                path.write_bytes(before)

    def test_admin_declaration_change_after_plan_aborts_apply_without_writes(self):
        for relative in ("config/connection-inputs.local.json", "runtime/inputs.snapshot.json"):
            with self.subTest(relative=relative):
                plan = self.make_plan()
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                before = self.runtime_bytes()
                with self.assertRaisesRegex(persona.ConfigurationError, "authorization_changed_since_plan"):
                    persona.apply_plan(plan)
                self.assertEqual(before, self.runtime_bytes())
                self.assertFalse((self.root / "runtime/backups").exists())
                path.write_bytes(original)

    def test_read_only_source_changes_after_outer_precheck_are_rejected_under_lock(self):
        original_apply = persona.gateway.apply_plan
        for relative in ("config/connection-inputs.local.json", "runtime/inputs.snapshot.json",
                         "fixture-persona.txt"):
            with self.subTest(relative=relative):
                plan = self.make_plan()
                path = self.root / relative
                original = path.read_bytes()
                changed = original + b"\n"
                def edit_before_transaction(selected):
                    path.write_bytes(changed)
                    return original_apply(selected)
                with patch.object(persona.gateway, "apply_plan", side_effect=edit_before_transaction):
                    with self.assertRaisesRegex(persona.ConfigurationError, "read_only_guard_changed_since_plan"):
                        persona.apply_plan(plan)
                self.assertEqual(path.read_bytes(), changed)
                for output, before, _ in plan["files"]:
                    self.assertEqual(persona.gateway.read_optional(output), before)
                self.assertFalse((self.root / "runtime/backups").exists())
                path.write_bytes(original)

    def test_read_only_source_edits_during_replacements_preserve_edits_and_rollback_outputs(self):
        original_replace = persona.gateway.atomic_replace
        for relative in ("config/connection-inputs.local.json", "runtime/inputs.snapshot.json",
                         "fixture-persona.txt"):
            for replace_number in (1, 2):
                with self.subTest(relative=relative, replacement=replace_number):
                    plan = self.make_plan()
                    source = self.root / relative
                    original = source.read_bytes()
                    changed = original + b"\n"
                    count = 0
                    def edit_during_replace(path, data, expected):
                        nonlocal count
                        self.assertNotIn(path, plan["read_only_guards"])
                        original_replace(path, data, expected)
                        count += 1
                        if count == replace_number:
                            source.write_bytes(changed)
                    with patch.object(persona.gateway, "atomic_replace", side_effect=edit_during_replace):
                        with self.assertRaisesRegex(persona.ConfigurationError, "apply_failed_rolled_back"):
                            persona.apply_plan(plan)
                    self.assertEqual(source.read_bytes(), changed)
                    for output, before, _ in plan["files"]:
                        self.assertEqual(persona.gateway.read_optional(output), before)
                    self.assertFalse((self.root / "runtime/.configure-gateway.lock").exists())
                    self.assertEqual(list((self.root / "runtime").rglob(".gateway-*.tmp")), [])
                    for backup in (self.root / "runtime/backups").iterdir():
                        self.assertFalse((backup / source.name).exists())
                        self.assertEqual(json.loads((backup / "status.json").read_text("utf-8"))["status"],
                                         "apply_failed_rolled_back")
                    source.write_bytes(original)

    def test_source_change_after_plan_aborts_apply(self):
        plan = self.make_plan()
        self.source.write_bytes(b"synthetic concurrent source edit")
        before = self.runtime_bytes()
        with self.assertRaisesRegex(persona.ConfigurationError, "persona_source_changed_since_plan"):
            persona.apply_plan(plan)
        self.assertEqual(before, self.runtime_bytes())

    def test_runtime_and_target_races_abort_without_overwriting_changes(self):
        for index in range(4):
            with self.subTest(file_index=index):
                plan = self.make_plan()
                path, original, _ = plan["files"][index]
                path.parent.mkdir(parents=True, exist_ok=True)
                changed = (original or b"synthetic race") + b"\n"
                path.write_bytes(changed)
                with self.assertRaisesRegex(persona.ConfigurationError, "runtime_changed_since_plan"):
                    persona.apply_plan(plan)
                self.assertEqual(path.read_bytes(), changed)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_second_replace_failure_rolls_back_new_copy_and_preserves_runtime(self):
        before = self.runtime_bytes()
        original = persona.gateway.atomic_replace
        def fail_on_gateway(path, data, expected):
            if path == self.plugin_path:
                raise OSError("synthetic private detail")
            return original(path, data, expected)
        with patch.object(persona.gateway, "atomic_replace", side_effect=fail_on_gateway):
            with self.assertRaisesRegex(persona.ConfigurationError, "apply_failed_rolled_back"):
                self.execute(apply=True)
        for relative, data in before.items():
            self.assertEqual((self.root / relative).read_bytes(), data)
        self.assertEqual(list((self.root / "runtime/roleplay/personas").glob("*")), [])
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertFalse((self.root / "runtime/.configure-gateway.lock").exists())
        self.assertEqual(list((self.root / "runtime").rglob(".gateway-*.tmp")), [])

    def test_rollback_works_with_missing_or_corrupt_persona_and_preserves_binding(self):
        stream = stream_fixture.stream
        for mode, broken in (("neutral_stream", "missing"), ("neutral_receipt", "corrupt")):
            with self.subTest(mode=mode):
                selected = self.execute(apply=True)
                path = self.root / "runtime/roleplay/personas" / selected["persona_filename"]
                if broken == "missing":
                    path.unlink()
                else:
                    path.write_bytes(b"synthetic invalid hash")
                gate_before = json.loads(self.plugin_path.read_text("utf-8"))
                result = stream.execute(self.root, mode=mode, apply=True)
                self.assertEqual(result["status"], "applied")
                self.assertEqual(json.loads(self.plugin_path.read_text("utf-8")), {**gate_before, "mode": mode})
                self.assertTrue(stream.checker.check_runtime(self.root)["ok"])
                path.unlink(missing_ok=True)

    def test_rollback_does_not_validate_persona_fields_and_cannot_enable_roleplay(self):
        stream = stream_fixture.stream
        self.gate.update(mode="roleplay_stream", persona_id="../bad", persona_filename="../private.txt",
                         memory_admin_ids={"invalid": True})
        self.write_gate()
        stream.execute(self.root, mode="neutral_receipt", apply=True)
        actual = json.loads(self.plugin_path.read_text("utf-8"))
        self.assertEqual(actual, {**self.gate, "mode": "neutral_receipt"})
        with self.assertRaisesRegex(stream.ConfigurationError, "unsupported_neutral_mode"):
            stream.execute(self.root, mode="roleplay_stream", apply=True)

    def test_markdown_alias_gets_supported_runtime_extension(self):
        source = self.source.with_suffix(".markdown")
        source.write_bytes(self.source_bytes)
        result = persona.execute(self.root, source, persona_id="weather_friend", name="测试角色", apply=True)
        self.assertTrue(result["persona_filename"].endswith(".md"))
        self.assertEqual(result["source_format"], "markdown")

    def test_cli_errors_never_echo_source_or_secrets(self):
        self.source.write_bytes(("气" * 2100).encode("utf-8"))
        output = io.StringIO()
        with redirect_stdout(output):
            code = persona.main(["--root", str(self.root), "--source", str(self.source),
                                 "--persona-id", "weather_friend", "--name", "测试角色"])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error_code"], "persona_budget_exceeded")
        for text in ("气", self.password, self.token, self.web_token, "sk-synthetic-stream-key-1234"):
            self.assertNotIn(text, output.getvalue())


if __name__ == "__main__":
    unittest.main()
