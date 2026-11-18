---
audience: human
doc-type: proposal
status: proposed
generated-by: AI
generated-at: 2026-11-18
based-on: 设计 v1.2、信号补充、插件 MVP、确认索引；integration/mvp-runnable HEAD 84d2d0f（已含 origin/dev ba36543 / PR #20 及此前 GPS）与当前未提交工作区；PR 23 head a7baf9e 的只读审查与 gh 元数据；2026-11-18 官方 modelcontextprotocol.io 规范核对；用户授权起草及2026-11-18限定五文档事实同步；部署/测试出处见工程交接，不代表MCP采纳或实现
---

# 提案：⑥ 的只读优先 MCP 接口

> **Proposed，尚未采纳、未实现、未启用。** 本文不是 PR #23 的合并批准，也不新增身份、授权或公共契约。现有单写者、反馈、用户确认保护和外部未知结果语义仍以[设计 v1.2](../design/design-v1.2.md)、[信号](../design/signals-supplement.md)、[插件 MVP](../design/plugins-mvp.md)及[确认索引](../decisions/mvp-confirmed-decisions.md)为准。工程交接、分工和验证门槛见[实施计划](../../ai-docs/plans/real-sample-web-mcp.md)。

## 1. 目标与本轮事实

目标是让获准的 MCP 主机查询 Ear 中**已有**的证据和结果，复用⑥读取边界，而不是构建第二个存储入口或默认开放纠正。Web 与 MCP 使用同一套⑥只读投影，但 Web 本地查看授权不等于把内容交给模型主机的授权。

2026-11-18 本轮只读核对得到（与早先 PR 审查分开）：

- 当前 HEAD 为 `84d2d0fa1291b3b454a3ad6e9786826c41e6373c`，本地 `origin/dev` 为 `ba36543dfb7c24551b6c591c9acc6287be0871db` 且是 HEAD 的祖先；已包含 PR #20 手机录音接收及此前 GPS 合并。早先 `697de3b` 正在合并 `7f98132`、尚未包含 ba36543 的描述已过时；当前新 evidence/Web/runtime 仍须区分工作区文件与已提交历史。
- 当前⑥已接 `GET /v1/evidence/search`，参数为 `start`、`end`、`q`、`limit`、`cursor`；`/workspace/` 已挂载 flat 三文件只读 viewer。**`/v1/evidence` 与 `/v1/persons` 不是当前路由**；人物现经 `/v1/records/person` 读取，没有专用服务端精确姓名查询。已部署的独立空库与真实 Chromium 无 mock 验证不等于已导入真实数据。
- **MCP 仍仅 proposed；本候选没有安装/接线 MCP 代码或默认启用客户端配置。** Web/API 已存在不构成 MCP 的采纳、实现、协议兼容或安全审查关闭证明。
- 历史 PR #23（`api/mcp`，head `a7baf9eb7713e8eacf595386b8088bb28df8ea64`）包含 `evidence.py`、`mcp.py`、stdio 测试、客户端配置与依赖变更；当时 `gh` 观察为 OPEN、非 draft、check SUCCESS、CONFLICTING / DIRTY。本轮不刷新该 PR 的远端状态，也不把旧成功 CI 当本候选结果。原 PR 工具含 `find_evidence`、`find_person`、`new_request_id`、`correct_attribution`、`feedback_status`；“用户已确认”的工具描述不构成可信确认。
- 当前 `evidence.py` 是主 agent 整合的有界 live 实现，不是原 PR 全库 snapshot handler。真实空库浏览、GPS/profile 修复、默认全套、UI 两项 P2 修复与最终 API 重部署的各自证据/限制统一见[工程交接](../../ai-docs/plans/real-sample-web-mcp.md)。源系统专用 Evidence 与 embedding 读授权尚未证明，**没有真实数据导入**；不借管理员令牌，不调用真实模型、geocoder 或外部动作。

## 2. 边界和架构

```text
获准的本地 MCP 主机 → 独立 stdio 薄适配器 → ⑥固定白名单 GET → ①当前可信授权
获准的同源 Web     → ⑥相同只读投影                → ①当前可信授权
```

- MCP 适配器不连数据库、不持有①/管理员/⑦凭据、不执行 shell、SQL、文件读取或任意 HTTP；只有部署者固定的⑥ origin。任何工具参数、证据正文或游标都不能改变目的地址/路径模板。
- 首期仅工具发现和调用；不增加 resources/prompts、采样、模型/provider 选择、订阅、后台处理、批量导出或远程 MCP listener。查看绝不触发导入、转写、重提取、逆地理编码、付费模型或日历动作。
- MCP 与 Web 不承担归属或提取推断，不直接改③④数据。⑧检索仍为以后；字面证据查询不等于语义 RAG 或全量问答已交付。
- 已有 action-v2 保持默认关闭、私有 journal、宿主显式注入适配器与所有方精确版本握手。禁止把 `prepare`、`pump`、`recovery`、`verify` 或审核工具加入首期 MCP；“只查证”也可能联网或写恢复日志，不能冒充纯读取。

## 3. 最小权限与数据同意

1. 只使用操作者显式给定的**普通⑥ API 用户令牌**，不复用管理员、storage 或 gateway 凭据；每次经⑥与①复核当前授权，撤权/停用后失败关闭。缺权限不返回伪空列表，不自动补 grant、换高权令牌、扫动态 registry 或改旧库。
2. 当前①按类型授权，覆盖当前和历史；没有逐记录/逐字段 ACL。时间窗、字段投影和工具白名单是本接口的数据最小化措施，**不宣称是新增① ACL**。计数、历史、引用 join、错误和续页同样不能泄露无权对象；保持现有 401/403/404 隐藏语义。
3. 当前 `api_user` 是非管理员身份，但现行 API 仍能写反馈；它**不是已有的“只读令牌 scope”**。首期由适配器固定 GET 路由限制自己的行为；令牌泄露或同 UID 主机失陷仍有风险。若需凭据级只读强保证，须另审 API scope/受控网关，不能靠 annotations、少列工具或本提案假装已有。
4. 一个本地子进程绑定一个身份；不让工具参数自报用户/角色/subject。游标不是授权；每次使用仍重新认证。多身份共享服务须另审可信主体绑定，不能复用另一人的缓存/分页状态。
5. stdio 在哪台机器运行、⑥连接目标、允许时间范围/类型、MCP 主机的数据保留及模型外发边界须在连接前告知用户。**本地 stdio 并不保证主机不把返回正文发给云模型或产生费用。** Ear 适配器不主动调用⑦，与主机本身可能调用模型是两回事；无明确数据外发同意时先用本地 Web/API，不能自动启用 MCP。
6. 固定可信解释器、工作目录及已审代码/锁文件；不在客户端配置写密钥，不 source `deploy/.env`。由受控宿主传最小环境；如后续增加令牌文件，需私有属主、0600、普通单链接、`O_NOFOLLOW`、绝对路径及有界读取，此能力目前不是已实现命令。

## 4. 拟议工具清单（都需实现与隔离验收）

| 阶段 / 工具 | 对应⑥读取 | 输入与结果最小化 |
|---|---|---|
| 首期 `find_evidence` | 现有 `GET /v1/evidence/search`（工具 phrase 映射 HTTP `q`）；MCP 适配仍未实现 | 显式 UTC `Z` 半开非空时间窗、非空字面 phrase、limit、opaque cursor；返回转写摘录、id/版本、时间、获权归属及可用性，不返回原音频 |
| 首期 `find_person` | 当前只有 `GET /v1/records/person`；原 PR `/v1/persons` 未接入，专用精确姓名查询待设计/实现 | 拟支持精确姓名、limit/cursor；最多一页 id/name/version/status；不得把现有 records 的本页筛选冒充完整精确姓名检索；同名人保持歧义，不自动选中或纠正 |
| 首期 `feedback_status` | 现有 `GET /v1/feedback/{id}/status` | 仅查询已知反馈 UUIDv7；区分 accepted/pending、所有方完成/拒绝及不可用，不能用消费标记当成功 |
| 后续只读扩展 `get_record` | 现有 `GET /v1/records/{type}/{id}` | 核心业务类型固定白名单、已知 UUIDv7、可选正整数 version；类型特定投影，不透传任意 payload、system_record、凭据或插件目录 |
| 后续只读扩展 `list_timeline` | 现有 `GET /v1/timeline` | 显式有界时间范围与一页摘要；再按已知 id 读取，不递归自动展开全部产物/历史 |
| 后续只读扩展 `get_action_status` | 现有 `GET /v1/actions/{id}/execution` | 仅当前只读状态投影，保持 disabled/unknown 与版本语义，不调用查证、推进或恢复 |

“后续只读扩展”不是 PR 当前工具，不因本文出现就默认注册。首期移除 `new_request_id` 与 `correct_attribution`，且直接调用这些未注册名称也不得触发 POST；所有反馈/规则/管理/凭据领取/外部动作写路径拒绝。

## 5. 有界输入、分页与输出

以下数值是**待评审的初期工程上限**，不是 MCP 协议规定或公共契约：

| 层次 | 拟议默认值 / 硬上限 |
|---|---|
| 时间 / 参数 | 必须 UTC `Z`，`start < end`，跨度 ≤24h；phrase/name 1–256 字符；cursor ≤512 ASCII 字符；limit 默认5、1–10，严格整数；拒绝未知字段、重复参数、非有限数和类型强转 |
| 实际查询工作 | 每调用至多100个候选、20页、64次 Reader 调用；返回行计账256 KiB；底层 I/O、解压、JSON解码与数据库查询另设硬限界，不以返回10条冒充成本有限 |
| 总时间 / 并发 | 真正整体 deadline 10s；每身份最多1个活跃数据调用、无无限等待队列，忙时固定错误；连接/读取超时取剩余时间，取消停止后续工作；无自动翻页/重试风暴 |
| 消息 | MCP 参数对象编码后 ≤8 KiB；⑥成功响应解压后 ≤64 KiB；MCP 完整工具结果序列化后 ≤64 KiB（含兼容文本与 structuredContent 两份开销）；错误 ≤1 KiB，超限先停读而不是完整缓冲后判断 |

- 不为查一小窗先 `snapshot()` 全量物化所有授权历史，也不提高①快照限额掩盖问题。当前主 agent 的新 evidence 计账与10秒检查是**同步调用之间的 cooperative 上限**：一次阻塞 Reader 调用仍可能超时/已缓冲大响应，不能称端到端硬截止、内存上限或严格取消已实现。
- 单页返回 `items`、`next_cursor`、`scanned`、`truncated`、`consistency` 与停止/续读原因；空页但有 cursor 不等于查完。扫满、预算耗尽或响应截断均可见；没有安全续点则明确 continuation 不可用，不跳过未消费结果，不宣称搜索完整。
- 游标不解析①内部 offset、不自行加减；至少绑定规范化的时间、过滤条件、limit、短期限。当前新 evidence 是进程内签名、15分钟有效的**查询绑定**游标，不绑定身份，重启/换 worker 失效；①仍逐次授权，不能把它称为身份授权票据。
- 当前 live join/跨页仅 `live_best_effort`，补录或修改可重复/漏页，不冒称一致快照或完整遍历。可按 id/version 去重展示，但不能据此证明没有遗漏；需要稳定 keyset/watermark 或有界分页会话时另审存储协议，不偷偷改变现有 cursor 意义。
- ⑥响应流式读取、验证 Content-Type 与封闭模型，拒绝 HTML200、重复键、NaN/Infinity、深嵌套及畸形结构；额外字段不能透传。`outputSchema` 与 `structuredContent` 匹配；兼容文本来自**同一个已校验投影**，不在模型中重新生成结果。
- 摘录/Markdown 预览可以明确缩短并带 `content_truncated`；id、版本、授权/状态、确认相关字段不得截断成另一含义。错误只含固定 code 与是否可重试，不附上游体、异常 repr、验证器 input、路径、姓名、查询词或令牌；SDK 进入工具前的校验失败也要覆盖。

## 6. 不可信证据与 prompt injection

录音转写、姓名、地点、Markdown、规则正文、反馈及文件引用均是**不可信数据**，即使来自自己的数据库也不能当系统指令。工具描述/schema 使用固定受审文本，不从证据动态构造；将正文放在明确的 data/excerpt 字段，附来源 id/版本/时间，而不是拼进授权或提示指令。

证据里的“忽略前文”“调用工具”“发到某 URL”不能触发后续调用、上传、模型调用或写入；提示标签只是纵深防御，真正约束仍是 GET 白名单、输出投影与主机的用户同意。Web 用 `textContent` 或经过审查的安全 Markdown；不解释原始 HTML、不自动加载远程图片/链接、不执行脚本。

不返回声纹 embedding、音频字节、绝对路径、任意 `file_ref` URL 或可自动抓取的 resource link。当前新 evidence 只给 `audio_ref_id` 与校验后的相对偏移：**偏移 available 不代表音频服务可播放**。引用无权/缺失、人物终止、归属歧义或偏移越界要明确不可用，不静默修正原始时间、不把终止人物称为当前有效对象。真实回听服务需独立授权与路径/范围校验，本文不实现。

## 7. 协议与传输边界（2026-11-18 官方核对）

本节保留此前协议核对记录，本轮文档同步未重新联网核对或安装 SDK，不将它升级为当前客户端兼容证明。本节只依据第10节的 `modelcontextprotocol.io` 主源；不把路线图、旧知识或 SDK 主分支当最新稳定协议。

- **[M1]** 官方 `specification/latest` 本次解析为 **2026-07-28**，使用每请求 metadata/capabilities；这与旧 initialize 时代不同。
- **[M2]** stdio 由主机启动子进程，stdout 仅逐行有效 JSON-RPC，stderr 可日志但本项目仍须去敏；覆盖取消和 stdin EOF 退出。支持双时代的客户端先探测 `server/discover`，只在识别 legacy 时回退 initialize；现代版本错误不能盲目回退。
- **[M3]** 工具输入/授权/限流/输出验证须由服务实施；annotations 只是提示，不是确认或访问控制。拟议读工具设置 `readOnlyHint=true`、`destructiveHint=false`、`idempotentHint=true`；`openWorldHint` 按⑥部署是否访问外部系统诚实声明，不用 false 冒充无网络。
- **[M4]** 官方该授权框架面向 HTTP；stdio 通过受控宿主提供凭据，不为本地使用另起 OAuth listener。⑥自身 Bearer 与①可信授权仍独立生效。
- **[M5]** 本地服务应使用最小进程/文件/网络权限及可信启动配置；stdio 限制接入面，但不等于 OS 沙箱、免外发或模型费用豁免。

PR 锁定 `mcp==1.30.0`；独立审查记录其最高协议为 `2025-11-25`，原测试只调用 initialize。本任务未安装或运行 SDK，**不宣称支持 2026-07-28 客户端**。发布前要固定 SDK/客户端版本，验证 legacy 协商和 modern 探测/回退，或另审升级；不静默改依赖/锁文件，不编造 SDK 不支持的配置参数。

## 8. 未来写反馈：不在首期启用

若负责人另行采纳，写能力独立显式启用，仍经⑥写反馈→①事件→③/④所属方处理，不跨写；需提供服务端可验证、绑定主体/命令/目标版本/过期时间的一次性确认，而不是 `confirmed: true`、docstring 或模型转述用户同意。

- 先用只读证据展示对象、当前归属版本、前后目标及影响；同名、未知/无权目标或用户确认保护冲突必须停下。提交前再次复核可信身份和 `expected_version`，不自动变基，版本/前目标双守护防 ABA。
- 调用方为一次逻辑操作生成 UUIDv7 `request_id`，持久保留内容指纹；同 id 同内容安全重放，同 id 不同内容拒绝。受理回执给稳定反馈 id，不等于业务成功；完成只依据可信所有方回执。原始反馈信封及 `pending_contract` 重放兼容不能被新响应文案改写。
- 网络/提交结果未知时返回未知及“仅原请求安全核对/同 id 原内容重放”的明确处置；不换 request_id 重做，不把重放可用性外推为外部 exactly-once。
- 审核日历建议、外部执行、确认已执行和带重复风险重试是**分别授权**的动作，不随纠正自动开放；维持 action-v2 的两个版本、同一 action_key、所有方握手与未知停自动重试。用户确认记录/新契约准确形状仍待负责人及相关模块审核，本提案不发明已存在的确认 API。

## 9. 审查缺口、复用决定与采纳门槛

以下是原 PR MCP 审查，不是后续 flat Web 两项 P2 复审；后者的修复验证单独记在工程交接，不能据此关闭 MCP 启用门槛。独立审查文件为仓库外 `/tmp/ear-mcp-review-20261118.json`（本任务只摘取结论，不复制个人数据/凭据）。以下均是**未关闭**项，不能把“设计有对策”当源码已修复；原审查的受限合成探针不是完整 SDK/浏览器/真实服务验收。

| 编号 / 优先级 | 审查发现 | 本提案的安全复用决定 |
|---|---|---|
| EAR-MCP-001 / P1 | 默认注册纠正 POST，缺可信确认 | 不默认启用 PR MCP；首期删除两个写流程工具，直接调用也拒绝 |
| EAR-MCP-002 / P1 | 100条扫描前先物化全授权历史 | 复用查询业务方向，不复用全库 snapshot handler；实测底层行/字节/I/O |
| EAR-MCP-003 / P2 | 新快照上的裸 offset，补录会重复/漏页 | 保留准确续点意识；查询绑定、短期限、live 一致性和截断说明，稳定遍历另审 |
| EAR-MCP-004 / P2 | 完整缓冲、额外字段与 file_ref 透传 | 复用固定 origin/GET；新增流式限额和封闭投影，不抓文件 |
| EAR-MCP-005 / P2 | 输入/总截止/取消不足，SDK 错误可能回显输入 | 严格 schema、异步/受控隔离、真实总截止与全路径去敏；不承诺已有 |
| EAR-MCP-006 / P2 | 测试/说明仍假定③一直 pending_contract | 保留当前 `attribution_result_query`、可信回执与 action-v2；不能整文件覆盖 app/config/business |
| EAR-MCP-007 / P2 | SDK 的 dotenv 默认读取边界未被说明 | 固定版本核对实际公开 API 或低层实现；不能仅凭应用没读 deploy/.env 就说依赖链不读 dotenv |
| EAR-MCP-008 / P2 | 终止人物和越界音频偏移投影不准确 | 新 evidence 有防御代码仍须测试；保留源 id/版本，不伪造有效关系/回听 |
| EAR-MCP-009 / 需求缺口 | PR 无 Web 改动；插件页/合成 demo 非真实浏览器 | 单独只读 Web 接线与浏览器验收，不以 MCP 或假截图冒充完成 |

正向保留 PR 的固定 origin、Bearer 读取、无任意 URL 工具、精确姓名/字面证据查询思路及 GET 服务骨架；审查**未证明** ACL 绕过、任意 URL SSRF 或实际隐私泄露。HTTP错误去正文是有用防御，不表示所有响应与 SDK 错误均已安全。

采纳/启用前至少通过：默认工具仅 GET 且隐藏写名称不可调用；当前授权与撤权/引用隐藏；参数/空页/跨页插入/游标过期；超大压缩响应/HTML200/畸形 JSON/注入文本；整体超时/取消/并发；stdout 协议纯净与所有错误去敏；锁定 SDK/客户端兼容；合成 Web↔⑥↔①浏览；保留 action-v2 默认关闭与真实③回执。真实数据只在主 agent 确认范围及外发边界后做小窗验收，结果不足明确失败/阻塞，不默认模型调用或全量处理。

## 10. 官方来源与待决事项

核对日期：**2026-11-18**；只使用官方主域。链接以代码形式记录，避免与内部规范混淆：

- [M1] `https://modelcontextprotocol.io/specification/latest` → `https://modelcontextprotocol.io/specification/2026-07-28`
- [M2] `https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio`
- [M3] `https://modelcontextprotocol.io/specification/2026-07-28/server/tools`
- [M4] `https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization`
- [M5] `https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices` → `https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices`

待负责人/评审者决定：是否采纳只读 MCP 与后续只读工具范围、允许的主机与数据外发范围、数值限额、所支持的客户端/SDK版本；凭据级只读 scope、稳定分页及未来确认/反馈共享契约另审。官方协议提供能力，不替 Ear 批准产品行为；本文件保持 `status: proposed`，不填写 reviewed 字段。
