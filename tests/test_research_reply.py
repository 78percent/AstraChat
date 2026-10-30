"""Offline lookup-to-reply integration; no real API, account or native transport."""
import asyncio
from dataclasses import replace
import json
import time
import unittest

import test_roleplay_stream as fixtures
from test_provider_gate import FakeStream, response
from test_reply_pacing import fast_policy
from plugins.astrbot_plugin_roleplay_gateway.online_lookup import LookupResult, LookupSource, LookupSettings, OnlineLookupService
from plugins.astrbot_plugin_roleplay_gateway.research_output import fit_bubble, source_footer, bounded_reference, failure_text
from plugins.astrbot_plugin_roleplay_gateway.context_budget import estimate_tokens


class FakeLookup:
    settings = LookupSettings(enabled=True)
    pending_tasks = 0
    reusable = True
    plan = staticmethod(OnlineLookupService.plan)
    def __init__(self):
        self.calls = []
        self.result = LookupResult('ok', (LookupSource('公开文档','https://docs.example.com/guide','工具返回的独立事实。'),))
        self.closed = False
    async def lookup(self, plan, *, namespace, timeout_seconds):
        self.calls.append((plan,namespace,timeout_seconds))
        return self.result
    async def close(self):
        self.closed = True


class ResearchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    open_stores = fixtures.RoleplayStreamTests.open_stores
    factory = fixtures.RoleplayStreamTests.factory
    make_service = fixtures.RoleplayStreamTests.make_service
    send = fixtures.RoleplayStreamTests.send
    dispatch = fixtures.RoleplayStreamTests.dispatch
    payload = fixtures.RoleplayStreamTests.payload
    asyncTearDown = fixtures.RoleplayStreamTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.RoleplayStreamTests.asyncSetUp(self)
        self.lookup = FakeLookup()
        self.service.lookup = self.lookup

    def model_reply(self, speech):
        raw = json.dumps({'speech':speech},ensure_ascii=False)
        self.backend.streams.append(FakeStream([response(raw),response('',chunk=False)]))

    async def test_research_sources_data_role_and_only_sent_memory(self):
        self.model_reply(['先给出已核对的结论。','详细说明中可以使用函数 f(x)，并说明适用范围。'])
        event = fixtures.fixture_event(text='机器人 /查 公开技术资料')
        await self.dispatch(event)
        self.assertEqual(len(self.outputs),2)
        self.assertIn('https://docs.example.com/guide',self.outputs[0][1])
        messages=self.payload()
        reference=next(m for m in messages if 'untrusted_reference_data' in m['content'] and '工具返回的独立事实' in m['content'])
        self.assertEqual(reference['role'],'user')
        self.assertNotIn('中性向导',self.lookup.calls[0][0].query)
        snapshot=await self.memory.snapshot(self.service.namespace(event))
        self.assertEqual(len(snapshot.turns),1)
        self.assertEqual([r.text for r in snapshot.turns[0].replies],[v[1] for v in self.outputs])
        self.assertNotIn('工具返回的独立事实',json.dumps([r.text for r in snapshot.turns[0].replies],ensure_ascii=False))

    async def test_normal_chat_does_not_lookup_or_gain_larger_budget(self):
        self.service.settings=replace(self.settings,reply_pacing=fast_policy())
        self.model_reply(['普通聊天一句。','普通聊天二句。'])
        await self.dispatch(fixtures.fixture_event(text='机器人今天有点累'))
        self.assertFalse(self.lookup.calls)
        self.assertEqual(len(self.outputs),2)
        self.assertNotIn('资料来源',self.outputs[0][1])

    async def test_probability_skipped_research_does_not_query(self):
        self.service.settings=replace(self.settings,reply_pacing=fast_policy(probability=0))
        await self.dispatch(fixtures.fixture_event(text='机器人 /查 最新版本'))
        self.assertFalse(self.lookup.calls)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.outputs)

    async def test_lookup_failure_is_fixed_honest_reply_without_model(self):
        self.lookup.result=LookupResult('error',reason='lookup_timeout')
        await self.dispatch(fixtures.fixture_event(text='机器人 /查 最新资料'))
        self.assertFalse(self.backend.calls)
        self.assertEqual(len(self.outputs),1)
        self.assertIn('没有成功',self.outputs[0][1])
        self.assertNotIn('https://',self.outputs[0][1])

    async def test_missing_city_asks_location_without_model(self):
        self.lookup.result=LookupResult('needs_location',reason='city_required')
        await self.dispatch(fixtures.fixture_event(text='机器人今天天气怎么样'))
        self.assertFalse(self.backend.calls)
        self.assertIn('具体城市',self.outputs[0][1])

    async def test_research_two_bubble_cap_and_fake_url_removed(self):
        self.model_reply(['见 https://fake.invalid/bad 有结论。','再解释。','这条不能发。'])
        await self.dispatch(fixtures.fixture_event(text='机器人 /查 资料'))
        self.assertEqual(len(self.outputs),2)
        self.assertNotIn('fake.invalid',''.join(v[1] for v in self.outputs))
        self.assertLessEqual(sum(len(v[1]) for v in self.outputs),800)

    async def test_expired_ingress_does_not_start_lookup_or_model(self):
        event=replace(fixtures.fixture_event(text='机器人 /查 资料'),received_at=time.time()-19)
        await self.dispatch(event)
        self.assertFalse(self.lookup.calls)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.outputs)

    async def test_reference_is_not_reused_in_next_ordinary_turn(self):
        self.model_reply(['有依据的答复。'])
        await self.dispatch(fixtures.fixture_event(text='机器人 /查 资料'))
        self.model_reply(['普通对话。'])
        await self.dispatch(fixtures.fixture_event('2',text='机器人你好'))
        self.assertEqual(len(self.lookup.calls),1)
        self.assertFalse(any('工具返回的独立事实' in m['content'] for m in self.payload(1)))
        self.assertFalse(self.service._lookup_turns)

    def stubborn_lookup(self, *, timeout_seconds):
        entered, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        async def search(query):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return []
        self.service.lookup = OnlineLookupService(
            LookupSettings(enabled=True, timeout_seconds=timeout_seconds), search=search)
        self.service._parallel = asyncio.Semaphore(1)
        return entered, release

    async def test_unsettled_lookup_timeout_retains_permit_and_open_ledgers(self):
        _, release = self.stubborn_lookup(timeout_seconds=.1)
        event = fixtures.fixture_event(text='机器人 /查 公开资料')
        await self.dispatch(event)
        self.assertEqual(self.service.lookup.pending_tasks, 1)
        self.assertTrue(self.service.blocked)
        self.assertTrue(self.service._parallel.locked())
        self.assertEqual(self.service._retained_parallel_permits, 1)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.outputs)
        with self.assertRaisesRegex(RuntimeError, 'lookup_cleanup_unsettled'):
            await self.service.close()
        self.assertIsNotNone(self.receipts._connection)
        self.assertIsNotNone(self.segments._connection)
        self.assertIsNotNone(self.memory._connection)
        self.assertEqual((await self.receipts.get(event.platform_id, event.incoming_message_id))['status'], 'FAILED')
        await self.segments.records(self.service._token(event))
        await self.memory.snapshot(self.service.namespace(event))
        release.set()
        await asyncio.sleep(.01)
        await self.service.close()
        # Late settlement does not restore a permit after its uncertain cleanup.
        self.assertTrue(self.service._parallel.locked())
        self.assertTrue(self.guard.blocked)

    async def test_close_during_unsettled_lookup_keeps_ledgers_and_permit(self):
        entered, release = self.stubborn_lookup(timeout_seconds=6)
        self.assertEqual(await self.service.accept(
            fixtures.fixture_event(text='机器人 /查 公开资料'), self.send), 'ACCEPTED')
        await asyncio.wait_for(entered.wait(), 1)
        with self.assertRaisesRegex(RuntimeError, 'lookup_cleanup_unsettled'):
            await asyncio.wait_for(self.service.close(), 2)
        self.assertEqual(self.service.lookup.pending_tasks, 1)
        self.assertTrue(self.service._parallel.locked())
        self.assertEqual(self.service._retained_parallel_permits, 1)
        self.assertTrue(self.service.blocked)
        self.assertFalse(self.outputs)
        self.assertIsNotNone(self.receipts._connection)
        self.assertIsNotNone(self.segments._connection)
        self.assertIsNotNone(self.memory._connection)
        release.set()

    async def test_cooperative_lookup_close_releases_permit_and_closes_ledgers(self):
        entered = asyncio.Event()
        async def search(query):
            entered.set()
            await asyncio.sleep(10)
        self.service.lookup = OnlineLookupService(LookupSettings(enabled=True), search=search)
        self.service._parallel = asyncio.Semaphore(1)
        await self.service.accept(fixtures.fixture_event(text='机器人 /查 公开资料'), self.send)
        await asyncio.wait_for(entered.wait(), 1)
        await self.service.close()
        self.assertEqual(self.service.lookup.pending_tasks, 0)
        self.assertFalse(self.service._parallel.locked())
        self.assertEqual(self.service._retained_parallel_permits, 0)
        self.assertFalse(self.service.blocked)
        self.assertIsNone(self.receipts._connection)
        self.assertIsNone(self.segments._connection)
        self.assertIsNone(self.memory._connection)

    async def test_other_healthy_lookup_does_not_retain_finished_turn_permit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        async def search(query):
            if query == '慢资料':
                entered.set()
                await release.wait()
            return [{'title':'资料', 'url':'https://example.com/guide', 'snippet':'已核对事实。'}]
        self.service.lookup = OnlineLookupService(LookupSettings(enabled=True), search=search)
        self.service._parallel = asyncio.Semaphore(2)
        self.assertEqual(await self.service.accept(
            fixtures.fixture_event('1', group='201', text='机器人 /查 慢资料'), self.send), 'ACCEPTED')
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(await self.service.accept(
            fixtures.fixture_event('2', text='机器人 /查 快资料'), self.send), 'ACCEPTED')
        async def fast_finished():
            while len(self.service._workers) != 1:
                await asyncio.sleep(.01)
        await asyncio.wait_for(fast_finished(), 2)
        self.assertEqual(self.service.lookup.pending_tasks, 1)
        self.assertFalse(self.service.blocked)
        self.assertEqual(self.service._retained_parallel_permits, 0)
        await asyncio.wait_for(self.service._parallel.acquire(), .1)
        self.service._parallel.release()
        release.set()
        await asyncio.wait_for(self.service.wait_idle(), 2)


class ResearchOutputTests(unittest.TestCase):
    def test_ambiguous_city_clarification_has_bounded_usable_choices(self):
        choices=[{'name':'北京', 'country':'中国'*50, 'region':'行政区'*50,
                  'query':f'/查 北京#{identifier} 天气'} for identifier in (1816670,8404324,10196578)]
        result=LookupResult('needs_location',reason='city_ambiguous',data=json.dumps({'candidates':choices}))
        text=failure_text(result)
        self.assertIn('/查 北京#8404324 天气', text)
        self.assertLessEqual(len(text), 280)
        self.assertLessEqual(len(text.encode('utf-8')), 900)
        for choice in choices:
            plan=OnlineLookupService.plan(choice['query'])
            self.assertEqual(plan.kind,'weather')
            self.assertIn('#',plan.city)

    def test_credentials_reason_has_actionable_failure_text(self):
        text = failure_text(LookupResult('error', reason='query_contains_credentials'))
        self.assertIn('密钥', text)
        self.assertIn('去掉', text)

    def test_utf8_limit_includes_source_footer(self):
        sources=(LookupSource('资料','https://example.com/data','x'),)
        footer=source_footer(sources,byte_limit=1023)
        result=fit_bubble('这是一段完整的解释。'*100,remaining=800,byte_limit=1023,footer=footer)
        self.assertLessEqual(len(result.encode('utf-8')),1023)
        self.assertLessEqual(len(result),800)
        self.assertIn('https://example.com/data',result)
    def test_reference_trim_keeps_valid_json_and_identity(self):
        result=LookupResult('ok',(LookupSource('资料','https://example.com/data','资料'*3000),),data='数据'*3000)
        encoded=bounded_reference(result)
        parsed=json.loads(encoded)
        self.assertEqual(parsed['sources'][0]['url'],'https://example.com/data')
        self.assertLessEqual(estimate_tokens(json.dumps({'untrusted_reference_data':encoded},ensure_ascii=False))+8,3800)
    def test_long_sources_leave_space_for_answer(self):
        sources=tuple(LookupSource('资料','https://example.com/'+str(i)+'a'*300,'x') for i in range(3))
        footer=source_footer(sources,byte_limit=1023)
        self.assertLessEqual(len(footer),350)
        self.assertTrue(fit_bubble('有依据的结论。',remaining=800,byte_limit=1023,footer=footer))


if __name__=='__main__':
    unittest.main()
