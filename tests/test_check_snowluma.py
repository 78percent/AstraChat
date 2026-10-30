"""Offline preflight boundary checks with synthetic credentials; no Docker calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_snowluma", PROJECT / "scripts/check_snowluma.py")
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class SnowLumaPreflightTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / "work/test-check-snowluma"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="snowluma-preflight-", dir=base)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(base.resolve())
        self.addCleanup(self.temp.cleanup)
        self.token = "synthetic-onebot-secret-never-print"
        self.access = {"webui_password": "SyntheticWebPassword!", "vnc_password": "FakeVNC8"}
        self.write("compose.snowluma.yaml", "fixture only")
        self.write_json("runtime/snowluma/access.local.json", self.access)
        self.write_json("config/connection-inputs.local.json", {"qq": {
            "bot_account_id": "123456789", "admin_account_ids": ["234567890"],
            "private_contact_allowlist": ["234567890"], "test_group_ids": ["345678901"]}})
        self.write_json("runtime/astrbot/cmd_config.json", {"platform": [{
            "id": "qq-sakura", "type": "aiocqhttp", "enable": True,
            "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199, "ws_reverse_token": self.token}]})
        self.generic = {"networks": {k: [] for k in checker.NETWORKS},
                        "statusCommand": {"enabled": False, "swallow": False, "cooldownSeconds": 5, "trigger": "#sl"},
                        "historySync": {"enabled": False}, "notifications": {"channelIds": []}}
        self.account = copy.deepcopy(self.generic)
        self.account["mode"] = "snapshot"
        self.account["networks"]["wsClients"] = [{
            "name": "astrbot-reverse", "enabled": True, "url": "ws://astrbot:6199/ws",
            "role": "Universal", "accessToken": self.token, "messageFormat": "array",
            "reportSelfMessage": False, "reconnectIntervalMs": 5000}]
        self.write_json("runtime/snowluma/data/config/onebot.json", self.generic)
        self.write_json("runtime/snowluma/data/config/onebot_123456789.json", self.account)
        self.compose = {"name": checker.PROJECT_NAME,
            "networks": {"existing-bot": {"name": "astrbot-roleplay_bot", "external": True, "ipam": {}}},
            "volumes": {name: {"name": checker.PROJECT_NAME + "_" + name}
                        for name in ("snowluma-qq-config", "snowluma-qq-data")},
            "services": {"snowluma": {
                "image": checker.IMAGE, "platform": "linux/amd64", "restart": "unless-stopped",
                "shm_size": "1073741824", "cap_add": ["SYS_PTRACE"], "security_opt": ["seccomp=unconfined"],
                "ulimits": {"nofile": {"soft": 65536, "hard": 1048576}},
                "networks": {"existing-bot": None}, "entrypoint": None, "command": None,
                "environment": {**checker.ENVIRONMENT, "VNC_PASSWD": self.access["vnc_password"],
                                "SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD": self.access["webui_password"]},
                "ports": [{"host_ip": "127.0.0.1", "target": p, "published": str(p), "protocol": "tcp", "mode": "ingress"}
                          for p in (5099, 6081)],
                "volumes": [
                    {"type": "bind", "source": str(self.root / "runtime/snowluma/data"), "target": "/app/data", "bind": {}},
                    {"type": "volume", "source": "snowluma-qq-config", "target": "/app/.config", "volume": {}},
                    {"type": "volume", "source": "snowluma-qq-data", "target": "/app/.local/share", "volume": {}}],
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}}}

    def write(self, name, value):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, "utf8")

    def write_json(self, name, value):
        self.write(name, json.dumps(value))

    def assert_redacted(self, value):
        for secret in (self.token, *self.access.values()):
            self.assertNotIn(secret, str(value))

    def test_valid_configuration_is_read_only_and_not_live_evidence(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(checker, "render_compose", return_value=self.compose), \
             patch.object(checker.legacy, "check_runtime", side_effect=AssertionError("must remain separate")):
            report = checker.check_snowluma(self.root)
        self.assertTrue(report["ok"])
        for flag in ("runtime_modified", "legacy_configuration_check_performed", "qq_login_verified",
                     "reverse_ws_connection_verified", "message_delivery_verified"):
            self.assertFalse(report[flag])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assert_redacted(json.dumps(report))

    def test_compose_boundary_changes_are_rejected(self):
        cases = [
            ("public management", lambda c, s: s["ports"][0].update(host_ip="0.0.0.0")),
            ("extra port", lambda c, s: s["ports"].append({"host_ip": "127.0.0.1", "target": 3000, "published": "3000"})),
            ("digest changed", lambda c, s: s.update(image="motricseven7/snowluma:latest")),
            ("entrypoint changed", lambda c, s: s.update(entrypoint=["sh", "-c", "unexpected"])),
            ("privileged", lambda c, s: s.update(privileged=True)),
            ("host network", lambda c, s: s.update(network_mode="host")),
            ("additional service", lambda c, s: c["services"].update(napcat={})),
            ("reused volume", lambda c, s: c["volumes"]["snowluma-qq-data"].update(name="napcat-data")),
            ("external volume", lambda c, s: c["volumes"]["snowluma-qq-data"].update(external=True)),
            ("extra mount", lambda c, s: s["volumes"].append({"type": "bind", "source": "/", "target": "/host"})),
            ("duplicate target", lambda c, s: s["volumes"][2].update(target="/app/.config")),
            ("unexpected network", lambda c, s: c["networks"]["existing-bot"].update(name="other")),
            ("implicit consent", lambda c, s: s["environment"].update(SNOWLUMA_ACCEPT_EULA="1")),
            ("wrong password", lambda c, s: s["environment"].update(VNC_PASSWD="other")),
            ("extra environment", lambda c, s: s["environment"].update(SNOWLUMA_DEV_MODE="1")),
            ("remote log", lambda c, s: s.update(logging={"driver": "syslog"})),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                config = copy.deepcopy(self.compose)
                mutate(config, config["services"]["snowluma"])
                with self.assertRaises(checker.PreflightError) as result:
                    checker.validate_compose(self.root, config, self.access)
                self.assert_redacted(result.exception)

    def test_napcat_bind_reuse_is_rejected(self):
        other = self.root / "runtime/napcat/qq"
        other.mkdir(parents=True)
        self.compose["services"]["snowluma"]["volumes"][0]["source"] = str(other)
        with self.assertRaisesRegex(checker.PreflightError, "独立目录"):
            checker.validate_compose(self.root, self.compose, self.access)

    def test_onebot_changes_are_rejected(self):
        cases = [
            lambda c: c.update(mode="merge"),
            lambda c: c.update(riskControl={"fake": True}),
            lambda c: c["networks"]["httpServers"].append({"enabled": True}),
            lambda c: c["networks"]["wsClients"].append(copy.deepcopy(c["networks"]["wsClients"][0])),
            lambda c: c["networks"]["wsClients"][0].update(url="ws://other:6199/ws"),
            lambda c: c["networks"]["wsClients"][0].update(role="Event"),
            lambda c: c["networks"]["wsClients"][0].update(messageFormat="string"),
            lambda c: c["networks"]["wsClients"][0].update(reportSelfMessage=True),
            lambda c: c["networks"]["wsClients"][0].update(accessToken="other"),
            lambda c: c["networks"]["wsClients"][0].update(enabled=1),
            lambda c: c["statusCommand"].update(enabled=True),
            lambda c: c["historySync"].update(enabled=True),
            lambda c: c["notifications"].update(channelIds=["unreviewed"]),
        ]
        for idx, mutate in enumerate(cases):
            with self.subTest(case=idx):
                config = copy.deepcopy(self.account)
                mutate(config)
                with self.assertRaises(checker.PreflightError) as result:
                    checker.validate_onebot(config, account=True, token=self.token)
                self.assert_redacted(result.exception)
        self.generic["networks"]["wsClients"] = self.account["networks"]["wsClients"]
        with self.assertRaises(checker.PreflightError):
            checker.validate_onebot(self.generic, account=False)

    def test_compose_render_is_config_only_and_captures_output(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(self.compose), "")
        with patch.object(checker.subprocess, "run", return_value=response) as run:
            result = checker.render_compose(self.root)
        self.assertEqual(result, self.compose)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-3:], ["config", "--format", "json"])
        self.assertIn("-f", args[0])
        self.assertTrue(kwargs["capture_output"])
        self.assertFalse(kwargs.get("shell", False))

    def test_failed_compose_output_is_never_exposed(self):
        secret = self.token + self.access["webui_password"]
        cases = [subprocess.CompletedProcess([], 1, secret, secret),
                 subprocess.CompletedProcess([], 0, secret, secret)]
        for response in cases:
            with patch.object(checker.subprocess, "run", return_value=response):
                with self.assertRaises(checker.PreflightError) as result:
                    checker.render_compose(self.root)
                self.assert_redacted(result.exception)
        with patch.object(checker.subprocess, "run", side_effect=subprocess.TimeoutExpired(secret, 30, output=secret)):
            with self.assertRaises(checker.PreflightError) as result:
                checker.render_compose(self.root)
            self.assert_redacted(result.exception)

    def test_cli_hides_unexpected_errors(self):
        for json_output in (False, True):
            output = io.StringIO()
            with patch.object(checker, "check_snowluma", side_effect=ValueError(self.token)), \
                 redirect_stdout(output), redirect_stderr(output):
                status = checker.main(["--root", str(self.root)] + (["--json"] if json_output else []))
            self.assertEqual(status, 1)
            self.assertIn("PREFLIGHT_INVALID", output.getvalue())
            self.assert_redacted(output.getvalue())

    def test_actual_astrbot_auth_must_match(self):
        self.account["networks"]["wsClients"][0]["accessToken"] = "changed"
        self.write_json("runtime/snowluma/data/config/onebot_123456789.json", self.account)
        with patch.object(checker, "render_compose", return_value=self.compose):
            with self.assertRaises(checker.PreflightError) as result:
                checker.check_snowluma(self.root)
        self.assertEqual(result.exception.code, "ONEBOT_SECRET_MISMATCH")


if __name__ == "__main__":
    unittest.main()
