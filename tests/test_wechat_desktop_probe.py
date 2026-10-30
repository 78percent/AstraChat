"""Desktop wrapper contracts using synthetic clients, never the live UI."""

import argparse
import builtins
import contextlib
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import wechat_desktop_probe as probe


class FakeClient:
    def __init__(self, messages=(), *, status=None):
        self.messages = messages
        self.info = {"chat_name": "synthetic-group", "chat_type": "group"}
        self.events = []
        self.status = {"status": "成功"} if status is None else status

    def ChatInfo(self):
        self.events.append("guard")
        return self.info

    def GetAllMessage(self):
        self.events.append("read")
        return self.messages

    def SendMsg(self, text, **kwargs):
        self.events.append(("send", text, kwargs))
        return self.status


def raw_message(*, ui_id=(42, 3), attr="friend", kind="text",
                sender="synthetic-member", content="synthetic-body"):
    return SimpleNamespace(id=ui_id, attr=attr, type=kind, sender=sender, content=content)


def prerequisites():
    return {"target_group": "synthetic-group", "account_confirmed": True,
            "unique_group_confirmed": True, "interactive_desktop_confirmed": True}


@contextlib.contextmanager
def test_directory():
    # The Windows sandbox's system TEMP can have a non-inherited deny ACL.
    # Keep synthetic test data and TemporaryDirectory cleanup inside the project.
    parent = (Path(__file__).resolve().parents[1] / "work").resolve()
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wxprobe-test-", dir=parent) as directory:
        if not Path(directory).resolve().is_relative_to(parent):
            raise RuntimeError("test_directory_outside_workspace")
        yield directory


class DesktopClientContractTests(unittest.TestCase):
    def test_wrong_target_guard_prevents_read(self):
        client = FakeClient()
        client.info["chat_name"] = "other-group"
        with self.assertRaisesRegex(probe.ProbeError, "target_not_confirmed"):
            probe.DesktopClient(client, "synthetic-group").snapshot()
        self.assertEqual(client.events, ["guard"])

    def test_private_chat_with_same_title_prevents_read(self):
        client = FakeClient()
        client.info["chat_type"] = "friend"
        with self.assertRaises(probe.ProbeError):
            probe.DesktopClient(client, "synthetic-group").snapshot()
        self.assertNotIn("read", client.events)

    def test_changed_target_after_read_prevents_message_field_access(self):
        class Unreadable:
            @property
            def id(self):
                raise AssertionError("message body must not be inspected")
        client = FakeClient([Unreadable()])
        original = client.GetAllMessage
        def changed():
            result = original()
            client.info = {"chat_name": "changed", "chat_type": "group"}
            return result
        client.GetAllMessage = changed
        with self.assertRaisesRegex(probe.ProbeError, "target_not_confirmed"):
            probe.DesktopClient(client, "synthetic-group").snapshot()
        self.assertEqual(client.events, ["guard", "read", "guard"])

    def test_snapshot_preserves_member_self_system_types_without_coercing_identity(self):
        client = FakeClient([
            raw_message(),
            raw_message(ui_id=43, attr="self", sender=None, content="self body"),
            raw_message(ui_id="sys-1", attr="system", kind="system", sender=None, content=None),
        ])
        messages = probe.DesktopClient(client, "synthetic-group").snapshot()
        self.assertEqual([item.ui_id for item in messages], ["42:3", "43", "sys-1"])
        self.assertEqual([item.attr for item in messages], ["friend", "self", "system"])
        self.assertEqual(messages[0].sender, "synthetic-member")
        self.assertEqual(messages[1].sender, "")
        self.assertEqual(messages[2].type, "system")
        self.assertEqual(messages[2].content, "")
        self.assertEqual(client.events, ["guard", "read", "guard", "guard"])

    def test_invalid_message_fields_and_collections_fail_closed(self):
        for raw in ([raw_message(ui_id=True)], [raw_message(ui_id=[1, True])],
                    [raw_message(sender=object())], [raw_message(content=object())],
                    {"unexpected": "mapping"}, [raw_message()] * 1001):
            with self.subTest(kind=type(raw).__name__), self.assertRaises(probe.ProbeError):
                probe.DesktopClient(FakeClient(raw), "synthetic-group").snapshot()

    def test_target_guard_prevents_send_and_send_does_not_choose_chat(self):
        client = FakeClient()
        client.info["chat_name"] = "changed"
        with self.assertRaises(probe.ProbeError):
            probe.DesktopClient(client, "synthetic-group").send("synthetic reply")
        self.assertEqual(client.events, ["guard"])
        client.info["chat_name"] = "synthetic-group"
        self.assertTrue(probe.DesktopClient(client, "synthetic-group").send("synthetic reply"))
        self.assertEqual(client.events[-2], ("send", "synthetic reply", {
            "who": None, "clear": True, "exact": True}))
        self.assertEqual(client.events[-1], "guard")

    def test_failed_nonempty_dict_and_other_truthy_values_are_not_success(self):
        for status in ({"status": "失败", "message": "nonempty"}, {"status": True},
                       {"success": True}, True, "成功", ["成功"]):
            with self.subTest(status=status):
                self.assertFalse(probe.DesktopClient(FakeClient(status=status), "synthetic-group").send("reply"))

    def test_changed_target_after_send_does_not_report_local_success(self):
        client = FakeClient()
        original = client.SendMsg
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            client.info["chat_name"] = "changed"
            return result
        client.SendMsg = changed
        with self.assertRaisesRegex(probe.ProbeError, "target_not_confirmed"):
            probe.DesktopClient(client, "synthetic-group").send("reply")
        self.assertEqual(sum(isinstance(event, tuple) for event in client.events), 1)


class ConfigurationAndNoUiTests(unittest.TestCase):
    def config_file(self, directory, value):
        path = Path(directory) / "synthetic-config.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_all_three_explicit_prerequisites_required(self):
        with test_directory() as directory:
            for name in ("account_confirmed", "unique_group_confirmed", "interactive_desktop_confirmed"):
                for value in (None, False, 1, "true"):
                    config = prerequisites()
                    config[name] = value
                    with self.subTest(name=name, value=value), self.assertRaisesRegex(
                            probe.ProbeError, "manual_prerequisites_not_confirmed"):
                        probe.load_config(self.config_file(directory, config))
            config = prerequisites()
            self.assertEqual(probe.load_config(self.config_file(directory, config)), config)

    def test_config_invalid_targets_and_missing_file_are_static_errors(self):
        with test_directory() as directory:
            for value in ("", " spaces ", "two\nlines", "x" * 81, None):
                config = prerequisites()
                config["target_group"] = value
                with self.subTest(value=value), self.assertRaisesRegex(probe.ProbeError, "invalid_target"):
                    probe.load_config(self.config_file(directory, config))
            with self.assertRaisesRegex(probe.ProbeError, "configuration_unavailable"):
                probe.load_config(Path(directory) / "private-file-name.json")

    def no_wxauto_import(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.startswith("wxauto"):
                raise AssertionError("environment/help must not import UI package")
            return original(name, *args, **kwargs)
        return patch("builtins.__import__", side_effect=guarded)

    def test_environment_check_reads_only_metadata(self):
        values = {"wxauto4": probe.VERSION, "pywin32": "311", "comtypes": "1.4.13",
                  "httpx": "0.28.1", "psutil": "7.1.0"}
        with self.no_wxauto_import(), patch.object(probe.importlib.metadata, "version", side_effect=values.__getitem__), \
                patch.object(probe, "installed_client_version", return_value=None):
            result = probe.environment_check()
        self.assertEqual(result["packages"], values)
        self.assertFalse(result["wechat_imported"])
        self.assertFalse(result["ui_touched"])

    def test_missing_dependency_is_not_ready(self):
        with self.no_wxauto_import(), patch.object(probe.importlib.metadata, "version",
                side_effect=importlib.metadata.PackageNotFoundError), \
                patch.object(probe, "installed_client_version", return_value=None):
            result = probe.environment_check()
        self.assertFalse(result["ready"])
        self.assertTrue(all(version is None for version in result["packages"].values()))

    def test_installed_version_comes_from_executable_file_resource(self):
        with test_directory() as directory:
            executable = Path(directory) / "synthetic-Weixin.exe"
            executable.write_bytes(b"synthetic file; never executable")
            config = self.config_file(directory, {"client_executable": str(executable)})
            calls = []
            def resource(path, key):
                calls.append((path, key))
                return {"FileVersionMS": (4 << 16) | 1, "FileVersionLS": (15 << 16) | 56}
            fake_win32api = SimpleNamespace(GetFileVersionInfo=resource)
            with self.no_wxauto_import(), patch.object(probe, "CONFIG", config), \
                    patch.object(probe.sys, "platform", "win32"), \
                    patch.dict(sys.modules, {"win32api": fake_win32api}):
                self.assertEqual(probe.installed_client_version(), (4, 1, 15, 56))
            self.assertEqual(calls, [(str(executable), "\\")])

    def test_dependency_ready_and_client_support_are_separate_metadata(self):
        versions = {"wxauto4": probe.VERSION, "pywin32": "311", "comtypes": "1.4.13",
                    "httpx": "0.28.1", "psutil": "7.1.0"}
        for version, exceeds in (((4, 1, 8, 107), False), ((4, 1, 15, 56), True), (None, None)):
            with self.subTest(version=version), self.no_wxauto_import(), \
                    patch.object(probe.importlib.metadata, "version", side_effect=versions.__getitem__), \
                    patch.object(probe, "installed_client_version", return_value=version), \
                    patch.object(probe.sys, "platform", "win32"), \
                    patch.object(probe.sys, "version_info", (3, 12, 0)):
                report = probe.environment_check()
            self.assertTrue(report["ready"])
            self.assertIs(report["client_exceeds_declared_maximum"], exceeds)
            self.assertFalse(report["live_compatibility_proven"])
            self.assertEqual(report["client_declared_maximum"], "4.1.8.107")

    def test_unsupported_client_blocks_parent_and_worker_before_ui_or_config(self):
        for extra in ([], ["--worker", "--report-path", "unused.json"]):
            with self.subTest(extra=extra), self.no_wxauto_import(), \
                    patch.object(probe, "environment_check", return_value={
                        "ready": True, "client_exceeds_declared_maximum": True}), \
                    patch.object(probe, "load_config", side_effect=AssertionError("no target access")), \
                    patch.object(probe, "worker", side_effect=AssertionError("no UI worker")), \
                    patch.object(probe.subprocess, "Popen", side_effect=AssertionError("no UI process")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                result = probe.main(["--mode", "run", "--allow-send", "--nonce", "abcd1234", *extra])
            self.assertEqual(result, 2)
            self.assertIn("client_outside_declared_support", output.getvalue())

    def test_help_does_not_import_ui_or_load_live_configuration(self):
        with self.no_wxauto_import(), patch.object(probe, "load_config", side_effect=AssertionError), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                probe.main(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_default_check_does_not_load_live_configuration(self):
        with self.no_wxauto_import(), patch.object(probe, "load_config", side_effect=AssertionError), \
                patch.object(probe, "environment_check", return_value={"ready": True}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main([]), 0)

    def test_old_nonce_report_is_not_overwritten_or_relaunched(self):
        with test_directory() as directory:
            root = Path(directory)
            report_dir = root / "probes"
            report_dir.mkdir()
            report = report_dir / "abcd1234.json"
            original = '{"stage":"finished","dispatch_attempted":true}'
            report.write_text(original, encoding="utf-8")
            with patch.object(probe, "ROOT", root), patch.object(probe, "REPORT_DIR", report_dir), \
                    patch.object(probe, "environment_check", return_value={"ready": True}), \
                    patch.object(probe, "load_config", return_value=prerequisites()), \
                    patch.object(probe.subprocess, "Popen", side_effect=AssertionError("must not relaunch")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(probe.main(["--mode", "run", "--nonce", "abcd1234"]), 2)
            self.assertIn("nonce_already_used", output.getvalue())
            self.assertEqual(report.read_text(encoding="utf-8"), original)

    def test_hidden_worker_cannot_replay_finished_or_unclaimed_nonce(self):
        for stage, token in (("finished", "synthetic-token"), ("starting", "")):
            with self.subTest(stage=stage), test_directory() as directory:
                report_dir = Path(directory) / "probes"
                report_dir.mkdir()
                report = report_dir / "abcd1234.json"
                original = json.dumps({"stage": stage, "launch_hash":
                                       hashlib.sha256(b"synthetic-token").hexdigest()})
                report.write_text(original, encoding="utf-8")
                with patch.object(probe, "REPORT_DIR", report_dir), \
                        patch.object(probe, "environment_check", return_value={"ready": True}), \
                        patch.object(probe, "load_config", return_value=prerequisites()), \
                        patch.object(probe, "worker", side_effect=AssertionError("must not replay")), \
                        patch.dict(probe.os.environ, {"ASTRBOT_WECHAT_PROBE_LAUNCH": token}), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    result = probe.main(["--worker", "--mode", "run", "--nonce", "abcd1234",
                                         "--report-path", str(report)])
                self.assertEqual(result, 2)
                self.assertIn("invalid_worker_launch", output.getvalue())
                self.assertEqual(report.read_text(encoding="utf-8"), original)

    def test_worker_launch_claim_is_consumed_only_once(self):
        with test_directory() as directory:
            report_dir = Path(directory) / "probes"
            report_dir.mkdir()
            report = report_dir / "abcd1234.json"
            report.write_text(json.dumps({"stage": "starting", "launch_hash":
                              hashlib.sha256(b"synthetic-token").hexdigest()}), encoding="utf-8")
            report.with_suffix(".consumed").write_text("", encoding="utf-8")
            with patch.object(probe, "REPORT_DIR", report_dir), \
                    patch.object(probe, "environment_check", return_value={"ready": True}), \
                    patch.object(probe, "load_config", return_value=prerequisites()), \
                    patch.object(probe, "worker", side_effect=AssertionError("must not replay")), \
                    patch.dict(probe.os.environ, {"ASTRBOT_WECHAT_PROBE_LAUNCH": "synthetic-token"}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                result = probe.main(["--worker", "--mode", "run", "--nonce", "abcd1234",
                                     "--report-path", str(report)])
            self.assertEqual(result, 2)
            self.assertIn("nonce_already_used", output.getvalue())


class WorkerContractTests(unittest.TestCase):
    def run_fake_worker(self, client, *, allow_send=True, mode="run"):
        args = argparse.Namespace(mode=mode, nonce="abcd1234", allow_send=allow_send, seconds=5)
        fake_module = SimpleNamespace(WeChat=lambda **kwargs: client, WxParam=SimpleNamespace())
        clock = [100.0]
        def tick():
            clock[0] += .1
            return clock[0]
        with test_directory() as directory:
            path = Path(directory) / "report.json"
            with patch.object(probe, "load_config", return_value=prerequisites()), \
                    patch.object(probe, "environment_check", return_value={"ready": True}), \
                    patch.object(probe, "QuietLibrary", return_value=contextlib.nullcontext()), \
                    patch.dict(sys.modules, {"wxauto4": fake_module}), \
                    patch.object(probe.time, "monotonic", side_effect=tick), \
                    patch.object(probe.time, "sleep"), patch.object(probe.logging, "disable"), \
                    patch.object(sys, "__stdout__", io.StringIO()):
                probe.worker(args, path)
            return json.loads(path.read_text(encoding="utf-8"))

    def conversation_client(self, *, send_failure=False, throw_send=False):
        class Conversation(FakeClient):
            reads = 0
            sent = False
            def GetAllMessage(self):
                self.events.append("read")
                self.reads += 1
                if self.reads == 1:
                    return [raw_message(content="old private body")]
                messages = [raw_message(ui_id="command", content="三叶 桌面测试 abcd1234")]
                if self.sent and not send_failure:
                    messages.append(raw_message(ui_id="reply", attr="self",
                                                content="桌面连通验证：已收到本次唤醒。[abcd1234]"))
                return messages
            def SendMsg(self, text, **kwargs):
                self.events.append(("send", text, kwargs))
                self.sent = True
                if throw_send:
                    raise RuntimeError("private body in vendor exception")
                return {"status": "失败" if send_failure else "成功"}
        return Conversation()

    def test_worker_sends_once_and_reports_only_ui_evidence(self):
        client = self.conversation_client()
        report = self.run_fake_worker(client)
        self.assertEqual(sum(isinstance(event, tuple) for event in client.events), 1)
        self.assertEqual(report["probe"]["state"], "UI_OBSERVED")
        self.assertIsNone(report["platform_message_id"])
        self.assertFalse(report["server_delivery_confirmed"])
        self.assertIsNone(report["peer_received"])
        self.assertFalse(report["model_called"])
        self.assertFalse(report["production_memory_written"])
        serialized = json.dumps(report)
        for private in ("old private body", "synthetic-member", "synthetic-group"):
            self.assertNotIn(private, serialized)

    def test_worker_observe_only_never_sends(self):
        client = self.conversation_client()
        report = self.run_fake_worker(client, allow_send=False)
        self.assertFalse(any(isinstance(event, tuple) for event in client.events))
        self.assertFalse(report["dispatch_attempted"])
        self.assertEqual(report["probe"]["counts"]["admissions"], 1)

    def test_failed_truthy_api_response_never_becomes_success_or_retry(self):
        client = self.conversation_client(send_failure=True)
        report = self.run_fake_worker(client)
        self.assertFalse(report["ui_call_reported_success"])
        self.assertEqual(report["probe"]["state"], "UNKNOWN")
        self.assertEqual(sum(isinstance(event, tuple) for event in client.events), 1)

    def test_send_exception_remains_unknown_without_leaking_vendor_text(self):
        client = self.conversation_client(throw_send=True)
        report = self.run_fake_worker(client)
        self.assertTrue(report["dispatch_attempted"])
        self.assertEqual(report["probe"]["state"], "UNKNOWN")
        self.assertEqual(report["error_type"], "RuntimeError")
        self.assertNotIn("private body", json.dumps(report))
        self.assertEqual(sum(isinstance(event, tuple) for event in client.events), 1)

    def test_guard_failure_before_dispatch_prevents_send(self):
        client = self.conversation_client()
        original = client.ChatInfo
        checks = [0]
        def changed_before_dispatch():
            checks[0] += 1
            if checks[0] == 7:
                client.info["chat_name"] = "changed"
            return original()
        client.ChatInfo = changed_before_dispatch
        report = self.run_fake_worker(client)
        self.assertFalse(report["dispatch_attempted"])
        self.assertFalse(any(isinstance(event, tuple) for event in client.events))
        self.assertEqual(report["probe"]["state"], "FAILED_BEFORE_DISPATCH")


if __name__ == "__main__":
    unittest.main()
