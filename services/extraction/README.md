---
audience: human
doc-type: guide
based-on: 设计 v1.2；c5c31f8 的④实现；本轮负责人授权④直接依赖⑦ GatewayClient；79e8dc3 中的④首片（orchestration.py、call_safety.py、domain.py、model_io.py、planning.py、worker.py、demo.py 与相应测试，作者读 git diff 与源码核对）；任务状态文件 /tmp/ear-mvp-extraction-followup.json 与 /tmp/ear-mvp-extraction-state.json（非仓库文件，其中的测试作者未复跑，只用 pytest --collect-only 核对项数）；2026-11-12 增补依据：工作区未提交的 services/extraction 源码（selection.py、reevaluation.py、artifacts.py、orchestration.py、planning.py、segmentation.py、suggestions.py、domain.py、demo.py、model_io.py，作者读源码与 git diff，不运行测试）、仓库外摘要 /tmp/ear-mvp-extraction-v12.json 与 /tmp/ear-mvp-action-protocol.json（作者读取，其中的测试结果作者未复跑，只用 pytest --collect-only 与文件哈希核对）、services/storage 的 core_deployment.py（只读核对）；代码基线 integration/mvp-runnable = 79e8dc3；用户在本任务中的授权仅为开始 MVP 实施、可用 vm-octopus 音频、在常驻分支实现提交并提 PR 由用户审核，本文不新增授权记录；未填写或改动任何 reviewed 字段；2026-11-18恢复增补：697de3b与7f98132合并候选、PR23只读审查及本轮限定文档授权（新说明非功能验收）
---

# ④ 活动提取

## 2026-11-18 合并候选说明

本页下文79e8dc3/2026-11-12的“第二批工作区未提交”与验证限制保留为历史；第二批v1.2/Finish/action-v2已在当前HEAD `697de3b` 历史中，本次正在合并 `7f98132`，尚无本次统一验收结论。合并保留这些集成功能，并保留`gps_geocode`等待、地名/楼宇作为原始证据的上游说明，不把它改成③推断地点。

当前core profile的extraction读授权仍未覆盖`gps_geocode`，缺权不能伪装无GPS/无结果；需主 agent与①/I显式处理和验证，不在文档任务中补grant或绕过等待。最小真实样本候选默认不启④/⑦，查看已有记录不触发提取；见[恢复交接](../../ai-docs/plans/real-sample-web-mcp.md)。⑥的[只读MCP设计](../../docs/proposals/mcp-interface.md)仍proposed，不授权模型调用或外部动作。

负责活动、规则、Markdown 产物及外部动作建议。**79e8dc3 含调用安全与重判完成首片；v1.2 对齐（纯 Markdown、`rule_selection`、按当前规则重判与完整 Finish）以及外部动作 action-v2 的 ④ 侧状态流转是第二批，只在工作区：未提交、未提 PR、未部署。仍不是完整的 ④，也不是 MVP 完成。**

- [目标设计](../../docs/design/design-v1.2.md)、[模块间信号](../../docs/design/signals-supplement.md)。
- [使用与接入](ai-docs/implementation.md)：演示命令、宿主注入示例、契约差距、首片的代码导航与限制（该文写于 v1.2 对齐之前，v1.2 部分以本文和源码为准，差距见[跑通交接第 8 节](../../ai-docs/plans/mvp-runtime-handoff.md)）。
- [网页演示](ai-docs/web-demo.md)：合成数据和固定模型回复。
- [MVP 跑通计划第 2 节](../../ai-docs/plans/mvp-runnable.md)与[MVP 运行时交接](../../ai-docs/plans/mvp-runtime-handoff.md)：工作区进展、验证来源与待补项。
- 本轮提案（均为 proposed，未采纳，2026-11-25）：不依赖③、可先做的[转写纠错派生层](../../docs/proposals/transcript-correction.md)（含 C07 修订建议）、[结果确定性审核与独立复核](../../docs/proposals/extraction-result-audit.md)、⑦的 [Codex 本地 fallback](../../docs/proposals/llm-gateway-codex-provider.md)；要等③匹配校准和 `person.is_self` 的[活动重要性评估骨架](../../docs/proposals/activity-assessment.md)。词表沿用[用户词表提案](../../docs/proposals/asr-user-glossary.md)。

模型端口保留 `ModelGateway`，实际 HTTP 客户端统一使用 workspace 包 `ear-llm-gateway` 的 `GatewayClient`，构造时传④的 `SYSTEM`。客户端仍由⑦维护，协议见[⑦入口](../llm_gateway/README.md)。演示仍用测试替身；远程宿主见[交接第 4 节](../../ai-docs/plans/mvp-runtime-handoff.md)的统一 worker。

## 离线时间线边界预览（实验）

此工具只生成可核对的**候选**，不生成业务活动、Markdown 或动作，也不触发定时、提取、重提取或归属。其目的是检验相邻录音/处理窗是否同一场，而不是让计算窗口决定场次。默认提取及现有服务接口保持不变。具体输入、输出和回归见[离线边界预览说明](ai-docs/timeline-preview.md)。

[离线重提取元数据预检](ai-docs/reextract-preflight.md)可单独复核完整采集范围、隔离区间、旧版本与应保留对象；不进行模型调用、写入或重提取。

## 自动划分的活动时长门槛

私有 extraction worker JSON 可设 `"activity_min_duration_minutes": 10`。省略时为 **10**；只接受 `1`–`1440` 的 JSON 整数，不接受 `0`、字符串、小数或布尔值。它不是反馈字段，也不会启用定时器。

- 适用于固定窗自动时间线、普通 `extract`（含定时和仅转写）及自动课堂划分。即使由用户提交 `extract`，只要是模型新划分就适用。先完成原有证据校验及接续，再用活动时段内被该活动引用的原始证据首尾计算时长；不是按查询窗长度或四舍五入。门槛不改已判定的时界，也不新增连接、跨缺口或补问。
- 只有短片段时，固定窗自动时间线以 `automatic_timeline_no_supported_activity` 阻塞，不写业务对象；课堂划分同样不提交。普通 `extract` 延用既有成功空结果语义，记 `no_supported_activity`，已处理的定时范围不反复扫描。时长过滤计数只写入运行审计；原始记录和失败／未知调用保护不变。
- 有旧目标的普通 `reextract` 仍要求精确预览；只要新结果为空，或任何候选因时长被过滤，整笔以 `reextract_incomplete_activity_replacement` 阻塞，不退休旧对象。固定窗自动替代先过滤，再核证据后继和不确定范围；有被滤掉的候选时还要求保留每条旧活动和有效产物的引文。任何校验失败都保留整批旧对象。人工复核稿、显式手工操作和一次性草稿哈希／精确重放不应用时长门槛。

## 部署可选：仅转写（`transcript_only`）

通过 `mvp_worker.py` 部署且未开启下述课堂模式时，仅允许手动 `extract` 匹配 `transcript_windows` 中的完整 UTC 窗口（最多 24 个，每个不超过 15 分钟，不能重叠）；必须指定 `transcript_summary_rule_id`，不能同时启用定时。超出窗口时在模型调用前阻止。worker 为此模式向①请求 `transcript_extraction` 快照：只省略依赖图投影，快照授权、限额和提交时的依赖校验不变；普通提取仍使用默认快照。该配置每任务最多 5 次模型调用。租约取 1,900 秒和「调用上限 × 向上取整的生效 `gateway_timeout`（未配置时用 `timeout`）+ 250 秒」两者中的较大值，给存储及本地处理留出余量；租约不是请求超时或任务硬时限。默认配置和 `gateway_timeout: 330` 仍使用 1,900 秒。租约超过 1,900 秒时，`health_max_age_seconds` 必须至少覆盖租约（上限仍为 3,600 秒）；例如 `gateway_timeout: 630` 时租约为 3,400 秒，可配置 `health_max_age_seconds: 3500`。普通提取 worker 的 900 秒租约不变。

宿主可显式传 `ExtractionService(storage, gateway, transcript_only=True)`；省略此参数或传 `False` 时，现有输入及归属、逆地理编码就绪门槛保持不变。此选项不是反馈参数、权限配置或历史迁移；历史成功区间仍按原逻辑去重，要重新处理请显式预览并提交重提取。

- 只经①的现有授权读取选定区间内的全部 `transcript_segment` 原始记录，不读取音频、声纹（含向量）、环境音、GPS/地址、手机信息或人物/地点/归属；也不检查这些未使用数据的就绪状态。转写不存在、不合法或无读取权限时不回落到其他证据，也不将其视为成功的空区间。选规则与生成使用和该活动重叠的全部转写，不仅限于划分时已引用的那几条。宿主应选择已完成导入的区间；④不另读采集清单来判断整个采集是否导入完成。
- 发给⑦的证据只有原始逐字稿、原事件起止时间和需要引用时的原始转写记录 ID；不附带音频 ID、文件路径或字节、采集/说话人 ID、人物/地点字段等元数据。正常的调用追溯信封保留；选规则只带可选规则的 ID 与适用范围，生成只带选中规则的指令。逐字稿和规则中本来就有的文字不会被改写或去标识。
- 活动和产物的证据链接指向原始转写记录，沿用⑥的记录查询接口；依赖通过①登记真实记录版本，不复制原始记录、不构造转写时间。④运行审计保留持久调用意图和摘要指纹，不存模型请求/回复正文；业务产物仍按规则写入 Markdown。⑦的正文留存另由宿主管理，部署时应保持关闭。
- 支持定时/手动提取和带有效预览的重提取。动作规则不参与选择；不生成动作建议。历史应用规则、单次规则、两类重判、动作审核与结果反馈显式失败，不向模型发送既有内容。重判按原完成协议记录失败。
- 提取和重提取在同一个存储快照内预读全部转写、规则候选及规则类型修订；重提取也预读预览所需的版本。首个模型调用前释放快照，不在模型返回后再读它；提交仍检查候选集合修订和依赖版本，不能用过时结果覆盖并发更改。
- `transcript_summary_rule_id` 是仅转写模式的可选宿主配置。设为规则 UUIDv7 时，只按该 ID 的当前启用版本为每个新活动生成产物，规则必须不限定既有活动且不带动作。无效或不可用时不发送模型请求，也不选用其他规则；有效时跳过模型选规则，只发送该规则的生成指令，仍保存 `rule_selection` 和所用规则版本的依赖。不设置此项仍按原逻辑自动选择。
- 长输入沿用不切开转写的划分和生成归并；不截断正文、也不发送部分规则选择。如果无法拆分，或单个活动的完整选择输入超限，任务失败，不提交部分结果。`transcript_max_calls` 在仅转写模式下默认每次运行最多 5 次模型调用（划分、合并、选择、生成及归并合计）；第 6 次不会登记或发送，运行以 `transcript_max_calls_exceeded` 失败，不提交部分业务结果或自动重发。宿主应按调用限额、单次超时和额外处理时间配置运行租约；默认多模态模式不受此次调用上限限制。

## 宿主可选：跨窗课堂提取（`transcript_course_sessions`）

遵循[设计 §7.1](../../docs/design/design-v1.2.md#71-触发与划分mvp)。只接受精确匹配按时间排序的允许窗连续子序列外边界的手动 `extract`；不自动处理历史或启用定时，默认不支持重提取（见下方精确范围例外）。旧活动重叠或重叠运行的结果未核清时阻止新模型调用。

| 私有 JSON 键 | 启用课堂模式的设置 |
|---|---|
| `module` / `consumer` | `"extraction"` / `"extraction.jobs"` |
| `transcript_only` / `transcript_course_sessions` | 均为 JSON 布尔值 `true` |
| `transcript_summary_rule_id` | 既有全局、启用且无动作的规则 UUIDv7；每个最终活动只用这条规则生成一份中文 Markdown，按内容分节 |
| `transcript_windows` | 保留既有允许列表中原始 UTC 起止时间，按时间升序排列；1–24 窗，每窗最多 15 分钟，互不重叠 |
| `schedule` | 省略或 `null` |
| `timeout` / `gateway_timeout` / `health_max_age_seconds` | 按选窗数量与生效调用预算配置存储／网关超时及健康门槛；沿用既有宿主连接键和凭据管理 |

- 先按原录音覆盖分块（相邻录音空档大于 10 分钟另起区块），不按识别分段、话题或录音文件分课。每个区块一次 `course_blocks` 调用，按时间读取各窗的有界转写摘录，统一提议活动范围。程序用可见引文核查首末行、顺序、覆盖和同类课堂的切分，展开成真实证据关联。分块只是组织输入，不要求每块一个活动。
- 按已校验的最终活动生成 Markdown，使用范围内完整的归属转写，不只用划分摘录；未分类行仍在原记录中，不单独生成活动。缺少任何选定窗的可用转写或整体无法分类，不提交业务结果。
- 仍用固定无动作规则、授权和持久调用意图；每选中窗五次总调用预算，24 窗最多 120 次，跨窗生成可按预算拆分归并。动态租约按实际窗数和单次网关/Storage 往返上限计算，健康探针按成功 Storage 交换刷新。未知发送或写入不自动重发。不开该宿主开关时，默认逻辑和逐窗五次调用上限不变。不新增 API 参数、模型权限或外部动作。

### 私有可选：按批准的精确范围重提取（`transcript_course_reextract`）

只用于单独获批的精确范围，不开放任意历史重跑。提交范围必须匹配宿主配置的完整 UTC 允许窗清单；不得选择子集、改变边界、把清单换成一个大窗或启用定时。只提交现有 `reextract` 命令，区间取首窗 `start` 到末窗 `end`，`preview` 取现有 `/v1/previews/reextract` 响应中的完整 `versions` 映射。预览本身仍是只读，不代表批准；业务替代成功后，同一整段的再次提交会被阻止。用途结束后取消启用并清除临时配置。本文不记录任何批次的授权或输入。

| 私有 JSON 键 | 精确范围配置 |
|---|---|
| `transcript_course_reextract` | JSON 布尔值 `true`；默认 `false`，要求已启用仅转写课堂分块模式 |
| `transcript_course_reextract_draft_sha256` | 启用时必填：已核验划分的 64 位小写十六进制 SHA-256 |
| `transcript_course_reextract_draft_spans` | 经核验的活动有序数组，1–48 项；每项 `start`、`end`（UTC `Z`）、`first`、`last`（整批从 1 编号，含两端）、`title_md`、`description_md`；`kind` 可选（`course`，默认；或 `other`，仅用于审计计数）。不填写证据 UUID 列表 |
| `transcript_course_reextract_reconciled_run_ids` | 默认 `[]`；只填独立核验的、仍触发重复保护的失败/阻塞前序 `extraction_run` UUIDv7（不是反馈 ID 或 call ID），最多 64 个，不能重复 |
| `transcript_course_reextract_schema_discard_attestations` | 默认 `[]`；最多 4 项、`run_id` 和 `call_id` 各自不能重复。每项只含 `run_id`（须在前序名单中）、`call_id`、`request_fingerprint`（持久调用意图中的 64 位小写十六进制值）、`proof: "schema_response_received"`、`stage: "extraction_schema"`。有证明时必填 `transcript_course_reextract_draft_spans` |

- **复用已核验划分**：沿用宿主现有的私有配置文件（运行用户所有、`0600`，原有 64 KiB 上限不变）。对全部允许窗中的唯一转写行按 `(event_start, event_end, id)` 升序编号；复用选窗编译器的归属规则，跨相接窗的行仅计一次。活动必须按顺序无遗漏、无重叠地分配每一行，各自限于同一录音区块，起止必须与首末证据原时间完全一致，且不能有更早开始但更晚结束的行伸出活动。某窗无转写、存在空白行、或不能重建原指纹就停止，不回退到模型划分。对已核验的 `Activity` 列表用 `ear_extraction.course_reextract.course_draft_sha256` 取得原指纹：它只覆盖按顺序的 `start`、`end`（`ear_contracts.format_utc`）和完整的有序 `evidence_ids`，使用 `json.dumps(draft, sort_keys=True, separators=(",", ":")).encode("utf-8")`；不覆盖标题或描述。已核验的标题和描述仅在本地写入活动，不发送给模型；仅用固定规则新生成笔记。不要保存完整日志、请求或回复作为配置。没有任何证明的其他获批批次，如不配置活动计划，则保留原来的重新划分并比对指纹流程。
- **替代范围**：旧活动两端必须落在同一录音区块的允许窗内；可跨不超过 10 分钟的短缝，但只落在缝里或跨录音区块的活动会阻塞整批。活动和有效产物引用的证据必须来自选窗且与各自时间相交。手工/已编辑对象，或关联到有效/状态不明的外部动作建议（含待审和已批准）阻塞整批，不会自动跳过；请人工复核并重新确认范围。已执行或已拒绝的建议原样保留，不修改日历、不执行动作，也不创建建议。
- **前序不重试**：ID 不是通行证。前序须为同一整段或完全落在选定录音区块内的手动提取/重提取。可核实的本地生成/校验失败须具备全部 `returned` 意图；末次意图仍为 `sending` 时，默认须由既有⑦认证审计端点确认同一 call ID 的已保存、不可恢复终态为 `success` 或明确 `provider_rejected`。唯一例外是宿主独立且唯一地逐调用关联到 `newapi_call_failed stage=extraction_schema`，确认收到回复但因本地 schema 校验丢弃、未写业务结果，并显式为该持久调用意图配置证明。仅允许早于本次新运行、恰好匹配一个允许窗的手动 `extract`，记录为 `blocked` / `gateway_outcome_unknown`，只有一次 `sending` 的 `segmentation` 意图，且上述⑦端点确认同 ID 已保存、不可恢复的 `failed` / `provider_outcome_unknown`。每一条证明都必须匹配并实际使用，不能以相近时间、同一批次或笼统的未知结果代替。没有明确证明、审计缺失、仍在执行、未追踪发送、可能已有业务写入或无法确认名单项均不放行。不会改写前序任务、重新发送旧请求或恢复回复正文。
- **写入与留存**：此路径不打开 Storage 广域快照；逐窗读原始转写，提交前按需逐窗核对记录数，再用转写类型修订守护提交。依赖和固定规则仍有原有提交守护。④单写者在写入前重核旧活动、产物、动作保护及 `rule_selection` 版本；旧对象仅追加 `superseded` 版，并与新活动/产物/选择、依赖及运行成功记录原子提交。运行只保留调用意图、指纹与计数；⑦用默认无正文设置，①保持 `EAR_MODEL_BODY_ENABLED=false`，不保存请求或回复正文。其他提取模式及既有公开接口不变。

### 私有交接：复用完整复核的稀疏时间线

经明确批准的本机替代可改用 `transcript_course_reextract_timeline`。沿用上节的课堂模式、固定无动作规则、原允许窗清单、手动全范围 `reextract` 和公开 `preview.versions`；另要求原允许窗彼此相接，且可信预检确认整段是一个无保留对象、无隔离的连续采集区间。如果只有拆分或改动原允许窗才能排除隔离，**本模式不执行**。

- 仍设 `transcript_course_reextract: true`。私有文件中的时间线对象来自[离线交接](ai-docs/timeline-preview.md#稀疏重提取的私有交接)，包含 `review_complete`、`input_sha256`、`draft_sha256`、`spans`（含本地已审核标题和描述）和 `old_pins`（含 `rule_selection`）。不同时配置旧草稿哈希、旧草稿 spans 或 schema 丢弃证明；不解除仍标为发送中的前序。只按现有独立可核实的明确写入前失败逻辑处理列明的前序。
- 只按已选候选生成笔记，不重做划分；未分类或未选行保留为原始记录，不写活动、不发送给生成。全量输入指纹仍覆盖这些行。旧的每个活动都须与至少一个新活动在时间与原始证据上有交集；任何一项不成立，整批不替代。
- 写入前重核完整私有版本钉与原公开版本映射；新旧对象和完成记录仍原子提交。私有交接不是反馈字段、API 或自动部署，不能替代隔离与保护检查。

## 宿主可选：有界自动时间线划分（`transcript_automatic_timeline`）

“自动”只指判断边界，不负责搜索未处理历史、排程或提交反馈。默认不开启；宿主为一个经独立预检且明确允许的完整采集组件配置私有 `transcript_automatic_timeline`，使用现有仅转写课堂宿主、固定无动作规则和原允许窗。配置不替代核对真实采集、隔离及旧对象，不能通过裁剪采集或移除隔离来放行。

- 旧目标为空：只接受完全匹配该组件的手动 `extract`；发现有效活动或范围内未预期的产物／选规则结果，发送前拒绝。
- 存在旧目标：只接受完全匹配该组件的手动 `reextract`，仍需公开的精确 `preview.versions`，并另核活动、产物和选规则版本。没有证据与时间重叠的可靠后继，或旧活动触及边界／参与不明的候选或不可见覆盖时，整批保留。不会改动任何动作；未知运行不会放行。与精确范围课堂替代或私有人工复核交接不能同时开启。
- 可由宿主为精确的新范围逐一钉住相交且确定在业务写入前失败的旧任务，再以新的手动反馈处理。须独立证明整个旧任务未产生任何业务写入（含历史及终止版本），且每次调用都已返回、有逐条匹配的已保存成功审计；原任务范围较大也须整笔核实。未决、未知发送或写入、有效结果未提交、已有成功的整家族重提取均不因该证明放行，不重发或改写旧调用。
  - 空目标仍要求范围内没有现行或已终止的家族对象。非空目标（含跨已知缺口）仅用此类失败证明处理老的手动 `extract`，不用于老的 `reextract`、定时或自动时间线任务。仍须完全匹配现行家族的私有版本钉及公开 `preview.versions`；只有新活动保留旧活动和有效产物的全部引文才允许整家族替代。
- 已明确核实的 v1 自动划分校验失败可由私有前序证明在同一完整范围开始一次 v2 试验，默认关闭；不重放旧请求，未知或可能写入的任务不适用。精确配置和核对步骤见下方说明。
- 边界由完整、逐窗验证的时间线预览证据决定。只为有证据支持的主动参与、观看／收听生成活动和固定规则笔记；背景播放、未确认、冲突、时间重叠及未归类内容不会硬填进活动。活动起止来自原转写。没有任何可落库活动时明确阻塞，不把该范围记为成功。
- 托管 v2 仅在收到 Gateway 确认已保存、与当前调用匹配的回复后，可丢弃形状合法但无法核实的换场提示和接续证明；这不证明换场或接续。与重叠视图的边界主张冲突时保留不确定性。参与角色引文无效或缺失时只降为 `unknown`，不写该片段。原始 `spans` 的形状、范围和顺序仍严格校验。降级触及旧活动时间，或新活动未保留旧活动与有效产物的任一引文时，整个旧家族不替代。不为降级补问或重试，离线人工复核不降级。

- 自动审阅用整点对齐的 **10 分钟核心区**，前后各最多 **2 分钟上下文**。按转写开始时间归入唯一核心区，不跨真实覆盖缺口，不需要把活动或时间线填满。审核窗不是活动边界。任一视图超过 512 行或 96,000 UTF-8 字节即在调用前拒绝整份计划（`automatic_timeline_window_budget_exceeded`）；不截掉转写行，也不回退到按行数切窗。
- 私有 worker JSON 的 `transcript_window_review_concurrency` 仅控制本模式的窗口审阅：严格整数 `1`–`8`，默认 `3`，设 `1` 完全串行；活动组装、笔记生成与归并仍按原顺序。发送前逐笔持久化调用意图。任一调用结果未知时不再派发新请求，只收尾已派发的调用，整笔不写业务结果，不自动重发或换 ID。⑦ 默认全局并发为 4，仍对共享流量限流；不要把④上限当作⑦保证可用的额度。

[宿主配置、审计与隔离测试](ai-docs/automatic-timeline.md)。本路径沿用现有 Gateway 和 Storage，不增加权限、连接或反馈字段。

### 私有可选：跨已知缺口替代旧活动家族

默认仍要求连续采集。`transcript_automatic_timeline_gaps` 非空时只接受一笔有完整旧对象版本预览的手动 `reextract`，且至少一个现行旧家族跨已声明的数据缺口。许可范围必须恰好覆盖全部选定采集组件及其真实间隙；读取和模型审阅只按采集组件进行，不读取缺口或把两侧拼成同一视图。采集保留原始 ID、边界和转写归属；计算读窗可在相接的覆盖内打包，仍限 24 窗、每窗最多 15 分钟。

配置中任何 hold，以及与整段范围相交的任何未核清／留置前序，均阻止处理（即使只相交于缺口）；只接受上文定义的显式写入前失败证明，不接受旧成功或 v1 划分试验的证明。缺口不证明活动开始、结束或延续。若每个旧活动及每份有效旧产物的全部转写引文都被至少一个有证据的后继活动保留，可以用多个独立片段替代旧家族；任一引文丢失、旧范围内出现未分类或不确定证据、版本变动或发现保护对象，保留全部旧对象。本模式不创建、修改或终止任何动作。见[宿主验收条件](ai-docs/automatic-timeline.md#跨缺口替代旧活动家族可选)。

### 私有可选：连续采集中的已核实 ASR 缺口

`transcript_automatic_timeline_asr_gaps` 默认为空。非空时仅支持精确匹配整段允许窗、无旧目标的全新手动 `extract`；不允许定时、前序证明、物理缺口覆盖或替代旧活动。原始采集必须是真实、完整、相接的 `audio_ref`，不得把缺字的时段从采集边界裁掉。只声明独立核实的准确 ASR 缺失时段；不能用转写稀疏、居间静默或旧任务发送结果不明来推测缺口或放行。

仍按原允许窗查询转写并核对每行的原采集标识及完整时界。如果任何转写行与声明的缺口相交，整笔在发送前阻止；不剪行、不删记录。审阅时从采集覆盖减去已声明的 ASR 缺口，仍用 10 分钟核心区和前后最多 2 分钟上下文，但划分与笔记都不跨缺口。只记录由原时界支持的片段；缺口不证明活动开始、结束、静默或延续。整段仍受旧任务、已有对象和提交守护约束；成功只表示已处理可用证据，不表示整段转写完整。见[私有配置与验证](ai-docs/automatic-timeline.md#连续采集中的已核实-asr-缺口可选)。

## 宿主可选：聊天证据（`chat_evidence`）

默认关闭。宿主显式构造 `ExtractionService(..., chat_evidence=True)` 时，[LibreChat 源适配器](../adapters/ai-docs/librechat-source.md)写入的 `chat_session`、`chat_message`、`chat_attachment` 才与录音一起进入**原有的通用提取流程**：同一个普通 `extract`／`reextract`／定时入口、同一个①快照、同一个⑦网关、同一套调用安全与原子提交。不开启时，读取、发送给模型的内容和存储访问都与以前相同。`mvp_worker.py` 支持同名宿主 JSON 键。

- **纯聊天与混合**：区间里只有聊天，或聊天与录音、GPS 同在，都是同一次处理、同一套活动划分和规则选择。录音仍按原规则等待归属与逆地理编码，聊天没有就绪标记，不会也不能绕过它们。
- **计时**：聊天是单点事件（开始等于结束，取消息创建时间），和 GPS 点一样按半开区间 `[start, end)` 命中。活动时长门槛对聊天与录音相同：按被引用证据的首尾计算，一条聊天没有时长。活动的结束必须晚于它引用的最后一个聊天时刻。
- **最高修订**：同一条消息、同一个附件、同一个会话各取 `revision` 最大的一条；旧修订留在①，但不发送也不引用。修订相同而内容不同时以 `chat_revision_conflict` 阻塞，不猜。聊天被编辑后，已有活动不会自动重算，需要显式 `reextract`。
- **助手内容是陈述，不是完成**：助手消息和助手生成的附件按“助手所称”处理，会话标题只作上下文。只靠这些内容支撑的活动被扣留（运行审计里记数），只靠它们支撑的笔记被拒绝；整体替代时只要有被扣留的候选就保留全部旧对象。要求模型把它们写成陈述，而不是已完成的事实或用户行为——这一点程序无法核实，程序能强制的只是“至少有一条用户或非聊天证据”。
- **发给模型的内容**：只有文本、角色、各 ID、修订号和截断／未完成／出错标记；不含文件路径、文件字节、文件名、工具调用、推理、模型名或设备名。运行审计不保存聊天正文。
- **访问**：只用①授予④的读权限；缺少读授权以 `chat_evidence_not_authorized` 阻塞，不当作空区间。不读取文件卷、URL 或其他存储，不新增权限、模型调用方或连接。
- **与仅转写流程分离**：本开关与 `transcript_only`、课堂分块、自动时间线及其历史替代互斥（构造即报错）；那些受保护流程读不到也不会发送聊天，也不能替代引用了聊天的活动。未开启本开关的服务不会重判或改写引用聊天的旧产物，而是阻塞为 `chat_evidence_not_enabled`。

工程细节、部署前提（登记表、读授权顺序）和限制见[聊天证据接入说明](ai-docs/chat-evidence.md)。

## 隔离试跑：混合文本（`mixed_text`）

默认关闭。仅供隔离环境用**一条已提交的用户反馈**做新的 `extract`：`chat_evidence: true`，另设私有 `mixed_text` 的 `feedback_id` 和与反馈完全一致的 UTC `interval`（最长 24 小时）。不复用线上仅转写 worker，不接受重提取、其他作业或定时。旧活动、重叠成功范围或未核清的重叠运行会阻塞，不退休、不补写旧对象。

- 同一次通用划分、选规则和 Markdown 生成，仅输入转写文本与三类聊天证据；不读声纹、归属、GPS 或逆地理编码。通用多模态入口仍读取原有证据并保留原有就绪门槛，课堂和自动时间线也不变。本试跑不选动作规则，也不创建建议。
- 仍通过原 Storage SDK 和 Gateway。计划预读四类原始数据、适用规则和修订后释放快照；按证据时长门槛和聊天落地规则筛选，空结果明确阻塞。运行审计只保留计数、调用指纹和状态；活动和笔记仍是业务对象。原子提交重核原始类型和规则修订，复用持久调用意图：未知发送或写入绝不重发。
- 快照复用 `transcript_extraction`，只省略图，不缩小权限域或快照扫描范围。大历史仍可能触发快照限额；这个试跑不是新快照协议、整日处理保证或同步就绪的替代品。

见[私有配置、启动顺序与停机条件](ai-docs/chat-evidence.md#混合文本隔离试跑mixed_text)。

## 状态

**c5c31f8 基线（已合入 dev）**：`GatewayClient.complete(Call)` 每次都生成新的 call_id。传输错误 `gateway_outcome_unknown` 可能已经执行，不能自动换新 id 重试；可恢复流程须发送前持久化 call_id 并用 `reconcile` 核对。基线上④的任务自动重试、存储冲突重算与租约恢复没有适配此语义，重判也缺请求关联字段，且 `extraction_run` 占用了请求的 event_id，使 ① 的 Finish 无法写入。

**79e8dc3（首批，已在分支历史中；不是完整 v1.2）**：

- **调用安全**：每次发送模型调用前，先把稳定的 `call_id` 和规范化请求的 SHA-256 以版本 CAS 写进 `extraction_run`；可能已经执行的调用（超时、断连、非 200、发送后崩溃、租约过期）一律停止为“结果未知”，**不自动换新 id、不重发**，`reconcile` 只补审计记录，不取回回复，也不解除阻塞。
- **重判与完成**：重判运行的 run id 与请求 event_id 解耦，请求的 root、depth、`request_event_id` 和全部额外起因原样传给⑦与存储写入；对 artifact 重判，业务写入、完整依赖、`FinishReevaluation`、`extraction.completion` 标记和 run 成功在同一批提交，无变化也提交完成结果但不新增 artifact 版本。基线上占用了请求 ID 的旧记录不改写、不迁移，未完成的会被阻塞。
- **远程订阅下的 worker**：`tick` 在客户端支持 `poll_once` 时先拉取一条 `extraction.jobs` 投递。

**第二批：v1.2 对齐（工作区，未提交）**：

- **纯 Markdown**：新生成的产物与动作文本是直接写名字的纯 Markdown；新输出里出现 `[[person:…]]`、`[[place:…]]` 引用标记会被拒绝（`legacy_entity_marker_in_new_output`）。旧文本不改写，也不反向解析成依赖。
- **活动划分只用原始数据**：划分与合并只读原始数据；归属就绪检查与实体解析分开，分段之后才组装人物、地点。实体输入只投影 `payload.name`（保留缺失与 null 的区别），登记名字字段依赖（含首次命名）；每个区间归属仍是粗粒度依赖，另加活动与当前规则。手动与自动产物都登记完整依赖集。
- **`rule_selection`**：每个活动一份版本化的选规则结果（核心类型，含空候选、空选择）。它的依赖覆盖活动、评估过的当前规则候选、所有区间归属和投影的实体字段；候选集合以规则类型修订在同一批里守护。**产物不依赖选择结果。**
- **按当前规则全集重选**：新选中的规则生成自动产物与建议；退出的规则使自动产物和仅 pending 的建议被替代；仍选中的不重新生成。`manual` 与 `source=manual` 的产物受保护，不被自动取消选择。
- **按当前规则重判**：产物重判读同一规则 id 的**当前**版本，规则出新版本时不再阻塞（旧代码 `historical_rule_dependency_unsupported` 已不在源码中）；规则版本变化时必须有完整的生成结果，字节相同的文本也可能新增产物版本。规则被删除或替代时，产物与仅 pending 的建议被终止，并以 `not_applicable` 完成；已审核的建议不动。规则编辑本身不扫历史。
- **起因与 Finish**：从规则历史发现的额外起因，连同所有贡献它们的 root 与 depth，写入 `execution_additional_causes`，原始请求起因保留；执行上下文在发送模型前持久化，并传给⑦、业务写入与 Finish。changed、unchanged、not_applicable、failed 四种结果都有真实的 `FinishReevaluation`；成功的活动目标带完整依赖，完成标记与 run 更新原子提交。已知非法输出和模型前的重试耗尽可以 Finish 为 failed；failed 的请求不可变，再试需要新请求。
- **登记要求**：`rule_selection` 须由可信 core 登记，`artifact` 须启用重判与强制依赖。工作区里 ① 的 `core_mvp_registry()` 提供这个登记（见[①部署指南第 9 节](../storage/ai-docs/deployment.md)，新库专用）。`DEFAULT_REGISTRY` 与 `contracts/` 没有改；演示与测试夹具用 `demo.fixture_registry()` 显式开启。未登记时，`extract`、`reextract` 在调用模型前阻塞为 `rule_selection_registration_required`，不静默回落到 v1.1。
- **旧数据**：不迁移、不回填。旧活动没有选择结果，不会被自动补出；仍可按当前规则直接重判已有产物。基线上占用请求 ID 的旧记录、未被追踪或结果未知的调用仍然阻塞。

**第二批：外部动作 action-v2 的 ④ 侧（工作区，未提交）**：`Command` 增加内部的 `execution` 字段与 `executing`、`unknown` 结果，`suggestions.py` 在业务提交里校验建议状态流转：`approved → executing → executed | failed | unknown`，`unknown` 经查证或用户声明已执行变 `executed`，`unknown` 或 `failed` 经带风险确认的显式重试回到 `executing`（同一 `action_key`，attempt 加一）。校验动作哈希与批准的精确版本，状态不符一律阻塞而不是变基。旧的终态回执只对没有 v2 交接的建议有效。这是模块内附加 payload，没有改公共契约。执行方在 ⑥（[⑥入口](../api/README.md)第 3.6 节）；④ 不执行外部动作，也不保证外部动作不重复。

**验证（来源与日期；作者未复跑，只用 `pytest --collect-only` 与文件哈希核对；全部合成数据，不是当前结果）**：

- 2026-11-12T01:58Z（extraction-v12 摘要，基线 79e8dc3 加当时工作区）：模块单元 113 passed（基线 84 项加 29 项新增）；`services/storage/tests/test_extraction_v12_runtime.py` 1 passed（临时 PostgreSQL，NATS 关，回环 ① HTTP、合成网关，事件分发由测试显式驱动）；模块测试用真实 `GatewayClient` 加进程内网关、journal、① 预检与合成 provider，没有真实模型与网络。当时无开关整仓 2178 passed、2 failed（e2e 夹具缺 `rule_selection` 登记）。后续 04:14Z–04:25Z 的几份摘要里，外部开关全关的整仓快照没有失败，但那是共享工作区在各自时刻的快照，不是当前结果。
- 2026-11-12T04:20Z（action-protocol 摘要）：新协议测试 84 passed（⑥ 与 ④ 合计，内存存储、真实 ④ consumer 与私有 SQLite，不是 PostgreSQL/NATS）；API 加 ④ 共 533 passed。
- 作者 04:31Z 的 `--collect-only`：`services/extraction/tests` 148 项（只收集，未执行）；报告里记录的 14 个文件中，除 `domain.py`（之后因 action-v2 增补而变）外，哈希与当前一致。
- 79e8dc3 的首批记录（真实 PostgreSQL/NATS 17 项一次连续运行等）见[跑通计划第 2.2 节](../../ai-docs/plans/mvp-runnable.md)；完整的最终数字由主 agent 在统一验证后补入[交接第 7 节](../../ai-docs/plans/mvp-runtime-handoff.md)。

**没做**：网关输出恢复与业务计划重放；用户确认重复风险后的新逻辑调用；模型已返回有效输出、业务提交被拒或结果未知时，停为 `gateway_result_not_committed`，不算 failed、不自动重发；规则级并发保护偏保守，模型返回后可能因无关的并发规则编辑而阻塞；v1.2 的 PostgreSQL 测试没有经 NATS 与真实网关传输；真实模型联调；对网关、数据库、消息总线做操作系统级强杀的恢复验证；外部动作的 HTTP 执行与真实日历适配器（在 ⑥，且未接）。详见[接入限制](ai-docs/implementation.md)。

GPS 输入按[已采纳逆地理编码提案](../../docs/proposals/gps-reverse-geocoding.md)等待②写完 `gps_geocode`；未完成时为 `geocode_pending`，终态结果作为原始证据进入模型。宿主需增加 `gps_geocode` 读授权，详见[接入说明](ai-docs/implementation.md#gps-地名等待)。
