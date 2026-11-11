---
audience: ai
doc-type: guide
based-on: docs/design/design-v1.2.md；docs/design/plugins-mvp.md；docs/decisions/mvp-confirmed-decisions.md；docs/adr/0001-storage-and-runtime-modules.md；代码契约基线origin/dev 48d3c51；其后①在 storage/implementation-preparation 分支实现 P0a（首个提交 a6a1c5a）与 P0b（cf337d1），P1（d7c9919）与提交前本地检查日志 /tmp/ear-p1http-full-OK7Aeg.log，P2/P3 后端 d0d8875（日志 /tmp/ear-p2b-mustfix/closing-*.log、/tmp/ear-p3-*.log、/tmp/ear-p3-checkpoint-validation.log）及合并提交 b7307e9（同步 dev 4afeae0）；P4 ①侧集合守护 f747e89、重判完成记录 106cd2e、多起因 daa767f，调用记录存储 e9391dd，以及本分支已实现的 P5 ①侧历史恢复（schema revision 14；提交状态以提交历史为准）；当前基线 origin/dev 376294d：PR8 已于 2026-11-07T09:32:19Z 合并（gh 只读核对，用户述已审核并合并），dev 上 CI run 42360158060 success（1503 passed, 5 skipped, 1 warning in 583.04s，POSTGRES/NATS/BACKUP 三开关均为 1）；api/gateway-mvp 分支⑥⑦的部分交付状态由用户授权同步（本次只改 Markdown，依据功能分支 `api/gateway-mvp`（待合并）的实际源码与 2026-11-07 本机取证记录 ear-67-browser-state.json、ear-api-gateway-deploy-state.json、ear-67-postfix-review.json、ear-67-full-acceptance.log、ear-67-acceptance-fixes.json、ear-67-full-v2.log（及 .exit）、ear-67-static-final.log（及 .exit），见第 6 节）
---

# 已采纳 MVP 的契约与实施计划

## 0. 这不是实现完成报告

负责人已明确采纳目标并授权设计同步，那一轮不改contracts、业务代码、依赖或部署；此后①按授权在功能分支 `storage/implementation-preparation` 分批提交（P0a a6a1c5a、P0b cf337d1、P1 d7c9919、P2/P3 后端 d0d8875、合并 b7307e9、P4 f747e89/106cd2e/daa767f/e9391dd、P5 9705b61 及隔离部署切片，各提交详情见[①进度](../../services/storage/ai-docs/progress.md)）。**当前基线：PR8 已于 2026-11-07T09:32:19Z 合并为 `dev` 376294d（`gh` 只读核对，合并人 aytony；用户述已审核并合并）；dev 上 push 触发的 GitHub Actions CI run 42360158060（workflow CI，headSha 376294d）结论 success，日志显示 `EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS` 均为 1，pytest 为 1503 passed, 5 skipped, 1 warning in 583.04s。** 这说明①存储切片在 CI 的隔离环境（真实 PostgreSQL、NATS、备份往返）通过，**不是**生产验收：没有生产启动，生产部署与升级迁移、流保留/清理、备份文件保留/删除仍未做；③④⑤尚未与①完成完整业务联调（④是 v1.1 演示基线，⑤调度未实现）；②适配器已实现远程单轮/常驻扫描和重放，真实①双门控联调测试与真实旧 Ear 数据写入临时①已于 2026-11-11 本机跑通（非生产验收）；⑥⑦在功能分支 `api/gateway-mvp`（待合并）里已部分实现，**部分交付、待审，不是完整 MVP**；修复后的最终全套验收已在合成/隔离环境通过（2067 passed、11 skipped，见第 6 节），不是生产验收。下文各阶段中“未合并”“尚未远程运行”等措辞是各自提交时的历史说明，以本段为准；历史测试数字不改。现有内存行为与新目标不同是显式待实现差距，不因测试绿就认定完成。目标定义见[设计](../../docs/design/design-v1.2.md)、[插件](../../docs/design/plugins-mvp.md)及[决策来源](../../docs/decisions/mvp-confirmed-decisions.md)。

①的首批PR拆分、6cea0b7代码证据与阶段出入口见[①实施准备](../../services/storage/ai-docs/implementation-plan.md)。本页是工程执行顺序和风险清单，新增字段/类型名除已讨论术语外只是待设计项；不替代公共契约审核。协议、索引、超时等细节可由实施者提出，不反复问产品负责人；会改变已确认行为时再请求决策。

## 1. 基线与代码导航

| 位置 | 当前能力 | 不应误认为已有 |
|---|---|---|
| `contracts/records/dependency-record.schema.json` | 对象版本引用；P0b 分支新增可选顶层 `fields` | 动态读取权限、持久化后端的并发防环 |
| `contracts/registry/data-types.json` | 核心类型/单写者、约束；P0b 分支 Schema 新增能力项（缺省 false，核心类型当前全 false） | 持久热注册、可信身份、完整插件生命周期、已启用的强制登记 |
| `contracts/events/` | 两类事件、主cause及追踪字段；P4 多起因（daa767f）增加 `additional_causes` 与请求关联 | ⑤的多因调度与合并策略、各模块完成信号接入 |
| `libs/ear_contracts/src/ear_contracts/` | 模型/UUIDv7/UTC/注册表 | 新结果、管理、外部未知状态已同步 |
| `libs/ear_storage_client/src/ear_storage_client/protocol.py` | 同步Reader/StorageClient/snapshot/subscribe及重试helper；P1（d7c9919）另有同包 `remote.py` 同步远程客户端与 `wire.py` | 可长期持有的快照（远程订阅已在 P2 实现，d0d8875） |
| 同包 `ops.py`、`memory.py` | 批次、版本、依赖整体覆盖、内存总线；P0b 分支：`DependencyInput` 字段依赖、按能力强制登记、批末新鲜度、当前图无环（内存全局锁） | 持久化并发正确性、可信身份/动态权限、生产可靠投递 |
| 同包 `queries.py`、`results.py` | 查询分页与冲突；P0b 分支 `DependencySet`、`dependency_cycle` | 远程游标一致性、所有读路径访问控制 |
| `services/storage/` | 文档入口；P1 服务包 `ear_storage`（PostgreSQL、身份授权、snapshot、HTTP、CLI；d7c9919）；P2 `relay.py`、`consumers.py`（有序 Relay 与服务代理的持久消费者）与 P3 后端 `management.py`、`credentials.py`、`reconfiguration.py`（d0d8875，已随 PR8 合入 dev 376294d） | ⑥管理页与真实①的浏览器联调（页面已在⑥实现，功能分支待合并；真实浏览器 34 项仅对假①，API 层另有对真实①的 26 项集成测试，见第 6 节）、流保留/清理、生产部署（隔离 Compose 切片已实现，见[隔离部署指南](../../services/storage/ai-docs/deployment.md)） |
| `services/adapters/` | ②：旧 Ear Evidence → 原始数据的转换、导入规则、内存试运行；命令行已支持远程单轮/常驻扫描和重放，真实①双门控联调测试与真实旧 Ear 数据写入临时①已于 2026-11-11 本机跑通（非生产验收）（[实现说明](../../services/adapters/ai-docs/implementation.md)） | 正式部署的 adapter 身份签发、Compose 部署上的命令行真实运行、旧 Ear 的列录音接口、GPS 采样密度 |
| `services/api/` | ⑥（功能分支 `api/gateway-mvp`，待合并；部分交付待审）：只读 API、反馈入口、管理接口与插件目录、轻量页面、受保护配置与命令行、离线外部动作切片；见[模块入口](../../services/api/README.md)与[实现说明](../../services/api/ai-docs/implementation.md) | 外部动作执行/确认/风险重试（HTTP 501，缺④协议）、③⑤反馈的消费（只记录 `pending_contract`）、真实日历适配器、真实浏览器↔真实①+⑤的端到端（API 层对真实①的 26 项集成测试已通过，见第 6 节）、生产运行（全套验收仅在合成/隔离环境通过，见第 6 节） |
| `services/llm_gateway/` | ⑦（功能分支 `api/gateway-mvp`，待合并；部分交付待审）：网关 HTTP 服务、调用方①令牌认证、重判上下文预检、call_id 占用检查、单 worker 私有 journal、终态 `llm_call` 写入、受限输出 schema、源码固定到 Octopus `b3c7cd5dd950a79e94533b114619082a542eac66` 的适配器、客户端/命令行/容器；见[模块入口](../../services/llm_gateway/README.md)与[实现说明](../../services/llm_gateway/ai-docs/implementation.md) | 真实模型调用与生产 wire 验证、exactly-once、实测用量（Octopus 的 usage 为合成估算，记为未知）、③④⑤和插件接入、正文保留期、生产运行（全套验收仅在合成/隔离环境通过，见第 6 节） |
| `services/dependency/` | ⑤：尚无代码；[模块入口](../../services/dependency/README.md)、[实现设计草案](../../services/dependency/ai-docs/design.md)与[接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)（proposed，传播记录、按钮反馈格式与①权限 A1–A4） | 调度实现、传播记录、按钮反馈消费、历史补处理接入、多层测试插件 |
| `deploy/compose.yaml` | ①存储切片的隔离 Compose（postgres、nats、storage、relay、manage-worker；显式 migrate/bootstrap/init-identities）与备份工具 `tools/storage_backup.py` 已随 PR8 合入 dev 376294d（该文件本身不含②③④⑤⑥⑦，非生产运行手册，见[隔离部署指南](../../services/storage/ai-docs/deployment.md)；⑥⑦的 opt-in 覆盖文件 `deploy/compose.api-gateway.yaml` 等见第 6 节，功能分支待合并） | 生产部署与升级迁移、备份文件保留/删除、其他模块部署 |
| `tests/e2e/` | E2E骨架 | 插件端到端完成 |

先复用正确的基础行为，不从零定义另一套category/ULID/事件命名。UUIDv7、data_type/payload、system根源、两类事件和单写者等保持既有公共约定。

## 2. 契约改动清单（①已执行的部分见[①进度](../../services/storage/ai-docs/progress.md)，其余尚未执行）

修改Schema仍由①与I审核，原则为只增加字段，不删除或改变旧字段含义。若现有结构无法无损表达目标，提出显式兼容版本方案，不把不兼容改义伪装成“增加字段”。

| 范围 | 需要设计/同步 | 配套验证 |
|---|---|---|
| 依赖记录/VersionRef/登记输入 | 可选顶层fields准确放置；读模型、操作和保存时不得丢失；字段规范化与空集区别 | 缺省版本比较、缺失/null、数组顺序、重复字段、空fields、A→B→A、替代 |
| 数据类型登记 | owner与可重判/必需依赖能力；核心内置+持久动态模块配置 | 单写者、未知/冲突类型、跨核心边界、批次缺登、空依赖有效 |
| 新业务类型 | rule_selection；完成记录按执行模块/插件单写 | 同批更新/无变化登记和完成，不新增第三事件 |
| 管理记录/反馈 | ⑥单写反馈、①单写配置结果；可信身份与审核上下文，阶段/幂等和权限声明 | 重投、撤权排队、跨组件恢复、受理≠生效 |
| 重判请求/传播记录 | 保留原cause/root/depth，兼容增加多个起因与全部追溯；结果和调用关联请求ID | 合并同root多因；跨root合并不能丢来源，策略未定前不得随意选一个root |
| 外部动作 | 执行中/结果未知及核对反馈、稳定动作标识、恢复信息 | 成功后崩溃不盲重试、明确失败/未知区别 |
| 插件动作交接 | 插件建议如何进入核心审核执行、反馈回哪个所属方 | 不准插件写④类型，不把系统管理反馈作为任意跨写通道 |
| 调用记录 | ⑦单写①保存，默认metadata、按需正文、请求关联 | 日志不能默认包含提示词/响应，业务产物仍保存 |

新增英文类型/枚举在契约阶段定名并同步术语，不能把文档示意当作已有字段。日志正文保留期/开启范围可配置；逐次尝试记录粒度是实施建议，不虚构负责人已经批准每个日志字段。

## 3. 分阶段工作

### P0 契约与内存基线

- 以当前Protocol/Schema映射上述目标，明确兼容策略和公共错误/冲突语义。
- 抽取共享内容比较、权限/所有权、字段失效和批次依赖校验，保留正确的原去重、事务、版本行为。
- 强制需依赖类型的Create/Update（包括unchanged）同批完整依赖；不自动继承；显式空集；Supersede免新登记。
- 处理批次最终图约束和历史图分离；不能只按单条操作检查，临时中间关系不应替代最终状态判断。
- 当前MemoryStorage只按版本失效；fields不能仅进入extra字段后在构造VersionRef时丢掉。
- 原可疑边界补回归后修：投递上限先出队、并发runner、空时间查询、系统记录更新时间。已作为 P0a 在 `storage/implementation-preparation` 分支实现并验证（提交与合并状态以提交历史和 PR 为准）；拆分与证据见[①实施准备](../../services/storage/ai-docs/implementation-plan.md)。
- P0b（字段依赖、登记表能力、批末强制）的契约选择见[P0 契约提案](../../docs/proposals/storage-p0-contracts.md)，已 accepted 并获功能分支实现授权；**已在本分支实现内存版本**（全量 426 passed、0 xfail，历史快照），已随 PR8 合入 dev（用户述已审核并合并，见第 0 节）。上面依赖相关各项在内存实现中已覆盖，但核心类型能力项全部 false（启用需③④迁移验证），核心/插件来源是静态可信登记表而非动态鉴权，`dependency_cycle` 旧客户端兼容测试未做；持久化版本属于 P1。

### P1 独立服务与持久事务

- 工程方案见[①P1 持久化服务方案](../../services/storage/ai-docs/persistent-service-plan.md)（已按实际实现修订；P1 已提交 d7c9919，已随 PR8 合入 dev 376294d）。
- 建正式storage包、迁移与PostgreSQL实现，同库模块schema+公共storage设施，不一类型强制一表。
- 可信凭据绑定模块；所有读取路径做授权，批次写入检查所有权，不信任传参module。
- 实现版本锁/唯一性、完整依赖、新鲜度和DAG的并发安全提交；不同事务分别添加边也不能并发形成环。
- 保留SDK兼容目标，明确远程snapshot生命周期、分页一致性、订阅回调、超时及提交结果未知时的安全重试；不能持有无限时长数据库事务等待模型。
- 同一批数据/依赖/结果/消费标记/Outbox原子生效，模型调用和业务计算在外部。

### P2 JetStream与可靠交接

- **当前状态**：P2 已提交 d0d8875（已随 PR8 合入 dev 376294d）：持久 Outbox、可恢复有序 Relay、服务端授权的持久消费者（HTTP 代理）、ack/重试/死信/同 consumer 重放、SDK `subscribe` 有界泵；有限去重的至少一次，不是恰好一次；流满拒收且无清理，生产保留仍欠，历史补/跳①侧见 P5。真实完整测试 802 项在 P2 工作区快照上两次通过；merge 前 P3 检查点 916 项全真实通过；b7307e9 合并当时未完整真实运行（历史说明）；当前 dev 的完整真实运行见第 0 节。见[①进度](../../services/storage/ai-docs/progress.md)。
- 持久Outbox和可恢复Relay，明确发送成功但标记前崩溃如何重投；不将自增序号自动等同于全局提交顺序。
- 持久消费者、确认策略、退避、失败记录/死信、管理员检查和重放。跨消费者各自进度，授权投递不能共享错误的工作队列吞掉其他模块通知。
- `write()`冲突不是默认异常；handler必须检查最终结果，失败不能正常返回当ack。already_processed是特例，不盲重试业务结果。
- 最小必要事件内容；请求描述不得成为绕过权限传送未授权数据的通道。

### P3 动态注册、权限和管理页

- **当前状态**：后端已提交 d0d8875（已随 PR8 合入 dev 376294d）：管理反馈/结果类型、可信管理员角色、运行时登记表与动态 namespace、启停与权限、`retry_task`、凭据经管理反馈轮换/撤销与一次性领取（schema 7）、订阅完整重配置（schema 8：新增类型只收未来事件、原积压保留、原子切换，已撤销身份不复活）；⑥管理页与管理接口已在⑥实现（部分交付待审，功能分支待合并；真实浏览器检查只对假①，API 层对真实①的 26 项集成测试已通过，无真实浏览器↔真实①联调，见第 6 节）。merge 前检查点全真实 916 passed、0 skipped；b7307e9 当时只有默认检查（历史说明），当前 dev 的完整真实运行见第 0 节，另见[①进度](../../services/storage/ai-docs/progress.md)。

- 核心配置初始化+动态持久模块注册，校验身份/命名空间/类型唯一、读取/订阅权限、重判和依赖能力，完成记录类型不能只固定③④。
- ①管理反馈消费者调用内部逻辑；⑥轻量页面提交反馈和查询阶段。管理HTTP入口如需存在也只是受控内部能力，不能取代已定日常反馈流程。
- 资源创建、凭据/总线权限、消费者配置分阶段幂等恢复；未就绪不enabled。撤销后的旧凭据、既有连接和队列均测试，不仅隐藏UI。
- 明确已受理/执行中/成功/失败以及启用/在线差别。反馈、普通查询、日志不暴露管理密钥。
- 停用不删数据；应急阻断由部署管理员操作并审计，非普通插件绕过路径。

### P4 ③④⑤⑦业务与传播对齐（可在内存基线上并行）

- **当前状态**：未完成。存储分支已提交①侧规则集合守护（`type_revision` / `AssertTypeRevision`，schema revision 9，f747e89）与按所属方验证的重判完成记录及权威查询（106cd2e）；多起因（`additional_causes`）已提交 daa767f；⑦调用记录的①侧存储已提交 e9391dd：`llm_gateway` 单写的终态 `llm_call` 系统记录、真实请求关联与按全部起因读授权，默认只存 metadata，正文由部署级 `ModelBodyPolicy` 显式开启（默认关闭、64 KiB），无 TTL。⑦网关服务已部分实现（合成回放验证；无真实模型调用，不是 exactly-once，用量未知，见第 6 节）；③④⑤⑦尚未接入完成记录、守护与调用记录（④入队 marker 不是业务完成）；①侧存储协议不代表业务对齐完成。b7307e9 合入的模块④是演示基线，其说明注明不代表已完成 v1.2（见[④实现记录](../../services/extraction/ai-docs/implementation.md)），④的 v1.2 对齐仍属本阶段。
- ③仅参考匹配时登记references，不因名字字段变化重归属；保留用户确认保护。
- ④活动仅原始数据；纯Markdown；选择结果按当前规则集合更新，自动/手动来源分离；最新规则或规则失效处理。
- ③④及插件提交完成记录，⑤查询无变化/失败/不适用，等待已知相关上游、合并原因并保留追溯；不再写死md终点。
  ⑤的调度算法、持久化与任务拆分见[⑤实现设计草案](../../services/dependency/ai-docs/design.md)；需要①补的身份与权限（A1–A4）见[⑤接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)第 4 节。
- ⑦经①保存统计追踪，按请求ID关联；正文开启/保留参数不凭文档示例变成永久留存。
- ⑥外部执行区分未执行与未知，稳定标识、安全恢复、用户核对；审核与执行反馈遵守单写者。

### P5 插件历史交接与集成

- **当前状态**：未完成。①侧已实现并随 PR8 合入 dev 376294d（schema revision 11–14）：管理反馈恢复 catch_up/skip（须 `expected_revision`）与显式范围 `history_backfill`、持久当前状态候选（不盲重放旧事件）、skip 不改旧结果/依赖且不伪造 Finish、W 之后交接即便首次 consumer 也保留、重叠门控与过期输入只读、所属方 claim（snapshot + 租约 + epoch/fence）与业务/进度原子提交，以及供⑤的真实请求关联（权限与起因覆盖）、所属方安全领取分配并自行 Finish、完成索引与历史依赖按 `commit_seq` 验新鲜、终态冻结。⑤实际调度、④业务改动、真实多层/分叉汇合示例未做；⑥恢复选择页已实现但未与真实①和⑤联调（见第 6 节）；①存储切片的隔离 Compose 与备份/恢复工具已随 PR8 合入 dev（非生产，见[隔离部署指南](../../services/storage/ai-docs/deployment.md)），生产部署、升级迁移与备份文件保留/删除未做。完整真实运行：早先检查点 1 failed / 1411 passed / 5 skipped（其唯一失败已修复，历史数字不改）；之后的三开启本地完整运行 1503 passed / 5 skipped、exit 0（5 个 skip 为需要认证 PostgreSQL 的 `memory` 参数）；同配置远程 CI（dev 376294d，run 42360158060）为 1503 passed, 5 skipped, 1 warning in 583.04s，success，PR8 已合并（见第 0 节），但仍不是生产验收；见[①进度](../../services/storage/ai-docs/progress.md)。
- 首次启用的快照/游标与实时事件无缝衔接需测试，不能误把历史读取变成默认全量批处理。
- 主动停用恢复补/跳、意外断线自动续、越过消息保留窗口等边界明确；补处理读取当前状态，不盲跑旧事件。
- 停用/撤权/上游替代的依赖查询与调度：①拒绝未授权输入，⑤标受阻，旧结果保留；恢复补处理尊重用户选择。
- I与①⑤⑥使用至少能组成多层和分叉汇合的合成示例插件，覆盖动态注册无需重启、成环拒绝、并发新鲜度、失败阻塞以及恢复。
- Compose、健康检查、持久卷、备份恢复和应急操作可运行；不默认①承担全项目集成责任。

## 4. 需要在实施中明确、不得偷偷改变行为的边界

1. **规则候选集合并发**：只检查已引用版本无法发现计算期间新增规则或之前未选中项的集合变化。需要集合快照/修订或等价校验，既保证“重选看当前全集”，又不把规则编辑变成自动重算全部历史。
2. **仍选中但规则已变**：遵守“选择结果不额外重算仍选中产物、产物独立依赖”。若只有选择结果被其他输入触发，不能凭实现方便给未收到重判的全部产物强制升级规则。后续产物收到自身请求时再采用当前规则。
3. **依赖刷新与审计**：同一业务版本无变化可重登依赖；完成记录应保留计算起因和使用版本追溯。历史版本保留不等于当前依赖登记的每次覆盖都已有审计历史。
4. **图与任务不是同一对象**：当前对象图无环不意味着长期输入下全局停止；替代目标的历史依赖须仍可用于找到受影响当前下游，不能为防环简单删掉全部反向边。停用不是整体替代，保留其依赖关系。
5. **下游等待范围**：等待已知相关受影响上游，非全局锁；无变化完成释放等待，多条路径不能因一路没变就漏另一原因。失败/停用必须有明确受阻状态和恢复路径。
6. **撤权和敏感关系**：读/计数/反向依赖/错误详情/事件均可能泄露存在性；成环错误给出可定位信息也要遵守调用方权限。不能因⑤能看全图就把全图发给插件。
7. **完成记录安全**：插件只能写自己的请求结果，不能用伪造event_id替他人完成任务。字段准确形式待契约设计。
8. **管理任务自恢复**：不向插件发可以提升自己权限的管理请求；管理记录必须能在资源部分创建和授权变化后恢复。暂停状态、在途访问的截止点是验收点，不假装SQL和总线权限原子。
9. **外部恢复**：不能声称本地消费标记保障外部恰好一次；允许必要恢复标识，不因旧“不记外部ID”禁止安全恢复。来源插件停用时仍须区分已批准/未知动作与未审核建议。
10. **保护用户确认**：自动重判和上游失效不能静默推翻已确认归属；冲突时显示明确不适用/受阻等实际结果，而不是为使依赖检查通过而改用户判断。

## 5. 测试矩阵与文档同步

| 层次 | 必需验证 |
|---|---|
| 公共契约 | 新旧正反例、未知可选字段兼容、不改旧字段含义、所有者与结果类型 |
| 共享行为 | 内存/持久化去重/版本/全批回滚、字段边界、无变化刷新、强制登记 |
| 实际并发 | 同声纹重复新建、同版本竞争、并发成环、计算输入更新、引用集合新增 |
| 权限 | 所有读写/历史/计数/依赖/订阅入口、撤凭据、队列/既有连接、登记冲突错误 |
| 事件故障 | 提交前失败、提交后Relay崩溃、发送确认丢失、消费重复/失败/重放、注册中断 |
| 调度 | 线性多层、分叉汇合、多因、无变化、失败/超时/停用、上游终止、持续输入 |
| 插件历史 | 首次启用边界、显式范围补处理、主动恢复补/跳、意外断线、超保留窗口 |
| 外部动作 | 可靠幂等、查证成功、明确未执行、未知、成功后回写前崩溃、用户核对 |
| 端到端 | 原组会8步（API/验收工具）+插件管理页面+授权、多层传播和恢复 |

现有提交前命令继续使用：

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest && uv run python tools/check_docs.py --base origin/dev
```

每阶段应更新模块progress、契约示例、实现和对应测试，记录实际通过项及未覆盖项。仅跑现有289项测试不能作为新MVP完成证明；新增测试数量以执行结果为准，不在计划里预报通过。

## 6. ⑥⑦ 当前状态（分支 `api/gateway-mvp`，基于 dev 376294d）

**部分交付，待审，功能分支待合并。⑥⑦ 都不是完整 MVP；本节不宣称任何模块完成或可用于生产；全套验收通过仅限合成/隔离环境（见下）。**

- 位置：`services/api/`、`services/llm_gateway/`、①侧的插件目录/调用预检/call_id 占用检查改动、`deploy/Dockerfile.api`、`deploy/Dockerfile.gateway`、`deploy/compose.api-gateway.yaml`、`deploy/compose.gateway-octopus.yaml`、`deploy/compose.gateway-provider.yaml`、`deploy/api_gateway_runtime.py`、`tests/test_api_gateway_deployment.py` 等，全部在功能分支 `api/gateway-mvp`，**待合并**（基线 dev 376294d，尚未合入 dev）。根 `pyproject.toml` 的 uv workspace 已包含两个模块。
- 入口与说明：[⑥入口](../../services/api/README.md)、[⑥实现说明](../../services/api/ai-docs/implementation.md)、[⑦入口](../../services/llm_gateway/README.md)、[⑦实现说明](../../services/llm_gateway/ai-docs/implementation.md)、[①接口说明第 6 节](../../services/storage/ai-docs/interface.md)。
- 授权与边界：用户授权继续实现⑥⑦并同步文档；本节只改 Markdown；不改 accepted 语义；③④由各自负责人维护，本分支不碰；契约增补须①负责人与 I 审核（只增不删）。

| 范围 | 已有 | 验证（2026-11-07 取证；出处见各实现说明第 5/6 节） | 未做/限制 |
|---|---|---|---|
| ⑥ API 与插件管理页 | 只读 API、反馈、管理接口与插件目录、轻量页面、受保护配置与命令行、容器与可选 Compose 覆盖 | 真实 Chromium 34 项通过，但用的是内存**假①**；另见下行对真实①的 API 层集成测试；合成容器 20 passed；独立复核确认插件目录无泄漏、不放宽业务 ACL | 真实浏览器↔真实①的页面联调、⑤调度、生产运行 |
| ⑥ 外部动作 | 离线执行/核对切片与私有日志 | 单元/接口测试（见⑥实现说明） | HTTP 上执行/确认/风险重试仍 501（缺④协议）；③⑤反馈只记录 `pending_contract`；无真实日历 |
| ⑦ 网关 | HTTP 服务、调用方①令牌认证、预检、call_id 占用检查、单 worker 私有 journal 与隔离、终态 `llm_call` 写入、受限输出 schema、客户端/命令行/容器 | 源码核对 + 合成回放；独立复核旧 3 项必须修复关闭；合成容器 20 passed | **无真实模型调用**；不是 exactly-once；usage 为估算、记为未知；③④⑤与插件未接入；正文保留期未做 |
| ⑦ Octopus 适配器 | 固定到 `b3c7cd5dd950a79e94533b114619082a542eac66` 的 Manager 聊天接口适配 | 源码核对 + 合成传输层 | 运行时不核对对端版本；无生产 wire 验证 |
| ⑥⑦ ↔ 真实①（API/网关 HTTP 层） | `services/storage/tests/test_api_gateway_integration.py`：真实回环①/⑥/⑦、隔离 PostgreSQL（NATS 开关开启）、合成 provider；管理反馈受理≠生效、API 普通拒绝与管理员撤权、插件目录（只读、重新鉴权、分页）、网关调用方鉴权与真实预检 | 26 passed（12:07 快照；与 `test_llm_preflight` 47 passed/6 skipped 合计 73 passed/6 skipped） | 不含真实浏览器↔真实①、⑤调度、真实模型/Octopus、常驻 manage-worker 容器（管理处理步骤由测试显式驱动）、生产 |
| ①侧新入口 | `GET /v1/management/plugins`、`POST /v1/llm/preflight`、`POST /v1/llm/call-id-check` 及 SDK | 隔离真实 PostgreSQL/NATS 12:07 快照 73 passed/6 skipped；定向 263 passed/6 skipped | 仅在功能分支（待合并），不在 dev 376294d；旧①缺入口时网关 fail-closed |

### 全套验收（PostgreSQL、NATS、备份、部署烟测四个开关全开）

- **当前（`/tmp/ear-67-full-v2.log`，14:09 起、14:26 结束）**：`uv run pytest -q -rs` **退出 0，2067 passed，11 skipped，2 warnings，用时 1001.65 秒（0:16:41）**；四个开关均为 1，镜像为 `ear-{storage,api,gateway}:ear-67-acceptance-fixes-20261107`。11 个跳过：`test_causality_context_guard.py` 5 个、`test_llm_preflight.py` 6 个，原因均为需要认证 PostgreSQL。这是合成/隔离环境（临时 PostgreSQL/NATS、备份往返、部署烟测）的结果，**不是生产验收**，也不含真实浏览器↔真实①、真实模型/Octopus、真实日历。
- **静态检查·当前（`/tmp/ear-67-static-final.log`，14:36）**：`ruff check`、`ruff format --check`、`pyright`、`uv lock --check`、`tools/check_docs.py`（含 `--base origin/dev`）、`git diff --check`、Node UI 测试（37 项）全部退出 0。
- **历史失败（保留；均已修复并重跑）**：
  - 12:35 全套（`ear-67-full-acceptance.log`）：**3 failed，2058 passed，11 skipped，2 warnings，879.49 秒——未通过。** 部署烟测 `[custom]`/`[builtin-octopus]` 漏设 `EAR_STORAGE_TEST_IMAGE`（运行脚本问题）；`test_real_offline_roundtrip` 因旧 storage 镜像里 root 拥有、权限 0600 的 `/app/runtime.py` 对 UID 10001 不可读。已按根因修复（storage 的非密钥运行代码显式 `COPY --chmod=0444`；缺镜像变量时诊断指明变量名，仍 fail-closed），13:53 定向验证通过（部署目标 3 passed、回归 24 passed、静态检查退出 0、镜像源码校验无差异）。
  - 14:26 静态检查（`/tmp/ear-67-static-v2.log`，退出 1）：`ruff format --check` 要求重排 `services/llm_gateway/README.md` 的 Python 示例代码块，其后各项当次未运行。仅格式问题，只改 Markdown，14:36 整条链重跑退出 0。

### 仍未完成与待确认

- ④ 的执行/恢复协议（解除⑥外部动作 501）；③⑤ 反馈契约；外部动作标识的存放位置；插件建议如何进入核心审核与执行——跨模块契约，须对应负责人与①负责人、I 审核。
- ⑦：真实模型调用与生产 wire 验证、对端 Octopus 版本核对、正文保留期与作用域、③④⑤和插件接入网关。
- 生产部署（全套验收已通过，但仅限合成/隔离环境，见上）。

### 谁来决定什么

- **普通工程选择，实现者自行决定、按代码评审调整，不需要用户批准**：⑥管理员认证（部署配置里的令牌绑定 + ①逐次复核可信管理员角色）、⑥执行日志与⑦调用 journal 的单 worker 私有本地文件模型、限流与重试默认值。
- **设计已定、不属于待批事项**：外部动作须“建议→用户审核→执行”；历史范围补处理须用户显式选择；所有大模型调用经⑦并带调用方模块和 `root_feedback_id`；默认只存 metadata。

本节、两个模块的入口与实现说明，以及[①进度](../../services/storage/ai-docs/progress.md)的“当前状态”和文末“⑥⑦ 与①的真实边界”已按上述真实边界刷新，并写入修复后的最终全套结果（`/tmp/ear-67-full-v2.log`，14:26，退出 0）与最终静态检查（`/tmp/ear-67-static-final.log`，14:36，退出 0）（仅 Markdown；历史失败与历史段保留并标明历史）。功能分支合并进 dev 后，删除“待合并”的说法并再复核一次。
