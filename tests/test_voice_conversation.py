"""Speech boundaries with synthetic identities; never touch live WeChat."""
import asyncio
import ctypes as c
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from scripts.wechat_native_bridge_core import BridgeCore, BridgeJournal, BridgeSettings, VOICE_COMMIT, native_event
from scripts.wechat_voice_bridge import VoiceBridge, voice_snapshot, NOTICE
from scripts.wechat_voice_runtime import SpeechProcess, SpeechError
from scripts.wechat_speech_worker import owned_path
from plugins.astrbot_plugin_roleplay_gateway.voice_intent import VoiceIntent, explicit_mode
from plugins.astrbot_plugin_roleplay_gateway.gate import Envelope, ApiAccepted
from plugins.astrbot_plugin_roleplay_gateway.segment_delivery import SegmentSender, SegmentLedger, AttemptToken, InputRef

SESSION = "a" * 32


def settings():
    return BridgeSettings(True, "wxid_bot", "123@chatroom", "b" * 64,
                          upstream_commit=VOICE_COMMIT, private_peer="wxid_peer")


def raw(**changes):
    value = dict(type=1, receiver=b"wxid_bot", sender=b"123@chatroom", native_message_id=1,
                 sequence=1, text="wxid_member:\n三叶你好".encode(), mention_source=b"")
    return SimpleNamespace(**{**value, **changes})


class V6(c.Structure):
    _fields_ = [("size",c.c_uint32),("present",c.c_uint32),("type",c.c_uint32),
                ("thread_id",c.c_uint32),("dropped",c.c_uint32),("reserved",c.c_uint32),
                ("sequence",c.c_uint64),("sender",c.c_char*128),("receiver",c.c_char*128),
                ("text",c.c_char*1024),("mention_source",c.c_char*4096),("image_xml",c.c_char*262144),
                ("native_message_id",c.c_uint64),("voice_offset",c.c_uint32),("voice_size",c.c_uint32)]


def voice_raw(private=False):
    message=V6(); message.size=c.sizeof(V6); message.type=34; message.sequence=2
    message.sender=b"wxid_peer" if private else b"123@chatroom"
    message.receiver=b"wxid_bot"; message.native_message_id=2**64-1
    xml=(b"" if private else b"wxid_member:\n") + b'<msg><voicemsg voicelength="20" endflag="1"/></msg>'
    message.image_xml=xml; message.voice_offset=len(xml)+1
    payload=b'\x02#!SILK_V3\x04\0ab\0c'; message.voice_size=len(payload)
    c.memmove(c.addressof(message)+V6.image_xml.offset+message.voice_offset,payload,len(payload))
    return message,payload


class FakeRuntime:
    def __init__(self):
        self.jobs={}; self.settings={}; self.calls=[]
    def synthesize(self,text):
        job=uuid.uuid4().hex; self.jobs[job]=None;self.calls.append(text);return job
    def recognize(self,payload):
        return self.synthesize("ASR")
    def result(self,job):
        result=self.jobs[job]
        if isinstance(result,Exception):raise result
        return result
    def encoded(self,job):
        raise SpeechError("test_encode_failure")
    def cancel(self,job):
        self.jobs.pop(job,None)
    def reap(self):pass


class HostVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.journal=BridgeJournal(Path(self.tmp.name)/'journal.sqlite');self.addCleanup(self.journal.close)
        self.core=BridgeCore(settings(),self.journal,SESSION)
        event=self.core.observe(raw(),100); self.core.mark_forwarded(event)
        self.runtime=FakeRuntime();self.voice=VoiceBridge(self.core,self.runtime)
    def prepare(self,**changes):
        frame=dict(type="prepare_voice",session=SESSION,request_id=uuid.uuid4().hex,
                   reply_to="1",room="123@chatroom",text="测试回复。")
        frame.update(changes);self.voice.prepare(frame,110);return frame
    def commit(self,frame,**changes):
        value={**frame,"type":"commit_voice","request_id":uuid.uuid4().hex,
               "handle":frame['request_id'],"expires_at":168}
        value.update(changes);return value
    def test_v6_binary_copy_preserves_nuls_private_and_group(self):
        for private in (False,True):
            message,payload=voice_raw(private)
            def validate(body,xml):
                self.assertEqual(body,payload);return body,20
            event,body,duration=voice_snapshot(message,settings(),SESSION,100,validate)
            self.assertEqual(body,payload);self.assertEqual(event['input_modality'],'voice')
            self.assertEqual(event['id'],str(2**64-1));self.assertEqual(event['received_at'],100)
            self.assertEqual(event['kind'],'private' if private else 'group')
            message.voice_offset+=1
            self.assertIsNone(voice_snapshot(message,settings(),SESSION,100,validate))
    def test_unrelated_payload_is_never_accessed(self):
        class Other:
            type=34;receiver=b"wxid_bot";sender=b"wxid_other"
            @property
            def image_xml(self):raise AssertionError("unrelated audio read")
        self.assertIsNone(voice_snapshot(Other(),settings(),SESSION,100))
    def test_private_binding_and_journal_target(self):
        event=self.core.observe(raw(sender=b'wxid_peer',text='你好'.encode(),native_message_id=5),100)
        self.assertEqual(event['kind'],'private');self.core.mark_forwarded(event)
        frame=dict(type='send',session=SESSION,request_id=uuid.uuid4().hex,reply_to='5',
                   room='wxid_peer',text='你好',expires_at=108)
        send,response=self.core.prepare(frame,101)
        self.assertIsNotNone(send);self.assertEqual(self.journal.existing(frame['request_id'])['room'],'wxid_peer')
        self.assertIsNone(native_event(raw(sender=b'wxid_other'),settings(),SESSION,100))
    def test_preparation_is_not_native_intent_and_failure_falls_back(self):
        frame=self.prepare()
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbound').fetchone()[0],0)
        job=next(iter(self.runtime.jobs));self.runtime.jobs[job]=SpeechError('failed')
        _,results=self.voice.poll(160);self.assertEqual(results[0]['status'],'fallback')
        send,response,audio=self.voice.commit(self.commit(frame),160)
        self.assertIsNone(audio);self.assertIsNone(response);self.assertEqual(send.text,NOTICE+frame['text'])
        outcome={'pid':1,'code':0,'cleanup_code':0,'success':True,
                 'report':{'completion_success':True,'target':send.room,'text':send.text}}
        self.assertEqual(self.voice.completed(send,outcome,1,audio)['status'],'native_completed')
        self.assertEqual(self.journal.existing(send.request_id)['characters'],len(frame['text']))
    def test_wrong_target_handle_unknown_and_expiry_do_not_resend(self):
        frame=self.prepare();job=next(iter(self.runtime.jobs));self.runtime.jobs[job]=SpeechError('fail')
        self.voice.poll(160)
        send,response,audio=self.voice.commit(self.commit(frame,text='different'),160)
        self.assertIsNone(send);self.assertEqual(response['status'],'rejected')
        commit=self.commit(frame)
        send,_,audio=self.voice.commit(commit,160)
        self.assertEqual(self.voice.completed(send,None,1,audio)['status'],'unknown')
        send,response,_=self.voice.commit(commit,161)
        self.assertIsNone(send);self.assertEqual(response['status'],'unknown')
        with self.assertRaisesRegex(Exception,'invalid_voice_binding'):
            self.voice.commit(self.commit(frame,expires_at=401),400)

    def test_completed_audio_hash_is_rechecked_and_voice_receipt_committed_once(self):
        from scripts.wechat_voice_runtime import sha256
        target=Path(self.tmp.name)/'audio.silk';target.write_bytes(b'test audio')
        reference=Path(self.tmp.name)/'ref.wav';reference.write_bytes(b'test reference')
        self.runtime.settings['reference_audio']=reference
        self.runtime.encoded=lambda job:dict(path=target,duration_ms=1000,sha256=sha256(target),reference_sha256=sha256(reference))
        frame=self.prepare();job=next(iter(self.runtime.jobs));self.runtime.jobs[job]={'result':{}}
        _,results=self.voice.poll(150);self.assertEqual(results[0]['status'],'ready')
        send,_,audio=self.voice.commit(self.commit(frame),160)
        self.assertEqual(send.text,frame['text']);self.assertEqual(audio['duration_ms'],1000)
        self.assertEqual(self.voice.completed(send,dict(state='NATIVE_COMPLETED',duration_ms=1000),1,audio)['status'],'native_completed')
        self.voice.release({'handle':frame['request_id']});self.assertFalse(self.runtime.jobs)

    def test_reference_change_before_commit_uses_definite_text_fallback(self):
        from scripts.wechat_voice_runtime import sha256
        target=Path(self.tmp.name)/'audio.silk';target.write_bytes(b'audio')
        reference=Path(self.tmp.name)/'ref.wav';reference.write_bytes(b'old')
        self.runtime.settings['reference_audio']=reference
        self.runtime.encoded=lambda job:dict(path=target,duration_ms=1000,sha256=sha256(target),reference_sha256=sha256(reference))
        frame=self.prepare();job=next(iter(self.runtime.jobs));self.runtime.jobs[job]={'result':{}}
        self.voice.poll(150);reference.write_bytes(b'new')
        send,_,audio=self.voice.commit(self.commit(frame),160)
        self.assertIsNone(audio);self.assertEqual(send.text,NOTICE+frame['text'])
    def test_asr_silence_duplicate_and_original_time(self):
        message,payload=voice_raw()
        snap=lambda *args, **kwargs:(dict(type='event',session=SESSION,id='2',room='123@chatroom',sender='wxid_member',
             text='',mentions=[],received_at=100,kind='group',input_modality='voice'),payload,20)
        with patch('scripts.wechat_voice_bridge.voice_snapshot',side_effect=snap):
            self.voice.observe(message,100);self.voice.observe(message,101)
        self.assertEqual(len(self.runtime.calls),1)
        job=next(iter(self.runtime.jobs));self.runtime.jobs[job]={'result':{'text':''}}
        events,_=self.voice.poll(102);self.assertFalse(events);self.assertEqual(self.voice.counts['silence'],1)

    def test_rejection_diagnostics_have_only_shape_and_static_reason(self):
        raw, _ = voice_raw()
        details = {}
        raw.voice_size = 0
        self.assertIsNone(voice_snapshot(raw, settings(), SESSION, 100, diagnostics=details))
        self.assertEqual(details['stage'], 'payload_bounds')
        self.assertEqual(details['payload_bytes'], 0)
        self.assertNotIn('wxid', json.dumps(details))
        raw.receiver = b'wxid_other'
        self.assertIsNone(voice_snapshot(raw, settings(), SESSION, 100, diagnostics=details))
        self.assertEqual(details, {'stage': 'identity'})


class IntentTests(unittest.IsolatedAsyncioTestCase):
    def event(self,text='你好',modality='text'):
        return Envelope('wechat-native-sakura','wxid_bot','wxid_peer','1','private',text=text,input_modality=modality)
    async def test_explicit_overrides_without_provider_call(self):
        selector=VoiceIntent(lambda: self.fail('unneeded model call'))
        self.assertEqual(await selector.choose(self.event('请用语音回答我')),('voice','explicit'))
        self.assertEqual(await selector.choose(self.event('不要发语音，用文字回复')),('text','explicit'))
        await selector.close()
    async def test_voice_test_and_sample_timbre_requests_need_no_classifier(self):
        selector = VoiceIntent(lambda: self.fail('explicit speech request reached classifier'))
        for text in (
            '@78percent\u2005测试一下语音功能，用indextts的示例声线说“早上好，今天吃什么”',
            '测试一下语音功能',
            '用示例声线说“早上好”',
            '请使用 IndexTTS2 合成“早上好”',
        ):
            with self.subTest(text=text):
                self.assertEqual(await selector.choose(self.event(text)), ('voice', 'explicit'))
        for text in ('不要语音，测试一下语音功能怎么配置，用文字回复',
                     '不用语音，用示例声线说早上好这条命令是什么意思'):
            self.assertEqual(await selector.choose(self.event(text)), ('text', 'explicit'))
        for text in ('IndexTTS 怎么安装？', '示例声线在哪里？', '语音功能的原理是什么？'):
            self.assertIsNone(explicit_mode(text))
        await selector.close()
    async def test_timeout_is_bounded_and_late_result_cannot_change_mode(self):
        release=asyncio.Event()
        class Adapter:
            pending_tasks=0;reusable=True
            async def consume(self,parts,delta,**kwargs):
                try:await release.wait()
                except asyncio.CancelledError:await release.wait()
                await delta('{"mode":"text"}')
                return SimpleNamespace(final_seen=True)
        selector=VoiceIntent(Adapter,timeout=.03)
        started=asyncio.get_running_loop().time()
        decision=await selector.choose(self.event(modality='voice'))
        self.assertLess(asyncio.get_running_loop().time()-started,.15)
        self.assertEqual(decision,('voice','timeout'))
        release.set();await asyncio.sleep(.01);await selector.close()
        self.assertEqual(decision,('voice','timeout'))
    async def test_model_and_invalid_output(self):
        for output,expected in [('{"mode":"voice"}',('voice','model')),('broken',('text','error'))]:
            class Adapter:
                pending_tasks=0;reusable=True
                async def consume(self,parts,delta,**kwargs):
                    await delta(output);return SimpleNamespace(final_seen=True)
            selector=VoiceIntent(Adapter,timeout=.1)
            self.assertEqual(await selector.choose(self.event()),expected);await selector.close()


class PreparationLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_preparation_stays_unsent_and_rechecks_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=SegmentLedger(Path(directory)/'segments.db');await ledger.open()
            token=AttemptToken('test','private:peer','1',0,0)
            entered,release=asyncio.Event(),asyncio.Event();current=True;calls=[]
            async def prepare(text):entered.set();await release.wait()
            async def send(text):calls.append(text);return ApiAccepted()
            sender=SegmentSender(ledger,token,send,lambda _:current,prepare_call=prepare,
                                 send_timeout=.01,id_kind='wechat_native')
            task=asyncio.create_task(sender.send('测试',attribution=(InputRef('1','peer','wechat_native'),)))
            await entered.wait();await asyncio.sleep(.03)
            self.assertEqual((await ledger.records(token))[0]['status'],'UNSENT')
            current=False;release.set();outcome=await task
            self.assertEqual(outcome.status,'FAILED');self.assertFalse(calls)
            await sender.close();await ledger.close()


class WorkerBoundaryTests(unittest.TestCase):
    def test_worker_paths_and_queue_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            for job in ('../escape','A'*32,'a'*31,'a'*32+'/file'):
                with self.assertRaises(ValueError):owned_path(root,job,'input.wav')
        worker=SpeechProcess.__new__(SpeechProcess)
        worker._lock=threading.Lock();worker._closed=False;worker.state='ready';worker._pending={};worker.capacity=1
        worker.process=SimpleNamespace(poll=lambda:None,stdin=io.StringIO())
        worker.submit('a'*32,text='hi')
        with self.assertRaisesRegex(SpeechError,'queue_full'):worker.submit('b'*32)
        self.assertEqual(len(worker._pending),1)
        with self.assertRaisesRegex(SpeechError,'too_large'):worker.submit('c'*32,text='x'*20000)


if __name__=='__main__':unittest.main()
