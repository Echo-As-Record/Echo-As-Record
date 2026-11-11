---
audience: human
doc-type: proposal
status: proposed
generated-by: Claude (AI)，应⑤负责人要求起草
generated-at: 2026-11-10
based-on: 设计 v1.2 §4、§5、§8；信号补充 §2.2、§2.3、§3.4、§6.3；工作分解 v1.2 ⑤节与第 6 节；术语表；origin/dev 8750a83 的 contracts/registry/data-types.json（propagation_record 已登记、无 payload 格式）、contracts/events/reevaluation-requested.schema.json、contracts/records/reevaluation-result.schema.json；services/api/src/ear_api/models.py 的 reevaluate_related 草案；ear_storage service.py 的依赖查询读授权过滤与 deploy init-identities
---

# 提案：⑤依赖分析的对外接口（传播记录、重判反馈、①权限）

> **状态：proposed，未采纳。** 目标行为已由[设计 v1.2 第 8 节](../design/design-v1.2.md)和[信号补充](../design/signals-supplement.md)确定，本文只提议**格式和接口形式**，不改变已采纳语义。涉及 `contracts/` 的部分采纳后仍需①负责人与 I 共同审核，只增不改义。⑤内部的调度算法见[⑤实现设计](../../services/dependency/ai-docs/design.md)，不在本文审议。

## 1. 为什么需要

⑤要交付“传播记录与统计”和“执行‘重新判断其他相关记录’反馈”，但：

- 登记表里只有 `propagation_record` 这个类型名（system_record，writer=`dependency`），**没有 payload 格式**；⑥展示状态、论文实验统计都要读它。
- ⑥已写下按钮反馈的草案 `reevaluate_related { previous_target_id, new_target_id }`，目前没有消费方；⑥还可能做成 MCP，输入来自模型，需要能校验、能明确拒绝、能容忍重复（见第 3 节）。
- ⑤要“不漏查”受影响依赖，但①当前对⑤不可读的类型**静默过滤**依赖边，部署也还没有⑤的身份（见第 4 节）。

## 2. 传播记录 `propagation_record`

两种记录共用一个类型，用 `record_kind` 区分。都是系统记录：不发事件，⑤单写，用 revision 更新状态。字段名为提议，准确定名随契约审核。

### 2.1 `trigger`：一次被接受的输入

| 字段 | 含义 |
|---|---|
| `record_kind` | `"trigger"` |
| `source` | `data_changed` / `reevaluate_related` / `history_job` |
| `source_ref` | 触发事件 id、反馈 id 或历史任务条目 id |
| `cause` | 触发对象 `(data_type, data_id, old_version, new_version)`；按钮反馈为纠正前后对象 |
| `root_feedback_id`、`depth`、`additional_causes` | 沿用输入的因果信息 |
| `status` | `open` / `closed` |
| `summary` | 关闭时写入：节点总数，`changed` / `unchanged` / `not_applicable` / `unaffected` / `blocked` 计数，阻塞原因分布 |
| `opened_at`、`closed_at` | UTC |

### 2.2 `step`：对一个目标的一次决定

| 字段 | 含义 |
|---|---|
| `record_kind` | `"step"` |
| `target_type`、`target_id`、`target_version` | 决定时目标的当前版本 |
| `trigger_ids` | 本次决定合并的全部 `trigger` 记录 |
| `causes` | 全部起因（主起因在前），每项含版本对、root、depth；不丢任何一项 |
| `waited_on` | 决定前等待完成的上游 `step` id |
| `decision` | `requested` / `unaffected` / `blocked` |
| `request_event_id` | `decision=requested` 时的请求 id；请求与本记录同批写入 |
| `outcome` | 结果记录的 `changed` / `unchanged` / `not_applicable` / `failed`，未完成为空 |
| `block_reason` | `failed`、`timeout`、`upstream_blocked`、`owner_disabled`、`owner_unauthorized`、`not_reevaluable`、`target_superseded`、`limit_exceeded`、`correction_not_applied` 等 |
| `absorbed_causes` | 在途请求期间到达、完成后确认已被吸收的起因 |
| `decided_at`、`completed_at` | UTC |

模型调用不复制进记录：`llm_call` 已带 `request_event_id`，按它关联查询。

**请审议**：记录粒度（每节点一条 `step`）在大闭包下的数量是否可接受；是否要求为它补 JSON Schema（建议补，便于⑥和实验脚本校验）。

## 3. “重新判断其他相关记录”反馈 `reevaluate_related`

⑥可能是前端按钮，也可能是 MCP 工具（输入来自自然语言或模型）。两种入口都只向①写**结构化的 `feedback` 记录**；把自然语言拆成具体命令是⑥的事，⑤不接收自然语言。

### 3.1 两种形式都接受

```json
{"module": "dependency",
 "command": {"kind": "reevaluate_related",
             "correction_feedback_ids": ["<纠正反馈 id>", "..."]}}
```

```json
{"module": "dependency",
 "command": {"kind": "reevaluate_related",
             "previous_target_id": "<纠正前的人物/地点>",
             "new_target_id": "<纠正后的人物/地点>"}}
```

| 形式 | 适用 | ⑤怎么用 |
|---|---|---|
| A：`correction_feedback_ids`（1–100 个，推荐） | MCP；⑥按钮也建议改用 | 从每条纠正反馈本身读出被纠正数据、纠正前后对象；逐条等纠正生效后再查依赖 |
| B：`previous_target_id` + `new_target_id` | ⑥现有草案，原样兼容 | 直接以这两个对象为种子；**不等纠正生效**（没有可等的依据），只适合纠正结果已经显示后的按钮 |
| 两者都给 | 过渡期 | 以 A 为准，B 只作交叉核对，不一致整条拒绝 |

设计 §5 说“按钮写反馈，携带纠正前后对象”。形式 A 不在反馈里重复写这两个对象，而是通过纠正反馈 id 引用它们，含义不变。

**给 MCP 的约定**：模型调用纠正工具时，⑥返回那条纠正反馈的 id；模型只需把这些 id 原样传给 `reevaluate_related`，不需要、也不应该自己填人物、地点或数据的 id。一次 prompt 改了几处，可以一次传多个 id，也可以分几次调用，结果相同（见 3.3）。

### 3.2 执行与拒绝

1. root 为这条反馈自身的 id，触发深度按 0，⑤发出的请求 depth = 1；门控与限额照常生效，不因来自模型而放宽。
2. **校验**：每个 id 必须是存在、⑤可读、类型为 `feedback`、命令为纠正归属的记录。任何一项不符，**整条反馈拒绝**：写 `status=rejected` 的 `trigger` 记录（带原因码，如 `invalid_correction_ref`、`not_a_correction`、`targets_mismatch`、`too_many_refs`），同批 `MarkProcessed`，**不重试**。⑥或模型可以按原因码告诉用户为什么没执行。
3. **等纠正生效**（形式 A）：被纠正数据的当前归属由该纠正反馈写入，或已有更新的版本，才继续；超时则该条 `blocked(correction_not_applied)`，其他已生效的纠正照常继续。
4. 以全部纠正前后对象为种子，找依赖它们的过时归属（排除被纠正的数据本身）并请③重判；归属变化之后照常传给下游。纠正时伴随的参考修改本身仍按历史门控处理，只有这条反馈才放行。
5. 用户确认的归属由③保护，⑤不预先过滤。处理状态通过 `trigger` 记录查询。

### 3.3 重复请求：不去重，靠“是否过时”自然收敛

⑤**不按 id 去重**，每条反馈都正常处理。重复不会造成重复重判，因为⑤只对**当前确实过时**的依赖发请求：

| 情况 | 结果 |
|---|---|
| 同一次纠正被请求两次，第一次已完成 | 第一次重判后依赖已刷新、不再过时；第二次查不到过时依赖，`trigger` 以 0 个请求关闭 |
| 两次请求同时在途 | 共用同一张工作图：同一目标的起因合并；目标已在途时记为后续轮次，完成后不再过时就记为“被吸收” |
| 同一数据被连续纠正（甲→乙，再乙→丙），各自请求 | 各自的种子都参与；已生效的检查接受“更新的版本”，所以针对旧纠正的请求不会卡住 |
| 一次 prompt 改了几处，一次传多个 id 或分几次调用 | 种子合并或工作图合并，结果相同 |
| ⑥写同一条反馈时网络重试 | ①按请求 id 重放、⑤按事件 `MarkProcessed`，只处理一次 |

所以不需要规定模型“只能请求一次”。真正需要防的是滥用（反复触发大范围重判），由已有的闭包规模、深度和轮次限额兜底，超限记录并提示。

**已由⑤负责人决定（2026-11-11）**：接受形式 B 作为过渡，并建议⑥改用形式 A；`correction_feedback_ids` 上限为 100。这两项是⑤负责人对本模块入口的决定，不代表全文已采纳；涉及⑥的部分仍需⑥确认。

## 4. 需要①提供的权限与状态

| 编号 | 提议 | 理由 |
|---|---|---|
| A1 | 部署签发 `dependency` 身份：读全部核心类型、`feedback`、各结果类型、`llm_call` 与 `propagation_record`；订阅可订阅的核心类型与 `feedback` | 当前 `init-identities` 只签发管理员与管理 worker |
| A2 | 插件类型注册生效时，⑤自动获得该类型的读与订阅，无需管理员逐个授权；若不采纳，依赖查询在隐去边时必须明确报出，不能静默返回部分结果 | 设计要求①⑤动态发现新插件；静默过滤会让⑤漏查插件下游而不自知 |
| A3 | ⑤可读各所属模块的生命周期状态（启用 / 停用 / 撤权），只读、不含凭据 | 让停用、撤权明确表现为“受阻”，而不是只能等超时 |
| A4 | ③④接入时，归属、产物、`rule_selection` 登记 `supports_reevaluation=true` 及结果类型 | ①只把请求投给支持重判的类型；能力项现在全为 false |

A2 让⑤能读全部业务类型。这与[实施计划](../../ai-docs/plans/mvp-implementation.md)第 4 节“不能因⑤能看全图就把全图发给插件”不冲突：⑤只把目标和起因的类型、id、版本放进请求，不把其他类型的内容转给插件。

## 5. 影响与验收

- **⑤**：按本文写传播记录并消费按钮反馈；实现与测试见[⑤实现设计](../../services/dependency/ai-docs/design.md)。
- **⑥**：现有按钮草案（形式 B）原样可用；MCP 和以后的按钮建议用形式 A，并在纠正工具的返回里带出纠正反馈 id；状态展示读 `trigger` 记录（⑥本轮暂缓，不影响⑤先行实现）。
- **①**：A1–A3 的权限与状态接口；若补 `propagation_record` 的 Schema，与 I 共同审核。
- **③④**：A4 的能力登记，随各自接入完成协议时一起做。
- 验收：按 root 能导出完整传播并统计；形式 A 在纠正生效前不会提前查依赖；无效引用被明确拒绝且不重投；重复请求不产生重复重判；⑤对新注册插件类型的下游不漏查（A2 的测试）。

## 6. 待决

1. 传播记录字段与粒度（第 2 节）；是否补 Schema。
2. ~~形式 B 作为过渡是否接受；`correction_feedback_ids` 的上限~~：已定，见 3.3 末尾。外部 Agent（MCP）写这条反馈前是否需要用户确认，由⑥与产品负责人决定，不在⑤。
3. A2 选自动授权还是“隐去即报错”。
4. 超时后⑤是否自动重发请求。当前方案不自动重发，只标受阻并继续等迟到结果；重试由新输入或运维操作触发。
