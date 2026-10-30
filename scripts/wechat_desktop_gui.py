"""One console-free, persistent control window for the local WeChat session."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk
import uuid

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "runtime/wechat-native"
PYTHONW = ROOT / ".runtime/wechat-native/.venv/Scripts/pythonw.exe"
WORKBENCH = "http://127.0.0.1:6185/#/extension/plugins/astrbot_plugin_roleplay_gateway?open_config=astrbot_plugin_roleplay_gateway"
TITLE = "三叶微信"


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def session_available():
    """Probe the shared lifetime lock without taking ownership of its services."""
    import msvcrt
    try:
        with (DATA / "desktop-start.lock").open("a+b") as handle:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return True
    except OSError:
        return False


class WindowLease:
    """The UI lease includes the entire shutdown; repeated clicks raise it."""
    def __init__(self, name="desktop-window"):
        self.handle = None
        self.lock_path = DATA / (name + ".lock")
        self.show_path = DATA / (name + ".show")

    def acquire(self):
        import msvcrt
        DATA.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_path.open("a+b")
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            if self.lock_path.stat().st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            self.show_path.touch()
            return False

    def close(self):
        if self.handle:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


class ControlWindow:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.session_id = None
        self.stopping = False
        self.finished = False
        self.stage = "starting"
        self.started = time.monotonic()
        self.timer = None
        self.waiting_for_session = False
        self.waiting_stop_only = False
        self.reconnect_pending = False
        root.title(TITLE)
        root.geometry("600x350")
        root.minsize(530, 330)
        root.configure(background="#f5f7fb")
        root.protocol("WM_DELETE_WINDOW", self.close_requested)
        root.option_add("*Font", ("Microsoft YaHei UI", 11))
        style = ttk.Style(root)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure("TButton", padding=(12, 10))
        body = tk.Frame(root, background="#f5f7fb", padx=28, pady=24)
        body.pack(fill="both", expand=True)
        self.title = tk.StringVar(value="正在启动")
        tk.Label(body, textvariable=self.title, font=("Microsoft YaHei UI", 20, "bold"),
                 background="#f5f7fb", foreground="#17243b", anchor="w").pack(fill="x")
        self.message = tk.StringVar(value="正在连接服务控制进程……")
        tk.Label(body, textvariable=self.message, background="#f5f7fb", foreground="#334155",
                 anchor="nw", justify="left", wraplength=520, height=3).pack(fill="x", pady=(14, 8))
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))
        self.progress.start(20)
        self.detail = tk.StringVar(value="")
        tk.Label(body, textvariable=self.detail, background="#f5f7fb", foreground="#475569",
                 anchor="w", justify="left").pack(fill="x")
        buttons = tk.Frame(body, background="#f5f7fb")
        buttons.pack(side="bottom", fill="x", pady=(20, 0))
        self.settings = ttk.Button(buttons, text="微信机器人设置", command=self.open_settings, state="disabled")
        self.settings.pack(side="left")
        self.reconnect = ttk.Button(buttons, text="重新连接", command=self.reconnect_requested, state="disabled")
        self.reconnect.pack(side="left", padx=10)
        self.stop = ttk.Button(buttons, text="停止并退出", command=self.close_requested)
        self.stop.pack(side="right")
        self.start_timer = root.after(100, self.begin)

    def begin(self, stop_only=False):
        if self.start_timer:
            self.root.after_cancel(self.start_timer)
            self.start_timer = None
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.session_id = uuid.uuid4().hex
        self.result_file = DATA / ("desktop-session-" + self.session_id + ".result.json")
        self.finished = False
        self.exit_seen = False
        self.waiting_for_session = False
        self.waiting_stop_only = stop_only
        self.reconnect_pending = False
        self.process = None
        self.stage = "stopping" if stop_only else "starting"
        self.stopping = stop_only
        self.started = time.monotonic()
        self.title.set("正在关闭" if stop_only else "正在启动")
        self.message.set("正在重新清理残留服务……" if stop_only else "正在连接服务控制进程……")
        self.detail.set("")
        self.settings.configure(state="disabled")
        self.reconnect.configure(state="disabled")
        self.stop.configure(text="正在关闭" if stop_only else "停止并退出",
                            state="disabled" if stop_only else "normal")
        self.progress.start(20)
        args = [str(PYTHONW), "-X", "utf8", "-u", str(ROOT / "scripts/wechat_desktop_session.py"),
                "--owner-pid", str(os.getpid()), "--session-id", self.session_id]
        if stop_only:
            args.append("--stop-only")
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            with (DATA / ("desktop-supervisor-" + self.session_id + ".log")).open("ab") as log:
                self.process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                  getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512))
        except OSError:
            self.fail("控制进程无法启动。", "请检查项目中的 Python 环境。")
        self.timer = self.root.after(200, self.tick)

    def open_settings(self):
        os.startfile(WORKBENCH)

    def reconnect_requested(self):
        if self.finished or self.stopping or self.waiting_for_session or self.reconnect_pending or not self.session_id:
            return
        try:
            (DATA / ("desktop-reconnect-" + self.session_id)).write_text("reconnect\n", encoding="ascii")
        except OSError:
            self.message.set("无法提交重连请求，请检查项目目录是否可写后重试。")
            return
        self.reconnect_pending = True
        self.progress.start(20)
        self.reconnect.configure(state="disabled")
        self.title.set("正在重新连接")
        self.message.set("正在检查配置和微信监听……")

    def close_requested(self):
        if not self.session_id or self.waiting_for_session:
            if self.start_timer:
                self.root.after_cancel(self.start_timer)
                self.start_timer = None
            # A rejected busy session owns no services. Its previous owner
            # remains responsible for completing that session's shutdown.
            self.root.destroy()
            return
        if self.finished:
            if self.stage == "stopped":
                self.root.destroy()
            else:
                self.title.set("正在关闭")
                self.message.set("正在重新清理残留服务……")
                self.progress.start(20)
                self.stop.configure(text="正在关闭", state="disabled")
                self.begin(stop_only=True)
            return
        if self.stopping:
            return
        try:
            (DATA / ("desktop-stop-" + self.session_id)).write_text("stop\n", encoding="ascii")
        except OSError:
            self.message.set("无法提交关闭请求，请检查项目目录是否可写后重试。")
            self.stop.configure(text="重试关闭", state="normal")
            return
        self.stopping = True
        self.reconnect_pending = False
        self.title.set("正在关闭")
        self.message.set("正在停止微信监听、机器人服务和 Docker Desktop……")
        self.progress.start(20)
        self.stop.configure(text="正在关闭", state="disabled")
        self.reconnect.configure(state="disabled")

    def fail(self, message, detail=""):
        self.finished = True
        self.stage = "failed"
        self.title.set("需要处理")
        self.message.set(message)
        self.detail.set(detail)
        self.progress.stop()
        self.settings.configure(state="disabled")
        self.stop.configure(text="重试关闭", state="normal")
        self.reconnect.configure(state="disabled")

    def callback_failed(self, exception_type, _error, _traceback):
        # tkinter normally only writes a traceback; pythonw has no terminal.
        # Keep a static diagnostic and request the same orderly shutdown path.
        print("GUI callback failed: " + exception_type.__name__, file=sys.stderr, flush=True)
        try:
            self.close_requested()
            if self.root.winfo_exists():
                self.message.set("控制窗口遇到异常，正在正常停止服务……")
                if self.timer is None:
                    self.timer = self.root.after(250, self.tick)
        except Exception:
            # If Tk itself is unusable, process exit is still observed by the
            # detached console-free supervisor's retained owner handle.
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def render(self, state):
        if self.stopping and state.get("stage") != "stopping":
            # A request precedes the supervisor's next snapshot; never repaint
            # an old ready snapshot over the user's pending shutdown.
            self.title.set("正在关闭")
            self.detail.set("已等待 %d 秒" % max(0, int(time.monotonic() - self.started)))
            return
        if self.reconnect_pending:
            pending = DATA / ("desktop-reconnect-" + self.session_id)
            if pending.exists():
                return
            self.reconnect_pending = False
        if state.get("stage") == "ready":
            heartbeat = state.get("heartbeat_updated_at")
            if (not isinstance(heartbeat, (int, float)) or
                    not -2 <= time.time() - heartbeat <= 20):
                state = {**state, "stage": "degraded", "connected": False,
                         "message": "微信监听状态已过期，点击“重新连接”检查。"}
        self.stage = state.get("stage", "starting")
        titles = {"starting": "正在启动", "waiting_for_wechat": "请登录机器人微信",
                  "ready": "微信监听已连接", "degraded": "微信监听已断开",
                  "reconnecting": "正在重新连接", "stopping": "正在关闭"}
        self.title.set(titles.get(self.stage, "正在处理"))
        self.message.set(state.get("message") or "正在检查服务状态……")
        self.settings.configure(state="normal" if state.get("dashboard_ready") and not self.stopping else "disabled")
        self.reconnect.configure(state="normal" if self.stage in {"ready", "degraded"} and not self.stopping else "disabled")
        if self.stage == "stopping":
            self.stopping = True
            self.stop.configure(text="正在关闭", state="disabled")
        if self.stage in {"ready", "degraded"}:
            self.progress.stop()
            voice = state.get("voice", {})
            if self.stage == "ready" and voice.get("enabled"):
                names = {"starting": "预热中", "ready": "就绪", "failed": "不可用", "closed": "已关闭"}
                self.detail.set("语音识别：%s　声音合成：%s" %
                                (names.get(voice.get("asr"), "未启用"), names.get(voice.get("tts"), "未启用")))
            else:
                self.detail.set("保存设置后，点击“重新连接”使微信监听恢复。")
        else:
            self.progress.start(20)
            elapsed = max(0, int(time.monotonic() - self.started))
            self.detail.set("已等待 %d 秒" % elapsed)

    def tick(self):
        self.timer = None
        try:
            show = DATA / "desktop-window.show"
            if show.exists():
                show.unlink()
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.root.after(200, lambda: self.root.attributes("-topmost", False))
            if not self.finished:
                if self.waiting_for_session:
                    if session_available():
                        self.begin(stop_only=self.waiting_stop_only)
                        return
                    state = read_json(DATA / "desktop-start-status.json")
                    shutting_down = state.get("stage") == "stopping"
                    self.title.set("等待上次服务关闭" if shutting_down else "已有控制进程运行")
                    self.message.set("上次关闭完成后将自动继续。" if shutting_down else
                                     "请先关闭原来的启动窗口；关闭完成后这里会自动继续。")
                    self.timer = self.root.after(500, self.tick)
                    return
                result = read_json(self.result_file)
                if result:
                    if result.get("stage") == "stopped":
                        self.finished, self.stage = True, "stopped"
                        self.title.set("全部服务已停止")
                        self.message.set("微信监听、机器人服务和 Docker Desktop 已退出。")
                        self.progress.stop()
                        reason = result.get("startup_error")
                        if reason and reason != "startup_cancelled":
                            self.detail.set("启动失败：" + str(reason))
                            self.stop.configure(text="关闭", state="normal")
                        else:
                            self.root.after(800, self.root.destroy)
                    elif result.get("stage") == "busy":
                        self.waiting_for_session = True
                        self.stage = "waiting_for_session"
                        self.title.set("等待上次服务关闭")
                        self.message.set("另一个控制进程仍在运行，关闭完成后将自动继续。")
                        self.settings.configure(state="disabled")
                        self.reconnect.configure(state="disabled")
                        self.stop.configure(text="关闭窗口", state="normal")
                    else:
                        self.fail("服务尚未完全停止。", "错误代码：" + str(result.get("reason", "unknown")))
                elif self.process is not None and self.process.poll() is not None:
                    # Re-read on the next tick to cover the result-write/exit edge.
                    if getattr(self, "exit_seen", False):
                        self.fail("后台控制进程异常退出，服务可能仍在运行。", "点击“重试关闭”完成清理。")
                    self.exit_seen = True
                else:
                    state = read_json(DATA / "desktop-start-status.json")
                    if state.get("session_id") == self.session_id:
                        self.render(state)
            self.timer = self.root.after(250, self.tick)
        except (OSError, tk.TclError):
            try:
                if self.root.winfo_exists():
                    self.timer = self.root.after(500, self.tick)
            except tk.TclError:
                pass


def main():
    if sys.stdout is None or sys.stderr is None:
        DATA.mkdir(parents=True, exist_ok=True)
        log = (DATA / "desktop-gui.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
    lease = WindowLease()
    if not lease.acquire():
        return 0
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        root = tk.Tk()
        window = ControlWindow(root)
        root.report_callback_exception = window.callback_failed
        root.mainloop()
    finally:
        lease.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
