---
audience: ai
doc-type: guide
generated-by: Codex
generated-at: 2026-11-05
based-on: docs/design/design-v1.2.md；dev 6cea0b7；本轮授权迁移与mock联调
---

# ④ 运行与接入

## 运行

仓库根目录执行：

```bash
uv sync --locked
uv run ear-extraction-demo
uv run ear-extraction-mock-integration
uv run pytest services/extraction/tests -q
```

第一个命令演示首次提取；第二个使用真实④、共享①内存实现及本地 HTTP mock ⑦，串联模拟②③⑤⑥。输出 JSON 检查报告；断言失败返回非零退出码。全部为合成数据，不调用真实模型或手机。

## 接入

```python
from ear_extraction import ExtractionService
from ear_extraction.worker import Schedule, serve

service = ExtractionService(storage.client("extraction"), gateway)
serve(service, stop_event, schedule=Schedule(start=start_utc))
```

- 一个部署只运行一个 worker。①提供 `StorageClient`；内存测试需显式投递事件。正式①持久化/远程订阅未联调。
- `gateway.complete(Call)` 对接⑦，携带 module、root_feedback_id、depth、run_id，重判时另带 request_id。HTTP 局部适配发送 Call + system，接收 `{ "output": {...} }`；不是已冻结的⑦协议。
- `HttpGateway(endpoint, token, trust_env=False)` 可关闭环境代理，本地 mock 使用此设置。生产端按部署需要选择。
- `service.runs()` 查询任务，`service.retry(run_id)` 显式重试终止任务。默认模型诊断只存用途、状态、耗时与追踪信息；短时调试可显式设置 `capture_model_content=True`。历史记录不会自动清洗，保留期/自动清理尚未实现。
- ④的诊断记录不能替代⑦统一调用记录；真实⑦应经①保存统计，模型请求经 Octopus Manager，不直连厂商。

## 反馈局部格式

⑥以自己的身份向①追加 feedback：`payload={"module":"extraction","command":{...}}`。确切校验见 `domain.Command`；公共字段仍待模块负责人对齐。

| kind | 主要字段 |
|---|---|
| extract | interval |
| reextract | interval、preview（`preview_reextract` 返回的 versions） |
| rule_create | rule |
| rule_update / rule_delete | target_id、expected_version；更新另带 rule |
| apply_rule / extract_once | target_id；分别带 rule_id / rule |
| review_suggestion | target_id、expected_version、decision |
| action_result | target_id、expected_version、outcome、receipt |

重判只经⑤的 `ReevaluationRequested(target_type="artifact")` 进入。任务 ID 使用请求 event_id，⑤通过 extraction_run 查询完成、无变化或失败；传播记录应使用自己的独立 ID，不能占用④任务 ID。

## 代码导航

- orchestration：受理、认领、重试、恢复、任务记录。
- segmentation / long_input：证据组装、划分、长输入合并。
- rules / artifacts / suggestions / planning：规则、产物、建议及原子写计划。
- persistence：共享存储读取；model_io：结构化模型端口；scheduler / worker：定时器。
- testing：模拟参与方、loopback HTTP 服务和可重复闭环。仅为验收夹具，不能作为正式③⑤⑥⑦部署。

产品目标、当前兼容处理和未完成部分见[迁移记录](v12-migration.md)。
