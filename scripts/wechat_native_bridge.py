"""Explicitly started Windows-to-AstrBot bridge; never auto-reconnect or reload.

Native calls, channel cleanup and BridgeGuard ownership stay on one OS thread.
The websocket implementation may manage its own I/O threads; they never access
NativeTransport. CLI output and status files contain static diagnostics only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.wechat_native_bridge_core import (
    BridgeCore, BridgeError, BridgeJournal, BridgeSettings, decode_frame, handshake_ack,
)
from scripts.wechat_native_transport import TransportError

CONFIG = ROOT / "config/wechat-native-bridge.local.json"
DATA = ROOT / "runtime/wechat-native"
UPSTREAM = ROOT / "work/helper-native-upstream"


def load_settings(path=CONFIG):
    try:
        if Path(path).stat().st_size > 65536:
            raise BridgeError("config_too_large")
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise BridgeError("config_unavailable") from None
    return BridgeSettings.parse(raw)


def atomic_status(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.05)


@contextlib.contextmanager
def single_bridge(path):
    """The local run marker and SQLite are owned by only one host process."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BridgeError("bridge_already_running") from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BridgeError("bridge_already_running") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def default_transport(settings):
    from scripts.wechat_native_transport import NativeTransport, VOICE_COMMIT
    upstream = ROOT / "work/helper-native-voice-upstream" if settings.upstream_commit == VOICE_COMMIT else UPSTREAM
    return NativeTransport(upstream, expected_self=settings.expected_self,
                           commit=settings.upstream_commit, private_peer=settings.private_peer)


def default_voice(core):
    from scripts.wechat_native_transport import VOICE_COMMIT
    if core.settings.upstream_commit != VOICE_COMMIT:
        return None
    from scripts.wechat_voice_bridge import VoiceBridge
    from scripts.wechat_voice_runtime import VoiceRuntime
    return VoiceBridge(core, VoiceRuntime())


def default_connect(*args, **kwargs):
    # Lazy import keeps offline policy tests independent of this dependency.
    from websockets.sync.client import connect
    return connect(*args, **kwargs)


def check(settings, *, transport_factory=default_transport):
    transport = transport_factory(settings)
    report = transport.preflight()
    if transport.self_id != settings.expected_self:
        raise BridgeError("account_binding_changed")
    return {"mode": "check", "ready": report.get("ready") is True,
            "enabled": settings.enabled, "native_module_loaded": False}


def recover(settings, *, directory=DATA, transport_factory=default_transport,
            wall=time.time, write_status=atomic_status):
    """Explicit recovery after clean preflight; never unload, edit ledger or resend."""
    directory = Path(directory)
    status_path = directory / "bridge-status.json"
    with single_bridge(directory / "bridge.lock"):
        try:
            previous = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BridgeError("previous_status_unreadable") from None
        if not isinstance(previous, dict):
            raise BridgeError("previous_status_unreadable")
        verified = check(settings, transport_factory=transport_factory)
        if verified.get("ready") is not True:
            raise BridgeError("recovery_preflight_not_ready")
        # NativeTransport.preflight rejects any existing bridge and verifies the
        # fixed source, DLLs, current process/core and expected logged-in account.
        previous.update(stage="stopped", reason="recovered_after_clean_preflight",
                        cleanup_required=False, manual_recovery_required=False,
                        native_pending=False, connected=False, updated_at=wall())
        write_status(status_path, previous)
        return {"mode": "recover", "stage": "stopped", "reason": "recovered_after_clean_preflight",
                "cleanup_required": False, "manual_recovery_required": False,
                "native_module_loaded": False, "ledger_unchanged": True}


def _previous_run(path):
    if not path.exists():
        return
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BridgeError("previous_status_unreadable") from None
    if (not isinstance(previous, dict) or previous.get("cleanup_required") is not False
            or previous.get("manual_recovery_required") is not False):
        raise BridgeError("previous_native_session_unresolved")


def run_bridge(settings, *, directory=DATA, transport_factory=default_transport,
               connector=default_connect, stop=None, wall=time.time,
               monotonic=time.monotonic, sleep=time.sleep, write_status=atomic_status,
               max_cycles=None, voice_factory=default_voice):
    """Dependency-injected loop. Tests supply fake transport and fake websocket."""
    if not settings.enabled:
        raise BridgeError("bridge_disabled")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stop = stop if stop is not None else threading.Event()
    status_path, sentinel = directory / "bridge-status.json", directory / "bridge.stop"
    with single_bridge(directory / "bridge.lock"):
        _previous_run(status_path)
        report = {"version": 1, "stage": "starting", "reason": "starting",
                  "cleanup_required": False, "manual_recovery_required": False,
                  "native_pending": False, "connected": False, "counts": {}}
        transport, ws, journal, core = None, None, None, None
        voice = scheduled = None
        status_broken = False
        normal_stop = False
        started_intent = False
        native_owner = threading.get_ident()
        def native(method, *args):
            if threading.get_ident() != native_owner:
                raise BridgeError("native_thread_changed")
            return method(*args)
        def save(stage, reason):
            report.update(stage=stage, reason=reason, updated_at=wall())
            if core is not None:
                report["counts"] = dict(core.counts)
                report["ack_results"] = dict(core.ack_results)
                report["last_ack"] = core.last_ack
                report["last_event"] = core.last_event
            if voice is not None:
                report["voice"] = {**voice.runtime.status(), "counts": dict(voice.counts),
                                   "last_input": dict(voice.last_input)}
            write_status(status_path, report)
        def stopping():
            return stop.is_set() or sentinel.exists()
        def send_frame(frame):
            ws.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
        try:
            save("starting", "starting")
            if stopping():
                normal_stop = True
                report["reason"] = "stop_requested"
            else:
                transport = transport_factory(settings)
                native(transport.preflight)
                if transport.self_id != settings.expected_self:
                    raise BridgeError("account_binding_changed")
                client_session = uuid.uuid4().hex
                save("connecting", "connecting")
                # Avoid library debug logs accidentally recording authorization or text.
                wire_logger = logging.getLogger("wechat_native_bridge.wire")
                wire_logger.disabled = True
                ws = connector(settings.url,
                               additional_headers={"Authorization": "Bearer " + settings.token},
                               open_timeout=8, close_timeout=2, max_size=16384, max_queue=32,
                               ping_interval=20, ping_timeout=20, logger=wire_logger, proxy=None)
                send_frame({"type": "hello", "version": 1, "platform_id": settings.platform_id,
                            "self_id": settings.expected_self, "session": client_session,
                            "capabilities": ["scheduled_text_v1"]})
                session = handshake_ack(decode_frame(ws.recv(timeout=8)), client_session)
                report["connected"] = True
                journal = BridgeJournal(directory / "bridge-ledger.sqlite3")
                core = BridgeCore(settings, journal, session)
                from scripts.wechat_scheduled_bridge import ScheduledBridge
                scheduled = ScheduledBridge(core, directory / 'scheduled-ledger.sqlite3')
                if stopping():
                    normal_stop = True
                    report["reason"] = "stop_requested"
                else:
                    report["cleanup_required"] = True
                    save("loading", "loading")  # Durable marker before the first side effect.
                    started_intent = True
                    native(transport.start)
                    voice = voice_factory(core)
                    save("running", "ready")
                    heartbeat, cycles = monotonic(), 0
                    while not stopping():
                        if max_cycles is not None and cycles >= max_cycles:
                            normal_stop = True
                            report["reason"] = "test_cycle_limit"
                            break
                        cycles += 1
                        activity = False
                        for _ in range(32):
                            if stopping():
                                break
                            raw = native(transport.poll)
                            if raw is None:
                                break
                            activity = True
                            if voice is not None and getattr(raw, "type", None) == 34:
                                voice.observe(raw, wall())
                                continue
                            event = core.observe(raw, wall())
                            if event is not None:
                                send_frame(event)
                                core.mark_forwarded(event)
                        if voice is not None:
                            events, responses = voice.poll(wall())
                            for event in events:
                                send_frame(event)
                                core.mark_forwarded(event)
                            for response in responses:
                                send_frame(response)
                        for _ in range(32):
                            if stopping():
                                break
                            try:
                                raw_frame = ws.recv(timeout=.01)
                            except TimeoutError:
                                break
                            activity = True
                            frame = decode_frame(raw_frame)
                            if frame.get("type") == "event_ack":
                                core.acknowledge(frame)
                                continue
                            if voice is not None and frame.get("type") == "prepare_voice":
                                response = voice.prepare(frame, wall())
                                if response is not None:
                                    send_frame(response)
                                continue
                            if voice is not None and frame.get("type") == "cancel_voice":
                                voice.cancel(frame)
                                continue
                            voice_commit = voice is not None and frame.get("type") == "commit_voice"
                            delivery_core = scheduled if frame.get('type') == 'scheduled_send' else core
                            audio = None
                            if voice_commit:
                                prepared, immediate, audio = voice.commit(frame, wall())
                            else:
                                prepared, immediate = delivery_core.prepare(frame, wall())
                            if prepared is None:
                                send_frame(immediate)
                                continue
                            if stopping() or wall() >= prepared.expires_at:
                                send_frame(delivery_core.cancel_before_dispatch(prepared))
                                continue
                            # Intention is already fsynced by SQLite. Never cancel this call.
                            core.counts["dispatches"] += 1
                            try:
                                if audio is not None:
                                    outcome = native(transport.send_voice, prepared.room,
                                                     audio["path"], audio["duration_ms"])
                                else:
                                    outcome = native(transport.send, prepared.room, prepared.text)
                            except BaseException:
                                result = delivery_core.complete(prepared, None, transport.pid)
                                try:
                                    send_frame(result)
                                except Exception:
                                    pass
                                raise BridgeError("native_send_unknown") from None
                            result = (voice.completed(prepared, outcome, transport.pid, audio)
                                      if voice_commit else delivery_core.complete(prepared, outcome, transport.pid))
                            send_frame(result)
                            if voice_commit:
                                voice.release(frame)
                            if result["status"] == "unknown":
                                raise BridgeError("native_completion_unverified")
                        if monotonic() - heartbeat >= 5:
                            save("running", "ready")
                            heartbeat = monotonic()
                        if not activity:
                            sleep(.1)
                    if stopping():
                        normal_stop = True
                        report["reason"] = "stop_requested"
        except BridgeError as error:
            # All BridgeError values originate from static diagnostic constants here/core.
            report["reason"] = str(error)
        except BaseException:
            report["reason"] = "bridge_operation_failed"
        finally:
            if voice is not None:
                try:
                    voice.runtime.close()
                except Exception:
                    report["reason"] = "voice_shutdown_failed"
                    normal_stop = False
            if transport is not None:
                try:
                    cleanup = native(transport.close)
                    clean = (isinstance(cleanup, dict) and cleanup.get("state") == "CLEAN"
                             and cleanup.get("dll_unloaded") is True)
                    report["cleanup_required"] = not clean
                    report["manual_recovery_required"] = not clean
                    report["native_pending"] = bool(transport.pending)
                except BaseException:
                    report["cleanup_required"] = started_intent
                    report["manual_recovery_required"] = started_intent
                    report["native_pending"] = bool(getattr(transport, "pending", False))
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            report["connected"] = False
            if scheduled is not None:
                scheduled.close()
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    report["reason"] = "journal_close_failed"
                    normal_stop = False
            stage = "stopped" if normal_stop else "failed"
            if report["manual_recovery_required"]:
                stage = "unknown"
            try:
                save(stage, report["reason"])
            except BaseException:
                status_broken = True
            if status_broken:
                report["stage"] = "failed"
                report["reason"] = "status_write_failed"
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local native WeChat bridge (explicit start only)")
    parser.add_argument("--mode", choices=("check", "run", "recover"), default="check")
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args(argv)
    stop = threading.Event()
    # A signal requests a stop; it must never interrupt an outstanding native call.
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, lambda *_: stop.set())
    try:
        settings = load_settings(args.config)
        if args.mode == "check":
            report = check(settings)
        elif args.mode == "recover":
            report = recover(settings)
        else:
            report = run_bridge(settings, stop=stop)
    except (BridgeError, TransportError) as error:
        report = {"stage": "failed", "reason": str(error)}
    except BaseException:
        report = {"stage": "failed", "reason": "bridge_setup_failed"}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report.get("ready") is True or report.get("stage") == "stopped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
