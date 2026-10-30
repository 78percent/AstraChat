"""Bounded host bridge; the live SQLite files are never opened on Windows."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import parse_qs

CONTAINER = 'astrbot-roleplay-astrbot-1'
READER = Path(__file__).with_name('console_container_snapshot.py')
GATE = threading.BoundedSemaphore(2)
ERROR_STATUS = {'session_not_found': 404, 'session_changed': 409, 'invalid_request': 400}


def manage_live(payload):
    if not isinstance(payload, dict) or payload.get('op') not in ('status', 'models', 'model_key', 'model_test', 'personas', 'context', 'settings', 'persona_save', 'persona_select', 'persona_read', 'persona_delete', 'features'):
        raise BridgeError('invalid_request', 400)
    script = READER.with_name('console_runtime_request.py').read_text(encoding='utf-8')
    try:
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, 'CREATE_NO_WINDOW') else {}
        result = subprocess.run(['docker', 'exec', '-i', CONTAINER, 'python', '-c', script],
                                input=json.dumps(payload, ensure_ascii=True).encode(), capture_output=True, timeout=55, **options)
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise BridgeError('management_unavailable')
        value = json.loads(result.stdout)
        if not value.get('ok'):
            code = value.get('error', '')
            code = code if isinstance(code, str) and re.fullmatch('[a-z_]{1,100}', code) else 'management_failed'
            raise BridgeError(code, 409 if code == 'revision_conflict' else 400 if code.startswith('invalid_') else 503)
        return value['data']
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise BridgeError('management_unavailable') from None


class BridgeError(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status


def request_args(path, query):
    try:
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise BridgeError('invalid_request', 400) from None
    if path == '/api/live/bootstrap' and not params:
        return ['bootstrap']
    if path != '/api/live/session':
        raise BridgeError('not_found', 404)
    if set(params) - {'id', 'before', 'epoch', 'limit'} or any(len(v) != 1 for v in params.values()):
        raise BridgeError('invalid_request', 400)
    identifier = params.get('id', [''])[0]
    if not re.fullmatch('[a-f0-9]{32}', identifier):
        raise BridgeError('invalid_request', 400)
    numbers = []
    for key, default, low, high in [('before', str(2**63-1), 1, 2**63-1),
                                    ('epoch', '', 0, 2**31-1), ('limit', '20', 1, 40)]:
        value = params.get(key, [default])[0]
        if not re.fullmatch('[0-9]{1,19}', value) or not low <= int(value) <= high:
            raise BridgeError('invalid_request', 400)
        numbers.append(str(int(value)))
    return ['session', identifier, *numbers]


def fetch_live(path, query):
    args = request_args(path, query)
    if not GATE.acquire(blocking=False):
        raise BridgeError('reader_busy', 503)
    try:
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, 'CREATE_NO_WINDOW') else {}
        result = subprocess.run(['docker', 'exec', '-i', CONTAINER, 'python', '-', *args],
                                input=READER.read_bytes(), capture_output=True, timeout=12, **options)
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise BridgeError('runtime_unavailable')
        try:
            payload = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise BridgeError('invalid_snapshot') from None
        if not isinstance(payload, dict) or not payload.get('ok'):
            code = payload.get('error') if isinstance(payload, dict) else None
            # Only static error codes emitted by our reader are allowed through.
            allowed = {*ERROR_STATUS, 'source_too_large', 'invalid_configuration',
                       'unsupported_memory_schema', 'too_many_sessions', 'invalid_persona_path',
                       'persona_revision_mismatch', 'invalid_memory_record', 'invalid_delivery_status',
                       'memory_read_failed', 'snapshot_unavailable'}
            code = code if code in allowed else 'invalid_snapshot'
            raise BridgeError(code, ERROR_STATUS.get(code, 503))
        return payload['data']
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        raise BridgeError('runtime_unavailable') from None
    finally:
        GATE.release()
