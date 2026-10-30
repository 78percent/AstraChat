"""Persistent worldbooks and local scheduled messages, owned by the live plugin."""
from __future__ import annotations
import asyncio
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import time
import uuid

from .worldbook import validate_entries, select_entries

CHINA = timezone(timedelta(hours=8))
PERIODS = {'daily': 86400, 'weekly': 604800}

def next_occurrence(at, repeat, now):
    if repeat == 'none':
        return None
    period = PERIODS[repeat]
    return at + (max(0, math.floor((now - at) / period)) + 1) * period

def parse_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.timestamp()
    except (TypeError, ValueError, OverflowError):
        raise ValueError('invalid_schedule_time') from None

class ConsoleFeatures:
    def __init__(self, admin, *, clock=time.time):
        self.admin, self.plugin, self.clock = admin, admin.plugin, clock
        self.path = admin.data / 'plugin_data/astrbot_plugin_roleplay_gateway/console-features.json'
        self.state = {'schema': 1, 'revision': 0, 'worldbooks': {}, 'schedulerEnabled': False, 'tasks': [], 'runs': []}
        if self.path.is_file():
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('invalid_feature_store')
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state.get('schema') != 1 or type(self.state.get('revision')) is not int:
                raise ValueError('invalid_feature_store')
            for entries in self.state['worldbooks'].values():
                validate_entries(entries)
            # Never retry a send whose process died before recording the result.
            changed = False
            for run in self.state['runs']:
                if run['status'] in ('preparing', 'dispatching'):
                    run['status'] = 'unknown' if run['status'] == 'dispatching' else 'cancelled'
                    changed = True
            if changed:
                self.persist()
        self.closed = asyncio.Event()
        self.task = None

    def persist(self, *, changed=False):
        from .console_admin import atomic_json
        if changed and self.path.is_file():
            previous = json.loads(self.path.read_text(encoding='utf-8'))
            backup = self.path.parent / 'console-feature-backups' / (uuid.uuid4().hex + '.json')
            atomic_json(backup, previous)
        if changed:
            self.state['revision'] += 1
        atomic_json(self.path, self.state)

    def worlds(self, persona_id):
        return copy.deepcopy(self.state['worldbooks'].get(persona_id, []))

    def targets(self, *, private=False):
        service = self.plugin.native_service
        if service is None:
            return []
        settings = service.settings
        result = []
        for kind, peers in (('group', settings.allowed_group_ids), ('private', settings.admin_private_ids)):
            for peer in sorted(peers):
                identity = hashlib.sha256(json.dumps(['native', kind, peer]).encode()).hexdigest()[:32]
                label = '桌面微信 · ' + ('群聊 ' if kind == 'group' else '私聊 ') + '••••' + peer.replace('@chatroom', '')[-4:]
                item = {'id': identity, 'label': label}
                if private:
                    item.update(peer=peer, kind=kind, platformId=settings.platform_id)
                result.append(item)
        return result

    def target(self, identity):
        result = next((v for v in self.targets(private=True) if v['id'] == identity), None)
        if result is None:
            raise ValueError('schedule_target_unavailable')
        return result

    def snapshot(self):
        persona_id = self.plugin.config['persona_id']
        server = self.plugin.native_server
        connection = getattr(server, '_connection', None)
        ready = bool(connection and not connection.closed and getattr(connection, 'scheduled', False))
        return {'revision': self.state['revision'], 'personaId': persona_id,
                'entries': self.worlds(persona_id), 'schedulerEnabled': self.state['schedulerEnabled'],
                'running': self.task is not None and not self.task.done(), 'deliveryReady': ready,
                'timezone': 'Asia/Shanghai', 'targets': self.targets(),
                'tasks': copy.deepcopy(self.state['tasks']), 'runs': copy.deepcopy(self.state['runs'][-50:][::-1])}

    def validate_task(self, value, *, creating=True):
        if not isinstance(value, dict):
            raise ValueError('invalid_schedule')
        text, mode, repeat = value.get('content'), value.get('mode'), value.get('repeat')
        if (mode not in ('fixed', 'persona') or repeat not in ('none', 'daily', 'weekly')
                or not isinstance(text, str) or not text.strip() or '\0' in text
                or len(text) > (125 if mode == 'fixed' else 800) or type(value.get('enabled')) is not bool):
            raise ValueError('invalid_schedule')
        self.target(value.get('targetId'))
        at = parse_time(value.get('at'))
        if not 946684800 < at <= self.clock() + 5 * 366 * 86400:
            raise ValueError('invalid_schedule_time')
        if creating and value['enabled'] and at <= self.clock() + 5:
            raise ValueError('schedule_time_in_past')
        return {k: value[k] for k in ('targetId', 'content', 'mode', 'repeat', 'enabled')} | {
            'at': datetime.fromtimestamp(at, CHINA).isoformat(), 'nextRun': at}

    async def manage(self, payload):
        prior = copy.deepcopy(self.state)
        try:
            return await self._manage(payload)
        except BaseException:
            self.state = prior
            raise

    async def _manage(self, payload):
        action = payload.get('action', 'get')
        if action == 'get':
            return self.snapshot()
        if action == 'preview':
            if not isinstance(payload.get('task'), dict):
                raise ValueError('invalid_schedule')
            value = self.validate_task({**payload['task'], 'enabled': False}, creating=False)
            return {'text': await self.render(value), 'sent': False}
        if payload.get('revision') != self.state['revision']:
            raise ValueError('revision_conflict')
        if action == 'worldbook':
            if payload.get('personaId') != self.plugin.config['persona_id']:
                raise ValueError('revision_conflict')
            self.state['worldbooks'][payload['personaId']] = validate_entries(payload.get('entries'))
        elif action == 'scheduler':
            if type(payload.get('enabled')) is not bool:
                raise ValueError('invalid_schedule')
            self.state['schedulerEnabled'] = payload['enabled']
            if payload['enabled']:
                self.skip_overdue()
        elif action == 'save_task':
            value = payload.get('task', {})
            if not isinstance(value, dict):
                raise ValueError('invalid_schedule')
            identity = value.get('id')
            prior = next((t for t in self.state['tasks'] if t['id'] == identity), None)
            if identity is not None and prior is None:
                raise ValueError('invalid_schedule')
            if any(r['taskId'] == identity and r['status'] in ('preparing', 'dispatching') for r in self.state['runs']):
                raise ValueError('schedule_busy')
            rearm = prior is not None and value.get('enabled') and not prior['enabled']
            spec = self.validate_task(value, creating=prior is None or value.get('at') != prior['at'] or (rearm and value.get('repeat') == 'none'))
            if prior is not None:
                spec.update(id=prior['id'], createdAt=prior['createdAt'], deleted=False)
                if spec['at'] == prior['at'] and spec['repeat'] == prior['repeat']:
                    spec['nextRun'] = prior['nextRun']
                if rearm and spec['repeat'] != 'none' and (spec['nextRun'] is None or spec['nextRun'] <= self.clock()):
                    spec['nextRun'] = next_occurrence(parse_time(spec['at']), spec['repeat'], self.clock())
                self.state['tasks'] = [spec if t['id'] == identity else t for t in self.state['tasks']]
            else:
                if len(self.state['tasks']) >= 100:
                    raise ValueError('too_many_schedules')
                spec.update(id=uuid.uuid4().hex, createdAt=self.clock(), deleted=False)
                self.state['tasks'].append(spec)
        elif action in ('delete_task', 'restore_task'):
            task = next((t for t in self.state['tasks'] if t['id'] == payload.get('id')), None)
            if task is None:
                raise ValueError('invalid_schedule')
            if any(r['taskId'] == task['id'] and r['status'] in ('preparing', 'dispatching') for r in self.state['runs']):
                raise ValueError('schedule_busy')
            task.update(deleted=action == 'delete_task', enabled=False)
        else:
            raise ValueError('invalid_action')
        self.persist(changed=True)
        return self.snapshot()

    def skip_overdue(self):
        now = self.clock()
        changed = False
        for task in self.state['tasks']:
            at = task['nextRun']
            if not task['deleted'] and task['enabled'] and at is not None and at < now - 120:
                changed = True
                self.state['runs'].append({'id': uuid.uuid4().hex, 'taskId': task['id'], 'at': at,
                                          'status': 'missed', 'finishedAt': now, 'text': ''})
                task['nextRun'] = next_occurrence(at, task['repeat'], now)
                if task['nextRun'] is None:
                    task['enabled'] = False
        self.state['runs'] = self.state['runs'][-200:]
        return changed

    async def render(self, task):
        if task['mode'] == 'fixed':
            return task['content']
        service = self.plugin.native_service
        if service is None or service._closing or service.blocked or not self.plugin._native_enabled():
            raise ValueError('schedule_service_unavailable')
        from .context_budget import build_prompt, ContextBudget
        from .memory_store import Namespace
        from .provider_gate import SpeakerInput
        from .roleplay_stream import ROLEPLAY_BOUNDARY
        from .dialogue_history import encode_dialogue_history
        target = self.target(task['targetId'])
        budget = getattr(service, 'context_budget', ContextBudget())
        persona = service.persona
        memory = await service.memory.snapshot(Namespace(target['platformId'], target['kind'], target['peer'], persona.persona_id), limit=budget.recent_turns)
        before, after, _ = select_entries(self.worlds(persona.persona_id), task['content'])
        instruction = '这是操作者安排的定时主动消息。请结合角色与既有上下文，围绕以下要求写一条自然的消息；不要假装收到新的用户回复。' + task['content']
        boundary = ROLEPLAY_BOUNDARY + '本次只输出一条完整speech，最多125个Unicode字符（含标点），不要输出多条。'
        parts = build_prompt(persona, memory, (SpeakerInput('scheduler', '定时任务', uuid.uuid4().hex, instruction),),
                             runtime_boundary=boundary, budget=budget, before_worldbook=before, after_worldbook=after,
                             assistant_projection=encode_dialogue_history).parts
        chunks = []
        async def collect(text):
            chunks.append(text)
        async with self.plugin._shared_parallel:
            adapter = service._lease_adapter()
            try:
                await service._consume_reply(adapter, parts, collect)
            finally:
                if adapter.pending_tasks == 0 and adapter.reusable:
                    service._available.append(adapter)
                else:
                    service._block('scheduled_provider_cleanup_unsettled')
        text = ''.join(chunks).strip()
        if not text or len(text) > 125 or '\0' in text:
            raise ValueError('schedule_generated_text_invalid')
        return text

    async def run_due(self):
        if not self.state['schedulerEnabled'] or self.closed.is_set():
            return
        if self.skip_overdue():
            self.persist(changed=True)
        now = self.clock()
        due = next((t for t in self.state['tasks'] if not t['deleted'] and t['enabled']
                    and t['nextRun'] is not None and t['nextRun'] <= now), None)
        if due is None:
            # Persist skipped occurrences before a restart can reconsider them.
            return
        at = due['nextRun']
        run = {'id': uuid.uuid4().hex, 'taskId': due['id'], 'at': at, 'status': 'preparing', 'text': ''}
        self.state['runs'].append(run)
        self.state['runs'] = self.state['runs'][-200:]
        due['nextRun'] = next_occurrence(at, due['repeat'], now)
        if due['nextRun'] is None:
            due['enabled'] = False
        self.persist(changed=True)
        try:
            if not self.plugin._native_enabled() or self.closed.is_set():
                raise ValueError('schedule_service_unavailable')
            target = self.target(due['targetId'])
            text = await self.render(due)
            if self.closed.is_set():
                raise ValueError('schedule_service_unavailable')
            run.update(text=text, status='dispatching')
            self.persist()
            await self.plugin.native_server.send_scheduled(target['kind'], target['peer'], text, run['id'], due['id'])
            run['status'] = 'api_accepted'
        except asyncio.CancelledError:
            run['status'] = 'unknown' if run['status'] == 'dispatching' else 'cancelled'
            raise
        except Exception as error:
            from .gate import DefiniteSendFailure
            run['status'] = 'failed' if run['status'] == 'preparing' or isinstance(error, (ValueError, DefiniteSendFailure)) else 'unknown'
        finally:
            run['finishedAt'] = self.clock()
            self.persist()

    async def loop(self):
        while not self.closed.is_set():
            try:
                async with self.admin.lock:
                    await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Static diagnostics only. Keep the scheduler inspectable after I/O failure.
                self.plugin._emit({'component': 'console_scheduler', 'code': 'tick_failed'})
            try:
                await asyncio.wait_for(self.closed.wait(), 1)
            except TimeoutError:
                pass

    def start(self):
        self.task = asyncio.create_task(self.loop(), name='console-scheduler')

    async def close(self):
        self.closed.set()
        if self.task is not None:
            await self.task
