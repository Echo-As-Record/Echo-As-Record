---
audience: human
doc-type: reference
---

# ② 适配器

把外部数据转成统一输入格式写入 ①，只做确定计算，不做推断。MVP 的录音、转写、局部说话人、声纹、环境声、GPS 和手机信息，全部来自旧 Ear 的 Evidence 只读 API：旧 Ear 照常从 Brain inbox 接收文件并跑语音、声纹、环境声模型，② 读取它的结果，不改旧 Ear 的处理逻辑（设计 3.4「封装、不重写」）。

- [职责与验收](../../docs/design/work-breakdown-v1.2.md)（② 适配器一节）
- [数据流、导入规则与验收方法](ai-docs/implementation.md)
- [各类型 payload 字段](ai-docs/payloads.md)，供 ③④ 使用

当前状态：转换、导入规则、远程写入和常驻扫描已实现。`ear-adapter dry-run` 使用内存并模拟重启重扫（兼容 `--dry-run`）；`ear-adapter once` 向①导入一轮；`ear-adapter run --interval 60` 常驻扫描，SIGTERM/SIGINT 在当前录音处理结束后退出。①临时不可用、提交结果未知均留待下轮，配置/授权错误退出非零。每段录音保持一个原子批次，超限不会拆批。

配置只读环境变量：`EAR_ADAPTER_EAR_URL`、`EAR_ADAPTER_EAR_TOKEN`、可选 `EAR_ADAPTER_DATA_ROOT`（默认 `~/.octopus/local`）；远程模式另需 `EAR_ADAPTER_STORAGE_URL`、`EAR_ADAPTER_STORAGE_TOKEN`。凭据不进入命令参数和日志，保存在被忽略的 `deploy/.env`。

②身份为 `adapter`，写权限由登记表 `writer=adapter` 决定；必须显式授予 `read_types=["audio_ref", "local_speaker", "voiceprint", "transcript_segment", "ambient_sound", "gps_point", "phone_info"]`，不需要订阅。每轮扫描先验证全部七种读权限，无授权不会被当作尚未导入。①当前 `init-identities` 只签发 api 管理员和 manage-worker，**没有 adapter 身份的签发流程**；正式部署由①负责人 / I 决定，本地测试用部署内 `AuthOperator.issue_principal("adapter", read_types=[...])`，不修改①部署脚本。

真实①联调测试已加入 PostgreSQL/NATS 双门控，2026-11-11 本机运行通过；同日真实旧 Ear 数据经 `Runner` 写入测试夹具起的临时①：384 段导入、重扫无重复、4008 个声纹事件的引用全部可读（[实现说明 7.3](ai-docs/implementation.md)）。Compose 临时部署上用命令行跑真实数据、常驻模式的掉线恢复尚未做，也不是生产验收。③④⑤仍未完整业务联调。旧 Ear 的列录音接口历史状态与 2026-11-05 内存试运行结果也保留在实现说明中。
