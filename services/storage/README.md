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

正式服务、数据库与总线尚未落地；现有联调实现位于 `libs/ear_storage_client/`。本入口不将已采纳设计描述为已实现能力。
