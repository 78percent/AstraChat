"""Read-only console snapshots. Executed via stdin INSIDE the writer container.

No MemoryStore initialization, migrations, model requests or runtime writes.
Only explicit public configuration fields leave this process.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

DATA = Path('/AstrBot/data')
STORES = (('native', '桌面微信', 'native/memory.sqlite3'),
          ('qq', 'QQ', 'memory.sqlite3'),
          ('weixin', '微信 ClawBot', 'weixin/memory.sqlite3'))
NS_FIELDS = ('platform_id', 'kind', 'peer_id', 'persona_id')
NS_WHERE = ' AND '.join(f'{key}=?' for key in NS_FIELDS)
SCHEMA = {
    'memory_namespaces': (*NS_FIELDS, 'epoch', 'summary_revision', 'summary',
                          'summary_source_floor', 'summary_source_watermark',
                          'last_sequence', 'created_at', 'updated_at'),
    'memory_turns': (*NS_FIELDS, 'epoch', 'sequence', 'batch_id', 'persona_revision',
                     'inputs_json', 'replies_json', 'complete', 'created_at'),
}
MAX_SESSIONS = 200
MAX_JSON = 512 * 1024


class SnapshotError(ValueError):
    """Static diagnostics only, never database or private text."""


def bounded_json(path: Path):
    with path.open('rb') as stream:
        value = stream.read(2 * 1024 * 1024 + 1)
    if len(value) > 2 * 1024 * 1024:
        raise SnapshotError('source_too_large')
    result = json.loads(value.decode('utf-8-sig'))
    if not isinstance(result, dict):
        raise SnapshotError('invalid_configuration')
    return result


@contextmanager
def readonly_db(path: Path):
    # URI mode=ro deliberately preserves SQLite's live WAL view. immutable=1 is
    # inappropriate for an actively written database and is never used here.
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 3
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute('BEGIN')
        if (db.execute('PRAGMA application_id').fetchone()[0] != 0x52504D45
                or db.execute('PRAGMA user_version').fetchone()[0] != 1):
            raise SnapshotError('unsupported_memory_schema')
        for table, fields in SCHEMA.items():
            if tuple(row[1] for row in db.execute(f'PRAGMA table_info({table})')) != fields:
                raise SnapshotError('unsupported_memory_schema')
        yield db
    finally:
        db.close()


def session_id(store: str, row) -> str:
    value = json.dumps([store, *[row[k] for k in NS_FIELDS]], ensure_ascii=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


def namespaces(db):
    # Do not read the private summary while enumerating sessions.
    rows = db.execute('SELECT platform_id,kind,peer_id,persona_id,epoch,summary_revision,'
                      'last_sequence,updated_at FROM memory_namespaces '
                      'ORDER BY updated_at DESC LIMIT ?', (MAX_SESSIONS + 1,)).fetchall()
    if len(rows) > MAX_SESSIONS:
        raise SnapshotError('too_many_sessions')
    return rows


def session_meta(db, store, label, row, config):
    where = (*[row[k] for k in NS_FIELDS], row['epoch'])
    count = db.execute(f'SELECT COUNT(*) FROM memory_turns WHERE {NS_WHERE} AND epoch=?', where).fetchone()[0]
    return {
        'id': session_id(store, row), 'channel': store, 'channelLabel': label,
        'kind': row['kind'], 'peerLabel': '••••' + str(row['peer_id'])[-4:],
        'personaId': row['persona_id'], 'activePersona': row['persona_id'] == config.get('persona_id'),
        'epoch': row['epoch'], 'turnCount': count,
        'summaryRevision': row['summary_revision'], 'updatedAt': row['updated_at'],
        'version': f"{row['epoch']}:{row['last_sequence']}:{row['summary_revision']}",
    }


def read_persona(data: Path, config):
    from plugins.astrbot_plugin_roleplay_gateway.persona import load_persona
    root = (data / 'roleplay/personas').resolve()
    filename = config.get('persona_filename', '')
    if not isinstance(filename, str) or not filename or '/' in filename or '\\' in filename:
        raise SnapshotError('invalid_persona_path')
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or not root.is_relative_to(data.resolve()):
        raise SnapshotError('invalid_persona_path')
    persona = load_persona(path, persona_id=config.get('persona_id'), name=config.get('persona_name'))
    if persona.revision != config.get('persona_sha256'):
        raise SnapshotError('persona_revision_mismatch')
    return {'id': persona.persona_id, 'name': persona.name, 'revision': persona.revision,
            'prompt': persona.prompt, 'examples': [asdict(item) for item in persona.examples],
            'format': persona.report['source_format']}


def safe_config(data: Path, config):
    from plugins.astrbot_plugin_roleplay_gateway.context_budget import ContextBudget
    from plugins.astrbot_plugin_roleplay_gateway.model_profiles import public_profiles, selected, profile_models
    providers = bounded_json(data / 'cmd_config.json').get('provider', [])
    provider = next((p for p in providers if isinstance(p, dict)
                     and p.get('id') == config.get('provider_id')), {})
    pacing = config.get('reply_pacing', {})
    allowed = ('enabled', 'first_delay_min', 'first_delay_max', 'interval_min', 'interval_max',
               'max_bubbles', 'max_characters', 'cooldown_seconds', 'window_seconds',
               'max_rounds', 'probability')
    profiles = public_profiles(config, data)
    return {
        'mode': config.get('mode'), 'providerId': 'kouri-chat' if profiles['active'] == 'kouri' else config.get('provider_id'),
        'model': (profile_models(config)[selected(config)] if config.get('console_profiles')
                  else config.get('console_model') or provider.get('model')),
        'providerEnabled': profiles['kouri']['keyConfigured'] if profiles['active'] == 'kouri' else provider.get('enable') is True,
        'modelProfiles': profiles,
        'channels': {key: config.get(field) is True for key, field in
                     (('qq', 'enabled'), ('native', 'native_enabled'), ('weixin', 'wechat_enabled'))},
        'searchModel': config.get('console_search_model', ''),
        'imageModel': config.get('console_image_model', ''),
        'onlineLookupEnabled': config.get('online_lookup', {}).get('enabled') is True,
        'pacing': {k: pacing[k] for k in allowed if isinstance(pacing.get(k), (bool, int, float))},
        'nativeReplyProbability': config.get('native_reply_probability'),
        # The deployed roleplay path calls build_prompt with these defaults.
        'contextBudget': {**asdict(ContextBudget()), 'recent_turns': config.get('console_context_limit', 12)},
    }


def parse_messages(encoded, reply=False):
    if not isinstance(encoded, str) or len(encoded) > MAX_JSON:
        raise SnapshotError('invalid_memory_record')
    try:
        rows = json.loads(encoded)
    except (ValueError, RecursionError):
        raise SnapshotError('invalid_memory_record') from None
    if not isinstance(rows, list) or len(rows) > 200:
        raise SnapshotError('invalid_memory_record')
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str):
            raise SnapshotError('invalid_memory_record')
        item = {'text': row['text'], 'edited': row.get('edited') is True}
        if reply:
            status = row.get('delivery_status', 'SENT')
            if status not in ('SENT', 'API_ACCEPTED'):
                raise SnapshotError('invalid_delivery_status')
            item['deliveryStatus'] = status
        else:
            name = row.get('display_name')
            item['displayName'] = ('用户 ••••' + str(row['sender_id'])[-4:]
                                   if name and name == row.get('sender_id') else
                                   name[:200] if isinstance(name, str) and name else '用户')
        result.append(item)
    return result


def detail(db, store, label, row, config, before, expected_epoch, limit):
    if row['epoch'] != expected_epoch:
        raise SnapshotError('session_changed')
    values = (*[row[k] for k in NS_FIELDS], row['epoch'])
    records = db.execute(f'SELECT sequence,persona_revision,inputs_json,replies_json,complete,created_at '
                         f'FROM memory_turns WHERE {NS_WHERE} AND epoch=? AND sequence<? '
                         'ORDER BY sequence DESC LIMIT ?', (*values, before, limit + 1)).fetchall()
    has_more = len(records) > limit
    records = records[:limit]
    summary = db.execute(f'SELECT summary,summary_source_watermark FROM memory_namespaces WHERE {NS_WHERE}', values[:4]).fetchone()
    if len(summary['summary']) > MAX_JSON:
        raise SnapshotError('source_too_large')
    turns = [{'sequence': r['sequence'], 'personaRevision': r['persona_revision'],
              'inputs': parse_messages(r['inputs_json']), 'replies': parse_messages(r['replies_json'], True),
              'complete': bool(r['complete']), 'createdAt': r['created_at']} for r in reversed(records)]
    from plugins.astrbot_plugin_roleplay_gateway.console_memory import namespace_key
    from plugins.astrbot_plugin_roleplay_gateway.memory_store import Namespace
    prefix = namespace_key(Namespace(*values[:4]))
    db_path = Path(db.execute('PRAGMA database_list').fetchone()[2])
    backups = sorted((db_path.parent / 'console-backups').glob(prefix + '-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    return {'session': session_meta(db, store, label, row, config), 'summary': summary['summary'],
            'backups': [{'id': p.stem, 'createdAt': p.stat().st_mtime} for p in backups],
            'summarySourceWatermark': summary['summary_source_watermark'], 'turns': turns,
            'nextBefore': records[-1]['sequence'] if records and has_more else None,
            'fetchedAt': time.time()}


def collect(operation='bootstrap', identifier='', before=2**63-1, epoch=0, limit=20, data=DATA):
    if operation not in ('bootstrap', 'session') or type(limit) is not int or not 1 <= limit <= 40:
        raise SnapshotError('invalid_request')
    if operation == 'session' and (not re.fullmatch('[a-f0-9]{32}', identifier)
                                    or not 1 <= before <= 2**63-1 or epoch < 0):
        raise SnapshotError('invalid_request')
    config = bounded_json(data / 'config/astrbot_plugin_roleplay_gateway_config.json')
    memory_root = data / 'plugin_data/astrbot_plugin_roleplay_gateway'
    sessions, sources = [], []
    for store, label, relative in STORES:
        path = memory_root / relative
        if not path.is_file():
            sources.append({'channel': store, 'label': label, 'status': 'missing'})
            continue
        try:
            with readonly_db(path) as db:
                rows = namespaces(db)
                if operation == 'session':
                    for row in rows:
                        if session_id(store, row) == identifier:
                            return detail(db, store, label, row, config, before, epoch, limit)
                else:
                    sessions.extend(session_meta(db, store, label, row, config) for row in rows)
                sources.append({'channel': store, 'label': label, 'status': 'ready'})
        except sqlite3.Error:
            raise SnapshotError('memory_read_failed') from None
    if operation == 'session':
        raise SnapshotError('session_not_found')
    from plugins.astrbot_plugin_roleplay_gateway.console_admin import config_version
    return {'schemaVersion': 1, 'fetchedAt': time.time(), 'readOnly': False, 'configVersion': config_version(config),
            'persona': read_persona(data, config), 'config': safe_config(data, config),
            'sessions': sessions, 'sources': sources}


def main():
    sys.path.insert(0, str(DATA))
    try:
        args = sys.argv[1:]
        if args == ['bootstrap']:
            result = collect()
        elif len(args) == 5 and args[0] == 'session':
            result = collect(args[0], args[1], int(args[2]), int(args[3]), int(args[4]))
        else:
            raise SnapshotError('invalid_request')
        encoded = json.dumps({'ok': True, 'data': result}, ensure_ascii=True, allow_nan=False)
        if len(encoded) > 8 * 1024 * 1024:
            raise SnapshotError('source_too_large')
        print(encoded)
    except SnapshotError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
    except Exception:
        # Do not forward paths, SQL, configuration or exception strings.
        print(json.dumps({'ok': False, 'error': 'snapshot_unavailable'}))


if __name__ == '__main__':
    main()
