"""Console reader contracts. Synthetic data only; never open live host SQLite."""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import console_container_snapshot as reader
import console_data_bridge as bridge
from serve_console_preview import PreviewHandler, ThreadingHTTPServer
from plugins.astrbot_plugin_roleplay_gateway.memory_store import MemoryStore


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='console-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        (self.data / 'config').mkdir()
        (self.data / 'roleplay/personas').mkdir(parents=True)
        self.prompt = '中性测试角色，喜欢天文学。'
        (self.data / 'roleplay/personas/persona.txt').write_text(self.prompt, encoding='utf-8')
        self.config = {'persona_filename': 'persona.txt', 'persona_id': 'fixture-role',
                       'persona_name': '星星', 'persona_sha256': hashlib.sha256(self.prompt.encode()).hexdigest(),
                       'provider_id': 'fixture-model', 'native_bridge_token': 'SECRET_CANARY',
                       'native_self_id': 'PRIVATE_SELF', 'allowed_group_ids': ['PRIVATE_GROUP'],
                       'enabled': True, 'native_enabled': True, 'reply_pacing': {'probability': 0.6, 'secret': 'SECRET_CANARY'}}
        self.write_config()
        (self.data / 'cmd_config.json').write_text(json.dumps({'provider': [
            {'id': 'fixture-model', 'model': 'test-model', 'enable': True, 'api_key': 'SECRET_CANARY'}]}), encoding='utf-8-sig')
        self.path = self.data / 'plugin_data/astrbot_plugin_roleplay_gateway/native/memory.sqlite3'
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            MemoryStore._migrate(db)
            for peer in ('peer-A-private', 'peer-B-private'):
                values = ('platform-A', 'group', peer, 'fixture-role')
                db.execute('INSERT INTO memory_namespaces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (*values, 1, 2, '测试摘要 ' + peer[-7:], 0, 1, 4, 1.0, 2.0))
                for epoch, sequence in ((0, 1), (1, 1), (1, 2), (1, 3), (1, 4)):
                    inputs = json.dumps([{'message_id': 'PRIVATE_ID', 'sender_id': 'PRIVATE_SENDER',
                                          'display_name': '测试者', 'text': f'epoch {epoch} turn {sequence}'}])
                    replies = json.dumps([{'sequence': 0, 'text': '你好', 'message_id': 'PRIVATE_ID'},
                                           {'sequence': 1, 'text': '再见', 'delivery_status': 'API_ACCEPTED', 'delivery_ref': 'PRIVATE_REF'}])
                    db.execute('INSERT INTO memory_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (*values, epoch, sequence, f'b-{sequence}', 'revision', inputs, replies, 1, 3.0))

    def write_config(self):
        (self.data / 'config/astrbot_plugin_roleplay_gateway_config.json').write_text(json.dumps(self.config), encoding='utf-8-sig')

    def bootstrap(self):
        return reader.collect(data=self.data)

    def test_metadata_excludes_secrets_and_personal_identifiers(self):
        data = self.bootstrap()
        text = json.dumps(data)
        for forbidden in ('SECRET_CANARY', 'PRIVATE_SELF', 'PRIVATE_GROUP', 'peer-A-private', 'peer-B-private', 'api_key', 'native_bridge_token'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(data['persona']['prompt'], self.prompt)
        self.assertEqual(data['config']['model'], 'test-model')
        self.assertEqual([s['turnCount'] for s in data['sessions']], [4, 4])
        self.assertEqual(len({s['id'] for s in data['sessions']}), 2)
        self.assertEqual([s['status'] for s in data['sources']], ['ready', 'missing', 'missing'])

    def test_current_epoch_namespace_and_cursor_isolation(self):
        s = self.bootstrap()['sessions'][0]
        d = reader.collect('session', s['id'], epoch=1, limit=2, data=self.data)
        self.assertEqual([t['sequence'] for t in d['turns']], [3, 4])
        self.assertTrue(all('epoch 1' in t['inputs'][0]['text'] for t in d['turns']))
        self.assertEqual(d['nextBefore'], 3)
        older = reader.collect('session', s['id'], before=d['nextBefore'], epoch=1, limit=2, data=self.data)
        self.assertEqual([t['sequence'] for t in older['turns']], [1, 2])
        self.assertIsNone(older['nextBefore'])
        self.assertEqual(d['session']['id'], s['id'])

    def test_delivery_evidence_is_not_upgraded_and_ids_are_excluded(self):
        s = self.bootstrap()['sessions'][0]
        d = reader.collect('session', s['id'], epoch=1, data=self.data)
        self.assertEqual([r['deliveryStatus'] for r in d['turns'][0]['replies']], ['SENT', 'API_ACCEPTED'])
        for forbidden in ('PRIVATE_ID', 'PRIVATE_SENDER', 'PRIVATE_REF', 'sender_id', 'message_id'):
            self.assertNotIn(forbidden, json.dumps(d))

    def test_reads_never_change_database_and_sqlite_refuses_writes(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        self.bootstrap()
        with reader.readonly_db(self.path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM memory_turns')
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())

    def test_epoch_change_and_unknown_session_fail_closed(self):
        s = self.bootstrap()['sessions'][0]
        with self.assertRaisesRegex(reader.SnapshotError, '^session_changed$'):
            reader.collect('session', s['id'], epoch=0, data=self.data)
        with self.assertRaisesRegex(reader.SnapshotError, '^session_not_found$'):
            reader.collect('session', '0' * 32, epoch=1, data=self.data)

    def test_bad_schema_and_json_do_not_leak_records(self):
        s = self.bootstrap()['sessions'][0]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE memory_turns SET inputs_json='SECRET_BROKEN_JSON'")
        with self.assertRaisesRegex(reader.SnapshotError, '^invalid_memory_record$'):
            reader.collect('session', s['id'], epoch=1, data=self.data)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(reader.SnapshotError, '^unsupported_memory_schema$'):
            self.bootstrap()

    def test_persona_revision_and_path_validation(self):
        self.config['persona_sha256'] = '0' * 64
        self.write_config()
        with self.assertRaisesRegex(reader.SnapshotError, '^persona_revision_mismatch$'):
            self.bootstrap()
        for filename in ('../secret.txt', '/secret.txt', '..\\secret.txt'):
            self.config['persona_filename'] = filename
            self.write_config()
            with self.assertRaisesRegex(reader.SnapshotError, '^invalid_persona_path$'):
                self.bootstrap()

    def test_limits_and_malformed_rows(self):
        for limit in (0, 41, True):
            with self.assertRaisesRegex(reader.SnapshotError, '^invalid_request$'):
                reader.collect(limit=limit, data=self.data)
        for value in ('{}', '[{}]', '[{"text":0}]', 'x' * (reader.MAX_JSON + 1)):
            with self.assertRaisesRegex(reader.SnapshotError, '^invalid_memory_record$'):
                reader.parse_messages(value)
        with self.assertRaisesRegex(reader.SnapshotError, '^invalid_delivery_status$'):
            reader.parse_messages('[{"text":"ok","delivery_status":"READ"}]', True)


class BridgeTests(unittest.TestCase):
    def test_query_bounds_duplicate_and_unknown_parameters(self):
        identifier = 'a' * 32
        self.assertEqual(bridge.request_args('/api/live/bootstrap', ''), ['bootstrap'])
        self.assertEqual(bridge.request_args('/api/live/session', f'id={identifier}&epoch=0&limit=2&before=3'), ['session', identifier, '3', '0', '2'])
        for query in (f'id={identifier}', f'id={identifier}&epoch=0&limit=41', f'id={identifier}&epoch=0&id={identifier}',
                      f'id={identifier}&epoch=0&path=/tmp', 'id=../../secret&epoch=0', f'id={identifier}&epoch=0&before=-1'):
            with self.assertRaises(bridge.BridgeError):
                bridge.request_args('/api/live/session', query)

    def test_subprocess_timeout_is_static_error(self):
        with patch.object(bridge.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret', 12)):
            with self.assertRaises(bridge.BridgeError) as exc:
                bridge.fetch_live('/api/live/bootstrap', '')
        self.assertEqual(exc.exception.code, 'runtime_unavailable')

    def test_reader_is_fixed_container_stdin_no_shell(self):
        result = subprocess.CompletedProcess([], 0, b'{"ok":true,"data":{"readOnly":true}}', b'')
        with patch.object(bridge.subprocess, 'run', return_value=result) as run:
            self.assertEqual(bridge.fetch_live('/api/live/bootstrap', ''), {'readOnly': True})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['docker', 'exec', '-i', bridge.CONTAINER, 'python', '-', 'bootstrap'])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['timeout'], 12)
        self.assertIn(b'mode=ro', kwargs['input'])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), PreviewHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, headers=None, method='GET', path='/api/live/bootstrap'):
        with closing(http.client.HTTPConnection('127.0.0.1', self.server.server_port)) as conn:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def test_same_origin_read_and_no_store(self):
        with patch('serve_console_preview.fetch_live', return_value={'readOnly': True}) as fetch:
            status, headers, data = self.request({'Host': '127.0.0.1:5178', 'X-Console-Read': '1', 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {'readOnly': True})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        fetch.assert_called_once()

    def test_foreign_sites_rebinding_and_missing_custom_header_rejected_before_read(self):
        base = {'Host': '127.0.0.1:5178', 'X-Console-Read': '1'}
        cases = ({'Host': '127.0.0.1:5178'}, {**base, 'Host': 'evil.test:5178'},
                 {**base, 'Origin': 'https://evil.test'}, {**base, 'Sec-Fetch-Site': 'cross-site'},
                 {**base, 'Origin': 'null'}, {**base, 'Sec-Fetch-Site': 'same-site'})
        with patch('serve_console_preview.fetch_live') as fetch:
            for headers in cases:
                self.assertEqual(self.request(headers)[0], 403)
        fetch.assert_not_called()

    def test_error_and_non_get_have_no_write_path(self):
        headers = {'Host': '127.0.0.1:5178', 'X-Console-Read': '1'}
        with patch('serve_console_preview.fetch_live', side_effect=bridge.BridgeError('runtime_unavailable')):
            status, _, data = self.request(headers)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(data), {'error': 'runtime_unavailable'})
        with patch('serve_console_preview.fetch_live') as fetch:
            self.assertEqual(self.request(headers, 'POST')[0], 403)
            for method in ('PUT', 'DELETE', 'PATCH', 'OPTIONS'):
                self.assertEqual(self.request(headers, method)[0], 501)
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
