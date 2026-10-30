"""Authenticated scheduled-text extension with its own durable dispatch ledger."""
import hashlib
import json
import sqlite3
from .wechat_native_bridge_core import BridgeError, PreparedSend, timestamp, uuid_text

class ScheduledBridge:
    def __init__(self, core, path):
        self.core = core
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS scheduled (id TEXT PRIMARY KEY, task TEXT NOT NULL, account TEXT NOT NULL, room TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL)')
        with self.db:
            self.db.execute("UPDATE scheduled SET status='unknown' WHERE status='in_flight'")

    def prepare(self, frame, now):
        self.core._session(frame)
        if (set(frame) != {'type', 'session', 'request_id', 'task_id', 'room', 'text', 'expires_at'}
                or frame['type'] != 'scheduled_send' or not uuid_text(frame['request_id']) or not uuid_text(frame['task_id'])):
            raise BridgeError('invalid_scheduled_frame')
        def rejected():
            return None, self.core.result(frame['request_id'], 'rejected')
        allowed = {self.core.settings.allowed_room}
        if self.core.settings.private_peer:
            allowed.add(self.core.settings.private_peer)
        if (not self.core.settings.enabled or not isinstance(frame['room'], str) or frame['room'] not in allowed
                or not isinstance(frame['text'], str) or not frame['text'].strip() or len(frame['text']) > 125
                or '\0' in frame['text'] or not timestamp(now) or not timestamp(frame['expires_at'])
                or not now < frame['expires_at'] <= now + 10):
            return rejected()
        digest = hashlib.sha256(json.dumps([frame['task_id'], self.core.settings.expected_self, frame['room'], frame['text']], ensure_ascii=False).encode()).hexdigest()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            prior = self.db.execute('SELECT * FROM scheduled WHERE id=?', (frame['request_id'],)).fetchone()
            if prior is not None:
                if prior['digest'] != digest:
                    return rejected()
                return None, self.core.result(frame['request_id'], 'unknown' if prior['status'] == 'in_flight' else prior['status'])
            count = self.db.execute('SELECT COUNT(*) FROM scheduled WHERE account=? AND created>?', (self.core.settings.expected_self, now - 60)).fetchone()[0]
            if count >= 10:
                return rejected()
            self.db.execute('INSERT INTO scheduled VALUES (?,?,?,?,?,?,?)',
                (frame['request_id'], frame['task_id'], self.core.settings.expected_self, frame['room'], digest, now, 'in_flight'))
        return PreparedSend(frame['request_id'], 'scheduled', frame['room'], frame['text'], frame['expires_at']), None

    def finish(self, prepared, status):
        with self.db:
            count = self.db.execute("UPDATE scheduled SET status=? WHERE id=? AND status='in_flight'", (status, prepared.request_id)).rowcount
        if count != 1:
            raise BridgeError('scheduled_intent_lost')
        return self.core.result(prepared.request_id, status)

    def cancel_before_dispatch(self, prepared):
        return self.finish(prepared, 'rejected')

    def complete(self, prepared, outcome, pid):
        report = outcome.get('report') if isinstance(outcome, dict) else None
        good = (isinstance(report, dict) and type(pid) is int and pid > 0
                and type(outcome.get('pid')) is int and outcome['pid'] == pid
                and type(outcome.get('code')) is int and outcome['code'] == 0
                and type(outcome.get('cleanup_code')) is int and outcome['cleanup_code'] == 0
                and outcome.get('success') is True and report.get('completion_success') is True
                and report.get('target') == prepared.room and report.get('text') == prepared.text)
        return self.finish(prepared, 'native_completed' if good else 'unknown')

    def close(self):
        self.db.close()
