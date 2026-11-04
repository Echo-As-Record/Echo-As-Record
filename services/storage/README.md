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
- [①实施准备与 P0 拆分](ai-docs/implementation-plan.md)；[P0 契约提案（accepted；P0b 已在功能分支实现，待合并前审核）](../../docs/proposals/storage-p0-contracts.md)
- [P1 持久化服务工程方案（计划，未实现）](ai-docs/persistent-service-plan.md)

正式服务、数据库与总线尚未落地；现有联调实现是 `libs/ear_storage_client/` 中的内存实现（已含 P0a 修复和 P0b 字段依赖、按能力强制登记、批末新鲜度与无环检查）。内存实现中的模块名由调用方自报，核心/插件区别来自构造时传入的静态可信登记表，二者都不是运行时可信身份或动态注册；核心类型的强制登记能力目前全部关闭。本入口不将已采纳设计描述为已实现能力。
