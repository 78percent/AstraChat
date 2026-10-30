"""Two explicit chat routes. Secrets never enter plugin config or snapshots."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from urllib.parse import urlsplit

DEEPSEEK_ID = 'deepseek-flash-official'
KOURI_ID = 'kouri-chat'
KOURI_BASE = 'https://api.kouri.chat/v1'
DEFAULTS = {'deepseek': 'deepseek-flash', 'kouri': 'gemini-4-argon'}


def profile_name(value):
    if value not in DEFAULTS:
        raise ValueError('invalid_model_profile')
    return value


def model_id(profile, value):
    profile_name(profile)
    pattern = r'deepseek-[A-Za-z0-9_.-]{1,80}' if profile == 'deepseek' else r'[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,159}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('invalid_console_model')
    return value


def selected(config):
    return profile_name(config.get('console_profile', 'deepseek'))


def profile_models(config):
    raw = config.get('console_profiles', {})
    if not isinstance(raw, dict):
        raise ValueError('invalid_model_profile')
    active = selected(config)
    return {p: model_id(p, raw.get(p, {}).get('model') or
                       (config.get('console_model') if p == active else None) or default)
            for p, default in DEFAULTS.items()}


def credential_path(data):
    return Path(data) / 'plugin_data/astrbot_plugin_roleplay_gateway/model-credentials.local.json'


def read_credentials(data):
    path = credential_path(data)
    if not path.exists():
        return {'revision': 'initial', 'key': ''}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) != {'revision', 'key'}
                or not isinstance(value['revision'], str) or not isinstance(value['key'], str)):
            raise ValueError
        if value['key']:
            validate_key(value['key'])
        return value
    except (ValueError, OSError, UnicodeError):
        raise ValueError('model_credentials_unavailable') from None


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{16,512}', key):
        raise ValueError('invalid_model_key')
    return key


def public_profiles(config, data):
    credentials = read_credentials(data)
    key = credentials['key']
    return {'active': selected(config), 'models': profile_models(config),
            'kouri': {'baseUrl': KOURI_BASE, 'keyConfigured': bool(key),
                      'maskedKey': '••••' + key[-4:] if key else '',
                      'keyRevision': credentials['revision']}}


def check_provider(provider, profile, model):
    """Do not widen either credential's allowed destination when adding routes."""
    profile_name(profile)
    model_id(profile, model)
    try:
        cfg = provider.provider_config
        url = urlsplit(str(provider.client.base_url))
        host = 'api.deepseek.com' if profile == 'deepseek' else 'api.kouri.chat'
        expected_extra = {'thinking': {'type': 'disabled'}} if profile == 'deepseek' else {}
        valid = (cfg.get('id') == (DEEPSEEK_ID if profile == 'deepseek' else KOURI_ID)
                 and cfg.get('type') == 'openai_chat_completion'
                 and cfg.get('provider') == ('deepseek' if profile == 'deepseek' else 'custom')
                 and cfg.get('custom_extra_body') == expected_extra
                 and provider.get_model() == model and url.scheme == 'https' and url.hostname == host
                 and url.port in (None, 443) and url.path in (('', '/', '/v1', '/v1/') if profile == 'deepseek' else ('/v1', '/v1/'))
                 and not url.username and not url.password and not url.query and not url.fragment)
        if profile == 'kouri':
            valid = valid and provider.client._client.follow_redirects is False
    except Exception:
        valid = False
    if not valid:
        raise ValueError('neutral_stream_provider_boundary_changed')


def new_provider(config):
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    provider = ProviderOpenAIOfficial(config, {})
    provider.client._client.follow_redirects = False
    return provider


class ModelProfiles:
    def __init__(self, plugin, data):
        self.plugin, self.data = plugin, Path(data)
        self.kouri = None
        self.kouri_revision = None
        self.last_test = None

    def deepseek(self):
        provider = getattr(self, '_deepseek_provider', None)
        return provider if provider is not None else self.plugin.context.get_provider_by_id(DEEPSEEK_ID)

    def _candidate_config(self, profile, model):
        model_id(profile, model)
        if profile == 'deepseek':
            provider = self.deepseek()
            if provider is None:
                raise ValueError('model_provider_unavailable')
            check_provider(provider, 'deepseek', provider.get_model())
            cfg = copy.deepcopy(provider.provider_config)
            cfg.update(key=list(provider.api_keys), model=model)
            return cfg
        key = read_credentials(self.data)['key']
        if not key:
            raise ValueError('model_key_required')
        return {'id': KOURI_ID, 'provider': 'custom', 'type': 'openai_chat_completion',
                'provider_type': 'chat_completion', 'enable': True, 'key': [key],
                'api_base': KOURI_BASE, 'model': model, 'timeout': 30,
                'proxy': '', 'custom_headers': {}, 'custom_extra_body': {}}

    def resolve(self, config=None):
        config = self.plugin.config if config is None else config
        profile = selected(config)
        model = profile_models(config)[profile]
        if profile == 'deepseek':
            provider = self.deepseek()
            if provider is None:
                return None
            if provider.get_model() != model:
                provider.set_model(model)
        else:
            credentials = read_credentials(self.data)
            if self.kouri is None:
                self.kouri = new_provider(self._candidate_config(profile, model))
                self.kouri_revision = credentials['revision']
            if credentials['revision'] != self.kouri_revision:
                raise ValueError('model_key_changed_restart_required')
            provider = self.kouri
            if provider.get_model() != model:
                provider.set_model(model)
        check_provider(provider, profile, model)
        return provider

    async def save_key(self, payload):
        current = read_credentials(self.data)
        if payload.get('keyRevision') != current['revision']:
            raise ValueError('revision_conflict')
        if selected(self.plugin.config) == 'kouri':
            raise ValueError('model_key_in_use')
        action = payload.get('action')
        if action not in ('save', 'delete'):
            raise ValueError('invalid_action')
        key = validate_key(payload.get('apiKey')) if action == 'save' else ''
        if self.kouri is not None:
            await self.kouri.client.close()
            self.kouri = None
        path = credential_path(self.data)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
                temporary = stream.name
                os.chmod(temporary, 0o600)
                json.dump({'revision': uuid.uuid4().hex, 'key': key}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        self.last_test = None
        return public_profiles(self.plugin.config, self.data)

    async def catalog(self, profile, model):
        provider = new_provider(self._candidate_config(profile, model))
        try:
            listing = await asyncio.wait_for(provider.client.models.list(), 8)
            models = []
            for item in listing.data[:5000]:
                try:
                    models.append(model_id(profile, item.id))
                except ValueError:
                    pass
            return {'models': sorted(set(models + [model]))}
        except Exception:
            return {'models': [model], 'catalogUnavailable': True}
        finally:
            await provider.client.close()

    async def probe(self, profile, model, *, allow_cached=False):
        from .provider_gate import FailFastOpenAI, PromptParts, SpeakerInput, ProviderAttemptError
        from .dialogue_output import DialogueStream, DialogueFormatError
        profile_name(profile)
        model_id(profile, model)
        revision = read_credentials(self.data)['revision'] if profile == 'kouri' else 'deepseek'
        identity = (profile, model, revision)
        if allow_cached and self.last_test and self.last_test[:3] == identity and time.monotonic() - self.last_test[3] < 120:
            return self.last_test[4]
        provider = new_provider(self._candidate_config(profile, model))
        check_provider(provider, profile, model)
        started = time.monotonic()
        first = None
        parser = DialogueStream(max_total_characters=4096)
        speech = []
        async def collect(text):
            nonlocal first
            if first is None:
                first = time.monotonic() - started
            try:
                speech.extend(parser.iter_feed(text))
            except DialogueFormatError:
                raise ProviderAttemptError('provider_response_invalid') from None
        try:
            adapter = FailFastOpenAI(provider)
            result = await adapter.consume(PromptParts(
                runtime_boundary='请仅输出 JSON 对象 {"speech":["连接成功"]}，不要输出 Markdown 或其他内容。',
                persona='', current=(SpeakerInput('probe', '连接测试', 'probe', '请确认连接。'),)),
                collect, timeout_seconds=45 if profile == 'kouri' else 25,
                json_output=True)
            parser.finish()
            if not speech or not result.final_seen:
                raise ValueError('model_response_invalid')
            value = {'profile': profile, 'model': model, 'streaming': True, 'sent': False,
                     'reply': ''.join(speech), 'firstTextSeconds': round(first, 3),
                     'elapsedSeconds': round(time.monotonic() - started, 3)}
            self.last_test = (*identity, time.monotonic(), value)
            return value
        except DialogueFormatError:
            raise ValueError('model_response_invalid') from None
        except ProviderAttemptError as exc:
            codes = {'provider_auth_failed': 'model_auth_failed', 'provider_access_denied': 'model_access_denied',
                     'provider_model_unavailable': 'model_not_available', 'provider_rate_limited': 'model_rate_limited',
                     'provider_parameters_rejected': 'model_parameters_rejected',
                     'provider_response_invalid': 'model_response_invalid',
                     'attempt_timeout': 'model_test_timeout', 'provider_timeout': 'model_test_timeout'}
            raise ValueError(codes.get(str(exc), 'model_test_failed')) from None
        finally:
            await provider.client.close()

    async def close(self):
        if self.kouri is not None:
            await self.kouri.client.close()
            self.kouri = None
