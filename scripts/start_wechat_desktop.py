"""Desktop startup for the existing, bound WeChat bot. No account/config migration."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.wechat_native_bridge import DATA, load_settings
from scripts.wechat_native_bridge import single_bridge as single_bridge  # Public lock used by the session supervisor.
from scripts.wechat_native_bridge_core import BridgeError
from scripts.control_native_wechat import control as local_control

PYTHON = ROOT / ".runtime/wechat-native/.venv/Scripts/python.exe"
CONTROL = ROOT / "scripts/control_native_wechat.py"
BRIDGE = ROOT / "scripts/wechat_native_bridge.py"
PLUGIN_CONFIG = ROOT / "runtime/astrbot/config/astrbot_plugin_roleplay_gateway_config.json"
PLUGIN_STATUS = ROOT / "runtime/astrbot/plugin_data/astrbot_plugin_roleplay_gateway/runtime-status.json"
START_STATUS = DATA / "desktop-start-status.json"
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DASHBOARD_URL = "http://127.0.0.1:6185/"
WORKBENCH_URL = DASHBOARD_URL + "#/extension/plugins/astrbot_plugin_roleplay_gateway?open_config=astrbot_plugin_roleplay_gateway"
CANCEL_EVENT = None
SESSION_FIELDS = {}
_STATE = {}
_STARTED_AT = None

LOGIN_REASONS = {
    "wechat_process_not_unique": "请在本机微信登录机器人账号；若开了多个微信，请只保留机器人窗口后等待。",
    "account_not_ready": "请在微信窗口登录机器人账号，完成后会自动继续。",
    "expected_account_mismatch": "当前微信不是已绑定的机器人账号，请手动切换到机器人账号，完成后会自动继续。",
    "account_read_failed": "正在等待微信完成登录，请保持微信窗口打开。",
    "account_read_open_failed": "暂时无法读取微信登录状态，请确认微信以当前用户权限运行。",
    "core_not_loaded": "正在等待微信打开并完成登录。",
    "existing_bridge_refused": "上次监听仍留在微信中。请从微信菜单正常退出，再打开并登录机器人账号；本窗口会自动继续。",
}
ERRORS = {
    "docker_missing": "未找到已安装的 Docker Desktop。",
    "docker_timeout": "Docker Desktop 未能就绪，请查看它显示的错误后重试。",
    "docker_wrong_engine": "Docker 当前不是 Linux 容器模式。",
    "astrbot_container_missing": "当前项目的 AstrBot 容器不存在，需先恢复原有部署。",
    "astrbot_not_ready": "AstrBot 的微信服务尚未就绪，请查看 Docker Desktop 中 astrbot 的日志。",
    "configuration_mismatch": "微信桥接与 AstrBot 的已有绑定不一致，已保留原配置并停止启动。",
    "native_disabled": "已有配置关闭了微信机器人，未擅自更改开关。",
    "native_runtime_missing": "项目中的微信 Python 环境或脚本缺失。",
    "login_timeout": "等待登录已超过 15 分钟。登录完成后重新双击桌面启动脚本即可。",
    "bridge_busy": "已有微信监听进程，但连接或心跳尚未就绪；本次未重复启动。",
    "bridge_not_ready": "微信监听未达到就绪状态，请保留本窗口中的错误代码。",
    "bridge_heartbeat_stalled": "微信监听没有继续更新心跳，未把它报告为启动成功。",
    "client_fingerprint_mismatch": "本机微信版本与已验证版本不同，需要适配后才能启动。",
    "command_timeout": "某个启动步骤超时；已保留现有服务，可根据错误阶段检查。",
    "command_failed": "启动命令执行失败，已保留配置和现有服务。",
    "invalid_response": "启动工具没有返回有效状态，已停止后续启动。",
    "startup_cancelled": "已收到关闭请求，正在停止服务。",
}


class StartupError(RuntimeError):
    pass


def emit(message):
    if sys.stdout is not None:
        print(message, flush=True)
    if SESSION_FIELDS:
        record(_STATE.get("stage", "starting"), _STATE.get("reason", "starting"), message=message)


def record(stage, reason, **fields):
    DATA.mkdir(parents=True, exist_ok=True)
    value = {**_STATE, **SESSION_FIELDS, "stage": stage, "reason": reason,
             "updated_at": time.time(), **fields}
    if _STARTED_AT is not None:
        value["elapsed_seconds"] = round(time.monotonic() - _STARTED_AT, 3)
    temporary = START_STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(START_STATUS)
    _STATE.clear()
    _STATE.update(value)


def check_cancel():
    if CANCEL_EVENT is not None and CANCEL_EVENT.is_set():
        raise StartupError("startup_cancelled")


def wait(seconds):
    if CANCEL_EVENT is not None:
        if CANCEL_EVENT.wait(seconds):
            raise StartupError("startup_cancelled")
    else:
        time.sleep(seconds)


def command(arguments, timeout=20):
    # Only our short CLI child is bounded; never terminate Docker/WeChat/the bridge.
    check_cancel()
    try:
        result = subprocess.run([str(item) for item in arguments], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=HIDDEN)
    except subprocess.TimeoutExpired:
        raise StartupError("command_timeout") from None
    except OSError:
        raise StartupError("command_failed") from None
    check_cancel()
    return result


def json_command(arguments, timeout=45):
    result = command(arguments, timeout)
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise StartupError("invalid_response") from None
    if not isinstance(value, dict):
        raise StartupError("invalid_response")
    return value


def native_control(action):
    check_cancel()
    try:
        result = local_control(action)
    except BridgeError as error:
        raise StartupError(str(error)) from None
    check_cancel()
    return result


def native_check():
    return json_command([PYTHON, "-X", "utf8", BRIDGE, "--mode", "check"])


def native_recover():
    result = json_command([PYTHON, "-X", "utf8", BRIDGE, "--mode", "recover"])
    if result.get("stage") != "stopped" or result.get("ledger_unchanged") is not True:
        raise StartupError(safe_reason(result, "bridge_not_ready"))


def safe_reason(state, fallback):
    reason = state.get("reason")
    return reason if isinstance(reason, str) and reason.isascii() and reason.replace("_", "").isalnum() and len(reason) <= 80 else fallback


def native_ready(state, now=None):
    stamp = state.get("updated_at")
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        return False
    age = (time.time() if now is None else now) - stamp
    return (state.get("process_lock_held") is True and state.get("stage") == "running"
            and state.get("connected") is True and state.get("manual_recovery_required") is False
            and state.get("native_pending") is False and -2 <= age <= 20)


def validate_local():
    if not all(path.is_file() for path in (PYTHON, CONTROL, BRIDGE, PLUGIN_CONFIG)):
        raise StartupError("native_runtime_missing")
    try:
        settings = load_settings()
        plugin = json.loads(PLUGIN_CONFIG.read_text(encoding="utf-8-sig"))
        if not settings.enabled or plugin.get("native_enabled") is not True:
            raise StartupError("native_disabled")
        if (settings.expected_self != plugin.get("native_self_id")
                or settings.token != plugin.get("native_bridge_token")
                or settings.allowed_room not in plugin.get("native_allowed_group_ids", [])):
            raise StartupError("configuration_mismatch")
        expected_private = [settings.private_peer] if settings.private_peer else []
        if plugin.get("native_allowed_private_ids", []) != expected_private:
            raise StartupError("configuration_mismatch")
    except StartupError:
        raise
    except Exception:
        raise StartupError("configuration_mismatch") from None
    docker = shutil.which("docker.exe")
    if not docker:
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker/Docker/resources/bin/docker.exe"
        docker = str(candidate) if candidate.is_file() else None
    if not docker:
        raise StartupError("docker_missing")
    return docker


def ensure_docker(docker, timeout=180):
    def ready():
        try:
            probe = command([docker, "info", "--format", "{{.OSType}}"], timeout=6)
        except StartupError as error:
            if str(error) == "command_timeout":
                return False
            raise
        if probe.returncode:
            return False
        if probe.stdout.strip() != "linux":
            raise StartupError("docker_wrong_engine")
        return True
    if ready():
        return
    desktop = Path(docker).resolve().parents[2] / "Docker Desktop.exe"
    if not desktop.is_file():
        desktop = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker/Docker/Docker Desktop.exe"
    if not desktop.is_file():
        raise StartupError("docker_missing")
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    check_cancel()
    SESSION_FIELDS["docker_launch_requested"] = True
    process = subprocess.Popen([str(desktop)], cwd=desktop.parent, startupinfo=info,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    SESSION_FIELDS["docker_launch_pid"] = process.pid
    record("starting", "docker")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        wait(.75)
    raise StartupError("docker_timeout")


def start_astrbot(docker):
    existing = command([docker, "compose", "ps", "--all", "--quiet", "astrbot"])
    if existing.returncode or not existing.stdout.strip():
        raise StartupError("astrbot_container_missing")
    state = json_command([docker, "inspect", "--format", "{{json .State}}", existing.stdout.strip()])
    if state.get("Running") is not True:
        result = command([docker, "start", existing.stdout.strip()], timeout=60)
        if result.returncode:
            raise StartupError("command_failed")
        state = json_command([docker, "inspect", "--format", "{{json .State}}", existing.stdout.strip()])
    if state.get("Running") is not True:
        raise StartupError("astrbot_not_ready")
    try:
        return datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        raise StartupError("invalid_response") from None


def backend_ready(container_started):
    try:
        status = json.loads(PLUGIN_STATUS.read_text(encoding="utf-8"))
        native = status["services"]["native"]
        # This snapshot is written on startup, not periodically. Bind it to this
        # container boot, then check the actual native endpoint independently.
        if (status.get("terminated") is not False or not native.get("ready")
                or not native.get("enabled")
                or float(status["updated_at_unix"]) < container_started - 2):
            return False
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open("http://127.0.0.1:6198/wechat-native/ws", timeout=2):
                return False
        except urllib.error.HTTPError as error:
            return error.code == 401
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return False


def wait_backend(container_started, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend_ready(container_started):
            return
        wait(.5)
    raise StartupError("astrbot_not_ready")


def open_wechat_if_absent():
    # Do not open another account/session when the client is already present.
    processes = command(["tasklist.exe", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"])
    if processes.returncode or '"weixin.exe"' in processes.stdout.lower():
        return
    candidates = [Path(r"D:\Weixin\Weixin.exe"),
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tencent/Weixin/Weixin.exe"]
    for path in candidates:
        if path.is_file():
            # The client is interactive: the user needs its login window.
            subprocess.Popen([str(path)], cwd=path.parent,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return


def prepare_wechat():
    """Read-only account preparation overlaps Docker startup; no native load."""
    check_cancel()
    state = native_control("status")
    if state.get("process_lock_held") is True:
        return None
    open_wechat_if_absent()
    return native_check()


def ensure_native(login_timeout=900, initial_probe=None):
    state = native_control("status")
    if native_ready(state):
        return state
    if state.get("process_lock_held") is True:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            wait(.5)
            state = native_control("status")
            if native_ready(state):
                return state
            if state.get("process_lock_held") is not True:
                break
        else:
            raise StartupError("bridge_busy")
    open_wechat_if_absent()
    deadline = time.monotonic() + login_timeout
    last_reason = None
    while True:
        check_cancel()
        probe = initial_probe if initial_probe is not None else native_check()
        initial_probe = None
        if probe.get("ready") is True and probe.get("enabled") is True and probe.get("native_module_loaded") is False:
            break
        reason = safe_reason(probe, "bridge_not_ready")
        if reason not in LOGIN_REASONS:
            raise StartupError(reason)
        if reason != last_reason:
            emit(LOGIN_REASONS[reason])
            record("waiting_for_wechat", reason)
            last_reason = reason
        if time.monotonic() >= deadline:
            raise StartupError("login_timeout")
        wait(3)
    # Re-check the host lock immediately before recovery/start. Recovery itself
    # obtains the same bridge lock and redoes the full account/module preflight.
    state = native_control("status")
    if state.get("process_lock_held") is True:
        if native_ready(state):
            return state
        raise StartupError("bridge_busy")
    if state.get("cleanup_required") is True or state.get("manual_recovery_required") is True:
        emit("正在恢复上次退出后的监听状态……")
        native_recover()
    emit("正在连接微信消息监听……")
    started = native_control("start")
    # A launched process is not evidence of a working native connection.
    if started.get("stage") in {"failed", "unknown"}:
        raise StartupError(safe_reason(started, "bridge_not_ready"))
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = native_control("status")
        if native_ready(state):
            return state
        if state.get("stage") in {"failed", "unknown"}:
            raise StartupError(safe_reason(state, "bridge_not_ready"))
        wait(.25)
    raise StartupError("bridge_not_ready")


def open_workbench():
    check_cancel()
    if os.environ.get('ASTRBOT_CONSOLE_EMBEDDED') == '1':
        return
    try:
        os.startfile(WORKBENCH_URL)
    except OSError:
        emit("配置工作台：" + WORKBENCH_URL)


def dashboard_ready():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(DASHBOARD_URL, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_heartbeat(first, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_cancel()
        state = native_control("status")
        if native_ready(state) and state["updated_at"] > first["updated_at"]:
            return state
        if state.get("process_lock_held") is not True or state.get("stage") in {"failed", "unknown"}:
            break
        wait(.25)
    raise StartupError("bridge_heartbeat_stalled")


def start():
    global _STARTED_AT
    _STARTED_AT = time.monotonic()
    docker = validate_local()
    timings = {}
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="wechat-precheck") as pool:
        precheck = pool.submit(prepare_wechat)
        record("starting", "docker")
        emit("[1/4] 正在准备 Docker Desktop……")
        ensure_docker(docker)
        timings["docker_ready"] = round(time.monotonic() - _STARTED_AT, 3)
        record("starting", "astrbot")
        emit("[2/4] 正在启动三叶回复服务……")
        container_started = start_astrbot(docker)
        # Open the configuration UI as soon as HTTP is ready, including while
        # WeChat is waiting for login. Native readiness is checked separately.
        until = time.monotonic() + 120
        while not dashboard_ready():
            if time.monotonic() >= until:
                raise StartupError("astrbot_not_ready")
            wait(.5)
        open_workbench()
        timings["dashboard_ready"] = round(time.monotonic() - _STARTED_AT, 3)
        record("starting", "astrbot", dashboard_url=WORKBENCH_URL,
               dashboard_ready=True, timings=timings.copy())
        wait_backend(container_started)
        record("starting", "wechat_login")
        emit("[3/4] 正在检查机器人微信；未登录时请完成登录，本窗口会自动继续。")
        first = ensure_native(initial_probe=precheck.result())
    timings["native_connected"] = round(time.monotonic() - _STARTED_AT, 3)
    record("starting", "verify_heartbeat")
    emit("[4/4] 正在确认微信监听持续连接……")
    final = wait_heartbeat(first)
    timings["ready"] = round(time.monotonic() - _STARTED_AT, 3)
    record("ready", "wechat_connected", process_lock_held=True, connected=True,
           heartbeat_updated_at=final["updated_at"], timings=timings.copy())
    emit("微信监听已连接，可在原来的 test 群 @机器人或叫她的名字发消息。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="一键启动现有三叶微信机器人")
    parser.add_argument("--check", action="store_true", help="仅检查本地环境，不启动服务")
    parser.add_argument("--console", action="store_true", help="使用命令行控制窗口")
    args = parser.parse_args(argv)
    if args.check:
        try:
            validate_local()
        except (StartupError, BridgeError):
            emit("本地启动环境检查失败。")
            return 1
        emit("本地启动环境完整；此检查未启动任何服务。")
        return 0
    if args.console:
        from scripts.wechat_desktop_console import run_console
        return run_console()
    from scripts.astrbot_launcher import main as gui_main
    return gui_main([])


if __name__ == "__main__":
    raise SystemExit(main())
