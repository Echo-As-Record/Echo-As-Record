---
audience: human
doc-type: guide
---

# macOS 常驻模板

`adapter.plist` 是占位模板，不是本机配置，不自动安装或重启服务。先在独立克隆运行 `uv sync --frozen`，把模板中的仓库、配置、日志路径改成部署目录的绝对路径。私有 JSON 配置权限设为 0600；内容为环境变量名到字符串的映射，仅允许 `EAR_ADAPTER_` 前缀。配置不提交仓库。

必需配置：`EAR_ADAPTER_EAR_URL`、`EAR_ADAPTER_DATA_ROOT`、`EAR_ADAPTER_STORAGE_URL`；凭据可直接设 `EAR_ADAPTER_EAR_TOKEN`、`EAR_ADAPTER_STORAGE_TOKEN`，也可通过 `EAR_ADAPTER_GATEWAY_FILE`（读取 `service_tokens.adapter`）和 `EAR_ADAPTER_STORAGE_TOKEN_FILE` 读取独立文件。迟到 Context 使用 `EAR_ADAPTER_DELIVERY_DB`。这替代仓库外的 adapter.py 包装器，不猜测个人目录、端口或令牌位置。

先执行 `uv run python deploy/adapter.py --config /PRIVATE/CONFIG/adapter.json once`，核对①中的结果后，再由部署者安装模板。常驻间隔在模板参数中设置。生产切换还必须让接收端、识别端和②指向同一 inbox；本次分支整理没有操作任何 LaunchAgent。

`deploy/legacy_ear.py --config <已有模型配置> --service-dir <目标服务数据目录>` 只准备兼容识别实例。目标必须与外部旧 Ear 的 `OCTOPUS_DATA_HOME/local/service/<该源码的服务身份>` 一致；旧 Ear 仍把服务身份定义在自己的源码中，本仓库不能保证任意目录能被它识别。该兼容依赖待自有识别实现替代，尚无删除日期。
