"""Credential-preserving offline configuration tests, using synthetic secrets only."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("configure_deepseek", PROJECT / "scripts/configure_deepseek.py")
assert SPEC and SPEC.loader
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class ConfigureDeepseekTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / "work/test-configure"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="config-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "astrbot").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.root / "reports").mkdir()
        self.key = "synthetic_test_secret_never_print_123"
        self.access = {"deepseek_official": {"base_url": "https://api.deepseek.com/v1",
                                             "api_key": self.key, "model_id": "deepseek-chat"}}
        self.config = {"provider_sources": [{"id": "unrelated-source", "key": ["$OTHER_KEY"]}],
                       "provider": [{"id": "unrelated-provider", "model": "other"}],
                       "provider_settings": {"enable": False, "web_search": False,
                                             "computer_use_runtime": "none", "default_personality": "existing"},
                       "untouched": {"label": "preserve"}}
        self.evidence = {"scope": "official_api_only_no_qq_latency_claim",
                         "runtime": "astrbot_container_linux", "models_http_status": 200,
                         "generation_http_status": 200, "requested_model": "deepseek-flash",
                         "actual_model": "deepseek-flash", "authenticated": True,
                         "success": True, "stream_done": True, "nonempty_content_chunks": 2}
        self.config_path = self.runtime / "astrbot/cmd_config.json"
        self.env_path = self.runtime / "astrbot.env"
        self.config_path.write_text(json.dumps(self.config), "utf-8")
        self.env_path.write_bytes(b"# retain this comment\r\nASTRBOT_DASHBOARD_INITIAL_PASSWORD=synthetic_dashboard\r\nOTHER=value\r\n")
        self.save_inputs()

    def save_inputs(self):
        (self.root / "config/model-access.local.json").write_text(json.dumps(self.access), "utf-8")
        (self.root / "reports/deepseek-connectivity.json").write_text(json.dumps(self.evidence), "utf-8")

    def snapshot(self):
        return self.config_path.read_bytes(), self.env_path.read_bytes()

    def test_dryrun_has_no_mutations_and_never_returns_secret(self):
        before = self.snapshot()
        result = configure.execute(self.root)
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(before == self.snapshot())
        self.assertFalse((self.runtime / "backups").exists())
        self.assertNotIn(self.key, json.dumps(result))

    def test_apply_preserves_unrelated_settings_and_dashboard_password(self):
        before = self.snapshot()
        original_access = (self.root / "config/model-access.local.json").read_bytes()
        result = configure.execute(self.root, apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertNotIn(self.key, json.dumps(result))
        current = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual(current["provider"][0], self.config["provider"][0])
        self.assertEqual(current["provider_sources"][0], self.config["provider_sources"][0])
        self.assertEqual(current["provider_settings"], self.config["provider_settings"])
        self.assertEqual(current["untouched"], self.config["untouched"])
        source, provider = current["provider_sources"][-1], current["provider"][-1]
        self.assertEqual(source["key"], ["$ROLEPLAY_DEEPSEEK_API_KEY"])
        self.assertEqual(source["timeout"], 30)
        self.assertEqual(source["proxy"], "")
        self.assertEqual(provider["provider_source_id"], source["id"])
        self.assertEqual(provider["custom_extra_body"], {"thinking": {"type": "disabled"}})
        self.assertTrue(self.key.encode() not in self.config_path.read_bytes())
        self.assertTrue(self.env_path.read_bytes().startswith(before[1]))
        self.assertTrue(self.key.encode() in self.env_path.read_bytes())
        self.assertTrue(original_access == (self.root / "config/model-access.local.json").read_bytes())
        backup = next((self.runtime / "backups").iterdir())
        self.assertTrue((backup / "astrbot.env").read_bytes() == before[1])
        self.assertTrue((backup / "cmd_config.json").read_bytes() == before[0])

    def test_repeated_apply_is_idempotent_and_adds_no_backup(self):
        configure.execute(self.root, apply=True)
        before = self.snapshot()
        backups = list((self.runtime / "backups").iterdir())
        result = configure.execute(self.root, apply=True)
        self.assertEqual(result["status"], "already_configured")
        self.assertTrue(before == self.snapshot())
        self.assertEqual(list((self.runtime / "backups").iterdir()), backups)

    def test_second_replace_failure_restores_original_pair(self):
        before = self.snapshot()
        replace = configure.os.replace
        calls = 0

        def fail_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic failure must not print " + self.key)
            return replace(source, target)

        with patch.object(configure.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(configure.ConfigurationError, "apply_failed_rolled_back") as caught:
                configure.execute(self.root, apply=True)
        self.assertNotIn(self.key, str(caught.exception))
        self.assertTrue(before == self.snapshot())
        self.assertFalse((self.runtime / ".configure-deepseek.lock").exists())
        self.assertEqual(list(self.runtime.rglob(".configure-*.tmp")), [])

    def test_evidence_requires_actual_container_and_matching_success(self):
        original = copy.deepcopy(self.evidence)
        for field, value in (("runtime", "windows"), ("generation_http_status", 401),
                             ("actual_model", "another-model"), ("success", False),
                             ("stream_done", False), ("nonempty_content_chunks", 0)):
            with self.subTest(field=field):
                self.evidence = copy.deepcopy(original)
                self.evidence[field] = value
                self.save_inputs()
                before = self.snapshot()
                with self.assertRaises(configure.ConfigurationError):
                    configure.execute(self.root, apply=True)
                self.assertTrue(before == self.snapshot())

    def test_unofficial_endpoint_and_dotenv_injection_are_refused(self):
        original = copy.deepcopy(self.access)
        for field, value in (("base_url", "https://other.invalid"),
                             ("api_key", "synthetic_key\nOTHER=overwritten")):
            with self.subTest(field=field):
                self.access = copy.deepcopy(original)
                self.access["deepseek_official"][field] = value
                self.save_inputs()
                before = self.snapshot()
                with self.assertRaises(configure.ConfigurationError):
                    configure.execute(self.root, apply=True)
                self.assertTrue(before == self.snapshot())

    def test_concurrent_file_change_is_not_overwritten(self):
        plan = configure.make_plan(self.root)
        self.env_path.write_bytes(self.env_path.read_bytes() + b"USER_ADDITION=keep\r\n")
        before = self.snapshot()
        with self.assertRaisesRegex(configure.ConfigurationError, "runtime_changed_since_plan"):
            configure.apply_plan(plan)
        self.assertTrue(before == self.snapshot())


if __name__ == "__main__":
    unittest.main()
