import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, DatePicker, Empty, Form, Input, Modal, Select, Space, Spin, Switch, Table, Tag, Typography } from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { writeControl } from './live-data';

type Entry = { id: string; title: string; content: string; enabled: boolean; mode: 'always' | 'keyword'; position: 'before' | 'after'; keywords: string[] };
type Scheduled = { id: string; targetId: string; content: string; mode: 'fixed' | 'persona'; repeat: 'none' | 'daily' | 'weekly'; at: string; nextRun: number | null; enabled: boolean; deleted: boolean; createdAt: number };
type Run = { id: string; taskId: string; at: number; status: string; text: string; finishedAt?: number };
type Features = { revision: number; personaId: string; entries: Entry[]; schedulerEnabled: boolean; running: boolean; deliveryReady: boolean; timezone: string; targets: { id: string; label: string }[]; tasks: Scheduled[]; runs: Run[] };
const { Text, Paragraph } = Typography;
const repeatLabels = { none: '仅一次', daily: '每天', weekly: '每周' };
const runLabels: Record<string, string> = { preparing: '生成中', dispatching: '发送中', api_accepted: '微信接口已接受', failed: '执行失败', unknown: '结果待确认', missed: '已错过', cancelled: '已取消' };
const stamp = (at: number) => dayjs(at * 1000).format('YYYY-MM-DD HH:mm:ss');

function useFeatures(poll = false) {
  const [data, setData] = useState<Features | null>(null), [error, setError] = useState('');
  const refresh = async () => {
    try { const value = await writeControl<Features>({ op: 'features' }); setData(value); setError(''); }
    catch (e) { setError((e as Error).message); }
  };
  useEffect(() => {
    let active = true, timer: number;
    const load = async () => {
      try { const value = await writeControl<Features>({ op: 'features' }); if (active) { setData(value); setError(''); } }
      catch (e) { if (active) setError((e as Error).message); }
      if (active && poll) timer = window.setTimeout(load, 10000);
    };
    load(); return () => { active = false; window.clearTimeout(timer); };
  }, [poll]);
  return { data, setData, error, refresh };
}

export function RealWorldbook() {
  const { message, modal } = App.useApp();
  const { data, setData, error, refresh } = useFeatures();
  const [entries, setEntries] = useState<Entry[]>([]), [expanded, setExpanded] = useState<string[]>([]), [busy, setBusy] = useState(false);
  useEffect(() => { if (data) setEntries(structuredClone(data.entries)); }, [data]);
  const change = (id: string, patch: Partial<Entry>) => setEntries(items => items.map(e => e.id === id ? { ...e, ...patch } : e));
  const move = (index: number, step: number) => setEntries(items => { const next = [...items]; [next[index], next[index + step]] = [next[index + step], next[index]]; return next; });
  const save = async () => {
    if (!data) return;
    setBusy(true);
    try { setData(await writeControl<Features>({ op: 'features', action: 'worldbook', revision: data.revision, personaId: data.personaId, entries: entries.map(e => ({ ...e, keywords: e.keywords.map(k => k.trim()).filter(Boolean) })) })); message.success('世界书已应用到当前人设'); }
    catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };
  return <Card title="世界书" extra={<Button type="text" aria-label="刷新世界书" icon={<ReloadOutlined />} disabled={busy} onClick={refresh} />}>
    <Paragraph type="secondary">可配置多条世界书，支持调整顺序（顺序越靠前，权重越高）。</Paragraph>
    {error && <Alert type="error" showIcon message={error} />}
    {!data ? <Spin /> : <><div className="world-list">{entries.map((entry, i) => <div className={`world-item ${entry.enabled ? '' : 'world-disabled'}`} key={entry.id}>
      <div className="world-heading"><Text strong>{entry.title || `条目 ${i + 1}`}</Text><Switch size="small" checked={entry.enabled} disabled={busy} aria-label={`启用世界书条目 ${i + 1}`} onChange={enabled => change(entry.id, { enabled })} />
        <Button type="text" size="small" aria-label={`上移世界书条目 ${i + 1}`} icon={<ArrowUpOutlined />} disabled={busy || i === 0} onClick={() => move(i, -1)} />
        <Button type="text" size="small" aria-label={`下移世界书条目 ${i + 1}`} icon={<ArrowDownOutlined />} disabled={busy || i === entries.length - 1} onClick={() => move(i, 1)} />
        <Button type="link" size="small" onClick={() => setExpanded(prior => prior.includes(entry.id) ? prior.filter(id => id !== entry.id) : [...prior, entry.id])}>{expanded.includes(entry.id) ? '收起' : '编辑'}</Button>
        <Button type="link" size="small" danger disabled={busy} onClick={() => modal.confirm({ title: '删除世界书条目？', content: '保存后生效。', okText: '删除', cancelText: '取消', onOk: () => setEntries(prior => prior.filter(e => e.id !== entry.id)) })}>删除</Button></div>
      {expanded.includes(entry.id) && <div className="world-editor"><label className="field-label" htmlFor={`world-title-${entry.id}`}>世界书条目（可选）</label><Input id={`world-title-${entry.id}`} value={entry.title} disabled={busy} maxLength={100} onChange={e => change(entry.id, { title: e.target.value })} />
        <label className="field-label required" htmlFor={`world-content-${entry.id}`}>世界书内容</label><Input.TextArea id={`world-content-${entry.id}`} value={entry.content} disabled={busy} rows={5} maxLength={6000} onChange={e => change(entry.id, { content: e.target.value })} />
        <div className="two-fields spaced"><div><label className="field-label">触发方式</label><Select aria-label={`条目 ${i + 1} 触发方式`} value={entry.mode} disabled={busy} style={{ width: '100%' }} options={[{ value: 'always', label: '始终生效' }, { value: 'keyword', label: '关键词触发' }]} onChange={mode => change(entry.id, { mode })} /></div>
          <div><label className="field-label">插入位置</label><Select aria-label={`条目 ${i + 1} 插入位置`} value={entry.position} disabled={busy} style={{ width: '100%' }} options={[{ value: 'before', label: '人设之前' }, { value: 'after', label: '人设之后' }]} onChange={position => change(entry.id, { position })} /></div></div>
        {entry.mode === 'keyword' && <><label className="field-label" htmlFor={`world-keys-${entry.id}`}>触发词（任一命中，以逗号分隔）</label><Input id={`world-keys-${entry.id}`} disabled={busy} value={entry.keywords.join(',')} onChange={e => change(entry.id, { keywords: e.target.value.split(/[,，]/) })} /></>}
      </div>}</div>)}</div>
      {!entries.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前人设暂无世界书" />}
      <Button block type="dashed" icon={<PlusOutlined />} disabled={busy || entries.length >= 20} onClick={() => { const id = crypto.randomUUID(); setEntries(prior => [...prior, { id, title: '', content: '', enabled: true, mode: 'always', position: 'after', keywords: [] }]); setExpanded(prior => [...prior, id]); }}>新增世界书</Button>
      <Paragraph type="secondary" className="compact-hint">最多 20 条，当前 {entries.length} 条。</Paragraph><Space className="form-actions"><Button type="primary" loading={busy} onClick={save}>保存</Button><Button danger disabled title="批量删除待接入">删除</Button><Button disabled={busy} onClick={() => setEntries(structuredClone(data.entries))}>重置</Button></Space></>}
  </Card>;
}

export function RealSchedules() {
  const { message, modal } = App.useApp();
  const { data, setData, error, refresh } = useFeatures(true);
  const [editing, setEditing] = useState<Scheduled | 'new' | null>(null), [busy, setBusy] = useState(false), [trash, setTrash] = useState(false), [preview, setPreview] = useState('');
  const [editingRevision, setEditingRevision] = useState(0);
  const [form] = Form.useForm();
  const mode = Form.useWatch('mode', form);
  const write = async (payload: object, revision = data?.revision) => {
    if (!data) return;
    setBusy(true);
    try { setData(await writeControl<Features>({ op: 'features', revision, ...payload })); }
    catch (e) { message.error((e as Error).message); throw e; }
    finally { setBusy(false); }
  };
  const open = (task: Scheduled | 'new') => {
    setPreview(''); setEditing(task); setEditingRevision(data?.revision ?? 0);
    form.setFieldsValue(task === 'new' ? { mode: 'fixed', content: '', targetId: data?.targets[0]?.id, at: dayjs().add(10, 'minute').second(0), repeat: 'none', enabled: true } : { ...task, at: dayjs(task.at) });
  };
  const values = async () => { const value = await form.validateFields(); return { ...value, id: editing !== 'new' && editing ? editing.id : undefined, at: value.at.format('YYYY-MM-DDTHH:mm:ss+08:00') }; };
  const submit = async () => { try { const task = await values(); await write({ action: 'save_task', task }, editingRevision); message.success('定时任务已保存'); setEditing(null); } catch { /* Field/API feedback is already visible. */ } };
  const previewMessage = async () => {
    let task; try { task = await values(); } catch { return; }
    setBusy(true); setPreview('');
    try { const result = await writeControl<{ text: string }>({ op: 'features', action: 'preview', task }); setPreview(result.text); }
    catch (e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };
  if (!data) return error ? <Alert type="error" showIcon message={error} action={<Button onClick={refresh}>重试</Button>} /> : <Card><Spin /></Card>;
  return <div className="page-stack">{error && <Alert type="error" showIcon message={error} />}
    <Card title="定时任务管理" extra={<Space wrap><Tag color={data.deliveryReady ? 'green' : 'orange'}>{data.deliveryReady ? '微信已连接' : '微信未连接'}</Tag><Switch checkedChildren="启用" unCheckedChildren="暂停" aria-label="启用定时服务" checked={data.schedulerEnabled} disabled={busy} onChange={enabled => { void write({ action: 'scheduler', enabled }).catch(() => {}); }} /><Button icon={<PlusOutlined />} type="primary" disabled={busy || !data.targets.length} onClick={() => open('new')}>新建任务</Button></Space>}>
      {!data.schedulerEnabled && <Alert type="info" showIcon message="定时服务已暂停，任务可继续编辑。" className="storage-alert" />}
      <div className="section-toolbar"><Text type="secondary">北京时间 · 运行中执行，错过超过两分钟不补发</Text><Button type="link" onClick={() => setTrash(!trash)}>{trash ? '返回任务' : `已删除（${data.tasks.filter(t => t.deleted).length}）`}</Button></div>
      <Table<Scheduled> rowKey="id" dataSource={data.tasks.filter(t => t.deleted === trash)} scroll={{ x: 850 }} pagination={{ pageSize: 8, hideOnSinglePage: true }} columns={[
        { title: '接收会话', width: 190, render: (_, t) => data.targets.find(v => v.id === t.targetId)?.label || '会话已不可用' },
        { title: '内容', render: (_, t) => <><Tag>{t.mode === 'fixed' ? '固定文字' : '人设生成'}</Tag><Paragraph ellipsis={{ rows: 2, expandable: true }}>{t.content}</Paragraph></> },
        { title: '下次执行', width: 170, render: (_, t) => t.nextRun ? stamp(t.nextRun) : '已结束' },
        { title: '重复', width: 80, render: (_, t) => repeatLabels[t.repeat] },
        { title: '状态', width: 90, render: (_, t) => <Switch aria-label={`启用定时任务 ${t.id}`} checked={t.enabled} disabled={busy || trash} onChange={enabled => { void write({ action: 'save_task', task: { ...t, enabled } }).catch(() => {}); }} /> },
        { title: '操作', width: 130, render: (_, t) => trash ? <Button type="link" disabled={busy} onClick={() => { void write({ action: 'restore_task', id: t.id }).catch(() => {}); }}>恢复</Button> : <Space size={0}><Button type="link" disabled={busy} onClick={() => open(t)}>编辑</Button><Button type="link" danger disabled={busy} onClick={() => modal.confirm({ title: '删除定时任务？', content: '删除后可从“已删除”中恢复。', okText: '删除', cancelText: '取消', onOk: () => write({ action: 'delete_task', id: t.id }) })}>删除</Button></Space> },
      ]} />
    </Card>
    <Card title="执行记录" extra={<Button type="text" icon={<ReloadOutlined />} aria-label="刷新执行记录" onClick={refresh} />}><Table<Run> rowKey="id" size="small" dataSource={data.runs} pagination={{ pageSize: 6, hideOnSinglePage: true }} columns={[
      { title: '计划时间', width: 175, render: (_, r) => stamp(r.at) },
      { title: '状态', width: 150, render: (_, r) => <Tag color={r.status === 'api_accepted' ? 'green' : ['unknown', 'failed'].includes(r.status) ? 'orange' : undefined}>{runLabels[r.status] || r.status}</Tag> },
      { title: '消息', render: (_, r) => <Paragraph ellipsis={{ rows: 2, expandable: true }}>{r.text || '—'}</Paragraph> },
    ]} /></Card>
    <Modal title={editing === 'new' ? '新建定时任务' : '编辑定时任务'} open={editing !== null} width={640} onCancel={() => { if (!busy) setEditing(null); }} onOk={submit} confirmLoading={busy} okText="保存任务" cancelText="取消" closable={!busy} maskClosable={!busy}>
      <Form form={form} layout="vertical" disabled={busy}>
        <Form.Item name="targetId" label="接收会话" rules={[{ required: true }]}><Select options={data.targets.map(t => ({ value: t.id, label: t.label }))} /></Form.Item>
        <Form.Item name="mode" label="消息方式" rules={[{ required: true }]}><Select options={[{ value: 'fixed', label: '固定文字' }, { value: 'persona', label: '按人设生成' }]} onChange={() => setPreview('')} /></Form.Item>
        <Form.Item name="content" label={mode === 'persona' ? '生成要求' : '消息内容'} rules={[{ required: true, whitespace: true }, { max: mode === 'persona' ? 800 : 125 }]}><Input.TextArea rows={4} maxLength={mode === 'persona' ? 800 : 125} showCount /></Form.Item>
        {mode === 'persona' && <Paragraph type="secondary">执行时使用当前人设、世界书和此会话上下文。</Paragraph>}
        <div className="two-fields"><Form.Item name="at" label="执行时间（北京时间）" rules={[{ required: true }]}><DatePicker showTime format="YYYY-MM-DD HH:mm:ss" style={{ width: '100%' }} /></Form.Item><Form.Item name="repeat" label="重复" rules={[{ required: true }]}><Select options={Object.entries(repeatLabels).map(([value, label]) => ({ value, label }))} /></Form.Item></div>
        <Form.Item name="enabled" label="任务状态" valuePropName="checked"><Switch checkedChildren="启用" unCheckedChildren="暂停" /></Form.Item>
      </Form><Button loading={busy} disabled={busy} onClick={previewMessage}>预览消息</Button>{preview && <Card size="small" className="spaced"><Paragraph>{preview}</Paragraph><Text type="secondary">预览未发送</Text></Card>}
    </Modal>
  </div>;
}
