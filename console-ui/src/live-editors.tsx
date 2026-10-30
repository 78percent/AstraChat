import { useEffect, useState } from 'react';
import { App, AutoComplete, Button, Card, Form, Input, InputNumber, Modal, Select, Space, Tag, Typography } from 'antd';
import { EyeOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { writeControl, type LiveBootstrap, type ModelProfile, type ModelProfiles } from './live-data';
import { ModelConnection } from './model-connection';
import { Pending, ReferenceAdvanced, ReferenceDanger, ReferenceImport, ReferenceSwitches, ReferenceWait } from './reference-panels';
const { Text, Paragraph } = Typography;

export function RealSettings({ data, saved }: { data: LiveBootstrap; saved: () => void }) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [profiles, setProfiles] = useState<ModelProfiles>(data.config.modelProfiles || {
    active: 'deepseek', models: { deepseek: data.config.model || 'deepseek-flash', kouri: 'gemini-4-argon' },
    kouri: { baseUrl: 'https://api.kouri.chat/v1', keyConfigured: false, maskedKey: '', keyRevision: 'initial' },
  });
  const [profile, setProfile] = useState<ModelProfile>(profiles.active);
  const [draftModels, setDraftModels] = useState(profiles.models);
  const watchedModel = Form.useWatch('model', form);
  const [busy, setBusy] = useState(false), [models, setModels] = useState<string[]>([data.config.model || 'deepseek-flash']);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const loadModels = async () => {
    setCatalogBusy(true);
    try { const result = await writeControl<{ models: string[]; catalogUnavailable?: boolean }>({ op: 'models', profile, model: form.getFieldValue('model') }); setModels(result.models); if (result.catalogUnavailable) message.warning('暂时无法读取模型目录，可以手填模型 ID。'); }
    catch (e) { message.error((e as Error).message); }
    finally { setCatalogBusy(false); }
  };
  const switchProfile = (next: ModelProfile) => {
    setDraftModels(previous => ({ ...previous, [profile]: form.getFieldValue('model') }));
    setProfile(next); setModels([]); form.setFieldValue('model', draftModels[next]);
  };
  const reset = () => { form.resetFields(); setProfile(profiles.active); setDraftModels(profiles.models); setModels([]); };
  const save = async () => {
    let values;
    try { values = await form.validateFields(); } catch { return; }
    setBusy(true);
    try {
      await writeControl({ op: 'settings', configVersion: data.configVersion, ...values });
      message.success('设置已保存并应用到机器人'); saved();
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };
  const field = (name: string, label: string, min: number, max: number, step = 1) => <Form.Item key={name} name={['pacing', name]} label={label} rules={[{ required: true }]}><InputNumber min={min} max={max} step={step} style={{ width: '100%' }} /></Form.Item>;
  return <Form form={form} layout="vertical" initialValues={{ profile: profiles.active, model: profiles.models[profiles.active], searchModel: data.config.searchModel || '', imageModel: data.config.imageModel || '', nativeReplyProbability: data.config.nativeReplyProbability, contextLimit: data.config.contextBudget.recent_turns, pacing: Object.fromEntries(Object.entries(data.config.pacing).filter(([key]) => ['probability','first_delay_min','first_delay_max','interval_min','interval_max','max_bubbles','max_characters','cooldown_seconds','window_seconds','max_rounds'].includes(key))) }} disabled={busy}>
    <div className="two-columns settings-columns reference-settings"><div className="page-stack"><Card title="个性化模型" extra={<Button type="link" loading={catalogBusy} onClick={loadModels}>读取可用模型</Button>}>
      <Form.Item name="profile" label="API 方案"><Select onChange={switchProfile} options={[{value:'deepseek',label:'我的 DeepSeek API'},{value:'kouri',label:'Kouri API'}]} /></Form.Item><div className="two-fields model-fields">
      <Form.Item name="model" label="会话模型 ID" extra={`当前生效：${profiles.active === 'kouri' ? 'Kouri' : 'DeepSeek'} · ${data.config.model || '默认'}`} rules={[{ required: true }]}><AutoComplete options={[...new Set([...models, profiles.models[profile]])].map(value => ({ value }))} placeholder={profile === 'kouri' ? 'gemini-4-argon' : '输入 DeepSeek 模型名称'} filterOption={(input, option) => String(option?.value).toLowerCase().includes(input.toLowerCase())} /></Form.Item>
      <Form.Item label={<>提醒模型 ID <Pending /></>} htmlFor="pending-reminder-model"><Input id="pending-reminder-model" disabled placeholder="待接入" /></Form.Item>
      <Form.Item label={<>回退会话模型 ID <Pending /></>} htmlFor="pending-fallback-model"><Input id="pending-fallback-model" disabled placeholder="待接入" /></Form.Item><div className="empty-cell" />
      <Form.Item label={<>联网搜索模型 <Pending /></>} htmlFor="pending-search-model"><Input id="pending-search-model" disabled value="kourichat-search" /></Form.Item>
      <Form.Item label={<>网页提取模型 <Pending /></>} htmlFor="pending-web-model"><Input id="pending-web-model" disabled value="kourichat-weblens" /></Form.Item>
    </div><Space wrap><Button type="primary" loading={busy} onClick={save}>保存并应用方案</Button><Button onClick={reset}>重置</Button><Button onClick={() => setAdvanced(true)}>高级设置</Button></Space>
    </Card><ReferenceSwitches /><ReferenceWait /></div><div className="page-stack"><ModelConnection profiles={profiles} profile={profile} model={() => watchedModel || form.getFieldValue('model') || ''} changed={setProfiles} /><ReferenceDanger /></div></div><div className="two-columns local-settings"><Card title="本机模型与回复"><div className="two-fields">
      <Form.Item name="searchModel" label="搜索模型" extra="待接入 · 当前联网搜索仍使用现有服务"><AutoComplete allowClear options={[{value:'deepseek-chat'},{value:'deepseek-reasoner'},{value:'sonar'},{value:'sonar-pro'}]} placeholder="输入模型名称或选择预设" /></Form.Item>
      <Form.Item name="imageModel" label="生图模型" extra="待接入 · 保存后暂不调用"><AutoComplete allowClear options={[{value:'gpt-image-1'},{value:'dall-e-3'},{value:'black-forest-labs/FLUX.1-schnell'}]} placeholder="输入模型名称或选择预设" /></Form.Item>
      <Form.Item name="contextLimit" label="模型最近对话轮数" rules={[{ required: true }]}><InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} /></Form.Item>
      <Form.Item name="nativeReplyProbability" label="桌面微信回复概率" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} /></Form.Item>
      {field('probability','QQ / ClawBot 回复概率',0,1,.05)}{field('max_bubbles','最多气泡数',1,data.config.channels.native ? 4 : 16)}{field('max_characters','每次回复字数上限',1,data.config.channels.native ? 125 : 2000)}
    </div></Card><Card title="本机回复等待与频率"><div className="two-fields">
      {field('first_delay_min','首条最短等待（秒）',0,60,.5)}{field('first_delay_max','首条最长等待（秒）',.5,60,.5)}
      {field('interval_min','气泡最短间隔（秒）',0,60,.5)}{field('interval_max','气泡最长间隔（秒）',.5,60,.5)}
      {field('cooldown_seconds','会话冷却（秒）',0,3600)}{field('window_seconds','频率窗口（秒）',1,3600)}{field('max_rounds','窗口内最多回复',1,100)}
    </div><Space wrap className="form-actions"><Button type="primary" loading={busy} onClick={save}>保存并应用</Button><Button onClick={reset}>重置表单</Button></Space></Card></div><ReferenceAdvanced open={advanced} close={() => setAdvanced(false)} />
  </Form>;
}

type CardInfo = { id: string; name: string; filename: string; revision: string; active: boolean };
export function RealPersonas({ data, saved }: { data: LiveBootstrap; saved: () => void }) {
  const { message, modal } = App.useApp();
  const [cards, setCards] = useState<CardInfo[]>([]), [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<CardInfo | 'new' | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [form] = Form.useForm();
  useEffect(() => {
    let active = true;
    writeControl<{ personas: CardInfo[] }>({ op: 'personas' }).then(r => { if (active) setCards(r.personas); }).catch(e => { if (active) message.error(e.message); });
    return () => { active = false; };
  }, [data.configVersion]);
  const open = async (card: CardInfo | 'new') => {
    if (card === 'new') { form.setFieldsValue({ name: '', prompt: '' }); setEditing(card); return; }
    setBusy(true);
    try { const result = await writeControl<CardInfo & { prompt: string }>({ op: 'persona_read', filename: card.filename }); form.setFieldsValue({ name: result.name, prompt: result.prompt }); setEditing(card); }
    catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };
  const remove = (card: CardInfo) => {
    const alternatives = cards.filter(c => c.id !== card.id);
    if (card.active && !alternatives.length) { modal.info({ title: '请先添加另一个角色', content: '机器人需要保留一个可用人设。', okText: '知道了' }); return; }
    let replacement = alternatives[0]?.filename;
    modal.confirm({ title: `删除“${card.name}”？`, content: <><Paragraph>角色卡将移入本地归档，已有会话记录保留。</Paragraph>{card.active && <><label className="field-label">切换到</label><Select aria-label="删除后使用的角色" defaultValue={replacement} style={{width:'100%'}} options={alternatives.map(c => ({value:c.filename,label:c.name}))} onChange={value => { replacement = value; }} /></>}</>, okText: '删除', cancelText: '取消', okButtonProps: {danger:true}, onOk: async () => {
      try {
        let configVersion = data.configVersion;
        if (card.active) { const result = await writeControl<{configVersion:string}>({op:'persona_select', configVersion, filename:replacement}); configVersion = result.configVersion; }
        const result = await writeControl<{personas:CardInfo[]}>({op:'persona_delete', configVersion, filename:card.filename}); setCards(result.personas); message.success('角色已删除');
      } catch (e) { message.error((e as Error).message); throw e; }
      finally { saved(); }
    } });
  };
  const save = async () => {
    let values;
    try { values = await form.validateFields(); } catch { return; }
    setBusy(true);
    try {
      await writeControl({ op: 'persona_save', configVersion: data.configVersion, id: editing && editing !== 'new' ? editing.id : undefined, ...values });
      message.success('人设已保存，机器人已使用新版本'); setEditing(null); saved();
    } catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };
  const select = (card: CardInfo) => modal.confirm({ title: `使用“${card.name}”？`, content: '不同角色使用各自的上下文；切回原角色可继续查看原来的记录。', okText: '使用', cancelText: '取消', onOk: async () => {
    try { await writeControl({ op: 'persona_select', configVersion: data.configVersion, filename: card.filename }); message.success('已切换人设'); saved(); }
    catch (e) { message.error((e as Error).message); throw e; }
  } });
  return <><div className="page-stack"><Card title="角色卡概览" extra={<Space wrap><Button type="text" icon={<EyeOutlined />} onClick={() => setPreviewOpen(true)}>预览</Button><Button type="text" icon={<ReloadOutlined />} onClick={saved}>重新加载</Button></Space>}><Paragraph>当前角色：<Text strong>{data.persona.name}</Text></Paragraph><Paragraph ellipsis={{ rows: 1 }}>{data.persona.prompt}</Paragraph></Card><div className="persona-columns"><Card title={`角色卡列表（${cards.length}）`} extra={<Button type="link" icon={<PlusOutlined />} disabled={busy} onClick={() => open('new')}>新建角色</Button>}><div className="persona-list">{cards.map(card => <div className="persona-item" key={card.filename}><div className="persona-info"><Text strong>{card.name}</Text><Text className="persona-source">{card.filename}</Text></div><Space wrap>{card.active ? <Tag color="green">正在使用</Tag> : <Button type="link" disabled={busy} onClick={() => select(card)}>设为当前</Button>}<Button type="link" icon={<ReloadOutlined />} disabled title="从远端更新角色卡待接入">更新</Button><Button type="link" disabled={busy} onClick={() => open(card)}>编辑</Button><Button type="link" danger disabled={busy} onClick={() => remove(card)}>删除</Button></Space></div>)}</div></Card><div className="page-stack"><ReferenceImport />
    <Card title="人设内容" extra={<Button type="link" disabled={busy} onClick={() => { const card = cards.find(c => c.active); if (card) void open(card); }}>编辑</Button>}><Input.TextArea aria-label="真实人设内容" readOnly value={data.persona.prompt} autoSize={{ minRows: 16, maxRows: 28 }} className="readonly-memory" />{data.persona.examples.length > 0 && <div className="spaced"><Text strong>示例对话</Text>{data.persona.examples.map((item, index) => <Paragraph key={index}>{item.role}：{item.content}</Paragraph>)}</div>}</Card></div></div></div>
    <Modal title={data.persona.name} open={previewOpen} onCancel={() => setPreviewOpen(false)} footer={<Button onClick={() => setPreviewOpen(false)}>关闭</Button>} width={720}><Paragraph className="preserve-lines">{data.persona.prompt}</Paragraph></Modal>
    <Modal title={editing === 'new' ? '新建角色' : '编辑人设'} open={editing !== null} width={720} confirmLoading={busy} okText="保存并使用" cancelText="取消" onOk={save} onCancel={() => { if (!busy) setEditing(null); }} closable={!busy} maskClosable={!busy}>
      <Form form={form} layout="vertical" disabled={busy}><Form.Item name="name" label="角色名称" rules={[{ required: true, whitespace: true, max: 100 }]}><Input maxLength={100} /></Form.Item><Form.Item name="prompt" label="人设内容" rules={[{ required: true, whitespace: true }]}><Input.TextArea rows={14} maxLength={32000} /></Form.Item></Form>
    </Modal></>;
}
