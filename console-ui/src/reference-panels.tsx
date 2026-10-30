import { Button, Card, Form, Input, InputNumber, Modal, Slider, Space, Switch, Tabs, Tag, Typography } from 'antd';
const { Paragraph, Text } = Typography;

// These reference controls deliberately have no form names or request handlers.
// A pending field must never enter an existing settings payload.
export function Pending() { return <Tag className="pending-tag">待接入</Tag>; }

export function ReferenceSwitches() {
  return <Card title="功能开关" extra={<Pending />}><div className="reference-switch-grid">{[
    ['启用定时任务', '关闭后将不会识别或执行任何提醒任务，但仍可以重新开启。'],
    ['启用主动拉黑', 'Ta 也会有自己的一些小情绪，在拉黑期间你的消息对 Ta 仍然可见。'],
    ['启用识图功能', '启用后，Ta 可以识别并分析你发送的图片，使用 kourichat-vision 模型进行多模态理解。'],
    ['启用联网搜索', '启用后，Ta 会自动识别需要联网搜索的问题，并获取最新的网络信息来回答。'],
  ].map(([label, hint]) => <Card size="small" key={label} title={<span>{label}{label === '启用联网搜索' && <Tag className="beta-tag" color="orange">BETA</Tag>}</span>} extra={<Switch disabled aria-label={label} />}><Paragraph type="secondary">{hint}</Paragraph>{label === '启用联网搜索' && <Text type="danger">Beta 功能，暂不推荐开启，可能出现不稳定或额外费用。</Text>}</Card>)}</div></Card>;
}
export function ReferenceDanger() {
  return <Card title="危险操作" className="danger-panel" extra={<Pending />}><Paragraph type="danger">永久删除所有与当前用户相关的数据，包括上下文、提示词、提醒、角色卡、自定义偏好等。此操作不可恢复。</Paragraph><Button danger disabled>删除我的所有数据</Button></Card>;
}
export function ReferenceWait() {
  return <Card title="回复等待时间" extra={<Pending />}><Paragraph type="secondary">收到新消息后延迟回复的时间设置；最长等待用于兜底合并，避免长时间挂起。</Paragraph><div className="two-fields"><div><label className="field-label" htmlFor="pending-wait">每条消息等待 (秒)</label><InputNumber id="pending-wait" disabled placeholder="待接入" style={{ width: '100%' }} /></div><div><label className="field-label" htmlFor="pending-max-wait">最长聚合等待 (秒)</label><InputNumber id="pending-max-wait" disabled placeholder="待接入" style={{ width: '100%' }} /></div></div><Space wrap className="form-actions"><Button type="primary" disabled>保存等待时间</Button><Button disabled>恢复默认</Button></Space></Card>;
}
export function ReferenceImport() {
  return <Card title="导入新角色" extra={<Pending />}><Paragraph type="secondary">支持直接粘贴角色卡链接或仅输入角色 ID，可选自定义名称并决定是否立即启用。</Paragraph><Form layout="vertical" component="div" disabled><Form.Item label="角色链接 / ID" required htmlFor="pending-persona-link"><Input id="pending-persona-link" placeholder="例如：https://... 或 123456" /></Form.Item><Form.Item label="自定义名称" htmlFor="pending-persona-name"><Input id="pending-persona-name" placeholder="可选，默认为角色卡原名" /></Form.Item><Form.Item label="导入后立即启用"><Switch aria-label="导入后立即启用" disabled /></Form.Item><Space wrap><Button type="primary">导入角色卡</Button><Button>获取角色卡</Button></Space></Form></Card>;
}
export function ReferenceContextLimit() {
  return <Card title="上下文限制" className="inner-card" extra={<Pending />}><div className="limit-form"><label htmlFor="pending-context-limit">自定义限制：</label><InputNumber id="pending-context-limit" disabled placeholder="—" /><Button type="primary" disabled>保存</Button><Button disabled>重置表单</Button></div><Text type="secondary">当前生效值：—</Text></Card>;
}
export function ReferenceSummaryOptions() {
  return <div className="two-fields summary-options"><div><label className="field-label">自动总结 <Pending /></label><Switch disabled aria-label="自动总结" /></div><div><label className="field-label required" htmlFor="pending-summary-interval">总结间隔（消息数）</label><InputNumber id="pending-summary-interval" disabled placeholder="待接入" /></div></div>;
}
export function ReferenceAdvanced({ open, close }: { open: boolean; close: () => void }) {
  const field = (label: string, min: number, max: number, step: number) => <div className="advanced-field" key={label}><Text>{label}</Text><div className="slider-row"><Slider disabled min={min} max={max} step={step} /><InputNumber disabled aria-label={label} placeholder="待接入" /></div></div>;
  return <Modal title={<Space>高级模型设置<Pending /></Space>} open={open} onCancel={close} width={520} footer={<div className="advanced-footer"><Space wrap><Button disabled>恢复会话默认</Button><Button disabled>恢复提醒默认</Button></Space><Space><Button onClick={close}>取消</Button><Button type="primary" disabled>保存</Button></Space></div>}><Paragraph type="secondary">自定义温度、最大输出 Token 等高级参数。留空表示沿用系统默认值。</Paragraph><Tabs centered items={[
    { key: 'conversation', label: '会话模型', children: <>{field('回退模型温度 (0 - 2)', 0, 2, .1)}{field('回退模型最大输出 Tokens（推荐 128 - 64000）', 128, 64000, 1)}<div className="divider" />{field('温度 (0 - 2)', 0, 2, .1)}{field('最大输出 Tokens（推荐 128 - 64000）', 128, 64000, 1)}</> },
    { key: 'reminder', label: '提醒模型', children: <>{field('温度 (0 - 2)', 0, 2, .1)}{field('最大输出 Tokens（推荐 128 - 64000）', 128, 64000, 1)}</> },
  ]} /></Modal>;
}
