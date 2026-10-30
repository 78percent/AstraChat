"""Scheduler/worldbook integration using isolated stores and fake deliveries."""
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import AsyncMock, patch

from plugins.astrbot_plugin_roleplay_gateway.console_features import ConsoleFeatures, CHINA
from plugins.astrbot_plugin_roleplay_gateway.worldbook import validate_entries, select_entries
from plugins.astrbot_plugin_roleplay_gateway.context_budget import build_prompt
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import SpeakerInput, assemble_messages
from plugins.astrbot_plugin_roleplay_gateway.persona import PersonaSnapshot
from plugins.astrbot_plugin_roleplay_gateway.memory_store import MemoryStore, Namespace, MemoryInput, MemoryReply, MemorySnapshot
from scripts.wechat_native_bridge_core import BridgeCore, BridgeJournal, BridgeSettings
from scripts.wechat_scheduled_bridge import ScheduledBridge

def entry(identity='a', **changes):
    return dict(id=identity, title='星空', content='小镇里有一座天文台。', enabled=True, mode='always', position='after', keywords=[]) | changes

class WorldbookTests(unittest.TestCase):
    def test_keywords_order_disabled_and_position_reach_actual_assembler(self):
        values = validate_entries([entry('a', position='before'), entry('b', mode='keyword', keywords=['STAR'], content='星星设定'), entry('c', enabled=False)])
        before, after, selected = select_entries(values, 'Tell me about stars')
        self.assertEqual(selected, ['a', 'b'])
        persona = PersonaSnapshot('p', 'rev', '人物', '独立人设')
        memory = MemorySnapshot(Namespace('x', 'private', 'p', 'p'), 0, 0, '', (), 0)
        prepared = build_prompt(persona, memory, (SpeakerInput('u','u','m','hi'),), runtime_boundary='运行约束', before_worldbook=before, after_worldbook=after)
        self.assertEqual([m.content for m in assemble_messages(prepared.parts)[:4]], ['运行约束', '小镇里有一座天文台。', '独立人设', '星星设定'])
        self.assertGreater(prepared.report['estimated_tokens']['worldbook'], 0)
        self.assertEqual(select_entries(values, 'unrelated')[2], ['a'])

    def test_worldbook_budget_and_invalid_keywords_fail_before_model_call(self):
        for value in ([entry(content='中'*1600)], [entry(mode='keyword')], [entry(), entry()]):
            with self.assertRaises(ValueError):
                validate_entries(value)

class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1793170000.0
        self.memory = MemoryStore(Path(self.temp.name)/'memory.sqlite3')
        await self.memory.open()
        self.ns = Namespace('native', 'group', '123@chatroom', 'test-role')
        await self.memory.commit_turn(self.ns, expected_epoch=0, batch_id='batch', persona_revision='revision',
            inputs=(MemoryInput('m', 'u', '用户', '记得那只猫'),), replies=(MemoryReply(0, 'out', '猫叫小白'),), complete=True)
        self.adapter = Obj(pending_tasks=0, reusable=True)
        self.parts = []
        async def consume(adapter, parts, callback):
            self.parts.append(parts)
            await callback('记得起来活动一下。')
        self.service = Obj(settings=Obj(allowed_group_ids={'123@chatroom'}, admin_private_ids=set(), platform_id='native'),
            _closing=False, blocked=False, persona=PersonaSnapshot('test-role', 'revision', '朋友', '友善的测试人设'), memory=self.memory,
            _lease_adapter=lambda:self.adapter, _consume_reply=consume, _available=[], _block=lambda reason:None)
        self.server = Obj(send_scheduled=AsyncMock(), _connection=Obj(closed=False, scheduled=True))
        self.plugin = Obj(config={'persona_id':'test-role'}, native_service=self.service, native_server=self.server,
                          _native_enabled=lambda:True, _shared_parallel=asyncio.Semaphore(2), _emit=lambda _:None)
        self.admin = Obj(plugin=self.plugin, data=Path(self.temp.name), lock=asyncio.Lock())
        self.features = ConsoleFeatures(self.admin, clock=lambda:self.now)
        self.target = self.features.targets()[0]['id']

    async def asyncTearDown(self):
        await self.features.close()
        await self.memory.close()
        self.temp.cleanup()

    def spec(self, **changes):
        return dict(targetId=self.target, content='休息一下', mode='fixed', repeat='none', enabled=True,
                    at=datetime.fromtimestamp(self.now+20, CHINA).isoformat()) | changes

    async def manage(self, action, **changes):
        return await self.features.manage(dict(action=action, revision=self.features.state['revision']) | changes)

    async def test_worldbook_persists_isolates_personas_and_rejects_stale_edit(self):
        await self.manage('worldbook', personaId='test-role', entries=[entry()])
        restored = ConsoleFeatures(self.admin, clock=lambda:self.now)
        self.assertEqual(restored.worlds('test-role'), [entry()])
        self.assertEqual(restored.worlds('another'), [])
        with self.assertRaisesRegex(ValueError, 'revision_conflict'):
            await self.features.manage({'action':'worldbook','revision':0,'personaId':'test-role','entries':[]})
        self.assertEqual(self.features.worlds('test-role'), [entry()])

    async def test_fixed_timer_sends_once_and_survives_restart(self):
        await self.manage('save_task', task=self.spec())
        await self.manage('scheduler', enabled=True)
        await self.features.run_due()
        self.server.send_scheduled.assert_not_awaited()
        self.now += 21
        await self.features.run_due()
        self.server.send_scheduled.assert_awaited_once()
        self.assertEqual(self.features.state['runs'][0]['status'], 'api_accepted')
        restored = ConsoleFeatures(self.admin, clock=lambda:self.now)
        await restored.run_due()
        self.server.send_scheduled.assert_awaited_once()

    async def test_generated_preview_uses_persona_worldbook_and_history_without_sending(self):
        await self.manage('worldbook', personaId='test-role', entries=[entry()])
        result = await self.features.manage({'action':'preview','task':self.spec(mode='persona')})
        self.assertEqual(result, {'text':'记得起来活动一下。','sent':False})
        combined = str(assemble_messages(self.parts[0]))
        for text in ('友善的测试人设','小镇里有一座天文台','猫叫小白','休息一下'):
            self.assertIn(text, combined)
        self.server.send_scheduled.assert_not_awaited()
        self.assertEqual((await self.memory.snapshot(self.ns)).last_sequence, 1)

    async def test_generated_task_uses_same_renderer_at_due_time(self):
        await self.manage('save_task', task=self.spec(mode='persona'))
        await self.manage('scheduler', enabled=True)
        self.now += 21
        await self.features.run_due()
        self.assertEqual(self.server.send_scheduled.call_args.args[2], '记得起来活动一下。')

    async def test_unknown_send_is_not_retried_and_unfinished_restart_is_unknown(self):
        await self.manage('save_task', task=self.spec())
        await self.manage('scheduler', enabled=True)
        self.server.send_scheduled.side_effect = TimeoutError()
        self.now += 21
        await self.features.run_due()
        self.assertEqual(self.features.state['runs'][0]['status'], 'unknown')
        self.features.state['runs'][0]['status'] = 'dispatching'
        self.features.persist()
        restored = ConsoleFeatures(self.admin, clock=lambda:self.now)
        self.assertEqual(restored.state['runs'][0]['status'], 'unknown')
        await restored.run_due()
        self.server.send_scheduled.assert_awaited_once()

    async def test_missed_repeating_task_advances_without_catchup_and_persists(self):
        await self.manage('save_task', task=self.spec(repeat='daily'))
        await self.manage('scheduler', enabled=True)
        self.now += 3*86400
        await self.features.run_due()
        self.server.send_scheduled.assert_not_awaited()
        restored = ConsoleFeatures(self.admin, clock=lambda:self.now)
        self.assertGreater(restored.state['tasks'][0]['nextRun'], self.now)
        self.assertEqual(restored.state['runs'][0]['status'], 'missed')

    async def test_paused_deleted_and_restored_tasks_do_not_send(self):
        data = await self.manage('save_task', task=self.spec())
        identity = data['tasks'][0]['id']
        await self.manage('delete_task', id=identity)
        await self.manage('scheduler', enabled=True)
        self.now += 30
        await self.features.run_due()
        await self.manage('restore_task', id=identity)
        await self.features.run_due()
        self.server.send_scheduled.assert_not_awaited()

    async def test_disabled_scheduler_does_not_generate_or_send(self):
        await self.manage('save_task', task=self.spec(mode='persona'))
        self.now += 21
        await self.features.run_due()
        self.assertEqual(self.parts, [])
        self.server.send_scheduled.assert_not_awaited()

    async def test_disk_error_rolls_back_worldbook_memory(self):
        with patch('plugins.astrbot_plugin_roleplay_gateway.console_admin.atomic_json', side_effect=OSError()):
            with self.assertRaises(OSError):
                await self.manage('worldbook', personaId='test-role', entries=[entry()])
        self.assertEqual(self.features.worlds('test-role'), [])
        self.assertEqual(self.features.state['revision'], 0)

    async def test_arbitrary_recipients_and_past_one_offs_are_rejected(self):
        for spec in (self.spec(targetId='arbitrary'), self.spec(at=datetime.fromtimestamp(self.now-1,CHINA).isoformat()), self.spec(content='中'*126)):
            with self.assertRaises(ValueError):
                await self.manage('save_task', task=spec)
        self.assertEqual(self.features.state['tasks'], [])

class ScheduledHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        settings = BridgeSettings(True, 'wxid_test', '123@chatroom', 'a'*64)
        self.journal = BridgeJournal(self.root/'normal.sqlite3')
        self.core = BridgeCore(settings, self.journal, '1'*32)
        self.bridge = ScheduledBridge(self.core, self.root/'scheduled.sqlite3')
        self.frame = dict(type='scheduled_send',session='1'*32,request_id='2'*32,task_id='3'*32,room='123@chatroom',text='休息一下',expires_at=1008.0)

    def tearDown(self):
        self.bridge.close()
        self.journal.close()
        self.temp.cleanup()

    def test_target_text_and_expiry_are_checked_independently(self):
        for patch_value in ({'room':'456@chatroom'}, {'text':'中'*126}, {'expires_at':2000}, {'expires_at':999}):
            prepared, response = self.bridge.prepare(self.frame | patch_value, 1000)
            self.assertIsNone(prepared)
            self.assertEqual(response['status'], 'rejected')
        self.assertEqual(self.bridge.db.execute('SELECT COUNT(*) FROM scheduled').fetchone()[0],0)

    def test_unknown_intent_survives_reconnect_without_duplicate_send(self):
        prepared, _ = self.bridge.prepare(self.frame,1000)
        self.assertIsNotNone(prepared)
        self.bridge.close()
        self.bridge=ScheduledBridge(self.core,self.root/'scheduled.sqlite3')
        prepared, response=self.bridge.prepare(self.frame,1001)
        self.assertIsNone(prepared)
        self.assertEqual(response['status'],'unknown')

    def test_native_completion_is_required_for_acceptance(self):
        prepared, _=self.bridge.prepare(self.frame,1000)
        result=self.bridge.complete(prepared,{'success':True},99)
        self.assertEqual(result['status'],'unknown')

    def test_successful_native_completion_replay_is_receipt_only(self):
        prepared, _=self.bridge.prepare(self.frame,1000)
        outcome={'pid':99,'code':0,'cleanup_code':0,'success':True,'report':{'completion_success':True,'target':prepared.room,'text':prepared.text}}
        self.assertEqual(self.bridge.complete(prepared,outcome,99)['status'],'native_completed')
        prepared, result=self.bridge.prepare(self.frame,1001)
        self.assertIsNone(prepared)
        self.assertEqual(result['status'],'native_completed')

if __name__ == '__main__':
    unittest.main()
