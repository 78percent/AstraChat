"""Fake-only runner checks: no sockets, upstream import, accounts or native calls."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.wechat_native_bridge import check, load_settings, main, recover, run_bridge, single_bridge
from scripts.wechat_native_bridge_core import BridgeError
from scripts.wechat_native_transport import TransportError
from tests.test_wechat_native_bridge_core import SESSION, incoming, settings

ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(self):
        self.self_id, self.pid, self.pending = "wxid_robot", 31, False
        self.calls, self.threads = [], []
        self.events = [incoming()]
        self.send_error = None
        self.bad_completion = False
        self.on_send = lambda *_: None
        self.poll_error = None

    def record(self, name):
        self.calls.append(name)
        self.threads.append(threading.get_ident())

    def preflight(self):
        self.record("preflight")
        return {"ready": True}

    def start(self):
        self.record("start")
        return {"started": True}

    def poll(self):
        self.record("poll")
        if self.poll_error:
            self.pending = True
            raise self.poll_error
        return self.events.pop(0) if self.events else None

    def send(self, room, text):
        self.record("send")
        self.on_send(room, text)
        if self.send_error:
            self.pending = True
            raise self.send_error
        return {"pid": 31, "code": 0, "cleanup_code": 0, "success": not self.bad_completion,
                "report": {"completion_success": True, "target": room, "text": text}}

    def close(self):
        self.record("close")
        return {"state": "UNKNOWN" if self.pending else "CLEAN", "dll_unloaded": not self.pending}


class FakeSocket:
    def __init__(self):
        self.sent, self.frames = [], []
        self.closed = False
        self.disconnect_on_event = False
        self.extra_hello = False
        self.send_reply = True
        self.on_event = lambda *_: None
        self.on_result = lambda *_: None

    def send(self, raw):
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["type"] == "hello":
            answer = {"type": "hello_ack", "version": 1, "session": SESSION, "client_session": frame["session"]}
            if self.extra_hello:
                answer["extra"] = True
            self.frames.append(answer)
        if frame["type"] == "event":
            self.on_event(frame)
            if self.disconnect_on_event:
                raise ConnectionError("private socket diagnostic must not escape")
            self.frames.append({"type": "event_ack", "session": SESSION, "id": frame["id"], "result": "accepted"})
            if self.send_reply:
                self.frames.append({"type": "send", "session": SESSION,
                                    "request_id": "bbbbbbbb222243338444555555555555", "reply_to": frame["id"],
                                    "room": frame["room"], "text": "合成回复", "expires_at": 108})
        if frame["type"] == "send_result":
            self.on_result(frame)

    def recv(self, timeout):
        if not self.frames:
            raise TimeoutError()
        return json.dumps(self.frames.pop(0), ensure_ascii=False)

    def close(self):
        self.closed = True


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-runner-", dir=ROOT / "work")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.transport, self.socket = FakeTransport(), FakeSocket()
        self.connect_options = None
        self.now = 100.0
        self.stop = threading.Event()

    def connect(self, url, **kwargs):
        self.connect_options = (url, kwargs)
        return self.socket

    def run_fake(self, **kwargs):
        options = dict(directory=self.directory, transport_factory=lambda _: self.transport,
                       connector=self.connect, stop=self.stop, wall=lambda: self.now,
                       monotonic=lambda: 1.0, sleep=lambda _: None, max_cycles=2)
        options.update(kwargs)
        # An accidental real NativeTransport construction/import is a test failure.
        with patch("scripts.wechat_native_bridge.default_transport", side_effect=AssertionError("native forbidden")):
            return run_bridge(settings(), **options)

    def test_happy_path_fixed_thread_real_intent_and_redacted_status(self):
        def inspect_intent(*_):
            with closing(sqlite3.connect(self.directory / "bridge-ledger.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT status FROM outbound").fetchone()[0], "in_flight")
        self.transport.on_send = inspect_intent
        result = self.run_fake()
        self.assertEqual(result["stage"], "stopped")
        self.assertEqual(result["counts"]["native_completed"], 1)
        self.assertEqual(result["ack_results"], {"ACCEPTED": 1})
        self.assertEqual(result["last_ack"], {"result": "ACCEPTED", "reason": "unspecified"})
        self.assertEqual(len(set(self.transport.threads)), 1)
        self.assertEqual(self.transport.calls.count("send"), 1)
        self.assertEqual(self.transport.calls[-1], "close")
        self.assertTrue(self.socket.closed)

    def test_scheduled_send_without_inbound_message_uses_separate_durable_ledger(self):
        self.transport.events = []
        original_send = self.socket.send
        def send(raw):
            original_send(raw)
            if json.loads(raw)['type'] == 'hello':
                self.socket.frames.append({'type':'scheduled_send','session':SESSION,'request_id':'c'*32,
                    'task_id':'d'*32,'room':settings().allowed_room,'text':'定时合成测试','expires_at':108.0})
        self.socket.send = send
        def inspect_intent(*_):
            with closing(sqlite3.connect(self.directory/'scheduled-ledger.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT status FROM scheduled').fetchone()[0], 'in_flight')
        self.transport.on_send = inspect_intent
        result = self.run_fake()
        self.assertEqual(result['stage'], 'stopped')
        self.assertEqual(self.transport.calls.count('send'), 1)
        self.assertEqual(len(set(self.transport.threads)), 1)
        self.assertEqual(next(f for f in self.socket.sent if f['type']=='send_result')['status'], 'native_completed')
        self.assertEqual(self.connect_options[1]["additional_headers"], {"Authorization": "Bearer " + "a" * 64})
        self.assertIsNone(self.connect_options[1]["proxy"])
        status = (self.directory / "bridge-status.json").read_text()
        for private in ("合成回复", "synthetic input", "wxid_robot", "wxid_member", "12345@chatroom", "a" * 64):
            self.assertNotIn(private, status)

    def test_disconnect_stops_and_normally_closes_without_send(self):
        self.socket.disconnect_on_event = True
        result = self.run_fake()
        self.assertEqual(result["stage"], "failed")
        self.assertFalse(result["manual_recovery_required"])
        self.assertNotIn("send", self.transport.calls)
        self.assertEqual(self.transport.calls[-1], "close")
        self.assertNotIn("private socket", json.dumps(result))

    def test_voice_preparation_keeps_polling_and_dispatches_once_on_owner_thread(self):
        from scripts.wechat_voice_bridge import VoiceBridge
        from scripts.wechat_voice_runtime import sha256
        from types import SimpleNamespace
        import uuid
        audio = self.directory / "fixture.silk"
        reference = self.directory / "reference.wav"
        audio.write_bytes(b"synthetic silk fixture")
        reference.write_bytes(b"synthetic reference fixture")
        polls = 0
        closed = []
        runtime = SimpleNamespace(settings={"reference_audio": reference},
                                  status=lambda: {"enabled": True, "asr": "ready", "tts": "ready"},
                                  reap=lambda: None, cancel=lambda job: None,
                                  close=lambda: closed.append(True))
        def synthesize(text):
            # No outbound intent may exist during preparation.
            with closing(sqlite3.connect(self.directory / "bridge-ledger.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM outbound").fetchone()[0], 0)
            return "a" * 32
        def result(job):
            nonlocal polls
            polls += 1
            return {"result": {}} if polls >= 3 else None
        runtime.synthesize, runtime.result = synthesize, result
        runtime.encoded = lambda job: {"path": audio, "duration_ms": 1000,
                                       "sha256": sha256(audio), "reference_sha256": sha256(reference)}
        def send_voice(room, path, duration):
            self.transport.record("send_voice")
            return {"state": "NATIVE_COMPLETED", "duration_ms": duration, "recipient_delivery_confirmed": False}
        self.transport.send_voice = send_voice
        original_send = self.socket.send
        self.socket.send_reply = False
        preparation_id = uuid.uuid4().hex
        def socket_send(raw):
            original_send(raw)
            value = json.loads(raw)
            if value["type"] == "event":
                self.socket.frames.append(dict(type="prepare_voice", session=SESSION, request_id=preparation_id,
                    reply_to=value["id"], room=value["room"], text="测试声音"))
            elif value["type"] == "voice_prepared":
                self.assertEqual(value["status"], "ready")
                self.socket.frames.append(dict(type="commit_voice", session=SESSION, request_id=uuid.uuid4().hex,
                    reply_to="33", room="12345@chatroom", text="测试声音", handle=preparation_id, expires_at=108))
        self.socket.send = socket_send
        result = self.run_fake(voice_factory=lambda core: VoiceBridge(core, runtime), max_cycles=6)
        self.assertEqual(result["stage"], "stopped")
        self.assertEqual(result["counts"]["native_completed"], 1)
        self.assertEqual(self.transport.calls.count("send_voice"), 1)
        self.assertGreaterEqual(self.transport.calls.count("poll"), 6)
        self.assertEqual(len(set(self.transport.threads)), 1)
        self.assertEqual(closed, [True])

    def test_unknown_send_stops_and_preserves_native_pending(self):
        self.transport.send_error = TimeoutError("private native diagnostic")
        result = self.run_fake()
        self.assertEqual(result["stage"], "unknown")
        self.assertTrue(result["manual_recovery_required"])
        self.assertEqual(self.transport.calls[-2:], ["send", "close"])
        self.assertEqual([f["status"] for f in self.socket.sent if f["type"] == "send_result"], ["unknown"])
        self.assertNotIn("private native", json.dumps(result))
        with self.assertRaisesRegex(BridgeError, "unresolved"):
            self.run_fake()

    def test_known_completed_but_unverified_result_stops_with_clean_close(self):
        self.transport.bad_completion = True
        result = self.run_fake()
        self.assertEqual(result["stage"], "failed")
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertFalse(result["cleanup_required"])

    def test_native_poll_unknown_never_continues_to_send(self):
        self.transport.poll_error = TimeoutError("native pending")
        result = self.run_fake()
        self.assertEqual(result["stage"], "unknown")
        self.assertEqual(self.transport.calls[-2:], ["poll", "close"])
        self.assertNotIn("send", self.transport.calls)

    def test_handshake_failure_never_loads_bridge(self):
        self.socket.extra_hello = True
        result = self.run_fake()
        self.assertEqual(result["stage"], "failed")
        self.assertNotIn("start", self.transport.calls)
        self.assertEqual(self.transport.calls, ["preflight", "close"])

    def test_preexisting_stop_sentinel_loads_nothing(self):
        (self.directory / "bridge.stop").touch()
        result = self.run_fake()
        self.assertEqual(result["stage"], "stopped")
        self.assertEqual(self.transport.calls, [])
        self.assertIsNone(self.connect_options)
        self.assertTrue((self.directory / "bridge.stop").exists())

    def test_expiry_is_rechecked_after_intent_before_native_call(self):
        from scripts.wechat_native_bridge_core import BridgeCore
        original = BridgeCore.prepare
        def prepare(core, frame, now):
            result = original(core, frame, now)
            self.now = 109.0
            return result
        with patch.object(BridgeCore, "prepare", prepare):
            result = self.run_fake()
        self.assertNotIn("send", self.transport.calls)
        self.assertEqual(result["counts"]["rejected"], 1)

    def test_stop_set_while_send_completes_no_interrupt_or_more_native_calls(self):
        self.transport.on_send = lambda *_: self.stop.set()
        result = self.run_fake()
        self.assertEqual(result["stage"], "stopped")
        self.assertEqual(result["counts"]["native_completed"], 1)
        self.assertEqual(self.transport.calls[-2:], ["send", "close"])

    def test_check_is_read_only_and_reports_no_native_load(self):
        result = check(settings(), transport_factory=lambda _: self.transport)
        self.assertTrue(result["ready"])
        self.assertFalse(result["native_module_loaded"])
        self.assertEqual(self.transport.calls, ["preflight"])

    def test_disabled_settings_never_open_network_or_native(self):
        with self.assertRaisesRegex(BridgeError, "disabled"):
            run_bridge(settings(enabled=False), directory=self.directory,
                       transport_factory=lambda _: self.fail("must not create transport"))

    def test_check_rejects_changed_identity(self):
        self.transport.self_id = "wxid_other"
        with self.assertRaisesRegex(BridgeError, "account_binding"):
            check(settings(), transport_factory=lambda _: self.transport)

    def test_load_settings_only_explicit_fake_file(self):
        path = self.directory / "config.json"
        data = vars(settings())
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(load_settings(path), settings())

    def test_32_message_batch_prevents_unbounded_native_drain(self):
        self.transport.events = [incoming(native_message_id=100 + n) for n in range(70)]
        self.socket.send_reply = False
        result = self.run_fake(max_cycles=1)
        self.assertEqual(result["counts"]["observed"], 32)
        self.assertEqual(self.transport.calls.count("poll"), 32)

    def test_durable_loading_marker_precedes_start(self):
        original = self.transport.start
        def start():
            status = json.loads((self.directory / "bridge-status.json").read_text())
            self.assertTrue(status["cleanup_required"])
            self.assertEqual(status["stage"], "loading")
            return original()
        self.transport.start = start
        self.run_fake()

    def test_status_failure_before_loading_does_not_start(self):
        from scripts.wechat_native_bridge import atomic_status
        def write(path, report):
            if report["stage"] == "loading":
                raise OSError("disk denied")
            atomic_status(path, report)
        result = self.run_fake(write_status=write)
        self.assertEqual(result["stage"], "failed")
        self.assertNotIn("start", self.transport.calls)
        self.assertFalse(result["cleanup_required"])

    def mark_unknown(self):
        path = self.directory / "bridge-status.json"
        path.write_text(json.dumps({"stage": "unknown", "cleanup_required": True,
                                    "manual_recovery_required": True, "native_pending": True}), encoding="utf-8")
        return path

    def test_recover_holds_singleton_lock_and_never_calls_native_if_busy(self):
        self.mark_unknown()
        with single_bridge(self.directory / "bridge.lock"):
            with self.assertRaisesRegex(BridgeError, "already_running"):
                recover(settings(), directory=self.directory, transport_factory=lambda _: self.transport)
        self.assertEqual(self.transport.calls, [])

    def test_recover_existing_bridge_rejection_preserves_unknown(self):
        status = self.mark_unknown()
        original = status.read_bytes()
        def refuses():
            raise BridgeError("existing_bridge_refused")
        self.transport.preflight = refuses
        with self.assertRaisesRegex(BridgeError, "existing_bridge"):
            recover(settings(), directory=self.directory, transport_factory=lambda _: self.transport)
        self.assertEqual(status.read_bytes(), original)
        self.assertNotIn("start", self.transport.calls)
        self.assertNotIn("close", self.transport.calls)

    def test_recover_clean_preflight_preserves_ledger_and_never_resends(self):
        status = self.mark_unknown()
        ledger = self.directory / "bridge-ledger.sqlite3"
        ledger.write_bytes(b"offline-ledger-preservation-fixture")
        result = recover(settings(), directory=self.directory, transport_factory=lambda _: self.transport)
        self.assertEqual(result["reason"], "recovered_after_clean_preflight")
        self.assertEqual(self.transport.calls, ["preflight"])
        self.assertEqual(ledger.read_bytes(), b"offline-ledger-preservation-fixture")
        recovered = json.loads(status.read_text())
        self.assertFalse(recovered["cleanup_required"])
        self.assertFalse(recovered["manual_recovery_required"])
        self.assertFalse(recovered["native_pending"])

    def test_recover_wrong_account_cannot_clear_unknown(self):
        status = self.mark_unknown()
        self.transport.self_id = "wxid_other"
        with self.assertRaisesRegex(BridgeError, "account_binding"):
            recover(settings(), directory=self.directory, transport_factory=lambda _: self.transport)
        self.assertTrue(json.loads(status.read_text())["cleanup_required"])


class CheckCliDiagnosticsTests(unittest.TestCase):
    def check_failure(self, error):
        output = io.StringIO()
        with patch("scripts.wechat_native_bridge.load_settings", return_value=settings()), \
                patch("scripts.wechat_native_bridge.check", side_effect=error) as preflight, \
                patch("scripts.wechat_native_bridge.run_bridge") as run, \
                patch("scripts.wechat_native_bridge.recover") as recovery, \
                patch("scripts.wechat_native_bridge.signal.signal"), redirect_stdout(output):
            code = main(["--mode", "check"])
        self.assertEqual(code, 1)
        preflight.assert_called_once()
        run.assert_not_called()
        recovery.assert_not_called()
        return json.loads(output.getvalue())

    def test_unlogged_account_reason_remains_actionable(self):
        result = self.check_failure(TransportError("account_not_ready"))
        self.assertEqual(result, {"stage": "failed", "reason": "account_not_ready"})

    def test_wrong_account_reason_remains_actionable(self):
        result = self.check_failure(TransportError("expected_account_mismatch"))
        self.assertEqual(result, {"stage": "failed", "reason": "expected_account_mismatch"})

    def test_existing_bridge_reason_requires_recovery(self):
        result = self.check_failure(TransportError("existing_bridge_refused"))
        self.assertEqual(result, {"stage": "failed", "reason": "existing_bridge_refused"})

    def test_unexpected_exception_details_stay_private(self):
        result = self.check_failure(OSError("private account or process details"))
        self.assertEqual(result, {"stage": "failed", "reason": "bridge_setup_failed"})


if __name__ == "__main__":
    unittest.main()
