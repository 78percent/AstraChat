"""Isolated, opt-in single visible WeChat group probe. No AstrBot/model access."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import logging
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.wechat_desktop_probe_core import ObservedMessage, ProbeState

VERSION = "41.1.7"
CLIENT_MAX = (4, 1, 8, 107)
CONFIG = ROOT / "config/wechat-desktop.local.json"
REPORT_DIR = ROOT / "runtime/wechat-desktop/probes"


class ProbeError(Exception):
    """Only static codes, never include third-party exception/chat content."""


def installed_client_version():
    """Read the executable resource, not a potentially stale uninstall record."""
    if sys.platform != "win32":
        return None
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        executable = config.get("client_executable")
        if not isinstance(executable, str) or not Path(executable).is_file():
            return None
        import win32api  # File metadata only; no wxauto or desktop actions.
        version = win32api.GetFileVersionInfo(executable, "\\")
        high, low = version["FileVersionMS"], version["FileVersionLS"]
        return (high >> 16, high & 65535, low >> 16, low & 65535)
    except (OSError, ValueError, ImportError, AttributeError, KeyError):
        return None


def environment_check():
    versions = {}
    for package in ("wxauto4", "pywin32", "comtypes", "httpx", "psutil"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    client_version = installed_client_version()
    return {
        "kind": "environment_only", "python": platform.python_version(),
        "windows": sys.platform == "win32", "packages": versions,
        "ready": (sys.platform == "win32" and sys.version_info[:2] == (3, 12)
                  and versions["wxauto4"] == VERSION and all(versions.values())),
        "wechat_imported": False, "ui_touched": False,
        "client_file_version": ".".join(map(str, client_version)) if client_version else None,
        "client_declared_maximum": ".".join(map(str, CLIENT_MAX)),
        "client_exceeds_declared_maximum": client_version > CLIENT_MAX if client_version else None,
        "live_compatibility_proven": False,
    }


def load_config(path=CONFIG):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ProbeError("configuration_unavailable") from None
    if not isinstance(config, dict):
        raise ProbeError("invalid_configuration")
    target = config.get("target_group")
    if (not isinstance(target, str) or not target or target != target.strip()
            or len(target) > 80 or not target.isprintable()):
        raise ProbeError("invalid_target")
    if (config.get("account_confirmed") is not True
            or config.get("unique_group_confirmed") is not True
            or config.get("interactive_desktop_confirmed") is not True):
        raise ProbeError("manual_prerequisites_not_confirmed")
    return config


class QuietLibrary:
    """Discard library stdout/logs so reports never collect chat body or names."""
    def __enter__(self):
        self.stream = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(self.stream))
        self.stack.enter_context(contextlib.redirect_stderr(self.stream))
        self.stack.enter_context(contextlib.chdir(ROOT / "runtime/wechat-desktop"))
        return self

    def __exit__(self, *args):
        logging.getLogger().handlers.clear()
        self.stack.close()
        self.stream.close()


class DesktopClient:
    def __init__(self, client, target):
        self.client = client
        self.target = target

    def guard(self):
        info = self.client.ChatInfo()
        if (not isinstance(info, dict) or info.get("chat_name") != self.target
                or info.get("chat_type") != "group"):
            raise ProbeError("target_not_confirmed")
        return info

    def snapshot(self):
        self.guard()  # Never read messages before confirming current target.
        raw = self.client.GetAllMessage()
        self.guard()
        if not isinstance(raw, (list, tuple)) or len(raw) > 1000:
            raise ProbeError("unexpected_message_collection")
        messages = []
        for item in raw:
            ui_id = getattr(item, "id", None)
            if isinstance(ui_id, (tuple, list)) and ui_id and all(type(x) is int for x in ui_id):
                ui_id = ":".join(map(str, ui_id))
            elif type(ui_id) is int:
                ui_id = str(ui_id)
            attr, kind = getattr(item, "attr", ""), getattr(item, "type", "")
            sender, content = getattr(item, "sender", ""), getattr(item, "content", "")
            # System/image records can have no sender; do not coerce arbitrary objects.
            sender = "" if sender is None else sender
            content = "" if content is None else content
            if not all(isinstance(x, str) for x in (ui_id, attr, kind, sender, content)):
                raise ProbeError("unexpected_message_fields")
            messages.append(ObservedMessage(ui_id, attr, kind, sender, content))
        self.guard()
        return messages

    def send(self, text):
        self.guard()
        result = self.client.SendMsg(text, who=None, clear=True, exact=True)
        self.guard()
        # Even a failed response dict is nonempty. This is local status only.
        return isinstance(result, dict) and result.get("status") == "成功"


def write_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@contextlib.contextmanager
def execution_lock(name="probe.lock"):
    """OS-held lock; a crashed supervisor cannot leave a stale permanent lock."""
    import msvcrt
    lock_path = REPORT_DIR.parent / name
    with lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ProbeError("another_probe_is_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def worker(args, report_path):
    config = load_config()
    state = ProbeState(config["target_group"], args.nonce)
    started = time.monotonic()
    report = {
        "schema_version": 1, "mode": args.mode, "nonce": args.nonce,
        "source": "wxauto4_free_visible_ui", "platform_message_id": None,
        "server_delivery_confirmed": False, "peer_received": None,
        "model_called": False, "production_memory_written": False,
        "allow_send": args.allow_send, "stage": "import", "dispatch_attempted": False,
        "environment": environment_check(),
    }
    def checkpoint(stage):
        report.update(stage=stage, elapsed_seconds=round(time.monotonic() - started, 3),
                      probe=state.summary())
        write_report(report_path, report)

    checkpoint("import")
    try:
        with QuietLibrary():
            from wxauto4 import WeChat, WxParam
            WxParam.ENABLE_FILE_LOGGER = False
            WxParam.TELEMETRY_ENABLED = False
            logging.disable(logging.CRITICAL)
            checkpoint("initialize")
            client = DesktopClient(WeChat(debug=False, resize=False), config["target_group"])
            checkpoint("baseline")
            initial = client.snapshot()
            state.observe(config["target_group"], "group", initial, time.monotonic())
            checkpoint("ready")
            # Write to original stdout deliberately, never relay library output.
            if state.state == "WAITING":
                sys.__stdout__.write(json.dumps({"stage": "ready", "command": state.command,
                                               "will_send": args.allow_send}, ensure_ascii=False) + "\n")
                sys.__stdout__.flush()
            if args.mode == "probe" or state.terminal:
                checkpoint("probe_complete")
                return
            deadline = time.monotonic() + args.seconds
            dispatch_time = None
            while not state.terminal and time.monotonic() < deadline:
                messages = client.snapshot()
                admitted = state.observe(config["target_group"], "group", messages, time.monotonic())
                if admitted:
                    checkpoint("observed")
                    if not args.allow_send:
                        state.stop("observe_only", time.monotonic())
                        break
                    client.guard()
                    state.mark_dispatched(time.monotonic())
                    report["dispatch_attempted"] = True
                    # Persist uncertainty before any side effect. Never retry this run.
                    checkpoint("dispatching")
                    report["ui_call_reported_success"] = client.send(state.reply)
                    dispatch_time = time.monotonic()
                checkpoint("polling")
                if dispatch_time is not None and time.monotonic() - dispatch_time >= 10:
                    state.fail("bubble_not_observed", time.monotonic())
                    break
                if not state.terminal:
                    time.sleep(0.5)
            if not state.terminal:
                state.stop("deadline", time.monotonic())
    except KeyboardInterrupt:
        state.stop("interrupted", time.monotonic())
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["failed_at_stage"] = report["stage"]
        report["error_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno,
                                   "function": frame.name}
                                  for frame in traceback.extract_tb(exc.__traceback__)[-6:]]
        report["error_keywords"] = [word for word in (
            "未找到", "没有找到", "找不到", "主窗口", "微信窗口", "登录", "版本", "不支持",
            "初始化", "超时", "列表", "SessionBox", "ChatBox", "UIAutomation") if word in str(exc)]
        # Only our own static codes are safe to include.
        reason = str(exc) if isinstance(exc, ProbeError) else "desktop_api_error"
        state.fail(reason, time.monotonic())
    finally:
        checkpoint("finished")


def main(argv=None):
    parser = argparse.ArgumentParser(description="指定可见微信群的一次性基础验证；默认只检查环境。")
    parser.add_argument("--mode", choices=("check", "probe", "run"), default="check")
    parser.add_argument("--allow-send", action="store_true", help="仅run模式：允许对新口令最多发一条中性回复")
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--nonce", default=None)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report-path", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.mode == "check":
        print(json.dumps(environment_check(), ensure_ascii=False, indent=2))
        return 0 if environment_check()["ready"] else 2
    if (not math.isfinite(args.seconds) or not 5 <= args.seconds <= 600
            or args.allow_send and args.mode != "run"):
        parser.error("seconds需在5–600之间；allow-send仅适用于run")
    args.nonce = args.nonce or uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[a-f0-9]{8,32}", args.nonce):
        parser.error("nonce应为8–32位小写十六进制字符")
    try:
        if not environment_check()["ready"]:
            raise ProbeError("isolated_environment_not_ready")
        if environment_check().get("client_exceeds_declared_maximum") is True:
            raise ProbeError("client_outside_declared_support")
        load_config()
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        expected = REPORT_DIR / (args.nonce + ".json")
        if args.worker:
            if not args.report_path or Path(args.report_path).resolve() != expected.resolve():
                raise ProbeError("invalid_worker_report")
            claim = json.loads(expected.read_text(encoding="utf-8"))
            token = os.environ.pop("ASTRBOT_WECHAT_PROBE_LAUNCH", "")
            if (not token or claim.get("stage") != "starting"
                    or claim.get("launch_hash") != hashlib.sha256(token.encode()).hexdigest()):
                raise ProbeError("invalid_worker_launch")
            # Atomic consume prevents a second worker from using the same claim.
            with expected.with_suffix(".consumed").open("x", encoding="utf-8"):
                pass
            # Also fence the actual UI process if its supervisor exits abruptly.
            with execution_lock("worker.lock"):
                worker(args, expected)
            return 0
        with execution_lock():
            # A nonce is never replayed, even after a process restart.
            launch_token = uuid.uuid4().hex
            with expected.open("x", encoding="utf-8") as stream:
                json.dump({"stage": "starting", "dispatch_attempted": False,
                           "parent_pid": os.getpid(),
                           "launch_hash": hashlib.sha256(launch_token.encode()).hexdigest()}, stream)
            command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                       "--worker", "--report-path", str(expected), "--mode", args.mode,
                       "--nonce", args.nonce, "--seconds", str(args.seconds)]
            if args.allow_send:
                command.append("--allow-send")
            child_env = {**os.environ, "ASTRBOT_WECHAT_PROBE_LAUNCH": launch_token}
            child = subprocess.Popen(command, cwd=ROOT, env=child_env)
            try:
                child.wait(timeout=args.seconds + 30)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                # A Windows venv launcher can own another python process.
                # Stop this exact process tree, not arbitrary Python processes.
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                child.wait(timeout=10)
                report = json.loads(expected.read_text(encoding="utf-8"))
                report["stage"] = "worker_stopped"
                report["supervisor_result"] = "UNKNOWN" if report.get("dispatch_attempted") else "FAILED_BEFORE_DISPATCH"
                write_report(expected, report)
        report = json.loads(expected.read_text(encoding="utf-8"))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("report=" + str(expected))
        return 0 if report.get("probe", {}).get("state") in ("WAITING", "UI_OBSERVED") else 2
    except FileExistsError:
        print("error=nonce_already_used; 使用新口令，禁止重发旧试验")
        return 2
    except ProbeError as exc:
        print("error=" + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
