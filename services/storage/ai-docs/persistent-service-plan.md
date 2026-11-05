---
audience: ai
doc-type: guide
based-on: ADR-0001（独立服务、PostgreSQL 同库多 schema、JetStream+Outbox/Relay，accepted）；设计 v1.2；P0 契约提案（accepted）；storage/implementation-preparation 分支 P0a a6a1c5a、P0b cf337d1；本分支实现的 P1 实际代码（提交记录为准，未合并；services/storage/src/ear_storage 的 schema/repository/backend/auth/service/snapshots/reader/http/cli，ear_storage_client 的 engine/wire/remote/remote_errors）与提交前检查日志 /tmp/ear-p1http-full-OK7Aeg.log；项目负责人授权“采用推荐方案，在功能分支持续实现并分批提交、推送和提 PR，合并前由负责人兼 I 审核”；PostgreSQL 17、psycopg 3、NATS JetStream、FastAPI 官方文档（见第 10 节）
---

# ① P1 持久化服务：工程指南（按实际实现修订）

本页原为 P1 的实施方案，现已按**本分支中的实际 P1 代码**修订（提交记录为准，未合并；以分支历史和 PR8 为准）。它落实 [ADR-0001](../../../docs/adr/0001-storage-and-runtime-modules.md)，覆盖[实施准备](implementation-plan.md)第 5 节要求的三项设计（请求 ID 与提交结果未知、远程 snapshot、可信身份与凭据）。它是工程指南，不是新的产品决策；随 P1 PR 接受合并前审核（负责人兼 I）。下文“实际”指当前代码，“未做”指仍属后续阶段。实现状态与测试结果见 [progress](progress.md)。

## 1. 技术选择（工程决定，不是产品承诺）

| 项 | 实际选择 | 说明 |
|---|---|---|
| 数据库 | PostgreSQL 17；测试用已有 `postgres:17-alpine` 镜像的隔离容器 | 部署形态（Compose、卷、备份）仍由 I 负责 |
| 驱动 | psycopg 3，同步 API | 与同步 `StorageClient` Protocol 一致 |
| 传输 | HTTP + JSON，`/v1` 前缀，FastAPI；`serve` 用 uvicorn | 请求体默认上限 8 MiB、读取超时 15 s（`create_app` 参数） |
| SDK | 同步 `RemoteStorageClient`（httpx），实现现有 `StorageClient` / `Reader` Protocol | `MemoryStorage` 保留为参考实现；共享纯逻辑在 `engine.py` |
| 版本 | 精确版本由 `uv.lock` 锁定 | 锁定版本不是对外承诺 |

## 2. 接口形态（实际）

- 路由：`GET /v1/identity`（token 对应的模块/principal 信息）、`GET /v1/registry`、`POST /v1/batches`（写批次，含 `request_id`）、`POST /v1/read`（统一读入口，按 `wire.ReadMethod` 分派，可带 snapshot）、`POST /v1/snapshots`（创建租约）、`DELETE /v1/snapshots/{snapshot_id}`（提前释放）。原方案中按资源拆分的 REST 读路由**未采用**。
- 编码：请求/结果/读参数由 `ear_storage_client.wire` 定义（封闭模型，拒绝未知键，时间必须 UTC）；冲突仍是正常结果中的 `conflict`，不是 HTTP 错误。具体状态码与错误体以 `http.py`、`wire.py` 和 `services/storage/tests/test_http.py` 为准，本页不重复。
- SDK 错误：`AuthenticationError`、`PermissionDenied` / `SnapshotUnavailable`（缺失、过期、他人或失效 principal 的租约同一错误）、`ResourceLimit`、`ServiceUnavailable`、`ProtocolError`、`CommitUnknown`（见第 6 节）。
- 订阅：P1 **不提供远程 subscribe**；`RemoteStorageClient.subscribe` 抛 `NotImplementedError`，不会静默无事件（P2）。
- 服务端派生的 module/registry 只供参考，不是授权能力。

## 3. 可信身份与读授权（实际）

- 凭据：`secrets.token_urlsafe(32)`（256 bit），DB 只存 SHA-256 摘要，绑定一个模块 principal；明文只在签发时返回一次，无取回接口。
- 签发：只有部署环境内部的 `AuthOperator`（`issue_principal`、`revoke`、`set_grants` 整体替换授权）。它**不经任何 token 认证的传输暴露**，也**没有 CLI 或 HTTP 命令**（原方案中的 `admin issue-token` 未实现）；不能替代 P3 的动态注册、凭据生命周期和管理反馈流程。
- 请求中的模块名不采信；principal 只来自 token。每个操作在全局写闸下重新认证并读取授权，撤权在其提交后对之后受理的操作立即生效。
- 授权模型：没有隐式授权（包括所有者读自己的类型）；一行 grant 即读权限，`can_subscribe` 是该行上的附加能力（订阅投递属 P2）。写入需要单写者所有权，**并**对本批实际读到的输入类型（目标、cause、依赖引用、被引用对象等）有读权限，隐式时间来源读取也不得成为存在性预言。
- 读授权覆盖全部 Reader 路径；依赖两端都检查，计数与查询同权，反向/过时依赖只返回可读条目；无权与不存在对外不可区分，错误详情脱敏。
- 核心/插件来源：`bootstrap` 写入受信核心登记表；插件动态注册属 P3。`Registry.extend` / `extend_core` 仍只是构造时的静态登记表。

## 4. 写事务与并发（实际）

- 每个操作一个短事务，`READ COMMITTED`，第一步执行事务级 advisory 锁 `WRITER_GATE`（`schema.py` 常量）；**写批次、普通读取、snapshot 创建/读取和 `AuthOperator` 管理共用同一闸**，取闸后再认证和读取校验数据。首版保正确不保吞吐；任何后续优化都不得靠跨请求或无界事务换一致性。
- 事务内只做校验与写入，不等待模型、外部 HTTP 或用户输入；锁随提交/回滚释放，方法返回后不残留锁。
- 每次提交在闸下分配单调 `commit_seq`（`storage.meta`），用于 Outbox 排序；它不是跨系统全局顺序。
- 批末检查（完整登记、新鲜度、无环、核心不依赖插件）复用 `engine.py` 的共享逻辑，在同一事务、闸下对**有界当前图投影**执行。`repository.Limits` 默认：10 000 节点、50 000 依赖边、32 MiB 投影、每批 2 000 操作、8 MiB 请求；超限抛 `ProjectionLimit`，整批不提交。该成本随当前图规模增长（不随历史版本增长），作为公开已知限制随 PR 审核。

## 5. 数据布局（实际）

- 不一类型一表，不把内存状态序列化为单行 blob。
- `storage` schema 公共表：`meta`（`schema_revision`、`commit_seq`）、`registry`、`publishers`、`modules`、`identity`、`dependency_sets`、`dependency_edges`、`processed`、`published`、`outbox`、`idempotency`、`principals`、`type_grants`、`snapshot_leases`、`snapshot_items`。
- 每个模块一个独立 schema，内含规范化的记录表（由 `bootstrap` 按模块创建）。
- payload 以 `canonical_json` 规范**文本**保存；“无变化”、字段新鲜度等比较在 Python 中对规范文本/解码值进行，不依赖 `jsonb` 相等。

## 6. 请求 ID 与提交结果未知（实际）

- `RemoteStorageClient.write(ops, context, request_id=None)` 默认生成 UUIDv7；SDK 不读环境变量、**没有隐藏重试**。
- 传输中断、服务不可用、回执格式异常时抛 `CommitUnknown(request_id)`：调用方用**同一** `request_id`、ops、context 重放以得知结果；不得自动重新计算。`write_with_retry` 遇到它不重算（有测试）。
- 服务端回执按 principal 作用域保存，与业务写入同一事务；同 ID 同内容返回已存结果（真实 TCP 下响应丢失后重放结果字节一致、不重复发事件）。
- 重放前按**当前**凭据复核，并对原请求的实际输入复核读权限；此前复用旧依赖输入可绕过权限的问题已修并有回归测试（`test_auth.py::test_reused_input_*`）。
- 保留期：P1 **不清理**幂等回执（原方案“保留期可配置”未实现）；保留策略属部署事项，与 I 确定。

## 7. 远程 snapshot（实际）

- snapshot 是**有界物化副本**，不是按 watermark 读旧状态（当前表不支持 as-of 读取）。
- 创建：在全局 `WRITER_GATE` 下的短 `READ COMMITTED` 读写事务中，按创建者当时的读授权把可读数据物化到 `snapshot_items`，写入租约 `snapshot_leases`（含 `commit_seq` 参考值、条目数、字节数、到期时间）后提交。**更正**：原方案写的 `REPEATABLE READ READ ONLY` 且“不需要全局写闸”与实现不符，也不正确（创建需要写租约行）；实际为上述短读写事务。不保持跨请求事务或锁。
- 上限（`SnapshotLimits` 默认）：每个 snapshot 50 000 行、32 MiB；每 principal 4 个活跃租约；TTL 默认 300 s、最大 3600 s。超限明确报错，不截断、不返回部分视图。`snapshot()` 签名不变。
- 读取：每次读取都复核 token、租约归属、`expires_at > clock_timestamp()` 和该 principal 当前读权限；他人租约、过期、principal 失效与不存在同一错误。物化只限定可见范围，不放大授权。
- 清理：`sweep_expired(dsn, batch_size=100)` 分批删除过期租约及条目；CLI 未提供清理命令，调度属部署事项。
- 非 snapshot 的 query 分页仍为 offset，跨页不保证一致。

## 8. 迁移、Outbox 与能力启用（实际）

- `SCHEMA_VERSION = 3`。revision 1 为基础 DDL（不可变，不改写成 2）；2 增加身份表；3 增加 snapshot 表。`ear-storage migrate` 显式逐级执行；服务与 `AuthOperator` 使用前 `check_schema`，不匹配即拒绝；任何路径不自动 drop/重建。有 revision 1 真实 DDL 升级后保留记录、边、Outbox 与重放的测试。
- CLI：`ear-storage {migrate,bootstrap,serve} [--host 127.0.0.1] [--port 8000]`，只读显式 `EAR_STORAGE_DSN`，不读 `.env`、无默认库，`serve` 不自动迁移。
- Outbox：写事务内原子写入 Outbox 行。**Relay、JetStream 持久消费者、远程订阅未做（P2）**；至少一次投递、消费端去重的约定不变。
- 能力项：核心类型 `requires_dependencies` / `supports_reevaluation` 仍全为 false；服务不得自行打开，须③④等迁移并验证后按类型启用。

## 9. 测试与验收（实际）

- 夹具：`services/storage/tests/conftest.py` 只用 `docker run --rm` 起独立临时 PostgreSQL（`postgres:17-alpine`，`--pull=never`，随机密码，只绑 `127.0.0.1`）；**不支持外部库**：启用时若设置了 `EAR_POSTGRES_TEST_DSN`，夹具抛 `RuntimeError` 拒绝运行、不连接该 DSN，不得指向开发库、共享库或生产库。需设 `EAR_RUN_POSTGRES_TESTS=1`，否则 `postgres` 标记的测试明确 skip；报告必须写出该次运行实际的 skip 数，不得预估。
- 覆盖：独立进程竞争（`test_p1a_regressions.py`）、重启持久与重放（`test_postgres.py`）、授权/撤权/脱敏/重放复核（`test_auth.py`）、HTTP 与 SDK 错误映射（`test_http.py`）、真实 TCP 提交后响应丢失（`test_http_postgres.py`）、snapshot 上限/TTL/跨 principal（`test_snapshots.py`）、编解码（`test_codec.py`）。
- 结果：全启用时 644 passed、0 skipped、1 warning（提交前的本地日志）；CI 已改为启用真实 PostgreSQL，但尚未在远程运行。
- 未做：P2 故障投递测试；`dependency_cycle` 旧客户端端到端兼容测试未确认。

## 10. 不在 P1 / 参考资料

- 不在 P1：Relay/JetStream/远程订阅（P2）；动态注册、凭据生命周期、①管理反馈与⑥管理页（P3）；④规则集合并发守护、结果记录所属方、多 cause（P4）；插件历史交接（P5）。均仍属 MVP。
- 参考（实现参考，非版本承诺）：
  - PostgreSQL 17 显式锁定：https://www.postgresql.org/docs/17/explicit-locking.html
  - psycopg 3 事务管理：https://www.psycopg.org/psycopg3/docs/basic/transactions.html
  - NATS JetStream：https://docs.nats.io/concepts/jetstream
  - FastAPI 并发与 `def` 路由：https://fastapi.tiangolo.com/async/
