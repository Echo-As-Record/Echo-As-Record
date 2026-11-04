---
audience: ai
doc-type: guide
based-on: ADR-0001（独立服务、PostgreSQL 同库多 schema、JetStream+Outbox/Relay，accepted）；设计 v1.2；P0 契约提案（accepted）；storage/implementation-preparation 分支 P0a 提交 a6a1c5a 与其后的 P0b 实现（提交以分支历史为准）；项目负责人授权“采用推荐方案，在功能分支持续实现并分批提交、推送和提 PR，合并前由负责人兼 I 审核”；PostgreSQL 17 显式锁定、psycopg 3 事务、NATS JetStream、FastAPI 并发的官方文档（见第 10 节）
---

# ① P1 持久化服务：工程方案

本页是 P1 的**工程实施指南**，落实 [ADR-0001](../../../docs/adr/0001-storage-and-runtime-modules.md) 已采纳的架构，覆盖[实施准备](implementation-plan.md)第 5 节要求在 P1 前写出的三项设计（请求 ID 与提交结果未知、远程 snapshot 与分页、可信身份与凭据）。它不是新的产品决策，不改变已采纳的行为，因此不再单独请求产品确认；随 P1 实现 PR 一并接受合并前审核。**P1 尚未实现**：本页的表名、路由、参数均为推荐，以实际代码和 PR 为准，不代表服务已可用。

## 1. 技术选择（工程决定，不是产品承诺）

| 项 | 选择 | 说明 |
|---|---|---|
| 数据库 | PostgreSQL 17；本地测试用已有的 `postgres:17-alpine` 镜像 | 部署形态（Compose、卷、备份）仍由 I 负责 |
| 驱动 | psycopg 3，**同步** API + 连接池 | 与现有同步 `StorageClient` Protocol 一致 |
| 传输 | HTTP + JSON，路径带版本 `/v1/...`，FastAPI 实现 | 路由函数用普通 `def`（阻塞 DB 驱动在线程池执行）；线程池与连接池大小一起配置 |
| SDK | 新增同步远程客户端，实现现有 `StorageClient` / `Reader` Protocol | 保留 `MemoryStorage` 作为参考实现与共享测试后端 |
| 版本 | 精确版本由 `uv.lock` 锁定 | 不在文档中写“最新版”，锁定版本不是对外承诺 |

## 2. 接口形态（推荐，给实现者）

- 写：`POST /v1/batches`，body 为 `{request_id, context, ops[]}`，ops 与 `ops.py` 一一对应（含 `DependencyInput` 的 `ref` + `fields`）；返回 `WriteResult` 的 JSON 形式。冲突是 200 + `conflict`，不是 HTTP 错误；`InvalidRequest` → 422，未认证 → 401，`PermissionDenied` → 403。
- 读：`GET /v1/records/{id}`、`.../versions/{n}`、`.../history`；`POST /v1/records:get_many`；`GET /v1/system-records/{type}/{key}`；`POST /v1/query`、`/v1/count`；`GET /v1/dependencies/{id}`（含 `dependency_set` 的“从未登记/空集”区别）、`/v1/dependents/{id}`；`POST /v1/stale-dependencies`；`GET /v1/processed/{consumer}/{event_id}`。
- snapshot：`POST /v1/snapshots` 创建租约，读请求带 `snapshot_id`；`DELETE /v1/snapshots/{id}` 提前释放。
- 订阅：P1 **不提供远程 subscribe**。远程客户端调用 `subscribe` 必须明确报错（不是静默无事件），P2 再实现。
- 错误体统一 `{code, message, details}`；`details` 先经授权过滤（第 3 节）。未知字段和未知冲突原因按“只增不改义”处理。

## 3. 可信身份与读授权

- 凭据：≥256 bit 随机 bearer token，只把 SHA-256 哈希存 DB，映射到模块 principal；明文只在签发时出现一次，不写日志。高熵随机值无需慢哈希。
- 签发：P1 只有部署受控的 bootstrap 命令（如 `ear-storage admin issue-token --module X`，在部署环境执行）；token 轮换、撤销 UI、动态模块的凭据生命周期属于 P3。
- 请求中的模块名一律不采信；principal 只来自 token。**每个请求**都重新查 principal 状态和授权（P1 不做跨请求缓存），撤权对下一个请求立即生效。
- 写授权沿用单写者：principal 必须是目标类型的 writer。
- 读授权覆盖**全部** Reader 路径：get/get_version/history/get_many/system record/query/count/dependencies/`dependency_set`/dependents/stale/is_processed/snapshot 内的每次读取。侧通道同样检查：`where`、`contains`、`unreferenced_by=(type, field)` 中出现的类型都需读权限；`count` 与 query 同权；反向依赖、过时依赖只返回调用方可读的条目。
- 无权读取与不存在对外不可区分；冲突 `details`（如 `dependency_cycle` 的路径、唯一键占用者）对不可读节点脱敏（省略或只给数量）。
- 核心/插件来源：内置核心配置由服务启动时作为可信来源写入 DB 登记表；插件类型只经①自己的注册流程写入（P3 完整化）。spec 中的自报字段不采信。**当前代码中的 `Registry.extend` / `extend_core` 只是构造时的静态登记表，不是运行时动态注册或鉴权**。

## 4. 写事务与并发

- 每个写批次：一个短事务，`READ COMMITTED`，**第一条语句**执行 `SELECT pg_advisory_xact_lock(<固定全库常量>)` 作为全局 writer gate；所有写路径（批次、系统记录、消费标记、重判发布、bootstrap 登记、迁移中的数据改动）都走同一个 gate。首版先保证正确，吞吐优化以后再做。全局 gate 是本指南的工程选择，不是产品承诺，有并发证明后可以修改；无论怎么改，都不得靠跨请求或无界的数据库事务、锁来换取一致性。
- 必须在拿到锁之后再读校验所需的数据：`READ COMMITTED` 每条语句取新快照，锁后读取能看到此前所有已提交写入；不要改用 `REPEATABLE READ`/`SERIALIZABLE` 写事务，否则快照可能在等锁之前就已取得。
- 事务级 advisory 锁在提交或回滚时自动释放，无需显式解锁；连接池归还前确认事务已结束。
- 事务内只做校验与写入，**不在事务中等待模型、外部 HTTP 或用户输入**；锁等待设 `lock_timeout`，超时作为可重试错误返回。
- 每次提交在 gate 下分配单调 `commit_seq`（存于 meta），用于 Outbox 排序与依赖边的有效区间；snapshot 租约只把它记为创建时的参考值，不据此读取旧状态（见第 7 节）。它是本库提交顺序，不推广为跨系统的全局顺序。
- 批末检查（完整登记、新鲜度、无环、核心不依赖插件）在同一事务内、gate 下执行，逻辑与内存实现一致。成环检查只需从本批新边出发查可达性（前提是现有当前图无环，见第 8 节）。

## 5. 数据布局

- 不一类型一表，也**不把 `MemoryStorage` 的私有完整状态序列化成单行 JSON blob** 作为生产后端。
- 每个模块一个 schema，内含通用的 `versions`（semantic/rule，按 `data_type` 列区分）、`raw_records`、`system_records`（含 revision 历史）。
- `storage` schema 放公共设施：`identity`（id → 模块/类型/当前版本）、唯一字段与互斥成员索引、时间范围索引、`dependency_sets` / `dependency_edges`（含 `fields`、`commit_seq` 有效区间）、`processed`、`outbox`、`idempotency`、`snapshot_leases` / `snapshot_items`（第 7 节）、`principals`、`registry`、`meta`（`schema_revision`、`commit_seq`）。
- payload 以 `canonical_json` 产出的**文本**为准（`text` 列）。可另存 `jsonb` 仅供查询索引；“内容无变化”、字段新鲜度等比较都在 Python 中对规范文本/解码值进行，不依赖 `jsonb` 相等（`jsonb` 会让 `1` 与 `1.0` 比较相等、重排键，改变现有语义）。
- 逻辑复用：把内存实现中的纯逻辑（fields 规范化与合并、`_stale` 规则、完整登记判断、成环算法、登记表校验）抽成两种后端共享的引擎函数；服务不 pickle 内存状态，也不通过内存对象转存。

## 6. 请求 ID 与提交结果未知

- SDK 为每次 `write()` 生成 UUIDv7 `request_id`，传输层重试复用同一 ID；`write_with_retry` 的重新计算是新计划，用新 ID。
- 作用域为 `(principal, request_id)`；存规范化请求的哈希。同 ID 同哈希 → 返回已存结果；同 ID 不同哈希 → 422（ID 复用错误），不执行。
- 结果（成功或冲突）与业务写入**在同一事务**写入 `idempotency`；`InvalidRequest`/鉴权失败不落库。客户端超时、连接断开后用同 ID 重放，即可得知是否已提交，不会重复执行。
- 返回已存结果前仍按**当前**凭据与权限复核；principal 已撤销则拒绝，不泄露旧结果。保留期可配置；过期后同 ID 视为新请求，文档与 SDK 须说明重放窗口。

## 7. 远程 snapshot 与分页

- 首版选择（工程选择）：snapshot 是**有界的物化副本**，不是“只记 watermark、按水位读旧状态”。第 5 节的布局撑不起后者：`commit_seq` 有效区间只给了 `dependency_edges`；`system_records` 只说明保留 revision 历史，没有可按水位读取的区间；`identity`、唯一/成员索引、时间范围索引、`processed` 只保存当前状态。仅凭 watermark 读不出旧时刻的系统记录、索引命中和消费标记。真正的 as-of 读取须另行设计并为全部相关表加提交区间，本页不承诺。
- 创建：在一个**短的、有界的** `REPEATABLE READ READ ONLY` 事务内取得一致视图（设 `statement_timeout`、`lock_timeout`；读取不需要全局写闸），按创建者**当时的读授权**把其可读数据有界物化到持久租约表 `snapshot_items`（或等价的安全物化方式；每行含 `snapshot_id`、种类、键、规范文本），并写租约行 `(snapshot_id, principal, commit_seq, expires_at, item_count)`，`commit_seq` 仅作参考。事务提交即结束；不保持跨请求的数据库事务，也不靠长事务维持快照。
- 上限：每个 snapshot 的行数与总字节数、每个 principal 的活跃租约数都有上限；超限则物化事务回滚并明确报错，**不静默截断，也不返回部分视图**。P1 首版不改 `snapshot()` 签名；是否增加范围参数来缩小物化范围，另议。
- 读取：snapshot 读请求只读 `snapshot_items`；query/count 在物化项上求值，语义与内存实现一致（由共享契约测试保证），游标指向 `(snapshot_id, 排序键)`，翻页稳定。**每次读取**都重新校验：凭据对应的 principal 当前有效；租约绑定该 principal，其他 principal 使用即拒绝且与不存在不可区分；租约未过 TTL（到期即拒，不等清理任务）；并按该 principal **当前**的读权限对返回内容重新鉴权与脱敏，撤权后不能继续读出旧视图。物化只会限定可见范围，不会放大授权。
- 清理：TTL 到期后由清理任务分批、用小事务删除租约及其 `snapshot_items`。snapshot 不依赖旧历史，不阻止常规的历史保留或清理。
- 非 snapshot 的 query 分页沿用“跨页不保证一致”的现有说明。

## 8. 迁移、Outbox 与能力启用

- `meta.schema_revision` 记录版本；每个迁移在一个事务中执行；由显式 `migrate` 命令应用。服务启动只校验版本，不匹配即拒绝启动；任何路径都不自动 drop / 重建数据库。
- 新库从空图开始；若将来导入已有数据，导入后先对当前图做全量环校验，遗留环显式报告、不自动删边（P0 提案 2.11）。
- Outbox：P1 在写事务内原子写入 Outbox 行（与数据、依赖、消费标记同批生效）。Relay 与 JetStream 持久消费者属于 P2；JetStream 提供至少一次投递，不宣称恰好一次，消费端靠“先查当前状态”和 `MarkProcessed` 去重。
- 能力项：核心类型的 `requires_dependencies` / `supports_reevaluation` 当前全为 false。服务不得自行打开；须在③④等调用方完成迁移（同批完整登记）并经业务测试验证后，按类型逐个启用。

## 9. 测试与验收

- 环境：本机 docker 可用、`docker compose` 不可用，已有 `postgres:17-alpine` 镜像。测试夹具用 `docker run --rm` 启动：随机标签（如 `ear-test=<uuid>`）、`-p 127.0.0.1::5432` 随机回环端口、`--tmpfs` 数据目录、限制内存/CPU、随机密码；清理只删除带本次标签的资源。不动已有容器和用户数据库，不需要 sudo，不全局安装，不自动拉取镜像。
- 依赖：在 `pyproject.toml`/`uv.lock` 中新增服务包（加入 uv workspace）及其依赖；pyright `include` 与 pytest `testpaths` 覆盖 `services/storage`。
- 开关：PostgreSQL 集成测试用 marker 与环境变量启用；本地缺环境时**明确 skip 并给出原因**，报告必须写明 skip 数量，不得称“全部通过”。CI 用 PostgreSQL 服务容器强制运行这些测试。
- 共享契约测试：同一组用例跑 `MemoryStorage` 与 PostgreSQL 后端，包括 P0b 的字段真值表（`1`/`1.0`、`True`/`1`、缺失/null、数组顺序）。
- P1 必测：多连接、多进程的真实并发（同版本竞争、同去重键并发新建、相反方向并发加边、含重复节点和长链的环、计算期间输入更新）；提交后断开连接的结果未知与同 ID 重放；服务重启后身份、登记表、幂等记录与 `commit_seq` 延续；撤权后的下一请求、snapshot 读与已存结果重放都被拒；snapshot 创建后的写入不可见、超限报错而非截断、TTL 到期即拒、跨 principal 使用被拒；所有读路径与侧通道的授权与脱敏；迁移版本不匹配时拒绝启动。

## 10. 不在 P1 / 参考资料

- 不在 P1：远程订阅与 Relay（P2）；动态注册、凭据生命周期、管理反馈（P3，当前内存 `extend` 不等于动态注册）；④规则集合并发守护、结果记录所属方、多 cause（P4 接口队列，仍属 MVP，不因延后而降级）。
- 参考（实现参考，非版本承诺）：
  - PostgreSQL 17 显式锁定（事务级 advisory 锁的释放、锁后快照）：https://www.postgresql.org/docs/17/explicit-locking.html
  - psycopg 3 事务管理（autocommit 与 `transaction()` 上下文）：https://www.psycopg.org/psycopg3/docs/basic/transactions.html
  - NATS JetStream（持久 consumer、至少一次）：https://docs.nats.io/concepts/jetstream
  - FastAPI 并发与 `def` 路由：https://fastapi.tiangolo.com/async/
