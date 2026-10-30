import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Descriptions, Space, Tag, Timeline, Typography } from 'antd';
import { PlayCircleOutlined, PoweroffOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import { writeControl } from './live-data';
import dayjs from 'dayjs';

type State = { stage: string; message: string; connected: boolean; active: boolean; canStart: boolean; canReconnect: boolean; heartbeatAt?: number; needsCleanup?: boolean; voice?: { enabled: boolean; asr?: string; tts?: string } };
const voiceTitles: Record<string, string> = { starting: '预热中', ready: '就绪', failed: '不可用', closed: '已关闭' };
const titles: Record<string, string> = { starting: '正在启动', waiting_for_wechat: '等待微信登录', ready: '机器人运行中', degraded: '需要重新连接', reconnecting: '正在重新连接', stopping: '正在停止', stopped: '机器人已停止', failed: '启动未完成', stop_failed: '停止未完成' };
export function HomePage() {
  const { message, modal } = App.useApp();
  const [state, setState] = useState<State | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState('');
  const [events, setEvents] = useState<{ stage: string; message: string; time: string }[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    const refresh = async () => {
      try {
        const response = await fetch('/api/launcher', { headers: { 'X-Console-Read': '1' }, cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error();
        const data: State = await response.json();
        if (controller.signal.aborted) return;
        setState(data); setError('');
        setEvents(prior => prior[0]?.stage === data.stage && prior[0]?.message === data.message ? prior : [{ stage: data.stage, message: data.message, time: dayjs().format('HH:mm:ss') }, ...prior].slice(0, 6));
      } catch { if (!controller.signal.aborted) setError('暂时无法连接本机启动服务。'); }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 2000);
    };
    refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, []);
  const run = async (action: string) => {
    setBusy(action);
    try { setState(await writeControl<State>({ action }, 'launcher')); }
    catch (e) { message.error((e as Error).message); }
    finally { setBusy(''); }
  };
  return <div className="page-stack">{error && <Alert type="error" showIcon message={error} />}
    <div className="two-columns home-columns"><Card title="启动机器人" extra={<Tag color={state?.connected ? 'green' : state?.active ? 'processing' : undefined}>{state?.connected ? '已连接' : state?.active ? '运行中' : '已停止'}</Tag>}>
      <Typography.Title level={3} className="home-status">{state ? titles[state.stage] || '正在检查' : '正在检查'}</Typography.Title>
      <Typography.Paragraph type="secondary" className="home-status-message">{state?.message || '正在读取服务状态…'}</Typography.Paragraph>
      <Space wrap size={12} className="home-actions"><Button type="primary" size="large" icon={<PlayCircleOutlined />} disabled={!state?.canStart || !!busy} loading={busy === 'start'} onClick={() => run('start')}>启动机器人</Button>
        <Button size="large" icon={<ReloadOutlined />} disabled={!state?.canReconnect || !!busy} loading={busy === 'reconnect' || state?.stage === 'reconnecting'} onClick={() => run('reconnect')}>重新连接</Button>
        <Button size="large" danger icon={<PoweroffOutlined />} disabled={(!state?.active && !state?.needsCleanup) || state?.stage === 'stopping' || !!busy} loading={busy === 'stop' || state?.stage === 'stopping'} onClick={() => modal.confirm({ title: '停止机器人？', content: '将停止微信监听、机器人服务和 Docker Desktop，保留配置和聊天记录。', okText: '停止', cancelText: '取消', onOk: () => run('stop') })}>停止机器人</Button>
      </Space>
    </Card><Card title="机器人设置"><Descriptions column={1} items={[
      { key: 'console', label: '控制台', children: '上下文、模型与人设' },
      { key: 'link', label: '微信监听', children: state?.connected ? <Tag color="green">已连接</Tag> : '未连接' },
      { key: 'time', label: '最近心跳', children: state?.heartbeatAt ? dayjs(state.heartbeatAt * 1000).format('HH:mm:ss') : '—' },
      ...(state?.voice?.enabled ? [
        { key: 'asr', label: '语音识别', children: voiceTitles[state.voice.asr || ''] || '未启用' },
        { key: 'tts', label: '声音合成', children: voiceTitles[state.voice.tts || ''] || '未启用' },
      ] : []),
    ]} /><Button type="primary" ghost size="large" icon={<SettingOutlined />} className="form-actions" onClick={() => { window.location.hash = '/'; }}>机器人设置</Button></Card></div>
    <Card title="运行记录"><Timeline items={events.map((event, index) => ({ key: index, color: index === 0 ? '#32a5b7' : 'gray', children: <Space size={20}><Typography.Text type="secondary">{event.time}</Typography.Text><Typography.Text>{event.message}</Typography.Text></Space> }))} /></Card>
  </div>;
}
