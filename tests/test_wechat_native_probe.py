"""Runner contracts with a fake transport; never import or execute upstream code."""

import contextlib
import copy
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import wechat_native_probe as runner


NONCE = "abcd1234abcd1234"
SELF = "wxid_synthetic_bot"
ROOM = "123456789@chatroom"


def config_value(**changes):
    config = {"target_group_label": "test", "native_trial_authorized": True,
              "upstream_commit": runner.COMMIT, "expected_self": None,
              "allowed_room": None, "unrelated_setting": {"keep": "unchanged"}}
    config.update(changes)
    return config


def command_message(native_id=9007199254740999):
    return SimpleNamespace(type=1, sender=ROOM.encode(), receiver=SELF.encode(),
                           text=("wxid_synthetic_member:\n三叶 原生测试 " + NONCE).encode(),
                           native_message_id=native_id, sequence=1)


@contextlib.contextmanager
def test_directory():
    parent = (Path(__file__).resolve().parents[1] / "work").resolve()
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-probe-test-", dir=parent) as directory:
        if not Path(directory).resolve().is_relative_to(parent):
            raise RuntimeError("test_directory_outside_workspace")
        yield Path(directory)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        self.now += .025
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeTransport:
    def __init__(self, events=None):
        self.self_id = SELF
        self.loaded = False
        self.pending = False
        self.events = list(events if events is not None else [command_message()])
        self.calls = []
        self.remove_calls = 0
        self.sent = []
        self.send_hook = None
        self.close_error = False
        self.completion_success = True

    def preflight(self):
        self.calls.append("preflight")
        return {"client_match": True, "source_match": True, "native_loaded": False}

    def start(self):
        self.calls.append("start")
        self.loaded = True
        return {"state": "LOADED"}

    def poll(self):
        self.calls.append("poll")
        event = self.events.pop(0) if self.events else None
        if isinstance(event, BaseException):
            self.pending = True
            raise event
        if callable(event):
            return event()
        return event

    def send(self, room, text):
        self.calls.append("send")
        self.sent.append((room, text))
        if self.send_hook is not None:
            self.send_hook()
        return {"success": self.completion_success, "code": 0, "cleanup_code": 0,
                "report_retained": False,
                "report": {"completion_success": self.completion_success, "target": room, "text": text}}

    def close(self):
        self.calls.append("close")
        if self.close_error:
            raise RuntimeError("private vendor cleanup error")
        if self.pending:
            return {"state": "UNKNOWN", "dll_unloaded": False, "manual_recovery_required": True}
        if self.loaded:
            self.remove_calls += 1
            self.loaded = False
        return {"state": "CLEAN", "dll_unloaded": True, "manual_recovery_required": False}


class NativeRunnerTests(unittest.TestCase):
    def prepare(self, directory, config=None):
        config = copy.deepcopy(config if config is not None else config_value())
        config_path = directory / "config.local.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        report_path = directory / "probe.json"
        return config, config_path, report_path

    def run_trial(self, transport, config, config_path, report_path, *, stop=None):
        clock = Clock()
        return runner.run_trial(transport, config, NONCE, 2, report_path,
                                stop or threading.Event(), config_path=config_path,
                                clock=clock, sleep=clock.sleep, emit=lambda *a, **kw: None)

    def test_normal_run_dispatches_once_and_cleans_up_with_private_binding(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory, config_value(expected_self=SELF))
            transport = FakeTransport([command_message(), command_message(2)])
            report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(transport.calls, ["preflight", "start", "poll", "send", "close"])
            self.assertEqual(transport.remove_calls, 1)
            self.assertEqual(report["probe"]["state"], "NATIVE_COMPLETED")
            self.assertEqual(report["cleanup"]["state"], "CLEAN")
            self.assertIsNone(report["peer_received"])
            self.assertIsNone(report["outgoing_platform_message_id"])
            self.assertFalse(report["model_called"])
            self.assertFalse(report["production_memory_written"])
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["expected_self"], SELF)
            self.assertEqual(saved["allowed_room"], ROOM)
            self.assertEqual(saved["unrelated_setting"], {"keep": "unchanged"})
            binding = json.loads(report_path.with_suffix(".binding.local.json").read_text(encoding="utf-8"))
            self.assertEqual(binding["room_id"], ROOM)
            self.assertEqual(binding["incoming_native_message_id"], "9007199254740999")
            public = json.dumps(report, ensure_ascii=False)
            for value in (SELF, ROOM, "wxid_synthetic_member", transport.sent[0][1]):
                self.assertNotIn(value, public)

    def test_existing_account_binding_is_never_overwritten(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory, config_value(expected_self="wxid_other"))
            before = config_path.read_text(encoding="utf-8")
            transport = FakeTransport()
            report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(report["error_code"], "account_binding_changed")
            self.assertEqual(transport.calls, ["preflight", "close"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), before)

    def test_config_edited_during_trial_is_preserved_and_prevents_send(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            def edited():
                current = json.loads(config_path.read_text(encoding="utf-8"))
                current["unrelated_setting"] = "external edit"
                config_path.write_text(json.dumps(current), encoding="utf-8")
                return command_message()
            transport = FakeTransport([edited])
            report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(report["error_code"], "config_changed_during_trial")
            self.assertNotIn("send", transport.calls)
            self.assertEqual(transport.remove_calls, 1)
            self.assertEqual(json.loads(config_path.read_text())["unrelated_setting"], "external edit")

    def test_native_poll_timeout_does_not_remove_or_retry(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport([runner.TransportError("native_timeout"), command_message()])
            report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(transport.calls, ["preflight", "start", "poll", "close"])
            self.assertEqual(transport.remove_calls, 0)
            self.assertTrue(report["native_in_flight"])
            self.assertEqual(report["cleanup"]["state"], "UNKNOWN")

    def test_unclassified_exception_during_native_send_is_pending_without_remove(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport()
            def interrupted():
                raise KeyboardInterrupt()
            transport.send_hook = interrupted
            report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(report["probe"]["state"], "UNKNOWN")
            self.assertTrue(transport.pending)
            self.assertEqual(transport.remove_calls, 0)
            self.assertEqual(len(transport.sent), 1)

    def test_unverified_completion_stops_without_second_send(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport([command_message(), command_message(2)])
            transport.completion_success = False
            report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(report["probe"]["state"], "UNKNOWN")
            self.assertEqual(transport.calls.count("send"), 1)
            self.assertEqual(transport.calls.count("poll"), 1)
            self.assertEqual(transport.remove_calls, 1)

    def test_dispatch_report_failure_prevents_send_but_still_closes(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport()
            original = runner.atomic_json
            def failed(path, value):
                if path == report_path and value.get("stage") == "dispatching":
                    raise OSError("synthetic write failure")
                original(path, value)
            with patch.object(runner, "atomic_json", side_effect=failed):
                report = self.run_trial(transport, config, config_path, report_path)
            self.assertNotIn("send", transport.calls)
            self.assertEqual(transport.calls.count("close"), 1)
            self.assertEqual(transport.remove_calls, 1)
            self.assertTrue(report["dispatch_attempted"])

    def test_first_report_write_failure_still_closes_without_start(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport()
            original = runner.atomic_json
            def failed(path, value):
                if path == report_path and value.get("stage") == "preflight":
                    raise OSError("synthetic initial write failure")
                original(path, value)
            with patch.object(runner, "atomic_json", side_effect=failed):
                self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(transport.calls, ["close"])

    def test_final_report_write_failure_cannot_skip_cleanup(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport()
            original = runner.atomic_json
            def failed(path, value):
                if path == report_path and value.get("stage") in ("stopping", "finished"):
                    raise OSError("synthetic final write failure")
                original(path, value)
            with patch.object(runner, "atomic_json", side_effect=failed):
                report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(transport.remove_calls, 1)
            self.assertTrue(report["report_write_failed"])
            self.assertEqual(report["cleanup"]["state"], "CLEAN")

    def test_stop_requested_before_start_never_loads(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport()
            stop = threading.Event()
            stop.set()
            report = self.run_trial(transport, config, config_path, report_path, stop=stop)
            self.assertEqual(transport.calls, ["preflight", "close"])
            self.assertEqual(report["probe"]["state"], "STOPPED")

    def test_stop_during_poll_prevents_dispatch_and_cleans_normally(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            stop = threading.Event()
            def stopped():
                stop.set()
                return command_message()
            transport = FakeTransport([stopped])
            report = self.run_trial(transport, config, config_path, report_path, stop=stop)
            self.assertNotIn("send", transport.calls)
            self.assertEqual(transport.remove_calls, 1)
            self.assertEqual(report["probe"]["state"], "STOPPED")

    def test_cleanup_exception_is_reported_without_vendor_message(self):
        with test_directory() as directory:
            config, config_path, report_path = self.prepare(directory)
            transport = FakeTransport()
            transport.close_error = True
            report = self.run_trial(transport, config, config_path, report_path)
            self.assertEqual(report["cleanup"]["state"], "UNKNOWN")
            self.assertTrue(report["cleanup"]["manual_recovery_required"])
            self.assertNotIn("private vendor", json.dumps(report))


class NativeRunnerEntryTests(unittest.TestCase):
    def test_check_calls_only_readonly_preflight(self):
        transport = FakeTransport()
        with patch.object(runner, "load_config", return_value=config_value()), \
                patch.object(runner, "NativeTransport", return_value=transport), \
                patch.object(runner, "single_probe", side_effect=AssertionError("no run lock for check")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--mode", "check"]), 0)
        self.assertEqual(transport.calls, ["preflight"])

    def test_used_nonce_never_starts_transport_or_overwrites_report(self):
        with test_directory() as directory:
            data = directory / "data"
            path = data / "probes" / (NONCE + ".json")
            path.parent.mkdir(parents=True)
            original = '{"stage":"finished","dispatch_attempted":true}'
            path.write_text(original, encoding="utf-8")
            transport = FakeTransport()
            with patch.object(runner, "DATA", data), \
                    patch.object(runner, "load_config", return_value=config_value()), \
                    patch.object(runner, "NativeTransport", return_value=transport), \
                    patch.object(runner, "single_probe", return_value=contextlib.nullcontext()), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                result = runner.main(["--mode", "run", "--allow-native-send", "--nonce", NONCE])
            self.assertEqual(result, 2)
            self.assertIn("nonce_already_used", output.getvalue())
            self.assertEqual(transport.calls, [])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_signal_handler_sets_stop_and_allows_inflight_native_send_to_return(self):
        with test_directory() as directory:
            config = config_value()
            config_path = directory / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            transport = FakeTransport()
            handlers = {}
            def register(signum, handler):
                previous = handlers.get(signum, signal.SIG_DFL)
                handlers[signum] = handler
                return previous
            def stop_while_sending():
                handler = handlers[signal.SIGINT]
                self.assertTrue(callable(handler))
                handler(signal.SIGINT, None)  # Must not raise into the native call.
            transport.send_hook = stop_while_sending
            original_run = runner.run_trial
            original_load = runner.load_config
            def trial(*args, **kwargs):
                clock = Clock()
                return original_run(*args, **kwargs, config_path=config_path,
                                    clock=clock, sleep=clock.sleep, emit=lambda *a, **kw: None)
            with patch.object(runner, "DATA", directory / "data"), \
                    patch.object(runner, "load_config", side_effect=lambda path=config_path: original_load(path)), \
                    patch.object(runner, "NativeTransport", return_value=transport), \
                    patch.object(runner, "single_probe", return_value=contextlib.nullcontext()), \
                    patch.object(runner.signal, "signal", side_effect=register), \
                    patch.object(runner.logging, "disable"), \
                    patch.object(runner, "run_trial", side_effect=trial), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = runner.main(["--mode", "run", "--allow-native-send", "--nonce", NONCE])
            self.assertEqual(result, 0)
            self.assertEqual(transport.calls.count("send"), 1)
            self.assertEqual(transport.remove_calls, 1)
            self.assertFalse(transport.pending)
            self.assertEqual(handlers[signal.SIGINT], signal.SIG_DFL)


class AtomicJsonTests(unittest.TestCase):
    def test_transient_permission_error_retries_and_final_json_is_valid(self):
        with test_directory() as directory:
            path = directory / "report.json"
            path.write_text('{"previous":true}', encoding="utf-8")
            payload = {"stage": "waiting_for_command", "message": "合成测试", "counts": [0, 1]}
            original_replace = runner.os.replace
            attempts = []
            def replace(source, destination):
                attempts.append((source, destination))
                if len(attempts) == 1:
                    raise PermissionError("synthetic Windows sharing violation")
                return original_replace(source, destination)
            with patch.object(runner.os, "replace", side_effect=replace), \
                    patch.object(runner.time, "sleep") as sleep:
                runner.atomic_json(path, payload)
            self.assertEqual(len(attempts), 2)
            sleep.assert_called_once_with(.05)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_persistent_permission_error_has_five_attempt_limit_and_preserves_previous_json(self):
        with test_directory() as directory:
            path = directory / "report.json"
            previous = {"stage": "prior_checkpoint", "dispatch_attempted": False}
            path.write_text(json.dumps(previous), encoding="utf-8")
            with patch.object(runner.os, "replace", side_effect=PermissionError("synthetic locked report")) as replace, \
                    patch.object(runner.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    runner.atomic_json(path, {"stage": "new_checkpoint"})
            self.assertEqual(replace.call_count, 5)
            self.assertEqual(sleep.call_count, 4)
            self.assertTrue(all(call.args == (.05,) for call in sleep.call_args_list))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), previous)

    def test_other_os_error_is_not_retried(self):
        with test_directory() as directory:
            path = directory / "report.json"
            path.write_text('{"previous":true}', encoding="utf-8")
            failure = OSError("synthetic non-sharing I/O failure")
            with patch.object(runner.os, "replace", side_effect=failure) as replace, \
                    patch.object(runner.time, "sleep") as sleep:
                with self.assertRaises(OSError) as raised:
                    runner.atomic_json(path, {"new": True})
            self.assertIs(raised.exception, failure)
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"previous": True})


if __name__ == "__main__":
    unittest.main()
