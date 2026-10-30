export type LiveSession = {
  id: string; channel: string; channelLabel: string; kind: 'private' | 'group'; peerLabel: string;
  personaId: string; activePersona: boolean; epoch: number; turnCount: number; summaryRevision: number; updatedAt: number; version?: string;
};
type LiveTurn = {
  sequence: number; personaRevision: string; complete: boolean; createdAt: number;
  inputs: { text: string; displayName: string; edited?: boolean }[];
  replies: { text: string; deliveryStatus: 'SENT' | 'API_ACCEPTED'; edited?: boolean }[];
};
export type LiveDetail = {
  session: LiveSession; summary: string; summarySourceWatermark: number; turns: LiveTurn[];
  nextBefore: number | null; fetchedAt: number;
  backups?: { id: string; createdAt: number }[];
};
export type LiveBootstrap = {
  schemaVersion: number; readOnly: boolean; fetchedAt: number; configVersion: string;
  persona: { id: string; name: string; revision: string; prompt: string; format: string; examples: { role: string; content: string }[] };
  config: {
    modelProfiles?: ModelProfiles;
    searchModel?: string; imageModel?: string;
    mode: string; providerId: string; model: string | null; providerEnabled: boolean;
    channels: Record<string, boolean>; onlineLookupEnabled: boolean; nativeReplyProbability: number | null;
    pacing: Record<string, number | boolean>; contextBudget: Record<string, number>;
  };
  sessions: LiveSession[]; sources: { channel: string; label: string; status: 'ready' | 'missing' }[];
};
export type ModelProfile = 'deepseek' | 'kouri';
export type ModelProfiles = {
  active: ModelProfile; models: Record<ModelProfile, string>;
  kouri: { baseUrl: string; keyConfigured: boolean; maskedKey: string; keyRevision: string };
};
const errors: Record<string, string> = {
  active_persona_delete: '请先切换到其他角色，再删除当前角色。',
  invalid_console_model: '请输入有效模型 ID；DeepSeek 官方方案需使用 deepseek- 开头的模型。',
  invalid_model_profile: '请选择 DeepSeek 或 Kouri 方案。',
  invalid_model_key: '请输入有效的 API Key，不要包含空格。',
  model_key_required: '请先保存 Kouri API Key。',
  model_key_in_use: '请先切回 DeepSeek，再更换或删除 Kouri Key。',
  model_key_changed_restart_required: '令牌文件已在外部更改，请重新连接机器人。',
  model_credentials_unavailable: '无法读取本机令牌配置，请检查文件。',
  model_provider_unavailable: '模型服务尚未就绪，请先启动机器人。',
  model_auth_failed: '令牌鉴权失败，请检查 API Key 是否有效。',
  model_access_denied: '渠道拒绝访问，请检查令牌权限或渠道限制。',
  model_rate_limited: '渠道限流或额度不足，请检查账户后重试。',
  model_parameters_rejected: '渠道不接受当前请求参数或模型 ID，方案未切换。',
  model_test_timeout: '模型测试超时，方案未切换。',
  model_response_invalid: '模型回复未通过机器人的流式格式检查，方案未切换。',
  model_test_failed: '模型调用未成功，请检查渠道或网络；当前方案保持不变。',
  invalid_persona: '角色已变化，请刷新后重试。',
  invalid_worldbook: '世界书内容不能为空，最多 20 条；请检查条目内容。',
  invalid_worldbook_keywords: '请填写至少一个触发词，多个词用逗号分隔。',
  worldbook_budget_exceeded: '已启用世界书超过 3,000 Token 预算，请缩短内容或暂停部分条目。',
  feature_service_unavailable: '世界书和定时服务暂未就绪，请检查机器人连接。',
  invalid_schedule: '请检查任务内容和会话；固定文字最多 125 字，生成要求最多 800 字。',
  invalid_schedule_time: '请选择有效的执行时间。',
  schedule_time_in_past: '请选择至少 5 秒后的时间，再启用任务。',
  schedule_target_unavailable: '此接收会话不在当前已允许的微信会话中。',
  schedule_service_unavailable: '机器人服务未就绪，请在首页检查连接。',
  schedule_generated_text_invalid: '生成消息为空或超过 125 字，请调整要求后重试。',
  schedule_busy: '此任务正在执行，请完成后再修改。',
  too_many_schedules: '任务已达到 100 项上限。',
  revision_conflict: '数据已发生变化，请刷新后重新编辑；本次没有覆盖新数据。',
  management_unavailable: '机器人控制接口尚未就绪，请在首页启动机器人。',
  management_failed: '操作未确认完成，请刷新检查状态后重试。',
  summary_budget_exceeded: '摘要超过当前模型预算，请缩短内容后保存。',
  fixed_context_budget_exceeded: '人设内容超过模型上下文预算，请缩短后保存。',
  persona_budget_exceeded: '人设内容过长，请缩短后保存。',
  invalid_context_text: '请输入非空内容，最多 10,000 字符。',
  incompatible_pacing_deadlines: '等待时间与气泡数超出回复时限，请缩短等待或减少气泡数。',
  model_not_available: '当前账号不可使用这个模型，请重新读取模型列表。',
  native_reply_limit: '桌面微信当前最多支持 4 个气泡、每次合计 125 字符。',
  runtime_unavailable: '暂时无法读取机器人服务，请确认 Docker 和 AstrBot 正在运行。',
  persona_revision_mismatch: '人设文件与配置中的版本不一致，已停止读取。',
  session_changed: '该会话已更新或清空，请刷新后查看。',
  session_not_found: '该会话已不存在，请刷新会话列表。',
  invalid_memory_record: '对话记录格式异常，本次未展示该会话。',
  unsupported_memory_schema: '当前记忆数据库版本尚不支持读取。',
  local_origin_required: '请从本机控制台地址打开此页面。',
  reader_busy: '数据读取正在进行，请稍后重试。',
};
export async function writeControl<T = Record<string, unknown>>(payload: object, endpoint = 'control'): Promise<T> {
  const response = await fetch(`/api/${endpoint}`, { method: 'POST', cache: 'no-store', credentials: 'omit',
    headers: { 'X-Console-Read': '1', 'X-Console-Write': '1', 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    signal: AbortSignal.timeout(60000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(errors[data.error] || '操作失败，请检查输入或刷新重试。');
  return data;
}
export async function readLive<T>(path: string, signal: AbortSignal): Promise<T> {
  const timeout = new AbortController();
  const timer = window.setTimeout(() => timeout.abort(), 16000);
  try {
    const response = await fetch(`/api/live/${path}`, {
      headers: { 'X-Console-Read': '1' }, cache: 'no-store', credentials: 'omit',
      signal: AbortSignal.any([signal, timeout.signal]),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(errors[payload.error] || '暂时无法读取真实数据，请刷新重试。');
    return payload as T;
  } catch (error) {
    if (signal.aborted) throw error;
    if (timeout.signal.aborted) throw new Error('读取超时，请刷新重试。');
    if (error instanceof TypeError || error instanceof SyntaxError) throw new Error('无法连接本地数据接口，请使用启动器打开控制台。');
    throw error;
  } finally { window.clearTimeout(timer); }
}
export function sessionKey(session: LiveSession) { return `${session.id}:${session.epoch}`; }
export function mergeOlder(current: LiveDetail, older: LiveDetail): LiveDetail {
  if (sessionKey(current.session) !== sessionKey(older.session)) throw new Error('会话已变化，请刷新。');
  const turns = new Map([...older.turns, ...current.turns].map(turn => [turn.sequence, turn]));
  return { ...current, turns: [...turns.values()].sort((a, b) => a.sequence - b.sequence), nextBefore: older.nextBefore };
}
