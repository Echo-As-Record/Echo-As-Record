---
audience: ai
doc-type: reference
generated-by: Codex (AI)
generated-at: 2026-11-05
based-on: services/extraction/ai-docs/mvp-design.md, docs/design/design-v1.1.md, docs/design/signals-supplement.md, dev 48d3c51；④ 直接依赖 ⑦ GatewayClient 的改动由 #13（c5c31f8）合入 dev（原写“本轮负责人授权”在仓库里没有可核对的出处，本文不当作用户授权记录），依据当前 client.py/models.py/orchestration.py；2026-11-11 增补：工作区未提交的④首片（call_safety.py、orchestration.py、domain.py、model_io.py、planning.py、worker.py、demo.py 与相应测试，作者读 git diff 与源码核对）；任务状态文件 /tmp/ear-mvp-extraction-followup.json 与 /tmp/ear-mvp-extraction-state.json（非仓库文件，其中的测试作者未复跑，只用 pytest --collect-only 核对项数）；2026-11-12 第二批增补依据：selection.py、reevaluation.py、artifacts.py、orchestration.py、segmentation.py、suggestions.py、domain.py 源码（作者读源码与 grep 核对，不运行测试），仓库外摘要 /tmp/ear-mvp-{action-http,eight-steps,action-evidence-fix}.json（作者读取并核对各自的退出码文件与 junit 用例名，测试结果未复跑）；本文不新增用户授权记录，也不增加用户决定；未填写或改动任何 reviewed 字段
---

# ④ MVP 使用与接入

> 本文记录已恢复的初版 MVP（`f3c28db`），用于演示，不代表已完成当前 v1.2 设计。
>
> 2026-11-11 增补：c5c31f8 之上有一个“调用安全与重判完成”首片，见“首片”一节。凡写“c5c31f8 基线”“基线上”的是首片之前的行为；首片也不是完整 v1.2。
>
> 2026-11-12 更新：该首片已在 79e8dc3（PR 16 为 OPEN，其 CI 成功只属于 79e8dc3）；v1.2 对齐与 action-v2 的 ④ 侧是**第二批**，只在工作区，未提交、未提 PR、未部署，见新增的“第二批”一节。旧节里与它冲突的说法（纯 Markdown、规则历史阻塞、引用标记与粗粒度依赖、“没做”清单）以新节和 [④ README](../README.md) 为准；旧句子保留作各自时点的记录。


网页模拟入口与 VS Code 端口转发步骤见[网页演示指南](web-demo.md)。

## 运行演示

在仓库根目录运行：

```bash
uv sync --locked
uv run ear-extraction-demo
uv run pytest services/extraction/tests -q
```

演示创建合成的录音转写、声纹、人物和归属，通过⑥身份写入规则与提取反馈，再由④消费。输出活动、Markdown 产物、提醒建议和任务状态。默认使用固定模型回复和①内存存储，不访问网络、不创建真实提醒。

## 本版功能

- 手动提取、规则增删改、历史应用、只改本次、带版本预览的重新提取。
- 规则选择、引用校验、产物和粗粒度依赖原子提交。选规则使用活动概述和归属信息，产物生成读取原始证据。
- 归属重判：活动不变；正文不变则保持版本并刷新依赖；正文变化时替代待审核建议。
- 审核、执行回执状态机；已通过、已执行、已拒绝的建议不被重判改写。
- 定时周期规划、成功区间扣除、重复事件去重、有限重试、前置等待上限和任务恢复。
- 七个内部模块对应源码中的 orchestration、segmentation、rules、artifacts、suggestions、persistence、model_io；planning 组织业务计划，scheduler/worker 是调度辅助。

## 集成入口

```python
from ear_extraction import ExtractionService
from ear_extraction.model_io import SYSTEM
from ear_extraction.worker import Schedule, serve
from ear_llm_gateway.client import GatewayClient

with GatewayClient(gateway_url, extraction_token, system=SYSTEM) as gateway:
    service = ExtractionService(storage.client("extraction"), gateway)
    # 宿主注入时区为 UTC 的起始时间和停止信号；一个部署只运行一个 worker。
    serve(service, stop_event, schedule=Schedule(start=start_utc))
```

- `storage` 由①提供。`service.subscribe()` 只订阅一次；①负责真实事件投递。内存演示需显式 `storage.run_until_idle()`。
- `gateway.complete(Call)` 是⑦接入端口。Call 包含 module、root_feedback_id、depth、run_id、purpose、data、output_schema。
- ④直接依赖 workspace 包 `ear-llm-gateway`，HTTP 实现统一使用 `GatewayClient`；保留 `ModelGateway` Protocol 供注入与测试替身。客户端仍在⑦，不移入 libs。`gateway_url` 是不带路径的网关根地址；`extraction_token` 是④自己的①令牌（43 位），由宿主安全注入。必须传 `system=SYSTEM`，否则客户端默认空提示词。客户端 POST `/v1/complete`，只返回已保存调用记录后的对象 `output`；不跟随重定向、不自动重试，宿主负责关闭连接。
- 不提供厂商直连。⑦接入 Octopus 时应通过 Manager 的 `/api/v1/llm/...`；④不绕过⑦。
- `service.runs()` 查询状态，`service.retry(run_id)` 显式重试终止任务；⑥可封装为受授权操作。本包不新建一套用户 HTTP API。

## ⑦ 契约核对与恢复限制（c5c31f8 基线）

> 本节的“目前”“当前”指 c5c31f8 基线。工作区首片已改变其中关于 `request_event_id`/`additional_causes`、`call_id` 持久化、自动重算位置的几条，见下一节；改动未提交。

- ④ `Call` 的七个字段均被⑦ `CallRequest` 接受，客户端补 `call_id`、`model`、`system`。两者校验强度不相同：⑦要求 run_id 为小写 UUIDv7、root 为 UUIDv7 或 `system`、depth 为 0–10000 的整数、purpose 为不超过 128 字符的 snake_case。④正常任务 run_id 来自事件 UUIDv7 或 `schedule_id` 生成的稳定 UUIDv7，现有 purpose 均合法，无需改 ID 生成或提示词。任意手造的④ Call 不保证通过⑦校验。
- ④目前不传 `request_event_id` / `additional_causes`。请求结构可以通过 `CallRequest`，但⑦服务要求无请求关联时 depth=0；因此 depth>0 的重判会被服务拒绝 `request_context_required`（客户端呈现 `gateway_request_failed`），depth=0 也不代表已完成重判请求关联。该跨模块差距本轮只报告，不重置 depth、不放宽⑦校验。
- 每次 `complete(Call)` 都生成新的 call_id，即使 run_id 和参数相同。传输错误 `gateway_outcome_unknown` 可能表示模型已经执行，**不能自动换新 id 重试**。可恢复流程需要在发送前持久化 call_id，构造带该 ID 的⑦ `CallRequest`，随后用 `reconcile(call_id)` 核对/补写记录；reconcile 不重发模型，也不恢复输出正文。当前④没有这套持久化与对账流程，不能把换客户端视为安全恢复已经完成。
- 现有自动重算位置均在 `orchestration.py`：`process` 的通用异常分支（包括 GatewayError）进入 retry_wait；业务提交存储冲突同样进入 retry_wait；`drain` / `process` 在重试期限或 running 租约到期后重新计算，`worker.tick` / `serve` 持续驱动。`_finish` 只限制尝试次数，不能防止重复模型调用。`retry(run_id)` 是显式重新排队，也不做对账。调用成功但后续提交失败或进程崩溃同样可能重发。按本轮授权边界保留上述业务流程；接真实⑦前仍需解决该风险。
- 依赖方向为⑥ → ④ → ⑦ → 共享契约/存储 SDK；⑦运行包没有反向依赖④或⑥。⑦现有跨模块测试导入④属于根 workspace 测试，不是运行包依赖。

## 首片：调用安全与重判完成（79e8dc3；2026-11-11 晚写作时尚在工作区）

目的：对应[跑通计划](../../../ai-docs/plans/mvp-runnable.md) T1——模型调用结果未知时不重发、重判请求的上下文完整传递、重判完成能写入 ①。范围有界，**不是完整 v1.2**。路径相对 `src/ear_extraction/`。

### 代码导航

| 位置 | 内容 |
|---|---|
| `call_safety.py` | `DurableCalls(storage, gateway, run_id, payload, revision, context)`。`complete(call)`：生成 `call_id`，构造 `CallRequest`（含网关实际的 `model` 与 `system`），对规范化请求取 SHA-256 作指纹（不存提示词），把 `{call_id, purpose, request_fingerprint, state: "sending"}` 追加到 `payload.gateway_calls` 并以 `expected_revision` CAS 写入 `extraction_run`，**写成功后才调用网关**；任何异常（含 `GatewayError`）一律抛 `Blocked("gateway_outcome_unknown")`；返回后把状态存为 `returned`。CAS 失败抛 `Blocked("run_fence_lost")`，不发送。`GatewayClient` 用带固定 `call_id` 的 `CallRequest` 调用，其他实现走 `ModelGateway.complete(Call)` |
| `orchestration.py` | `reevaluation_run_id(request_event_id)`：域分隔、确定性的 UUIDv7，不等于请求 ID；`COMPLETION_CONSUMER = "extraction.completion"`；`_retry_barrier(run)`；`reconcile_run(run_id)`；`_held_range(run)`；`process` 里的 `DurableCalls` 与重判分支；`handle_event` 对 `ReevaluationRequested` 的兼容处理 |
| `domain.py` | `Call` 增加可选的 `call_id`、`request_event_id`、`additional_causes`；序列化时省略缺失项，保持旧假实现的形状 |
| `model_io.py` | `invoke(...)` 增加 `request_event_id`、`additional_causes` |
| `planning.py` | `rejudge_plan` 返回 `(ops, detail, before_version, outcome)`，`outcome` 为 `changed` 或 `unchanged` |
| `worker.py` | `tick` 在客户端有 `poll_once` 时先 `poll_once(CONSUMER)` 拉取一条投递；内存客户端仍由宿主驱动；不轮询其他 consumer |
| `demo.py` | 演示夹具用本地登记表打开 `artifact` 的重判能力，不改核心登记表 |

### `extraction_run` 的新增字段

`request_event_id`、`additional_causes`、`request_occurred_at`（重判请求时间，用于 `completed_at = max(clock, request_occurred_at)`）、`gateway_calls`（`call_id`、`purpose`、`request_fingerprint`、`state` 为 `sending` 或 `returned`）。重判 run 的 `event_id` 仍记原请求事件，run id 另行派生；`_admit` 对已存在的记录要求 `event_id`、`request_event_id`、`scheduled` 都匹配才视为重复，否则抛 `Blocked("extraction_run_id_occupied")`。

### 阻塞原因（`_retry_barrier`）

| 原因 | 条件 | 行为 |
|---|---|---|
| `gateway_outcome_unknown` | 最后一次调用仍是 `sending` | `process` 直接阻塞；`retry(run_id)` 抛 `ValueError(原因)` |
| `gateway_result_not_committed` | 有调用记录且最后一次为 `returned`，但业务计划没能提交 | 阻塞，**不重新计算**（本首片没有输出恢复，重算就是新的逻辑调用） |
| `legacy_gateway_outcome_untracked` | 升级前的模型命令可能已发送但没有意图记录（`attempts>0`、`running`、`retry_at` 或 `lease_until` 存在） | 阻塞 |
| `legacy_completion_id_collision` | 旧 `extraction_run` 占用了重判请求的 event_id | 未完成的置为阻塞；已终态的不改写、不迁移、不换 id |
| `reevaluation_request_required` | `reevaluate` 命令缺请求上下文 | 阻塞或在接纳时失败 |

被这些原因阻塞的区间还会被 `_held_range` 排除在“成功覆盖”之外，定时任务遇到重叠的更大区间时以 `overlapping_unresolved_model_call` 阻塞，避免换一个 run id 隐式重发。`reconcile_run(run_id)` **只做审计**：返回 `call_id`、`record_saved`、`status`、`error_code`、固定的 `response_recoverable: False`，不返回模型输出，也不改变重试资格；在 `gateway_outcome_unknown` 时 `_finish` 会尽力调用一次（吞掉错误）。**目前没有解除这些阻塞的处置路径**：“用户确认重复风险后作为新的逻辑调用重新生成、并留下与旧 id 的对应”没有实现。

### 重判与完成

1. `handle_event(ReevaluationRequested, target_type="artifact")`：已有 `get_reevaluation_result` 则返回；若有 id 等于请求 event_id 的旧 `extraction_run`，`event_id` 或命令类型不符抛 `Blocked("reevaluation_request_id_occupied")`，否则未终态的置阻塞后返回；其余情况用 `reevaluation_run_id` 接纳，`MarkProcessed(extraction.jobs, event_id)` 同批提交。
2. `process`：先看 `_retry_barrier`；认领后以 `DurableCalls` 为模型端口，每次调用都带请求的 root、depth、`request_event_id`、`additional_causes`。
3. 重判分支要求 `registry.data_types["artifact"].supports_reevaluation`，否则 `Blocked("artifact_reevaluation_not_enabled")`；已有结果则 `Blocked("reevaluation_already_completed")`。成功时同批提交：业务写入、依赖刷新、`FinishReevaluation(ReevaluationResult(...))`、`MarkProcessed(extraction.completion, request_id)`、`extraction_run` 成功（`expected_revision=tracker.revision`）。`unchanged` 不新增 artifact 版本，不发业务事件。
4. 提交失败：版本冲突进 `retry_wait`（若已有未知调用则改为阻塞）、`Waiting`、`Blocked`、其他异常按原规则；最终 `_finish` 用 `tracker.payload` 写，避免丢掉调用记录。

### 订阅与身份

只有 `extraction.jobs` 有消息订阅。`extraction.completion` 只是完成标记的保留身份，不是第二个任务订阅；真实运行测试里，可信的 extraction 主体同时保留二者，同模块的对等主体既不能保留也不能标记。

### 测试

| 范围 | 文件与内容 | 运行条件 |
|---|---|---|
| 单元（内存） | `tests/test_call_safety.py`：未知结果不因重启、重试或 reconcile 重发；reconcile 报错不放行；崩溃窗口只恢复到阻塞；意图提交结果未知时未确认不发送；CAS 冲突在发送前围住失败方；旧记录有无尝试的处理；定时器不能用更大区间绕过；业务提交结果未知被围住而不是重算；返回确认丢失不启动下一步；租约过期后仍在 provider 内的 worker 被围住。`tests/test_completion_integration.py`：Finish 使用真实请求 id 与上下文；旧请求 ID 冲突不重排队；反馈不能冒充重判请求；未知重判只留接纳记录；终态旧记录不被改写。`test_boundaries.py`、`test_extraction.py` 有小改 | 无外部服务 |
| 真实运行 | `services/storage/tests/test_extraction_completion_runtime.py`：真实 PostgreSQL、NATS，回环的 ⑦ 网关与 ① HTTP，进程内合成 provider；用独立的登记表夹具（只在夹具里打开 artifact 重判与强制依赖），子进程模拟重启。覆盖：真实 Finish 与重复投递、HTTP 提交结果未知（认领/发送/返回/Finish 各阶段，提交与未提交两种）、未知 provider、旧请求 ID 冲突跨进程保持、并发 CAS 只有赢家发送、迟到 CAS 冲突整批回滚、并发输入变化阻止 Finish、`worker.tick` 真实拉取 NATS | `EAR_RUN_POSTGRES_TESTS=1` 与 `EAR_RUN_NATS_TESTS=1`，需要 Docker；只在临时容器运行，不连外部 DSN |

### 验证记录（来源：任务记录，作者未复跑）

| 项目 | 结果 | 来源与维度 |
|---|---|---|
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider services/extraction/tests` | 84 项通过 | 任务记录 2026-11-11T20:05Z；作者 `--collect-only` 收集到 84 项，未执行。输入合成｜模型回放｜外部不适用｜平台替身 |
| 真实运行测试 | 17 项通过、0 跳过，**分 8 组不相交的 `-k` 选择运行，不是一次连续的完整运行** | 任务记录；作者收集到 17 项，未执行。输入合成｜模型回放（进程内合成 provider）｜外部不适用｜平台真实（测试夹具起的临时 PostgreSQL/NATS、回环 ⑦ 网关与 ① HTTP） |
| 范围内的 ruff、format、pyright、`git diff --check` | 通过 | 任务记录；范围为 `services/extraction/src`、`services/extraction/tests` 与上述真实运行测试文件 |
| 全仓 pytest、ruff、pyright、`check_docs` | 当时失败：1771 passed、26 failed（全在 ② 的 `test_source.py`）、648 skipped；静态检查与文档检查也有失败 | 任务记录，**历史结果**，发生在其他模块并行修改时，不是当前状态，也不是 ④ 的失败 |

分组运行的方式（需要 Docker，作者未执行）：

```bash
env EAR_RUN_POSTGRES_TESTS=1 EAR_RUN_NATS_TESTS=1 EAR_RUN_BACKUP_TESTS=0 EAR_RUN_API_GATEWAY_DEPLOY_TESTS=0 \
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -x -p no:cacheprovider \
  services/storage/tests/test_extraction_completion_runtime.py -k '<选择>'
```

任务记录里用过的 `-k` 选择：`live_finish`；`unknown_provider`；`legacy_completion or concurrent_input`；`real_http_commit and claim`；`real_http_commit and sending`；`real_http_commit and returned`；`real_http_commit and finish`；`late_cas or concurrent_http or worker_tick`。

### 首片没有做的（2026-11-11 晚的清单；其中纯 Markdown、`rule_selection` 的登记与版本化、按当前规则重判、字段依赖、`failed`/`not_applicable` 的 Finish、统一 worker 作为远程运行入口已在第二批实现，见下一节）

完整 v1.2 对齐（纯 Markdown、`rule_selection` 的登记与版本化、按当前规则重判、字段依赖）；网关输出恢复与业务计划重放；风险确认后的新调用；终态 `failed`、`not_applicable` 的 Finish；旧记录迁移；远程运行入口、部署与真实模型联调；对网关、数据库、总线做操作系统级强杀的验证（只做了新子进程重启与故障注入）。核心登记表、`contracts/`、根配置、锁文件和 ① 实现都没有改。一个部署只运行一个 worker；进程恢复需要持久化的 ①，`MemoryStorage` 不跨进程保存数据。

## 第二批：v1.2 对齐与 action-v2 的 ④ 侧（工作区，未提交；2026-11-12）

人读的行为与限制见 [④ README](../README.md)，产品语义以[设计 v1.2 第 7 节](../../../docs/design/design-v1.2.md)为准，这里只补代码导航与不变量，不复述。路径相对 `src/ear_extraction/`。

| 位置 | 内容 |
|---|---|
| `selection.py` | 每个活动一份版本化的 `Selection`（核心类型 `rule_selection`，含空候选与空选择）；产物**不依赖**选择结果；候选集合以 `AssertTypeRevision` 在同一批里守护；按当前规则全集重选：新选中的生成自动产物与建议，退出的替代自动产物与仅 pending 的建议（`suggestions.pending_replacements`），手动来源的受保护 |
| `reevaluation.py` | `ReevaluationPlan(ops, detail, before, after, outcome, reason_code, disposition)`：`outcome` 为 `changed`/`unchanged`/`not_applicable`，`disposition` 为 `terminated`/`unavailable`/`kept_protected`；`unavailable`、`terminate` 构造规则被删除或替代时的终止计划；规则历史里发现的额外起因连同贡献它们的 root 与 depth 一起带回，不凭空造 root |
| `artifacts.py` | `create_artifact`、`input_dependencies`、`validate_generated`：新输出含 `[[person:…]]`、`[[place:…]]` 引用标记即拒绝（`legacy_entity_marker_in_new_output`），旧文本不改写、不反向解析成依赖 |
| `segmentation.py`、`planning.py` | 活动划分与合并只读原始数据；归属就绪检查与实体解析分开，分段之后才组装人物与地点；实体输入只投影 `payload.name`（保留缺失与 null 的区别），登记名字字段依赖，每个区间归属仍是粗粒度依赖 |
| `orchestration.py` | 未登记 `rule_selection` 时在调用模型**之前** `Blocked("rule_selection_registration_required")`；发现的额外起因写入 `execution_additional_causes`，在发送模型之前持久化，并传给 ⑦、业务写入与 Finish；changed、unchanged、not_applicable、failed 四种结果都提交真实的 `FinishReevaluation`，完成标记与 run 更新原子提交；已知非法输出和模型前的重试耗尽可 Finish 为 `failed`，failed 的请求不可变，再试需要新请求。`Command.target_type` 为 `artifact`（默认，旧命令序列化形状不变）或 `rule_selection` |
| `suggestions.py`、`domain.py` | action-v2 的 ④ 侧：`Command.outcome` 增加 `executing`、`unknown`，`Command.execution`（`ActionExecution`，含 `action_key`、`action_hash`、`approved_version`、`attempt`、`acknowledge_duplicate_risk`、`acknowledge_executed`，`operation` 取 `start`/`result`/`confirm_executed`/`retry`）；`execution_key`、`execution_payload` 在业务提交里校验 `approved → executing → executed|failed|unknown`，`unknown`/`failed` 经带风险确认的重试回到 `executing`（同一 `action_key`、attempt 加一）；动作哈希或批准版本不符就阻塞，不变基；没有 v2 交接的建议仍接受旧的终态回执，`executing`/`unknown` 没有 `execution` 时拒绝（`execution_protocol_required`），带 `execution` 必须带严格整数的 `expected_version` |

- **测试**：新增 `tests/test_v12.py`、`tests/test_execution_protocol.py`，并修改 `test_extraction.py`、`test_completion_integration.py`、`test_web_demo.py`；真实运行在 `services/storage/tests/test_extraction_v12_runtime.py`、`test_action_protocol_runtime.py`（真实 PostgreSQL，NATS 视用例而定，回环 ① HTTP、合成网关）和仓库根 `tests/test_mvp_eight_steps_runtime.py`（真实 CLI worker、`GatewayClient` 与网关 HTTP、合成 provider）。
- **证据（合成；作者未复跑，只核对退出码文件、日志末行与 junit 用例名）**：04:42Z 的 action-http 摘要里 API 与 ④ 全部测试 589 passed，真实的 `test_action_protocol_runtime.py` 加 `test_extraction_v12_runtime.py` 9 passed（PG+NATS，187.66 s）；06:19Z 的两个八步用例里，④ 在独立 CLI 进程里按真实定时（一个完整的 1800 s 周期）提取，纠正后 changed 产物一份、unchanged 产物字节相同、`rule_selection` 不变、pending 建议被替换，经审核与外部动作推进到 `executed`，每用例 7 次权威 `llm_call`（合成 provider），耗时与范围见[交接第 6 节](../../../ai-docs/plans/mvp-runtime-handoff.md)；06:37Z 的证据修复后，API 与 `services/extraction/tests/test_execution_protocol.py` 共 649 passed（无 PG/NATS）。八步用例在 06:24Z 的 ⑥ 证据修复之前运行，没有在修复之后重跑；新的四开关全量验收由主 agent 委派，运行中，结果未出，不预填，补入交接第 7 节。
- **仍没做**：网关输出恢复与业务计划重放；用户确认重复风险后的新逻辑调用；旧活动不回填，旧记录不迁移；真实模型联调；对网关、数据库、总线的 OS 强杀恢复验证；外部动作的真实适配器与执行 worker（在 ⑥，命令行不注入适配器）。

## 反馈格式（模块局部草案）

⑥使用自己的①客户端写入 `feedback` 原始记录，payload 为：

```json
{
  "module": "extraction",
  "command": {
    "kind": "extract",
    "interval": {"start": "2026-11-05T09:00:00Z", "end": "2026-11-05T10:00:00Z"}
  }
}
```

Command 的确切字段和校验见 `domain.py`；反馈根源采用该反馈自身 ID。格式仍需⑥对齐，未修改公共 contracts。

| kind | 必需字段 |
|---|---|
| extract | interval |
| reextract | interval、preview（`preview_reextract(interval)` 返回的 versions） |
| rule_create | rule（scope、instruction，可选 action、activity_id、enabled） |
| rule_update | target_id、expected_version、rule |
| rule_delete | target_id、expected_version |
| apply_rule | target_id（活动）、rule_id |
| extract_once | target_id（活动）、rule |
| review_suggestion | target_id、expected_version、decision=approved/rejected |
| action_result | target_id、expected_version、outcome=executed/failed、receipt（回执去重标识，不是外部条目 ID） |

⑤使用现有 `ReevaluationRequested(target_type="artifact")`。④不会对普通归属变化直接执行模型任务。c5c31f8 基线：无变化完成结果保存在 extraction_run.detail，供⑤按事件 ID（也是 run ID）读取，不新增总线事件；该做法让 run 占用了请求 event_id，使 ① 的 Finish 无法写入。工作区首片：重判改为提交 `FinishReevaluation`，run id 另行派生，见上一节。

②③输入适配草案：转写引用对应 voiceprint_id 或局部说话人来源；归属 payload 使用①建议的 subject_id、target_type、target_id；未知人物/地点应有合法目标对象。字段需相关负责人对齐，不由④写人物或归属。完整演示见 demo.fixture。

## 运行保证与限制

- 收到事件时原子写入任务和 MarkProcessed；任务最终结果与业务写入同批提交。认领任务用① revision 做 fencing；运行中崩溃后，租约到期可由新 worker 重新计算。
- c5c31f8 基线：只保证业务提交去重，模型调用在失败恢复时可能重复。工作区首片对可能已执行的调用改为停止、不重发（见上一节），但不保证任意外部 exactly-once。一个部署使用一个 worker；⑥的真实外部动作幂等不属于本包。
- ①内存实现不会跨进程保存数据。本版验证的是在①保留记录时重建④执行器的恢复行为；正式进程重启保证需要持久化①。
- 模型输入有显式字符预算。长输入按不截断话语的边界分窗划分活动，再判断相邻活动是否合并；长产物分段生成后合并并校验原始证据引用。单条证据、合并请求或重判输入仍超预算时明确失败并保留旧结果，不静默截断。合成全天数据测试不代表已经通过真实全天音频验收。
- （2026-11-12 更新）第二批里重判读同一规则 id 的**当前**版本，规则出新版本时不再阻塞，`historical_rule_dependency_unsupported` 已不在源码中；规则被删除或替代时终止产物并以 `not_applicable` 完成。本条原写“返回 blocked / historical_rule_dependency_unsupported，保留原产物”是首片与 v1.1 基线的行为，与已采纳设计 v1.2（历史重判用当前规则）冲突，已被第二批取代，细节见 [④ README](../README.md)。
- 半区间重新提取遇到跨界旧活动返回明确错误，要求选择完整活动；不自动扩大删除范围。
- 原始依据和模型输入/输出写入运行记录用于追溯，普通日志仅含 ID、状态和错误类型。输入引用的结构校验不能代替对模型语义准确性的评测。
- （2026-11-12 更新）新产物已是纯 Markdown：新输出里的 `[[person:…]]`、`[[place:…]]` 引用标记被拒绝，字段级依赖按已采纳设计 v1.2 实现，旧文本不改写（第二批，见上面“第二批”一节）。本条原写“本版仍使用人物/地点引用标记和粗粒度依赖”是 v1.1 演示基线的行为；仍不修改 `contracts/`。

## 验证范围

测试覆盖真实共享存储接口上的事件重复投递、原子回滚、归属版本冲突、重判不变/改变、审核回执、旧规则阻塞、任务重试/恢复、定时区间、模型输出校验与 HTTP 适配边界。模型回复为合成测试数据。

真实⑦模型调用、真实②③数据和手机执行尚未验证；本版可作为这些模块的接入基线。首片另用测试夹具起的真实 PostgreSQL/NATS 与回环 ⑦ 网关做过分组验证（来源与限制见“首片”一节），第二批的合成验证见“第二批”一节（含真实 CLI worker、`GatewayClient` 与合成 provider 的八步用例，不是 Compose 部署，也没有真实模型），都不是持久化 ① 的部署验收。
