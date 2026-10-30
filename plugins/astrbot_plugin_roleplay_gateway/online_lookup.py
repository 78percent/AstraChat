"""Bounded read-only lookup; never receives persona or conversation history.

Search is injected from AstrBot's built-in tool. Public HTTP uses only fixed
Open-Meteo/GitHub endpoints, no redirects or arbitrary-page downloader.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass, fields, replace
import ipaddress
import json
import math
import re
import time
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .lookup_intent import LookupPlan, classify_lookup


MAX_HTTP_BYTES = 512 * 1024
MAX_REFERENCE_CHARACTERS = 4800
_HOSTS = frozenset({'api.github.com', 'api.open-meteo.com', 'geocoding-api.open-meteo.com'})
_SECRET = re.compile(r'\b(?:sk-|tvly-)[A-Za-z0-9_-]{12,}|\bBearer\s+\S{12,}|(?:api[_-]?key|access[_-]?token|password|密码)\s*[=:：]\s*\S+', re.I)


@dataclass(frozen=True)
class LookupSettings:
    enabled: bool = False
    timeout_seconds: float = 6.0
    max_sources: int = 3

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('invalid_lookup_enabled')
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not .1 <= self.timeout_seconds <= 8):
            raise ValueError('invalid_lookup_timeout')
        if type(self.max_sources) is not int or not 1 <= self.max_sources <= 3:
            raise ValueError('invalid_lookup_max_sources')

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None = None):
        raw = {} if raw is None else raw
        if not isinstance(raw, Mapping) or set(raw) - {f.name for f in fields(cls)}:
            raise ValueError('invalid_lookup_settings')
        return cls(**raw)


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value if c in '\n\t' or c.isprintable()).strip()[:limit]


def _public_url(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return ''
    if any(c.isspace() or not c.isprintable() for c in value) or _SECRET.search(value):
        return ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if (parsed.scheme not in {'http', 'https'} or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 80, 443) or not host or '.' not in host
                or host.lower().endswith(('.local', '.localhost', '.internal', '.lan'))
                or not re.fullmatch(r'[A-Za-z0-9.-]+', host)):
            return ''
        try:
            if not ipaddress.ip_address(host).is_global:
                return ''
        except ValueError:
            if re.fullmatch(r'[0-9.]+', host):
                return ''
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except (ValueError, UnicodeError):
        return ''


@dataclass(frozen=True)
class LookupSource:
    title: str
    url: str
    excerpt: str


@dataclass(frozen=True)
class LookupResult:
    status: str
    sources: tuple[LookupSource, ...] = ()
    data: str = ''
    reason: str = ''
    cached: bool = False

    def reference_text(self) -> str:
        """Valid bounded JSON, with explicit data-only provenance."""
        sources = [{'index':i + 1, 'title':_text(s.title, 100), 'url':_public_url(s.url),
                    'excerpt':_text(s.excerpt, 650)} for i, s in enumerate(self.sources[:3]) if _public_url(s.url)]
        value = {'untrusted_reference_data': True, 'status': self.status,
                 'reason': self.reason, 'cached': self.cached,
                 'data': _text(self.data, 1200), 'sources': sources}
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        while len(encoded) > MAX_REFERENCE_CHARACTERS:
            if value['data']:
                value['data'] = value['data'][:max(0, len(value['data']) - 200)]
            elif any(s['excerpt'] for s in sources):
                for s in sources:
                    s['excerpt'] = s['excerpt'][:max(0, len(s['excerpt']) - 100)]
            else:
                sources.pop()
            encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return encoded


class _LookupFailure(Exception):
    pass


class OnlineLookupService:
    def __init__(self, settings: LookupSettings, *, search, session_factory=None):
        if not isinstance(settings, LookupSettings) or not callable(search):
            raise ValueError('invalid_lookup_service')
        self.settings, self.search = settings, search
        self._session_factory = session_factory
        self._cache: OrderedDict[tuple[str, LookupPlan], tuple[float, LookupResult]] = OrderedDict()
        self._jobs: set[asyncio.Task] = set()
        self._closed = self._blocked = False

    @staticmethod
    def plan(text: str, wake_names=()) -> LookupPlan | None:
        return classify_lookup(text, wake_names)

    @property
    def pending_tasks(self) -> int:
        return sum(not task.done() for task in self._jobs)

    @property
    def reusable(self) -> bool:
        return not self._closed and not self._blocked

    async def _settle(self, task: asyncio.Task) -> bool:
        if not task.done():
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=.2)
        if not done:
            self._blocked = True
            return False
        return True

    async def lookup(self, plan: LookupPlan, *, namespace: str, timeout_seconds: float) -> LookupResult:
        if not self.settings.enabled:
            return LookupResult('disabled', reason='lookup_disabled')
        if not self.reusable:
            return LookupResult('error', reason='lookup_unavailable')
        if (not isinstance(plan, LookupPlan) or plan.kind not in {'search','weather','github_repo','github_search','knowledge'}
                or not isinstance(namespace, str) or not 1 <= len(namespace) <= 1024
                or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            return LookupResult('error', reason='invalid_lookup_request')
        if not all(isinstance(x, str) for x in (plan.query, plan.city, plan.repo)):
            return LookupResult('error', reason='invalid_lookup_request')
        if plan.kind == 'knowledge':
            return LookupResult('knowledge', reason='no_live_lookup_needed')
        if _SECRET.search(plan.query) or _SECRET.search(plan.city):
            return LookupResult('error', reason='query_contains_credentials')
        if plan.kind in {'search','github_search'} and not 1 <= len(plan.query.strip()) <= 128:
            return LookupResult('error', reason='query_length_invalid')
        key = (namespace, plan)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return replace(cached[1], cached=True)
        if cached:
            del self._cache[key]
        task = asyncio.create_task(self._perform(plan), name='bounded-online-lookup')
        self._jobs.add(task)
        def finished(done):
            self._jobs.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=min(self.settings.timeout_seconds, timeout_seconds))
            if not done:
                settled = await self._settle(task)
                return LookupResult('error', reason='lookup_timeout' if settled else 'lookup_cleanup_unsettled')
            if task.cancelled():
                return LookupResult('error', reason='lookup_cancelled')
            result = task.result()
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._settle(task))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                self._blocked = True
            raise
        except _LookupFailure as error:
            result = LookupResult('error', reason=str(error))
        except Exception:
            result = LookupResult('error', reason='lookup_failed')
        if result.status == 'ok':
            ttl = 600 if plan.kind == 'weather' else 300 if plan.kind.startswith('github') else 120
            self._cache[key] = (time.monotonic() + ttl, result)
            self._cache.move_to_end(key)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
        return result

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        import aiohttp
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
                                     trust_env=False)

    async def _json(self, session, url, *, params=None, optional=False):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in _HOSTS or parsed.query or parsed.fragment or parsed.username:
            raise _LookupFailure('http_target_rejected')
        headers = {'Accept':'application/vnd.github+json', 'User-Agent':'AstrBot-Roleplay-Lookup/1.0'}
        async with session.get(url, params=params, headers=headers, allow_redirects=False) as response:
            if response.status == 404 and optional:
                return None
            if response.status == 429 or response.status == 403:
                raise _LookupFailure('source_rate_limited')
            if response.status != 200:
                raise _LookupFailure('source_http_error')
            length = response.headers.get('Content-Length')
            if length and (not str(length).isdigit() or int(length) > MAX_HTTP_BYTES):
                raise _LookupFailure('source_too_large')
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                if len(raw) > MAX_HTTP_BYTES:
                    raise _LookupFailure('source_too_large')
            try:
                value = json.loads(bytes(raw))
            except (ValueError, UnicodeError):
                raise _LookupFailure('source_invalid_json') from None
            if not isinstance(value, dict):
                raise _LookupFailure('source_invalid_json')
            return value

    async def _perform(self, plan):
        if plan.kind == 'search':
            raw = await self.search(plan.query)
            if isinstance(raw, dict):
                raw = raw.get('results')
            if not isinstance(raw, list):
                raise _LookupFailure('search_invalid_results')
            sources, seen = [], set()
            for item in raw[:20]:
                if not isinstance(item, dict):
                    continue
                url = _public_url(item.get('url'))
                if not url or url in seen:
                    continue
                snippet = _text(item.get('snippet') or item.get('summary') or item.get('content'), 650)
                if not snippet:
                    continue
                sources.append(LookupSource(_text(item.get('title'), 100) or urlsplit(url).hostname, url, snippet))
                seen.add(url)
                if len(sources) == self.settings.max_sources:
                    break
            return LookupResult('ok' if sources else 'empty', tuple(sources),
                                data=json.dumps({'retrieved_at_unix':int(time.time())}), reason='' if sources else 'no_usable_results')
        async with self._session() as session:
            if plan.kind == 'weather':
                return await self._weather(session, plan)
            return await self._github(session, plan)

    async def _weather(self, session, plan):
        if not plan.city:
            return LookupResult('needs_location', reason='city_required')
        if (not 1 <= len(plan.city) <= 40 or type(plan.day_offset) is not int or not 0 <= plan.day_offset <= 2):
            raise _LookupFailure('weather_request_invalid')
        selected_id = None
        city = plan.city
        selector = re.fullmatch(r'(.+?)#([1-9][0-9]{0,9})', city)
        if selector:
            city, selected_id = selector.group(1), int(selector.group(2))
        city = self._city_name(city)
        result = await self._json(session, 'https://geocoding-api.open-meteo.com/v1/search',
                                  params={'name':city,'count':5,'language':'zh','format':'json'})
        places = result.get('results', [])
        if not isinstance(places, list):
            raise _LookupFailure('weather_invalid_location')
        places = [p for p in places if isinstance(p, dict) and self._coordinates(p)]
        exact = [p for p in places if self._city_name(p.get('name', '')) == city]
        candidates = exact or places
        if not candidates:
            return LookupResult('needs_location', reason='city_not_found')
        if selected_id is not None:
            selected = [p for p in candidates if type(p.get('id')) is int and p['id'] == selected_id]
            if len(selected) != 1:
                return self._city_choices(candidates, reason='city_selection_not_found')
            candidates = selected
        elif exact:
            # Geocoding includes tiny homonymous settlements: Beijing currently
            # has one PPLC (18.9m) and two population-unknown PPL villages. Only
            # an exact-name, unique large capital/province seat may dominate
            # same-country small PPLs. Cross-country or multiple city seats
            # remain ambiguous; population alone never chooses a city.
            capitals = [p for p in candidates if p.get('feature_code') in {'PPLC', 'PPLA'}
                        and type(p.get('population')) is int and p['population'] >= 1_000_000]
            if len(capitals) == 1:
                capital = capitals[0]
                country = capital.get('country_code')
                def minor_same_country(place):
                    population = place.get('population')
                    return (place is capital or (place.get('country_code') == country
                            and place.get('feature_code') == 'PPL'
                            and (population is None or type(population) is int and 0 <= population < 50_000)))
                if isinstance(country, str) and re.fullmatch(r'[A-Z]{2}', country) and all(
                        minor_same_country(place) for place in candidates):
                    candidates = [capital]
        first = candidates[0]
        distinct = any(p.get('country_code') != first.get('country_code') or p.get('admin1') != first.get('admin1')
                       or abs(p['latitude'] - first['latitude']) > .5 or abs(p['longitude'] - first['longitude']) > .5
                       for p in candidates[1:])
        if distinct:
            return self._city_choices(candidates)
        params = {'latitude':first['latitude'],'longitude':first['longitude'], 'timezone':'auto',
                  'forecast_days':plan.day_offset + 1,
                  'current':'temperature_2m,apparent_temperature,weather_code,wind_speed_10m',
                  'daily':'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max'}
        forecast = await self._json(session, 'https://api.open-meteo.com/v1/forecast', params=params)
        daily = forecast.get('daily')
        if not isinstance(daily, dict) or not isinstance(daily.get('time'),list) or len(daily['time']) <= plan.day_offset:
            raise _LookupFailure('weather_invalid_forecast')
        day = {k:v[plan.day_offset] for k,v in daily.items() if k in {'time','weather_code','temperature_2m_max','temperature_2m_min','precipitation_probability_max'} and isinstance(v,list) and len(v)>plan.day_offset}
        current = forecast.get('current', {}) if plan.day_offset == 0 else {}
        current = {k:v for k,v in current.items() if k in {'time','temperature_2m','apparent_temperature','weather_code','wind_speed_10m'}} if isinstance(current,dict) else {}
        evidence = {'city':_text(first.get('name'),60),'region':_text(first.get('admin1'),60),
                    'country':_text(first.get('country'),60),'timezone':_text(forecast.get('timezone'),80),
                    'current':current,'day':day,'temperature_unit':'Celsius','wind_unit':'km/h',
                    'data_kind':'weather_model_not_station_observation','retrieved_at_unix':int(time.time())}
        data = json.dumps(evidence,ensure_ascii=False,separators=(',',':'))
        return LookupResult('ok',(LookupSource('Open-Meteo天气','https://open-meteo.com/',data[:650]),),data=data)

    @staticmethod
    def _city_name(value):
        name = ' '.join(_text(value, 80).split()).casefold()
        if re.fullmatch(r'[\u4e00-\u9fff]{2,}市', name):
            name = name[:-1]
        return name

    @classmethod
    def _city_choices(cls, candidates, *, reason='city_ambiguous'):
        choices = []
        for place in candidates[:3]:
            name = ' '.join(_text(place.get('name'), 40).split())
            choice = {'name':name, 'region':_text(place.get('admin1'), 60),
                      'country':_text(place.get('country'), 60)}
            identifier = place.get('id')
            selected = f'{name}#{identifier}'
            if (type(identifier) is int and 0 < identifier < 10_000_000_000 and len(selected) <= 40
                    and re.fullmatch(r'[\w .\u2019\u2018\u2013\u2014\x27-]+', name)):
                choice['query'] = f'/查 {selected} 天气'
            choices.append(choice)
        return LookupResult('needs_location', data=json.dumps({'candidates':choices}, ensure_ascii=False), reason=reason)

    @staticmethod
    def _coordinates(place):
        lat, lon = place.get('latitude'), place.get('longitude')
        return (not isinstance(lat,bool) and isinstance(lat,(int,float)) and math.isfinite(lat) and -90 <= lat <= 90
                and not isinstance(lon,bool) and isinstance(lon,(int,float)) and math.isfinite(lon) and -180 <= lon <= 180)

    async def _github(self, session, plan):
        if plan.kind == 'github_search':
            raw = await self._json(session, 'https://api.github.com/search/repositories', params={'q':plan.query,'per_page':self.settings.max_sources})
            items = raw.get('items')
            if not isinstance(items,list):
                raise _LookupFailure('github_invalid_results')
            sources = tuple(s for item in items[:3] if (s := self._repo_source(item)) is not None)[:self.settings.max_sources]
            return LookupResult('ok' if sources else 'empty', sources, reason='' if sources else 'no_usable_results')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}',plan.repo) or plan.repo.endswith('/.') or plan.repo.endswith('/..'):
            raise _LookupFailure('github_repository_invalid')
        base = 'https://api.github.com/repos/' + plan.repo
        # Child requests share the same enclosing cancellation/total budget.
        tasks = [asyncio.create_task(self._json(session, base)),
                 asyncio.create_task(self._json(session, base+'/readme', optional=True)),
                 asyncio.create_task(self._json(session, base+'/releases/latest', optional=True))]
        try:
            metadata, readme, release = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        source = self._repo_source(metadata)
        if source is None:
            raise _LookupFailure('github_invalid_repository')
        sources = [source]
        if readme and readme.get('encoding') == 'base64' and isinstance(readme.get('content'),str):
            try:
                body = base64.b64decode(readme['content'],validate=False).decode('utf-8')
            except (ValueError,UnicodeError):
                body = ''
            url = _public_url(readme.get('html_url'))
            if body and url:
                sources.append(LookupSource('README',url,_text(body,650)))
        if release:
            url = _public_url(release.get('html_url'))
            if url and not release.get('draft') and not release.get('prerelease'):
                body = json.dumps({'tag':_text(release.get('tag_name'),100),'published_at':_text(release.get('published_at'),50),'notes':_text(release.get('body'),450)},ensure_ascii=False)
                sources.append(LookupSource('最新正式发布',url,body[:650]))
        return LookupResult('ok',tuple(sources[:self.settings.max_sources]),data=json.dumps({'retrieved_at_unix':int(time.time())}))

    @staticmethod
    def _repo_source(item):
        if not isinstance(item,dict) or item.get('private') is True:
            return None
        url = _public_url(item.get('html_url'))
        if not url or urlsplit(url).hostname != 'github.com':
            return None
        info = {k:item.get(k) for k in ('full_name','description','language','stargazers_count','archived','pushed_at')}
        info['license'] = (item.get('license') or {}).get('spdx_id') if isinstance(item.get('license'),dict) else None
        return LookupSource(_text(item.get('full_name'),100) or 'GitHub仓库',url,_text(json.dumps(info,ensure_ascii=False),650))

    async def close(self):
        self._closed = True
        self._cache.clear()
        jobs = tuple(self._jobs)
        for task in jobs:
            if not task.done():
                task.cancel()
        if jobs:
            _, pending = await asyncio.wait(jobs,timeout=.3)
            if pending:
                self._blocked = True
                raise RuntimeError('lookup_cleanup_unsettled')
