---
audience: ai
doc-type: guide
based-on: ADR-0001（独立服务、PostgreSQL 同库多 schema、JetStream+Outbox/Relay，accepted）；设计 v1.2；P0 契约提案（accepted）；storage/implementation-preparation 分支 P0a a6a1c5a、P0b cf337d1；P1 实际代码（提交 d7c9919；services/storage/src/ear_storage 的 schema/repository/backend/auth/service/snapshots/reader/http/cli，ear_storage_client 的 engine/wire/remote/remote_errors）与提交前检查日志 /tmp/ear-p1http-full-OK7Aeg.log；P2/P3 后端提交 d0d8875（其中 Relay：relay.py、schema revision 4、cli relay 命令、tests/test_relay*.py 与 conftest NATS 夹具；早期分组日志 /tmp/ear-p2-*-final.log）；合并提交 b7307e9；均在 PR8，未合并；项目负责人授权“采用推荐方案，在功能分支持续实现并分批提交、推送和提 PR，合并前由负责人兼 I 审核”；PostgreSQL 17、psycopg 3、NATS JetStream、FastAPI 官方文档（见第 10 节）
---

# ① P1 持久化服务与 P2 Relay：工程指南（按实际实现修订）

本页原为 P1 的实施方案，现已按**实际 P1 代码**修订（提交 d7c9919，PR8 未合并；以分支历史和 PR8 为准）；第 1、8、9 节另记录 P2 Relay（提交 d0d8875）；P2 持久消费者/远程订阅与 P3 管理、凭据、订阅重配置本页未展开，以 [progress](progress.md) 与代码为准。它落实 [ADR-0001](../../../docs/adr/0001-storage-and-runtime-modules.md)，覆盖[实施准备](implementation-plan.md)第 5 节要求的三项设计（请求 ID 与提交结果未知、远程 snapshot、可信身份与凭据）。它是工程指南，不是新的产品决策；随 P1 PR 接受合并前审核（负责人兼 I）。下文“实际”指当前代码，“未做”指仍属后续阶段。实现状态与测试结果见 [progress](progress.md)。

## 1. 技术选择（工程决定，不是产品承诺）

| 项 | 实际选择 | 说明 |
|---|---|---|
| 数据库 | PostgreSQL 17；测试用已有 `postgres:17-alpine` 镜像的隔离容器 | 部署形态（Compose、卷、备份）仍由 I 负责 |
| 驱动 | psycopg 3，同步 API | 与同步 `StorageClient` Protocol 一致 |
| 传输 | HTTP + JSON，`/v1` 前缀，FastAPI；`serve` 用 uvicorn | 请求体默认上限 8 MiB、读取超时 15 s（`create_app` 参数） |
| SDK | 同步 `RemoteStorageClient`（httpx），实现现有 `StorageClient` / `Reader` Protocol | `MemoryStorage` 保留为参考实现；共享纯逻辑在 `engine.py` |
| 版本 | 精确版本由 `uv.lock` 锁定 | 锁定版本不是对外承诺 |
| 总线（P2，d0d8875） | NATS JetStream，`nats-py>=2,<3` 异步客户端；测试用已有 `nats:2.11-alpine` 官方镜像的隔离容器 | 部署 NATS（账户、TLS、持久卷、容量）由 I 负责 |

## 2. 接口形态（实际）

- 路由：`GET /v1/identity`（token 对应的模块/principal 信息）、`GET /v1/registry`、`POST /v1/batches`（写批次，含 `request_id`）、`POST /v1/read`（统一读入口，按 `wire.ReadMethod` 分派，可带 snapshot）、`POST /v1/snapshots`（创建租约）、`DELETE /v1/snapshots/{snapshot_id}`（提前释放）。原方案中按资源拆分的 REST 读路由**未采用**。
- 编码：请求/结果/读参数由 `ear_storage_client.wire` 定义（封闭模型，拒绝未知键，时间必须 UTC）；冲突仍是正常结果中的 `conflict`，不是 HTTP 错误。具体状态码与错误体以 `http.py`、`wire.py` 和 `services/storage/tests/test_http.py` 为准，本页不重复。
- SDK 错误：`AuthenticationError`、`PermissionDenied` / `SnapshotUnavailable`（缺失、过期、他人或失效 principal 的租约同一错误）、`ResourceLimit`、`ServiceUnavailable`、`ProtocolError`、`CommitUnknown`（见第 6 节）。
- 订阅：d7c9919 时远程 `subscribe` 抛 `NotImplementedError`；P2（d0d8875）已实现服务端授权、经 HTTP 代理的持久消费者与 SDK `subscribe` / `poll_once` / `run_until_idle`，见 [progress](progress.md)。
- 服务端派生的 module/registry 只供参考，不是授权能力。

## 3. 可信身份与读授权（实际）

- 凭据：`secrets.token_urlsafe(32)`（256 bit），DB 只存 SHA-256 摘要，绑定一个模块 principal；明文只在签发时返回一次，无取回接口。
- 签发：部署环境内部的 `AuthOperator`（`issue_principal`、`revoke`、`set_grants` 整体替换授权，以及应急 `rotate(principal_id)`：保留 ID 与授权，递增 `credential_epoch` 使待领取项作废，不启用模块、不恢复授权）。它**不经任何 token 认证的传输暴露**，也**没有 CLI 或 HTTP 命令**（原方案中的 `admin issue-token` 未实现），不是日常管理入口。插件凭据的轮换/撤销由 P3 管理反馈（`rotate_credentials` / `revoke_credentials`）加一次性 claim 完成（P3，d0d8875，见 [progress](progress.md)）。
- 请求中的模块名不采信；principal 只来自 token。每个操作在全局写闸下重新认证并读取授权，撤权在其提交后对之后受理的操作立即生效。
- 授权模型：没有隐式授权（包括所有者读自己的类型）；一行 grant 即读权限，`can_subscribe` 是该行上的附加能力（P2 起用于持久消费者的投递过滤）。写入需要单写者所有权，**并**对本批实际读到的输入类型（目标、cause、依赖引用、被引用对象等）有读权限，隐式时间来源读取也不得成为存在性预言。
- 读授权覆盖全部 Reader 路径；依赖两端都检查，计数与查询同权，反向/过时依赖只返回可读条目；无权与不存在对外不可区分，错误详情脱敏。
- 核心/插件来源：`bootstrap` 写入受信核心登记表；插件动态注册属 P3。`Registry.extend` / `extend_core` 仍只是构造时的静态登记表。

## 4. 写事务与并发（实际）

- 每个操作一个短事务，`READ COMMITTED`，第一步执行事务级 advisory 锁 `WRITER_GATE`（`schema.py` 常量）；**写批次、普通读取、snapshot 创建/读取和 `AuthOperator` 管理共用同一闸**，取闸后再认证和读取校验数据。首版保正确不保吞吐；任何后续优化都不得靠跨请求或无界事务换一致性。
- 事务内只做校验与写入，不等待模型、外部 HTTP 或用户输入；锁随提交/回滚释放，方法返回后不残留锁。
- 每次提交在闸下分配单调 `commit_seq`（`storage.meta`），用于 Outbox 排序；它不是跨系统全局顺序。
- 批末检查（完整登记、新鲜度、无环、核心不依赖插件）复用 `engine.py` 的共享逻辑，在同一事务、闸下对**有界当前图投影**执行。`repository.Limits` 默认：10 000 节点、50 000 依赖边、32 MiB 投影、每批 2 000 操作、8 MiB 请求；超限抛 `ProjectionLimit`，整批不提交。该成本随当前图规模增长（不随历史版本增长），作为公开已知限制随 PR 审核。

## 5. 数据布局（实际）

- 不一类型一表，不把内存状态序列化为单行 blob。
- `storage` schema 公共表：`meta`（`schema_revision`、`commit_seq`）、`registry`、`publishers`、`modules`、`identity`、`dependency_sets`、`dependency_edges`、`processed`、`published`、`outbox`、`idempotency`、`principals`、`type_grants`、`snapshot_leases`、`snapshot_items`；revision 4（P2）增加 `relay_state` 并给 `outbox` 增加投递列（见第 8 节）；revision 5–8（持久消费者、管理反馈、凭据、订阅重配置，d0d8875）见 [progress](progress.md)。
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

## 8. 迁移、Outbox Relay 与能力启用（实际；Relay 为 P2，提交 d0d8875）

- 迁移：P1 提交时 `SCHEMA_VERSION = 3`（revision 1 基础 DDL 不可变，不改写成 2；2 增加身份表；3 增加 snapshot 表）。P2 Relay 升至 **4**（P2 消费者为 5；P3 管理 6、凭据 7、订阅重配置 8，d0d8875 为 8；P4 集合守护 9 增加 `type_revisions`，见 progress）：只增加 `outbox` 投递列（`attempts`、`next_attempt_at`、`last_error`、`claim_token`、`delivered_at`、`ack_stream`、`ack_seq`）、未投递部分索引和单行表 `relay_state`（`fence`、`lease_until`、`paused`、`destination`），原事件文本不变。`ear-storage migrate` 从 1/2/3 显式逐级升到 4；服务、`AuthOperator` 与 Relay 使用前 `check_schema`，不匹配即拒绝；任何路径不自动 drop/重建。有 revision 1 真实 DDL 升级后保留记录、边、Outbox 与重放的测试，以及 revision 1–3 升到 4 保留事件与已有状态的测试（`test_relay_schema.py`）。
- CLI：`ear-storage {migrate,bootstrap,serve} [--host 127.0.0.1] [--port 8000]`，只读显式 `EAR_STORAGE_DSN`，不读 `.env`、无默认库，`serve` 不自动迁移。P2 增加 `relay [--once]`（持续运行，或只跑一批并输出 JSON 摘要）、`relay-health`、`relay-pause`、`relay-resume`；这些命令另读部署环境的 `EAR_NATS_SERVERS`（逗号分隔，`nats://`/`tls://`，须带端口，URL 不得含用户名/密码）及可选 `EAR_NATS_TOKEN`、`EAR_NATS_STREAM`（默认 `EAR_EVENTS`）、`EAR_NATS_PREFIX`（默认 `ear.events`），其余参数取 `RelayConfig` 默认值。NATS 凭据只来自部署环境，不写入数据库或普通记录，不出现在健康输出。
- Outbox：写事务内原子写入 Outbox 行（P1）。P2 Relay（d0d8875）（`relay.py`）：
  - 顺序与 fence：数据库全局唯一租约（默认 30 s）加单调 fence。认领在短事务内 `FOR UPDATE` 锁 `relay_state`，只取按 `(commit_seq, ordinal)` 的**队首**并递增 fence，提交后才做网络 I/O；broker 调用不在任何数据库事务内。标记须 fence 一致、租约未过期、未暂停，否则不推进。租约过期的慢发布者仍可能把同一事件再发到 broker，但不能标记或推进其他事件。
  - 先 PubAck 后标记：拿到 PubAck（校验 stream 与 seq ≥ 1）后在新事务写 `delivered_at`、`ack_stream`、`ack_seq`。PubAck 后数据库标记失败或进程退出会重发，这是有意的至少一次。`Nats-Msg-Id` 为 `event_id`，只在流的 `duplicate_window`（默认 120 s）内去重；超出窗口的重复无法排除，**不是外部恰好一次**，消费方仍按 `event_id` 幂等。
  - 失败处理：失败队首阻塞后续事件，**从不跳过**；退避 `min(retry_max, retry_base × 2^(attempts−1))`，默认 1 s 起、上限 60 s，次数不限。单次网络总时限 `timeout`（默认 5 s，含连接、流校验与 PubAck）。安全错误码仅 `publish_failed`、`invalid_event`、`stream_mismatch`。没有 Relay 侧死信或跳过命令：无效队首需人工处置，可用 `relay-pause` 暂停（同时递增 fence，使在途认领无法标记）。
  - 流：不存在时创建——文件存储、limits 保留、`discard=new`、默认 256 MiB / 1 000 000 条、`max_age=0`（无 TTL）、单副本、禁止 delete/purge、`duplicate_window` 120 s。已存在的流（含并发创建者建立的）逐项比对上述配置，并拒绝 mirror、sources、republish、subject transform、rollup、消息 TTL 等额外能力；不符即 `stream_mismatch` 失败关闭，**绝不修改**已有流。流满时 broker 拒收新消息、不淘汰已接受消息，Outbox 行保持未投递并按退避重试。
  - 主题：`<prefix>.<event_type>.<类型>.<所属模块>`；`data_changed` 取 `data_type`，`reevaluation_requested` 取 `target_type`，所属模块为 registry 中该类型的 `writer`。仍只有这两类事件，消息体是 Outbox 中原样的事件 JSON，不附加记录正文。类型缺失或路由字段不合法记为 `invalid_event`。
  - 目标：servers/stream/prefix 的 SHA-256 摘要记入 `relay_state.destination`；部署改指其他目标时认领失败关闭，不静默换流。
  - 健康：`relay-health` 只读数据库（`scope: database_relay_state`、`broker_probed: false`），给出 `paused`、`leased`、`pending`、`delivered`、队首（`commit_seq`、`ordinal`、`attempts`、安全错误码、`next_attempt_at`）、`ready` 与 `status`（idle / pending / retry / in_flight / paused）；不含事件内容、DSN、token、服务器地址，**不能证明 broker 可达**。
  - P2 后续（d0d8875，见 progress）已实现服务端授权的持久消费者、ack/nack/退避/死信/重放与 SDK `subscribe`；仍未做按模块隔离的 NATS 账户/主题权限。
- 能力项：核心类型 `requires_dependencies` / `supports_reevaluation` 仍全为 false；服务不得自行打开，须③④等迁移并验证后按类型启用。

## 9. 测试与验收（实际）

- 夹具：`services/storage/tests/conftest.py` 只用 `docker run --rm` 起独立临时 PostgreSQL（`postgres:17-alpine`，`--pull=never`，随机密码，只绑 `127.0.0.1`）；**不支持外部库**：启用时若设置了 `EAR_POSTGRES_TEST_DSN`，夹具抛 `RuntimeError` 拒绝运行、不连接该 DSN，不得指向开发库、共享库或生产库。需设 `EAR_RUN_POSTGRES_TESTS=1`，否则 `postgres` 标记的测试明确 skip；报告必须写出该次运行实际的 skip 数，不得预估。
- 覆盖：独立进程竞争（`test_p1a_regressions.py`）、重启持久与重放（`test_postgres.py`）、授权/撤权/脱敏/重放复核（`test_auth.py`）、HTTP 与 SDK 错误映射（`test_http.py`）、真实 TCP 提交后响应丢失（`test_http_postgres.py`）、snapshot 上限/TTL/跨 principal（`test_snapshots.py`）、编解码（`test_codec.py`）。
- P1 结果（d7c9919 提交前）：全启用时 644 passed、0 skipped、1 warning（提交前的本地日志）；CI 已改为启用真实 PostgreSQL，但尚未在远程运行。
- P2 NATS 夹具（d0d8875）：同一 `conftest.py` 用 `docker run --rm` 起隔离 NATS（官方 `nats:2.11-alpine`，`--pull=never`，随机名称与标签，128 MiB 内存、1 CPU，数据在 64 MiB tmpfs，只绑 `127.0.0.1` 随机端口；随机 token 经 `docker exec` 的 stdin 写入配置，不进 argv、环境或诊断）。需同时设 `EAR_RUN_NATS_TESTS=1` 与 `EAR_RUN_POSTGRES_TESTS=1`，只设前者会 `pytest.fail` 而不静默 skip；设置 `EAR_NATS_TEST_DSN` 同样 `pytest.fail`，**不支持外部 NATS**。
- P2 覆盖：`test_relay_unit.py`（配置校验/脱敏/流不符，30 项）、`test_relay_schema.py`（revision 1–3 真实升级，3 项）、`test_relay.py`（16 项，真实 PostgreSQL + JetStream，故障场景列表见 [progress](progress.md)）。
- P2 Relay 阶段早期结果（已被 progress 中 P2 完整 802 项运行取代）：分组运行各 exit 0、0 skip，合计 693 项（552 + 42 + 29 + 14 + 37 + 19，分组与日志见 progress）；默认不启用时 561 passed、132 skipped、1 warning。没有完整启用的单次 693 项运行。CI 已加 NATS 镜像与 `EAR_RUN_NATS_TESTS=1`，未在远程运行。
- 消费者侧真实测试见 progress（P2 工作区快照完整 802 项两次通过；merge 前 P3 检查点 916 项；b7307e9 后尚无完整真实运行）；`dependency_cycle` 旧客户端端到端兼容测试未确认。

## 10. 不在 P1 / 参考资料

- 不在 P1：Relay/JetStream/远程订阅（P2，d0d8875，见第 8 节与 progress）；动态注册、凭据生命周期、订阅重配置、①管理反馈（P3 后端，d0d8875）与⑥管理页（P3 余项，未做）；④规则集合并发守护、结果记录所属方、多 cause（P4）；插件历史交接（P5）。均仍属 MVP。
- 参考（实现参考，非版本承诺）：
  - PostgreSQL 17 显式锁定：https://www.postgresql.org/docs/17/explicit-locking.html
  - psycopg 3 事务管理：https://www.psycopg.org/psycopg3/docs/basic/transactions.html
  - NATS JetStream：https://docs.nats.io/concepts/jetstream
  - NATS JetStream 流配置与去重窗口：https://docs.nats.io/nats-concepts/jetstream/streams
  - FastAPI 并发与 `def` 路由：https://fastapi.tiangolo.com/async/
