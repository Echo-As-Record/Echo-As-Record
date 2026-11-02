---
generated-by: Claude (AI)
generated-at: 2026-11-01
based-on: docs/design/design-v1.1.md (reviewed-at 2026-10-30), docs/design/signals-supplement.md (reviewed-at 2026-10-29), docs/glossary.md, ai-docs/proposals/module-internal-structure.md, 分支 storage/contracts-and-sdk
---

# ① 存储与事件：进度与已采用的默认决定（未审定）

## 已完成（分支 storage/contracts-and-sdk）

- contracts：raw-record、semantic-record、system-record、dependency-record、registry/data-types（含 schema 与正反例）；data-changed 新增可选字段 writer、batch_id。
- libs/ear_contracts：与 contracts 一一对应的 pydantic 类型、UUIDv7、UTC 时间、登记表；一致性测试。
- libs/ear_storage_client：接口（protocol.py）、写入操作、结果与冲突、查询、内存实现与进程内总线、造数据工具；行为测试覆盖工作分解 ① 的五条验收。
- 接口说明：本目录 interface.md。

## 未完成

1. ADR-0001：数据库、总线、outbox、① 是服务还是库。
2. 正式实现：PostgreSQL 表结构、outbox、relay、总线适配；与内存实现共用同一套行为测试。
3. deploy/compose.yaml 加入数据库、总线、storage。

## 默认决定（负责人已于 2026-11-01 认可）

- 整体替代 = 新增 status=superseded 的版本 n+1（内容同 n），事件 operation=superseded、new_version=n+1、old_version=n。
- 一批写入原子提交；冲突整批拒绝并返回结果，权限和格式错误抛异常。
- 原始数据去重键已存在 → deduplicated（成功、不发事件）；语义对象 dedup_key 已存在 → 冲突。
- skip_history 按每条写入设置；root_feedback_id、depth 按整批设置（Context）。
- 登记依赖时被依赖方必须是当前版本，否则 stale_dependency；重新判断结果不变时要重新登记。
- 归属关系 payload 需有 subject_id；人物/地点 references 互斥。字段名待 ③ 确认。
- 客户端接口为同步 API。
