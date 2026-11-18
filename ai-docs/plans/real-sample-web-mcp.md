---
audience: ai
doc-type: guide
status: proposed
generated-by: AI
generated-at: 2026-11-18
based-on: README.md、AGENTS.md、docs/README.md；设计v1.2/信号/插件/确认索引；HEAD 84d2d0f与origin/dev ba36543祖先核对；当前app.py/evidence.py/flat workspace与core_deployment.py；/tmp/ear-real-deployment-20261118.json、ear-real-ui-fix-20261118.json、ear-gps-integration-20261118.json与ear-real-default-final-20261118.{exit,xml,log}；本轮实现授权与文档分工；最终重部署见正文
---

# 真实小样本运行时、只读 Web 与 MCP：事实与恢复交接

> **MCP/后续工程计划保持 proposed；下面已观察事实不等于提案采纳。** 这是截至2026-11-18的限定文档同步，不是完整MVP、真实数据处理或生产验收。行为定义见[人读MCP提案](../../docs/proposals/mcp-interface.md)与已采纳设计；旧[跑通计划](mvp-runnable.md)、[运行时交接](mvp-runtime-handoff.md)保留各时点证据，不能当作本候选的新结果。

## 1. 基线、写入边界与历史说明

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

## 3. 独立空运行时：已部署，但没有真实数据

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

## 4. GPS/profile 与真实数据阻塞分别处理

当前 `services/storage/src/ear_storage/core_deployment.py` 的 `RAW_TYPES` 已包含 `gps_geocode`，extraction的读取策略引用该集合；早先“缺gps_geocode read grant”的新库profile阻塞已修复，**不是仍待实现**。这不自动修改已有库grant，也不证明源Evidence/embedding授权成立；本任务没有补grant。

④在归属检查后查询GPS地理编码并等待 `geocode_pending`；`ok/no_result/failed` 三终态均释放等待。②地名与 `building_name`、`building_type`、`building_distance_m` 是确定性原始证据，不是③place；provider v2缓存/楼宇语义保留。合成GPS夹具已补齐终态证据，不能通过略过查询、扩大授权或改变产品等待语义让测试绿。只读Web不启动geocoder，MCP也不能隐式调用第三方地图。

## 5. 验证证据：各范围分别记录，不相加

文档任务读取下列实际产物，不声称亲自重跑这些功能验收；默认全套核对exit/JUnit/log，GPS核对摘要与各套exit/JUnit。未执行最新四开关全量重跑。

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
2. 真实导入前明确小时间窗/capture集合、源系统专用Evidence及embedding读grant、数据同意和处理范围；私有材料不进仓库。缺任一授权继续阻塞，不借admin。新库读取可用不是源数据读取获批。
3. 查看与处理分离：只读不隐式触发导入、转写、LLM、geocoder、重新提取或外部动作。真实处理验收须另记输入/模型/效果/平台四维，不将空库和合成八步混称真实数据跑通。
4. MCP仍待采纳与实现：固定GET白名单、普通API身份、严格投影/限额/错误去敏、当前授权与撤权、stdio/SDK兼容/取消、主机数据外发同意；不得默认注册纠正或动作工具。精确姓名查询缺口显式保留。
5. 最新候选完整四开关重跑、主机重启恢复与长时可靠性未证明；历史2026-11-07/12的全量不能替代本轮。

## 7. 文档检查与恢复记录

本轮实际文档检查与文件哈希写入仓库外 `/tmp/ear-real-docs-final-20261118.json`：包括起止Git基线、五文件前后摘要、reviewed字段/proposed保护、`tools/check_docs.py`、`--base origin/dev`人工复核清单与diff检查。文档检查不代替人工批准或运行验收；不为修检查越权改其他文件。记录是磁盘交接点，不是Git提交，`/tmp`不保证跨重启保存；关键结论已写入本页，不包含凭据/真实数据。
