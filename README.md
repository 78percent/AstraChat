# AstraChat

面向本机部署的 AstrBot 角色聊天项目，包含 React 控制台、Python 协调插件、QQ / 微信适配器和 Windows 桌面启动器。

当前源码提供角色卡、按平台与会话隔离的近期记忆、摘要编辑、世界书、流式分段回复、联网查询，以及桌面微信的固定文字 / 人设生成定时任务。首次部署需要配置自己的平台账号和模型接口。运行数据与私密配置不随源码发布。

## 开发与验证

需要 Python 3.12、uv、Node.js 22.12+ 或 24 LTS。Windows 使用 PowerShell；Linux 将 Python 路径换为 `.venv/bin/python`。

```powershell
uv sync --frozen
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests
cd console-ui
npm ci
npm test
npm run build
cd ..
.venv/Scripts/python.exe -X utf8 scripts/serve_console_preview.py
```

打开 http://127.0.0.1:5178/ 。静态页面可在构建后启动；真实数据页还需要本机 AstrBot 容器和已配置的插件，未连接时会显示服务不可用。`npm run dev` 只启动前端开发服务器，不代理管理 API。两者使用同一端口，请勿同时运行。

静态审查：`uvx ruff check scripts plugins tests`。前端构建同时检查未使用变量和参数。CI 模板见 [docs/ci-checks.yml](docs/ci-checks.yml)，启用时需由具有工作流权限的维护者放入 `.github/workflows/`。Git 提交后可运行 `python scripts/check_release.py` 检查纳入版本库的文件边界与常见密钥格式。

## 首次配置 QQ

1. 将 `config/connection-inputs.example.json` 复制为 `config/connection-inputs.local.json`，填入自己的机器人 QQ、管理员、允许联系人和群号。示例数字不可作为实际授权名单。示例人设可替换；替换后更新清单中的 SHA256。
2. 执行 `.venv/Scripts/python.exe -X utf8 scripts/prepare_local.py`。它只在首次启动时生成运行配置和随机凭据，已有部署不会被覆盖。首次生成后、启动容器前，再执行 `.venv/Scripts/python.exe -X utf8 scripts/configure_gateway.py --apply` 安装受控固定回执配置。
3. 安装并启动 Docker Desktop 的 Linux 容器引擎，执行 `docker compose build astrbot`、`docker compose up -d`，进入 http://127.0.0.1:6185/ 和 http://127.0.0.1:6099/webui 完成初始化及 QQ 登录。初始登录资料保存在本机 `runtime/access.local.json`。
4. 默认配置使用固定中性回执。需要模型回复时，将 `config/model-access.example.json` 复制为同名 `.local.json`，填写自己的 API Key；执行 `scripts/probe_deepseek.py` 验证接口，再在停止 AstrBot 后执行 `scripts/configure_deepseek.py --apply`、`scripts/configure_stream.py --apply`。使用同一个项目 Python 和 `-X utf8`。接口探测会产生真实模型请求。
5. 用 `scripts/configure_persona.py --source personas/example.txt --persona-id example --name 示例角色` 预览角色绑定，再加 `--apply` 应用，最后启动 AstrBot。查看命令的 `--help` 及[插件说明](plugins/astrbot_plugin_roleplay_gateway/README.md)。

已有部署应继续沿用原来的启动入口和账号，不要重新生成运行目录。`start-local.ps1` 默认采用 SnowLuma；只有已有 SnowLuma 配置时才使用此入口，NapCat 可使用 `-Protocol napcat`。切换协议会停止另一端，避免同一 QQ 并行登录。

## Windows 微信与语音

[桌面微信部署依赖](docs/WINDOWS-WECHAT.md)列出了独立 Python 环境、helper 固定源码、客户端指纹及本地模型要求。仓库不携带微信 DLL、模型权重、登录状态或预绑定账号；仅克隆源码不能直接操作微信。

已有配置可运行 `.runtime/wechat-native/.venv/Scripts/python.exe -X utf8 scripts/start_wechat_desktop.py`，默认打开 AstraChat。浏览器版可双击 `启动控制台预览.cmd`。停止按钮与关闭桌面启动器会走既有的服务关闭流程；退出浏览器标签页不停止机器人。

## 功能边界

- 定时任务主动发送目前只接桌面微信；预览不发送消息。
- 搜索 / 生图模型字段可以保存，但独立模型路由仍标记“待接入”。自动摘要、自动主备回退、远程角色导入及若干灰色参考控件尚未实现。
- 旧模拟控制台已移除；浏览器仅保存主题偏好，并兼容旧版主题值。
- 当前测试覆盖离线协议、存储、配置、调度、生命周期与前端数据处理；不替代真实客户端收发、长时间稳定性或语音接收验收。

源码目录：`plugins/astrbot_plugin_roleplay_gateway` 为插件，`scripts` 为部署与桥接，`console-ui` 为控制台，`tests` 为回归测试。清理结果和保留原因见[审查记录](docs/CLEANUP-2026-10-30.md)，第三方来源见[THIRD_PARTY.md](THIRD_PARTY.md)。本机历史操作笔记、OpenSpec 记录和截图报告保留在原目录，但不进入首次发布。
