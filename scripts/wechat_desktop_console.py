"""Visible lifetime owner for a detached, graceful WeChat/Docker session."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from scripts import start_wechat_desktop as startup


def read_json(path):
    try:
        value = Path(path).read_text(encoding="utf-8")
        value = json.loads(value)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def request_stop(session_id):
    if uuid.UUID(session_id).hex != session_id:
        raise ValueError("invalid_session")
    (startup.DATA / ("desktop-stop-" + session_id)).write_text("stop\n", encoding="ascii")


def spawn_session(session_id, *, stop_only=False):
    if uuid.UUID(session_id).hex != session_id:
        raise ValueError("invalid_session")
    # python.exe in this venv is a redirector. DETACHED_PROCESS only detaches
    # that first executable; the real console interpreter can create a second,
    # blank console. A GUI-subsystem interpreter never needs a console, and
    # remains alive when the visible owner window closes.
    python = startup.PYTHON.with_name("pythonw.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512)
    # The owner PID is the actual Python interpreter, not the outer venv
    # redirector. The supervisor keeps an OS handle to it across console close.
    startup.DATA.mkdir(parents=True, exist_ok=True)
    command = [str(python), "-X", "utf8", str(startup.ROOT / "scripts/wechat_desktop_session.py"),
               "--owner-pid", str(os.getpid()), "--session-id", session_id]
    if stop_only:
        command.append("--stop-only")
    with (startup.DATA / ("desktop-supervisor-" + session_id + ".log")).open("ab") as log:
        return subprocess.Popen(
            command,
            cwd=startup.ROOT, creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )


def keyboard_stop_requested():
    if os.name != "nt":
        return False
    import msvcrt
    if msvcrt.kbhit():
        return msvcrt.getwch() in {"\r", "q", "Q", "\x03"}
    return False


def run_console():
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW("三叶微信 — 关闭窗口即停止服务")
    print("三叶微信\n关闭此窗口或按 Enter，会停止机器人服务并退出 Docker Desktop。\n", flush=True)
    session_id = uuid.uuid4().hex
    result_file = startup.DATA / ("desktop-session-" + session_id + ".result.json")
    process = spawn_session(session_id)
    last_message = None
    started = time.monotonic()
    stopping = False
    joined = False
    while True:
        try:
            result = read_json(result_file)
            if result:
                stage = result.get("stage")
                if stage == "busy":
                    print("已有启动窗口正在管理服务，请使用原窗口。", flush=True)
                    return 0
                if stage == "stopped":
                    if result.get("startup_error"):
                        reason = startup.safe_reason({"reason": result["startup_error"]}, "unexpected_startup_error")
                        if reason != "startup_cancelled":
                            print("启动未完成：" + startup.ERRORS.get(reason, reason), flush=True)
                            print("本次服务已正常停止。", flush=True)
                            return 1
                    print("微信监听、机器人服务和 Docker Desktop 已停止。", flush=True)
                    return 0
                print("未能完成关闭，请保留这个窗口。错误代码：" +
                      startup.safe_reason(result, "session_failed"), flush=True)
                return 1
            state = read_json(startup.START_STATUS)
            if state.get("session_id") == session_id:
                joined = True
                message = state.get("message")
                if message and message != last_message:
                    print(message, flush=True)
                    last_message = message
                if state.get("stage") == "stopping" and not stopping:
                    stopping = True
                    print("正在正常停止服务，请稍候……", flush=True)
            if keyboard_stop_requested() and not stopping:
                request_stop(session_id)
                stopping = True
                print("正在正常停止服务，请稍候……", flush=True)
            if process.poll() is not None:
                # Results are persisted before supervisor exit. Re-read once to
                # cover the write/exit boundary rather than claiming success.
                if read_json(result_file):
                    continue
                print("后台控制进程异常退出，不能确认服务已停止。", flush=True)
                return 1
            if not joined and time.monotonic() - started > 20:
                request_stop(session_id)
                print("启动窗口未能连接控制进程，已请求停止。", flush=True)
                return 1
            time.sleep(.2)
        except KeyboardInterrupt:
            if not stopping:
                request_stop(session_id)
                stopping = True
                print("\n正在正常停止服务，请稍候……", flush=True)
        except Exception:
            # If this process exits, the detached supervisor observes the real
            # owner handle and still completes the orderly shutdown.
            request_stop(session_id)
            print("控制窗口遇到异常，已请求后台正常停止服务。", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(run_console())
