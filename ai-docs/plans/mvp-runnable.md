---
audience: ai
doc-type: guide
generated-by: Claude (AI)
generated-at: 2026-11-11
based-on: README.md、AGENTS.md、docs/README.md；设计 v1.2、信号补充、插件 MVP、工作分解 v1.2、确认索引、ADR-0001（均 accepted，仅表示目标已采纳，不表示实现完成）；ai-docs/plans/mvp-implementation.md 与各模块 README、ai-docs；代码基线 integration/mvp-runnable = origin/dev c5c31f8（用 git rev-parse 复核，未 fetch）及工作区未提交改动（2026-11-11 晚只读核对）；用户在本任务中的授权仅有三项：开始以 MVP 跑起来为目标的实施、可使用 vm-octopus 的音频、在常驻分支上实现并提交并提 PR 由用户审核，本文不记录也不假定用户的其他澄清或决定；主 agent 2026-11-11 对 dev CI 日志的查验与对 vm-octopus 的只读盘点（交接材料，本文作者未复验）；各模块任务状态文件 /tmp/ear-mvp-attribution-state.json、/tmp/ear-mvp-extraction-followup.json、/tmp/ear-mvp-dependency-state.json（非仓库文件，其中的测试结果作者未复跑）；2026-11-12 增补依据：/tmp/ear-mvp-acceptance.{sh,log,exit}、/tmp/ear-mvp-unified.{log,exit}、/tmp/ear-mvp-adapter-{current,fixed}.{log,exit}、/tmp/ear-mvp-attribution-{recovery-red,real-final}.log（均非仓库文件，作者读取）及作者本次的定向复跑，见第 2.2 节；2026-11-12 journal 硬化增补依据：/tmp/ear-mvp-journal-fix.json、/tmp/ear-mvp-precommit-review.json、/tmp/ear-mvp-journal-{attribution,runtime,default,docs,ruff,format,pyright}.{log,exit}、/tmp/ear-mvp-journal-{original-repro,red,safety-first,fixed-repro}.log、/tmp/ear-mvp-final-local.log（均非仓库文件，作者读取并核对）、services/attribution 的 secure_files.py、transactions.py、tests/test_journal_safety.py 源码及作者的定向复跑，见第 2.3 节；未填写或改动任何 reviewed 字段
---

# MVP 跑通实施计划

> 工程执行计划：排顺序、定门槛、分任务。**不是完成报告，也不是产品决策。** 目标语义引用已采纳的[设计 v1.2](../../docs/design/design-v1.2.md)等，不在此复述；代码与契约以仓库实际为准。标为"待实现"或"部分"的项，在对应 PR 合并并有证据之前不得写成完成。
> 授权：本计划能追溯到的用户授权只有三项——开始以 Echo-As-Record MVP 完整跑起来为目标的实施；可使用 vm-octopus 上的音频；在常驻分支 `integration/mvp-runnable` 上实现并提交，提 PR 由用户审核。除此之外，本计划**不记录、也不假定**用户的其他澄清或决定（包括谁担任 ① 负责人与 I、普通工程选择是否无需逐项批准、哪些动作无需再请示）：这些要么以仓库规则为准（[根 README](../../README.md) 第 5、7、9 节与 [AGENTS](../../AGENTS.md)），要么列为需要用户确认的事项（第 9 节）。外发、付费调用、真实日历/提醒事项写入不在这三项授权内，做之前必须取得用户确认（第 6 节）。PR 按根 README 第 9 节规则面向 `dev`，不自动合并。该授权覆盖开始实施和已授权范围内的工程增补，不等于逐项批准任何 `proposed` 文档或跨产品决策（第 7 节）。完整前端不纳入 MVP 出自[工作分解第 5 节](../../docs/design/work-breakdown-v1.2.md)（MVP 内只有轻量插件管理页）。分工以各任务的独占文件范围为准：文档由文档方维护，代码与测试由其他实施方负责。

## 1. 目标与完成标准

"跑起来"指在可重复的隔离部署中，按第 3 节门槛依次通过，缺一不可：

- [工作分解 v1.2](../../docs/design/work-breakdown-v1.2.md)第 2 节 I 的完整八步；
- 同文件第 3 节的动态插件与跨模块验收矩阵（用合成示例插件）；
- 恢复与故障：重启、重复投递、提交结果未知、模型结果未知、外部动作结果未知；
- 用真实数据连续跑完一整天。

工作分解要求八步、整天可靠性和新增验收共同通过，八步只是其中一段。本计划只排顺序，不削减范围。**不纳入 MVP**：完整前端（时间线、地图、人物、规则页面，移动端 App 等非插件客户端），也不为它排任务；八步第 4、5、7 步用 API/测试客户端验收。唯一的客户端例外是 ⑥ 的轻量插件管理页（已在 dev，见工作分解 ⑥ 一节），仍须对真实 ① 联调。其余不做项见工作分解第 5 节。

## 2. 基线（以 c5c31f8 的 git 记录、目录和模块文档为准）

`integration/mvp-runnable` 的 HEAD 等于 `origin/dev`，均为 c5c31f8（本文修订时用 `git rev-parse` 复核；未 fetch，`origin/dev` 是本地已知引用）；主 agent 查验的 dev CI 运行，head 也是 c5c31f8。该集成分支是当前工作基线；PR 按仓库规则面向 `dev`。下表只按 c5c31f8 的 git 记录；工作区里后来出现的 ②③④⑤ 未提交改动没有混入下表，单独记在第 2.1 节并标明来源与验证状态。本地 `dev` 分支指针停在 62439c7，不能当基线。下表的状态来自 git 记录、目录和模块文档，作者没有运行测试，没有任何一项是生产验收。

状态词：**已有**＝代码在 c5c31f8，且文档记录过合成或隔离环境验证；**部分**＝有代码但缺关键能力；**待实现**＝无代码或只有草案。

| 模块 | 状态 | 实际情况 | 距跑通的缺口 |
|---|---|---|---|
| ① 存储与事件 | 已有（隔离） | 独立服务、PostgreSQL、NATS Outbox/Relay、身份与读授权、动态插件管理后端、历史恢复、完成记录/多起因/`llm_call` 存储；[隔离 Compose](../../deploy/compose.yaml)只含 postgres、nats、storage、relay、manage-worker | `init-identities` 只签发 api 管理员和 manage-worker，②③④⑤⑦ 没有正式签发流程（② 测试靠 `AuthOperator`）；核心类型重判与强制依赖能力缺省 false，只有 `attribution_completion`、`extraction_completion` 声明了结果能力，`rule_selection` 未登记（[登记表](../../contracts/registry/data-types.json)）；流保留/清理、生产部署未做 |
| ② 适配器 | 已有 | 旧 Ear Evidence 转原始数据；远程单轮/常驻扫描、`CommitUnknown` 重放；2026-11-11 本机夹具联调通过，真实旧 Ear 数据经 `Runner` 写入临时①（段数与耗时见[实现说明 7.3](../../services/adapters/ai-docs/implementation.md)，本文不复述） | Compose 上命令行真跑与常驻掉线恢复未做；adapter 正式身份；GPS 稀疏（手机侧）；时间未听录音核对；流容量与消费吞吐须按真实量估算（事件总量与远程消费速率见实现说明）；现场旧 Ear 缺列表接口（404），而 c5c31f8 的 ② 命令行没有按已知 ID 显式导入的入口（见下方盘点记录，T4、E8；工作区已有未提交的实现，见 2.1） |
| ③ 归属 | 待实现（c5c31f8）；工作区有未提交首片（2.1） | dev 上 `services/attribution` 只有 `.gitkeep`。分支 `origin/attribution/initial-implementation`（617fbc5，基于 48d3c51，未合入 dev）只有事件处理框架，没有匹配、未命名人物、未知地点、参考、纠正确认等业务；其 P1–P7 交接约定全部"待确认" | c5c31f8 上全部业务；工作区首片的缺口见 2.1 与[③实现说明](../../services/attribution/ai-docs/implementation.md) |
| ④ 活动提取 | 部分（v1.1 演示基线，c5c31f8）；工作区有未提交的安全与完成协议首片（2.1） | 手动/定时提取、规则、产物、建议状态机、重试与恢复；只有 `ear-extraction-demo`、`ear-extraction-web-demo`，没有连远程①的运行入口；#13 起改用⑦的 `GatewayClient`（[④ 实现说明](../../services/extraction/ai-docs/implementation.md)） | c5c31f8 上：仍用 `[[person:ID]]` 引用标记和粗粒度依赖；无 `rule_selection`，不用 `fields`；历史规则出新版本时以 `historical_rule_dependency_unsupported` 阻塞，设计要求用当前规则。c5c31f8 上还有三个 T1 缺口，工作区首片已处理（2.1，未提交）：`call_id` 每次新生成且不持久化；不传 `request_event_id`/`additional_causes`（depth>0 的重判会被⑦拒绝）；`extraction_run` 占用请求 event_id，使 Finish 无法写入（主 agent 发现；T1 处理）。完整 v1.2 对齐、远程运行入口和部署仍未做 |
| ⑤ 依赖分析 | 待实现（c5c31f8）；工作区有未提交的内存实现首片，不支持远程①（2.1） | c5c31f8 上无代码；只有[模块入口](../../services/dependency/README.md)、[实现设计草案](../../services/dependency/ai-docs/design.md)（proposed）、[接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)（proposed） | c5c31f8 上全部实现；工作区首片只在内存①上验收，远程①、身份与读授权 A1–A4（待①）、按钮入口所需的③字段（待确认）等见 2.1 |
| ⑥ 客户端与 API | 部分（已在 dev） | 只读 API、反馈入口、管理接口、插件管理页、离线外部动作切片（[⑥入口](../../services/api/README.md)） | HTTP 上外部动作执行/确认/风险重试返回 501（缺④协议）；③⑤ 命令只记 `pending_contract`，没有消费方；无真实日历适配器与执行 worker 接线；浏览器检查只对假①，未对真实①+⑤ |
| ⑦ 大模型入口 | 部分（已在 dev） | 网关、journal、`llm_call` 写入、固定提交的 Octopus 适配器、客户端（[⑦入口](../../services/llm_gateway/README.md)） | 无真实模型调用与生产 wire 验证；usage 记未知；限额/重试无部署入口；③⑤插件未接入；④的恢复与重判关联未接 |
| 集成/部署/数据 | 部分 | Compose 只有①存储切片，另有 opt-in 的 `compose.api-gateway.yaml`（⑥⑦）；`fixtures/`、`tests/e2e/` 只有 `.gitkeep` | ②③④⑤ 没有服务定义；没有 D1 合成数据和八步 e2e 驱动 |

- **测试历史（不是本次结果）**：主 agent 于 2026-11-11 用 `gh run view 43016187481 --log` 独立查验了 dev 的 CI 记录：head c5c31f8，pytest 2178 passed、11 skipped、2 warnings，Node 37 项，`EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS`、`EAR_RUN_API_GATEWAY_DEPLOY_TESTS` 四个开关均为 1。这组数字不是用户提供的，来源是那次 CI 日志；本文作者没有读该日志，只核对了 `.github/workflows/ci.yml` 确实把这四个开关设为 `"1"`，本轮也没有重跑。仓库文档里没有这组数字。它只说明那一次 CI 的结果，不是任何任务的通过证据（既有文档中的 37 项是 ⑥ 页面纯逻辑测试）。[实施计划](mvp-implementation.md)里的 1503、2067 等是更早基线的历史记录，不要混用。
- **常驻实例**：交接材料称常驻 `ear-dev` 在 028d3f9、不自动更新（本轮对话里没有这条说明的原始出处，作者未核实）。028d3f9 是 #11 的分支提交，不是 c5c31f8 的祖先，与 c5c31f8 相差 32 个文件，含 ② 的远程命令行、④ 改用 GatewayClient、⑥ 的反馈入口。本计划任何任务都不得更新、重启或迁移它，除非用户另行指示；它的行为也不能当作 c5c31f8 的验证。
- **真实输入只读盘点（主 agent，2026-11-11；作者未复验）**：vm-octopus 上存在相关音频与派生数据（本文不记文件名、数量、人数或内容），本轮没有复制或外发任何内容。现场旧 Ear（dev，eef64ce；旧 Ear 是 ② 读取的上游服务，不是常驻 `ear-dev`）没有 `GET /internal/api/captures` 列表接口，请求返回 404。② 的 `HttpEarSource` 正是在 `/internal` 基址下请求 `/api/captures`，`ear-adapter` 的 `dry-run`、`once`、`run` 都先靠它枚举，所以现场目前无法按现有命令导入或盘点。命令行没有按已知 ID 显式导入的入口（`Runner.run_once` 总是先列表）。出路有二：(1) ② 增加已知 ID 显式导入（E8，工程增补；工作区已有未提交实现，见 2.1，尚未对现场验证）；(2) 另经授权给旧 Ear 补源接口，[实现说明第 3 节](../../services/adapters/ai-docs/implementation.md)记录该接口在旧 Ear 的未合并分支上。
- **文档陈旧点（仅报告，本任务不改）**：`mvp-implementation.md` 第 0、6 节及 ⑥⑦ README 仍写"功能分支待合并、基线 dev 376294d"，而 git 记录显示 #11 已合入；② 实现说明第 3 节写"本机运行中的旧 Ear 目前就是 `dev-capture-list` 分支"，可能与上面盘点到的现场不一致（也可能是不同机器），待②文档方核对。本计划以 git 记录和上面的盘点为准；这些文件不属于本任务的两个文件，作者没有改。

### 2.1 工作区未提交进展（2026-11-11 晚核对；不是基线，不是完成）

> 2026-11-12 更新：本节保留 2026-11-11 晚的时点记录。其中“仍在修复”“根接入进行中”“全绿待统一检查”“没有真实存储运行”等说法，以第 2.2 节为准。

本节记录 c5c31f8 之上、工作区里尚未提交的 ②③④⑤ 改动。来源是各模块的任务状态文件（`/tmp/ear-mvp-attribution-state.json`、`/tmp/ear-mvp-extraction-followup.json`、`/tmp/ear-mvp-dependency-state.json`，非仓库文件）和作者对实际代码、测试文件的只读核对：读源码；用 `pytest --collect-only` 核对项数；对 ③⑤ 比对了任务记录中的文件 sha256，全部与当前文件一致。作者**没有运行测试、ruff、pyright，没有运行 `uv sync`，没有启动任何服务**。这些改动**未提交、未提 PR**，工作区由多个 agent 共用，随时在变；下表的通过数是各任务当时自己的运行结果，不是当前状态，**更不是门槛通过**。

| 模块 | 工作区里有什么 | 验证（来源、维度） | 没做 / 限制 |
|---|---|---|---|
| ② 适配器 | 按已知 ID 显式导入：`ear-adapter dry-run/once/run` 可重复传 `--capture-id`，`Runner.run_once(capture_ids=…)` 不再先调列表接口（ID 先校验、去重并冻结，请求与响应的 capture id 须一致，校验错误不回显正文）；原位音频定位加固：`InboxAudioLocator` 固定目录深度、逐级 `dir_fd` 加 `O_NOFOLLOW`，拒绝符号链接、硬链接与非常规文件，媒体类型和扩展名白名单，大小与 SHA-256 须匹配，读取期间文件变化即拒绝；`HttpEarSource` 禁用环境代理与重定向，错误不带响应正文 | 新增 `test_selection.py`、`test_locator_safety.py`、`test_source.py`，改动 `test_cli.py`、`test_runtime.py`；`services/adapters/tests` 收集到 197 项（作者只收集，未执行）。④ 任务记录（2026-11-11T20:05Z）曾观察到 `test_source.py` 有 26 项失败，当时 ② 正被并行修改，**那是历史观察，不是当前结果** | **据任务交接，协调者正在修 ② 的 locator fd 处理与并发路径检查，并把 ⑤ 接入根配置；在其完成并统一检查前，不得用任何中间输出宣称 ② 全绿。** 仍未做：Compose 上命令行真跑、常驻掉线恢复、adapter 正式身份；显式 ID 导入没有对现场旧 Ear 验证（本轮没有读取、复制任何真实数据）；locator 只在可信的只读视图下成立，不抵抗挂载替换 |
| ③ 归属 | 首片：声纹余弦与 GPS 球面距离匹配（阈值显式配置）、未命名人物、未知地点、纠正/确认/增删参考/改名、用户确认保护、`FinishReevaluation` 完成协议、按实际读到的候选登记依赖、远程写入的持久重放日志 | 任务记录（2026-11-11T19:10Z）：模块本地 67 项（内存 `MemoryStorage`）通过，作者收集到 67 项，未复跑。输入合成｜模型不适用｜外部不适用｜平台替身 | **`DEFAULT_REGISTRY` 未接入**：缺 `attribution_state`、`attribution_feedback_result` 两个系统记录类型，`attribution` 未打开强制依赖与重判能力，`AttributionService.business()` 在默认登记表上直接失败（作者只读复核）；**真实验证未做**：没有真实 PostgreSQL/NATS、没有运行中的 ① 服务、没有真实声纹或 GPS；`create_place`、`reattribute_range` 未实现（显式拒绝）；见[③实现说明](../../services/attribution/ai-docs/implementation.md) |
| ④ 活动提取 | 首片：发送模型调用前把 `call_id` 与请求指纹持久化到 `extraction_run`（版本 CAS），可能已执行的调用停止、不重发，`reconcile` 只补审计；传 `request_event_id`、额外起因、根源与深度；重判 run id 与请求 event_id 解耦；对 artifact 重判提交 `FinishReevaluation` 与 `extraction.completion` 标记，无变化不新增业务版本；`worker.tick` 在远程订阅下拉取一条投递 | 任务记录（2026-11-11T20:05Z）：extraction 单元测试 84 项通过（作者收集到 84 项，未复跑）；真实 PostgreSQL/NATS 加回环 ⑦ 网关与 ① HTTP 的运行 17 项通过，分 8 组不相交的选择运行，**不是一次连续的完整运行**（作者收集到 17 项，未复跑）。输入合成｜模型回放（进程内合成 provider）｜外部不适用｜平台真实（测试夹具起的临时 PostgreSQL/NATS 与 ① HTTP）。同一记录的全仓结果为 1771 passed、26 failed（全在 ②）、648 skipped，全仓静态检查与 `check_docs` 当时失败，**均为历史结果** | **完整 v1.2 没做**：纯 Markdown、`rule_selection`、按当前规则重判、字段依赖。也没做：网关输出恢复与业务计划重放、风险确认后的新调用、终态 failed/not_applicable 的 Finish、旧记录迁移、远程运行入口与部署、真实模型。`DEFAULT_REGISTRY` 没变，测试夹具显式开启了 artifact 重判能力 |
| ⑤ 依赖分析 | 首片：`DependencyService`（只接内存①）：历史门控、有界的当前 DAG 闭包、就绪与等待上游、起因合并与追溯、以 `get_reevaluation_result` 判完成、失败/受保护/不可用阻塞、从存活记录重建 worker、`CommitUnknown` 故障注入、限额失败关闭；传播记录用私有的版本化格式。**已不是纯草案，但仍是首片** | 任务记录（2026-11-11T19:52Z）：模块本地 34 项内存测试通过，ruff、format、pyright 通过（作者收集到 34 项，未复跑）。输入合成｜模型不适用｜外部不适用｜平台替身（内存） | **不支持远程①**：要求显式 `full_visibility=True` 且只接受 `MemoryClient`，远程或非内存客户端被拒绝（没有传输层 `request_id`，不伪造）；`MemoryStorage` 不持久。未做：`RemoteStorageClient`、①侧调度权威与完整图投影、身份签发与动态授权/生命周期、真实 PostgreSQL/NATS、命令行与后台轮询、按钮反馈入口、与真实 ③④ 的联调、多 worker 租约、部署。传播记录只是私有实现格式，[接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)仍是 proposed。根配置尚未包含 `services/dependency`（pyright 的 include 里原本就有），**据任务交接，接入由协调者进行中** |

- **共同限制**：以上全部是工作区未提交代码。②③④⑤ 都没有门槛通过；③④⑤ 之间没有联调；v1.2 的其余能力和完整部署（Compose 服务定义、身份签发、八步 e2e、插件示例、整天运行）都没做。
- **统一检查**：全仓统一检查（ruff、format、pyright、pytest、`check_docs`、`git diff --check`）由协调者在 ② 的修复与 ⑤ 的根接入完成后执行；结果出来前，本文不记录"全绿"。作者本轮运行的检查见第 10 节。（2026-11-12：已有整体验收的日志，见第 2.2 节。）
- **不引用的结果**：各任务记录里的全仓 pytest、ruff、pyright 失败，发生在其他模块并行修改的中间状态，本文不当作当前结果引用，也不据此判断任何一个模块的状态。

### 2.2 2026-11-12 验收证据与状态更新（只追加；工作区仍未提交，不是门槛通过）

本节不改写 2.1 的时点记录，只补充此后的证据；2.1 里与本节冲突的说法以本节为准，原文保留作历史。证据来自仓库外日志（`/tmp/ear-mvp-*`，非仓库文件，作者读取，没有重放整次验收）和本次文档任务运行的定向命令（UTC，2026-11-12）。

**声明（没有变）**：改动仍在工作区，**未提交、未提 PR、未合入 `dev`**（已暂存的约 60 个文件只是索引状态，不是提交）；**未部署**——验收只用本地构建的临时镜像和测试夹具起的临时容器，不是 Compose 部署验收，本文也没有核对它与常驻 `ear-dev` 的隔离；本节列出的验证**没有使用真实录音、GPS、手机数据，没有真实模型调用，没有真实日历或提醒事项写入**（[② 实现说明 7.3](../../services/adapters/ai-docs/implementation.md)里 2026-11-11 的旧 Ear 真实数据试跑是另一件事，不是本节证据）。演练维度：输入合成｜模型回放或合成替身｜外部不适用｜平台为内存，或测试夹具起的临时 PostgreSQL/NATS/① HTTP。不据此记任何门槛通过。

**A. 四开关整体验收**（仓库外 `/tmp/ear-mvp-acceptance.sh`，退出码文件为 0；脚本文件时间 2026-11-11T23:50:55Z，日志结束 2026-11-12T00:12:56Z，由日志尾的耗时倒推 pytest 会话起点约 23:51:29Z）

| 阶段 | 结果（读自 `/tmp/ear-mvp-acceptance.log`） |
|---|---|
| 静态 | `ruff check`、`ruff format --check`（209 个文件）、`pyright`（0 errors）、`uv lock --check --offline`、`check_docs.py --base origin/dev`（43 个 Markdown，列出 8 个待人工复核文件）、`git diff --check`（无输出）、`node --test` 两个 UI 测试文件（37 项）均通过 |
| 构建 | `docker build --pull=false` 三个镜像（`deploy/Dockerfile`、`Dockerfile.api`、`Dockerfile.gateway`，标签 `mvp-first-slice`）成功 |
| 测试 | 四个开关（`EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS`、`EAR_RUN_API_GATEWAY_DEPLOY_TESTS`）均为 1，镜像变量指向上述本地镜像：`pytest -q --tb=short` 为 **2485 passed、11 skipped、2 warnings，1287.40 s** |

- 11 个 skipped 的原因日志没有给出（没有 `-rs`），未归因。
- **覆盖边界**：`tests/e2e/test_core_memory_loop.py`（23:57:05Z）、`services/attribution/src/ear_attribution/service.py`、`transactions.py`、`services/storage/tests/test_attribution_runtime.py`（均 23:58:35Z）的修改时间晚于脚本启动、早于日志结束；它们正是 `git status` 里带未暂存改动的那 4 个代码与测试文件。按 pytest 在会话开始时导入测试与被测模块推断（未另行证明），整体验收跑的是它们修改前的内容（当时 `test_attribution_runtime.py` 为 3 项，现为 5 项）。所以这次 2485 项和静态检查**不是这 4 个文件当前内容的证据**；其余代码和测试文件的修改时间都不晚于 23:49:50Z，验收之后到本次核对之间除这 4 个文件外没有其他代码文件被改。
- **2026-11-12 更新（只追加，上一条保留为当时的核对）**：此后 ③ 的 journal 又做了安全修复，`transactions.py` 再次修改，并新增 `secure_files.py` 与 `test_journal_safety.py`（第 2.3 节）。因此这次 **2485 passed、11 skipped、2 warnings 只是 journal 修复之前的基线，不是修复后的完整验收**；修复后没有重跑四开关全量，修复后的数字见第 2.3 节，两组开关不同，不能相加、相减或互相替代。

**B. 对当前内容的定向复核**（本文作者运行，`-p no:cacheprovider`，不全仓运行 pytest）

| 范围 | 结果 |
|---|---|
| 上述 4 个文件的 `ruff check`、`ruff format --check`、`pyright` | 通过 |
| `tests/e2e/test_core_memory_loop.py` 加 `services/attribution/tests`（无 Docker） | 69 passed（2 + 67） |
| `services/storage/tests/test_attribution_runtime.py`（两个开关，临时容器） | 5 passed，29.61 s；任务日志 23:59:11Z 同为 5 passed（31.15 s），23:58:09Z 新增的 2 项在实现前为红（2 failed） |
| `services/storage/tests/test_extraction_completion_runtime.py`（两个开关，**一次连续运行**） | 17 passed、0 skipped，216.48 s（00:37:07Z–00:40:44Z） |
| `services/adapters/tests`（无开关） | 194 passed、3 skipped（需两个开关的 `test_storage_integration.py`） |

**C. 按模块更新**

- **② 适配器**：`test_source.py` 的 26 项失败（④ 任务记录 20:05Z，当时 ② 在并行修改）已不再出现。日志：23:22Z `/tmp/ear-mvp-adapter-current.log` 为 189 passed、5 failed、3 skipped，5 项全在 `test_locator_safety.py`（fstat 失败后 fd 未关闭 1 项、已打开的目录被搬出根仍被读取 4 项）；23:43Z `/tmp/ear-mvp-adapter-fixed.log` 为 194 passed、3 skipped、退出码 0；23:49Z 无开关全仓运行 `/tmp/ear-mvp-unified.log` 为 1842 passed、651 skipped、退出码 0；整体验收也含本模块，其间未修改。26 项与 5 项失败的历史保留在 2.1 与 ② README，不删。locator 现在关闭失败路径上的 fd，并在列目录与读哈希前后复核祖先目录链；仍只适用于可信只读视图，不是文件系统锁，没有记录显示显式 ID 导入与加固后的 locator 在现场旧 Ear 上跑过。
- **③ 归属**：（a）**journal 启动恢复不再仅靠总线重投**：`AttributionService.subscribe()` 先调用 `Transactions.recover_pending()`（默认最多 1000 条），把私有 journal 里未决的写入按保存的请求原样重放，再订阅；重放抛异常则保留并让 `subscribe()` 失败。原因是提交已成功而回执丢失时，重投事件不会再进入处理函数。只有 `test_attribution_runtime.py` 的 2 项覆盖（进程内重开 journal，**不是 OS 强杀**），模块单元测试不覆盖；没有常驻入口，超出 1000 条时没有显式恢复入口。（b）真实存储运行：该文件 5 项（入库→归属→确认→重判完成→重复投递；提交结果未知的原样重放；启动恢复）在临时 PostgreSQL/NATS/① HTTP 上，登记表是夹具里显式构造的，身份由 `AuthOperator` 签发，不是正式流程。（c）**首片实际边界**：本次只读脚本复核，`DEFAULT_REGISTRY` 仍没有 `attribution_state`、`attribution_feedback_result`，`attribution`、`artifact` 的重判与强制依赖能力仍为 false，核心登记表和 `contracts/` 没有改；`create_place`、`reattribute_range` 未做；阈值未校准；无正式身份签发与部署入口。
- **④ 活动提取**：`test_extraction_completion_runtime.py` 的 17 项现在有一次连续运行（上表）；整体验收（该文件与 ④ 源码在其间未修改）也在 2485 项内。这取代 2.1 与 ④ 文档里“分 8 组、不是一次连续”的限制，分组记录作为历史保留。仍是进程内合成 provider 与回环 ⑦ 网关；完整 v1.2、远程运行入口、真实模型都没做。④ 的 README 与实现说明本次未改。
- **⑤ 依赖分析**：根 `pyproject.toml` 已含 `services/dependency`（workspace、dev、`tool.uv.sources`、`testpaths`，已暂存），34 项在整体验收的收集范围内（其间未修改）。**仍只接受 `MemoryClient`，`RemoteStorageClient` 被拒绝**；与真实服务没有联调。
- **②③④⑤ 合成联调** `tests/e2e/test_core_memory_loop.py`（2 项，参数为是否重建 ⑤ 的 scheduler）：真实的 ② `Runner`、③ `AttributionService`、④ `ExtractionService`、⑤ `DependencyService` 在 `MemoryStorage` 与本地构造的登记表上跑一次纠正闭环：显式 capture id 入库（不枚举）、重复导入为 `AlreadyIngested`、两条规则与手动提取、纠正反馈（经 ⑥ `FeedbackInput` 规范化）成功并受保护、⑤ 发出 2 个重判请求、结果 changed 与 unchanged 各一、不变的产物版本不变而变化的产物加一、旧建议被替代且新建议为 pending、重复投递不再调用模型。模型是确定性合成替身，**不经 ⑦ 网关与 HTTP**；⑥ 只用到反馈输入模型，没有 HTTP 鉴权与读取接口；没有外部动作执行；没有 v1.2 `rule_selection`。因此**不是完整八步**，也不代替 G1–G4。

### 2.3 2026-11-12 ③ journal 安全硬化与修复后验证（只追加；工作区仍未提交，不是门槛通过）

本节不改写 2.1、2.2。依据是仓库外的提交前审查与修复记录（`/tmp/ear-mvp-precommit-review.json`、`/tmp/ear-mvp-journal-fix.json` 及同前缀日志，非仓库文件）和作者对源码的阅读；记录里的文件哈希、修改时间与日志时间由作者核对一致，每个数字另标出作者是否复跑。**声明不变**：改动仍在工作区，未提交、未提 PR、未合入 `dev`、未部署；没有真实录音、GPS、手机数据，没有真实模型调用与真实日历写入；`contracts/`、核心登记表、根配置、锁文件和 reviewed 字段都没有改。

**问题与范围。** 提交前审查（2026-11-12T00:50Z）报告一项 P2：journal 目录可经可写的非 sticky 祖先被改名替换，`sqlite3.connect` 随即打开新路径，而 `worker.lock` 仍锁着已脱离的旧目录；复现时在目录外创建了权限 0644 的库并写入 1 条待重放项。前置条件是不可信方能改名 journal 目录，整条祖先链可信时未复现。修复任务（2026-11-12T01:03Z，记录状态 `scoped_fix_complete_tests_passed_not_committed`）只写了三个文件，其他模块、文档、根配置、锁文件都没动，Git 索引哈希前后相同（作者本次核对 `.git/index` 的 sha256 仍与该记录一致）：

| 文件（`services/attribution/` 下） | 状态 | sha256 前 12 位（与记录一致，作者核对） |
|---|---|---|
| `src/ear_attribution/secure_files.py` | 新增，**未跟踪** | d36993a03f11 |
| `src/ear_attribution/transactions.py` | 已暂存文件上的未暂存修改 | 7ccf404d1480 |
| `tests/test_journal_safety.py` | 新增，**未跟踪**（76 项） | 06cfd0a759df |

**要求与行为（以源码为准；细节与测试覆盖见[③实现说明第 15 节](../../services/attribution/ai-docs/implementation.md)）**

- **仅 Linux 本地文件系统**：SQLite 经 `file:/proc/self/fd/<目录 fd>/commits.sqlite3?mode=rw` 打开，依赖 `/proc/self/fd`、本地 POSIX 属主与权限、`flock`。不是多机协调，不保证 NFS 或断电后的持久性。
- **祖先与身份**：从 `/` 起逐级以 `O_NOFOLLOW`、`O_DIRECTORY` 加 `dir_fd` 打开，不解析符号链接，拒绝 `..`。每级祖先须属 root 或当前 euid，且不得 group/world 可写；唯一例外是 root 所有的 sticky 目录（如 `/tmp`），用户自己拥有的 sticky 祖先也拒绝。目录与 DB 以 fd 钉住并记录 `(st_dev, st_ino)`，本地读、事务前后、提交前后、连接后执行 SQL 前，以及正常与启动恢复路径的**每次远程写入之前**都重新验证；对 `worker.lock`（与旧 worker 互斥）和实际 DB inode 都加 `flock`，替换 `worker.lock` 绕不过 DB 锁。
- **0700 / 0600**：终点目录须 euid 所有且权限恰为 0700；DB、`worker.lock` 与回滚旁文件 `-journal` 须 euid 所有、常规文件、权限恰为 0600、硬链接数 1。创建时请求 0700/0600，仍受进程 umask 限制。**权限或属主不符的既有目录与文件一律失败关闭，不 chmod、不改 umask、不删除恢复证据。**
- **已有 WAL 拒绝，不转换、不删除**：DB 头部为 WAL 格式，或存在 `-wal`、`-shm` 旁文件，在 SQLite 连接之前就报错；不做模式转换、checkpoint 或旁文件清理，DB 与旁文件原样保留，由运维人员受控恢复或迁移。仍使用 rollback `DELETE`、`synchronous=FULL`、`trusted_schema=OFF`，`mode=rw` 而非 `rwc`（文件消失时不会新建另一个库）；`-journal` 在恢复或提交后合法消失。任一安全或 SQLite 错误会关闭连接与 fd，`close()` 幂等，之后须重新打开。
- **未知结果不重发**：③ 的远程写入结果未知（传输、协议异常）时，journal 条目保留，不删除、不重新规划，之后只用保存的**同一 request_id 与请求体**原样重放（幂等重放，不是新请求）；发送前验证失败则**不发送**。④ 的模型调用可能已执行（`gateway_outcome_unknown`）时停止，不换新 `call_id`、不自动重发，`reconcile` 只补审计（见 2.1，此次修复未触及 ④，也未重跑）。
- **不变**：部署命名空间校验、保存请求的原样重放、`recover_pending`、业务规划与冲突语义。

**首批已修的安全与可靠性问题（汇总；均在工作区、未提交，来源见各行，不新增结论）**

| 模块 | 问题 | 处理 | 证据与口径 |
|---|---|---|---|
| ② | 原位音频 locator：fstat 失败后 fd 未关闭；已打开的目录被搬出根仍被读取（`test_locator_safety.py` 5 项失败） | 失败路径关闭 fd；列目录与读哈希前后复核祖先目录链 | 2.2：23:22Z 189 passed、5 failed，23:43Z 194 passed、3 skipped；仅适用于可信只读视图，不是文件系统锁 |
| ② | 显式 ID 导入与源读取 | ID 先校验去重；`HttpEarSource` 禁用环境代理与重定向，错误不带响应正文；locator 拒绝符号链接、硬链接与非常规文件 | 2.1；测试含在 194 项内；未对现场旧 Ear 验证 |
| ③ | 提交成功、回执丢失后，重投事件不会再进处理函数，journal 条目清不掉 | `subscribe()` 先 `recover_pending()` 原样重放 | 2.2：`test_attribution_runtime.py` 的 2 项，进程内重开，不是 OS 强杀 |
| ③ | journal 目录经可写祖先被替换（P2） | 本节 | 本节验证表 |
| ④ | `call_id` 每次新生成且不持久化，结果未知可能重发；`extraction_run` 占用请求 event_id 使 Finish 无法写入 | 发送前持久化 `call_id` 与请求指纹，未知停止；run id 与请求 event_id 解耦 | 2.1、2.2：17 项真实 PostgreSQL/NATS，一次连续运行；本次未重跑 |

**修复后验证（UTC，2026-11-12）**

| 项 | 命令 / 范围 | 结果 | 来源与口径 |
|---|---|---|---|
| ③ 模块单元 | `pytest services/attribution/tests -q -p no:cacheprovider` | **143 passed**（67 原有 + 76 新增），0.93 s，退出码 0 | 修复任务日志（00:57:52Z）；**作者复跑**（01:08Z）143 passed，0.90 s；`--collect-only` 为 143，新文件 76 |
| 修复前为红 | 新测试跑旧 `transactions.py` | 2 failed（0777 共享祖先；`sqlite3.connect` 时目录替换） | `/tmp/ear-mvp-journal-red.log`，作者读日志 |
| 真实 PG/NATS | `EAR_RUN_POSTGRES_TESTS=1 EAR_RUN_NATS_TESTS=1 pytest services/storage/tests/test_attribution_runtime.py` | **5 passed**，29.90 s，退出码 0 | 修复任务日志（01:00:36Z）；临时 `postgres:17-alpine`、`nats:2.11-alpine` 与回环 ① HTTP；夹具登记表、`AuthOperator` 身份，不是 `DEFAULT_REGISTRY`；作者读日志并 `--collect-only` 核对 5 项，**作者复跑**（01:14Z）5 passed，29.81 s，退出码 0，结束后没有 `ear-storage-it`、`ear-nats-it` 容器残留 |
| 默认全量 | `pytest -q -p no:cacheprovider`（未设任何门控开关） | **1921 passed、653 skipped、2 warnings**，33.55 s，退出码 0 | 修复任务日志（01:01:24Z，**修复后最新一次默认全量**）；作者读日志，**作者复跑**（01:14Z）同为 1921 passed、653 skipped、2 warnings，33.42 s，退出码 0 |
| 静态 | `ruff check`、`ruff format --check`（211 个文件）、`pyright` | 均通过，pyright 0 errors、0 warnings | 修复任务日志，退出码 0，作者读日志，未复跑 |

- 维度：输入合成｜模型不适用｜外部不适用｜平台：journal 单元与安全测试在本机真实 Linux 文件系统与 SQLite 上，远程写入为替身；5 项运行为测试夹具起的临时 PostgreSQL/NATS/① HTTP。
- 1921 与修复前一次默认运行（00:46Z，`/tmp/ear-mvp-final-local.log`：1845 passed、653 skipped、2 warnings，审查记录观察，作者读日志）相差 76，与新增测试数一致。653 项跳过没有逐项归因（没有 `-rs`）；默认运行不设开关，PostgreSQL/NATS/备份/部署类测试按设计跳过，**所以 1921 不是真实服务证据**，真实 PG/NATS 的证据只有上表的 5 项。
- **2485 passed、11 skipped、2 warnings（四开关）是 journal 修复前的基线**，不覆盖 `secure_files.py`、新的 `transactions.py` 与 `test_journal_safety.py`，不能当作修复后的完整验收。修复后**没有重跑**：四开关全量、备份与部署镜像测试、`test_extraction_completion_runtime.py` 的 17 项；`services/adapters`、`services/dependency`、`services/extraction` 只含在默认全量里，没有单独重跑。上面的 1921/653 与 2485/11 开关不同，不能相加、相减或互相替代。2.2 B 里的 69 passed（e2e 2 + ③ 67）是硬化前的数字，当前 ③ 单元为 143。
- 修复后用原复现脚本（仓库外）再跑，退出码 1 是**预期的拒绝**（`ValueError: untrusted journal ancestor`，在被打补丁的 connect 执行前抛出），脚本没有捕获该异常，不是新故障。

**限制与升级影响**

- 不抵抗恶意 root，也不保证抵抗同 UID 进程的任意竞态；目录、DB、sidecar 的替换测试是确定性注入，不是并发穷举证明。属主检查用合成的 stat 结果测试，没有真正的跨 UID 攻击进程。
- 热回滚恢复只覆盖真实子进程非干净退出一次；没有 worker 强杀端到端、断电或文件系统持久性测试。
- 对已有 journal 的影响：目录不是 0700、文件不是 0600、属主不符、硬链接、WAL 或祖先目录组/其他人可写的，现在都会拒绝启动，需要运维处置；本任务没有迁移或删除任何真实 journal。祖先路径上的符号链接一律拒绝。
- 仍没有：命令行或常驻启动入口、日志枚举与清理、超出 1000 条的显式恢复入口、WAL 的受控恢复或迁移工具、既有 journal 的权限修复工具。

**分支与工作区状态（2026-11-12T01:05Z，只读 git）**：`integration/mvp-runnable` 与 `origin/dev` 均为 c5c31f8（本地引用，未 fetch），`git status -sb` 无领先或落后，**没有任何本地提交**；本地已知的远程引用中只有 `origin/dev` 包含 HEAD，没有功能分支被推送。暂存区 60 个文件（8339 行增、129 行删，仅索引状态）；其中 9 个文件另有未暂存修改（`service.py`、`transactions.py`、两个测试文件和 5 份文档）；**另有 2 个未跟踪文件**（`secure_files.py`、`test_journal_safety.py`）。相对 HEAD 共 62 个文件有变化。提交时须把这 2 个未跟踪文件和上述未暂存修改一并纳入，否则提交内容不含本节修复。

## 3. 阶段门槛

门槛是验收顺序，不是工期承诺。各任务可用合成替身并行，但替身通过不等于门槛通过（[工作分解第 4 节](../../docs/design/work-breakdown-v1.2.md)）。每个门槛的结果都要按第 4 节分别标注输入、模型、外部适配器、平台四个维度，并记录提交、命令、日期和通过项数；不同维度组合的结果互不替代。

| 门槛 | 覆盖 | 通过所需证据 | 当前 |
|---|---|---|---|
| G0 可重复隔离环境 | 集成部署 | 平台真实：从空环境起①存储切片和②③④⑤⑥⑦，身份齐全，健康检查通过，重复一次结果相同；不触碰常驻实例 | 部分：只有①切片和 ⑥⑦ 覆盖文件 |
| G1 入库与归属 | 八步 1–2（②→①→③） | 输入合成：合成数据入库，声纹归人物、GPS 归地点，重复投递幂等，用户确认不被推翻；输入真实：私有环境导入与归属抽查（须先能枚举或按已知 ID 导入）。两种输入分别记录 | ② 已有，现场枚举受阻（工作区的显式 ID 导入未提交；2026-11-11 晚曾在修复，2026-11-12 的测试已通过，见 2.2；未对现场验证）；③ 首片在工作区（未接核心登记表；2026-11-12 另有临时容器上的真实存储运行 5 项，见 2.2，仍是合成输入、夹具登记表，不是门槛通过） |
| G2 提取与可见 | 八步 3–4（④ v1.2、⑦、⑥读取） | 输入合成＋模型回放：定时划分、规则选择、纯 Markdown、提醒建议，API/测试客户端读到时间线与产物；模型真实：同样的检查经⑦真实调用（付费或外发先确认）。回放通过不得写成真实模型通过 | ④ 首片（调用安全与 Finish）在工作区；完整 v1.2 对齐未做 |
| G3 纠正与传播 | 八步 5、6、8（⑥→③→⑤→③④，⑦关联） | 输入合成＋模型回放＋外部假：纠正后待办负责人更新、未审核建议重生成、规则版本未变且未受影响的产物逐字不变、无变化完成能释放等待、起因/完成/阻塞/关联 `llm_call` 可查 | ⑤ 首片只支持内存①；③④ 首片已在工作区各自实现完成协议；三者没有真实服务联调，2026-11-12 的内存合成联调见 2.2（`tests/e2e/test_core_memory_loop.py`，不是门槛通过），均未提交 |
| G4 审核执行与结果未知 | 八步 7（⑥→④） | 外部假：成功、明确未执行、结果未知时停止自动重试、成功后回写前崩溃、用户确认已执行/风险重试；外部真实：真实日历出现正确内容（写入前确认），另行记录 | 部分：离线切片，HTTP 501 |
| G5 动态插件与多层 | 工作分解第 3 节矩阵全部 | 输入合成＋合成示例插件：经 ⑥ 管理页完成注册、授权、启停、换凭据、撤权；多层与分叉汇合，成环拒绝；首次启用/主动停用恢复/意外断线语义；插件外部动作走审核；⑦ 默认无正文 | ① 后端已有，⑤ 与示例插件待实现，页面未对真实① |
| G6 恢复与故障 | 重启、重复投递、Relay/①不可用、提交结果未知、模型结果未知、②掉线 | 平台替身：故障注入；平台真实：真实 PostgreSQL/NATS 门控测试。两者分别记录 | 分散在各模块，未汇总 |
| G7 真实一天 | 整天连续运行 | 输入真实＋平台真实：真实音频连续一天经全链路不中断；模型与外部适配器各自是真实还是回放/假，由用户确认后如实记录，不因整天跑通就推定模型质量或真实外部动作已验证 | 未开始；模型真实、外部真实、外发须另行确认 |

八步全部落在 G1–G4，插件矩阵落 G5，恢复落 G6，整天落 G7，门槛之间不能互相替代。2026-11-12：第 2.2、2.3 节追加的证据是合成数据在内存、本机文件系统或临时容器上的结果，不使 G0–G7 任何一项通过；上表“当前”列除 G1、G3 两格外仍按 2.1 的记录。

## 4. 演练维度

S/A/M/X 这类单一等级不再使用：输入、模型、外部适配器、平台是四个相互独立的维度，不是一条由低到高的阶梯。每个结果都要分别写出四个维度的取值（不适用就写"不适用"），例如"输入合成｜模型回放｜外部假｜平台真实（隔离 Compose）"。不折算成单一等级，不按最低维度认定，也不让某一维度的通过替代另一维度。

| 维度 | 取值 | 含义 | 能证明 | 不能证明 | 进仓库/CI |
|---|---|---|---|---|---|
| 输入 | 合成 | D1 合成数据、`ear_adapter.testing` 的合成 Evidence | 流程、协议、权限、幂等、故障恢复逻辑 | 真实声纹与转写的准确率、真实数据量下的行为、整天可靠性 | 可以 |
| 输入 | 真实 | vm-octopus 的音频、GPS、手机信息及其派生数据（经旧 Ear Evidence API 由②读取，或按已知 ID 导入） | 数据量、完整性、导入不丢不重、时间对齐、整天运行 | 模型效果（模型维度为回放时） | 不可以，只在私有隔离环境 |
| 模型 | 回放 | 录制或固定的模型回复、合成 provider | 提示词到产物的流程、解析与校验、失败处理 | 真实模型的输出质量、wire 行为、用量 | 可以 |
| 模型 | 真实 | 经⑦的真实模型调用 | ⑦ 的真实 wire、提示词与输出 schema 的真实表现、用量 | 输入为合成时，不能证明在真实数据上的表现 | 不可以，CI 只用回放 |
| 外部适配器 | 假 | 假外部适配器 | 审核→执行→结果未知/确认的流程与幂等逻辑 | 真实系统的幂等、查证与结果未知行为 | 可以 |
| 外部适配器 | 真实 | 真实日历/提醒事项写入 | 与真实系统的幂等、查证、结果未知行为 | — | 不可以 |
| 平台 | 替身 | 内存存储、假①、假⑥ 等 | 模块内逻辑 | PostgreSQL/NATS 行为、服务间协议 | 可以 |
| 平台 | 真实 | 真实① + PostgreSQL + NATS（隔离 Compose，或门控测试的临时容器），写明哪些组件真实 | 持久化、投递、权限、服务间协议 | 生产运行（常驻实例与生产不在本计划内） | 门控测试可以（临时容器）；Compose 部署只在私有环境 |

1. 真实输入（vm-octopus 音频）已获用户授权使用，本计划把它限定在私有隔离环境（第 6 节）；模型真实涉及外发或付费、外部适配器真实写入真实日历，须另行确认。
2. 混合时分别写各维度，不得把合成输入的全链路通过写成真实输入通过，不得把回放模型通过写成真实模型通过，不得把假外部适配器通过写成真实日历通过，不得把内存替身通过写成真实 PostgreSQL/NATS 通过。
3. 合成输入入库用 `ear_adapter.testing` 的合成 Evidence 响应经 `RemoteStorageClient` 写真实①，不需要真实音频。D1 脚本和标准答案目前不存在（`fixtures/` 为空），由 T4 交付。

## 5. 依赖顺序与首批任务

```text
并行起步（互不等待）
  T1 ④⑦ 调用身份 / 结果未知停止 / 请求上下文 / run id 与请求 event_id 解耦
  T2b ⑤ 骨架与纯逻辑 → 内存① → 真实①（合成所属方）
  T4 集成部署：身份、服务入口、D1 与 e2e 驱动、容量估算、真实输入的只读盘点与已知 ID 导入
  接口增补评审（第 7 节 E1–E8）
随接口增补落地
  T2a ③ 业务（等 E1、E2）        T3 ⑥ 反馈消费与外部执行（等 E1、E4、E5）
汇合（第二批，首批合格后再派）
  ④ v1.2 对齐 → ⑤ 接入 ③④ 完成协议 → G3 → G4
之后
  G5 动态插件与多层 → G6 恢复与故障 → G7 真实一天
```

每个任务一个或多个小 PR，前一步合格再派下一步（沿用[⑤实现设计第 10 节](../../services/dependency/ai-docs/design.md)的做法）。分工以各任务的独占文件范围为准，代码与测试、文档与前端分别由对应实施方负责。

### 首批任务

| 任务 | 范围 | 依赖 | 退出证据 | 状态 |
|---|---|---|---|---|
| T1 ④⑦ 调用身份与上下文 | ④：发送前持久化稳定 `call_id`（绑定 run_id 或业务键），同一业务操作重试复用；`gateway_outcome_unknown` 时停止，不自动换新 id、不自动重发，任务停在"结果未知"等显式处置；`reconcile` 只补审计记录、取不回回复，重新生成须显式重试并作为新的逻辑调用，且留下与旧 id 的对应；按此改 `orchestration.py` 里的自动重试、存储冲突重算、租约恢复三处。上下文：重判调用传 `request_event_id`、`additional_causes`、根源与深度，非重判 depth=0（见[④ 实现说明](../../services/extraction/ai-docs/implementation.md)、[⑦入口](../../services/llm_gateway/README.md)第 3.4 节）。**run id 与请求 event_id 解耦**：`handle_event` 现把 `event.event_id` 直接作为 `extraction_run` 的记录 id（`_admit(event.event_id, …)`），而 ① 的 `FinishReevaluation` 把终态结果 ID 定为 request_event_id，被系统记录占用的 id 会以 `id_exists` 拒绝，Finish 因此无法落盘（主 agent 发现）。改用独立、确定性的 run id（例如由 event_id 派生），`event_id` 仍留在记录里并继续配套 `MarkProcessed`，`_admit` 的重复检查改用新 id；已存在的旧 id 记录如何兼容在 PR 里说明。⑦预计不改，发现缺口按工程增补处理 | ⑦与①预检/call_id 占用检查已在 dev；重判请求的真实来源要等⑤，先用①测试里发布的请求 | 输入合成｜模型回放（合成 provider）｜外部不适用｜平台：故障注入用替身，请求与 Finish 用真实①。故障注入（发送后崩溃、超时、断连、重复投递、提交冲突）下同一业务操作不产生第二个 call_id，未知态不自动重试；对真实①和⑦网关（合成 provider）发 depth>0 的重判能被接受，并写出带 `request_event_id` 的 `llm_call`；处理一个重判请求后，其 request_event_id 没有被 `extraction_run` 占用，真实①上以该 id 做 `FinishReevaluation` 不再因 id 被占用而失败，重复投递仍只产生一个 run（完成结果的内容由第二批 ④ v1.2 对齐写入，本任务只证明 id 不再冲突）。模型真实不在本任务 | 首片在工作区，未提交、未提 PR；任务记录称退出证据所列的故障注入、depth>0 重判、Finish 不再冲突等场景已由 17 项真实 PostgreSQL/NATS 分组测试覆盖（作者未复跑）；完整 ④ v1.2 未做 |
| T2a ③ 归属 | 先把 `origin/attribution/initial-implementation` 同步到当前 dev（merge 或 rebase 由实施方选择，PR 里说明），再实现：匹配与新建未命名人物、未知地点兜底、参考管理、纠正/确认及保护、命名/改名、手动建地点、范围重新归属、按请求重判、候选依赖登记（含 `fields`，只靠参考匹配时不登记 `name`）、`attribution_completion`（无变化也写）；按真实量级评估事件消费吞吐 | E1、E2；用户确认保护按已采纳设计 §6.2 实现，保护字段形状归入 E1（待确认） | 输入合成｜外部不适用：设计第 6 节与工作分解 ③ 的验收项：一条数据至多一个对象的参考、任何重判不推翻用户确认、改名不触发只靠参考的归属、无变化有完成记录且不发业务事件、重复投递幂等 | 首片在工作区，未提交；`DEFAULT_REGISTRY` 未接入，真实验证未做；`create_place`、`reattribute_range` 未做；2026-11-12 追加的 journal 安全硬化（仅 Linux）、真实存储运行 5 项与未提交文件清单见 2.2、2.3 |
| T2b ⑤ 调度 | 按[实现设计](../../services/dependency/ai-docs/design.md)第 10 节顺序：骨架与纯逻辑（门控、闭包、就绪、起因合并、限额）→ 服务与持久化（内存①，故障注入）→ 合成插件与真实①（PostgreSQL+NATS 门控）→ 按钮入口与历史补处理 → 导出/统计/命令行。先用合成所属方，不等③④ | E6（`dependency` 身份、生命周期只读、能力登记）；D2 未定时测试显式授权并记录前提；E3（`propagation_record` 格式，proposed） | 输入合成｜平台先替身后真实：设计第 9 节测试计划：表驱动纯逻辑、内存①、真实①；无变化释放等待、失败不伪成功、多起因追溯、并发新鲜度、多层 DAG、成环拒绝。八步第 6、8 步要等③④接入后才验 | 首片在工作区，未提交；内存①，不支持远程①；其余见 2.1 |
| T3 ⑥ 反馈与外部执行 | (a) 给 `attribution`/`dependency` 命令接上消费方，退出 `pending_contract`；按钮优先用 `correction_feedback_ids`（形式 A），形式 B 兼容；(b) 与④定外部动作执行/恢复协议，解除 HTTP 501（执行、确认已执行、风险重试）；(c) 执行 worker 接线，先用假适配器；(d) 状态展示读 `trigger` 记录可后做，不阻塞⑤。文档方：⑥ 文档同步；八步 4、5、7 用 API/测试客户端，非插件页面不做 | E1、E4、E5；④ 的建议状态扩展 | 输入合成｜外部假：G4 全部场景；`pending_contract` 的命令有消费方，重复反馈只处理一次；已批准/执行中/未知的建议不随产物变化作废 | 部分 |
| T4 集成部署 | (a) 补齐 adapter、attribution、extraction、dependency、llm_gateway 的受控身份签发流程，凭据只进被忽略的 `deploy/.env` 和 0600 卷文件；(b) 给②③④⑤ 提供运行入口与服务定义（④ 现无远程运行入口），沿用隔离项目名和独立卷；(c) D1 合成组会脚本与标准答案、插件和故障用合成 fixtures、`tests/e2e` 的八步合成输入驱动；(d) 按真实数据量估算 NATS 流容量与消费吞吐；(e) 真实输入的枚举与盘点：现场旧 Ear 缺列表接口，先做 ② 已知 ID 显式导入（E8；工作区已有未提交实现：`ear-adapter` 的 `--capture-id` 与 `Runner.run_once(capture_ids=…)`，见 2.1；真实 ID 清单放仓库外，不入 Git、PR、日志，且命令行参数会出现在进程列表与 shell 历史里，使用前先评估），或另经授权给旧 Ear 补源接口；能枚举后用 `ear-adapter dry-run`（内存，不写①）统计数量、各类型条数、Pending/Failed 原因分布和时间范围，精确数字存仓库外，Git 里只写有无、量级和门槛是否通过；(f) 文档方：运行手册与文档同步 | (a)(b) 触及 deploy 与①身份机制，须 ① 负责人与 I 审核（E6；本计划不假定用户担任这些角色，须向用户确认由谁审核）；(c)(d) 无前置；(e) 的 ② 部分无前置，补源接口部分需另行授权 | G0；D1 与 e2e 驱动能直接用于 G1；真实输入的枚举路径可用且未复制或外发任何内容 | 部分；(e) 的 ② 显式 ID 导入在工作区有未提交实现，仍在修复，未对现场验证 |

### 第二批及之后（首批合格后再细化，不预判工期）

- ④ v1.2 对齐：纯 Markdown（去掉引用标记）、`rule_selection`（登记、版本化、用 `type_revision` 做候选集合守护）、按当前规则重判与失效替代、字段依赖加粗粒度归属依赖、`extraction_completion`、远程①运行入口。这是已采纳目标（[设计第 7 节](../../docs/design/design-v1.2.md)）的实现同步，不是新决策。
- ⑤ 接入③④的完成协议，打通 G3、G4。
- 合成示例插件（多层、分叉汇合）；⑥ 插件管理页（仅这一个页面）对真实①的浏览器联调（前端）；汇总故障矩阵。
- 真实输入：vm-octopus 音频已获授权使用，按第 6 节的私有隔离边界先做；真实模型、真实外部动作按第 6 节确认后再做，最后 G7。

## 6. 真实数据、外部动作与凭据

- **已知授权（来自用户的只有这一项）**：可使用 vm-octopus 的音频。本计划把使用方式限定为只读盘点、导入、归属与提取处理、测试，且只在私有隔离环境进行（经旧 Ear Evidence API 与 Octopus inbox，具体路径和端口以现场为准，本文不记录；私有环境指独立 Compose 项目和卷、仓库外私有目录）。这个限定是本计划采取的保守边界，不是用户给出的细则；用户原话没有覆盖的用途，按下面的清单处理。
- **未经用户另行确认，不做**（下列是已识别的几类，不是穷尽清单；拿不准的用途同样先确认）：
  1. **外发**：真实音频、转写、GPS、手机信息的内容或其派生内容离开受控环境，包括进入任何外部模型或第三方服务，以及进入 PR、issue、CI 日志、公共日志；跨机器复制前先确认接收端在受控范围内，拿不准按外发处理；
  2. **付费调用**：真实模型等会产生费用的调用，包括输入为合成的真实模型调用；是否收费不明时先确认；
  3. **真实日历/提醒事项写入**。
- **授权范围**：本计划按最窄范围理解对音频的授权：只覆盖上面的私有使用，不包括新增数据，也不是对全部内容（含录音里涉及的他人）外发的同意；每次外发须另行确认具体范围。本轮盘点没有复制或外发任何内容。
- **永不进入 Git、公共日志或任何 AI 记忆/长期记忆存储**：音频、转写文本、GPS、手机信息、声纹向量、真实人名、凭据（令牌、API key、DSN、身份文件），以及精确的私人文件名、录音/capture 的 ID 清单和涉及的人数。精确数量只记在仓库外的私有位置；Git 里的结果记录只写有无、量级、状态和耗时，不写内容；私有输出放仓库外，权限 0600/0700。
- **隔离**：私有测试用独立的 Compose 项目名和卷，不指向常驻 `ear-dev`，不指向生产；真实标注（D2）不入库。真实数据运行时保持⑦默认的不保存正文。
- **用途边界**：设计第 12.1 节规定真实数据只用于经同意的连续一天可靠性检查、调参和辅助标注，不作标准答案，不入库。

## 7. 接口增补与决策边界

在已授权的实现范围内，实现方式、夹具、命名、分批方式、merge/rebase、已有接口内的取舍属于工程选择，按根 README 第 5 节由实施方在 PR 描述里列明，交审核者审阅；本计划不替用户批准其中任何一项。没有现成接口的地方，可以作为工程增补开 PR；触及 `contracts/` 的须按根 README 由 ① 负责人与 I 共同审核，只增不删不改义。跨产品决策保持 `proposed`/待决，PR 可按某个选项实现，但须在描述里标"工程增补，待负责人确认"，文档状态不变，不得宣称已获准，不得标 `accepted`，不改 reviewed 字段。决策项未定前，代码只做不预设选择的部分，或把选择做成显式配置。**例外**：已被已采纳设计回答的事项不是待决项，实现按设计做，不做成可关闭的选项；用户确认保护即属此类（见下面的 D1）。

### 工程增补

| 编号 | 内容 | 现状与出处 |
|---|---|---|
| E1 | ③ 的 payload 与对象：归属/人物/地点字段（P1）、声纹向量与 GPS 坐标约定（P2）、反馈命令字段（P3）、`UNKNOWN_PLACE_ID` 与未命名人物 `#N`（P4）、用户确认保护的字段形状与旧数据兼容（原 P5 中仍属工程的部分；保护本身已由设计 §6.2 决定） | P1–P4 均"待确认"，③ 工作区首片对它们做了具体工程选择（见[③实现说明第 13 节](../../services/attribution/ai-docs/implementation.md)），仍待确认（见分支 `origin/attribution/initial-implementation` 的 `services/attribution/ai-docs/development.md`）；⑥ 的草案命令在 `services/api/src/ear_api/models.py`；⑤ 暂按①草案字段名实现并标"待③确认" |
| E2 | `rule_selection` 登记与 payload；attribution、artifact、`rule_selection` 的重判与强制依赖能力启用 | 登记表无 `rule_selection`；能力缺省 false，按类型在③④接入时启用 |
| E3 | `propagation_record` payload 与 Schema | 登记表只有类型名；[接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)（proposed） |
| E4 | `reevaluate_related` 反馈格式 | ⑤负责人 2026-11-11 决定本入口接受形式 B 过渡、建议⑥改用形式 A、上限 100；提案注明不代表全文采纳，涉⑥部分待⑥确认（该决定记在[接口提案](../../docs/proposals/dependency-scheduler-interfaces.md)里，本文未另行核实） |
| E5 | ④ 外部动作执行/恢复：执行中、结果未知、核对的状态与反馈，稳定动作标识，外部标识存放位置 | 设计第 7.6 节与 [⑥入口](../../services/api/README.md)第 5 节均标"待契约设计"；⑥ 现为 501 |
| E6 | ① 侧：`dependency` 等身份签发（A1）、⑤ 只读生命周期状态（A3）、③④ 能力登记（A4） | [提案第 4 节](../../docs/proposals/dependency-scheduler-interfaces.md)；触 deploy 与①身份 |
| E7 | ④ 内部的 `call_id` 持久化位置与"结果未知"任务状态 | 要求见 [⑦入口](../../services/llm_gateway/README.md)第 3.4 节；格式未定，④ 内部选择，PR 里说明 |
| E8 | ② 按已知 ID 显式导入（旧 Ear 无列表接口时的替代枚举），ID 清单放仓库外 | 2026-11-11 主 agent 只读盘点；`HttpEarSource.list_captures` 与 `Runner.run_once` 在 c5c31f8 上都依赖列表接口，工作区已有未提交的显式 ID 实现（2.1，仍在修复）；不涉及公共契约 |

### 跨产品决策（仍 proposed/待决）

本表的 D1–D8 只是本计划的编号，与工作分解里的 D1（模拟组会）、D2（标注数据）不是同一组东西；文中出现"D1 合成数据""D1 与 e2e 驱动"处指工作分解的 D1。

| 编号 | 内容 | 出处 |
|---|---|---|
| D1 | **已撤销，不再是待决项。**「用户确认/纠正过的数据在移除参考后是否仍受保护」已由[设计 v1.2 §6.2](../../docs/design/design-v1.2.md)回答：任何重新归属均不得改变用户纠正或确认过的归属结果，上游删除或整体替代也不推翻，所以保护在移除参考后仍然存在，不提供解除保护的选项，也不作为待决事项。保护字段形状与旧数据兼容属工程增补，归入 E1 | 设计 v1.2 §6.2；③ 分支的 P5（其中"是否仍受保护"一问已被设计回答） |
| D2 | A2：插件类型对⑤的读授权是自动授权，还是依赖查询隐去边时明确报错 | [提案第 4、6 节](../../docs/proposals/dependency-scheduler-interfaces.md) |
| D3 | 插件建议/审核/执行反馈如何交接给④、所有权归谁 | 设计第 7.6 节、[插件第 8 节](../../docs/design/plugins-mvp.md)、[确认索引](../../docs/decisions/mvp-confirmed-decisions.md)"未冻结"一节 |
| D4 | ⑤ 请求超时后是否自动重发（草案不重发） | 提案第 6 节 |
| D5 | 外部 Agent（MCP）写 `reevaluate_related` 前是否需用户确认 | 提案第 6 节 |
| D6 | 多 cause 跨 root 合并的主起因规则，以及与旧 `cause`/`root`/`depth` 的兼容表达 | 设计第 4.2 节；⑤设计第 4.2 节只是候选方案 |
| D7 | 模型调用正文的保留期与作用域 | [⑦入口](../../services/llm_gateway/README.md)第 6 节 |
| D8 | 验收指标：认人准确率、活动边界一致度、允许时间误差、真实数据天数、G7 通过线 | 工作分解第 6 节 |

## 8. 协作与交付规则

- **分支与 PR**：授权的工作方式是在常驻集成分支 `integration/mvp-runnable` 上实现并提交，提 PR 由用户审核；PR 按根 README 第 9 节规则面向 `dev`，不自动合并；是否另拆 `<模块>/<主题>` 功能分支、怎样拆 PR，在提 PR 时说明；不直接 push `dev`/`main`，不强推，不改写已推送历史。CI 通过不等于采纳。本轮文档任务没有 checkout、stage、commit、push 或 reset。
- **并发**：多个 agent 共用工作区时，各自只改自己负责的文件；本计划与 `ai-docs/README.md` 里的链接由文档方独占；不做全仓格式化；不引用另一个 agent 未提交的中间测试，报告只写自己运行过或已提交的结果；转述其他 agent 的查验结果时写明来源和日期，并说明自己是否复验。他人的未提交改动属于其作者，不 checkout、stash、reset、stage 或回退；中间输出不当作全绿的证据。
- **检查**：提交前跑 README 的全部检查，加 `uv run python tools/check_docs.py --base origin/dev` 和 `git diff --check`；⑥ 页面改动另跑 `node --test`（用法见[⑥入口](../../services/api/README.md)）；①的真实 PostgreSQL/NATS 测试用既有开关 `EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`（CI 另设 `EAR_RUN_BACKUP_TESTS`、`EAR_RUN_API_GATEWAY_DEPLOY_TESTS`），只在临时容器夹具上运行，不连外部 DSN。
- **状态更新**：任务 PR 合并后，在第 2 节表和第 10 节追加日期、提交、命令、通过项数和各演练维度取值；失败与未跑照实写；"计划""已提 PR""已合并""已验证"分开写。
- **文档**：不改 `reviewed-by`/`reviewed-at`，不把 `proposed` 标 `accepted`；发现设计、契约、代码冲突先报告，不为一致而静默改义。

## 9. 已知授权与待用户确认

**用户的授权（本计划能追溯到的仅此三项）**：

- 开始以 Echo-As-Record MVP 完整跑起来为目标的实施；
- 可使用 vm-octopus 的音频（本计划把使用限定在私有隔离环境，第 6 节）；
- 在常驻分支 `integration/mvp-runnable` 上实现并提交，提 PR 由用户审核。

**仓库既有规则（不是用户在本任务中的新决定）**：功能 PR 面向 `dev`、不自动合并、CI 通过不等于采纳（根 README 第 9 节）；完整前端不纳入 MVP（工作分解第 5 节，MVP 内只有轻量插件管理页）；`contracts/` 变更须 ① 负责人与 I 审核（根 README 第 7 节）。本计划不假定用户担任哪个角色，也不假定"普通工程选择无需逐项批准"这类授权已经给出；工程选择在 PR 描述里列明，由审核者审阅。

**需要用户确认的事项**（下列是已识别的，不是穷尽清单；拿不准的按需要确认处理）：

1. 外发与付费：要做模型真实时，用哪个后端、预算上限，以及是否允许把真实数据的派生内容发给模型（外发）；输入合成的真实模型调用若会收费，也先确认。
2. 真实日历/提醒事项写入：是否有专用测试日历或提醒列表，写入前由用户确认（外部适配器真实，G4、G7）。
3. 旧 Ear 补源接口：是否授权对旧 Ear（另一个仓库与服务）补或合并列表接口。不授权则走 ② 已知 ID 显式导入（E8，已授权实现范围内的工程增补，工作区已有未提交实现，见 2.1），ID 清单由受控环境内的只读盘点取得，放仓库外，不入 Git。
4. 跨产品决策 D2–D8（第 7 节；D1 已撤销，保护由设计 §6.2 决定）何时决定；未定前按第 7 节只做不预设选择的部分。D8 含 G7 的通过线。G7 具体用哪几天的音频属于使用授权内的工程选择，由实施方选择并在 PR 里说明，只记天数等聚合。

## 10. 状态记录（只追加）

| 日期 | 事件 | 维度 | 说明 |
|---|---|---|---|
| 2026-11-11 | 创建本计划 | — | 仅文档；未运行代码测试；各任务状态见第 2、5 节，均为待实现或部分 |
| 2026-11-11 | 修订本计划 | — | 【后续更正：本行里"依据用户澄清""授权范围…仅外发/付费/真实日历写需另行确认"的表述没有对话证据，已在第 6、9 节改正，本行仅保留修订历史】仅文档。依据用户澄清与主 agent 报告：PR 目标 `dev`、审核人为用户；测试数字改记为主 agent 对 CI 日志的查验（head c5c31f8，本轮未重跑，作者未读日志，只核对了 ci.yml 的四个开关）；S/A/M/X 改为四个独立演练维度；授权范围改为私有隔离测试准备与音频使用，仅外发/付费/真实日历写需另行确认；T1 加入 `extraction_run` 占用请求 event_id 的修正；记录现场旧 Ear 缺列表接口与 ② 已知 ID 导入缺口（E8）。作者只对照代码核对了 ④ 的 id 来源、② 的列表路径和 ci.yml 开关，未运行任何测试，未复验主 agent 的盘点 |
| 2026-11-11 | 二次修订本计划与 ②③④⑤ 模块文档 | — | 仅文档。改动：删除或改写对话里没有证据的“用户澄清/决定”表述，授权只记三项（开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核），PR 面向 `dev` 改记为仓库规则，“用户即 ①负责人与 I”“普通工程选择无需逐项批准”“不再请示”等不再出现；第 7 节原 D1 撤销（用户确认保护已由设计 §6.2 回答，移除参考后仍受保护，不是待决项，字段形状归 E1）；新增第 2.1 节记录 ②③④⑤ 的工作区未提交进展（④ 的调用安全与 Finish 首片、⑤ 当前不是纯草案但不支持远程①、③ 首片 67 项内存测试且 DEFAULT 登记未接、② 显式 ID 与定位安全仍在修复）并同步门槛与任务状态列；补建 `services/attribution/ai-docs/implementation.md`，③ README 引用不再悬空；同步 ⑤④② 的 README 与 ④ 实现说明。作者本轮运行：`.venv/bin/python -B tools/check_docs.py` 起初失败 6 项（均为缺少上述 implementation.md），补文件后通过（43 个 Markdown 文件）；`--base origin/dev` 通过并列出 8 个待人工复核文件；`git diff --check` 通过（只覆盖已跟踪文件，未跟踪文档另查了行尾空白）；`pytest --collect-only` 只收集不执行：extraction 84、storage 真实运行测试文件 17、attribution 67、dependency 34、adapters 197；一个只读脚本确认 `DEFAULT_REGISTRY` 没有 `attribution_state`，`AttributionService.business()` 因此失败；比对了 ③⑤ 任务记录里的文件 sha256，全部与当前一致。**没有运行**：任何实测 pytest、ruff、pyright、`uv sync`、真实服务；没有 fetch、checkout、stage、commit、push、reset；没有改 Python、根配置、锁文件、核心登记表或本计划之外的文档；没有改 reviewed 字段，没有把 proposed 标为 accepted。②的 locator fd 与并发路径检查、⑤ 的根接入仍在协调者手里，统一检查结果出来前不记录“全绿”。机器可读记录：`/tmp/ear-mvp-docs-followup.json`（非仓库文件） |
| 2026-11-12 | 追加验收证据与状态更新（仅文档，只追加） | 输入合成｜模型回放或合成替身｜外部不适用｜平台为内存或测试夹具起的临时 PostgreSQL/NATS/① HTTP；不记门槛通过 | 新增第 2.2 节，并在第 2.1、3 节加更新指引；同步 ② README、③ README 与实现说明、⑤ README（这 5 个文件原本已暂存，本轮编辑留在工作区，没有再 stage）；④ 的 README 与实现说明本次未改，其“17 项分 8 组”的限制按 2.2 更新。证据：仓库外整体验收 `/tmp/ear-mvp-acceptance.*`（退出码 0，2485 passed、11 skipped，启动于 4 个文件最后一次修改之前，不覆盖它们）及作者的定向复跑：4 个文件 ruff、format、pyright 通过；e2e 加 ③ 单元 69 passed；`test_attribution_runtime.py` 5 passed；`test_extraction_completion_runtime.py` 17 passed（一次连续）；`services/adapters/tests` 194 passed、3 skipped。③ 的 journal 启动恢复（`subscribe()` 先 `recover_pending()`）已记录，覆盖只有 2 项进程内测试。仍未提交、未提 PR、未部署，验证没有用真实数据、真实模型或真实日历；⑤ 仍拒绝远程①；`DEFAULT_REGISTRY` 仍未启用 `attribution_state` 等核心新类型与能力。本轮文档检查：`check_docs.py` 与 `--base origin/dev` 均通过（43 个 Markdown 文件，列出 8 个待人工复核文件），`git diff --check` 与 `git diff --cached --check` 通过。未改 reviewed 与 status 字段，未 fetch、checkout、stage、commit、push、reset，未改 Python。机器可读记录：`/tmp/ear-mvp-docs-acceptance.json`（非仓库文件） |
| 2026-11-12 | 追加 ③ journal 安全硬化与修复后验证记录；起草首批 PR 描述（仅文档，只追加） | 输入合成｜模型不适用｜外部不适用｜平台：journal 单元在本机文件系统与 SQLite 上，5 项运行为测试夹具起的临时 PostgreSQL/NATS/① HTTP；不记门槛通过 | 新增第 2.3 节，并在 2.2 A、第 3 节、T2a 状态格追加指引；同步 ③ README 与实现说明（新增第 15 节）。本任务只写 3 个仓库文件（本文、③ README、③ 实现说明），另在仓库外写 PR 描述草稿 `/tmp/ear-mvp-first-pr.md`（非仓库文件；**尚未创建 PR，没有 CI 结果，没有提交**）。作者运行（01:08Z–01:15Z，UTC）：`pytest services/attribution/tests` 143 passed；`EAR_RUN_POSTGRES_TESTS=1 EAR_RUN_NATS_TESTS=1 pytest services/storage/tests/test_attribution_runtime.py` 5 passed（29.81 s，无容器残留）；默认全量 `pytest -q` 1921 passed、653 skipped、2 warnings（33.42 s）；`check_docs.py` 与 `--base origin/dev` 均通过（43 个 Markdown，列出 8 个待人工复核文件）；`git diff --check` 与 `git diff --cached --check` 通过。2485 passed、11 skipped（四开关）是修复前基线，修复后未重跑四开关全量，不能当作修复后的完整验收。核对：HEAD 与 `origin/dev` 均为 c5c31f8（未 fetch），无本地提交；`.git/index` 的 sha256 与修复任务记录一致；暂存 60 个文件，另有 2 个未跟踪文件（`secure_files.py`、`test_journal_safety.py`）须提交时纳入。未改 Python、根配置、锁文件、`contracts/`、核心登记表、其他文档，未改 reviewed 与 status 字段，未 stage、commit、push、reset、fetch、checkout |
