"""Offline bootstrap checks; all generated credentials stay in temporary folders."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_local", PROJECT / "scripts/prepare_local.py")
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class PrepareLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        # Windows sandbox temporary folders may deny their own child writes.
        temp_base = PROJECT / "work/test-bootstrap"
        temp_base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="astrbot-bootstrap-test-", dir=temp_base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.persona = self.root / "persona.txt"
        self.persona.write_text("用于离线测试的人设。\n", encoding="utf-8")
        self.inputs = {
            "qq": {
                "bot_account_id": "123456789",
                "admin_account_ids": ["234567890"],
                "private_contact_allowlist": ["345678901"],
                "test_group_ids": ["456789012"],
            },
            "persona": {
                "source_file": "persona.txt",
                "sha256": hashlib.sha256(self.persona.read_bytes()).hexdigest(),
            },
        }
        self.save_inputs()

    def save_inputs(self) -> None:
        config = self.root / "config/connection-inputs.local.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps(self.inputs), encoding="utf-8")

    def runtime_bytes(self) -> dict[str, bytes]:
        runtime = self.root / "runtime"
        return {str(p.relative_to(runtime)): p.read_bytes() for p in runtime.rglob("*") if p.is_file()}

    def test_creation_has_matching_tokens_and_closed_model_access(self) -> None:
        self.assertTrue(bootstrap.prepare(self.root))
        runtime = self.root / "runtime"
        config = json.loads((runtime / "astrbot/cmd_config.json").read_text("utf-8"))
        onebot = json.loads((runtime / "napcat/config/onebot11_123456789.json").read_text("utf-8"))
        generic = json.loads((runtime / "napcat/config/onebot11.json").read_text("utf-8"))
        self.assertTrue(config["platform_settings"]["enable_id_white_list"])
        self.assertEqual(config["platform_settings"]["id_whitelist"], [
            "qq-sakura:GroupMessage:456789012", "qq-sakura:FriendMessage:345678901",
        ])
        self.assertFalse(config["provider_settings"]["enable"])
        self.assertEqual(config["provider"], [])
        self.assertEqual(generic["network"]["websocketClients"], [])
        # Compare via a boolean so unittest never prints generated secret values.
        self.assertTrue(config["platform"][0]["ws_reverse_token"] ==
                        onebot["network"]["websocketClients"][0]["token"])
        self.assertGreaterEqual(len(config["platform"][0]["ws_reverse_token"]), 32)
        self.assertEqual((runtime / "roleplay/personas/persona.txt").read_bytes(), self.persona.read_bytes())

    def test_repeated_prepare_preserves_credentials_login_and_edited_config(self) -> None:
        bootstrap.prepare(self.root)
        login = self.root / "runtime/napcat/qq/login-state.bin"
        login.write_bytes(b"temporary login-state fixture")
        path = self.root / "runtime/astrbot/cmd_config.json"
        config = json.loads(path.read_text("utf-8"))
        config["log_level"] = "ERROR"
        path.write_text(json.dumps(config), "utf-8")
        before = self.runtime_bytes()
        self.assertFalse(bootstrap.prepare(self.root))
        self.assertTrue(before == self.runtime_bytes())

    def test_verification_progress_changes_do_not_block_restart(self) -> None:
        bootstrap.prepare(self.root)
        before = self.runtime_bytes()
        self.inputs["qq"]["napcat_login_verified"] = True
        self.inputs["qq"]["onebot_connection_verified"] = True
        self.inputs["verification"] = {"checked_at": "2026-10-23", "status": "local-ready"}
        self.inputs["persona"]["import_verified"] = True
        self.save_inputs()
        self.assertFalse(bootstrap.prepare(self.root))
        self.assertFalse(bootstrap.prepare(self.root))
        self.assertTrue(before == self.runtime_bytes())

    def test_changed_inputs_fail_without_touching_runtime(self) -> None:
        bootstrap.prepare(self.root)
        before = self.runtime_bytes()
        self.inputs["qq"]["test_group_ids"] = ["567890123"]
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, "不一致"):
            bootstrap.prepare(self.root)
        self.assertTrue(before == self.runtime_bytes())

    def test_missing_required_file_is_not_regenerated(self) -> None:
        bootstrap.prepare(self.root)
        missing = self.root / "runtime/napcat.env"
        missing.unlink()
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "缺少配置"):
            bootstrap.prepare(self.root)
        self.assertTrue(before == self.runtime_bytes())
        self.assertFalse(missing.exists())

    def test_missing_generic_onebot_guard_blocks_restart(self) -> None:
        bootstrap.prepare(self.root)
        missing = self.root / "runtime/napcat/config/onebot11.json"
        missing.unlink()
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "缺少配置"):
            bootstrap.prepare(self.root)
        self.assertTrue(before == self.runtime_bytes())
        self.assertFalse(missing.exists())

    def test_missing_persona_copy_blocks_restart(self) -> None:
        bootstrap.prepare(self.root)
        missing = self.root / "runtime/roleplay/personas/persona.txt"
        missing.unlink()
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "缺少配置"):
            bootstrap.prepare(self.root)
        self.assertTrue(before == self.runtime_bytes())
        self.assertFalse(missing.exists())

    def test_missing_persona_import_blocks_restart(self) -> None:
        bootstrap.prepare(self.root)
        missing = self.root / "runtime/imports/sakura_mitsuha.json"
        missing.unlink()
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "缺少配置"):
            bootstrap.prepare(self.root)
        self.assertTrue(before == self.runtime_bytes())
        self.assertFalse(missing.exists())

    def test_empty_whitelists_and_admin_lists_fail_closed(self) -> None:
        original = copy.deepcopy(self.inputs)
        for key in ("admin_account_ids", "private_contact_allowlist", "test_group_ids"):
            with self.subTest(key=key):
                self.inputs = copy.deepcopy(original)
                self.inputs["qq"][key] = []
                self.save_inputs()
                with self.assertRaisesRegex(ValueError, "非空"):
                    bootstrap.prepare(self.root)
                self.assertFalse((self.root / "runtime").exists())

    def test_bot_cannot_be_admin_or_private_test_contact(self) -> None:
        original = copy.deepcopy(self.inputs)
        for key in ("admin_account_ids", "private_contact_allowlist"):
            with self.subTest(key=key):
                self.inputs = copy.deepcopy(original)
                self.inputs["qq"][key] = [self.inputs["qq"]["bot_account_id"]]
                self.save_inputs()
                with self.assertRaisesRegex(ValueError, "机器人账号"):
                    bootstrap.prepare(self.root)
                self.assertFalse((self.root / "runtime").exists())

    def test_persona_path_cannot_escape_project(self) -> None:
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside fixture", "utf-8")
        self.inputs["persona"]["source_file"] = "../outside.txt"
        self.inputs["persona"]["sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, "项目目录内"):
            bootstrap.prepare(self.root)
        self.assertEqual(outside.read_text("utf-8"), "outside fixture")
        self.assertFalse((self.root / "runtime").exists())

    def test_changed_persona_fails_without_runtime(self) -> None:
        self.persona.write_text("unapproved edit", "utf-8")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            bootstrap.prepare(self.root)
        self.assertFalse((self.root / "runtime").exists())

    def test_partial_creation_rolls_back_only_staging(self) -> None:
        sentinel = self.root / "unrelated.txt"
        sentinel.write_text("retain", "utf-8")
        with patch.object(bootstrap.shutil, "copyfile", side_effect=OSError("simulated copy failure")):
            with self.assertRaisesRegex(OSError, "simulated"):
                bootstrap.prepare(self.root)
        self.assertFalse((self.root / "runtime").exists())
        self.assertEqual(list(self.root.glob(".prepare-*")), [])
        self.assertEqual(sentinel.read_text("utf-8"), "retain")


if __name__ == "__main__":
    unittest.main()
