---
audience: ai
doc-type: reference
generated-by: Codex (AI)
generated-at: 2026-11-05
based-on: services/extraction/ai-docs/mvp-design.md, docs/design/design-v1.1.md, docs/design/signals-supplement.md, dev 48d3c51
---

# ④ MVP 使用与接入

> 本文记录已恢复的初版 MVP（`f3c28db`），用于演示，不代表已完成当前 v1.2 设计。


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
from ear_extraction.worker import Schedule, serve

service = ExtractionService(storage.client("extraction"), gateway)
# 宿主注入时区为 UTC 的起始时间和停止信号；一个部署只运行一个 worker。
serve(service, stop_event, schedule=Schedule(start=start_utc))
```

- `storage` 由①提供。`service.subscribe()` 只订阅一次；①负责真实事件投递。内存演示需显式 `storage.run_until_idle()`。
- `gateway.complete(Call)` 是⑦接入端口。Call 包含 module、root_feedback_id、depth、run_id、purpose、data、output_schema。
- `HttpGateway(endpoint, token)` 提供候选 HTTP 适配：向配置地址 POST Call 和 system，期望 `{ "output": {...} }`。⑦尚未实现，该 HTTP 格式需要⑦负责人对齐，不是仓库已有部署接口。
- 不提供厂商直连。⑦接入 Octopus 时应通过 Manager 的 `/api/v1/llm/...`；④不绕过⑦。
- `service.runs()` 查询状态，`service.retry(run_id)` 显式重试终止任务；⑥可封装为受授权操作。本包不新建一套用户 HTTP API。

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
