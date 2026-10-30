"""Synthetic sources only; no real HTTP, credentials, history or chat sends."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.lookup_intent import classify_lookup, research_eligible, LookupPlan
from plugins.astrbot_plugin_roleplay_gateway.online_lookup import (
    LookupSettings, LookupSource, LookupResult, OnlineLookupService, MAX_HTTP_BYTES,
    MAX_REFERENCE_CHARACTERS, _public_url,
)


class FakeContent:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, size):
        for offset in range(0,len(self.data),size):
            yield self.data[offset:offset+size]


class FakeResponse:
    def __init__(self, value=None, *, status=200, data=None, headers=None, delay=0):
        self.status, self.delay = status, delay
        raw = json.dumps(value).encode() if data is None else data
        self.headers = {} if headers is None else headers
        self.content = FakeContent(raw)
        self.exited = False

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self,*args):
        self.exited = True


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = responses, []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self,*args):
        self.closed = True

    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        response = self.responses[url]
        return response() if callable(response) else response


class IntentTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(LookupSettings.from_mapping(),LookupSettings())
        for value in ({'enabled':1},{'timeout_seconds':0},{'timeout_seconds':9},
                      {'timeout_seconds':float('nan')},{'max_sources':4},{'unknown':1}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                LookupSettings.from_mapping(value)

    def test_chat_and_negative_queries_do_not_trigger(self):
        for text in ('你好三叶','今天很累','今天天气真好','不要搜索，陪我聊聊','别联网查资料','不用搜索，解释一下彩虹',
                     '你是什么角色','我们是什么关系','今天是什么心情','你喜欢的颜色是什么','你是谁'):
            with self.subTest(text=text):
                self.assertIsNone(classify_lookup(text))

    def test_explicit_query_and_visible_mentions(self):
        for text in ('/查 Python asyncio','三叶，帮我查 Python asyncio','@机器人\u2005搜索 Python asyncio'):
            with self.subTest(text=text):
                plan = classify_lookup(text)
                self.assertEqual(plan.kind,'search')
                self.assertEqual(plan.query,'Python asyncio')
                self.assertTrue(research_eligible(text))

    def test_teaching_has_no_network_requirement(self):
        for text in ('三叶，解释一下彩虹','教我Python列表','什么是微积分','What is a coroutine?'):
            self.assertEqual(classify_lookup(text).kind,'knowledge')

    def test_weather_explicit_location_and_day(self):
        cases = [('三叶，帮我查一下武汉明天天气','武汉',1),('北京今天天气怎么样','北京',0),
                 ('查上海后天天气','上海',2),('weather in London tomorrow','London',1)]
        for text,city,day in cases:
            with self.subTest(text=text):
                plan = classify_lookup(text,wake_names=('三叶',))
                self.assertEqual((plan.kind,plan.city,plan.day_offset),('weather',city,day))

    def test_no_inferred_weather_location(self):
        for text in ('今天天气如何','我这里明天天气','查附近天气'):
            self.assertEqual(classify_lookup(text).city,'')

    def test_github_known_repo_and_project_discovery(self):
        plan = classify_lookup('三叶，看看 https://github.com/AstrBotDevs/AstrBot 的README')
        self.assertEqual((plan.kind,plan.repo),('github_repo','AstrBotDevs/AstrBot'))
        self.assertEqual(classify_lookup('帮我找一个开源项目做知识库').kind,'github_search')
        self.assertIsNone(classify_lookup('https://evil.example/AstrBotDevs/AstrBot'))

    def test_untrusted_urls_are_not_citable(self):
        for url in ('http://127.0.0.1/x','https://127.1/x','https://localhost/x','https://host.local/x',
                    'https://user:pass@example.com/x','file:///etc/passwd','https://[::1]/','https://example.com:9999/x',
                    'https://example.com/?api_key=sk-syntheticlongsecret123'):
            self.assertEqual(_public_url(url),'')
        self.assertEqual(_public_url('https://docs.python.org/3/library/asyncio.html'),'https://docs.python.org/3/library/asyncio.html')

    def test_reference_json_remains_bounded_and_untrusted(self):
        result=LookupResult('ok',tuple(LookupSource('t'*100,'https://example.com/'+'a'*250,'引文"\n'*500) for _ in range(3)),data='数'*9000)
        raw=result.reference_text()
        self.assertLessEqual(len(raw),MAX_REFERENCE_CHARACTERS)
        parsed=json.loads(raw)
        self.assertTrue(parsed['untrusted_reference_data'])
        self.assertEqual(len(parsed['sources']),3)


class LookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.search_calls=[]
        async def search(query):
            self.search_calls.append(query)
            return [{'title':'官方资料','url':'https://docs.python.org/example','snippet':'公开资料片段。'}]
        self.search=search
        self.services=[]

    def service(self,session=None,**kwargs):
        service=OnlineLookupService(LookupSettings(enabled=True),search=kwargs.pop('search',self.search),
                                    session_factory=(lambda:session) if session else None,**kwargs)
        self.services.append(service)
        return service

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()

    async def lookup(self,service,plan,**kwargs):
        return await service.lookup(plan,namespace=kwargs.pop('namespace','scope-a'),timeout_seconds=kwargs.pop('timeout_seconds',1),**kwargs)

    async def test_disabled_and_knowledge_do_not_search(self):
        disabled=OnlineLookupService(LookupSettings(),search=self.search)
        self.assertEqual((await self.lookup(disabled,LookupPlan('search',query='hello'))).status,'disabled')
        service=self.service()
        self.assertEqual((await self.lookup(service,LookupPlan('knowledge',query='解释一下'))).status,'knowledge')
        self.assertEqual(self.search_calls,[])
        await disabled.close()

    async def test_search_only_current_query_and_scoped_cache(self):
        service=self.service();plan=LookupPlan('search',query='asyncio documentation')
        first=await self.lookup(service,plan)
        second=await self.lookup(service,plan)
        third=await self.lookup(service,plan,namespace='scope-b')
        self.assertEqual(first.status,'ok');self.assertFalse(first.cached)
        self.assertTrue(second.cached);self.assertFalse(third.cached)
        self.assertEqual(self.search_calls,[plan.query,plan.query])
        self.assertNotIn('scope-a',first.reference_text())

    async def test_query_credentials_and_overlong_queries_not_sent(self):
        service=self.service()
        for query,reason in [('api_key=sk-secretvalue123456789','query_contains_credentials'),('长'*129,'query_length_invalid'),('','query_length_invalid')]:
            self.assertEqual((await self.lookup(service,LookupPlan('search',query=query))).reason,reason)
        self.assertEqual(self.search_calls,[])

    async def test_sources_are_bounded_deduplicated_and_private_urls_removed(self):
        async def search(query):
            return [{'url':'http://127.0.0.1/private','snippet':'private'},
                    {'url':'https://example.com/a','title':'A','snippet':'x'*5000},
                    {'url':'https://example.com/a','snippet':'duplicate'},
                    {'url':'https://example.com/b','summary':'B'},
                    {'url':'https://example.com/c','snippet':'C'},
                    {'url':'https://example.com/d','snippet':'D'}]
        result=await self.lookup(self.service(search=search),LookupPlan('search',query='public'))
        self.assertEqual(len(result.sources),3)
        self.assertEqual(len(result.sources[0].excerpt),650)
        self.assertEqual([x.url for x in result.sources],['https://example.com/a','https://example.com/b','https://example.com/c'])

    async def test_empty_and_exception_are_static_and_not_cached(self):
        async def fail(query):
            raise RuntimeError('secret from vendor')
        result=await self.lookup(self.service(search=fail),LookupPlan('search',query='public'))
        self.assertEqual(result.reason,'lookup_failed')
        self.assertNotIn('secret',result.reference_text())
        async def empty(query):return []
        self.assertEqual((await self.lookup(self.service(search=empty),LookupPlan('search',query='public'))).status,'empty')

    async def test_timeout_cancels_cooperative_request_and_can_reuse(self):
        cancelled=[]
        async def slow(query):
            try: await asyncio.sleep(10)
            finally: cancelled.append(True)
        service=self.service(search=slow)
        result=await self.lookup(service,LookupPlan('search',query='public'),timeout_seconds=.01)
        self.assertEqual(result.reason,'lookup_timeout')
        self.assertEqual(cancelled,[True]);self.assertEqual(service.pending_tasks,0);self.assertTrue(service.reusable)

    async def test_caller_cancellation_settles_search(self):
        entered=asyncio.Event();cancelled=[]
        async def slow(query):
            entered.set()
            try: await asyncio.sleep(10)
            finally: cancelled.append(True)
        service=self.service(search=slow)
        task=asyncio.create_task(self.lookup(service,LookupPlan('search',query='public')))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(cancelled,[True]);self.assertEqual(service.pending_tasks,0)

    async def test_unsettled_search_blocks_further_requests(self):
        release=asyncio.Event()
        async def stubborn(query):
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
            return []
        service=self.service(search=stubborn)
        result=await self.lookup(service,LookupPlan('search',query='public'),timeout_seconds=.01)
        self.assertEqual(result.reason,'lookup_cleanup_unsettled')
        self.assertFalse(service.reusable)
        self.assertEqual((await self.lookup(service,LookupPlan('search',query='again'))).reason,'lookup_unavailable')
        release.set();await asyncio.sleep(0);await asyncio.sleep(0)

    async def test_close_cancels_inflight_and_prevents_more_work(self):
        entered=asyncio.Event()
        async def slow(query):entered.set();await asyncio.sleep(10)
        service=self.service(search=slow)
        task=asyncio.create_task(self.lookup(service,LookupPlan('search',query='public')))
        await entered.wait();await service.close();await task
        self.assertEqual(service.pending_tasks,0)
        self.assertEqual((await self.lookup(service,LookupPlan('search',query='next'))).reason,'lookup_unavailable')

    def weather_session(self,places=None,forecast=None):
        places = [{'name':'武汉','latitude':30.59,'longitude':114.30,'country':'中国','country_code':'CN','admin1':'湖北'}] if places is None else places
        forecast = {'timezone':'Asia/Shanghai','current':{'time':'2026-10-25T23:00','temperature_2m':20},
                    'daily':{'time':['2026-10-25','2026-10-26','2026-10-27'],'temperature_2m_max':[25,24,23],
                             'temperature_2m_min':[15,14,13],'precipitation_probability_max':[10,20,30],
                             'weather_code':[0,1,2]}} if forecast is None else forecast
        return FakeSession({'https://geocoding-api.open-meteo.com/v1/search':FakeResponse({'results':places}),
                            'https://api.open-meteo.com/v1/forecast':FakeResponse(forecast)})

    async def test_weather_missing_city_does_not_http(self):
        session=self.weather_session();result=await self.lookup(self.service(session),LookupPlan('weather'))
        self.assertEqual(result.reason,'city_required');self.assertEqual(session.calls,[])

    async def test_weather_explicit_city_has_date_timezone_and_model_provenance(self):
        session=self.weather_session();result=await self.lookup(self.service(session),LookupPlan('weather',city='武汉',day_offset=1))
        self.assertEqual(result.status,'ok');data=json.loads(result.data)
        self.assertEqual(data['day']['time'],'2026-10-26')
        self.assertEqual(data['current'],{})
        self.assertEqual(data['timezone'],'Asia/Shanghai')
        self.assertEqual(data['data_kind'],'weather_model_not_station_observation')
        self.assertEqual(session.calls[0][1]['params']['name'],'武汉')
        self.assertFalse(session.calls[0][1]['allow_redirects']);self.assertTrue(session.closed)

    async def test_weather_ambiguous_city_is_not_guessed(self):
        session=self.weather_session(places=[{'name':'Springfield','latitude':40,'longitude':-90,'country_code':'US','admin1':'A'},
                                             {'name':'Springfield','latitude':30,'longitude':-90,'country_code':'US','admin1':'B'}])
        result=await self.lookup(self.service(session),LookupPlan('weather',city='Springfield'))
        self.assertEqual(result.reason,'city_ambiguous');self.assertEqual(len(session.calls),1)

    def beijing_places(self):
        # Public geocoder response verified 2026-10-26; names are all identical.
        return [
            {'id':1816670,'name':'北京','feature_code':'PPLC','population':18960744,
             'latitude':39.9075,'longitude':116.39723,'country_code':'CN','country':'中国','admin1':'北京市'},
            {'id':8404324,'name':'北京','feature_code':'PPL','population':None,
             'latitude':30.72608,'longitude':108.67483,'country_code':'CN','country':'中国','admin1':'重庆市'},
            {'id':10196578,'name':'北京','feature_code':'PPL','population':None,
             'latitude':30.9699,'longitude':103.94,'country_code':'CN','country':'中国','admin1':'四川'}]

    async def test_weather_beijing_capital_dominates_same_country_small_settlements(self):
        for city in ('北京', '北京市'):
            with self.subTest(city=city):
                session=self.weather_session(places=list(reversed(self.beijing_places())))
                result=await self.lookup(self.service(session),LookupPlan('weather',city=city))
                self.assertEqual(result.status,'ok')
                self.assertEqual(json.loads(result.data)['region'],'北京市')
                self.assertEqual(session.calls[1][1]['params']['latitude'],39.9075)
                self.assertEqual(len(session.calls),2)

    async def test_weather_other_large_seats_or_other_country_remain_ambiguous(self):
        for changes in ({'feature_code':'PPLA','population':2_000_000},
                        {'country_code':'US','country':'美国'},
                        {'population':100_000}, {'feature_code':None}):
            with self.subTest(changes=changes):
                places=self.beijing_places()
                places[1].update(changes)
                session=self.weather_session(places=places)
                result=await self.lookup(self.service(session),LookupPlan('weather',city='北京'))
                self.assertEqual(result.reason,'city_ambiguous')
                self.assertEqual(len(session.calls),1)
                choices=json.loads(result.data)['candidates']
                self.assertEqual(choices[1]['query'],'/查 北京#8404324 天气')

    async def test_weather_selector_only_uses_returned_candidate_id(self):
        session=self.weather_session(places=self.beijing_places())
        result=await self.lookup(self.service(session),LookupPlan('weather',city='北京#8404324'))
        self.assertEqual(result.status,'ok')
        self.assertEqual(session.calls[0][1]['params']['name'],'北京')
        self.assertEqual(session.calls[1][1]['params']['latitude'],30.72608)
        session=self.weather_session(places=self.beijing_places())
        result=await self.lookup(self.service(session),LookupPlan('weather',city='北京#999999'))
        self.assertEqual(result.reason,'city_selection_not_found')
        self.assertEqual(len(session.calls),1)
        self.assertEqual(len(json.loads(result.data)['candidates']),3)

    async def test_weather_fuzzy_matches_never_use_capital_dominance(self):
        session=self.weather_session(places=self.beijing_places())
        result=await self.lookup(self.service(session),LookupPlan('weather',city='somewhere'))
        self.assertEqual(result.reason,'city_ambiguous')
        self.assertEqual(len(session.calls),1)

    async def test_weather_no_result_and_invalid_coordinates_do_not_forecast(self):
        session=self.weather_session(places=[{'name':'X','latitude':True,'longitude':40}])
        result=await self.lookup(self.service(session),LookupPlan('weather',city='X'))
        self.assertEqual(result.reason,'city_not_found');self.assertEqual(len(session.calls),1)

    async def test_http_redirect_rate_limit_and_size_bounds(self):
        for response,reason in [(FakeResponse({},status=302),'source_http_error'),
                                (FakeResponse({},status=429),'source_rate_limited'),
                                (FakeResponse({},headers={'Content-Length':str(MAX_HTTP_BYTES+1)}),'source_too_large'),
                                (FakeResponse(data=b'x'*(MAX_HTTP_BYTES+1)),'source_too_large'),
                                (FakeResponse(data=b'not json'),'source_invalid_json')]:
            with self.subTest(reason=reason):
                session=FakeSession({'https://geocoding-api.open-meteo.com/v1/search':response})
                result=await self.lookup(self.service(session),LookupPlan('weather',city='Wuhan'))
                self.assertEqual(result.reason,reason);self.assertTrue(session.closed)

    def github_session(self):
        base='https://api.github.com/repos/example/project'
        repo={'full_name':'example/project','html_url':'https://github.com/example/project','description':'A useful library',
              'private':False,'language':'Python','stargazers_count':123,'license':{'spdx_id':'MIT'}}
        readme={'encoding':'base64','content':base64.b64encode(b'# Project\nRead-only test').decode(),
                'html_url':'https://github.com/example/project/blob/main/README.md'}
        release={'tag_name':'v1.0','published_at':'2026-01-01','body':'Changes','html_url':'https://github.com/example/project/releases/tag/v1.0'}
        return FakeSession({base:FakeResponse(repo),base+'/readme':FakeResponse(readme),base+'/releases/latest':FakeResponse(release)})

    async def test_github_repo_reads_metadata_readme_release_only(self):
        session=self.github_session();result=await self.lookup(self.service(session),LookupPlan('github_repo',repo='example/project'))
        self.assertEqual(result.status,'ok');self.assertEqual(len(result.sources),3)
        self.assertIn('Read-only test',result.sources[1].excerpt)
        self.assertEqual(len(session.calls),3)
        self.assertTrue(all(url.startswith('https://api.github.com/repos/example/project') for url,_ in session.calls))
        self.assertTrue(all('Authorization' not in kwargs['headers'] for _,kwargs in session.calls))
        self.assertTrue(session.closed)

    async def test_github_missing_readme_or_release_is_allowed(self):
        session=self.github_session()
        for path in ('readme','releases/latest'):
            session.responses['https://api.github.com/repos/example/project/'+path]=FakeResponse({},status=404)
        result=await self.lookup(self.service(session),LookupPlan('github_repo',repo='example/project'))
        self.assertEqual(result.status,'ok');self.assertEqual(len(result.sources),1)

    async def test_github_repo_path_injection_and_private_repo_rejected(self):
        service=self.service(self.github_session())
        for repo in ('../private','example/../x','x/y?token=s','x/y/z'):
            self.assertEqual((await self.lookup(service,LookupPlan('github_repo',repo=repo))).reason,'github_repository_invalid')
        session=self.github_session()
        session.responses['https://api.github.com/repos/example/project']=FakeResponse({'private':True,'html_url':'https://github.com/example/project'})
        self.assertEqual((await self.lookup(self.service(session),LookupPlan('github_repo',repo='example/project'))).reason,'github_invalid_repository')

    async def test_repository_search_uses_repositories_not_code_endpoint(self):
        session=FakeSession({'https://api.github.com/search/repositories':FakeResponse({'items':[{'full_name':'a/b','html_url':'https://github.com/a/b'}]})})
        result=await self.lookup(self.service(session),LookupPlan('github_search',query='knowledge base'))
        self.assertEqual(result.status,'ok')
        self.assertEqual(session.calls[0][1]['params'],{'q':'knowledge base','per_page':3})

    async def test_lookup_request_bounds(self):
        service=self.service()
        for plan,namespace,timeout in [(LookupPlan('invalid'),'x',1),(LookupPlan('search',query='x'),'',1),
                                        (LookupPlan('search',query='x'),'x',float('inf'))]:
            self.assertEqual((await service.lookup(plan,namespace=namespace,timeout_seconds=timeout)).reason,'invalid_lookup_request')


if __name__ == '__main__':
    unittest.main()
