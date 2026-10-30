import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Empty, Input, Modal, Select, Space, Spin, Switch, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { mergeOlder, readLive, writeControl, sessionKey, type LiveBootstrap, type LiveDetail, type LiveSession } from './live-data';
import { RealSettings, RealPersonas } from './live-editors';
import { RealWorldbook, RealSchedules } from './live-features';
import { ReferenceContextLimit, ReferenceSummaryOptions } from './reference-panels';

const { Paragraph, Text } = Typography;
const date = (value: number) => dayjs(value * 1000).format('MM-DD HH:mm:ss');
const sessionLabel = (s: LiveSession) => `${s.channelLabel} · ${s.kind === 'group' ? '群聊' : '私聊'} ${s.peerLabel}`;
function SessionSummary({ detail, loading }: { detail: LiveDetail | null; loading: boolean }) {
  if (loading) return <div className="live-loading"><Spin /></div>;
  if (!detail) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="请选择会话" />;
  return detail.summary ? <Paragraph className="memory-preview">{detail.summary}</Paragraph> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此会话尚无长期记忆摘要" />;
}

export function LiveConsole({ route }: { route: string }) {
  const { message, modal } = App.useApp();
  const [bootstrap, setBootstrap] = useState<LiveBootstrap | null>(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState('');
  const [record, setRecord] = useState<{ key: string; detail: LiveDetail } | null>(null);
  const [detailError, setDetailError] = useState('');
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [backupId, setBackupId] = useState<string>();
  const [loadingOlder, setLoadingOlder] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  const lastPersona = useRef('');
  const selection = bootstrap?.sessions.find(s => s.id === selected);
  const key = selection ? `${refresh}:${sessionKey(selection)}` : '';
  const currentKey = useRef(key); currentKey.current = key;
  const detail = record?.key === key ? record.detail : null;
  const loading = Boolean(selection && !detail && !detailError);

  useEffect(() => {
    const controller = new AbortController();
    setBootstrap(null); setError(''); setRecord(null); setDetailError('');
    readLive<LiveBootstrap>('bootstrap', controller.signal).then(data => {
      if (controller.signal.aborted) return;
      if (data.schemaVersion !== 1) throw new Error('数据接口版本不匹配。');
      setBootstrap(data);
      const changedPersona = lastPersona.current !== data.persona.id;
      lastPersona.current = data.persona.id;
      setSelected(prior => !changedPersona && data.sessions.some(s => s.id === prior) ? prior : (data.sessions.find(s => s.activePersona)?.id || ''));
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    setRecord(null); setDetailError(''); setLoadingOlder(false); setRestoreOpen(false); setBackupId(undefined);
    if (!selection) return;
    const controller = new AbortController(); requestRef.current = controller;
    readLive<LiveDetail>(`session?id=${selection.id}&epoch=${selection.epoch}`, controller.signal).then(data => {
      if (!controller.signal.aborted && currentKey.current === key) setRecord({ key, detail: data });
    }).catch(e => { if (!controller.signal.aborted && currentKey.current === key) setDetailError(e.message); });
    return () => controller.abort();
  }, [key]);

  async function older() {
    const controller = requestRef.current;
    if (!detail?.nextBefore || !controller || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const next = await readLive<LiveDetail>(`session?id=${detail.session.id}&epoch=${detail.session.epoch}&before=${detail.nextBefore}`, controller.signal);
      if (!controller.signal.aborted && currentKey.current === key) setRecord(prior => prior?.key === key ? { key, detail: mergeOlder(prior.detail, next) } : prior);
    } catch (e) { if (!controller.signal.aborted && currentKey.current === key) setDetailError((e as Error).message); }
    finally { if (!controller.signal.aborted && currentKey.current === key) setLoadingOlder(false); }
  }
  async function mutate(payload: object) {
    if (!detail) throw new Error('请先选择会话');
    await writeControl({ op: 'context', id: detail.session.id, version: detail.session.version, ...payload });
    message.success('操作已完成'); setRefresh(value => value + 1);
  }
  const confirmContext = (action: 'clear' | 'restore', backupId?: string) => modal.confirm({
    title: action === 'clear' ? '清空当前会话？' : '恢复这份上下文备份？',
    content: '仅作用于当前会话。修改前会保留一份恢复备份。', okText: '确认', cancelText: '取消',
    onOk: async () => { try { await mutate({ action, backupId }); } catch (e) { message.error((e as Error).message); throw e; } },
  });
  const selector = <Select aria-label="选择真实会话" className="live-session-select" value={selection?.id} placeholder="暂无会话" options={bootstrap?.sessions.map(s => ({ value: s.id, label: sessionLabel(s) + (s.activePersona ? '' : ' · 历史角色') }))} onChange={id => { setDetailError(''); setSelected(id); }} />;
  if (!bootstrap) return <div className="page-stack">{error ? <Alert type="error" showIcon message={error} action={<Button onClick={() => setRefresh(v => v + 1)}>重试</Button>} /> : <Card><div className="live-loading"><Spin tip="正在读取真实数据"><div className="live-spinner" /></Spin></div></Card>}</div>;
  const { persona, sources } = bootstrap;
  let page;
  if (route === '/context') page = <div className="two-columns live-context-columns">
    <Card title="对话历史" extra={detail && <Text type="secondary">{detail.session.turnCount} 轮</Text>}>
      <div className="section-toolbar"><span><Text type="secondary">当前角色卡：</Text><Tag>{persona.name}</Tag></span><Space wrap><Button onClick={() => setRefresh(v => v + 1)}>刷新对话</Button><Button type="primary" ghost disabled title="新增上下文待接入">新增上下文</Button></Space></div>
      <div className="history-toolbar">{selector}</div>
      <LiveHistory key={key} detail={detail} loading={loading} mutate={mutate} />
      {detail?.nextBefore && <Button className="form-actions" loading={loadingOlder} onClick={older}>加载更早对话</Button>}
    </Card>
    <div className="page-stack"><Card title="上下文管理"><ReferenceContextLimit /><div className="action-list">
      <div className="action-row"><div><Text strong>清空当前上下文</Text><Paragraph>立即清空当前会话上下文，操作前自动保留备份。</Paragraph></div><Space><Switch checked disabled checkedChildren="备份" aria-label="清空前自动备份" /><Button type="primary" disabled={!detail} onClick={() => confirmContext('clear')}>清空上下文</Button></Space></div>
      <div className="action-row"><div><Text strong>恢复最近备份</Text><Paragraph>从最近保存的备份中恢复会话上下文。</Paragraph></div><Button disabled={!detail?.backups?.length} onClick={() => setRestoreOpen(true)}>恢复备份</Button></div>
      <div className="action-row"><div><Text strong>清空上下文与记忆数据</Text><Paragraph>仅清理上下文、备份、提醒与核心记忆等对话相关数据，保留角色卡与个性化配置。</Paragraph></div><Button danger disabled title="待接入">清空</Button></div>
      <div className="action-row"><Text strong>备份当前上下文</Text><Button disabled={!detail} onClick={async () => { try { await mutate({ action: 'backup' }); } catch (e) { message.error((e as Error).message); } }}>备份上下文</Button></div>
    </div></Card><Card title="长期记忆"><SessionSummary detail={detail} loading={loading} /></Card></div>
  </div>;
  else if (route === '/memory') page = <div className="two-columns">
    <Card title="核心记忆"><div className="section-toolbar history-toolbar">{selector}</div>{detail ? <SummaryEditor key={detail.session.version} detail={detail} mutate={mutate} /> : <SessionSummary detail={detail} loading={loading} />}</Card>
    <RealWorldbook key={persona.id} />
  </div>;
  else if (route === '/personas') page = <RealPersonas data={bootstrap} saved={() => setRefresh(v => v + 1)} />;
  else if (route === '/model-settings') page = <RealSettings key={bootstrap.configVersion} data={bootstrap} saved={() => setRefresh(v => v + 1)} />;
  else if (route === '/reminders') page = <RealSchedules />;
  else page = <Card><Empty description="页面不存在" /></Card>;
  return <><div className="live-toolbar"><Button title={`读取于 ${date(bootstrap.fetchedAt)}`} icon={<ReloadOutlined />} onClick={() => setRefresh(v => v + 1)}>刷新数据</Button></div>
    {detailError && <Alert type="error" showIcon message={detailError} className="storage-alert" action={<Button onClick={() => setRefresh(v => v + 1)}>重试</Button>} />}
    {sources.some(s => s.status === 'missing') && <Alert type="warning" showIcon message="部分通道尚无记忆存储，当前仅展示已读取的数据。" className="storage-alert" />}{page}<Modal title="恢复上下文备份" open={restoreOpen} okText="恢复" cancelText="取消" okButtonProps={{ disabled: !backupId }} onCancel={() => { setRestoreOpen(false); setBackupId(undefined); }} onOk={async () => { try { await mutate({ action: 'restore', backupId }); setRestoreOpen(false); setBackupId(undefined); } catch (e) { message.error((e as Error).message); throw e; } }}><Select aria-label="选择备份" style={{width:'100%'}} placeholder="选择备份时间" value={backupId} onChange={setBackupId} options={detail?.backups?.map(b => ({value:b.id,label:date(b.createdAt)}))} /><Paragraph className="spaced">恢复会替换当前会话的历史与记忆，替换前自动保留备份。</Paragraph></Modal></>;
}

type Mutate = (payload: object) => Promise<void>;
function SummaryEditor({ detail, mutate }: { detail: LiveDetail; mutate: Mutate }) {
  const { message } = App.useApp();
  const [text, setText] = useState(detail.summary), [busy, setBusy] = useState(false), [editing, setEditing] = useState(false);
  return <><label className="field-label" htmlFor="live-core-memory">核心记忆内容</label><Input.TextArea id="live-core-memory" aria-label="核心记忆内容" rows={8} value={text} disabled={busy} readOnly={!editing} onClick={() => setEditing(true)} placeholder="例如：用户喜欢吃苹果，每天很晚才回家……" onChange={e => setText(e.target.value)} />
    <Paragraph type="secondary" className="field-hint">点击上方内容或下方“编辑”按钮后才可修改核心记忆。</Paragraph><ReferenceSummaryOptions />
    <Space className="form-actions"><Button disabled={busy} onClick={() => setEditing(true)}>编辑</Button><Button disabled={busy || !editing} onClick={() => { setText(detail.summary); setEditing(false); }}>取消</Button><Button type="primary" disabled={!editing} loading={busy} onClick={async () => { setBusy(true); try { await mutate({ action: 'summary', text }); setEditing(false); } catch(e) { message.error((e as Error).message); } finally { setBusy(false); } }}>保存</Button></Space></>;
}

type HistoryRow = { id: string; role: 'user' | 'assistant'; index: number; content: string; sequence: number; time: number; status?: string; complete: boolean; edited: boolean };
function LiveHistory({ detail, loading, mutate }: { detail: LiveDetail | null; loading: boolean; mutate: Mutate }) {
  const { message, modal } = App.useApp();
  const [editing, setEditing] = useState<HistoryRow[] | null>(null), [text, setText] = useState(''), [busy, setBusy] = useState(false);
  const rows: HistoryRow[] = detail?.turns.flatMap(turn => [
    ...turn.inputs.map((input, index) => ({ id: `${turn.sequence}-u-${index}`, role: 'user' as const, index, content: input.text, sequence: turn.sequence, time: turn.createdAt, complete: turn.complete, edited: !!input.edited })),
    ...turn.replies.map((reply, index) => ({ id: `${turn.sequence}-a-${index}`, role: 'assistant' as const, index, content: reply.text, sequence: turn.sequence, time: turn.createdAt, status: reply.deliveryStatus, complete: turn.complete, edited: !!reply.edited })),
  ]) ?? [];
  const groups: HistoryRow[][] = [];
  for (const row of rows) {
    const prior = groups.at(-1);
    if (prior?.[0].role === row.role) prior.push(row); else groups.push([row]);
  }
  const refs = (group: HistoryRow[]) => group.map(({sequence,index}) => ({sequence,index}));
  const remove = (group: HistoryRow[]) => modal.confirm({ title: '删除这条消息？', content: '删除这张卡片中的内容，对方消息保留。可从备份中恢复。', okText: '删除', cancelText: '取消', okButtonProps: { danger: true }, onOk: async () => { try { await mutate({ action: 'delete_group', role: group[0].role, messages: refs(group) }); } catch (e) { message.error((e as Error).message); throw e; } } });
  const save = async () => {
    if (!editing || !text.trim()) return;
    setBusy(true);
    try { await mutate({ action: 'edit_group', role: editing[0].role, messages: refs(editing), text }); setEditing(null); }
    catch(e) { message.error((e as Error).message); }
    finally { setBusy(false); }
  };
  return <><Spin spinning={loading}><div className="history-list"><div className="history-columns" aria-hidden="true"><span>角色</span><span>内容</span><span>操作</span></div>{groups.map(group => <article className={`message-card message-${group[0].role}`} key={group[0].id}>
    <div className="message-heading"><Tag color={group[0].role === 'assistant' ? 'blue' : 'green'}>{group[0].role === 'assistant' ? 'AI' : '用户'}</Tag></div>
    <div className="message-content"><Text type="secondary" className="message-time">{date(group[0].time)}</Text><Paragraph className="context-text" ellipsis={{rows:4,expandable:'collapsible',symbol:expanded => expanded ? '收起' : '展开全文'}}>{group.map(row => row.content).join('\n\n')}</Paragraph></div>
    <div className="message-footer"><span>{group.some(row => row.edited) && <Text type="secondary">已编辑</Text>}</span><Space className="message-controls" size={4}><Button type="text" aria-label={`编辑消息 ${group[0].id}`} onClick={() => { setEditing(group); setText(group.map(row => row.content).join('\n\n')); }}>编辑</Button><Button type="text" danger aria-label={`删除消息 ${group[0].id}`} onClick={() => remove(group)}>删除</Button></Space></div>
  </article>)}{!rows.length && !loading && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前会话暂无对话记录" />}</div></Spin>
    <Modal title={editing?.[0].role === 'assistant' ? '编辑 AI 上下文' : '编辑用户上下文'} open={!!editing} onCancel={() => { if (!busy) setEditing(null); }} onOk={save} confirmLoading={busy} okText="保存到机器人" cancelText="取消" okButtonProps={{disabled:!text.trim()}}><Input.TextArea aria-label="上下文内容" value={text} onChange={e => setText(e.target.value)} rows={10} maxLength={100000} disabled={busy} /></Modal></>;
}
