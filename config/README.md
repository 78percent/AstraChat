# 配置样例

复制所需 `*.example.json` 为 `*.local.json` 后填写自己的值。`.local.json`、运行时凭据和聊天数据均不进入 Git。

- `connection-inputs`：首次 QQ 部署的账号名单与人设文件 SHA256，交给 `prepare_local.py`，不能直接导入 AstrBot。
- `model-access`：DeepSeek 官方接口资料，先探测再配置。空 API Key 不可用。
- `wechat-native` / `wechat-desktop`：独立微信试验配置，默认未授权；保持固定源码版本及真实客户端核验。
- `wechat-voice` / `indextts`：可选本地模型与参考音频路径，示例盘符需要替换。

已有配置不应被示例覆盖。当前使用的 API Key、账户身份和导入人设只保存在本机，不从仓库推定任何真实连接状态。
