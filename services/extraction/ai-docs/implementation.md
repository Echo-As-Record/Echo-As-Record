---
audience: ai
doc-type: reference
generated-by: Codex (AI)
generated-at: 2026-11-05
based-on: services/extraction/ai-docs/mvp-design.md, docs/design/design-v1.1.md, docs/design/signals-supplement.md, dev 48d3c51；本轮负责人授权④直接依赖⑦ GatewayClient，依据当前 client.py/models.py/orchestration.py
---

# ④ MVP 使用与接入

> 本文记录已恢复的初版 MVP（`f3c28db`），用于演示，不代表已完成当前 v1.2 设计。


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

## ⑦ 契约核对与恢复限制

- ④ `Call` 的七个字段均被⑦ `CallRequest` 接受，客户端补 `call_id`、`model`、`system`。两者校验强度不相同：⑦要求 run_id 为小写 UUIDv7、root 为 UUIDv7 或 `system`、depth 为 0–10000 的整数、purpose 为不超过 128 字符的 snake_case。④正常任务 run_id 来自事件 UUIDv7 或 `schedule_id` 生成的稳定 UUIDv7，现有 purpose 均合法，无需改 ID 生成或提示词。任意手造的④ Call 不保证通过⑦校验。
- ④目前不传 `request_event_id` / `additional_causes`。请求结构可以通过 `CallRequest`，但⑦服务要求无请求关联时 depth=0；因此 depth>0 的重判会被服务拒绝 `request_context_required`（客户端呈现 `gateway_request_failed`），depth=0 也不代表已完成重判请求关联。该跨模块差距本轮只报告，不重置 depth、不放宽⑦校验。
- 每次 `complete(Call)` 都生成新的 call_id，即使 run_id 和参数相同。传输错误 `gateway_outcome_unknown` 可能表示模型已经执行，**不能自动换新 id 重试**。可恢复流程需要在发送前持久化 call_id，构造带该 ID 的⑦ `CallRequest`，随后用 `reconcile(call_id)` 核对/补写记录；reconcile 不重发模型，也不恢复输出正文。当前④没有这套持久化与对账流程，不能把换客户端视为安全恢复已经完成。
- 现有自动重算位置均在 `orchestration.py`：`process` 的通用异常分支（包括 GatewayError）进入 retry_wait；业务提交存储冲突同样进入 retry_wait；`drain` / `process` 在重试期限或 running 租约到期后重新计算，`worker.tick` / `serve` 持续驱动。`_finish` 只限制尝试次数，不能防止重复模型调用。`retry(run_id)` 是显式重新排队，也不做对账。调用成功但后续提交失败或进程崩溃同样可能重发。按本轮授权边界保留上述业务流程；接真实⑦前仍需解决该风险。
- 依赖方向为⑥ → ④ → ⑦ → 共享契约/存储 SDK；⑦运行包没有反向依赖④或⑥。⑦现有跨模块测试导入④属于根 workspace 测试，不是运行包依赖。

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

⑤使用现有 `ReevaluationRequested(target_type="artifact")`。④不会对普通归属变化直接执行模型任务。无变化完成结果保存在 extraction_run.detail，供⑤按事件 ID（也是 run ID）读取，不新增总线事件。

②③输入适配草案：转写引用对应 voiceprint_id 或局部说话人来源；归属 payload 使用①建议的 subject_id、target_type、target_id；未知人物/地点应有合法目标对象。字段需相关负责人对齐，不由④写人物或归属。完整演示见 demo.fixture。

## 运行保证与限制

- 收到事件时原子写入任务和 MarkProcessed；任务最终结果与业务写入同批提交。认领任务用① revision 做 fencing；运行中崩溃后，租约到期可由新 worker 重新计算。
- 只保证业务提交去重，模型调用在失败恢复时可能重复。一个部署使用一个 worker；⑥的真实外部动作幂等不属于本包。
- ①内存实现不会跨进程保存数据。本版验证的是在①保留记录时重建④执行器的恢复行为；正式进程重启保证需要持久化①。
- 模型输入有显式字符预算。长输入按不截断话语的边界分窗划分活动，再判断相邻活动是否合并；长产物分段生成后合并并校验原始证据引用。单条证据、合并请求或重判输入仍超预算时明确失败并保留旧结果，不静默截断。合成全天数据测试不代表已经通过真实全天音频验收。
- 重判遇到原规则已有新版本时，返回 blocked / historical_rule_dependency_unsupported，保留原产物。①“依赖必须是当前版本”与设计“使用原规则”的冲突仍待①④⑤决定，没有静默改用新规则。
- 半区间重新提取遇到跨界旧活动返回明确错误，要求选择完整活动；不自动扩大删除范围。
- 原始依据和模型输入/输出写入运行记录用于追溯，普通日志仅含 ID、状态和错误类型。输入引用的结构校验不能代替对模型语义准确性的评测。
- 新的依赖细化提案尚未审定；本版仍使用人物/地点引用标记和粗粒度依赖，不修改原契约。

## 验证范围

测试覆盖真实共享存储接口上的事件重复投递、原子回滚、归属版本冲突、重判不变/改变、审核回执、旧规则阻塞、任务重试/恢复、定时区间、模型输出校验与 HTTP 适配边界。模型回复为合成测试数据。

真实⑦模型调用、真实②③数据、手机执行和持久化①的跨进程联调尚未验证；本版可作为这些模块的接入基线。

## GPS 地名等待

按[已采纳提案](../../../docs/proposals/gps-reverse-geocoding.md)，`segmentation.inputs()` 在原有归属检查之后，使用同一半开时间范围查询 `gps_point` 的 `unreferenced_by=("gps_geocode", "gps_point_id")`；未完成抛 `Waiting("geocode_pending")`。`RAW_TYPES` 增加 `gps_geocode`，连同坐标进入模型 evidence；ok、no_result、failed 都表示编码已结束，不把原始地名当作③的 place。

宿主授予 extraction 身份 `gps_geocode` 读权限（原有 gps_point/attribution 权限仍需保留）。无需新增订阅或依赖类型：活动仍按现有流程保存 evidence_ids，未启用新的强制依赖能力。等待期限与重试流程保持原样，不额外改变④调度语义。相关回归在② `tests/test_geo.py`，涵盖半开范围、等待和三种终态进入证据。
