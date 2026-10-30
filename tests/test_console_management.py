"""Synthetic management contracts; no account sends or live store writes."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from plugins.astrbot_plugin_roleplay_gateway.memory_store import MemoryStore, MemoryInput, MemoryReply, Namespace
from plugins.astrbot_plugin_roleplay_gateway.console_admin import ConsoleAdmin, config_version
from plugins.astrbot_plugin_roleplay_gateway.roleplay_stream import RoleplayStreamService
from plugins.astrbot_plugin_roleplay_gateway.context_budget import build_prompt
from plugins.astrbot_plugin_roleplay_gateway.persona import PersonaSnapshot
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import SpeakerInput
import console_launcher_control as launcher
from plugins.astrbot_plugin_roleplay_gateway import console_admin
from plugins.astrbot_plugin_roleplay_gateway.gate import GateSettings
from plugins.astrbot_plugin_roleplay_gateway.reply_pacing import ReplyPacing
from plugins.astrbot_plugin_roleplay_gateway.stream_reply import ReplyLimits


class ContextManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='console-management-')
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / 'memory.sqlite3')
        await self.store.open()
        self.ns = Namespace('test', 'private', 'first', 'test-role')
        self.other = Namespace('test', 'private', 'second', 'test-role')
        for ns in (self.ns, self.other):
            await self.store.commit_turn(ns, expected_epoch=0, batch_id='batch', persona_revision='revision',
                inputs=(MemoryInput('in', 'sender', '用户', '原始输入'),), replies=(MemoryReply(0, 'out', '原始回复'),), complete=True)
        self.base = {'version': '0:1:0', 'action': 'edit', 'sequence': 1, 'role': 'user', 'index': 0, 'text': '修改后的输入'}

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def test_edit_reaches_actual_model_prompt_and_retains_original_recovery(self):
        snapshot = await self.store.console_change(self.ns, self.base)
        self.assertEqual(snapshot.epoch, 1)
        self.assertTrue(snapshot.turns[0].inputs[0].edited)
        prepared = build_prompt(PersonaSnapshot('test-role','revision','星星','中性测试人设'), snapshot,
                                (SpeakerInput('u','用户','2','你好'),), runtime_boundary='用中文回复')
        self.assertIn('修改后的输入', str(prepared.parts))
        backups = list((self.root / 'console-backups').glob('*.json'))
        self.assertEqual(len(backups), 1)
        self.assertIn('原始输入', backups[0].read_text(encoding='utf-8'))
        other = await self.store.snapshot(self.other)
        self.assertEqual((other.epoch, other.turns[0].inputs[0].text), (0, '原始输入'))

    async def test_delete_each_side_keeps_other_side_in_model_history(self):
        for role, namespace, kept in [('user', self.ns, '原始回复'), ('assistant', self.other, '原始输入')]:
            result = await self.store.console_change(namespace, {'version':'0:1:0', 'action':'delete_message', 'role':role, 'sequence':1, 'index':0})
            prepared = build_prompt(PersonaSnapshot('test-role','revision','星星','中性测试人设'), result,
                                   (SpeakerInput('u','用户','2','你好'),), runtime_boundary='用中文回复')
            self.assertIn(kept, str(prepared.parts))
            self.assertEqual(len(result.turns[0].inputs if role == 'user' else result.turns[0].replies), 0)
        result = await self.store.console_change(self.ns, {'version':'1:1:1', 'action':'delete_message', 'role':'assistant', 'sequence':1, 'index':0})
        self.assertEqual(result.turns, ())

    async def test_edit_group_is_atomic_and_preserves_opposite_side(self):
        await self.store.commit_turn(self.ns, expected_epoch=0, batch_id='second', persona_revision='revision',
            inputs=(MemoryInput('in2','sender','用户','第二次输入'),),
            replies=(MemoryReply(0,'out2','第一段'),MemoryReply(1,'out3','第二段')), complete=True)
        payload = {'version':'0:2:0','action':'edit_group','role':'assistant',
                   'messages':[{'sequence':2,'index':0},{'sequence':2,'index':1}], 'text':'合并修改后的回复'}
        snap = await self.store.console_change(self.ns,payload)
        self.assertEqual(snap.epoch,1)
        self.assertEqual(len(snap.turns[1].replies),1)
        self.assertEqual(snap.turns[1].replies[0].text,'合并修改后的回复')
        self.assertEqual(snap.turns[1].inputs[0].text,'第二次输入')
        self.assertEqual(snap.turns[0].replies[0].text,'原始回复')
        self.assertEqual(len(list((self.root/'console-backups').glob('*.json'))),1)
        with self.assertRaisesRegex(ValueError,'revision_conflict'):
            await self.store.console_change(self.ns,payload)

    async def test_delete_group_keeps_opposite_side_and_restores_backup(self):
        removed = await self.store.console_change(self.ns,{'version':'0:1:0','action':'delete_group','role':'user','messages':[{'sequence':1,'index':0}]})
        self.assertEqual(removed.turns[0].inputs,())
        self.assertEqual(removed.turns[0].replies[0].text,'原始回复')
        backup=next((self.root/'console-backups').glob('*.json')).stem
        restored=await self.store.console_change(self.ns,{'version':'1:1:1','action':'restore','backupId':backup})
        self.assertEqual(restored.turns[0].inputs[0].text,'原始输入')

    async def test_invalid_group_does_not_partially_change_messages(self):
        for refs in ([],[{'sequence':1,'index':False}], [{'sequence':1,'index':0},{'sequence':1,'index':0}], [{'sequence':1,'index':0},{'sequence':1,'index':99}]):
            with self.assertRaises(ValueError):
                await self.store.console_change(self.ns,{'version':'0:1:0','action':'delete_group','role':'assistant','messages':refs})
        self.assertEqual((await self.store.snapshot(self.ns)).epoch,0)
        self.assertFalse((self.root/'console-backups').exists())

    async def test_manual_backup_does_not_mutate_context_and_can_restore(self):
        result = await self.store.console_change(self.ns, {'version':'0:1:0','action':'backup'})
        self.assertEqual(result.epoch, 0)
        backup = next((self.root / 'console-backups').glob('*.json')).stem
        await self.store.console_change(self.ns, {'version':'0:1:0','action':'clear'})
        restored = await self.store.console_change(self.ns, {'version':'1:1:1','action':'restore','backupId':backup})
        self.assertEqual(restored.turns[0].replies[0].text, '原始回复')

    async def test_compare_and_swap_rejects_stale_form(self):
        await self.store.console_change(self.ns, self.base)
        with self.assertRaisesRegex(ValueError, 'revision_conflict'):
            await self.store.console_change(self.ns, {**self.base, 'text': '覆盖'})
        snap = await self.store.snapshot(self.ns)
        self.assertEqual(snap.turns[0].inputs[0].text, '修改后的输入')

    async def test_old_generation_cannot_commit_after_an_edit(self):
        await self.store.console_change(self.ns, self.base)
        accepted = await self.store.commit_turn(self.ns, expected_epoch=0, batch_id='late', persona_revision='revision',
            inputs=(MemoryInput('later','sender','用户','旧任务'),), replies=(MemoryReply(0,'later-out','旧回复'),), complete=True)
        self.assertFalse(accepted)

    async def test_service_invalidates_active_epoch_at_same_time_as_database(self):
        service = SimpleNamespace(_closing=False, memory=self.store, _clearing_namespaces={}, _memory_epochs={self.ns: 0}, _memory_clears=set())
        snapshot = await RoleplayStreamService.clear_memory(service, self.ns, mutation=lambda: self.store.console_change(self.ns, self.base))
        self.assertEqual(service._memory_epochs[self.ns], snapshot.epoch)
        self.assertEqual(service._clearing_namespaces, {})

    async def test_delete_and_restore_are_scoped_and_recoverable(self):
        removed = await self.store.console_change(self.ns, {'version':'0:1:0','action':'delete_turn','sequence':1})
        self.assertEqual(removed.turns, ())
        backup = next((self.root / 'console-backups').glob('*.json')).stem
        restored = await self.store.console_change(self.ns, {'version':'1:1:1','action':'restore','backupId':backup})
        self.assertEqual(restored.turns[0].inputs[0].text, '原始输入')
        self.assertEqual(restored.epoch, 2)

    async def test_summary_persists_and_clear_recovers_both_summary_and_history(self):
        summary = await self.store.console_change(self.ns, {'version':'0:1:0','action':'summary','text':'喜欢天文学'})
        self.assertEqual(summary.summary, '喜欢天文学')
        cleared = await self.store.console_change(self.ns, {'version':'1:1:1','action':'clear'})
        self.assertEqual((cleared.summary, cleared.turns), ('', ()))
        with self.assertRaisesRegex(ValueError, 'summary_budget_exceeded'):
            await self.store.console_change(self.ns, {'version':'2:1:2','action':'summary','text':'中' * 1000})

    async def test_noop_does_not_change_epoch_or_create_backup(self):
        result = await self.store.console_change(self.ns, {**self.base, 'text': '原始输入'})
        self.assertEqual(result.epoch, 0)
        self.assertFalse((self.root / 'console-backups').exists())

    async def test_invalid_paths_and_fields_do_not_write(self):
        for payload in ({**self.base, 'text':''}, {**self.base,'index':True}, {'version':'0:1:0','action':'restore','backupId':'../../secret'}):
            with self.assertRaises(ValueError):
                await self.store.console_change(self.ns, payload)
        self.assertEqual((await self.store.snapshot(self.ns)).epoch, 0)


class ManagementSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_management_requires_local_bearer_and_rejects_origin(self):
        plugin = SimpleNamespace(config={'native_bridge_token':'a' * 64})
        admin = ConsoleAdmin(plugin, ROOT)
        request = SimpleNamespace(headers={'Authorization':'Bearer ' + 'a' * 64, 'Origin':'https://foreign.test'}, method='GET')
        # No real web server or plugin import is needed for the authentication branch.
        import types
        module = types.ModuleType('aiohttp')
        module.web = SimpleNamespace(json_response=lambda data, status=200, **kw: (status,data))
        with patch.dict(sys.modules, {'aiohttp':module}):
            self.assertEqual((await admin.handle(request))[0],403)
            request.headers = {}
            self.assertEqual((await admin.handle(request))[0],403)

    def test_config_version_ignores_credentials_and_normalizes_new_defaults(self):
        self.assertEqual(config_version({}), config_version({'console_model':'','console_context_limit':12,'native_bridge_token':'secret'}))
        self.assertNotEqual(config_version({}), config_version({'console_model':'deepseek-other'}))

    async def test_stale_config_does_not_touch_provider_or_disk(self):
        plugin = SimpleNamespace(config={'persona_id':'old'})
        admin = ConsoleAdmin(plugin, ROOT)
        with self.assertRaisesRegex(ValueError,'revision_conflict'):
            await admin.execute({'op':'settings','configVersion':'stale'})

    async def test_unknown_operation_is_rejected(self):
        admin = ConsoleAdmin(SimpleNamespace(), ROOT)
        with self.assertRaisesRegex(ValueError,'invalid_action'):
            await admin.execute({'op':'shell','command':'something'})


class LauncherSafetyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows desktop leases')
    def test_console_opens_beside_legacy_without_raising_it_and_stays_single_instance(self):
        from scripts import wechat_desktop_gui as gui
        with tempfile.TemporaryDirectory() as d, patch.object(gui, 'DATA', Path(d)):
            legacy = gui.WindowLease()
            console = gui.WindowLease('console-window')
            duplicate = gui.WindowLease('console-window')
            try:
                self.assertTrue(legacy.acquire())
                self.assertTrue(console.acquire())
                self.assertFalse(duplicate.acquire())
                self.assertTrue((Path(d) / 'console-window.show').exists())
                self.assertFalse((Path(d) / 'desktop-window.show').exists())
            finally:
                duplicate.close()
                console.close()
                legacy.close()

    def test_original_entry_opens_console_without_forwarding_cli_flags(self):
        from scripts import start_wechat_desktop as startup
        with patch('scripts.astrbot_launcher.main', return_value=0) as open_console:
            self.assertEqual(startup.main([]), 0)
        open_console.assert_called_once_with([])

    def test_duplicate_start_never_spawns_second_supervisor(self):
        control = launcher.LauncherControl()
        with patch.object(control,'status',return_value={'canStart':False,'active':True}), patch.object(launcher.subprocess,'Popen') as popen:
            control.command({'action':'start'})
        popen.assert_not_called()

    def test_start_uses_existing_supervisor_and_hidden_process(self):
        control = launcher.LauncherControl()
        with tempfile.TemporaryDirectory() as d, patch.object(launcher,'DATA',Path(d)), patch.object(control,'status',return_value={'canStart':True,'active':False}), patch.object(launcher.subprocess,'Popen') as popen:
            control.command({'action':'start'})
        args, kwargs = popen.call_args
        self.assertIn('wechat_desktop_session.py', args[0][4])
        self.assertEqual(kwargs['env']['ASTRBOT_CONSOLE_EMBEDDED'],'1')
        self.assertNotIn('shell',kwargs)

    def test_launcher_restores_only_last_running_choice(self):
        control = launcher.LauncherControl()
        with tempfile.TemporaryDirectory() as d, patch.object(launcher, 'DATA', Path(d)), \
                patch.object(control, 'status', return_value={'canStart': True, 'active': False}), \
                patch.object(launcher.subprocess, 'Popen') as popen:
            control.command({'action': 'restore'})
            popen.assert_not_called()
            control.command({'action': 'start'})
            self.assertTrue(launcher.resume_enabled())
            control.command({'action': 'restore'})
            self.assertEqual(popen.call_count, 2)

    def test_window_exit_preserves_start_and_explicit_stop_clears_it(self):
        control = launcher.LauncherControl()
        with tempfile.TemporaryDirectory() as d, patch.object(launcher, 'DATA', Path(d)), \
                patch.object(control, 'status', return_value={'active': True, 'canReconnect': True}):
            session = 'a' * 32
            (Path(d) / 'desktop-start-status.json').write_text(json.dumps({'session_id': session}))
            control.command({'action': 'stop_for_exit'})
            self.assertTrue(launcher.resume_enabled())
            control.command({'action': 'stop'})
            self.assertFalse(launcher.resume_enabled())


class ConfigurationApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='console-apply-')
        self.root = Path(self.temp.name)
        (self.root / 'roleplay/personas').mkdir(parents=True)
        (self.root / 'config').mkdir()
        prompt = '友善的中性测试角色。'
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        filename = 'test-role-' + digest + '.txt'
        (self.root / 'roleplay/personas' / filename).write_text(prompt,encoding='utf-8')
        self.config = {'enabled':True,'mode':'roleplay_stream','provider_id':'deepseek-flash-official',
                       'platform_id':'test-qq','bot_self_id':'900','admin_private_ids':['100'], 'memory_admin_ids':['100'],
                       'allowed_group_ids':['200'],'persona_id':'test-role','persona_name':'星星','persona_filename':filename,
                       'persona_sha256':digest,'reply_pacing':asdict(ReplyPacing(enabled=True)), 'native_reply_probability':1.0,
                       'native_enabled':False,'wechat_enabled':False}
        self.path = self.root / 'config/astrbot_plugin_roleplay_gateway_config.json'
        self.path.write_text(json.dumps(self.config),encoding='utf-8')
        self.provider = SimpleNamespace(model_name='deepseek-flash')
        self.provider.get_model = lambda:self.provider.model_name
        self.provider.set_model = lambda value:setattr(self.provider,'model_name',value)
        self.service = SimpleNamespace(settings=GateSettings.from_mapping(self.config), _closing=False, blocked=False,
                                       persona=console_admin.load_persona(self.root/'roleplay/personas'/filename,persona_id='test-role',name='星星'),
                                       wait_idle=AsyncMock(), limits=ReplyLimits())
        self.status_snapshots = []
        self.plugin = SimpleNamespace(config=self.config,settings=self.service.settings, service=self.service,
                                      wechat_service=None,native_service=None,native_server=None,_console_busy=False,
                                      context=SimpleNamespace(get_provider_by_id=lambda _:self.provider),
                                      _stream_provider=AsyncMock(return_value=self.provider),
                                      _write_runtime_status=lambda _: self.status_snapshots.append(self.plugin._console_busy))
        self.admin = ConsoleAdmin(self.plugin,self.root)
        self.admin.models = AsyncMock(return_value={'models':['deepseek-flash','deepseek-test']})

    async def asyncTearDown(self):
        self.temp.cleanup()

    def settings_request(self):
        pacing={k:v for k,v in self.config['reply_pacing'].items() if k not in ('enabled','first_delay_mean','first_delay_std','interval_mean','interval_std')}
        return {'op':'settings','configVersion':config_version(self.config),'model':'deepseek-test','pacing':pacing,
                'nativeReplyProbability':1.0,'contextLimit':8}

    async def test_model_and_context_limit_apply_to_disk_and_active_objects(self):
        result=await self.admin.execute(self.settings_request())
        self.assertTrue(result['changed'])
        self.assertEqual(self.provider.get_model(),'deepseek-test')
        self.assertEqual(self.service.context_budget.recent_turns,8)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['console_model'],'deepseek-test')
        self.assertFalse(self.plugin._console_busy)
        self.assertEqual(self.status_snapshots[-1], False)
        self.service.wait_idle.assert_awaited_once()

    async def test_persona_save_and_switch_change_live_prompt_identity(self):
        old_filename=self.config['persona_filename']
        await self.admin.execute({'op':'persona_save','configVersion':config_version(self.config),'name':'测试朋友','prompt':'爱好星空的中性朋友。'})
        self.assertEqual(self.service.persona.name,'测试朋友')
        self.assertNotEqual(self.service.persona.persona_id,'test-role')
        await self.admin.execute({'op':'persona_select','configVersion':config_version(self.config),'filename':old_filename})
        self.assertEqual(self.service.persona.persona_id,'test-role')

    async def test_custom_models_are_saved_without_catalog_and_round_trip(self):
        request = self.settings_request() | {'model':'deepseek-custom', 'searchModel':'sonar-pro', 'imageModel':'gpt-image-1'}
        await self.admin.execute(request)
        self.admin.models.assert_not_awaited()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['console_search_model'], 'sonar-pro')
        self.assertEqual(saved['console_image_model'], 'gpt-image-1')
        self.assertEqual(self.provider.get_model(), 'deepseek-custom')

    async def test_persona_read_edit_dedup_and_archive(self):
        old = self.config['persona_filename']
        await self.admin.execute({'op':'persona_save', 'configVersion':config_version(self.config), 'name':'另一个角色', 'prompt':'新的测试人设'})
        current = self.config['persona_filename']
        read = await self.admin.execute({'op':'persona_read','filename':old})
        self.assertEqual(read['id'], 'test-role')
        await self.admin.execute({'op':'persona_save','configVersion':config_version(self.config),'id':'test-role','name':'修改角色','prompt':'修改后的测试人设'})
        self.assertEqual(len(self.admin.personas()['personas']), 2)
        with self.assertRaisesRegex(ValueError, 'active_persona_delete'):
            await self.admin.execute({'op':'persona_delete','configVersion':config_version(self.config),'filename':self.config['persona_filename']})
        await self.admin.execute({'op':'persona_delete','configVersion':config_version(self.config),'filename':current})
        self.assertEqual(len(self.admin.personas()['personas']), 1)
        self.assertTrue(list((self.root / 'roleplay/personas/deleted').glob('*.txt')))

    async def test_failed_application_restores_disk_provider_and_persona(self):
        original=json.loads(self.path.read_text(encoding='utf-8'))
        write=console_admin.atomic_json
        def fail_names(path,value):
            if path.name=='console-names.json':
                raise OSError('synthetic failure')
            return write(path,value)
        with patch.object(console_admin,'atomic_json',side_effect=fail_names):
            with self.assertRaises(OSError):
                await self.admin.execute(self.settings_request())
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),original)
        self.assertEqual(self.provider.get_model(),'deepseek-flash')
        self.assertEqual(self.service.persona.persona_id,'test-role')
        self.assertFalse(self.plugin._console_busy)
        self.assertEqual(self.status_snapshots[-1], False)

    async def test_external_changes_are_not_overwritten(self):
        external={**self.config,'native_bridge_token':'synthetic external value'}
        self.path.write_text(json.dumps(external),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'revision_conflict'):
            await self.admin.execute(self.settings_request())
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),external)


if __name__ == '__main__':
    unittest.main()
