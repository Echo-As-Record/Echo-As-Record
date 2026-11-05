---
audience: human
doc-type: reference
---

# ① 存储与事件

独立存储服务，负责统一读写、版本、去重、依赖与权限、事件基础设施及动态模块管理。只消费自身管理反馈，不承担归属、提取或传播调度业务。

- [职责与接口目标](../../docs/modules/storage.md)
- [本轮已确认决定](../../docs/decisions/mvp-confirmed-decisions.md)
- [动态插件 MVP](../../docs/design/plugins-mvp.md)
- [SDK 当前用法](ai-docs/interface.md)
- [实现状态](ai-docs/progress.md)及[全局实施计划](../../ai-docs/plans/mvp-implementation.md)
- [①实施准备与 P0 拆分](ai-docs/implementation-plan.md)；[P0 契约提案（accepted；P0a a6a1c5a、P0b cf337d1 已提交，待合并前审核）](../../docs/proposals/storage-p0-contracts.md)
- [P1 持久化服务与 P2 Relay 工程指南（P1 已提交 d7c9919；P2/P3 后端已提交 d0d8875，消费者、管理与订阅重配置以 progress 为准）](ai-docs/persistent-service-plan.md)

## 当前状态（功能分支 `storage/implementation-preparation`，PR8，未合并、未经合并前审核）

- **P0**：内存实现（`libs/ear_storage_client/`）含 P0a 回归修复及 P0b 字段依赖、按能力强制登记、批末新鲜度与无环检查。核心类型的强制登记能力全部关闭（false），需③④提供实际输入、迁移验证后按类型启用。
- **P1（提交 d7c9919）**：服务包 `ear-storage`：PostgreSQL 后端、可信 token 身份与全部读路径授权、短事务全局写闸、持久 Outbox、请求 ID 幂等重放、有界持久 snapshot、FastAPI `/v1` HTTP 入口和部署 CLI；SDK 侧同步 `RemoteStorageClient`。
- **P2（已提交 d0d8875，PR8 未合并；完成时 schema revision 5）**：有序 Outbox Relay → JetStream；服务端授权的 JetStream 持久 pull 消费者，由服务经 HTTP（`/v1/consumers/*`）代理；SDK `subscribe` / `poll_once` / `run_until_idle` 有界泵。按读与订阅授权过滤 `data_changed`，`reevaluation_requested` 只投给该类型所属且支持重判的模块。数据库租约与全局 `queue_seq` 定序，ack/重试退避/死信/同 consumer 重放；租约过期确定返回 `DeliveryExpired`（410）。语义是有限去重的**至少一次**，不是恰好一次。注册默认只收未来事件。真实 PostgreSQL + 隔离 NATS 完整测试 802 项在 P2 工作区快照（P3 改动前）上两次通过。
- **P3 后端（已提交 d0d8875；schema revision 8；⑥管理页未做）**：管理反馈类型 `plugin_management_feedback`（`api` 写）与结果 `plugin_management_result`（①写）；可信管理员角色绑定在身份上；运行时登记表与动态 namespace；启用待资源就绪后生效，停用关闭授权并防复权；`retry_task` 恢复失败任务；凭据经管理反馈 `rotate_credentials` / `revoke_credentials` 与一次性 claim（仅原提交者且仍为 active 管理员、300 秒内、epoch 与 principal 不变；明文只在响应中返回，库中只存 SHA-256；响应丢失须提交新的 rotate 反馈，不自动恢复授权或启用模块）；订阅完整重配置（schema 8）：新增类型只从切换时按类型记录的提交 watermark 之后投递、原有积压保留；多 consumer 在数据库事务外准备资源、最终在数据库事务内原子切换；已撤销/停用的身份可准备资源但不复活 token 或授权；owner/授权/凭据 epoch 竞态会使准备失效；退役代的 ack 在已无权限时保持待清理，不阻塞当前已授权的新投递，普通 ack 仍按当前授权复核。验证：merge 前 P3 检查点（d0d8875）真实 PostgreSQL + NATS 完整 916 passed、0 skipped；b7307e9 合并 dev 后只跑默认 657 passed / 298 skipped，b7307e9 本身未做完整真实运行（合并后的完整真实运行见下方 106cd2e）。更早的 103 / 216 项目标组快照见 progress。
- **P4 集合守护（已提交 f747e89，schema revision 9；不是完整 P4）**：`Reader.type_revision(data_type) -> int` 与提交期操作 `AssertTypeRevision(data_type, expected_revision)`，冲突为可重试的 `collection_changed`；查询与 revision 须取自同一 snapshot。只有实际业务变更使 revision 增加；守护不产生事件或依赖边，不扫描历史；旧 snapshot 缺 revision 元数据时明确失败。规则与验证见 [progress](ai-docs/progress.md)。
- **P4 重判完成记录（已提交 106cd2e）**：按所属方验证的完成记录与权威查询；完成协议以①权威索引验真，只有登记表显式声明 `reevaluation_result` 能力的类型受保护，普通名称后缀不自动受保护，旧同名类型兼容。④的入队 marker 不是业务完成，④业务未改。真实 PostgreSQL + NATS 完整运行 1091 passed、0 skipped（`/tmp/ear-p4-completion-full.log`，EXIT=0）。
- **P4 多起因（已提交 daa767f 并推送，基于 106cd2e）**：保留 `cause` / `root` / `depth` 原含义；新增 `additional_causes`，每项（`TracedCause`）仅含 `cause` / `root_feedback_id` / `depth`，不带请求 ID；`request_event_id` 在 Context、语义记录与 `DataChanged` 上，`ReevaluationRequested.trigger_request_event_id` 指向上游请求，关联须指向真实已提交请求、不可自循环；执行中新增来源须在 Context 预合并并与 Finish 一致；逐 origin 复核读权限（含历史、结果、消费者与旧消费者代的待确认消息）。真实 PostgreSQL + NATS 完整运行 1168 passed、5 skipped（`/tmp/ear-p4-causality-full.log` 内 EXIT=0，`/tmp/ear-p4-causality-full.exit` 为 0；5 个 skip 均为 memory 参数不适用认证路径，明细见 progress）。
- **P4 ⑦调用记录存储（本分支已实现，基于 daa767f；提交状态以提交历史为准）**：①为⑦网关提供调用记录的存储协议：系统记录类型 `llm_call` 只能由 `llm_gateway` 以 `PutSystemRecord` 为每个逻辑调用单写一次终态（`success` / `failed`），写后不可替换，系统记录不发事件；`caller_module` / `model` 是⑦的断言，不是①对远端调用方或模型的认证证明。请求关联须指向真实已提交请求并保存其全部起因；当前、历史、查询、计数与 snapshot 读取在 `llm_call` 授权之外逐一复核全部起因类型的读权限；按调用方或根反馈用现有 `Query(where=...)` / `count`。默认只存 metadata；正文 `request_text` / `response_text`（严格字符串，空串也算提供）须部署方经 `ModelBodyPolicy`（构造参数或 `serve` 环境变量）显式开启：默认关闭、两项累计 UTF-8 上限 64 KiB、`allowed_callers=None` 允许全部调用方、空元组全部拒绝，请求不能自行开启；关闭后既有正文仍按原类型/起因 ACL 可读，不删除，也没有 TTL。读取按类型授权，没有逐字段 ACL。schema revision 保持 10、无 DDL；已有库须显式可信 `bootstrap` 才登记新核心类型 `llm_call`。未改⑦服务，不发起实际模型调用。验证只有默认与定向真实分组（不是完整运行），见 progress。
- **未做**：⑥管理页（P3 余项）；⑦网关服务与实际模型调用、调用正文保留期与清理，以及③④⑤⑦对完成记录、集合守护、多起因与调用记录的业务接入（P4 其余部分；①侧提供存储协议不代表业务对齐完成）；插件历史补/跳任务、多层集成与示例（P5）；生产流保留/清理、部署 Compose 与 PostgreSQL/NATS 运行环境（与 I 协作）；包含调用记录的完整真实运行（尚无结果）、远程 CI 实跑。

已知限制：JetStream 流满时拒收新消息、Outbox 积压并重试，目前没有清理；默认持久化不强制每条 fsync；全局写闸串行化所有操作。部署内 `AuthOperator` 的签发、消费者名预留与旧标记认领都不是日常管理入口，不能替代 P3 管理反馈流程。CLI 只读显式 `EAR_STORAGE_DSN`（relay、manage-worker 另读部署环境变量），不读 `.env`，`serve` 不自动迁移；调用正文策略只由部署方经构造参数或 `serve` 的 `EAR_MODEL_BODY_ENABLED` / `EAR_MODEL_BODY_MAX_BYTES` / `EAR_MODEL_BODY_ALLOWED_CALLERS` 设置，缺省关闭。本入口不将已采纳设计描述为已实现能力。
