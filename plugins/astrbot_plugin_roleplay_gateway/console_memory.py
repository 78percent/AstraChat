"""Operator context edits. Original values are retained in local recovery files.

Called on the owning MemoryStore connection inside the AstrBot process. Epoch
changes fence pending model work; the service updates its in-memory epoch too.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
import uuid

from .memory_store import _NS_WHERE, _transaction
from .context_budget import estimate_tokens, ContextBudget


def namespace_key(namespace):
    return hashlib.sha256(json.dumps(namespace.values, ensure_ascii=True).encode()).hexdigest()[:32]


def version(row):
    return f"{row['epoch']}:{row['last_sequence']}:{row['summary_revision']}"


def change(store, namespace, payload):
    action = payload.get('action')
    if action not in ('edit', 'delete_message', 'edit_group', 'delete_group', 'delete_turn', 'clear', 'summary', 'restore', 'backup'):
        raise ValueError('invalid_action')
    with store._lock:
        db = store._db()
        with _transaction(db):
            row = db.execute(f'SELECT * FROM memory_namespaces WHERE {_NS_WHERE}', namespace.values).fetchone()
            if row is None or payload.get('version') != version(row):
                raise ValueError('revision_conflict')
            original = [dict(r) for r in db.execute(
                f'SELECT * FROM memory_turns WHERE {_NS_WHERE} AND epoch=? ORDER BY sequence',
                (*namespace.values, row['epoch']))]
            if len(original) > 2000:
                raise ValueError('context_too_large')
            turns = json.loads(json.dumps(original))
            summary = row['summary']
            directory = store.path.parent / 'console-backups'
            prefix = namespace_key(namespace)
            if action in ('edit_group', 'delete_group'):
                side, refs, text = payload.get('role'), payload.get('messages'), payload.get('text')
                if side not in ('user', 'assistant') or not isinstance(refs, list) or not 1 <= len(refs) <= 2000:
                    raise ValueError('invalid_context_text')
                if any(not isinstance(ref, dict) or type(ref.get('sequence')) is not int or type(ref.get('index')) is not int for ref in refs):
                    raise ValueError('invalid_context_text')
                flat = [(t['sequence'], role, index) for t in turns for role, field in
                        (('user', 'inputs_json'), ('assistant', 'replies_json')) for index, _ in enumerate(json.loads(t[field]))]
                targets = [(ref['sequence'], side, ref['index']) for ref in refs]
                try:
                    start = flat.index(targets[0])
                except ValueError:
                    raise ValueError('revision_conflict') from None
                if flat[start:start + len(targets)] != targets:
                    raise ValueError('revision_conflict')
                if action == 'edit_group' and (not isinstance(text, str) or not text.strip() or len(text) > 100000 or '\0' in text):
                    raise ValueError('invalid_context_text')
                field = 'inputs_json' if side == 'user' else 'replies_json'
                by_sequence = {t['sequence']: t for t in turns}
                if action == 'edit_group' and text == '\n\n'.join(json.loads(by_sequence[seq][field])[index]['text'] for seq, _, index in targets):
                    return store._snapshot(db, namespace, row, 12)
                # Reverse deletion preserves all original message indices; one transaction,
                # epoch change and recovery file cover the entire visible message card.
                for target in reversed(targets):
                    seq, _, index = target
                    turn = by_sequence[seq]
                    messages = json.loads(turn[field])
                    if action == 'edit_group' and target == targets[0]:
                        messages[index].update(text=text, edited=True)
                    else:
                        messages.pop(index)
                    turn[field] = json.dumps(messages, ensure_ascii=False)
                turns = [t for t in turns if json.loads(t['inputs_json']) or json.loads(t['replies_json'])]
            elif action in ('edit', 'delete_message'):
                side = payload.get('role')
                index = payload.get('index')
                text = payload.get('text')
                if (side not in ('user', 'assistant') or type(index) is not int or index < 0
                        or (action == 'edit' and (not isinstance(text, str) or not text.strip() or len(text) > 10000 or '\0' in text))):
                    raise ValueError('invalid_context_text')
                turn = next((t for t in turns if t['sequence'] == payload.get('sequence')), None)
                if turn is None:
                    raise ValueError('revision_conflict')
                field = 'inputs_json' if side == 'user' else 'replies_json'
                messages = json.loads(turn[field])
                if index >= len(messages):
                    raise ValueError('revision_conflict')
                if action == 'edit' and messages[index]['text'] == text:
                    return store._snapshot(db, namespace, row, 12)
                if action == 'delete_message':
                    messages.pop(index)
                else:
                    messages[index]['text'] = text
                    messages[index]['edited'] = True
                turn[field] = json.dumps(messages, ensure_ascii=False)
                if not json.loads(turn['inputs_json']) and not json.loads(turn['replies_json']):
                    turns.remove(turn)
            elif action == 'delete_turn':
                turns = [t for t in turns if t['sequence'] != payload.get('sequence')]
                if len(turns) == len(original):
                    raise ValueError('revision_conflict')
            elif action == 'clear':
                turns, summary = [], ''
            elif action == 'summary':
                summary = payload.get('text')
                if not isinstance(summary, str) or '\0' in summary or estimate_tokens(summary) > ContextBudget().summary_tokens:
                    raise ValueError('summary_budget_exceeded')
            elif action == 'restore':
                backup = payload.get('backupId')
                if not isinstance(backup, str) or not re.fullmatch(prefix + r'-[a-f0-9]{32}', backup):
                    raise ValueError('invalid_backup')
                path = directory / (backup + '.json')
                if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError('invalid_backup')
                prior = json.loads(path.read_text(encoding='utf-8'))
                if prior.get('namespace') != list(namespace.values):
                    raise ValueError('invalid_backup')
                turns, summary = prior['turns'], prior['summary']
            if action != 'backup' and turns == original and summary == row['summary']:
                return store._snapshot(db, namespace, row, 12)
            backup = {'namespace': namespace.values, 'summary': row['summary'], 'turns': original,
                      'version': version(row), 'createdAt': time.time(), 'action': action}
            encoded = json.dumps(backup, ensure_ascii=False).encode('utf-8')
            if len(encoded) > 8 * 1024 * 1024:
                raise ValueError('context_too_large')
            directory.mkdir(parents=True, exist_ok=True)
            # Exclusively create the recovery file before committing any change.
            with (directory / (prefix + '-' + uuid.uuid4().hex + '.json')).open('xb') as stream:
                stream.write(encoded)
                stream.flush()
                import os
                os.fsync(stream.fileno())
            if action == 'backup':
                return store._snapshot(db, namespace, row, 12)
            epoch = row['epoch'] + 1
            db.execute(f'DELETE FROM memory_turns WHERE {_NS_WHERE} AND epoch=?', (*namespace.values, row['epoch']))
            fields = ('platform_id', 'kind', 'peer_id', 'persona_id', 'epoch', 'sequence', 'batch_id',
                      'persona_revision', 'inputs_json', 'replies_json', 'complete', 'created_at')
            for turn in turns:
                turn.update(dict(zip(('platform_id', 'kind', 'peer_id', 'persona_id'), namespace.values)))
                turn['epoch'] = epoch
                db.execute('INSERT INTO memory_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', tuple(turn[k] for k in fields))
            last_sequence = max(row['last_sequence'], max((t['sequence'] for t in turns), default=0))
            db.execute(f'UPDATE memory_namespaces SET epoch=?,summary=?,summary_revision=summary_revision+1,'
                       f'last_sequence=?,updated_at=? WHERE {_NS_WHERE}',
                       (epoch, summary, last_sequence, time.time(), *namespace.values))
            updated = db.execute(f'SELECT * FROM memory_namespaces WHERE {_NS_WHERE}', namespace.values).fetchone()
            return store._snapshot(db, namespace, updated, 12)
