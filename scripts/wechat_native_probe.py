"""One authorized neutral WeChat group experiment, separate from production bots."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.wechat_native_probe_core import NativeProbe
from scripts.wechat_native_transport import NativeTransport, TransportError

UPSTREAM = ROOT / "work/helper-native-upstream"
CONFIG = ROOT / "config/wechat-native.local.json"
DATA = ROOT / "runtime/wechat-native"
COMMIT = "a81c495770b397646ba0661118927c83589c41fc"


class RunnerError(Exception):
    """Static, non-sensitive diagnostic codes."""


def load_config(path=CONFIG):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise RunnerError("config_unavailable") from None
    if (not isinstance(value, dict) or value.get("target_group_label") != "test"
            or value.get("native_trial_authorized") is not True
            or value.get("upstream_commit") != COMMIT):
        raise RunnerError("config_not_authorized_for_test")
    if value.get("expected_self") is not None and not isinstance(value["expected_self"], str):
        raise RunnerError("invalid_account_binding")
    if value.get("allowed_room") is not None and not isinstance(value["allowed_room"], str):
        raise RunnerError("invalid_room_binding")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows readers and scanners can briefly deny replacement of an open
    # report. Retry only the local rename; never repeat a native operation.
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def persist_binding(config, self_id, room=None, *, path=CONFIG):
    current = load_config(path)
    if current != config:
        raise RunnerError("config_changed_during_trial")
    if current.get("expected_self") not in (None, self_id):
        raise RunnerError("account_binding_changed")
    if room is not None and current.get("allowed_room") not in (None, room):
        raise RunnerError("room_binding_changed")
    current["expected_self"] = self_id
    if room is not None:
        current["allowed_room"] = room
        current["binding_basis"] = "user_sent_unique_nonce_in_named_test_group"
    atomic_json(path, current)
    config.clear()
    config.update(current)


@contextlib.contextmanager
def single_probe():
    import msvcrt
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "probe.lock").open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RunnerError("another_trial_is_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def run_trial(transport, config, nonce, seconds, path, stop, *, config_path=CONFIG,
              clock=time.monotonic, sleep=time.sleep, emit=print):
    """All calls are serial; signal handlers only set stop, never interrupt ctypes."""
    begin = clock()
    report = {
        "schema_version": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "nonce": nonce, "scope": "one_neutral_reply_in_test_group",
        "source_commit": COMMIT, "model_called": False, "production_memory_written": False,
        "peer_received": None, "outgoing_platform_message_id": None,
        "dispatch_attempted": False, "native_in_flight": False,
        "cleanup": None,
    }
    probe = None
    def save(stage):
        report.update(stage=stage, elapsed_seconds=round(clock() - begin, 6))
        if probe is not None:
            report["probe"] = probe.summary()
        atomic_json(path, report)

    def invoke(function, *arguments):
        try:
            return function(*arguments)
        except BaseException as exc:
            # Static transport errors already classify whether a remote call ran.
            if not isinstance(exc, TransportError) and transport.loaded:
                transport.pending = True
            raise

    try:
        save("preflight")
        report["preflight"] = transport.preflight()
        persist_binding(config, transport.self_id, path=config_path)
        probe = NativeProbe(transport.self_id, nonce, allowed_room=config.get("allowed_room"))
        if stop.is_set():
            probe.stop("user_stop", clock())
            return report
        report["native_in_flight"] = True
        save("loading")
        report["start"] = invoke(transport.start)
        report["native_in_flight"] = False
        save("waiting_for_command")
        emit(json.dumps({"stage": "ready", "group": "test", "command": probe.command,
                         "native_loaded": True, "max_replies": 1}, ensure_ascii=False), flush=True)
        deadline = clock() + seconds
        while not stop.is_set() and not probe.terminal and clock() < deadline:
            report["native_in_flight"] = True
            save("polling")
            raw = invoke(transport.poll)
            report["native_in_flight"] = False
            if raw is not None and probe.observe(raw, clock()):
                persist_binding(config, transport.self_id, probe.room, path=config_path)
                # IDs of the approved event are private; never print full native objects.
                atomic_json(path.with_suffix(".binding.local.json"), {
                    "self_id": transport.self_id, "room_id": probe.room,
                    "incoming_native_message_id": str(probe.incoming_id),
                })
                if stop.is_set():
                    probe.stop("user_stop", clock())
                    break
                probe.mark_dispatched(clock())
                report["dispatch_attempted"] = True
                report["native_in_flight"] = True
                save("dispatching")  # Persist uncertainty before the actual send.
                outcome = invoke(transport.send, probe.room, probe.reply)
                report["native_in_flight"] = False
                completed = probe.complete(outcome, clock())
                report["temporary_native_report_retained"] = outcome.get("report_retained", True)
                save("native_completed" if completed else "send_unknown")
                emit(json.dumps({"stage": report["stage"], "peer_received": None},
                                ensure_ascii=False), flush=True)
            else:
                save("waiting_for_command")
            if not probe.terminal:
                sleep(0.15)
        if not probe.terminal:
            probe.stop("user_stop" if stop.is_set() else "deadline", clock())
    except BaseException as exc:
        report["error_type"] = type(exc).__name__
        report["failed_at_stage"] = report.get("stage", "preflight")
        if isinstance(exc, (RunnerError, TransportError)):
            # The transport contract requires static errors; never relay vendor text.
            code = getattr(exc, "code", str(exc))
            if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
                report["error_code"] = code
        if probe is not None:
            probe.fail("native_error", clock())
    finally:
        # Report failure must never skip native cleanup.
        try:
            save("stopping")
        except OSError:
            report["report_write_failed"] = True
        try:
            report["cleanup"] = transport.close()
        except BaseException as exc:
            report["cleanup"] = {"state": "UNKNOWN", "dll_unloaded": False,
                                 "manual_recovery_required": True,
                                 "error_type": type(exc).__name__}
        report["native_in_flight"] = bool(transport.pending)
        try:
            save("finished")
        except OSError:
            report["report_write_failed"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="原生微信test群限定试验；默认只读预检。")
    parser.add_argument("--mode", choices=("check", "run"), default="check")
    parser.add_argument("--allow-native-send", action="store_true",
                        help="明确允许本轮加载模块并最多发送一条中性回复")
    parser.add_argument("--seconds", type=float, default=300)
    parser.add_argument("--nonce")
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 5 <= args.seconds <= 900:
        parser.error("seconds必须在5–900之间")
    if args.mode == "run" and not args.allow_native_send:
        parser.error("run需要--allow-native-send")
    nonce = args.nonce or uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[a-f0-9]{16,32}", nonce):
        parser.error("nonce应为16–32位小写十六进制字符")
    try:
        config = load_config()
        transport = NativeTransport(UPSTREAM, expected_self=config.get("expected_self"))
        if args.mode == "check":
            print(json.dumps(transport.preflight(), ensure_ascii=False, indent=2))
            return 0
        with single_probe():
            path = DATA / "probes" / (nonce + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                json.dump({"stage": "claimed", "nonce": nonce}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            stop = threading.Event()
            def request_stop(_signum, _frame):
                stop.set()
            previous = {}
            for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    previous[signum] = signal.signal(signum, request_stop)
            logging.disable(logging.CRITICAL)
            try:
                report = run_trial(transport, config, nonce, args.seconds, path, stop)
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print("report=" + str(path))
            return 0 if (report.get("probe", {}).get("state") == "NATIVE_COMPLETED"
                         and report.get("cleanup", {}).get("state") == "CLEAN") else 2
    except FileExistsError:
        print("error=nonce_already_used; 旧试验不会重放")
        return 2
    except (RunnerError, TransportError) as exc:
        code = getattr(exc, "code", str(exc))
        print("error=" + (code if re.fullmatch(r"[a-z_]{1,80}", code) else "preflight_failed"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
