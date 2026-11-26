---
audience: ai
doc-type: guide
based-on: 用户 2026-11-26 对下游聊天证据集成的实施授权（提取侧显式默认关闭开关；纯聊天与聊天加录音混合在同一次处理中走现有通用流程、worker 与网关；每个对象取最高修订；助手内容按陈述而非完成处理；三类登记 writer 均为 adapter；先用隔离合成数据验证）；services/adapters/ai-docs/librechat-source.md 的 payload 约定与 convert.py 的实际输出；services/extraction 的 orchestration.py、planning.py、segmentation.py、selection.py、artifacts.py 当前源码；services/storage 的 core_deployment.py、health.py、schema.py 与 services/api 的 business.py（只读核对）；本轮合成测试（作者在本机运行，见“测试”一节）
---

# 聊天证据（`chat_evidence`）：提取侧接入

人读的行为说明见 [④ README](../README.md#宿主可选聊天证据chat_evidence)。本文是代码导航、不变量、部署前提和限制。**未部署、未接真实 LibreChat 数据或真实模型**；全部验证使用合成数据与假模型。

## 范围

- 数据来自 [LibreChat 源适配器](../../adapters/ai-docs/librechat-source.md) 写入 ① 的 `chat_session`、`chat_message`、`chat_attachment`（`writer=adapter`），提取只读。
- 开关为宿主私有 JSON `chat_evidence: true`，由 `tools/mvp_worker.py` 传给 `ExtractionService`。默认关闭，只允许通用提取。
- 开启后走**原有通用流程**（`extract_plan` 的 `else` 分支：`raw_inputs` → `segment` → `inputs` → `create_selection`）：同一个 `storage.snapshot()`、`DurableCalls`（调用意图、指纹、未知结果不重发）、同一个 `GatewayClient`/`ModelGateway`、同一批原子提交。没有新 worker、新模型端口或新存储路径。

## 代码导航（路径相对 `src/ear_extraction/`）

| 位置 | 内容 |
|---|---|
| `chat_evidence.py` | 常量与 `CHAT_EVIDENCE_POLICY`；`read_chat_evidence`（三类查询、授权缺失 → `Blocked`）；`latest_revisions`（校验、取最高修订、孤立会话丢弃）；`chat_row`（发给模型的封闭投影）；`evidence_status`/`grounds`/`has_grounding`（断言与落地规则）；`audit_input`（运行审计去正文）；`refuse_unreadable_chat`（关闭状态下的失败关闭） |
| `segmentation.py` | `raw_inputs(..., chat_evidence)` 在原有归属/地理等待之后追加聊天；`inputs(..., chat_evidence)`；`evidence_rows` 对聊天行用 `chat_row`，其余行仍是原来的完整 dump |
| `planning.py` | `extract_plan(..., chat_evidence)`：计划级严格守卫、审计计数 `detail["chat_evidence"]`、无落地证据的候选被扣留（`claim_only_candidates`）、重提取遇到被扣留候选整笔保留；`apply_plan`/`rejudge_plan` 同样传参并在关闭时调用 `refuse_unreadable_chat` |
| `selection.py` | `reselect_plan(..., chat_evidence)` 同上 |
| `artifacts.py` | `validate_generated`：存在聊天行时，产物引用的证据至少要有一条能落地 |
| `orchestration.py` | 构造参数与互斥校验；`ask` 里给每次调用加 `evidence_policy` 并把审计输入去正文；向各计划传 `chat_evidence` |

## 语义与不变量

1. **时间**：三类记录 `event_start == event_end`（取消息创建时间），否则 `chat_evidence_invalid`。查询与重叠规则用 SDK 的 `TimeRange.overlaps`，与 `gps_point`、`phone_info` 等单点数据完全相同：`[start, end)` 命中 `start <= t < end`。活动时长门槛 `meets_activity_floor` 不改：按被引用证据首尾计算，一条聊天的时长为 0，需要跨度不少于门槛的多条聊天或录音。活动的 `end` 必须晚于所引用的最后一个聊天时刻（与任何单点证据一样，否则 `validate_activity` 抛 `evidence_outside_activity`，运行 `failed`）；提示里已写明。
2. **最高修订**：键为 消息 `(conversation_id, message_id)`、附件 `(…, file_id)`、会话 `conversation_id`；取 `revision` 最大者（缺省视为 1，必须是不小于 1 的整数，布尔值不算）。同修订内容相同（先比 `content_sha256`，否则比整个 payload）按最小 id 稳定去重；内容不同 → `chat_revision_conflict`，不猜。校验失败一律 `chat_evidence_invalid`，不静默跳过。假定同一对象所有修订的事件时间相同（适配器如此写）；如果某修订落在区间之外，本区间看不到它。
3. **断言而非完成**：`evidence_status` 用正向白名单：用户消息、`origin == "upload"` 的附件是 `user_provided`；助手消息、其余附件（含 `generated`、`inline`、缺少 `origin`）是 `assistant_claim_unverified`；会话是 `metadata_context_only`。程序强制的只有“落地”：活动或产物引用的证据里必须至少有一条非聊天证据或 `user_provided` 聊天，否则活动被扣留并计入 `claim_only_candidates`，产物以 `artifact_requires_non_claim_evidence` 失败。“把助手内容写成陈述而不是事实”只能靠发给模型的 `evidence_policy`，**程序不能验证模型是否遵守**。
4. **封闭投影**：聊天行为 `{id, data_type, event_start, event_end, evidence_status, payload}`，`payload` 只含会话/消息/文件 ID、角色与文本（会话只有标题）、`parent_message_id`、`revision`，以及值为 `true` 的 `text_truncated`/`unfinished`/`error`（附件另有 `text_kind`）。不含 `file_ref`、文件名、文件字节、`reasoning`、`tool_calls`、`sender`/`model`/`endpoint`/`agent_id`、标签、设备名、`dedup_key`、`source`。录音等非聊天行保持原来的完整 dump。
5. **系统提示不变**：宿主传给 `GatewayClient` 的 `SYSTEM` 没有改。聊天规则以 `evidence_policy` 字段随每次调用的 `data` 发送（含选择和归并调用）。
6. **审计**：`extraction_run.detail.chat_evidence` 只有四个计数（`messages`、`attachments`、`sessions`、`other_rows`）；`model_calls[].input` 里的聊天行只保留 `id`、`data_type`、时间和 `evidence_status`，不复制正文，也去掉重复的 `evidence_policy`。⑦ 的正文留存仍由宿主管理，部署时应保持关闭。
7. **依赖**：和录音一样，通用流程不把原始证据登记为依赖（只有 `transcript_only` 才登记）。聊天不新增依赖种类；归属/实体依赖照旧。后果：聊天记录之后被编辑不会触发重判，需要显式 `reextract`。
8. **权限**：只用传入的 Reader（①授予④的读权限）。聊天类型被拒读（`PermissionDenied`）→ `Blocked("chat_evidence_not_authorized")`，不当作空区间；`SnapshotUnavailable`（它是 `PermissionDenied` 的子类）原样抛出，按暂时性故障重试。不读文件卷、URL 或其他存储。
9. **关闭状态的失败关闭**：`apply_rule`/`extract_once`、产物重判、`rule_selection` 重选在**未开启**时，如果目标引用了本流程读不到的聊天证据，→ `chat_evidence_not_enabled`（读不到这些 ID 的授权也一样），不在看不到聊天的情况下改写笔记。检查只读取本流程没有提供的 ID，因此从未使用过聊天的数据不增加任何读取（测试断言默认关闭的提取运行与应用规则运行中 `get_many` 零调用、不查询也不读取聊天类型）。
10. **时间线/课堂/历史替代流程的分隔**：构造时与 `transcript_only`、`transcript_summary_rule_id`、`transcript_course_sessions`、`course_blocks`、`transcript_allowed_windows`、全部 `transcript_course_reextract*` 与 `transcript_automatic_timeline*` 互斥（`ValueError("chat_evidence_requires_general_flow")`）；`extract_plan`、`raw_inputs` 也各有一层 `Blocked` 守卫。这些受保护流程**读不到也不会发送聊天**（测试用拒读天花板证明它们没有查询聊天类型）。已存在的引用聊天证据的活动会被它们的证据范围检查拒绝（`course_reextract_old_evidence_out_of_scope`），不会被它们替代。没有修改它们的任何代码。

## 已知限制与未做

- 聊天没有“导入完成”标记。定时提取可能在适配器同步之前处理区间并把它记为已覆盖，之后到达的聊天不会被处理；建议只对已同步的区间做手动 `extract`，或之后显式 `reextract`。
- 附件不会因为后来的消息修订不再列出它而被剔除：记录只追加、没有墓碑，提取也不依赖适配器的扩展字段 `attachment_ids`，所以旧修订时挂上的附件仍按它自己的最高修订出现。源端删除同样看不到（适配器不同步删除）。
- ⑥ 不添加页面。现有证据框可在 `chat_evidence_read: true` 且不免登录时回读被引用的聊天证据，数据库仍复核读权限；通用记录路由不开放聊天类型，回复不含文件路径或字节。
- 过长文本不截断：单条消息超过 `max_input_chars` 时按时间边界拆分，仍放不下则 `InputBudgetExceeded`（运行失败），不发送部分正文。
- 动作规则不受影响：聊天证据下同样可以生成外部动作建议（仍须用户审核，④不执行外部动作）。助手的“已发送/已创建”类陈述同样不是完成；只靠发给模型的提示约束，没有额外的程序级检查。
- 真实 ① 的 PostgreSQL/NATS 联调、真实 LibreChat 数据、真实模型都**没有做**。

## 登记表与部署前提（不在本任务内执行）

- 公共原始记录 Schema 增加三类可选 payload 和正例。`DEFAULT_REGISTRY` 不变；已初始化的实例显式运行 `ear-storage bootstrap-chat-source`，幂等地登记 `writer=adapter` 三类。隔离 PostgreSQL 验证了登记、默认就绪和拒绝隐式扩权。
- 只对需要读取的运行身份授予类型，例如 adapter 去重、extraction 取证据，以及启用受保护证据入口时的 API 主体。严格调度配置有自己的覆盖验证，不能默认给依赖或网关扩权。
- 对当前 `transcript_only` 实例不可原地打开 `chat_evidence`；先独立验证通用提取、录音归属就绪和部署的模型审计策略。

## 测试

```bash
uv run pytest services/extraction/tests/test_chat_evidence.py tests/test_librechat_extraction_flow.py -q
uv run pytest libs tests/test_contracts.py services/extraction/tests services/api/tests -q
```

- `services/extraction/tests/test_chat_evidence.py`（假模型 + 内存 ① + 测试用读天花板）：默认关闭零聊天读取且无额外读；互斥配置与计划级守卫；受保护的仅转写服务不查询聊天；受保护替代拒绝聊天活动；纯聊天与混合（同一次运行、同样的依赖种类、同样的归属等待）；单点半开区间、活动门槛对聊天与录音一致、活动结束早于最后聊天时刻的失败；最高修订（任意写入顺序、缺省修订、同修订冲突、非法记录、非单点记录）；助手断言（白名单、扣留、产物守卫、重提取整笔保留）；拒读与快照失效的区别；关闭状态重判/应用规则的失败关闭（含授权被撤销）；真实 `GatewayClient` 加 `httpx.MockTransport` 的请求体：封闭投影、无泄漏（含金丝雀字符串）、`evidence_policy`、意图里只有指纹。
- `tests/test_librechat_extraction_flow.py`：用**真实适配器**（`Ingestor`、编辑产生 r2、带文件字节的附件）写入内存 ①，校验记录满足共享 JSON Schema，再跑提取；这是两侧约定的漂移守卫，它发现过一次把标签发给模型的问题（已修）。
- 变异检查（临时、不入库）：删除或改坏 15 处关键行为（最高修订、落地守卫、投影、白名单、互斥、授权处理、关闭状态守卫、提示、审计去正文、冲突检测、单点检查、孤立会话等），测试全部能发现；首轮有两处存活，据此补了测试。
- 另用隔离 PostgreSQL 验证三类的显式登记、读取授权和写入归属；未进行生产 NATS 或真实模型验收。

不要在回环免认证模式接入会进入派生产物的真实会话。
