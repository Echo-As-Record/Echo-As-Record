---
audience: human
doc-type: reference
generated-by: Claude (AI)
generated-at: 2026-11-11
based-on: 设计 v1.2 第 6 节与信号补充 §3.2、§4（均 accepted，仅表示目标已采纳）；工作分解 v1.2 ③节；integration/mvp-runnable 工作区中 services/attribution 的实际源码与测试（基线 c5c31f8，未提交，本文作者读代码核对并用 pytest --collect-only 核对项数，未运行测试）；③任务状态文件 /tmp/ear-mvp-attribution-state.json（非仓库文件）；2026-11-12 增补依据：transactions.py、service.py 源码，services/storage/tests/test_attribution_runtime.py 与 tests/e2e/test_core_memory_loop.py 源码，仓库外日志 /tmp/ear-mvp-acceptance.{sh,log,exit}、/tmp/ear-mvp-attribution-{recovery-red,real-final}.log，以及本次对相关文件的定向复跑（见跑通计划第 2.2 节）；2026-11-12 journal 硬化增补依据：secure_files.py、transactions.py、tests/test_journal_safety.py 源码，仓库外记录 /tmp/ear-mvp-precommit-review.json、/tmp/ear-mvp-journal-fix.json 与 /tmp/ear-mvp-journal-*.{log,exit}（非仓库文件，作者读取并核对哈希与时间），以及作者的 pytest 复跑（见跑通计划第 2.3 节）；设计 v1.2 §6.2 关于用户确认保护；2026-11-12 第二批增补依据：create_place.py、feedback.py 与 ⑥ 的 attribution_results.py 源码（只读核对），仓库外摘要 /tmp/ear-mvp-api-place.json（此前读取）与 /tmp/ear-mvp-eight-steps.json（作者读取并核对退出码文件与 junit 用例名），测试结果均未复跑；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核，本文不新增授权记录，也不增加用户决定；未填写或改动任何 reviewed 字段
---

# ③ 归属模块

把声纹归到人物、GPS 点归到地点，维护参考、未命名人物、未知地点和用户确认保护，执行归属类反馈，并在被请求重新判断时按当前状态处理、提交完成记录。目标行为以[设计 v1.2 第 6 节](../../docs/design/design-v1.2.md)和[信号补充 §3.2、§4](../../docs/design/signals-supplement.md)为准，职责与验收见[工作分解 v1.2 ③节](../../docs/design/work-breakdown-v1.2.md)，本文不重复。

- [实现说明](ai-docs/implementation.md)：代码导航、处理流程、反馈命令表、登记表与根配置要求、来源（provenance）、测试与验证记录、未实现项、实现选择与待确认项。
- [MVP 跑通计划](../../ai-docs/plans/mvp-runnable.md)：T2a 任务、门槛与演练维度，工作区进展见该文第 2.1 节。

## 状态

**首批代码切片已在 79e8dc3（PR 16 为 OPEN，其 CI 成功只属于 79e8dc3）；第二批（`create_place`、⑥ 对结果回执的投影）在工作区，未提交、未提 PR、未部署。这不是完整的 ③，也不是 MVP 完成。** 下面带“2026-11-11 晚”“未提交”的各条是各自时点的记录，现状以带“第二批”或“2026-11-12 更新”的条目为准。状态词按[跑通计划第 8 节](../../ai-docs/plans/mvp-runnable.md)分开写：这里只能说“已实现、合成验证通过”，不是“已合并”或生产验收。

- **能做什么**：声纹按余弦相似度、GPS 按球面距离与参考匹配（阈值必须显式配置，没有默认值）；已是某对象参考的数据直接归该对象；认不出的声纹新建未命名人物并设为首个参考，认不出的 GPS 归全局唯一的未知地点；纠正、确认、增删参考、改名五种反馈，第二批加 `create_place`；用户纠正或确认过的归属不被自动重判推翻；重判请求都以 `FinishReevaluation` 收尾，无变化也刷新依赖并提交完成结果；候选依赖按实际读到的候选登记，不登记 `name`。
- **当前不支持**：`reattribute_range`（按时间范围重新归属）。收到时写一条 `unsupported_command` 的拒绝回执，不冒充成功，也不排后台任务。（`create_place` 在 2026-11-11 晚也是这样处理，第二批已实现，见下面的第二批条目。）空人物整体替代、“还有 M 段声音”提示数据、启动与补扫入口等同样未做，完整清单见[实现说明第 12 节](ai-docs/implementation.md)；反馈状态回写给 ⑥ 现由 ⑥ 经 `attribution_feedback_result` 只读投影（第二批）。
- **2026-11-12 第二批（工作区，未提交）：`create_place` 与结果回执**。⑥ 的 `create_place` 命令带 `name` 与显式 `gps_ids`（1–100 个、无重复、须为 `gps_point`）；③ 在一个受围栏的批次里新建地点，把这些 GPS 点改归属到它（沿用已有归属对象的 ID、移除旧参考、新版本带完整依赖字段），`expected_version` 须为空，受保护的归属被拒，不排后台任务。③ 为每条归属命令写 `attribution_feedback_result` 回执，⑥ 只读投影它，字段与错误见[⑥入口第 3.4 节](../api/README.md)；`reattribute_range` 仍写 `unsupported_command` 拒绝回执。验证（合成，作者未复跑）：04:14Z 的 api-place 摘要里 `create_place` 定向 177 passed（161 项纯边界加 16 项真实回环，其中 4 项经真实 Relay/NATS 与真实 ③ 服务）；06:19Z 的合成八步用例里，③ 作为 `tools/mvp_worker.py` 的独立进程消费真实事件，完成归属，纠正后受保护的归属与历史版本保持，2 项通过（输入合成｜模型回放｜外部假｜平台：认证的回环 HTTP 加临时 PostgreSQL/NATS，**不是 Compose**，见[交接第 6 节](../../ai-docs/plans/mvp-runtime-handoff.md)）。阈值仍须显式配置、未校准；`DEFAULT_REGISTRY` 与 `contracts/` 没有改。
- **在当前核心登记表上不能运行**：首批切片需要两个新的系统记录类型（`attribution_state`、`attribution_feedback_result`），以及 `attribution` 打开“强制依赖”和“支持重判”两项能力。核心登记表（`contracts/registry/data-types.json`）至今没有新增这些类型和能力，`AttributionService.business()` 在 `DEFAULT_REGISTRY` 上会直接失败，不会在运行时补登记；测试用的登记表只在 `tests/support.py` 里本地构造。因此**不能声称 ③ 已能在 `DEFAULT_REGISTRY` 或任何真实 ① 上运行**。核心登记、JSON/Python 同步和迁移须由 ① 负责人和 I 审核，见[实现说明第 9 节](ai-docs/implementation.md)。**第二批更新**：① 的 `core_mvp_registry()` 与 `bootstrap-core`、`init-core-identities` 可在显式迁移过的**空库**上提供这些类型、能力与 ③ 的身份（见[①部署指南第 9 节](../storage/ai-docs/deployment.md)，旧库与 `DEFAULT_REGISTRY` 不变，不自动迁移）。
- **根工作区**：2026-11-11 晚本文写下时，根 `pyproject.toml` 的未提交改动已含 `services/attribution` 的 workspace 成员、dev 依赖、`tool.uv.sources`、pytest `testpaths` 与 pyright `include`，`uv.lock` 也有未提交改动，由协调者维护且仍在进行中，当时不据此宣称根目录检查通过。**2026-11-12 更新**：这些改动现在在已暂存的索引里；仓库外的整体验收里 `ruff check`、`ruff format --check`、`pyright`（0 errors）、`uv lock --check --offline` 通过（见下面的更新，它在 `service.py` 与 `transactions.py` 最后一次修改之前启动，这两个文件另做了 ruff、format、pyright，通过）。本文作者没有改这些根文件，也没有运行 `uv sync`。
- **验证（2026-11-11 晚记录，保留为历史）**：模块本地测试 67 项通过（来自任务记录，本文作者只用 `--collect-only` 核对到 67 项，**未执行也未复跑**；记录里列出的 15 个文件的 sha256 与当时工作区一致），全部使用合成声纹和 GPS 数据，存储是内存实现 `MemoryStorage`；远程写入的重放测试用假 HTTP 和内存回执测试台，**当时的这些测试**不是真实 PostgreSQL/NATS，也不是运行中的 ① 服务。没有真实声纹、GPS、真实人名或模型；阈值没有校准，D2 认人准确率没有验证。全仓最终统一检查由协调者执行，当时待补记（记录位置见[实现说明第 11 节](ai-docs/implementation.md)）。
- **2026-11-12 更新：真实存储运行与 journal 启动恢复**。
  - 真实存储运行：`services/storage/tests/test_attribution_runtime.py` 共 5 项，在测试夹具起的临时 PostgreSQL、NATS 与 ① HTTP 服务上，经真实 relay 与 ② `Runner`（显式 capture id、合成 Evidence）跑通：入库→声纹归新人物、GPS 归未知地点→确认反馈→重判请求完成→重复投递幂等，以及提交结果未知时的原样重放。登记表是夹具里显式构造的，**不是 `DEFAULT_REGISTRY`**；身份由 `AuthOperator` 签发，**不是正式签发流程**。需要 `EAR_RUN_POSTGRES_TESTS=1` 与 `EAR_RUN_NATS_TESTS=1`。
  - journal 启动恢复**不再仅靠总线重投**：`AttributionService.subscribe()` 现在先调用 `Transactions.recover_pending()`，把私有 journal 里未决的写入按保存的请求 ID、操作和上下文原样重放（不重新规划），再注册订阅。原因是“提交已成功、回执丢失”时 `MarkProcessed` 已提交，重投的事件不会再进入处理函数，只靠重投清不掉这条日志。重放抛异常（结果未知、鉴权、传输）则保留日志条目并让 `subscribe()` 失败；写调用返回（包括返回冲突结果；这一点按源码，没有单独测试）则释放条目；默认最多 1000 条，超出报错要求显式恢复。覆盖它的只有上述文件里的 2 项（回执在提交前丢失、提交后丢失；重开 journal 与新客户端后订阅即恢复），是进程内重开，**不是 OS 强杀**，模块单元测试没有覆盖；仍没有命令行或常驻启动入口、超限的显式恢复工具。细节见[实现说明第 3、14 节](ai-docs/implementation.md)。
  - 合成联调：`tests/e2e/test_core_memory_loop.py`（2 项）用真实的 ②③④⑤ 类在 `MemoryStorage` 上跑一次纠正闭环（纠正反馈生效并受保护→⑤发出 2 个重判请求→④ 结果 changed、unchanged 各一→重复投递不再调用模型），模型是确定性的合成替身，不经 ⑦ 与 HTTP，**不是完整八步**。
  - 数字：整体验收（四开关全开，退出码 0，2485 passed、11 skipped）在 `service.py`、`transactions.py`、上述两个测试文件最后一次修改**之前**启动，所以不覆盖它们的当前内容；当前内容另有定向结果：任务日志 23:59:11Z 的 `test_attribution_runtime.py` 5 passed，以及本次复跑 `tests/e2e/test_core_memory_loop.py` 加 `services/attribution/tests` 共 69 passed、`test_attribution_runtime.py`（两个开关）5 passed。汇总与边界见[跑通计划第 2.2 节](../../ai-docs/plans/mvp-runnable.md)。
  - **首片实际边界不变**：未提交、未提 PR、未部署；`DEFAULT_REGISTRY` 仍没有 `attribution_state`、`attribution_feedback_result`，`attribution` 的重判与强制依赖能力仍为 false，核心登记表与 `contracts/` 没有改；没有真实声纹、GPS、模型、日历；阈值未校准；`create_place`、`reattribute_range` 仍未实现；没有正式身份签发和部署入口。（本条是 2.2 时点的记录：第二批已实现 `create_place`，新空库的身份签发（`init-core-identities`）与统一 worker 入口（`tools/mvp_worker.py`）现已在工作区，`reattribute_range` 仍未实现，见上面的第二批条目与[交接](../../ai-docs/plans/mvp-runtime-handoff.md)。）
- **2026-11-12 更新：journal 安全硬化（仅 Linux；未提交）**。提交前审查发现 journal 目录可经可写的非 sticky 祖先被替换，`sqlite3.connect` 会打开新路径而文件锁仍在旧目录；现已修复（新增 `secure_files.py` 与 `tests/test_journal_safety.py`，修改 `transactions.py`）。
  - **运行要求**：Linux（依赖 `/proc/self/fd`、本地 POSIX 权限与 `flock`）。journal 路径逐级不得含符号链接；每级祖先须属 root 或当前用户，且不得组或其他人可写（root 所有的 sticky 目录如 `/tmp` 除外）；终点目录须属当前用户且权限恰为 **0700**；数据库、`worker.lock`、回滚旁文件须属当前用户、是常规文件、权限恰为 **0600**、硬链接数为 1。权限或属主不符的既有目录与文件**直接拒绝，不自动修复**，不改 umask，不删除恢复证据。
  - **已有 WAL 拒绝，不转换、不删除**：数据库头部为 WAL 格式，或存在 `-wal`、`-shm` 旁文件，在连接 SQLite 之前就报错，不做模式转换、checkpoint 或旁文件清理，状态原样保留，由运维人员处置；目前没有恢复或迁移工具。
  - **未知结果不重发**：每次远程写入之前和本地事务前后都重新验证目录与文件身份，验证失败则不发送。结果未知的写入保留在 journal，只用保存的同一 request_id 与请求体原样重放，不重新规划、不新建请求。
  - **限制**：不是多机协调，不保证 NFS 或断电后的持久性；不抵抗恶意 root，也不保证抵抗同 UID 进程的任意竞态（替换类测试是确定性注入，不是并发穷举）；属主检查用合成 stat 结果测试；已有不符合上述要求的 journal 升级后会拒绝启动。细节与测试覆盖见[实现说明第 15 节](ai-docs/implementation.md)。
  - **验证（UTC，2026-11-12）**：③ 模块单元 **143 passed**（67 原有 + 76 新增；修复任务日志，作者复跑同为 143 passed）；真实 PostgreSQL/NATS `test_attribution_runtime.py` **5 passed**（修复任务日志 29.90 s；作者复跑同为 5 passed，29.81 s；临时容器、夹具登记表，不是 `DEFAULT_REGISTRY`）；修复后最新默认全量（未设任何开关）**1921 passed、653 skipped、2 warnings**（修复任务日志 33.55 s；作者复跑同为 1921 passed、653 skipped、2 warnings，33.42 s）。四开关整体验收的 2485 passed、11 skipped、2 warnings 是**修复之前的基线**，不能当作修复后的完整验收，修复后未重跑；上一条里的 69 passed（e2e 2 + 单元 67）也是硬化前的数字。汇总见[跑通计划第 2.3 节](../../ai-docs/plans/mvp-runnable.md)。
  - **提交注意**：`secure_files.py` 与 `test_journal_safety.py` 目前是**未跟踪文件**，`transactions.py` 的修改未暂存，提交时须一并纳入。仍未提交、未提 PR、未部署，没有真实数据，`contracts/` 与核心登记表没有改。

## 与设计、待决事项的关系

- **来源**：复用了 `origin/attribution/initial-implementation`（617fbc5，作者 yixiang-fan）的事件处理框架，用 `git show` 取内容、在当前基线上原地改造，没有 checkout 或 merge，也没有改写任何已推送历史。逐项复用与改动见[实现说明第 10 节](ai-docs/implementation.md)。`.github/CODEOWNERS` 里 `services/attribution/` 的负责人是 yixiang-fan。
- **待确认的实现选择**：该分支交接文档里的 P1–P7 仍是“待确认”。本切片为了写出可测试的代码，对 P1–P6 采用了具体的工程选择（payload 形状、命令字段、阈值参数、依赖粒度等），这些选择**待负责人确认，没有因为实现而被采纳**，清单见[实现说明第 13 节](ai-docs/implementation.md)。
- **用户确认保护不是待决项**：[设计 v1.2 第 6.2 节](../../docs/design/design-v1.2.md)已规定用户纠正或确认过的数据，任何重新归属均不得改变其归属结果。因此本切片中被纠正或确认的归属在移除参考后仍受保护，符合已采纳设计；它不是一个可配置的待决选择，也没有解除保护的开关。仍待确认的只是保护字段（`user_protected`、`protection_feedback_id`）的形状和旧数据缺字段时的兼容处理，见[实现说明第 13 节](ai-docs/implementation.md)。
- **审核**：按仓库规则功能 PR 面向 `dev`，由用户审核，不自动合并；涉及 `contracts/` 与核心登记表的改动按根 README 还须 ① 负责人与 I 审核（本文不假定谁担任这些角色）。本文和实现说明未经人工审核，不能当作已审定的设计。
