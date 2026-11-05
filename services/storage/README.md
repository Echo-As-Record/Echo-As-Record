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
- [P1 持久化服务工程指南（已按本分支实际实现修订；提交记录为准，未合并）](ai-docs/persistent-service-plan.md)

## 当前状态（功能分支 `storage/implementation-preparation`，未合并、未经合并前审核）

- **P0**：内存实现（`libs/ear_storage_client/`）已含 P0a 回归修复及 P0b 字段依赖、按能力强制登记、批末新鲜度与无环检查。核心类型的强制登记能力全部关闭（false），需③④提供实际输入、迁移验证后按类型启用。
- **P1（本分支实现，提交记录为准，未合并）**：本目录新增服务包 `ear-storage`（`src/ear_storage/`）：PostgreSQL 后端（显式迁移到 schema revision 3）、可信 token 身份与全部读路径授权、短事务全局写闸、持久 Outbox 行、请求 ID 幂等重放、有界持久 snapshot 租约、FastAPI `/v1` HTTP 入口和部署 CLI；SDK 侧新增同步 `RemoteStorageClient`。详见 [progress](ai-docs/progress.md)。
- **未做**：Outbox Relay、JetStream 与远程订阅（P2）；动态持久注册、凭据生命周期、①管理反馈与⑥管理页（P3）；③④⑤⑦业务接口对齐（P4）；插件历史交接与多层集成（P5）；部署 Compose 与运行环境（与 I 协作）。

部署凭据只由部署环境内部的 `AuthOperator` 签发，它不是日常管理入口，不能替代 P3 的管理反馈流程。CLI 只读显式环境变量 `EAR_STORAGE_DSN`，不读 `.env`，`serve` 不自动迁移。本入口不将已采纳设计描述为已实现能力。
