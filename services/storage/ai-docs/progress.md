---
generated-by: Claude (AI)
generated-at: 2026-11-01
based-on: docs/design/design-v1.1.md (reviewed-at 2026-10-30), docs/design/signals-supplement.md (reviewed-at 2026-10-29), docs/glossary.md, ai-docs/proposals/module-internal-structure.md, commit ada0814
---

# ① 存储与事件：进度与已采用的默认决定（未审定）

## 已完成（分支 storage/contracts-and-sdk）

- contracts：semantic-record、system-record、dependency-record、registry/data-types（含 schema 与正反例）；data-changed 新增可选字段 writer、batch_id。
- libs/ear_contracts：与 contracts 一一对应的 pydantic 类型、UUIDv7 生成、UTC 时间、登记表；一致性测试。
- libs/ear_storage_client：Context、写入操作（ops.py）。

## 未完成（按优先级）

1. results.py（Outcome、ConflictReason、OpResult、Conflict、WriteResult）、errors.py、queries.py（Query、TimeRange、Referrer、Page、StaleDependency）、protocol.py（Reader、StorageClient）。
2. memory.py：写时复制的内存实现 + 进程内总线（run_until_idle、重复投递模拟、死信）；testing.py 的造数据工具。
3. 覆盖工作分解 ① 五条验收的测试；接口说明 v0.2（本目录）；ADR-0001 草稿。
4. 之后：PostgreSQL + outbox + relay + 总线，compose。

## 默认决定（待负责人确认）

- 整体替代 = 新增 status=superseded 的版本 n+1（内容同 n），事件 operation=superseded、new_version=n+1、old_version=n。
- 一批写入原子提交；冲突（version_mismatch、object_superseded、id_exists、dedup_key_exists、unique_field_taken、member_taken、stale_dependency、revision_mismatch、already_processed）整批拒绝、返回结果而非异常；权限、格式错误抛异常。
- 原始数据去重键已存在 → deduplicated（成功、不发事件）；语义对象 dedup_key 已存在 → 冲突。
- skip_history 按每条写入设置；root_feedback_id、depth 按整批设置（Context）。
- 登记依赖时被依赖方必须是当前版本，否则 stale_dependency（防止 ⑤ 已处理完修改事件后才登记过时依赖而漏查）。重新判断结果不变时，要用新版本重新登记依赖。
- 归属关系 payload 需有 subject_id（唯一、复制时间、「未归属」查询）；人物/地点 references 互斥。字段名待 ③ 确认。
- 客户端接口为同步 API。
