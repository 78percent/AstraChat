# AstraChat 控制台

沿用已确认的 KouriChat / Ant Design 外观，品牌显示 AstraChat，保留原头像；桌面快捷方式使用同一头像。桌面 AstraChat 默认打开“首页”，可启动、重新连接和停止机器人，并从“机器人设置”进入控制台。使用本机 AstrBot 的真实数据。旧模拟页面已移除。

## 真实数据

- 首页：沿用现有机器人生命周期管理；显示微信心跳及已启用的语音识别、合成状态。停止机器人或关闭桌面窗口会正常停止监听、AstrBot 和 Docker Desktop，保留账号、配置和聊天数据。
- 角色卡：读取已绑定人设及历史版本，支持新建、编辑、保存并使用、切换；不同角色 ID 使用各自的上下文。
- 上下文：桌面微信、QQ、ClawBot 微信分别读取，按平台、联系人/群聊、角色隔离。支持分页、编辑消息、删除整轮、清空和恢复本地备份。身份标签固定为绿色“用户”、蓝色“AI”。
- 长期记忆：编辑当前会话摘要，按实际 Token 预算校验；自动总结尚未启用。
- 世界书：按人设 ID 持久化，最多 20 条、启用条目合计最多 3,000 估算 Tokens。支持始终生效或任一关键词触发、不区分大小写；关键词匹配当前消息（定时生成为生成要求）。可选择人设之前/之后插入，各位置内按页面顺序排列。已匹配的内容实际进入 QQ、ClawBot 与桌面微信的请求，并计入总上下文预算。修改从下一次提示词构建生效。
- 定时服务：桌面微信已绑定的群聊、私聊可创建一次性/每天/每周任务，使用北京时间。支持固定文字和按执行时的人设、世界书、对应会话上下文生成；每次最多 125 字。预览会调用真实模型但不发送（固定文字无需模型）。可暂停、编辑、删除及从已删除列表恢复。任务执行结果和消息保留在执行记录，不伪造用户对话或收件人送达回执。
- 配置：从当前提供方读取真实模型目录，保存模型、最近对话轮数、回复概率、气泡和等待参数。保存通过正在运行的插件应用，无需为普通保存重载插件或断开微信监听。桌面微信仍受 4 气泡 / 合计 125 字符限制。
- 表单和上下文均有版本检查，过期编辑不会覆盖新数据。上下文变更会保存原始记录，并使旧的生成任务失效。

真实数据通过本机接口管理。浏览器仅保存主题偏好；首页的启动控制管理本机机器人。定时任务由后台插件执行，关闭浏览器标签页仍能执行；停止机器人、退出桌面启动器或电脑休眠时不能执行。超过计划时间两分钟则记为错过，不连续补发；日/周任务顺延到下一周期。发送结果不明时不自动重试。

世界书与任务保存在 `runtime/astrbot/plugin_data/astrbot_plugin_roleplay_gateway/console-features.json`，变更前的版本保存在同目录 `console-feature-backups`。任务启用前应确认接收会话、内容和时间。当前定时发送接入桌面微信，QQ / ClawBot 未扩展主动发送。

## 打开

双击桌面 `AstraChat.lnk`，原来的 `start_wechat_desktop.py` 默认入口也会打开新窗口。窗口使用原生 WebView2；依赖位于 `.runtime/wechat-native/.venv`，版本见 `requirements-console.txt`。重复打开只唤起同一个新控制台。

也可用项目根目录 `启动控制台预览.cmd` 打开浏览器版，地址 `http://127.0.0.1:5178/`。浏览器版关标签页不等同于停止服务，请使用首页“停止机器人”。旧 Tk 窗口若仍在运行，继续负责原会话的生命周期；新控制台可直接管理该会话，两者共用服务锁。旧会话停止后，下一次从新窗口启动将由新窗口负责。

## 开发

```powershell
cd console-ui
npm ci
npm run dev
npm run build
npm test
```

Windows 如遇 npm.ps1 包装器卡住，可以使用 `npm.cmd`。`dev` / Vite `preview` 只提供前端资源，检查真实数据请构建后使用根目录启动器；二者使用相同端口，不要同时启动。

## 数据适配层

`scripts/serve_console_preview.py` 仅监听 `127.0.0.1:5178`。API 校验本机 Host、同源 Origin / Fetch Metadata；读取需要 `X-Console-Read: 1`，写入还需要 `X-Console-Write: 1` 和 JSON 请求体。不开放跨域，响应禁止缓存。

`console_data_bridge.py` 通过固定的 Docker 容器、参数数组和 12 秒超时运行 `console_container_snapshot.py`。读取脚本经 stdin 进入容器，不安装、不复制脚本到运行容器。**实时 SQLite 只在写入容器内以 `mode=ro` 和 `query_only` 读取，不在 Windows 挂载目录打开，不实例化 MemoryStore，不迁移数据库。**

写入由 `console_runtime_request.py` 经固定 Docker 命令进入插件，仅连接容器内部回环管理接口；凭据在容器内部读取，不交给前端。修改上下文使用运行服务所拥有的 MemoryStore 连接和锁。配置变更原子保存、保留备份，失败时回滚进程和磁盘状态。

数据库、会话、请求页数、源文件大小均有限制；密钥、账户原始标识、消息 ID 和投递引用不返回前端。已知错误转换成固定提示，服务不可用时不回退成模拟成功。验证不向联系人发送消息。

验证命令（项目根目录）：

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_console_live.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_console_management.py -v
.venv/Scripts/python.exe -m unittest discover -s tests -p test_console_features.py -v
.runtime/wechat-native/.venv/Scripts/python.exe scripts/astrbot_launcher.py --verify-hidden
```

前端在 `console-ui` 中执行 `npm test` 与 `npm run build`。品牌资源来源见 [第三方说明](../THIRD_PARTY.md)。本次清理审查见 [审查记录](../docs/CLEANUP-2026-10-30.md)。
