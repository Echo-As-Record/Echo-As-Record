---
audience: human
doc-type: guide
based-on: 设计 v1.2；当前④实现；本轮负责人授权④直接依赖⑦ GatewayClient
---

# ④ 活动提取

负责活动、规则、Markdown 产物及外部动作建议。当前仍是 v1.1 演示基线，不表示已完成 v1.2。

- [目标设计](../../docs/design/design-v1.2.md)、[模块间信号](../../docs/design/signals-supplement.md)。
- [使用与接入](ai-docs/implementation.md)：演示命令、宿主注入示例、契约差距与重试位置。
- [网页演示](ai-docs/web-demo.md)：合成数据和固定模型回复。

模型端口保留 `ModelGateway`，实际 HTTP 客户端统一使用 workspace 包 `ear-llm-gateway` 的 `GatewayClient`，构造时传④的 `SYSTEM`。客户端仍由⑦维护，协议见[⑦入口](../llm_gateway/README.md)。演示仍用测试替身。

每次 `complete(Call)` 都生成新的 call_id。传输错误 `gateway_outcome_unknown` 可能已经执行，不能自动换新 id 重试；可恢复流程须发送前持久化 call_id 并用 `reconcile` 核对，不能靠重新调用 complete 恢复。当前④的任务自动重试、存储冲突重算与租约恢复尚未适配此语义；重判也缺请求关联字段。此次仅统一客户端，未完成安全恢复或真实模型联调，详见[接入限制](ai-docs/implementation.md)。

GPS 输入按[已采纳逆地理编码提案](../../docs/proposals/gps-reverse-geocoding.md)等待②写完 `gps_geocode`；未完成时为 `geocode_pending`，终态结果作为原始证据进入模型。宿主需增加 `gps_geocode` 读授权，详见[接入说明](ai-docs/implementation.md#gps-地名等待)。
