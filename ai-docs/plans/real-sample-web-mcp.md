---
audience: ai
doc-type: guide
status: proposed
generated-by: AI
generated-at: 2026-11-18
based-on: README.md、AGENTS.md、docs/README.md；设计v1.2/信号/插件/确认索引；HEAD 84d2d0f与origin/dev ba36543祖先核对；当前app.py/evidence.py/flat workspace与core_deployment.py；/tmp/ear-real-deployment-20261118.json、ear-real-ui-fix-20261118.json、ear-gps-integration-20261118.json与ear-real-default-final-20261118.{exit,xml,log}；本轮实现授权与文档分工；最终重部署见正文
---

# 真实小样本运行时、只读 Web 与 MCP：事实与恢复交接

> **MCP/后续工程计划保持 proposed；下面已观察事实不等于提案采纳。** 本文记录真实最小证据链路，不是完整MVP、LLM提取或生产验收。行为定义见[人读MCP提案](../../docs/proposals/mcp-interface.md)与已采纳设计；旧[跑通计划](mvp-runnable.md)、[运行时交接](mvp-runtime-handoff.md)保留各时点证据，不能当作本候选的新结果。

## 当前访问方式更新：本地测试免登录

2026-11-18 用户明确要求取消测试版用户侧API token检查，现已在独立样本实例启用 `test_no_auth`。`/workspace/` 打开自动连接，无需从clients.json取令牌；下文取token的说明仅适用于此前认证模式。仍用127.0.0.1:18186及SSH回环隧道，不扩大LAN暴露。内部身份/权限保留，普通测试身份不具有插件管理权；源身份仍已撤销。本次无Bearer HTTP、真实Chromium全部页面及数据保留验证通过，具体实现与测试见[⑥实现说明](../../services/api/ai-docs/implementation.md)。重启恢复限制未因免登录而解决。

## 0. 最终真实验收与收尾（优先于下文初始空库记录）

**代码已提交并推送：`85c8e5b`，既有PR #16；包含最新dev ba36543。真实最小链路已通过，不再是空库。** 下文15:09空库、15:39构建中、源授权未证明等是此前阶段的取证，不覆盖本节。本节只保存汇总，不含原文、人名、坐标、向量、样本ID或凭据。

- 正常源Evidence/embedding API → ②标准适配器 → 独立① → ③确定性归属 → ⑥实际Web：3段已处理录音导入1007条原始记录（audio_ref3、transcript_segment573、local_speaker141、voiceprint124、ambient_sound151、gps_point1、phone_info14）。原始内容、引用和时间保留；2798条链接校验通过。
- 124条声纹及1个GPS全部具有且仅具有一条归属。自动人物为候选，不是经人工确认的真实人数或识别准确率；本次阈值没有质量验收。
- 同一3段显式选择再运行：全部AlreadyIngested、新增原始记录0、全部业务计数不变。成功导入的3个批次回执保留；读取观察中断/超时后先核对回执再继续观察，未盲目重导入。
- 真实Chromium直连实际后端、无mock：三个样本时间窗、转写原文、人物/历史/聚合、证据搜索和追溯均通过；39次HTTP响应无错误，无JS/CSP错误。精确路由、窗口及7张截图仅存私有目录，不进仓库或CI。
- **没有真实LLM提取或外部动作**。④/⑦/geocoder/执行器未启用；activity/artifact/rule/rule_selection/action_suggestion仍0，不编造活动或产物。真实GPS未地理编码，后续提取仍受既有门控约束。
- **队列尚未全部排空**：2026-11-18 17:30:52Z，attribution pending/ack_pending均0；dependency pending602、ack_pending0、redelivered0。所需125条归属完成不等于全传播完成；吞吐和长期恢复未验收。

### 源授权变化和已完成收尾

用户明确授权最多3段已处理录音、必要内网读取/复制；发现现有服务身份有额外写能力后，用户明确表示“暂时不为了只读这样的权限问题修改 ear，可写身份也可以先用来测试。”因此**没有改Ear源码、没有重启Ear或Manager**。专用token通过原机制热加载Evidence/embedding grants；凭据仍有任务注册能力、无服务端样本范围限制，本次脚本仅对固定3段及引用集合执行正常GET，不把客户端只读用法说成服务端只读角色。没有借用其他admin身份、源数据库或处理缓存绕过API。

3个原音频文件仅经SSH内网复制到私有目录，共14,663,017字节，源/本地/规范引用的SHA-256和大小一致，InboxAudioLocator通过。未注册任务、重处理、调用模型/地图/日历、修改源音频。

**临时源凭据已经撤销**：只移除此身份与新增grants，无关/并发配置保留；撤销后401、健康200、源进程不变。专用SSH控制连接已关闭，source connection为enabled=false/revoked=true。目标API用户、已导入资料和服务保留。未来导入必须重新明确范围和源凭据，不自动复活本次授权。

### 最新验证（各范围不累加）

| 范围 | 结果 | 边界 |
|---|---|---|
| 代码85c8e5b远端CI run44342822642 | **SUCCESS；4058 passed、13 skipped、2 warnings；1899.57秒；Node144通过** | 2026-11-18 16:36:39Z完成，PG/NATS/备份/API-gateway四开关全1；输入/模型/效果合成，不含用户资料 |
| 提交前本机最终全套 | **3209 passed、862 skipped、2 warnings；88.94秒；Node144通过** | ear-real-release-20261118日志/JUnit/exit0；ruff/format/pyright/lock/docs/staged gitleaks通过 |
| 真实最小链路 | 3段、1007原始、125归属、重入0新增、实际Web通过 | ear-real-import-acceptance-20261118.json，不是LLM八步或准确率验收 |
| 源端收尾 | 撤销401、健康200、进程不变、专用连接关闭 | ear-real-source-cleanup-20261118.json；不改目标资料 |

验收报告持久副本在vm-remote私有 `/home/aytony/.local/state/ear-real-sample-20261118/import-acceptance/acceptance.json`，源收尾副本在其source目录；/tmp同名报告便于当前会话取证但不保证重启持久。恢复先读摘要，不输出整个私有目录或真实资料；不要因会话丢失重新导入/发token。

访问仍用第3节的SSH隧道、`/workspace/`和私有clients.json中的api_user_token。样本日期未必是页面默认今天，先看概览，再用私有验收窗口切换时间线。原初始化回执保持不变，UI更新回执另存；helper up的冻结检查仍保守拒绝，转发单元仍不跨重启持久。**安全的更新回执采纳/重启恢复、dependency积压、人物校准、真实④/⑦提取和MCP实现仍待完成。** 本次没有授权真实资料进入云模型。

## 1. 初始基线与文档任务边界（历史）

- 只读 Git 核对：HEAD `84d2d0fa1291b3b454a3ad6e9786826c41e6373c`；`origin/dev` 为 `ba36543dfb7c24551b6c591c9acc6287be0871db`，且是 HEAD 的祖先；含 PR #20 手机录音接收及此前 GPS。没有未合并索引项。早先 HEAD697de3b、MERGE_HEAD7f98132、尚待合并ba36543的文字只属于历史。
- 新 evidence、flat viewer、runtime及部分修复仍有工作区改动；HEAD包含上游不等于这些内容已提交。本任务只写 `docs/proposals/mcp-interface.md`、本页、`services/api/README.md`、`services/api/ai-docs/implementation.md`、`ai-docs/README.md`；不修改源码/测试/部署/锁文件，不暂存、提交、fetch、合并或改Git索引。
- 保留③可信回执投影、`create_place`、v1.2、action-v2默认关闭/CLI不注入适配器/未知不自动重试；保留上游GPS/楼宇说明。⑥核心可浏览类型仍为 **17类（原15 + rule_selection + gps_geocode）**，不自动纳入system_record或动态插件。
- 原 reviewed 元数据不填写或修改。MCP提案与本页的 `status: proposed` 不改变；历史审核不是本次新正文的审核证明。

## 2. 当前 API/Web 与 MCP 的边界

| 范围 | 当前事实 | 不能推导 |
|---|---|---|
| evidence | `app.py` 已注册 `GET /v1/evidence/search`；`start/end` 是UTC Z非空≤24h半开窗口，`q` 长1–256；limit默认10、1–10；可传cursor | `/v1/evidence` 是可调用别名、语义RAG、搜索全量完整 |
| 人物 | 现有 `/v1/records/person` 及单条/历史读取 | `/v1/persons` 已接通、服务端精确姓名检索已实现；本页筛选不是完整检索 |
| flat viewer | `/workspace/` → `static/workspace.html`，同目录 `workspace.js`/`workspace.css`；不依赖旧 `static/workspace/` prototype，不替换 `/plugins/` | 旧prototype测试可作为当前入口验证；完整地图/移动客户端已完成 |
| 只读页面 | 查看17类、时间线、产物/人物/地点/规则、已有建议与反馈状态；不提供纠正/审核/执行/恢复动作 | API用户令牌本身已具备凭据级只读scope；现行普通API仍可写反馈 |
| MCP | 提案、复用方向与启用门槛存在；本候选未安装/接线MCP代码或客户端配置 | Web/API可用即MCP已交付，或PR #23 CI覆盖本候选 |

`evidence.py` 使用Reader.query/get而不全库snapshot；100候选、20页、64次Reader调用、返回行计账256KiB、响应64KiB与10秒 **cooperative** 检查是实现边界，不是底层数据库成本、wire解码内存或硬截止保证。游标查询绑定、15分钟有效、进程签名，不绑定身份；重启/换worker失效，每次仍须当前授权。跨页/join为 `live_best_effort`，补录可能重复/遗漏；截断无cursor不表示已查完。音频只投影引用id和合法偏移，available不代表已有回听服务。

页面令牌仅内存，刷新/断开清空；不写URL、日志或浏览器持久存储。正文安全显示，不加载外部资源。通用records仍可返回完整获权记录，**UI隐藏字段不等于网络未传输**；不得称其为服务器字段级ACL或MCP封闭输出。无数据/未启用处理应如实为空，不能用demo替代。

原PR #23 a7baf9e的OPEN/CI成功/冲突是此前只读观察，本轮未刷新远端PR状态。EAR-MCP-001～009保留在[提案§9](../../docs/proposals/mcp-interface.md#9-审查缺口复用决定与采纳门槛)，不是本轮Web两项P2。不得整文件覆盖app/config/business以迁就PR旧pending_contract语义；写工具、新request_id工具不进首期，stdio/SDK版本/取消/严格输出仍待实现验收。

## 3. 初始空库部署与仍适用的访问/恢复限制（历史空库）

依据 `/tmp/ear-real-deployment-20261118.json`（completed_at `2026-11-18T15:09:00Z`）：

- `vm-remote` 上项目 **`ear-real-sample-20261118`** 有8个运行服务：PostgreSQL、NATS、storage、relay、manage-worker、api、mvp-attribution、mvp-dependency。**7个healthy；relay运行但未配置健康探针**，不能说8个均经健康检查。
- 独立新库、卷、身份和私有配置；旧 `ear-dev` 保持未动。未启动④提取、⑦网关、geocoder、旧Ear源扫描或外部动作执行器；没有真实LLM、网络地理编码或外部动作。
- **NO REAL DATA IMPORTED。** 普通API用户已验证17类均200且0行，证据查询200且0行；匿名/无效令牌401，普通用户管理接口403。真实Chromium浏览已部署页面、真实HTTP/①/PG链路，**mocks=false**，观察空页面、普通用户鉴权、断开清除令牌与无持久凭据。它证明空库浏览，不证明真实内容质量/完整链路。
- 启动③/⑤属于处理能力，不是只读查看的必需步骤；空库启动不授权以后自动导入或全历史处理。真实源专用 **Evidence + embedding 读取授权尚未证明**；目的端adapter凭据/API用户令牌不等于源授权。缺权应停止，不借管理员、不扩grant、不碰旧库。

### 网络现状与重启限制

Docker网络全部保持 `internal`；虽然Compose声明回环端口，实际Docker没有发布host ports。当前可达性由私有、有界的 **host userspace loopback forwarder** 提供，不是Docker端口发布已修复：只绑定 `127.0.0.1:18100`/`127.0.0.1:18186`，每连接核对容器id与project，最多32连接、总超时180秒；应用日志关闭。

转发脚本/配置在 `vm-remote` 的私有 `/home/aytony/.local/state/ear-real-sample-20261118/`，用户级单元 `ear-real-sample-20261118-loopback.service` 是 **transient、不跨主机重启持久化**。主机重启后须操作者核对已有容器、网络、卷、身份与私有配置，人工重建/重新启动所需运行和转发状态，再验证空库/鉴权/旧ear-dev未变。不得重跑初始化覆盖材料或删卷，不承诺自动恢复、长期可靠性或从仓库命令完整可复现；`restart: unless-stopped` 不能代替转发单元和worker的恢复验证。本任务没有执行这些运维操作。

### 安全查看（不导入、不调用处理）

在操作者本机建立前台SSH隧道，保持进程运行：

```bash
ssh -N -L18186:127.0.0.1:18186 vm-remote
```

本机打开 `http://127.0.0.1:18186/workspace/`（保留末尾斜杠；用127.0.0.1以匹配public_origin）。在凭据所在的受信任本地环境中私下查看该部署的私有 `clients.json`，仅取 **`api_user_token`** 输入页面；原文件位于vm-remote私有状态目录。不要把文件/令牌打印进终端记录、聊天、报告、截图或Git，不取adapter/storage/admin凭据，不借管理员。此文与文档任务不读取其内容；无法安全取得普通用户令牌时停止并交操作者处理。隧道不提供新的API权限，也不能恢复已丢失的远端转发服务。

## 4. 初始GPS/profile与源授权调查（历史，授权结果见第0节）

当前 `services/storage/src/ear_storage/core_deployment.py` 的 `RAW_TYPES` 已包含 `gps_geocode`，extraction的读取策略引用该集合；早先“缺gps_geocode read grant”的新库profile阻塞已修复，**不是仍待实现**。这不自动修改已有库grant，也不证明源Evidence/embedding授权成立；本任务没有补grant。

④在归属检查后查询GPS地理编码并等待 `geocode_pending`；`ok/no_result/failed` 三终态均释放等待。②地名与 `building_name`、`building_type`、`building_distance_m` 是确定性原始证据，不是③place；provider v2缓存/楼宇语义保留。合成GPS夹具已补齐终态证据，不能通过略过查询、扩大授权或改变产品等待语义让测试绿。只读Web不启动geocoder，MCP也不能隐式调用第三方地图。

## 5. 验证证据：各范围分别记录，不相加

下表保留早期各套取证；当时尚无最新四开关结果，现已由第0节的85c8e5b远端CI补齐，不再是当前未测项。

| 验证/出处 | 实际结果 | 范围与限制 |
|---|---|---|
| `/tmp/ear-real-default-final-20261118.{exit,xml,log}` | **exit 0；3209 passed、862 skipped、2 warnings，109.06秒**；JUnit4071项、0 failures/errors | 默认全套，结束于2026-11-18 15:12 UTC；外部服务门控大量跳过，不是四开关全量验收，也早于UI最终修复 |
| `/tmp/ear-gps-integration-20261118.json` 与 `-runtime.{exit,xml,log}` | **10 passed、0 skipped、exit 0**；其中normal与lookup-recovery两个八步均通过 | 真临时PostgreSQL/NATS、回环HTTP/worker；输入、模型和外部效果合成，不是真实录音/LLM/geocoder/日历 |
| 同GPS报告 `-memory`、`-contracts-api` | 分别 **347 passed**、**926 passed**，均0 skipped、exit 0 | 与默认全套及其他定向范围重叠，**不相加为独立总数** |
| `/tmp/ear-real-ui-fix-20261118.json` | 两项P2 `EAR-REAL-INDEPENDENT-001/002`已修；Node **144 passed**；全部UI文件集合为144 passed/1 skipped；真实Chromium **5个合成检查通过** | 错误被筛选抹成空态、Markdown节点预算/异常边界修复；Chromium用file fixture及mock fetch、隔离网络，不是线上HTTP/CSP或重部署验收 |
| `/tmp/ear-real-deployment-20261118.json` | 空库8服务、7healthy+relay无探针；真实Chromium无mock、17类空结果/鉴权通过 | UI修复前的已部署版本；不拿本报告证明修复已在线 |

**历史失败保留**：早先默认套件 **7 failed、3197 passed、861 skipped**；4项为缺GPS终态合成夹具、2项为旧prototype断言、1项为umask下私有journal夹具。已分别修正并由上面的最终默认套件取代；不改安全检查、不把旧失败说成当前仍失败。GPS定向报告内“另3项由主agent处理”是当时边界，已被最终默认结果取代。

UI额外探针 `SCAN_DROPPED_ROW_COMPLETENESS` 的合成丢行仍可能 `complete=true`，不是本轮两项必须修复之一，**未关闭**；节点耗尽可丢弃未完成Markdown块但保证截断提示，不能宣称渲染完整。MCP原审查也未因这些UI测试自动关闭。

### API-only最终重部署

最终文件 `/tmp/ear-real-ui-deployment-final-20261118.json` 已核对完成：API-only镜像更新及83项HTTP/镜像检查通过，真实Chromium直接访问实际空库（无mock），确认修复后的JS哈希、鉴权与17类空结果；其他服务及旧ear-dev保持不变。此前15:39 UTC观察到的 `building_api_only` 为历史中间状态，不是最终结论。

原始 `initialized.json` 与 `initialization-intent.json` **原样保留**；另存私有 `ui-update-receipt.json` 以及更新前镜像。当前服务已验证运行，但 `real_sample_runtime.py up` 仍因冻结源码/镜像变化而拒绝，返回 `frozen_source_or_images_changed`；尚未实现经过审核的更新回执采纳流程。不得覆盖初始化材料、放宽校验或冒称启动helper已支持升级。转发单元仍为临时单元；重启/再次启动须按私有更新记录人工核对，不能直接重跑初始化。

## 6. 后续门槛（不是新增授权）

1. API-only最终重部署已经完成；后续需补安全的更新回执采纳与重启恢复，不放宽旧初始化校验。
2. 本次3段真实导入已完成、源身份已撤销。未来新导入仍须明确范围与源凭据；不复活旧授权、不自动扫描。
3. 查看与处理分离：只读不隐式触发导入、转写、LLM、geocoder、重新提取或外部动作。真实处理验收须另记输入/模型/效果/平台四维，不将空库和合成八步混称真实数据跑通。
4. MCP仍待采纳与实现：固定GET白名单、普通API身份、严格投影/限额/错误去敏、当前授权与撤权、stdio/SDK兼容/取消、主机数据外发同意；不得默认注册纠正或动作工具。精确姓名查询缺口显式保留。
5. 代码85c8e5b四开关远端CI已通过；主机重启恢复、长时可靠性、dependency积压仍未解决，历史验收不能替代这些未测项。

## 7. 文档检查与恢复记录

本轮实际文档检查与文件哈希写入仓库外 `/tmp/ear-real-docs-final-20261118.json`：包括起止Git基线、五文件前后摘要、reviewed字段/proposed保护、`tools/check_docs.py`、`--base origin/dev`人工复核清单与diff检查。文档检查不代替人工批准或运行验收；不为修检查越权改其他文件。记录是磁盘交接点，不是Git提交，`/tmp`不保证跨重启保存；关键结论已写入本页，不包含凭据/真实数据。
