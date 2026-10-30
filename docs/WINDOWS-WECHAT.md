# Windows 微信依赖

桌面控制台与旧 Tk 启动窗口共用同一后台会话管理。旧窗口仍有显式启动入口和退出处理，不能作为死模块删除。`wechat_desktop_probe.py` 是早期桌面探测工具，与当前原生桥接不同，保留用于诊断。

## 独立环境

```powershell
uv venv --python 3.12 .runtime/wechat-native/.venv
uv pip install --python .runtime/wechat-native/.venv/Scripts/python.exe -r requirements-wechat-native.txt -r requirements-console.txt
```

原生窗口需要 WebView2。开发控制台需要先在 `console-ui` 执行 `npm ci` 和 `npm run build`。微信桥接额外需要兼容的 Windows 微信客户端、合法取得的 helper 源码及对应 DLL；版本和指纹验证在 `scripts/wechat_native_transport.py` 中。

## 固定上游

上游为 https://github.com/Prometheus-first/helper.git ，分别在以下目录保留完整 Git 历史、许可和第三方声明：

| 本机目录 | 固定提交 | 用途 |
|---|---|---|
| `work/helper-native-upstream` | `a81c495770b397646ba0661118927c83589c41fc` | 文本桥接与早期探测 |
| `work/helper-native-voice-upstream` | `233c29ca061ac03e9ef4717ade8712dab941d1f7` | 带语音能力的桥接 |

`work` 不进入 Git，但这两个子目录是运行依赖，不属于可随意清空的缓存。按上游说明取得 DLL 后，必须通过源码完整性、DLL 哈希及客户端核心指纹检查。不要修改哈希来绕过失败；不兼容的微信版本需重新适配。

`config/wechat-native.example.json` 的本地副本默认不授权原生试验。已有用户使用自己的绑定配置；新部署需按实际账号和允许的群/私聊完成检查与绑定，不能复制他人的 `expected_self` 或会话 ID。`scripts/configure_native_wechat.py` 提供预览及显式 `--apply`，要求相应探测证据。

## 本地语音

语音为可选组件，需要独立的 IndexTTS / FunASR Python 环境和本地模型目录，配置格式见 `config/wechat-voice.example.json`。IndexTTS 工作进程设置离线缓存，并在引入模型前初始化 Hugging Face 缓存常量；这类看似未使用的导入实际影响初始化顺序，应保留。

代码包含语音意图、合成、发送及接收接口，但可调用能力仍取决于 helper 和客户端。离线测试通过不代表已完成真实语音收发验收。
