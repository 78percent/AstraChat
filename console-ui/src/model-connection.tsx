import { useState } from 'react';
import { App, Button, Card, Descriptions, Input, Popconfirm, Space, Tag, Typography } from 'antd';
import { writeControl, type ModelProfile, type ModelProfiles } from './live-data';

type Probe = { profile: ModelProfile; model: string; reply: string; elapsedSeconds: number; firstTextSeconds: number; sent: false };
export function ModelConnection({ profiles, profile, model, changed }: {
  profiles: ModelProfiles; profile: ModelProfile; model: () => string; changed: (profiles: ModelProfiles) => void;
}) {
  const { message } = App.useApp();
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState('');
  const [result, setResult] = useState<Probe | null>(null);
  const updateKey = async (action: 'save' | 'delete') => {
    setBusy(action); setResult(null);
    try {
      const value = await writeControl<ModelProfiles>({ op: 'model_key', action, keyRevision: profiles.kouri.keyRevision,
        ...(action === 'save' ? { apiKey: key.trim() } : {}) });
      setKey(''); changed(value); message.success(action === 'save' ? 'Key 已保存' : 'Key 已删除');
    } catch (error) { message.error((error as Error).message); }
    finally { setBusy(''); }
  };
  const test = async () => {
    setBusy('test'); setResult(null);
    try {
      const value = await writeControl<Probe>({ op: 'model_test', profile, model: model().trim() });
      setResult(value); message.success('流式调用与回复格式检查通过');
    } catch (error) { message.error((error as Error).message); }
    finally { setBusy(''); }
  };
  const activeKouri = profiles.active === 'kouri';
  return <Card title={profile === 'kouri' ? 'Kouri API Key' : 'DeepSeek API'} extra={<Tag color={profile === 'deepseek' || profiles.kouri.keyConfigured ? 'green' : 'default'}>{profile === 'deepseek' || profiles.kouri.keyConfigured ? '已配置' : '未配置'}</Tag>}>
    <Descriptions column={1} size="small" items={[
      { key: 'url', label: '调用端点', children: profile === 'kouri' ? profiles.kouri.baseUrl : 'https://api.deepseek.com' },
      { key: 'key', label: 'API Key', children: profile === 'kouri' ? profiles.kouri.maskedKey || '—' : '沿用现有本机配置' },
    ]} />
    {profile === 'kouri' && <div className="key-input"><label className="field-label" htmlFor="kouri-api-key">{profiles.kouri.keyConfigured ? '更换 Key' : 'API Key'}</label>
      <Input.Password id="kouri-api-key" autoComplete="new-password" value={key} maxLength={512} disabled={!!busy || activeKouri}
        placeholder={activeKouri ? '切回 DeepSeek 后可更换' : '输入 Kouri API Key'} onChange={e => setKey(e.target.value)} />
      <Space wrap style={{ marginTop: 12 }}><Button type="primary" disabled={!key.trim() || !!busy || activeKouri} loading={busy === 'save'} onClick={() => updateKey('save')}>保存 Key</Button>
        <Popconfirm title="删除已保存的 Kouri Key？" onConfirm={() => updateKey('delete')} okText="删除" cancelText="取消" disabled={!profiles.kouri.keyConfigured || activeKouri || !!busy}>
          <Button danger disabled={!profiles.kouri.keyConfigured || activeKouri || !!busy}>删除 Key</Button>
        </Popconfirm></Space>
    </div>}
    <Button style={{ marginTop: 16 }} loading={busy === 'test'} disabled={!!busy || (profile === 'kouri' && (!profiles.kouri.keyConfigured || !!key.trim()))} onClick={test}>测试连接</Button>
    {result && result.profile === profile && result.model === model().trim() && <div role="status" style={{ marginTop: 16 }}>
      <Typography.Text type="success">{result.reply}</Typography.Text>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>{result.model} · {result.elapsedSeconds} 秒 · 未发送消息</Typography.Paragraph>
    </div>}
  </Card>;
}
