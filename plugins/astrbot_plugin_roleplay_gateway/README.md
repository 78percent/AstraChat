# QQ 与微信角色回复插件

适配 AstrBot **4.29.1**。QQ 由 SnowLuma/OneBot 接入，个人微信使用 AstrBot 原生 `weixin_oc`；微信默认关闭。原生 LLM 保持 `provider_settings.enable=false`，由本插件统一负责准入、模型、分段和角色记忆，避免一条消息触发两套回复。

## 在哪里配置

打开 AstrBot `http://127.0.0.1:6185` → **插件** → **QQ 与微信角色回复及隔离记忆** → 配置。SnowLuma 的 `http://127.0.0.1:5099` 只管理 QQ 进程与登录。

| 配置 | 作用 |
|---|---|
| `platform_id` / `bot_self_id` | 受管 QQ 节点 ID / 已登录机器人 QQ 号 |
| `admin_private_ids` | 允许与角色私聊的 QQ 号；空列表不响应私聊 |
| `allowed_group_ids` | 允许回复的 QQ 群；空列表不响应群聊 |
| `wake_names` / `wake_regex` | QQ 群字面名字、别名和正则，与实际 @机器人按 OR 唤醒 |
| `memory_admin_ids` | QQ 记忆管理员；允许私聊不自动授予群记忆管理权 |
| `wechat_enabled` / `wechat_platform_id` | 微信独立开关与原生平台实例 ID，默认 `false` / `wechat-sakura` |
| `wechat_allowed_private_ids` | 允许微信私聊的不透明用户 ID；不是自行填写的微信号 |
| `wechat_admin_ids` | 微信记忆管理员，不能管理 QQ 数据 |
| `reply_pacing` | 两端共用回复概率、等待、气泡/字符上限与限频配置 |

名单、唤醒和模式修改后需要重新加载插件。AstrBot 的平台白名单是外层门禁；新增联系人或群时也需同步该白名单，本插件不会绕过它。QQ 群中的未唤醒消息不进入角色历史。私聊无需名字唤醒，但仍受回复概率与限频控制。

微信只处理指定实例的文本私聊，拒绝群聊、媒体、缺失稳定原始消息 ID、伪造或不一致的事件身份。版本、方法或微信配置不兼容时微信路径关闭，QQ 账本保持独立。

## 原生微信登录

AstrBot → **机器人** → **创建机器人** → **个人微信**，ID 填 `wechat-sakura`，在原生表单扫码后保存。此路线直接对接原生 `openclaw-weixin` 接口，不必另装 OpenClaw；不是 SnowLuma 的另一个 QQ 进程。

固定版本官方文档要求微信内有 ClawBot 插件（iOS ≥ 8.0.70，Android ≥ 8.0.69）：[AstrBot 4.29.1 个人微信文档](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/docs/zh/platform/weixin_oc.md)。普通微信群不在此接入范围。

先保持微信角色关闭；用户扫码并主动给微信机器人发消息，核对原生事件对端 ID 后，定向填写允许私聊列表、管理员和平台白名单，再启用微信角色。未绑定时不调用模型或写角色记忆。

登录 token、账号、同步游标与每位对端的 `context_token` 全由 AstrBot 原生保存在 `data/cmd_config.json`；插件不另存凭据。若原生发送因缺少上下文拒绝，需要用户先发一条消息刷新上下文。停用时设置 `wechat_enabled=false` 并重新加载插件，微信数据保留；QQ 无需退出。

## 模式与持久化

| 模式 | 行为 |
|---|---|
| `neutral_receipt` | 固定连通回复，不调用模型 |
| `neutral_stream` | 中性单轮模型分段，不用人设、历史或工具 |
| `roleplay_stream` | 校验已导入人设及 SHA256，加载会话近期历史与长期摘要，输出完整直接对话段落 |

角色与模型配置两端共用，当前模型边界是固定官方 `deepseek-flash-official`。未实现自动接口回退、自动摘要、工具扩权或高级世界书。

插件数据目录由 `StarTools.get_data_dir()` 定位，通常是 `data/plugin_data/astrbot_plugin_roleplay_gateway/`。QQ 保留原路径，微信使用其 `weixin/` 子目录，分别包含：

- `receipts.sqlite3`：入站去重、准入决策、请求状态及第一段发送证据。
- `segments.sqlite3`：分段序号、输入归属、尝试/epoch、发送状态与证据，不存正文。
- `memory.sqlite3`：角色会话近期问答、长期摘要和失效 epoch；只有确认发送的正文进入记忆。

独立 SQLite 连接、恢复与关闭生命周期，微信服务不会结算 QQ 的在途账本。每个 QQ 群独立、群内共享，每个私聊独立；微信还以平台/人设/会话命名空间隔离。`/记忆` 命令沿用权限与去重，微信本阶段只支持当前会话管理，不支持跨会话选择器。

QQ 与微信共享最多 **3 轮**活动回复并发，包含段间等待。两端各自保留会话串行、队列与准入账本。管理命令不调用模型、不受角色回复概率影响。未决发送的安全保护通过长寿命 AstrBot Context 保留，热重载不能绕过。

## 发送语义与节奏

QQ 直接使用 OneBot action，只有合法真实 `message_id` 才记 `SENT` / `PLATFORM_MESSAGE`。微信复用固定版本原生 `_send_items_to_session`，严格 `True` 才记 `API_ACCEPTED`；外部平台编号为空，内部引用独立保存，不伪造数字回执，不宣称客户端可见或已读。`False` 是明确拒绝，异常/超时/取消是 `UNKNOWN`，均不自动重发未知气泡。

微信原生不提供模型流式消息，本插件以多次普通完整文本发送复用角色分段。当前已选策略为 60% 回复概率、首段 5–10 秒与段间 3–5 秒截断正态等待、每轮最多 4 段 125 字、同会话 15 秒冷却、滚动一分钟最多 3 轮。模型生成与首段等待重叠；真实接口变慢仍可能超过择时上界。

QQ 已有用户可见的 5–6 秒首段、3 条干净气泡试聊反馈。微信的多气泡可见性、实际延迟、记忆管理及跨平台隔离必须独立验收；不能引用 QQ 结果作为微信成绩。60% 主动不回复与限频跳过也不能计为全体有效唤醒 95%/10 秒达标。

## 验证与回退

离线测试不访问真实模型或发送聊天消息：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
```

`tests/test_weixin_adapter.py` 覆盖固定原生发送契约、真实稳定 ID、身份核对、纯文本与拒绝/未知发送；`tests/test_gateway_adapter.py` 覆盖双平台配置、服务生命周期、白名单、管理与隔离。固定镜像中的原生契约及用户实际收发需另外记录。

不要通过删除账本修复登录或清除 UNKNOWN。停用微信保留其独立目录。代码回退须使用部署前源码及匹配账本版本，并保留上线后的 QQ 新对话；不能直接用旧快照覆盖当前 QQ 数据。项目部署见 [README](../../README.md)，本机历史预检与备份记录仍保留在未发布的操作文档中。
