# 第三方组件与资源

本仓库包含项目自有的协调插件、控制台和部署脚本；第三方项目及资源仍遵循各自许可。

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：机器人框架；Dockerfile 固定版本和镜像摘要。
- [SnowLuma](https://github.com/MotricSeven/SnowLuma) 与 [NapCat](https://github.com/NapNeko/NapCatQQ)：QQ 接入，镜像由 Compose 引用。
- [helper](https://github.com/Prometheus-first/helper)：Windows 微信原生桥接源码与 DLL。源码在本机 `work/helper-native-upstream` / `work/helper-native-voice-upstream` 单独获取，不随本仓库打包。请查看上游许可及第三方声明；不能将其非商业自用条款理解为任意商业授权。
- [IndexTTS](https://github.com/index-tts/index-tts) 与 [FunASR](https://github.com/modelscope/FunASR)：可选本地语音组件，运行环境与模型权重独立安装，不随仓库分发。
- React、Ant Design、Vite 等前端组件：版本在 `console-ui/package-lock.json`，许可见对应发行包。
- `console-ui/public/kouri-logo.avif` 来自 [KouriChat 公开资源](https://static.kourichat.com/pic/KouriChat.avif)，`astrchat-icon.ico` 由该头像制作。沿用项目既有品牌资源；其权利属于原权利方，本仓库不为其另行授予许可。

仓库不包含账号凭据、聊天记录、用户角色原件、模型权重或微信 DLL。
