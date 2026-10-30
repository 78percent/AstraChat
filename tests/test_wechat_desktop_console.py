"""Offline lifetime-console tests: no background processes or live stop files."""
from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import wechat_desktop_console as console

SESSION = "1234567890abcdef1234567890abcdef"
OTHER_SESSION = "fedcba0987654321fedcba0987654321"
REAL_SPAWN = console.spawn_session
REAL_REQUEST_STOP = console.request_stop


class DesktopConsoleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(console.startup, "DATA", self.directory))
        self.stack.enter_context(patch.object(console.startup, "START_STATUS", self.directory / "desktop-start-status.json"))
        self.stack.enter_context(patch.object(console, "ctypes", Mock()))
        self.stack.enter_context(patch.object(console.uuid, "uuid4", return_value=SimpleNamespace(hex=SESSION)))
        self.process = Mock()
        self.process.poll.return_value = None
        self.spawn = self.stack.enter_context(patch.object(console, "spawn_session", return_value=self.process))
        self.request_stop = self.stack.enter_context(patch.object(console, "request_stop"))
        self.keyboard = self.stack.enter_context(patch.object(console, "keyboard_stop_requested", return_value=False))
        self.stack.enter_context(patch.object(console.subprocess, "Popen", side_effect=AssertionError("Unexpected real subprocess")))
        self.stack.enter_context(patch.object(console, "read_json", side_effect=AssertionError("Unexpected filesystem status read")))
        self.elapsed = 0.0
        self.stack.enter_context(patch.object(console.time, "monotonic", side_effect=lambda: self.elapsed))
        self.stack.enter_context(patch.object(console.time, "sleep", side_effect=self.sleep))
        self.output = io.StringIO()
        self.stack.enter_context(patch("sys.stdout", self.output))

    def sleep(self, seconds):
        self.elapsed += seconds

    def result_then_stop(self):
        count = 0
        def read(path):
            nonlocal count
            if Path(path).name.endswith(".result.json"):
                count += 1
                return {} if count == 1 else {"session_id": SESSION, "stage": "stopped"}
            return {"session_id": SESSION, "stage": "ready", "message": "ready-current"}
        return read

    def test_supervisor_has_no_console_and_owns_actual_python_pid(self):
        with patch.object(console.os, "getpid", return_value=4242), \
                patch.object(console.subprocess, "Popen") as popen:
            REAL_SPAWN(SESSION)
        args, kwargs = popen.call_args
        command = args[0]
        self.assertEqual(command[command.index("--owner-pid") + 1], "4242")
        self.assertEqual(command[command.index("--session-id") + 1], SESSION)
        self.assertEqual(command[0], str(console.startup.PYTHON.with_name("pythonw.exe")))
        self.assertEqual(Path(command[3]).name, "wechat_desktop_session.py")
        self.assertFalse(kwargs["creationflags"] & getattr(console.subprocess, "DETACHED_PROCESS", 8))
        if hasattr(console.subprocess, "CREATE_NO_WINDOW"):
            self.assertTrue(kwargs["creationflags"] & console.subprocess.CREATE_NO_WINDOW)
        self.assertTrue(kwargs["creationflags"] & getattr(console.subprocess, "CREATE_NEW_PROCESS_GROUP", 512))
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["stdin"], console.subprocess.DEVNULL)
        self.assertEqual(Path(kwargs["stdout"].name).name, "desktop-supervisor-" + SESSION + ".log")
        self.assertIs(kwargs["stdout"], kwargs["stderr"])

    def test_stop_only_uses_same_hidden_supervisor(self):
        with patch.object(console.subprocess, "Popen") as popen:
            REAL_SPAWN(SESSION, stop_only=True)
        self.assertIn("--stop-only", popen.call_args.args[0])

    def test_busy_second_window_does_not_request_stop(self):
        with patch.object(console, "read_json", return_value={"session_id": SESSION, "stage": "busy"}):
            self.assertEqual(console.run_console(), 0)
        self.request_stop.assert_not_called()
        self.keyboard.assert_not_called()

    def test_enter_requests_stop_for_own_session_and_waits_for_result(self):
        self.keyboard.return_value = True
        with patch.object(console, "read_json", side_effect=self.result_then_stop()):
            self.assertEqual(console.run_console(), 0)
        self.request_stop.assert_called_once_with(SESSION)
        self.assertIn("已停止", self.output.getvalue())

    def test_ctrl_c_requests_stop_for_own_session_and_waits_for_result(self):
        self.keyboard.side_effect = KeyboardInterrupt
        with patch.object(console, "read_json", side_effect=self.result_then_stop()):
            self.assertEqual(console.run_console(), 0)
        self.request_stop.assert_called_once_with(SESSION)
        self.assertIn("已停止", self.output.getvalue())

    def test_global_status_from_another_session_is_not_displayed_or_joined(self):
        def read(path):
            if Path(path).name.endswith(".result.json"):
                return {}
            return {"session_id": OTHER_SESSION, "stage": "ready", "message": "foreign-session-message"}
        def advance(_):
            self.elapsed += 11
        with patch.object(console, "read_json", side_effect=read), patch.object(console.time, "sleep", side_effect=advance):
            self.assertEqual(console.run_console(), 1)
        self.request_stop.assert_called_once_with(SESSION)
        self.assertNotIn("foreign-session-message", self.output.getvalue())
        self.assertIn("未能连接控制进程", self.output.getvalue())

    def test_repeated_status_message_is_not_reprinted(self):
        count = 0
        def read(path):
            nonlocal count
            if Path(path).name.endswith(".result.json"):
                count += 1
                return {} if count < 4 else {"session_id": SESSION, "stage": "stopped"}
            return {"session_id": SESSION, "stage": "ready", "message": "one-progress-line"}
        with patch.object(console, "read_json", side_effect=read):
            self.assertEqual(console.run_console(), 0)
        self.assertEqual(self.output.getvalue().count("one-progress-line"), 1)
        self.request_stop.assert_not_called()

    def test_result_write_exit_boundary_is_rechecked_before_failure(self):
        self.process.poll.return_value = 0
        reads = iter(({}, {"session_id": SESSION, "stage": "starting"},
                      {"session_id": SESSION, "stage": "stopped"},
                      {"session_id": SESSION, "stage": "stopped"}))
        with patch.object(console, "read_json", side_effect=lambda _: next(reads)):
            self.assertEqual(console.run_console(), 0)
        self.assertNotIn("异常退出", self.output.getvalue())

    def test_supervisor_exit_without_result_is_not_reported_stopped(self):
        self.process.poll.return_value = 1
        with patch.object(console, "read_json", return_value={}):
            self.assertEqual(console.run_console(), 1)
        self.assertIn("不能确认服务已停止", self.output.getvalue())
        self.assertNotIn("微信监听、机器人服务和 Docker Desktop 已停止", self.output.getvalue())

    def test_startup_failure_remains_a_failure_even_after_clean_shutdown(self):
        result = {"session_id": SESSION, "stage": "stopped", "startup_error": "astrbot_not_ready"}
        with patch.object(console, "read_json", return_value=result):
            self.assertEqual(console.run_console(), 1)
        self.assertIn("启动未完成", self.output.getvalue())

    def test_stop_request_filename_is_scoped_to_valid_session(self):
        REAL_REQUEST_STOP(SESSION)
        files = list(self.directory.iterdir())
        self.assertEqual([path.name for path in files], ["desktop-stop-" + SESSION])
        self.assertEqual(files[0].read_text(encoding="ascii"), "stop\n")
        for invalid in ("../outside", "not-a-session", "12345678-90ab-cdef-1234-567890abcdef"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                REAL_REQUEST_STOP(invalid)
        self.assertEqual(len(list(self.directory.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
