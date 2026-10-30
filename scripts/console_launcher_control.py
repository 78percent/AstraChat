"""Shared launcher controls, using the existing serialized desktop supervisor."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'runtime/wechat-native'
PYTHONW = ROOT / '.runtime/wechat-native/.venv/Scripts/pythonw.exe'


def read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def resume_enabled():
    return read(DATA / 'service-resume.json').get('running') is True


def save_resume(running):
    if type(running) is not bool:
        raise ValueError('invalid_resume_state')
    DATA.mkdir(parents=True, exist_ok=True)
    target = DATA / 'service-resume.json'
    temporary = DATA / ('service-resume-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'running': running}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def held(path):
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False


class LauncherControl:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner_pid = os.getpid()
        self.pending = None
        self.pending_until = 0
        self.process = None

    def status(self):
        state = read(DATA / 'desktop-start-status.json')
        active = held(DATA / 'desktop-start.lock')
        if self.pending and time.monotonic() < self.pending_until:
            matching = state.get('session_id') == self.pending
            if not matching or not active:
                result = read(DATA / ('desktop-session-' + self.pending + '.result.json'))
                if result:
                    self.pending = None
                    return {'stage': 'failed', 'message': '启动未完成，请重试。', 'connected': False, 'active': False, 'canStart': True, 'canReconnect': False}
                return {'stage': 'starting', 'message': '正在启动机器人服务……', 'connected': False, 'active': True, 'canStart': False, 'canReconnect': False}
            self.pending = None
        needs_cleanup = not active and state.get('stage') in ('stop_failed', 'failed')
        stage = state.get('stage', 'stopped') if active or needs_cleanup else 'stopped'
        stamp = state.get('heartbeat_updated_at')
        healthy = active and stage == 'ready' and isinstance(stamp, (int, float)) and -2 <= time.time() - stamp <= 20
        if stage == 'ready' and not healthy:
            stage = 'degraded'
        session = state.get('session_id', '')
        if active and isinstance(session, str) and len(session) == 32:
            if (DATA / ('desktop-stop-' + session)).exists():
                stage = 'stopping'
            elif (DATA / ('desktop-reconnect-' + session)).exists():
                stage = 'reconnecting'
        message = state.get('message', '等待启动') if active or needs_cleanup else '机器人已停止'
        if stage == 'stopping':
            message = '正在停止微信监听、机器人服务和 Docker Desktop……'
        elif stage == 'reconnecting':
            message = '正在重新连接微信监听……'
        voice = state.get('voice', {}) if healthy else {}
        voice_states = {'starting', 'ready', 'failed', 'closed'}
        return {'stage': stage, 'message': message, 'connected': healthy, 'active': active,
                'canStart': not active and not needs_cleanup, 'canReconnect': active and stage in ('ready', 'degraded'), 'needsCleanup': needs_cleanup,
                'heartbeatAt': stamp if healthy else None,
                'voice': {'enabled': voice.get('enabled') is True,
                          'asr': voice.get('asr') if voice.get('asr') in voice_states else None,
                          'tts': voice.get('tts') if voice.get('tts') in voice_states else None}}

    def command(self, payload):
        action = payload.get('action')
        with self.lock:
            if action == 'claim':
                pid = payload.get('ownerPid')
                if type(pid) is not int or pid <= 0:
                    raise ValueError('invalid_owner')
                self.owner_pid = pid
                return {'accepted': True}
            state = self.status()
            if action == 'restore':
                if not resume_enabled():
                    return state
                action = 'start'
            closing_window = action == 'stop_for_exit'
            if closing_window:
                action = 'stop'
            if action == 'start' or (action == 'stop' and state.get('needsCleanup')):
                stop_only = action == 'stop'
                if not state['canStart'] and not stop_only:
                    return state
                session = uuid.uuid4().hex
                DATA.mkdir(parents=True, exist_ok=True)
                args = [str(PYTHONW), '-X', 'utf8', '-u', str(ROOT / 'scripts/wechat_desktop_session.py'),
                        '--owner-pid', str(self.owner_pid), '--session-id', session]
                if stop_only:
                    args.append('--stop-only')
                env = dict(os.environ, ASTRBOT_CONSOLE_EMBEDDED='1')
                with (DATA / ('desktop-supervisor-' + session + '.log')).open('ab') as log:
                    self.process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 512))
                self.pending, self.pending_until = session, time.monotonic() + 25
                if action == 'start':
                    save_resume(True)
                elif not closing_window:
                    save_resume(False)
                return self.status()
            if action in ('stop', 'reconnect'):
                current = read(DATA / 'desktop-start-status.json')
                session = self.pending or current.get('session_id', '')
                if not state['active']:
                    return state
                if not isinstance(session, str) or len(session) != 32 or uuid.UUID(session).hex != session:
                    raise ValueError('invalid_session')
                if action == 'reconnect' and not state['canReconnect']:
                    raise ValueError('launcher_busy')
                if action == 'stop':
                    save_resume((state['active'] or resume_enabled()) if closing_window else False)
                (DATA / ('desktop-' + action + '-' + session)).write_text(action + '\n', encoding='ascii')
                return {**state, 'stage': 'stopping' if action == 'stop' else 'reconnecting', 'accepted': True}
            raise ValueError('invalid_action')
