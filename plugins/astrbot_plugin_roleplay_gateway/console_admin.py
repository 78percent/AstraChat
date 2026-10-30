"""Authenticated local management, executed in the live plugin's event loop.

Bound to container loopback only. Browser requests go through the loopback host
console; the bridge credential is read inside the container and never returned.
"""
from __future__ import annotations
import asyncio
import copy
from dataclasses import replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from .gate import GateSettings
from .memory_store import Namespace, MemorySnapshot
from .persona import load_persona
from .context_budget import build_prompt, ContextBudget
from .provider_gate import SpeakerInput
from .roleplay_stream import ROLEPLAY_BOUNDARY
from .native_settings import settings_from_config as native_settings
from .weixin_adapter import settings_from_config as weixin_settings
from .model_profiles import selected, profile_models, model_id, profile_name, public_profiles


def config_version(config):
    # A hash of non-secret, user-editable fields only.
    keys = ('persona_id', 'persona_name', 'persona_filename', 'persona_sha256',
            'console_model', 'reply_pacing', 'native_reply_probability', 'console_context_limit')
    values = {k: config.get(k) for k in keys}
    values.update(console_model=config.get('console_model') or '', console_context_limit=config.get('console_context_limit', 12))
    values.update(console_search_model=config.get('console_search_model', ''), console_image_model=config.get('console_image_model', ''))
    values.update(console_profile=selected(config), console_profiles=profile_models(config))
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as f:
            temp = f.name
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


class ConsoleAdmin:
    def __init__(self, plugin, data_root):
        self.plugin, self.data = plugin, Path(data_root)
        self.runner = None
        self.lock = asyncio.Lock()
        self.models_cache = None
        self.features = None

    def services(self):
        return [('qq', self.plugin.service), ('weixin', self.plugin.wechat_service), ('native', self.plugin.native_service)]

    def status(self):
        return {'available': True, 'configVersion': config_version(self.plugin.config),
                'model': profile_models(self.plugin.config)[selected(self.plugin.config)],
                'modelProfiles': public_profiles(self.plugin.config, self.data),
                'personaRevision': self.plugin.config.get('persona_sha256'),
                'services': {key: {'ready': s is not None and not s._closing and not getattr(s, 'blocked', False),
                                  'personaRevision': s.persona.revision if s is not None and hasattr(s, 'persona') else None,
                                  'recentTurns': getattr(s, 'context_budget', ContextBudget()).recent_turns}
                             for key, s in self.services()}}

    async def start(self):
        from aiohttp import web
        from .console_features import ConsoleFeatures
        self.features = ConsoleFeatures(self)
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get('/status', self.handle)
        app.router.add_post('/manage', self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, '127.0.0.1', 6199).start()
            self.features.start()
        except BaseException:
            await self.runner.cleanup()
            self.runner = None
            raise

    async def close(self):
        if self.features is not None:
            await self.features.close()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def handle(self, request):
        from aiohttp import web
        token = self.plugin.config.get('native_bridge_token', '')
        if (not re.fullmatch('[a-f0-9]{64}', token) or request.headers.get('Origin') is not None
                or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token)):
            return web.json_response({'ok': False, 'error': 'unauthorized'}, status=403)
        try:
            if request.method == 'GET':
                result = self.status()
            else:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise ValueError('invalid_request')
                async with self.lock:
                    result = await self.execute(payload)
            return web.json_response({'ok': True, 'data': result}, headers={'Cache-Control': 'no-store'})
        except (ValueError, FileNotFoundError) as exc:
            code = str(exc)
            if not re.fullmatch('[a-z_]{1,100}', code):
                code = 'invalid_request'
            return web.json_response({'ok': False, 'error': code}, status=409 if code == 'revision_conflict' else 400)
        except Exception:
            return web.json_response({'ok': False, 'error': 'management_failed'}, status=503)

    async def execute(self, payload):
        op = payload.get('op')
        if op == 'features':
            if self.features is None:
                raise ValueError('feature_service_unavailable')
            return await self.features.manage(payload)
        if op == 'models':
            return await self.models(payload)
        if op in ('model_key', 'model_test'):
            router = self.plugin._profiles()
            if op == 'model_key':
                return await router.save_key(payload)
            profile = profile_name(payload.get('profile'))
            return await router.probe(profile, model_id(profile, payload.get('model')))
        if op == 'personas':
            return self.personas()
        if op == 'persona_read':
            candidate = next((p for p in self.personas()['personas'] if p['filename'] == payload.get('filename')), None)
            if candidate is None:
                raise ValueError('invalid_persona')
            persona = load_persona(self.data / 'roleplay/personas' / candidate['filename'], persona_id=candidate['id'], name=candidate['name'])
            return {**candidate, 'prompt': persona.prompt}
        if op == 'context':
            return await self.context_change(payload)
        if op not in ('settings', 'persona_save', 'persona_select', 'persona_delete'):
            raise ValueError('invalid_action')
        config = copy.deepcopy(dict(self.plugin.config))
        if payload.get('configVersion') != config_version(config):
            raise ValueError('revision_conflict')
        if op == 'persona_delete':
            candidate = next((p for p in self.personas()['personas'] if p['filename'] == payload.get('filename')), None)
            if candidate is None:
                raise ValueError('invalid_persona')
            if candidate['active']:
                raise ValueError('active_persona_delete')
            root = self.data / 'roleplay/personas'
            archive = root / 'deleted'
            archive.mkdir(exist_ok=True)
            for path in root.glob(candidate['id'] + '-*'):
                if path.is_file() and re.fullmatch(re.escape(candidate['id']) + r'-[a-f0-9]{64}\.(txt|md|json|png)', path.name):
                    path.replace(archive / (uuid.uuid4().hex + '-' + path.name))
            return self.personas()
        if op == 'settings':
            profile = profile_name(payload.get('profile', selected(config)))
            model = model_id(profile, payload.get('model'))
            models = profile_models(config)
            models[profile] = model
            config['console_profile'] = profile
            config['console_profiles'] = {p: {'model': m} for p, m in models.items()}
            config['console_model'] = model
            for field in ('searchModel', 'imageModel'):
                value = payload.get(field, config.get('console_' + ('search_model' if field == 'searchModel' else 'image_model'), ''))
                if not isinstance(value, str) or len(value) > 160 or any(ord(c) < 32 for c in value):
                    raise ValueError('invalid_settings')
                config['console_' + ('search_model' if field == 'searchModel' else 'image_model')] = value.strip()
            pacing = payload.get('pacing')
            allowed = {'probability', 'first_delay_min', 'first_delay_max', 'interval_min', 'interval_max',
                       'max_bubbles', 'max_characters', 'cooldown_seconds', 'window_seconds', 'max_rounds'}
            if not isinstance(pacing, dict) or set(pacing) - allowed:
                raise ValueError('invalid_reply_pacing')
            config['reply_pacing'] = {**config.get('reply_pacing', {}), **pacing}
            if config.get('native_enabled') and (config['reply_pacing']['max_bubbles'] > 4 or config['reply_pacing']['max_characters'] > 125):
                raise ValueError('native_reply_limit')
            for prefix in ('first_delay', 'interval'):
                p = config['reply_pacing']
                lo, hi = p[prefix + '_min'], p[prefix + '_max']
                p[prefix + '_mean'], p[prefix + '_std'] = (lo + hi) / 2, (hi - lo) / 4
            probability, limit = payload.get('nativeReplyProbability'), payload.get('contextLimit')
            if type(probability) not in (int, float) or not 0 <= probability <= 1 or type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError('invalid_settings')
            config['native_reply_probability'], config['console_context_limit'] = probability, limit
        else:
            if op == 'persona_save':
                identity = payload.get('id') or 'role_' + uuid.uuid4().hex[:12]
                name, prompt = payload.get('name'), payload.get('prompt')
                if (not isinstance(identity, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}', identity)
                        or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000):
                    raise ValueError('invalid_persona')
                encoded = prompt.encode('utf-8')
                revision = hashlib.sha256(encoded).hexdigest()
                filename = identity + '-' + revision + '.txt'
                path = self.data / 'roleplay/personas' / filename
                if not path.exists():
                    path.write_bytes(encoded)
                elif path.read_bytes() != encoded:
                    raise ValueError('persona_revision_mismatch')
            else:
                candidate = next((p for p in self.personas()['personas'] if p['filename'] == payload.get('filename')), None)
                if candidate is None:
                    raise ValueError('invalid_persona')
                identity, name, filename = candidate['id'], candidate['name'], candidate['filename']
                path = self.data / 'roleplay/personas' / filename
            persona = load_persona(path, persona_id=identity, name=name)
            build_prompt(persona, MemorySnapshot(Namespace('console', 'private', 'check', identity), 0, 0, '', (), 0),
                         (SpeakerInput('check', '预检', '1', '你好'),), runtime_boundary=ROLEPLAY_BOUNDARY)
            config.update(persona_id=identity, persona_name=persona.name, persona_filename=filename, persona_sha256=persona.revision)
        # Validate every channel before pausing work or writing configuration.
        settings = GateSettings.from_mapping(config)
        wx, native = weixin_settings(config), native_settings(config)
        current = self.plugin.config
        if config_version(config) == config_version(current):
            return {**self.status(), 'changed': False}
        return await self.apply(config, settings, wx, native)

    async def models(self, payload=None):
        if hasattr(self.plugin, '_profiles'):
            payload = payload or {}
            profile = profile_name(payload.get('profile', selected(self.plugin.config)))
            model = payload.get('model') or profile_models(self.plugin.config)[profile]
            return await self.plugin._profiles().catalog(profile, model_id(profile, model))
        provider = await self.plugin._stream_provider(allow_missing=False)
        if self.models_cache and time.monotonic() - self.models_cache[0] < 300:
            return {'models': self.models_cache[1]}
        try:
            listing = await asyncio.wait_for(provider.client.models.list(), timeout=8)
            models = sorted({m.id for m in listing.data if isinstance(m.id, str) and re.fullmatch(r'deepseek-[A-Za-z0-9_.-]{1,80}', m.id)})
            if provider.get_model() not in models:
                models.append(provider.get_model())
            self.models_cache = (time.monotonic(), models)
            return {'models': models}
        except Exception:
            # Keep the configured model selectable offline, never invent choices.
            return {'models': [provider.get_model()], 'catalogUnavailable': True}

    def personas(self):
        config = self.plugin.config
        catalog = {}
        root = self.data / 'roleplay/personas'
        for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_-]{0,63})-([a-f0-9]{64})\.(txt|md|json|png)', path.name)
            if not match or not path.resolve().is_relative_to(root.resolve()):
                continue
            identity, revision = match[1], match[2]
            active = path.name == config.get('persona_filename')
            # Retain meaningful names for saved cards without putting content in metadata.
            names_path = root / 'console-names.json'
            try:
                names = json.loads(names_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                names = {}
            name = config['persona_name'] if active else names.get(identity, identity)
            if identity not in catalog or active:
                catalog[identity] = {'id': identity, 'name': name, 'filename': path.name, 'revision': revision, 'active': active}
        return {'personas': sorted(catalog.values(), key=lambda p: not p['active'])}

    async def apply(self, config, settings, wx, native):
        plugin = self.plugin
        previous = copy.deepcopy(dict(plugin.config))
        provider = await plugin._stream_provider(allow_missing=False)
        old_model = provider.get_model()
        router = plugin._profiles() if hasattr(plugin, '_profiles') else None
        next_profile = selected(config)
        next_model = profile_models(config)[next_profile]
        route_changed = (next_profile, next_model) != (selected(previous), profile_models(previous)[selected(previous)])
        if route_changed and router is not None:
            await router.probe(next_profile, next_model, allow_cached=True)
        persona = load_persona(self.data / 'roleplay/personas' / config['persona_filename'],
                               persona_id=config['persona_id'], name=config['persona_name'])
        path = self.data / 'config/astrbot_plugin_roleplay_gateway_config.json'
        # Externally saved configuration must not be overwritten by this editor.
        disk_bytes = path.read_bytes()
        disk = json.loads(disk_bytes.decode('utf-8-sig'))
        if config_version(disk) != config_version(previous) or any(previous.get(k) != v for k, v in disk.items()):
            raise ValueError('revision_conflict')
        plugin._console_busy = True
        written = False
        try:
            await asyncio.wait_for(asyncio.gather(*(s.wait_idle() for _, s in self.services() if s is not None)), 35)
            if path.read_bytes() != disk_bytes:
                raise ValueError('revision_conflict')
            backup = self.data / 'plugin_data/astrbot_plugin_roleplay_gateway/console-config-backups' / (uuid.uuid4().hex + '.json')
            atomic_json(backup, disk)
            atomic_json(path, config)
            written = True
            plugin.config.clear()
            plugin.config.update(config)
            if router is not None:
                router.resolve(config)
            else:
                provider.set_model(next_model)
            plugin.settings, plugin.wechat_settings, plugin.native_settings = settings, wx, native
            for channel, service in self.services():
                if service is not None:
                    if router is not None:
                        # Old pooled adapters retain the previous provider reference.
                        service._available.clear()
                    service.settings = {'qq': settings, 'weixin': wx, 'native': native}[channel]
                    service.persona = persona
                    service.context_budget = replace(ContextBudget(), recent_turns=config.get('console_context_limit', 12))
                    from .stream_reply import ReplyLimits
                    limits = (ReplyLimits(first_receipt_seconds=40, generation_seconds=45,
                                          total_seconds=50) if next_profile == 'kouri' else ReplyLimits())
                    first = service.settings.reply_pacing.validate_deadlines(
                        first=limits.first_receipt_seconds, generation=limits.generation_seconds,
                        total=limits.total_seconds,
                        send_timeout=service.settings.send_timeout_seconds)
                    service.limits = replace(limits, first_receipt_seconds=first)
            if plugin.native_server is not None:
                plugin.native_server.settings = native
            names_path = self.data / 'roleplay/personas/console-names.json'
            try:
                names = json.loads(names_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                names = {}
            names.setdefault(previous['persona_id'], previous['persona_name'])
            names[persona.persona_id] = persona.name
            atomic_json(names_path, names)
            return {**self.status(), 'changed': True}
        except BaseException:
            if not written:
                raise
            # Restore disk and all in-process settings as a unit on failure.
            atomic_json(path, previous)
            plugin.config.clear()
            plugin.config.update(previous)
            provider.set_model(old_model)
            if router is not None:
                router.resolve(previous)
            plugin.settings = GateSettings.from_mapping(previous)
            plugin.wechat_settings, plugin.native_settings = weixin_settings(previous), native_settings(previous)
            old_persona = load_persona(self.data / 'roleplay/personas' / previous['persona_filename'],
                                      persona_id=previous['persona_id'], name=previous['persona_name'])
            for channel, service in self.services():
                if service is not None:
                    if router is not None:
                        service._available.clear()
                    service.settings = {'qq': plugin.settings, 'weixin': plugin.wechat_settings, 'native': plugin.native_settings}[channel]
                    service.persona = old_persona
                    service.context_budget = replace(ContextBudget(), recent_turns=previous.get('console_context_limit', 12))
                    from .stream_reply import ReplyLimits
                    old_profile = selected(previous)
                    limits = (ReplyLimits(first_receipt_seconds=40, generation_seconds=45,
                                          total_seconds=50) if old_profile == 'kouri' else ReplyLimits())
                    first = service.settings.reply_pacing.validate_deadlines(
                        first=limits.first_receipt_seconds, generation=limits.generation_seconds,
                        total=limits.total_seconds,
                        send_timeout=service.settings.send_timeout_seconds)
                    service.limits = replace(limits, first_receipt_seconds=first)
            if plugin.native_server is not None:
                plugin.native_server.settings = plugin.native_settings
            raise
        finally:
            plugin._console_busy = False
            plugin._write_runtime_status('console_applied')

    async def context_change(self, payload):
        identifier = payload.get('id')
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('invalid_session')
        for channel, service in self.services():
            if service is None or not hasattr(service, 'memory'):
                continue
            # Enumeration under the same store lock; no new namespaces are created.
            def find():
                with service.memory._lock:
                    rows = service.memory._db().execute('SELECT platform_id,kind,peer_id,persona_id FROM memory_namespaces LIMIT 201').fetchall()
                    for row in rows:
                        ns = Namespace(*tuple(row))
                        raw = json.dumps([channel, *ns.values], ensure_ascii=False)
                        if hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32] == identifier:
                            return ns
            namespace = await asyncio.to_thread(find)
            if namespace is not None:
                result = await service.clear_memory(namespace, mutation=lambda: service.memory.console_change(namespace, payload))
                return {'version': f'{result.epoch}:{result.last_sequence}:{result.summary_revision}', 'applied': True}
        raise ValueError('session_not_found')
