"""AstraChat: native WebView2 window for the local console."""
from __future__ import annotations
import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.wechat_desktop_gui import WindowLease, DATA

URL = 'http://127.0.0.1:5178/'
APP_NAME = 'AstraChat'
APP_ID = 'AstraChat.Desktop.Console'
APP_ICON = ROOT / 'console-ui/public/astrchat-icon.ico'


def configure_window_icon(window):
    # before_show runs on the WinForms UI thread after the native form exists.
    # Setting Form.Icon supplies both WM_GETICON sizes and the taskbar image.
    from System.Drawing import Icon
    window.native.Icon = Icon(str(APP_ICON))


def request(path, data=None):
    req = urllib.request.Request(URL + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'X-Console-Read': '1', 'X-Console-Write': '1', 'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as response:
        return json.load(response)


def ensure_server():
    try:
        health = request('__preview_health')
        if health.get('app') == 'mitsuha-console' and health.get('version', 0) >= 3:
            return
        raise RuntimeError('console_server_version_mismatch')
    except OSError:
        python = ROOT / '.venv/Scripts/pythonw.exe'
        subprocess.Popen([str(python), '-X', 'utf8', str(ROOT / 'scripts/serve_console_preview.py')], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(40):
        time.sleep(.25)
        try:
            if request('__preview_health').get('version', 0) >= 3:
                return
        except OSError:
            pass
    raise RuntimeError('console_server_unavailable')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-hidden', action='store_true', help='Load WebView2 without showing a window or changing robot lifecycle')
    args = parser.parse_args(argv)
    DATA.mkdir(parents=True, exist_ok=True)
    if sys.stderr is None:
        log = (DATA / 'console-window.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = log
    # The old Tk window may still be supervising an existing robot session.
    # Keep service ownership intact while allowing the new console to open.
    lease = None if args.verify_hidden else WindowLease('console-window')
    if lease is not None and not lease.acquire():
        return 0
    try:
        ensure_server()
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        import webview
        window = webview.create_window(APP_NAME, URL + '#/home', width=1280, height=860,
                                       min_size=(760, 540), background_color='#000000', text_select=True,
                                       hidden=args.verify_hidden)
        window.events.before_show += lambda: configure_window_icon(window)
        if args.verify_hidden:
            result = ROOT / 'reports/kouri-launcher-control-20261028/desktop-window-check.json'
            result.parent.mkdir(parents=True, exist_ok=True)
            def loaded_check():
                result.write_text(json.dumps({'loaded': True, 'engine': 'edgechromium', 'hidden': True,
                                               'title': window.title, 'appId': APP_ID,
                                               'iconSize': [window.native.Icon.Width, window.native.Icon.Height],
                                               'iconPath': str(APP_ICON),
                                               'url': URL + '#/home', 'checkedAt': time.time()}), encoding='utf-8')
                window.destroy()
            window.events.loaded += loaded_check
            webview.start(gui='edgechromium', private_mode=False, storage_path=str(DATA / 'console-webview-check'))
            return 0
        def restore_services():
            request('api/launcher', {'action': 'claim', 'ownerPid': os.getpid()})
            request('api/launcher', {'action': 'restore'})
        window.events.loaded += restore_services
        shutting_down = threading.Event()
        may_close = threading.Event()

        def shutdown():
            try:
                request('api/launcher', {'action': 'stop_for_exit'})
                until = time.monotonic() + 180
                while time.monotonic() < until:
                    state = request('api/launcher')
                    if not state.get('active') and state.get('stage') == 'stopped':
                        may_close.set()
                        window.destroy()
                        return
                    time.sleep(.5)
                window.evaluate_js("window.location.hash='/home'")
            except Exception:
                pass
            finally:
                shutting_down.clear()

        def closing():
            if may_close.is_set():
                return True
            if not shutting_down.is_set():
                shutting_down.set()
                threading.Thread(target=shutdown, daemon=True).start()
            return False

        def watch():
            while not may_close.wait(.4):
                show = lease.show_path
                if show.exists():
                    show.unlink(missing_ok=True)
                    window.restore()
                    window.show()
        window.events.closing += closing
        webview.start(watch, gui='edgechromium', private_mode=False, storage_path=str(DATA / 'console-webview'))
    finally:
        if lease is not None:
            lease.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
