"""Offline fake-native lifecycle contracts. Never import upstream or touch WeChat."""
from __future__ import annotations

import importlib
import importlib.util
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.wechat_native_transport import NativeTransport, TransportError, verify_upstream, _check_module_cache, _git


class HiddenGitTests(unittest.TestCase):
    def test_fingerprint_git_queries_cannot_open_console_from_pythonw(self):
        import subprocess
        with patch("scripts.wechat_native_transport.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=b"ok")) as run:
            self.assertEqual(_git(Path("helper"), "rev-parse", "HEAD"), b"ok")
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))


class FakeChannel:
    def __init__(self):
        self.pending = False
        self.closed = False
        self.result = None
        self.error = None

    def poll(self):
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


class Fixture:
    def __init__(self):
        self.client = NativeTransport(ROOT / "work/fake-native-source")
        self.module_map = {"weixin.dll": (100, "fake/core.dll")}
        self.calls = []
        self.channel = FakeChannel()
        self.guard = SimpleNamespace(close=Mock(), start_watchdog=Mock())
        dll = self.client.upstream / "native/build_text/text_bridge.dll"
        self.listen_result = {"pid": 31, "code": 0}
        self.send_result = {"pid": 31, "code": 0, "cleanup_code": 0, "success": True,
                            "report": {"completion_success": True}}
        self.remove_result = {"pid": 31, "code": 0, "dll_unloaded": True}
        self.throws = {}
        self.leave_loaded = False
        def run(mode, **kwargs):
            self.calls.append((mode, kwargs))
            if mode == "--listen":
                # Native code might load before reporting an error.
                self.module_map["text_bridge.dll"] = (200, str(dll))
            if mode in self.throws:
                raise self.throws[mode]
            if mode == "--listen":
                return self.listen_result
            if mode == "--dispatch":
                return self.send_result
            if mode == "--remove":
                if not self.leave_loaded:
                    self.module_map.pop("text_bridge.dll", None)
                return self.remove_result
            raise AssertionError("unexpected native mode")
        self.api = SimpleNamespace(
            native=SimpleNamespace(TEXT_DLL=dll, run=run,
                                   modules=lambda pid: dict(self.module_map),
                                   wechat_candidates=Mock(return_value=[31])),
            receive=SimpleNamespace(ReceiveChannel=Mock(return_value=self.channel),
                                    ReceivedMediaMessageV5=object),
            lifetime=SimpleNamespace(BridgeGuard=Mock(return_value=self.guard)),
        )
        self.client._api = self.api
        self.client._load_api = Mock()
        self.client._verify_core = Mock(return_value=100)
        self.client._read_account = Mock(return_value="wxid_fixture")

    def start(self):
        self.client.preflight()
        return self.client.start()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.pin = patch("scripts.wechat_native_transport.verify_upstream",
                         return_value={"source_clean": True})
        self.cache = patch("scripts.wechat_native_transport._check_module_cache")
        self.pin.start()
        self.cache.start()
        self.addCleanup(self.pin.stop)
        self.addCleanup(self.cache.stop)
        self.f = Fixture()

    def test_preflight_is_read_only_and_redacted(self):
        result = self.f.client.preflight()
        self.assertTrue(result["ready"])
        self.assertEqual(self.f.calls, [])
        self.assertNotIn("wxid_fixture", str(result))
        self.assertNotIn("31", str(result))
        self.assertEqual(self.f.client.self_id, "wxid_fixture")

    def test_multiple_processes_refused_before_account_read(self):
        self.f.api.native.wechat_candidates.return_value = [31, 32]
        with self.assertRaisesRegex(TransportError, "not_unique"):
            self.f.client.preflight()
        self.f.client._read_account.assert_not_called()

    def test_existing_bridge_is_not_removed(self):
        self.f.module_map["text_bridge.dll"] = (200, "foreign.dll")
        with self.assertRaisesRegex(TransportError, "existing_bridge"):
            self.f.client.preflight()
        self.assertEqual(self.f.calls, [])

    def test_expected_account_mismatch_is_redacted(self):
        self.f.client.expected_self = "other-account"
        with self.assertRaisesRegex(TransportError, "expected_account_mismatch"):
            self.f.client.preflight()
        self.assertEqual(self.f.calls, [])

    def test_start_uses_raw_v5_without_watchdog(self):
        result = self.f.start()
        self.assertTrue(result["started"])
        self.f.guard.start_watchdog.assert_not_called()
        self.assertEqual(self.f.calls[0][0], "--listen")
        self.assertFalse(self.f.calls[0][1]["preserve_report"])
        self.assertEqual(self.f.api.receive.ReceiveChannel.call_args.kwargs["poll_export"], b"WxPollMediaMessageV5")

    def test_explicit_voice_profile_uses_v6_and_only_bound_private_target(self):
        from scripts.wechat_native_transport import VOICE_COMMIT
        self.f.client.commit = VOICE_COMMIT
        self.f.client.private_peer = "wxid_peer"
        self.f.api.receive.ReceivedMediaMessageV6 = object
        self.f.start()
        self.assertEqual(self.f.api.receive.ReceiveChannel.call_args.kwargs["poll_export"], b"WxPollMediaMessageV6")
        self.f.client.send("wxid_peer", "测试")
        with self.assertRaises(TransportError):self.f.client.send("wxid_other", "测试")
        with self.assertRaises(TransportError):self.f.client.send(None, "测试")
        self.assertEqual(self.f.client.close()["state"], "CLEAN")

    def test_normal_poll_is_raw_without_business_mapping(self):
        self.f.start()
        self.f.channel.result = SimpleNamespace(type=37, private_value="not-to-log")
        self.assertIs(self.f.client.poll(), self.f.channel.result)
        self.assertEqual([mode for mode, _ in self.f.calls], ["--listen"])

    def test_poll_uses_stat_runtime_check_not_full_git_hash(self):
        self.f.start()
        self.f.client._check_runtime = Mock()
        self.f.client.poll()
        self.f.client._check_runtime.assert_called_once_with(require_loaded=True)

    def test_account_change_refuses_poll_before_remote(self):
        self.f.start()
        self.f.client._read_account.return_value = "other"
        self.f.channel.poll = Mock()
        with self.assertRaisesRegex(TransportError, "account_changed"):
            self.f.client.poll()
        self.f.channel.poll.assert_not_called()

    def test_start_timeout_retains_module_and_guard(self):
        self.f.throws["--listen"] = TimeoutError("sensitive native details")
        with self.assertRaisesRegex(TransportError, "execution_unknown"):
            self.f.start()
        self.assertTrue(self.f.client.loaded)
        self.assertTrue(self.f.client.pending)
        self.assertEqual(self.f.client.close()["state"], "UNKNOWN")
        self.f.guard.close.assert_not_called()
        self.assertEqual([mode for mode, _ in self.f.calls], ["--listen"])

    def test_start_rejection_known_return_can_be_cleaned(self):
        self.f.listen_result = {"pid": 31, "code": 5}
        with self.assertRaisesRegex(TransportError, "start_rejected"):
            self.f.start()
        self.assertFalse(self.f.client.pending)
        self.assertEqual(self.f.client.close()["state"], "CLEAN")

    def test_start_unknown_pid_retains_module_and_guard(self):
        for pid in (None, True, 31.0, 32):
            with self.subTest(pid=pid):
                fixture = Fixture()
                if pid is None:
                    fixture.listen_result.pop("pid")
                else:
                    fixture.listen_result["pid"] = pid
                with self.assertRaisesRegex(TransportError, "start_result_unknown"):
                    fixture.start()
                self.assertTrue(fixture.client.pending)
                self.assertEqual(fixture.client.close()["state"], "UNKNOWN")
                fixture.guard.close.assert_not_called()
                fixture.api.receive.ReceiveChannel.assert_not_called()
                self.assertEqual([mode for mode, _ in fixture.calls], ["--listen"])

    def test_poll_timeout_never_frees_channel_buffer_or_unloads(self):
        self.f.start()
        self.f.channel.error = TimeoutError()
        with self.assertRaises(TransportError):
            self.f.client.poll()
        outcome = self.f.client.close()
        self.assertEqual(outcome["state"], "UNKNOWN")
        self.assertFalse(self.f.channel.closed)
        self.assertEqual(len(self.f.calls), 1)

    def test_poll_keyboard_interrupt_is_also_unknown(self):
        self.f.start()
        self.f.channel.error = KeyboardInterrupt()
        with self.assertRaises(TransportError):
            self.f.client.poll()
        self.assertTrue(self.f.client.pending)
        self.assertEqual(self.f.client.close()["state"], "UNKNOWN")

    def test_send_checks_runtime_and_only_text_options(self):
        self.f.start()
        result = self.f.client.send("room@chatroom", "neutral")
        self.assertIs(result, self.f.send_result)
        kwargs = self.f.calls[-1][1]
        self.assertTrue(kwargs["keep_loaded"])
        self.assertFalse(kwargs["preserve_report"])
        self.assertNotIn("mentions", kwargs)
        self.assertNotIn("account", kwargs)

    def test_invalid_send_target_rejected_without_native_call(self):
        self.f.start()
        for target in ("friend", "群@chatroom", "room @chatroom", "@chatroom", "bad@room@chatroom", None):
            with self.assertRaises(TransportError):
                self.f.client.send(target, "neutral")
        self.assertEqual(len(self.f.calls), 1)

    def test_send_exception_disallows_poll_send_and_cleanup(self):
        self.f.start()
        self.f.throws["--dispatch"] = OSError("opaque native state")
        with self.assertRaisesRegex(TransportError, "execution_unknown"):
            self.f.client.send("room@chatroom", "neutral")
        with self.assertRaises(TransportError):
            self.f.client.poll()
        with self.assertRaises(TransportError):
            self.f.client.send("room@chatroom", "neutral")
        self.assertEqual(self.f.client.close()["state"], "UNKNOWN")
        self.assertEqual([mode for mode, _ in self.f.calls], ["--listen", "--dispatch"])

    def test_send_incomplete_native_cleanup_retains_module(self):
        self.f.start()
        self.f.send_result["cleanup_code"] = 170
        with self.assertRaisesRegex(TransportError, "completion_unknown"):
            self.f.client.send("room@chatroom", "neutral")
        self.assertTrue(self.f.client.pending)
        self.assertEqual(self.f.client.close()["state"], "UNKNOWN")

    def test_confirmed_send_failure_is_not_mislabeled_success(self):
        self.f.start()
        self.f.send_result["success"] = False
        self.f.send_result["report"]["completion_success"] = False
        self.assertFalse(self.f.client.send("room@chatroom", "neutral")["success"])
        self.assertEqual(self.f.client.close()["state"], "CLEAN")

    def test_send_unknown_pid_retains_channel_module_and_guard(self):
        for pid in (None, True, 31.0, 32):
            with self.subTest(pid=pid):
                fixture = Fixture()
                fixture.start()
                if pid is None:
                    fixture.send_result.pop("pid")
                else:
                    fixture.send_result["pid"] = pid
                with self.assertRaisesRegex(TransportError, "send_completion_unknown"):
                    fixture.client.send("room@chatroom", "neutral")
                self.assertTrue(fixture.client.pending)
                self.assertEqual(fixture.client.close()["state"], "UNKNOWN")
                fixture.guard.close.assert_not_called()
                self.assertFalse(fixture.channel.closed)
                self.assertEqual([mode for mode, _ in fixture.calls], ["--listen", "--dispatch"])

    def test_normal_close_removes_once_and_checks_absence(self):
        self.f.start()
        result = self.f.client.close()
        self.assertEqual(result["state"], "CLEAN")
        self.assertTrue(result["dll_unloaded"])
        self.assertTrue(self.f.channel.closed)
        self.f.guard.close.assert_called_once()
        self.assertEqual(self.f.client.close(), result)
        self.assertEqual([mode for mode, _ in self.f.calls], ["--listen", "--remove"])

    def test_remove_return_true_without_module_absence_is_unknown(self):
        self.f.start()
        self.f.leave_loaded = True
        result = self.f.client.close()
        self.assertEqual(result["state"], "UNKNOWN")
        self.f.guard.close.assert_not_called()

    def test_remove_timeout_is_not_retried(self):
        self.f.start()
        self.f.throws["--remove"] = TimeoutError()
        self.assertEqual(self.f.client.close()["state"], "UNKNOWN")
        self.assertEqual(self.f.client.close()["state"], "UNKNOWN")
        self.assertEqual(sum(mode == "--remove" for mode, _ in self.f.calls), 1)

    def test_remove_unknown_pid_is_not_confirmed_or_retried(self):
        for pid in (None, True, 31.0, 32):
            with self.subTest(pid=pid):
                fixture = Fixture()
                fixture.start()
                if pid is None:
                    fixture.remove_result.pop("pid")
                else:
                    fixture.remove_result["pid"] = pid
                result = fixture.client.close()
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertFalse(result["dll_unloaded"])
                self.assertTrue(fixture.client.pending)
                self.assertEqual(fixture.client.close(), result)
                fixture.guard.close.assert_not_called()
                self.assertEqual(sum(mode == "--remove" for mode, _ in fixture.calls), 1)

    def test_pin_failure_before_send_does_not_call_native(self):
        self.f.start()
        self.f.client._check_runtime = Mock(side_effect=TransportError("source_changed"))
        with self.assertRaisesRegex(TransportError, "source_changed"):
            self.f.client.send("room@chatroom", "neutral")
        self.assertEqual(len(self.f.calls), 1)

    def test_close_before_start_is_clean_without_native_call(self):
        self.assertEqual(self.f.client.close()["state"], "CLEAN")
        self.assertEqual(self.f.calls, [])

    def test_channel_pending_prevents_even_runtime_recheck_on_close(self):
        self.f.start()
        self.f.channel.pending = True
        self.f.client._check_runtime = Mock(side_effect=AssertionError("must not inspect again"))
        self.assertEqual(self.f.client.close()["state"], "UNKNOWN")
        self.f.client._check_runtime.assert_not_called()
        self.assertFalse(self.f.channel.closed)

    def test_failed_channel_initialization_can_remove_known_loaded_module(self):
        self.f.api.receive.ReceiveChannel.side_effect = OSError("allocation failed")
        with self.assertRaisesRegex(TransportError, "setup_failed"):
            self.f.start()
        self.assertFalse(self.f.client.pending)
        self.assertEqual(self.f.client.close()["state"], "CLEAN")


class ImportSafetyTests(unittest.TestCase):
    def test_importing_transport_does_not_import_native_modules(self):
        with patch("importlib.import_module", side_effect=AssertionError("must remain lazy")):
            spec = importlib.util.spec_from_file_location("isolated_transport", ROOT / "scripts/wechat_native_transport.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.NativeTransport(ROOT / "work/fake-native-source")

    def test_cache_from_another_root_is_rejected(self):
        with patch.dict(sys.modules, {"run_selftest": SimpleNamespace(__file__=str(ROOT / "elsewhere/run_selftest.py"))}):
            with self.assertRaisesRegex(TransportError, "cache_conflict"):
                _check_module_cache(ROOT / "work/fake-native-source")


class PinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="transport-pin-", dir=ROOT / "work")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.library = self.root / "native/build_text/text_bridge.dll"
        self.library.parent.mkdir(parents=True)
        self.library.write_bytes(b"offline-fixture")
        self.dirty = b""
        self.untracked = b""
        self.commit = "a81c495770b397646ba0661118927c83589c41fc"
        def git(root, *args):
            if args == ("rev-parse", "HEAD"):
                return self.commit.encode()
            if args == ("rev-parse", "--show-toplevel"):
                return str(self.root).encode()
            if args[0] == "diff":
                return self.dirty
            if args[0] == "ls-files":
                return self.untracked
            raise AssertionError(args)
        self.git_reply = git
        self.mock_git = patch("scripts.wechat_native_transport._git", side_effect=git)
        self.mock_pins = patch("scripts.wechat_native_transport.DLLS", {
            "native/build_text/text_bridge.dll": (15, hashlib.sha256(b"offline-fixture").hexdigest())})
        self.mock_git.start()
        self.mock_pins.start()
        self.addCleanup(self.mock_git.stop)
        self.addCleanup(self.mock_pins.stop)

    def test_matching_fixture_pin_passes(self):
        self.assertTrue(verify_upstream(self.root)["bridge_fingerprints_match"])

    def test_independent_git_queries_overlap_and_keep_their_results(self):
        barrier = threading.Barrier(4, timeout=5)
        calls = []
        guard = threading.Lock()

        def concurrent_git(root, *args):
            with guard:
                calls.append((threading.get_ident(), args))
            barrier.wait()
            return self.git_reply(root, *args)

        with patch("scripts.wechat_native_transport._git", side_effect=concurrent_git):
            result = verify_upstream(self.root)
        self.assertEqual(result, {"upstream_commit": self.commit, "source_clean": True,
                                  "bridge_fingerprints_match": True})
        self.assertEqual(len({thread for thread, _ in calls}), 4)
        self.assertNotIn(threading.get_ident(), {thread for thread, _ in calls})
        self.assertCountEqual([args for _, args in calls], [
            ("rev-parse", "HEAD"), ("rev-parse", "--show-toplevel"),
            ("diff", "--name-only", "HEAD", "--"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ])

    def test_later_query_failure_does_not_override_earlier_commit_mismatch(self):
        later_finished = threading.Event()

        def out_of_order_git(root, *args):
            if args == ("rev-parse", "HEAD"):
                self.assertTrue(later_finished.wait(5))
                return b"wrong-commit"
            if args[0] == "ls-files":
                later_finished.set()
            raise TransportError("upstream_git_check_failed")

        with patch("scripts.wechat_native_transport._git", side_effect=out_of_order_git):
            with self.assertRaisesRegex(TransportError, "^upstream_commit_mismatch$"):
                verify_upstream(self.root)

    def test_root_mismatch_still_precedes_dirty_and_untracked_results(self):
        self.dirty = b"native/run_selftest.py\n"
        self.untracked = b"native/extra.py\0"

        def changed_root(root, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return str(self.root.parent).encode()
            return self.git_reply(root, *args)

        with patch("scripts.wechat_native_transport._git", side_effect=changed_root):
            with self.assertRaisesRegex(TransportError, "^upstream_root_mismatch$"):
                verify_upstream(self.root)

    def test_each_git_query_failure_still_refuses_the_source(self):
        for failing in ("commit", "root", "tracked", "untracked"):
            with self.subTest(query=failing):
                def git_failure(root, *args):
                    kind = ("commit" if args == ("rev-parse", "HEAD") else
                            "root" if args == ("rev-parse", "--show-toplevel") else
                            "tracked" if args[0] == "diff" else "untracked")
                    if kind == failing:
                        raise TransportError("upstream_git_check_failed")
                    return self.git_reply(root, *args)

                with patch("scripts.wechat_native_transport._git", side_effect=git_failure):
                    with self.assertRaisesRegex(TransportError, "^upstream_git_check_failed$"):
                        verify_upstream(self.root)

    def test_commit_mismatch_stops_before_import(self):
        self.commit = "different"
        with self.assertRaisesRegex(TransportError, "commit_mismatch"):
            verify_upstream(self.root)

    def test_tracked_source_change_is_refused(self):
        self.dirty = b"native/run_selftest.py\n"
        with self.assertRaisesRegex(TransportError, "tracked_changes"):
            verify_upstream(self.root)

    def test_only_runtime_and_pycache_untracked_are_accepted(self):
        self.untracked = b".runtime/report.json\0native/__pycache__/cache.pyc\0"
        self.assertTrue(verify_upstream(self.root)["source_clean"])
        self.untracked = b"native/extra.py\0"
        with self.assertRaisesRegex(TransportError, "untracked_files"):
            verify_upstream(self.root)

    def test_same_size_changed_binary_is_refused(self):
        self.library.write_bytes(b"x" * 15)
        with self.assertRaisesRegex(TransportError, "fingerprint_mismatch"):
            verify_upstream(self.root)


if __name__ == "__main__":
    unittest.main()
