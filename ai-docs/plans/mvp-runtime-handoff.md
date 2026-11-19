---
audience: ai
doc-type: guide
generated-by: Claude (AI)
generated-at: 2026-11-12
based-on: README.md、AGENTS.md、docs/README.md；设计 v1.2、信号补充、插件 MVP、确认索引（均 accepted，只表示目标已采纳，不表示实现完成）；ai-docs/plans/mvp-runnable.md；代码基线 integration/mvp-runnable = 79e8dc3，第二批是其上的工作区未提交改动（索引为空）；最终验收证据为仓库外 /tmp/ear-mvp-second-final.{sh,log,exit,xml} 与 ear-mvp-second-final-frozen-source.json（作者读取，并把当前源码哈希重新对照冻结清单）、/tmp/ear-mvp-coverage-fix/verification.json、/tmp/ear-mvp-stability-final.json，历史失败为 /tmp/ear-mvp-second-unified.json；各切片说明来自作者对源码的只读核对；测试结果作者未复跑；PR 16 状态为 2026-11-12 19:34–19:47Z 用 gh 只读查询（两次，结果相同）；未读取个人音频、真实数据或私有清单；不新增用户授权；未填写或改动任何 reviewed 与 status 字段
---

# MVP 运行时交接：第二批工作区切片（最终验收后口径）

> 工程交接，不是完成报告，也不是产品决策。目标语义以已采纳设计为准，这里只记工作区源码此刻能做什么、没做什么、证据在哪、还差什么。第二批**未提交**，不属于任何 CI 结果。计划与门槛见[跑通计划](mvp-runnable.md)（第 2.5、2.6 节是本文摘要）。

## 1. 口径与边界

- **基线与状态**：`integration/mvp-runnable` 的 HEAD 是 79e8dc3（首批）。第二批是它之上的工作区改动，**未提交、未暂存、未推送，待主 agent 提交**。本次文档任务开始时只读核对：索引为空，44 个已跟踪文件被修改加 39 个未跟踪文件，共 83 个路径（路径数，不是行数）；本次又把 `ai-docs/README.md` 加入修改。
- **PR 16 的 CI 只属于 79e8dc3**：本次用 `gh pr view 16` 只读复验，OPEN、base `dev`、head 79e8dc3，`check` 通过（run 43133804296，2026-11-12T01:38Z 结束）。这次成功**不包含**第二批，**第二批目前没有任何 CI 结果**；工作区的 `.github/workflows/ci.yml` 比 PR 多一步 worker 镜像构建，它在 CI 里还没跑过。
- **授权**：用户授权仍只有[跑通计划第 9 节](mvp-runnable.md)的三项；“补齐整链”只是授权 1 范围内的工作指示，不是新授权。外发、付费调用、真实日历写入、部署、提交、推送、把 `proposed` 当作已采纳都不在其中；提交与 PR 由主 agent 决定。已被已采纳设计回答的事项（如用户确认保护，设计 §6.2）不重新提问；普通工程选择写进 PR 描述由审核者审阅；涉及公共契约的按[计划第 7 节](mvp-runnable.md)走审核。本文不记录新的用户决定。
- **不得宣称**：全 MVP 已完成；已部署或生产更新；Compose 全部署；整天可靠性；任何门槛 G0–G7 通过；第二批已提交或已有 CI。全部验证用合成数据：没有真实录音、GPS、手机数据，没有真实模型，没有真实日历或提醒事项。
- **数字口径**：每个数字写来源、时间（UTC）、范围与开关；不同时间点、不同开关的数字不相加，重叠的测试集不当作独立覆盖。**当前结论只看第 6.1 节的最终验收**，其余是历史或分项。

## 2. 切片总览（工作区；未提交）

| 切片 | 代码位置 | 现在能做 | 没做 / 限制 |
|---|---|---|---|
| ① strict 调度读授权与两个安全问题 | `storage/.../scheduler_access.py`；`service.py` 写入闸门；`GET /v1/scheduler/state`；SDK `RemoteStorageClient.scheduler_state()`（见[① 接口说明](../../services/storage/ai-docs/interface.md)） | 只有完整的可信 core 轮廓才选 strict，对该库所有 `dependency` 身份生效；覆盖不完整一律 403，不给部分图；发布与回放对主起因和全部额外起因逐一核对单个活跃所属方授权。**两个安全问题已关闭**：（1）受限的同模块 `dependency` 身份曾能写 `propagation_record` 注入 pending step，让完整授权的 worker 代发无权的重判，现在每个 `propagation_record` 写入（含回执重放）在变更前都须通过 strict 的完整覆盖检查；（2）缺新 core 来源位的混合来源候选曾被当旧库，能读部分图、能 Publish，现在先识别候选再校验，缺失即拒绝，纯旧库与无关插件重名仍是旧语义 | 只证明已登记图的读/订阅授权，不证明历史追赶、consumer 就绪、模型正确或数据新鲜；不自动授权（A2 未决）；`online` 恒为 null；部署级后端直写不在这道边界内，已被篡改的旧记录不清洗；不是安全证明 |
| ① core 初始化 | `core_deployment.py`；CLI `bootstrap-core`、`init-core-identities` | 第 3 节：只对显式迁移过的**空库**初始化 `mvp-core-v1`，重复执行只验证；六个非管理员身份加四个 consumer 名字预留，凭据只进 0600 文件 | **绝不对旧库自动迁移、升级、接管或修复**；不启动服务，不建 broker 资源，不生成 ⑥ 公开令牌与 worker 配置；`DEFAULT_REGISTRY`、`contracts/`、`schema.py`、根 `pyproject.toml` 与 `uv.lock` 都没改 |
| ⑤ 远程路径 | `ear_dependency/remote.py`、`authority.py`、`journal.py`、`secure_files.py`、`causal_fields.py`、`service.py` | `RemoteDependencyService` 经 `scheduler_state()` 在完整覆盖下调度，覆盖或所属方变化时失败关闭并保留证据，相同 request_id 与请求体持久重放；独立的 `created`（null→1）只在能证明活动版本为不可变 v1 时才忽略；propagation 与 Finish 只读用新鲜的授权读取，不再物化业务图快照。**公开 `records()` 在有界读取前后都要求当前 strict 覆盖，覆盖被撤销或旧库时抛 `PermissionDenied`，不返回空表或部分表**（6.2） | 按钮入口（`feedback_not_implemented`）、P5 历史分配与追赶未做；无分布式租约；没有跨整个调度过程的图改线串行化，只验证有界小图；显式批量大于 1 时仍可能跨 root 合并，主起因仍取首个来源（设计待决 D6） |
| ③ `create_place` 与 ⑥ 结果投影 | `ear_attribution/create_place.py`、`feedback.py`；`ear_api/attribution_results.py`、`business.py` | ③ 对显式 `gps_ids`（1–100 个、唯一、须为 `gps_point`）在一个受围栏批次内建地点并改归属，`expected_version` 须为空，受保护归属被拒；⑥ 受理 `attribution` 命令后返回 `integration: attribution_result_query`，状态由 ③ 写的 `attribution_feedback_result`（可信 core 单写类型）只读投影 | **`reattribute_range` 未做**（③ 写 `unsupported_command` 拒绝回执）；`dependency` 命令（`reevaluate_related`）仍记 `pending_contract`，无消费方；受理后恒为 pending，直到出现回执；阈值未校准 |
| ④ v1.2 | `ear_extraction/selection.py`、`reevaluation.py`、`artifacts.py`、`orchestration.py`、`planning.py`、`segmentation.py`、`suggestions.py` | 新产物为纯 Markdown；每个活动有版本化的 `rule_selection`；按当前规则全集重选与重判；changed/unchanged/not_applicable/failed 都有真实 `FinishReevaluation`；action-v2 的 ④ 侧状态流转（见[④ README](../../services/extraction/README.md)） | 旧活动不回填；网关输出恢复与计划重放、用户确认风险后的新逻辑调用未做；未登记 `rule_selection` 时在调用模型前阻塞 |
| 统一 workers | `tools/mvp_worker.py`、`deploy/Dockerfile.worker`、`deploy/compose.mvp-workers.yaml` | 第 4 节：一个入口、一种私有配置，宿主 ③④⑤ | 没有 Compose 部署；没有默认历史扫描 |
| ⑥④ 外部动作 action-v2 | `ear_api/execution.py`（`ExecutionPump`）、`execution_http.py`、`app.py`、`config.py`、`execution_journal.py` | 第 5 节：可选的 HTTP 接线，默认关闭，需宿主注入可信适配器；在合成适配器上验证 | 命令行不注入适配器，默认仍是旧的 501；没有真实适配器与执行 worker |

### 2.1 仍未做、未覆盖（精确范围；不得宣称全 MVP）

- **反馈与命令**：`dependency` 命令 `reevaluate_related`（按钮入口）没有消费方，仍 `pending_contract`；`reattribute_range` 未做；`attribution`/`dependency` 命令的共享契约与 ⑥ 里的临时结果形状仍待 ③ 与 ① 的负责人及 I 审核。
- **⑤ 与 ①**：P5 历史分配、追赶与 ⑤ 对历史恢复接口的实际调用未做；插件类型的读授权仍只有显式逐类授权，动态/自动授权（A2）未决；没有默认历史扫描；无分布式租约。
- **外部动作**：没有真实适配器（日历、提醒事项）和执行 worker，命令行服务不注入适配器，默认 501；没有真实 provider；没有轮询 `extraction_run` 拒绝原因；没有通用的外部恰好一次。
- **输入与模型**：没有真实录音、GPS、手机数据；模型只有合成 provider（经真实 `GatewayClient` 与网关 HTTP）；没有真实模型调用；③ 的阈值未校准；④ 的网关输出恢复、计划重放和风险确认后的新逻辑调用未做，旧活动不回填。
- **部署与环境**：没有**完整的隔离 Compose 部署与常驻运行**（worker 镜像只做过单容器冒烟）、没有整天运行、没有更新常驻 `ear-dev`；没有操作系统强杀、断电、数据库或 broker 故障注入、多主机租约和持续负载验证；⑥ 页面没有对真实 ① 的浏览器联调；② 在 Compose 上的命令行真跑与常驻掉线恢复未做。
- **契约与登记**：`DEFAULT_REGISTRY` 与 `contracts/registry/data-types.json` 仍没有 `rule_selection`、`attribution_state`、`attribution_feedback_result`，它们只在代码里的可信 core 轮廓中定义；契约改动须 ① 负责人与 I 审核（计划 E1、E2、E3），本文不替他们批准。
- **提交状态**：第二批未提交、未提 PR、无 CI；PR 16 的 CI 只属于 79e8dc3。

## 3. 新空库的初始化顺序（操作者显式执行；本任务没有执行任何一步）

1. 新建空 PostgreSQL 库，用独立项目名与卷；`EAR_STORAGE_DSN` 必须显式给出，不读 `.env`。先执行既有的 `migrate`。
2. `bootstrap-core`：在一个写事务里建模块和 `mvp-core-v1` 登记；输出 `{"status":"created"}`；同一轮廓再执行输出 `unchanged`，只验证不修改。
3. 需要管理页/管理 API 时，既有 `init-identities` 必须**先于**第 4 步（它在 `api` 用户已存在后拒绝签发管理员，新工具不绕开）。**不要**走 Compose `init` 链条里的通用 `bootstrap`（原因见[①部署指南第 9 节](../../services/storage/ai-docs/deployment.md)）。
4. `init-core-identities --output /绝对路径/新建私有目录/identities.json`：叶目录由工具新建（0700；重试时只复核已存在的同一份材料），祖先目录须可信；结果是六个非管理员身份（`adapter`、`attribution`、`extraction`、`dependency`、`api_user`、`llm_gateway`）和四个 consumer 名字预留（`attribution`、`extraction.jobs`、`extraction.completion`、`dependency.changes`）。`extraction` 的存储凭据同时是 ⑦ 的调用方凭据；`llm_gateway` 是独立的 `llm_call` 写入身份；`api_user` 是 API 对 ① 的存储身份，不是 ⑥ 的公开令牌。
5. 为 ③④⑤ 各写一份私有 worker 配置（第 4 节）；④ 另需单独启动并配置 ⑦ 网关。
6. 启动 worker。

要点：**保留所有恢复材料**。工具先落 `intent.json`，再逐个签发并保存回执，最终输出在数据库校验通过后才独占发布；重试逐项复核存量身份与授权，不修复、不重签、不轮换、不覆盖、不删除。签发响应丢失可能留下取不回的令牌，只能由操作者依据 intent、回执和数据库人工处置。预留 consumer 名字不等于 JetStream consumer 已就绪或 worker 在线。仅限本地 POSIX 文件系统，不抵抗恶意 root 或同 UID 进程，不是多机协调。

## 4. 统一 workers

- **入口**：`python tools/mvp_worker.py --config <绝对路径>`；`--healthcheck` 只读本地元数据，不联网。令牌只能写在私有配置里，不接受命令行或环境变量。
- **必填键**：`module`（`attribution`/`extraction`/`dependency`）、`storage_url`、`storage_token`、`consumer`、`journal_namespace`、`state_dir`。consumer 固定为 `attribution`、`extraction.jobs`、`dependency.changes`，与第 3 节的预留一致，不符则拒绝启动。
- **可选键**：`poll_seconds`（默认 1）、`timeout`（35）、`health_max_age_seconds`（300）、`max_records`（5000，④ 的 run 枚举与 ⑤ 的记录读取，超过即失败关闭，不截断）；`max_jobs_per_tick`（默认 1，只作用于 ④ 的 `drain`）；`max_events_per_tick`（默认 1，只作用于 ⑤，范围 1–100）。默认 1 是 05:44–05:49Z 的修复：原默认 32 会让旧触发在后续纠正到达前一直保持打开，延后权威 Finish，并把纠正并入这些旧步骤（6.3）；更大的批次是显式的吞吐取舍，没有放宽任何超时。③ 每个周期只 `poll_once` 一次。
- **③**：`cosine_threshold` 与 `gps_threshold_m` 必须显式给出，没有默认值，也未校准；其他模块给这两个键会被拒绝。**④**：`gateway_url` 与 `model`，直接用 `GatewayClient`，没有 provider 工厂与 provider 环境变量。**⑤**：不需要额外键；没有 `full_visibility` 选项，也不回落到内存。
- **没有默认历史扫描**：订阅一律 `from_beginning=False`，包装层对其他值直接拒绝；不注册历史补处理。唯一的定时开关是 `schedule: {start: <RFC3339 UTC，以 Z 结尾>, every_seconds: <正整数>}`，缺省关闭；起点由操作者显式给出，等于操作者对区间范围的决定。启动只续跑已存在的 `extraction_run`，不批量生成历史。
- **私有文件与状态**：配置须是当前 UID 的 0600 单链接普通文件，祖先链不跟随符号链接；`state_dir` 须已存在且为 0700；本地 `flock` 保证单进程，状态绑定 `(module, consumer, journal_namespace, storage origin)`，令牌轮换允许；不修权限、不删证据；journal 在 `state/journal` 下。健康只表示 pump 在转，不表示业务完成、图新鲜或模型可用。SIGTERM/SIGINT 只在周期与任务之间停止；强杀依靠各模块自己的恢复语义。
- **Compose 叠加与镜像**：`deploy/compose.mvp-workers.yaml` 是 opt-in profile `mvp-workers`，三个服务各一份私有配置（路径变量 `EAR_MVP_{ATTRIBUTION,EXTRACTION,DEPENDENCY}_CONFIG_FILE`，不是密钥值）、独立 state 卷、UID 10001、只读根、丢弃全部 capability、`restart: "no"`，叠加在 `deploy/compose.yaml` 之上。`deploy/Dockerfile.worker` 只装 ③④⑤ 及其 workspace 依赖。`tests/test_mvp_worker_image.py` 是 opt-in 镜像验收（要 `EAR_RUN_API_GATEWAY_DEPLOY_TESTS=1` 和显式本地标签 `EAR_MVP_WORKER_TEST_IMAGE`，缺镜像则跳过，测试不构建也不拉取）。**最终验收里已构建 `ear-worker:mvp-second-final-20261112` 并运行该文件，11 项通过**（镜像导入与运行时加固、配置失败关闭、本地健康、临时真实 ① 加一次投递的有效配置冒烟）；这是单容器冒烟，不是 Compose 部署。工作区 `ci.yml` 新增的镜像构建步骤与变量未提交，没在 CI 跑过。
- **限制**：每个 consumer 一个 worker，本地锁不是跨主机租约；不依赖也不创建任何身份、授权、迁移、核心类型登记。

## 5. 外部动作 action-v2：协议、HTTP 接线与旧 501

- **协议**（`ExecutionPump`，④/⑥ 两阶段，模块内附加 payload，没有改公共契约）：`pending → approved`（审核反馈）→ `executing`（`prepare` 后的 start 反馈）→ `executed | failed | unknown`（适配器结果反馈）；`unknown` 可经查证成功或用户声明已执行（来源记为用户、没有外部标识）变 `executed`；`unknown`/`failed` 经带“重复风险”确认且带两个版本号的显式重试回到 `executing`（同一 `action_key`，attempt 加一，再做一次所有方握手）。④ 侧在 `suggestions.py` 校验精确版本与动作哈希，业务提交后执行方才可动手；“已受理”或排队的 run 都不是执行授权。执行方先在私有 journal 持久化意图，再最多调用一次适配器；未知结果不自动重试、不换标识。
- **HTTP 接线（可选，默认关闭）**：须同时满足 `Settings.execution_action_v2=True`（严格布尔，默认 `False`）、已有的 `execution_journal` 私有绝对路径、宿主在 `create_app(settings, adapter=<可信适配器>)` 显式注入适配器；缺一不启用，推进类路径仍 501。**`ear-api serve` 调 `create_app(settings)`，不注入适配器，所以命令行服务上默认仍是旧的 501**，仓库里也没有真实适配器与真实 provider，v2 只能由宿主代码接入。不起后台线程，没有自动执行 worker：由操作者或客户端显式调用，并另行运行真实的 ④ consumer 推进状态。
  - `GET /v1/actions/{id}/execution`：只读，不查证、不写反馈、不推进；返回两个版本号、所有方建议状态、本地执行状态、反馈投递状态与可用的恢复操作，不披露动作正文、键、哈希或外部标识。
  - `POST .../prepare`：`{expected_version}`，只冻结意图并提交 start 反馈，拒绝未审核、过期或已开始的建议；`POST .../pump`：两个版本号，在同一把 journal 锁内核对后推进**一个**有界回合；`POST .../recovery`：`operation` 为 `verify`/`reconcile`（只查证，查不到也不自动重试）、`confirm_executed`（须 `acknowledge_executed=true`）、`retry`（须 `acknowledge_duplicate_risk=true`），都带两个版本号。三者成功返回 202。
  - 审核仍是 `POST /v1/feedback` 的 `review_suggestion`；`prepare`、`pump`、`recovery` **绕不过审核**：要先有真正 approved 的所有方状态。请求体走既有的有界严格 JSON 解码，封闭模型，不接受 URL、provider、键、动作、结果、回执、外部标识。每次请求都重新向 ① 认证并复核对建议的当前访问；无读授权返回 404（① 的 id 读取隐藏，不是 403），令牌无效或已撤销返回 401。恢复须先让待处理反馈真正被 ④ 应用并同步（先显式 `pump`）。
- **证据校验（EAR-ACTION-REVIEW-001，P2，已修）**：适配器返回值曾可借 `model_construct`、`model_copy(update=…)` 绕过校验。现在 `_evidence` 对实例字典做 `ExternalResult.model_validate(…, strict=True)`，非 `ExternalResult`、带额外字段、结果枚举或字段类型非法、键或哈希不符、`succeeded` 缺真实外部标识，以及任何异常，一律按 `unknown` 处理；只有 `not_executed`，或带 `retry_acknowledged` 的规范化 `unknown`（且已通过确切的所有方握手）才允许 `execute`。旧的离线 `process`/`verify` 共用同一校验。
- **旧 501**：`app.py` 在未启用 v2 时，执行、确认已执行、风险重试仍返回 501 `extraction_execution_recovery_protocol_pending`；旧 `ExecutionService` 是离线终态回执的兼容切片，拒绝 v2 的 journal 与建议状态，**不要**把它接到外部 worker。
- **限制**：单所有者的私有 SQLite journal，仅本地文件系统，不能清空或替换来认领 `executing`；没有通用的外部恰好一次承诺；宿主注入的适配器须声明可靠幂等并自行设有限 I/O 超时，HTTP 回合有界但不会强制取消适配器；所有方不匹配、被拒或缺失的反馈不会变基重执行，核心协议也还不轮询 `extraction_run` 的拒绝原因；只测了注入的 journal 写失败加应用重开，没有强杀、断电或多主机。

## 6. 验证记录

均为合成数据，基线 79e8dc3 加工作区。“开关”指 `EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS`、`EAR_RUN_API_GATEWAY_DEPLOY_TESTS`；PG/NATS 是测试夹具起的临时容器（postgres:17-alpine、nats:2.11-alpine，loopback 随机端口）。作者读取了退出码文件、日志与 junit，没有复跑。

### 6.1 最终验收（当前结论）

| 项 | 内容 |
|---|---|
| 来源 | `/tmp/ear-mvp-second-final.sh`（`set -euo pipefail`）；脚本与源码冻结清单写于 2026-11-12T12:03:17Z，日志与退出码结束于 12:58:36Z；退出码文件为 `0`；HEAD 79e8dc3 加当时工作区，索引为空 |
| 源码冻结 | 冻结 370 个非 Markdown 文件（已跟踪加未忽略的未跟踪）的 sha256；全量结束后脚本输出 `SOURCE_CHANGED []`。本次文档任务开始时作者重新计算，370 个文件哈希与冻结清单逐一相同，没有新增非 Markdown 文件，所以包括 `ci.yml` 在内的源码、测试、配置自验收起没变。Markdown 不在冻结范围 |
| 静态 | `ruff check` 通过；`ruff format --check` 247 个文件；`pyright` 0 errors、0 warnings；`uv lock --check --offline`（Resolved 66 packages）；`check_docs.py --base origin/dev` 通过（44 个 Markdown，列出 13 个待人工复核文件）；`git diff --check` 无输出；`node --test` 两个 UI 测试文件 37 pass、0 fail |
| 镜像构建 | `docker build --pull=false` 四个镜像全部成功：`ear-{storage,api,gateway,worker}:mvp-second-final-20261112` |
| pytest | 四个开关均为 1，`EAR_STORAGE_TEST_IMAGE`、`EAR_TEST_BACKUP_IMAGE`、`EAR_API_TEST_IMAGE`、`EAR_GATEWAY_TEST_IMAGE`、`EAR_MVP_WORKER_TEST_IMAGE` 指向上述本地镜像；`pytest -q --tb=short -rs`：**3659 passed、11 skipped、2 warnings，3198.40 s（0:53:18）**；junit 共 3670 用例，0 failures、0 errors，起于 12:05:17Z |
| 11 个 skipped | `test_causality_context_guard.py` 5 项与 `test_llm_preflight.py` 6 项，均为 `memory` 参数化用例，日志原因 `requires authenticated PostgreSQL`（`-rs`），未进一步归因 |
| 覆盖（junit 用例名） | 按目录通过数：libs 258、adapters 197、api 614、attribution 194、dependency 231、extraction 148、llm_gateway 194、storage 1394、`tests/` 429，合计 3659。其中：三 worker 过程测试 1 项（124.17 s）；八步 `normal`（163.60 s）与 `lookup-recovery`（170.35 s）；`test_mvp_core_runtime.py` 6 项（核心回路 2 项加网络夹具 4 项：accepted-socket 的 TCP_NODELAY、keep-alive 大于 SDK 空闲池、两个 socket 故障注入 `read-unavailable`/`commit-unknown`）；`test_dependency_runtime.py` 18 项（475.2 s）；scheduler 三个文件 28+23+24=75 项；`test_action_protocol_runtime.py` 24 项；`test_mvp_worker_image.py` 11 项；`tests/test_mvp_worker.py` 81 项；`test_core_deployment.py` 77 项。这些是同一次运行里各文件的数字，互相有重叠，不相加 |
| 维度 | 输入合成｜模型回放（合成 provider 经真实 `GatewayClient` 与网关 HTTP）｜外部假（可信幂等的 fake 适配器）｜平台：认证的回环 ①/⑥/⑦ HTTP 在 pytest 进程内，三个 CLI worker 是独立 OS 进程，PG17 与 NATS 2.11 是一次性容器；镜像测试用本地构建的镜像；**不是 Compose 全部署** |
| 边界 | 一次全绿不证明不再偶发；没有真实录音、GPS、手机数据，没有真实模型、真实日历，没有整天运行，没有操作系统强杀、断电或多主机；备份与部署测试用的 compose 可执行文件是本机的 `ear-dev-compose`（`EAR_COMPOSE_BIN`/`EAR_TEST_BACKUP_COMPOSE_BIN` 指向的外部二进制，备份测试用私有 project 名；作者没有审计这个二进制）。作者只读观察 `docker ps`：常驻 `ear-dev-*` 的应用容器（api、storage、gateway、relay、manage-worker）仍用 `dev-028d3f9` 镜像标签，没有被更新 |

### 6.2 最终验收前的两项修复（对应历史失败，见 6.3）

- **`records()` 的覆盖撤销（08:48–09:04Z，`/tmp/ear-mvp-coverage-fix/verification.json`）**：动机是 6.3 的第 3 个失败。`RemoteDependencyService._records_reader` 原来直接返回客户端，覆盖被撤销后公开的 `records()` 读不报错；现在它在同一把锁内，于有界读取前后都校验当前 strict 覆盖，`coverage_denied` 转为 `PermissionDenied`，其他阻塞（如 `scheduler_state_changed`）仍是 `SchedulerBlocked`，旧库同样拒绝。改动文件：`ear_dependency/remote.py`、`tests/test_remote.py`、`test_dependency_runtime.py`（新增 2 个 `records()` 回归，该文件 16 项变为 18 项）。红：真实运行 3 failed、单元 17 failed；绿：`services/dependency/tests` **231 passed**（10.07 s），`test_dependency_runtime.py` **18 passed**（548.67 s，PG+NATS）；范围内 ruff、format、pyright、`git diff --check` 退出 0。三个文件的 sha256 与验证记录及当前工作区一致。这是工程选择（按失败关闭原则，也与测试的预期一致），**不是已采纳设计的新语义**，写进 PR 描述由审核者审阅。
- **网络夹具稳定性（11:44–12:02Z，`/tmp/ear-mvp-stability-final.json`）**：只改了 `services/storage/tests/test_mvp_core_runtime.py`（测试夹具）。（a）loopback 预绑定 socket 显式用 `IPPROTO_TCP`，使被接受的连接带 `TCP_NODELAY`（红：`accepted TCP_NODELAY [0], expected {1}`）；（b）夹具服务端 keep-alive 由 1 s 改为 30 s，大于已安装 SDK 空闲池的 5 s 过期（红：`1 > 5.0` 不成立）；（c）新增连接生命周期回归和两个真实 TCP 的故障注入：池取出连接后、发送请求头前由服务端关闭，读取必须抛 `ServiceUnavailable`，写入必须抛带同一 request_id 的 `CommitUnknown`，各只尝试一次、不重连。**生产代码、SDK 默认值、安全检查、请求超时与 90 s 业务截止都没改，没有加重试。** 独立正式三 case（三 worker 加两种八步）跑了两次，都通过：keep-alive 修改前 3 passed（11:44–11:51Z，461.55 s），修改后 3 passed（11:53–12:01Z，463.43 s；纠正提交到 Finish：`normal` 32.18 s、`lookup-recovery` 36.71 s，均小于 90 s；7 次权威 `llm_call`，三 worker 过程测试 43 条 Outbox 事件）；范围内静态检查退出 0。**根因没有被锁定**：6.3 里第一个失败的底层 `httpx.HTTPError` 被 SDK 抑制，没留下具体原因；夹具上这两项缺陷（socket 选项、过短的 keep-alive）是已确认的，探针也曾记录到 `/v1/batches` 上的 `ConnectionResetError`，但那是另一次带插桩的运行，**不能断言它们就是原先失败的原因**；强制关闭的回归只证明存在这种交错的可能，30 s 是有限的夹具策略，不是对任意暂停或断连的免疫，也不是生产传输修复。

### 6.3 历史（保留，不当作当前结果）

- **07:47–08:40Z 的全量验收失败**（`/tmp/ear-mvp-second-unified.json`，`acceptance_passed: false`）：同样的四开关，镜像标签 `mvp-second-20261112`，**3 failed、3632 passed、11 skipped、2 warnings，3183.08 s**，源码冻结无变化。三个失败：（1）八步 `lookup-recovery` 在 `core.rows` 经 `/v1/read` 抛 `ServiceUnavailable`（15.7 s；底层 `httpx.HTTPError` 被 `from None` 抑制，无法区分超时、断连或其他）；（2）三 worker 过程测试在 90 s 截止时仍未收敛（三个 worker 存活、已有 3 个请求、`rule_selection` 已 done，2 个 artifact 步骤仍 requested 且结果为空，有一个 reevaluate run 还在 running；快照不证明网络或 CPU 根因）；（3）`test_real_blocking_never_claims_empty_graph_success[coverage]`：撤销覆盖后 `records()` 没抛 `PermissionDenied`。后续修复见 6.2，通过证据见 6.1。
- **早期分项（均被 6.1 取代，只留来源与失败；来源为仓库外 `/tmp/ear-mvp-{scheduler-final,created-event-fix,workers-final-fix,action-http,eight-steps,action-evidence-fix}.json`，更早的 8 份为 `/tmp/ear-mvp-{storage-second,scheduler-compat,extraction-v12,dependency-second,api-place,core-deployment,workers,action-protocol}.json`）**：scheduler-final 05:31Z：三个 scheduler 文件 75 passed，`test_dependency_runtime.py` 16 passed（668.37 s），之后 ⑤ 又被改；created-event-fix 05:02–05:12Z：⑤ 单元 202、因果字段 65、`test_mvp_core_runtime.py` 2 passed（584.38 s）；workers-final-fix 05:53Z：三 worker 1 passed（147.88 s）；action-http 04:42Z：⑥+④ 589 passed，动作与 v1.2 运行时 9 passed；eight-steps 06:19Z：2 passed（449.86 s）、外部开关全关的整仓 2615 passed、842 skipped；action-evidence-fix 06:37Z：API 与协议 649 passed、动作运行时 24 passed。01:51–04:25Z 的 8 份摘要里的整仓数字（2157、2178、2958、2461、2497–2499 等）是各自时刻共享工作区的快照，不再引用。
- **`test_dependency_runtime.py` 的 16 项冲突**：02:12Z 报告通过，03:50Z 报告在 strict 轮廓下失败；原因是夹具只在 `DEFAULT_REGISTRY` 上加了一个类型、没走正式 core 轮廓，是夹具与收紧后的 strict 不一致，不是生产旁路；05:17–05:28Z 修夹具后通过，现为 18 项（6.2）。
- **安全两问的红灯**：04:53Z 的探针复现了注入与混合来源降级（Outbox 各 1 条），05:09Z 起转绿。
- **三 worker 多次失败后才通过，没有延长任何超时**：05:05Z 因真实 worker 截止失败（178.37 s），当时对 `created` 展开历史闭包延迟了纠正；之后发现另一层：三个请求和权威 Finish 在截止前其实已存在，但主 root 是更早的改名反馈，纠正 root 只在 `additional_causes` 里，按纠正 root 查请求得到 0 条；原默认批次 32 与对新建后代的延迟展开使旧步骤吞并了纠正。把默认批次改为 1、并把单写者记录读与业务快照分开后，05:52Z 通过。
- **八步第一次运行失败**（146.84 s）：初始积压占用了纠正的 90 s 窗口，截止时只有 `rule_selection` 的 Finish 完成。最终用例改为在纠正前只读地等初始积压结清（测试内的 setup 屏障，既有 helper 的 90 s 不变，另加“纠正到 Finish 少于 90 s”的断言）。**这不是吞吐验收，未结清积压时 90 s 的限制没有被修复。**
- **整仓默认运行 06:14Z 一次失败**（1 failed、2614 passed、842 skipped）：运行 shell 的 umask 077 让既有安全测试里 `mkdir(mode=0755)` 变成 0700；无源码或测试改动，改用 umask 022 重跑通过。
- **证据绕过的红灯**：修复前 3 个回归用例为红（3 failed），修复后转绿；中间的过程性失败不当作独立发现。

## 7. 最终验收与提交状态（主 agent）

| 项 | 状态 |
|---|---|
| 四开关全量验收 | **完成**：见 6.1（3659 passed、11 skipped、2 warnings，3198.40 s，退出码 0，`SOURCE_CHANGED []`） |
| 最终代码上的重跑 | **完成**：原先标“没有重跑”的项都在最终全量里通过：`test_dependency_runtime.py`（现 18 项）、`test_mvp_core_runtime.py`（6 项，含原 2 项）、八步 2 项 |
| worker 镜像 | **完成**：四个镜像构建成功，`tests/test_mvp_worker_image.py` 11 passed；单容器冒烟，不是 Compose 部署 |
| 门槛判定 | **待主 agent**：是否据此把 G1–G4 的合成格记为通过由主 agent 判定；本文不记任何门槛通过，G5–G7 不适用 |
| 提交与 PR | **待主 agent**：第二批未提交，提交 hash 待定。提交时索引为空，须把 44 个已修改文件和 39 个未跟踪文件（含本文与新模块、新测试）一并纳入，否则内容不全；提交后非 Markdown 文件的哈希应仍与冻结清单一致。第二批提交后要有自己的 CI；PR 16 当前的 CI 成功只属于 79e8dc3 |

## 8. 文档与源码的差距（仅报告；除本轮已改的文件外未改）

- **本轮同步的文档**：本文、[跑通计划](mvp-runnable.md)（新增 2.6 与状态记录）、[⑤ README](../../services/dependency/README.md)、[AI 工程上下文索引](../README.md)（新增本文入口）。更早的 ③④⑥① 文档（[③ README](../../services/attribution/README.md)、[④ 实现说明](../../services/extraction/ai-docs/implementation.md)、[⑥ README](../../services/api/README.md)、[① 接口说明](../../services/storage/ai-docs/interface.md)）沿用上一轮同步。
- **仍冲突、本轮未改**（超出本任务的文档范围）：
  - 这些文档仍写“新的四开关全量验收结果未出，或由主 agent 补入交接第 7 节”，结果现在在本文 6.1 与第 7 节：[⑥ README](../../services/api/README.md)（第 203 行）、[⑥ 实现说明](../../services/api/ai-docs/implementation.md)（第 130 行）、[④ README](../../services/extraction/README.md)（第 46 行）、[① 部署指南](../../services/storage/ai-docs/deployment.md)（第 209 行）。
  - [⑦ README](../../services/llm_gateway/README.md)、[⑦ 实现说明](../../services/llm_gateway/ai-docs/implementation.md)、[实施计划](mvp-implementation.md)仍写“功能分支待合并、基线 dev 376294d”，而 8750a83（#11）与 376294d 都是 HEAD 的祖先；⑦ README（第 277 行）、⑦ 实现说明（第 108 行）和实施计划（第 179 行）还把普通工程选择写成“不需要用户批准”，与跑通计划第 7、9 节不一致（本轮重新 grep，仍在）。
  - [④ README](../../services/extraction/README.md) 的 `based-on` 写“本轮负责人授权④直接依赖⑦ GatewayClient”，仓库里没有这条授权的出处（上一轮核对，本轮未重新核对）。
  - [① 状态记录](../../services/storage/ai-docs/progress.md)没有 `bootstrap-core`、`init-core-identities` 与 strict 调度授权。
  - `ear_api/execution.py` 的模块 docstring 仍写“HTTP is NOT wired to it”，与 `execution_http.py`、`app.py` 不符（源码注释，本任务不改源码；本轮重新 grep，仍在第 3 行）。
  - `contracts/registry/data-types.json` 的缺口见第 2.1 节。
