"""Offline checks using synthetic credentials; no Docker or network calls."""
from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_runtime", PROJECT / "scripts/check_runtime.py")
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class CheckRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_base = PROJECT / "work/test-check-runtime"
        temporary_base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-check-", dir=temporary_base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "compose.yaml").write_text((PROJECT / "compose.yaml").read_text("utf-8"), "utf-8")
        for directory in ("runtime/astrbot", "runtime/roleplay/state", "runtime/napcat/qq", "runtime/napcat/config",
                          "runtime/napcat/plugins", "plugins/astrbot_plugin_roleplay_gateway"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for filename in ("Dockerfile.astrbot", "requirements-runtime.txt"):
            (self.root / filename).write_text("test fixture only\n", "utf-8")
        self.inputs = {
            "qq": {"bot_account_id": "123456789", "admin_account_ids": ["234567890"],
                   "private_contact_allowlist": ["345678901"], "test_group_ids": ["456789012"]},
            "persona": {"source_file": "private-source-must-not-be-opened.txt", "sha256": "a" * 64},
        }
        self.write_json("config/connection-inputs.local.json", self.inputs)
        self.write_json("runtime/inputs.snapshot.json", self.inputs)
        self.token = "synthetic-onebot-token-not-a-real-credential"
        self.web_token = "synthetic-webui-token-not-a-real-credential"
        self.password = "synthetic-initial-password-not-a-real-credential"
        self.config = {
            "admins_id": ["234567890"],
            "provider_settings": {"enable": False},
            "plugin_set": [],
            "disable_builtin_commands": True,
            "platform_settings": {"enable_id_white_list": True,
                "id_whitelist": ["qq-sakura:GroupMessage:456789012", "qq-sakura:FriendMessage:345678901"],
                "wl_ignore_admin_on_group": False, "wl_ignore_admin_on_friend": False,
                "ignore_bot_self_message": True, "ignore_at_all": True},
            "dashboard": {"enable": True, "host": "0.0.0.0", "port": 6185, "username": "astrbot"},
            "platform": [{"id": "qq-sakura", "type": "aiocqhttp", "enable": True,
                "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199, "ws_reverse_token": self.token}],
        }
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.write_json("runtime/access.local.json", {
            "astrbot_url": "http://127.0.0.1:6185", "napcat_url": "http://127.0.0.1:6099",
            "astrbot_username": "astrbot", "astrbot_initial_password": self.password,
            "napcat_webui_token": self.web_token,
        })
        self.write_json("runtime/napcat/config/webui.json", {
            "host": "0.0.0.0", "port": 6099, "prefix": "", "token": self.web_token,
        })
        self.generic = {"network": {key: [] for key in checker.NETWORK_FIELDS}}
        self.write_json("runtime/napcat/config/onebot11.json", self.generic)
        self.account = copy.deepcopy(self.generic)
        self.account["network"]["websocketClients"] = [{"enable": True, "url": "ws://astrbot:6199/ws",
            "messagePostFormat": "array", "reportSelfMessage": False, "debug": False, "token": self.token}]
        self.write_json("runtime/napcat/config/onebot11_123456789.json", self.account)
        (self.root / "runtime/napcat.env").write_text("ACCOUNT=123456789\n", "utf-8")
        (self.root / "runtime/astrbot.env").write_text("ASTRBOT_DASHBOARD_INITIAL_PASSWORD=" + self.password + "\n", "utf-8")

    def write_json(self, relative: str, value: object) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), "utf-8")

    def runtime_bytes(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / "runtime").rglob("*") if p.is_file()}

    def assert_code(self, code: str) -> None:
        with self.assertRaises(checker.RuntimeCheckError) as raised:
            checker.check_runtime(self.root)
        self.assertEqual(raised.exception.code, code)
        for secret in (self.token, self.web_token, self.password):
            self.assertNotIn(secret, str(raised.exception))

    def test_existing_runtime_is_preserved_without_persona_files(self) -> None:
        before = self.runtime_bytes()
        report = checker.check_runtime(self.root)
        self.assertTrue(report["ok"])
        self.assertFalse(report["persona_source_read"])
        self.assertFalse(report["persona_hash_accepted"])
        self.assertFalse(report["live_qq_login_verified"])
        self.assertTrue(before == self.runtime_bytes())

    def test_persona_declaration_changes_only_warn_and_are_not_adopted(self) -> None:
        before = self.runtime_bytes()
        self.inputs["persona"] = {"source_file": "../../never-open-this.txt", "sha256": "b" * 64}
        self.write_json("config/connection-inputs.local.json", self.inputs)
        report = checker.check_runtime(self.root)
        self.assertTrue(report["ok"])
        self.assertTrue(report["persona_declaration_changed"])
        self.assertIn("PERSONA_DECLARATION_CHANGED", [w["code"] for w in report["warnings"]])
        self.assertTrue(before == self.runtime_bytes())

    def test_persona_body_is_not_opened_or_hashed(self) -> None:
        source = self.root / self.inputs["persona"]["source_file"]
        source.write_text("synthetic edited source; no valid hash required", "utf-8")
        original_read, original_bytes = Path.read_text, Path.read_bytes

        def text_guard(path: Path, *args, **kwargs):
            if path == source:
                self.fail("Neutral check opened a persona source")
            return original_read(path, *args, **kwargs)

        def bytes_guard(path: Path, *args, **kwargs):
            if path == source:
                self.fail("Neutral check read persona bytes")
            return original_bytes(path, *args, **kwargs)

        with patch.object(Path, "read_text", text_guard), patch.object(Path, "read_bytes", bytes_guard):
            report = checker.check_runtime(self.root)
        self.assertTrue(report["ok"])
        self.assertIn("PERSONA_SOURCE_NOT_VERIFIED", [w["code"] for w in report["warnings"]])

    def test_changed_account_or_whitelist_fails_without_modification(self) -> None:
        before = self.runtime_bytes()
        self.inputs["qq"]["test_group_ids"] = ["567890123"]
        self.write_json("config/connection-inputs.local.json", self.inputs)
        self.assert_code("DEPLOYMENT_ACCOUNT_CHANGED")
        self.assertTrue(before == self.runtime_bytes())

    def test_wrong_account_env_fails(self) -> None:
        (self.root / "runtime/napcat.env").write_text("ACCOUNT=567890123\n", "utf-8")
        self.assert_code("DEPLOYMENT_ACCOUNT_CHANGED")

    def test_missing_required_files_are_not_regenerated(self) -> None:
        for relative in ("runtime/access.local.json", "runtime/astrbot.env", "runtime/napcat.env",
                         "runtime/napcat/config/onebot11.json", "runtime/napcat/config/onebot11_123456789.json",
                         "runtime/napcat/config/webui.json", "runtime/astrbot/cmd_config.json"):
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.unlink()
                self.assert_code("REQUIRED_PATH_MISSING")
                self.assertFalse(path.exists())
                path.write_bytes(original)

    def test_missing_runtime_is_not_created(self) -> None:
        empty = self.root / "empty-project"
        empty.mkdir()
        with self.assertRaises(checker.RuntimeCheckError) as raised:
            checker.check_runtime(empty)
        self.assertEqual(raised.exception.code, "REQUIRED_PATH_MISSING")
        self.assertFalse((empty / "runtime").exists())

    def test_onebot_token_mismatch_is_redacted(self) -> None:
        self.account["network"]["websocketClients"][0]["token"] = "different-synthetic-secret"
        self.write_json("runtime/napcat/config/onebot11_123456789.json", self.account)
        self.assert_code("SECRET_MISMATCH")

    def test_webui_secret_missing_is_rejected(self) -> None:
        self.write_json("runtime/napcat/config/webui.json", {"host": "0.0.0.0", "port": 6099, "prefix": "", "token": ""})
        self.assert_code("SECRET_MISMATCH")

    def test_unapproved_compose_change_requires_review(self) -> None:
        path = self.root / "compose.yaml"
        path.write_text(path.read_text("utf-8").replace("127.0.0.1:6099", "0.0.0.0:6099"), "utf-8")
        self.assert_code("COMPOSE_NEEDS_REVIEW")

    def test_original_compose_profile_is_still_supported(self) -> None:
        path = self.root / "compose.yaml"
        text = path.read_text("utf-8")
        text = text.replace("    image: astrbot-roleplay-astrbot:4.29.1-gate1\n    build:\n      context: .\n      dockerfile: Dockerfile.astrbot\n",
                            "    image: soulter/astrbot:v4.29.1@sha256:a140a326163ac5f82f62153f033cddeac74e38e4ee1809c6e333e950ff19edc4\n")
        text = text.replace("      - ./plugins/astrbot_plugin_roleplay_gateway:/AstrBot/data/plugins/astrbot_plugin_roleplay_gateway:ro\n", "")
        # The original upstream manifest predates the native WeChat loopback port.
        text = text.replace('      - "127.0.0.1:6198:6198"\n', "")
        path.write_text(text, "utf-8")
        self.assertEqual(checker.check_runtime(self.root)["compose_profile"], "upstream-pinned")

    def test_generic_account_guard_and_foreign_endpoints_fail(self) -> None:
        self.generic["network"]["websocketClients"] = copy.deepcopy(self.account["network"]["websocketClients"])
        self.write_json("runtime/napcat/config/onebot11.json", self.generic)
        self.assert_code("GENERIC_ACCOUNT_GUARD_DISABLED")
        self.generic["network"]["websocketClients"] = []
        self.write_json("runtime/napcat/config/onebot11.json", self.generic)
        self.account["network"]["websocketClients"][0]["url"] = "ws://unapproved.invalid/ws"
        self.write_json("runtime/napcat/config/onebot11_123456789.json", self.account)
        self.assert_code("ONEBOT_BOUNDARY_CHANGED")

    def test_runtime_whitelist_expansion_fails(self) -> None:
        self.config["platform_settings"]["id_whitelist"].append("qq-sakura:GroupMessage:567890123")
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.assert_code("WHITELIST_CHANGED")

    def enable_gateway_fixture(self) -> dict:
        self.config["plugin_set"] = [checker.GATEWAY_PLUGIN]
        self.config["platform_settings"]["rate_limit"] = {"count": 0}
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        gate = {"enabled": True, "mode": "neutral_receipt", "platform_id": checker.PLATFORM_ID,
                "bot_self_id": "123456789", "admin_private_ids": ["345678901"],
                "allowed_group_ids": ["456789012"]}
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        return gate

    def enable_stream_fixture(self) -> dict:
        gate = self.enable_gateway_fixture()
        gate.update(mode="neutral_stream", provider_id=checker.STREAM_PROVIDER_ID,
                    wake_names=["fixture name"], wake_regex=["^fixture"], send_timeout_seconds=8)
        self.config["provider_sources"] = [{
            "id": checker.STREAM_SOURCE_ID, "provider": "deepseek", "type": "openai_chat_completion",
            "provider_type": "chat_completion", "enable": True,
            "key": ["$ROLEPLAY_DEEPSEEK_API_KEY"], "api_base": "https://api.deepseek.com/v1",
            "timeout": 30, "proxy": "", "custom_headers": {}}]
        self.config["provider"] = [{
            "id": checker.STREAM_PROVIDER_ID, "provider_source_id": checker.STREAM_SOURCE_ID,
            "enable": True, "model": checker.STREAM_MODEL,
            "custom_extra_body": {"thinking": {"type": "disabled"}}}]
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        path = self.root / "runtime/astrbot.env"
        path.write_text(path.read_text("utf-8") + "ROLEPLAY_DEEPSEEK_API_KEY=sk-synthetic-stream-key-1234\n", "utf-8")
        return gate

    def test_stream_profile_validates_and_leaves_files_untouched(self) -> None:
        gate = self.enable_stream_fixture()
        before = self.runtime_bytes()
        report = checker.check_runtime(self.root)
        self.assertEqual(report["neutral_runtime_profile"], "neutral-stream-enabled")
        self.assertEqual(before, self.runtime_bytes())
        self.assertFalse(report["persona_source_read"])
        gate["enabled"] = False
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.assertEqual(checker.check_runtime(self.root)["neutral_runtime_profile"], "neutral-stream-disabled")

    def enable_roleplay_fixture(self) -> tuple[dict, Path]:
        gate = self.enable_stream_fixture()
        path = self.root / "runtime/roleplay/personas/synthetic.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes("这是合成测试角色，不读取用户资料。".encode("utf-8"))
        gate.update(mode="roleplay_stream", persona_id="synthetic", persona_name="测试角色",
                    memory_admin_ids=list(self.config["admins_id"]),
                    persona_filename=path.name, persona_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        return gate, path

    def test_roleplay_checks_only_explicit_runtime_copy_without_mutation(self) -> None:
        gate, card = self.enable_roleplay_fixture()
        original = self.root / self.inputs["persona"]["source_file"]
        original.write_bytes(b"synthetic original must stay unopened")
        unrelated = card.with_name("unselected.txt")
        unrelated.write_bytes(b"synthetic unselected must stay unopened")
        before = self.runtime_bytes()
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path in (original, unrelated):
                self.fail("Checker opened unselected persona data")
            return real_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            report = checker.check_runtime(self.root)
        self.assertTrue(report["ok"])
        self.assertEqual(report["runtime_profile"], "roleplay-stream-enabled")
        self.assertEqual(report["mode"], "existing-roleplay-integration")
        self.assertTrue(report["runtime_persona_read"])
        self.assertTrue(report["runtime_persona_hash_verified"])
        self.assertTrue(report["memory_admin_scope_verified"])
        self.assertFalse(report["persona_source_read"])
        self.assertFalse(report["persona_hash_accepted"])
        self.assertEqual(before, self.runtime_bytes())
        gate["enabled"] = False
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.assertEqual(checker.check_runtime(self.root)["runtime_profile"], "roleplay-stream-disabled")

    def test_roleplay_requires_explicit_admins_not_private_contacts_before_persona_read(self) -> None:
        gate, card = self.enable_roleplay_fixture()
        self.assertNotEqual(self.config["admins_id"], gate["admin_private_ids"])
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == card:
                self.fail("Invalid memory administrator scope must fail before persona read")
            return real_open(path, *args, **kwargs)
        values = (None, [], {}, "234567890", [234567890], [True],
                  ["234567890", "234567890"], ["123456789"], ["345678901"],
                  ["234567890", "345678901"])
        with patch.object(Path, "open", guarded_open):
            for enabled in (True, False):
                for value in values:
                    with self.subTest(enabled=enabled, value=value):
                        changed = {**gate, "enabled": enabled, "memory_admin_ids": value}
                        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", changed)
                        self.assert_code("MEMORY_ADMIN_SCOPE_CHANGED")
                missing = {**gate, "enabled": enabled}
                missing.pop("memory_admin_ids")
                self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", missing)
                self.assert_code("MEMORY_ADMIN_SCOPE_CHANGED")

    def test_roleplay_explicit_admin_sources_must_be_consistent_and_unique(self) -> None:
        gate, _ = self.enable_roleplay_fixture()
        self.config["admins_id"] = ["345678901"]
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.assert_code("WHITELIST_CHANGED")
        duplicated = ["234567890", "234567890"]
        self.config["admins_id"] = duplicated
        self.inputs["qq"]["admin_account_ids"] = duplicated
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.write_json("config/connection-inputs.local.json", self.inputs)
        self.write_json("runtime/inputs.snapshot.json", self.inputs)
        gate["memory_admin_ids"] = duplicated
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.assert_code("MEMORY_ADMIN_SOURCE_INVALID")

    def test_roleplay_rejects_invalid_fields_before_opening_card(self) -> None:
        gate, card = self.enable_roleplay_fixture()
        cases = [("persona_id", value, "PERSONA_ID_INVALID")
                 for value in (None, "", "../bad", "中文", "_starts_bad", "a" * 65)]
        cases += [("persona_name", value, "PERSONA_NAME_INVALID")
                  for value in (None, "", " ", "a" * 101, "line\nbreak", "line\rbreak", "line\u2028break")]
        cases += [("persona_filename", value, "PERSONA_FILENAME_INVALID")
                  for value in (None, "", "../synthetic.txt", "sub/synthetic.txt", "sub\\synthetic.txt",
                                "C:\\synthetic.txt", "synthetic.txt:secret", "synthetic.exe", "synthetic.txt ")]
        cases += [("persona_sha256", value, "PERSONA_HASH_INVALID")
                  for value in (None, "", "A" * 64, "g" * 64, "a" * 63)]
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == card:
                self.fail("Invalid persona metadata must fail before body read")
            return real_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            for field, value, code in cases:
                with self.subTest(field=field, value=value):
                    self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", {**gate, field: value})
                    self.assert_code(code)

    def test_roleplay_rejects_missing_changed_and_oversized_copy(self) -> None:
        _, card = self.enable_roleplay_fixture()
        card.write_bytes(b"synthetic changed content")
        self.assert_code("PERSONA_HASH_MISMATCH")
        card.write_bytes(b"a" * (checker.MAX_PERSONA_BYTES + 1))
        self.assert_code("PERSONA_TOO_LARGE")
        card.write_bytes(b"")
        self.assert_code("PERSONA_TOO_LARGE")
        card.unlink()
        self.assert_code("REQUIRED_PATH_MISSING")

    def test_roleplay_rejects_resolved_file_outside_persona_directory(self) -> None:
        gate, _ = self.enable_roleplay_fixture()
        outside = self.root / "runtime/fixture-outside-personas.txt"
        outside.write_bytes(b"synthetic path fixture")
        actual_local = checker.local_path
        selected_relative = "runtime/roleplay/personas/" + gate["persona_filename"]
        def resolved_outside(root, relative, **kwargs):
            if relative == selected_relative:
                return outside
            return actual_local(root, relative, **kwargs)
        with patch.object(checker, "local_path", side_effect=resolved_outside):
            self.assert_code("PERSONA_PATH_OUTSIDE_DIRECTORY")

    def test_neutral_modes_ignore_retained_persona_fields_and_never_read_copy(self) -> None:
        gate, card = self.enable_roleplay_fixture()
        gate.update(persona_id="../invalid", persona_sha256="not-a-hash", memory_admin_ids={"invalid": True})
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == card:
                self.fail("Neutral modes must not inspect retained persona")
            return real_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            for mode in ("neutral_receipt", "neutral_stream"):
                gate["mode"] = mode
                self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
                report = checker.check_runtime(self.root)
                self.assertTrue(report["ok"])
                self.assertFalse(report["runtime_persona_read"])
                self.assertFalse(report["runtime_persona_hash_verified"])
                self.assertFalse(report["memory_admin_scope_verified"])

    def test_disabled_roleplay_still_checks_hash_and_official_provider(self) -> None:
        gate, card = self.enable_roleplay_fixture()
        gate["enabled"] = False
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.config["provider_sources"][0]["api_base"] = "https://relay.invalid/v1"
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        self.assert_code("STREAM_ENDPOINT_INVALID")
        self.config["provider_sources"][0]["api_base"] = "https://api.deepseek.com/v1"
        self.write_json("runtime/astrbot/cmd_config.json", self.config)
        card.write_bytes(b"synthetic edited copy")
        self.assert_code("PERSONA_HASH_MISMATCH")

    def test_stream_requires_fixed_provider_and_registered_source(self) -> None:
        gate = self.enable_stream_fixture()
        path = f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json"
        changed = dict(gate)
        changed.pop("provider_id")
        self.write_json(path, changed)
        self.assert_code("STREAM_PROVIDER_ID_CHANGED")
        self.write_json(path, gate)
        original = copy.deepcopy(self.config)
        cases = [lambda c: c.update(provider=[]),
                 lambda c: c["provider"].append(copy.deepcopy(c["provider"][0])),
                 lambda c: c.update(provider_sources=[]),
                 lambda c: c["provider_sources"].append(copy.deepcopy(c["provider_sources"][0]))]
        for mutate in cases:
            changed = copy.deepcopy(original)
            mutate(changed)
            self.write_json("runtime/astrbot/cmd_config.json", changed)
            self.assert_code("STREAM_PROVIDER_INVALID")

    def test_stream_rejects_model_payload_tool_or_retry_overrides(self) -> None:
        self.enable_stream_fixture()
        original = copy.deepcopy(self.config)
        cases = [dict(model="other-model"), dict(enable=False), dict(provider_source_id="other"),
                 dict(custom_extra_body={"thinking": {"type": "enabled"}}),
                 dict(custom_extra_body={"messages": []}), dict(request_max_retries=3),
                 dict(max_retries=2), dict(tools=[]), dict(custom_headers={})]
        for changes in cases:
            with self.subTest(fields=list(changes)):
                changed = copy.deepcopy(original)
                changed["provider"][0].update(changes)
                self.write_json("runtime/astrbot/cmd_config.json", changed)
                self.assert_code("STREAM_PROVIDER_INVALID")

    def test_stream_rejects_source_auth_proxy_headers_or_retry_changes(self) -> None:
        self.enable_stream_fixture()
        original = copy.deepcopy(self.config)
        cases = [dict(provider="custom"), dict(type="other"), dict(enable=False),
                 dict(key=["plaintext-synthetic-key"]), dict(key=["$OTHER_KEY"]),
                 dict(key=["$ROLEPLAY_DEEPSEEK_API_KEY", "$OTHER_KEY"]),
                 dict(proxy="http://proxy.invalid"), dict(custom_headers={"Authorization": "synthetic"}),
                 dict(timeout=True), dict(timeout=60), dict(max_retries=2), dict(api_version="x"),
                 dict(extra_query={})]
        for changes in cases:
            with self.subTest(fields=list(changes)):
                changed = copy.deepcopy(original)
                changed["provider_sources"][0].update(changes)
                self.write_json("runtime/astrbot/cmd_config.json", changed)
                self.assert_code("STREAM_SOURCE_INVALID")

    def test_stream_only_allows_reviewed_official_https_bases(self) -> None:
        self.enable_stream_fixture()
        source = self.config["provider_sources"][0]
        for base in ("https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1",
                     "https://api.deepseek.com:443/v1/"):
            source["api_base"] = base
            self.write_json("runtime/astrbot/cmd_config.json", self.config)
            self.assertTrue(checker.check_runtime(self.root)["ok"])
        for base in ("http://api.deepseek.com/v1", "https://relay.invalid/v1", "https://api.deepseek.com.evil/v1",
                     "https://user:synthetic@api.deepseek.com/v1", "https://api.deepseek.com:444/v1",
                     "https://api.deepseek.com/v1?key=synthetic", "https://api.deepseek.com/v1#fragment",
                     "https://api.deepseek.com/chat/completions"):
            source["api_base"] = base
            self.write_json("runtime/astrbot/cmd_config.json", self.config)
            self.assert_code("STREAM_ENDPOINT_INVALID")

    def test_stream_key_required_even_when_gate_disabled(self) -> None:
        gate = self.enable_stream_fixture()
        (self.root / "runtime/astrbot.env").write_text(
            "ASTRBOT_DASHBOARD_INITIAL_PASSWORD=" + self.password + "\n", "utf-8")
        self.assert_code("STREAM_KEY_REQUIRED")
        gate["enabled"] = False
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.assert_code("STREAM_KEY_REQUIRED")

    def test_receipt_rollback_can_keep_fixed_provider_id_without_model_key(self) -> None:
        gate = self.enable_gateway_fixture()
        gate["provider_id"] = checker.STREAM_PROVIDER_ID
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.assertEqual(checker.check_runtime(self.root)["neutral_runtime_profile"], "neutral-receipt-enabled")
        gate["provider_id"] = "unreviewed-provider"
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.assert_code("STREAM_PROVIDER_ID_CHANGED")

    def test_native_generation_commands_and_unknown_plugins_are_rejected(self) -> None:
        cases = (("provider_settings", {"enable": True}, "NATIVE_PROVIDER_ENABLED"),
                 ("provider_settings", {}, "NATIVE_PROVIDER_ENABLED"),
                 ("disable_builtin_commands", False, "BUILTIN_COMMANDS_ENABLED"),
                 ("plugin_set", ["unreviewed_plugin"], "PLUGIN_BOUNDARY_CHANGED"),
                 ("plugin_set", "*", "PLUGIN_BOUNDARY_CHANGED"))
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.config)
                changed[field] = value
                self.write_json("runtime/astrbot/cmd_config.json", changed)
                self.assert_code(code)

    def test_prepared_and_active_or_disabled_neutral_gate_pass(self) -> None:
        self.assertEqual(checker.check_runtime(self.root)["neutral_runtime_profile"], "prepared-inert")
        gate = self.enable_gateway_fixture()
        self.assertEqual(checker.check_runtime(self.root)["neutral_runtime_profile"], "neutral-receipt-enabled")
        gate["enabled"] = False
        self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", gate)
        self.assertEqual(checker.check_runtime(self.root)["neutral_runtime_profile"], "neutral-receipt-disabled")

    def test_neutral_gateway_requires_native_rate_limit_disabled(self) -> None:
        self.enable_gateway_fixture()
        for value in ({"count": 3}, {"count": False}, {}, None):
            with self.subTest(value=value):
                self.config["platform_settings"]["rate_limit"] = value
                self.write_json("runtime/astrbot/cmd_config.json", self.config)
                self.assert_code("NATIVE_RATE_LIMIT_CHANGED")

    def test_neutral_gateway_rejects_mode_or_scope_drift(self) -> None:
        gate = self.enable_gateway_fixture()
        cases = (("mode", "llm", "GATEWAY_MODE_CHANGED"),
                 ("enabled", "false", "GATEWAY_MODE_CHANGED"),
                 ("platform_id", "other", "GATEWAY_SCOPE_CHANGED"),
                 ("bot_self_id", "567890123", "GATEWAY_SCOPE_CHANGED"),
                 ("admin_private_ids", ["234567890"], "GATEWAY_SCOPE_CHANGED"),
                 ("allowed_group_ids", [], "GATEWAY_SCOPE_CHANGED"))
        for field, value, code in cases:
            with self.subTest(field=field):
                changed = copy.deepcopy(gate)
                changed[field] = value
                self.write_json(f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json", changed)
                self.assert_code(code)

    def test_missing_active_gateway_config_fails_without_regeneration(self) -> None:
        self.enable_gateway_fixture()
        path = self.root / f"runtime/astrbot/config/{checker.GATEWAY_PLUGIN}_config.json"
        path.unlink()
        self.assert_code("REQUIRED_PATH_MISSING")
        self.assertFalse(path.exists())

    def test_extra_environment_keys_fail(self) -> None:
        (self.root / "runtime/napcat.env").write_text("ACCOUNT=123456789\nOTHER=synthetic-value\n", "utf-8")
        self.assert_code("ENV_INVALID")

    def test_optional_deepseek_key_is_accepted_without_changes_or_disclosure(self) -> None:
        path = self.root / "runtime/astrbot.env"
        original = path.read_text("utf-8")
        for key in ("A" * 16, "z" * 256, "sk-Synthetic_1234"):
            with self.subTest(length=len(key)):
                path.write_text(original + f"ROLEPLAY_DEEPSEEK_API_KEY={key}\n", "utf-8")
                before = self.runtime_bytes()
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(checker.main(["--root", str(self.root), "--json"]), 0)
                self.assertTrue(json.loads(output.getvalue())["ok"])
                self.assertNotIn(key, output.getvalue())
                self.assertEqual(before, self.runtime_bytes())

    def test_optional_deepseek_key_invalid_shape_is_rejected_and_redacted(self) -> None:
        path = self.root / "runtime/astrbot.env"
        original = path.read_text("utf-8")
        invalid = ("a" * 15, "a" * 257, "a" * 16 + " ", '"' + "a" * 16 + '"',
                   "a" * 16 + "$", "a" * 16 + "\nOTHER=synthetic-value", "")
        for key in invalid:
            with self.subTest(length=len(key)):
                path.write_text(original + f"ROLEPLAY_DEEPSEEK_API_KEY={key}\n", "utf-8")
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(checker.main(["--root", str(self.root), "--json"]), 1)
                expected_code = "ENV_INVALID" if key else "SECRET_MISSING"
                self.assertEqual(json.loads(output.getvalue())["code"], expected_code)
                if key:
                    self.assertNotIn(key, output.getvalue())

    def test_duplicate_allowed_environment_keys_are_rejected(self) -> None:
        path = self.root / "runtime/astrbot.env"
        original = path.read_text("utf-8")
        optional = "ROLEPLAY_DEEPSEEK_API_KEY=sk-Synthetic_1234\n"
        for duplicate in (optional, original):
            with self.subTest(key=duplicate.partition("=")[0]):
                path.write_text(original + optional + duplicate, "utf-8")
                self.assert_code("ENV_INVALID")

    def test_unknown_astrbot_environment_keys_remain_rejected(self) -> None:
        path = self.root / "runtime/astrbot.env"
        original = path.read_text("utf-8")
        for name in ("OTHER", "ROLEPLAY_GEMINI_API_KEY", "ROLEPLAY_DEEPSEEK_API_KEY_EXTRA"):
            with self.subTest(name=name):
                path.write_text(original + "ROLEPLAY_DEEPSEEK_API_KEY=sk-Synthetic_1234\n"
                                + f"{name}=synthetic-value\n", "utf-8")
                self.assert_code("ENV_INVALID")

    def test_deepseek_key_is_not_allowed_in_napcat_environment(self) -> None:
        (self.root / "runtime/napcat.env").write_text(
            "ACCOUNT=123456789\nROLEPLAY_DEEPSEEK_API_KEY=sk-Synthetic_1234\n", "utf-8")
        self.assert_code("ENV_INVALID")

    def test_optional_key_does_not_replace_required_dashboard_password(self) -> None:
        (self.root / "runtime/astrbot.env").write_text(
            "ROLEPLAY_DEEPSEEK_API_KEY=sk-Synthetic_1234\n", "utf-8")
        self.assert_code("ENV_INVALID")

    def test_cli_json_never_exposes_credentials_even_on_invalid_json(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(checker.main(["--root", str(self.root), "--json"]), 0)
        valid_output = output.getvalue()
        (self.root / "runtime/access.local.json").write_text('{"secret": ' + self.password, "utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(checker.main(["--root", str(self.root), "--json"]), 1)
        for secret in (self.token, self.web_token, self.password):
            self.assertNotIn(secret, valid_output + output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "CONFIG_INVALID")


if __name__ == "__main__":
    unittest.main()
