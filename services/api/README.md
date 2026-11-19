---
audience: human
doc-type: reference
based-on: 设计 v1.2、信号补充、插件 MVP、工作分解 v1.2；origin/dev 376294d 的 contracts 与①代码；api/gateway-mvp 功能分支（待合并）中 services/api 的实际源码（config、cli、app、boundary、business、models、upstream、execution、execution_journal、static）与 deploy 覆盖文件；2026-11-07 本机取证记录（均在 /tmp，不属于仓库）：ear-67-browser-state.json、ear-api-gateway-deploy-state.json、ear-67-postfix-review.json、ear-67-full-acceptance.log、ear-67-acceptance-fixes.json、ear-67-full-v2.log（及 .exit）、ear-67-static-v2.log、ear-67-static-final.log（及 .exit）；用户对⑥⑦实现与文档同步的授权；2026-11-12 增补依据：services/api 工作区未提交的源码（attribution_results.py、business.py、execution.py、execution_journal.py、app.py、config.py、execution_http.py，作者读源码与 git diff，不运行测试）、services/attribution 的 create_place.py 与 feedback.py、services/extraction 的 domain.py 与 suggestions.py（只读核对）、仓库外摘要 /tmp/ear-mvp-api-place.json 与 /tmp/ear-mvp-action-protocol.json（作者读取，其中的测试结果作者未复跑，只用 pytest --collect-only 与文件哈希核对）、git 记录（8750a83 即 #11 是 HEAD 79e8dc3 的祖先）；2026-11-12 action-v2 HTTP 与证据校验增补依据：execution_http.py、execution.py、app.py、config.py、cli.py 源码与仓库外摘要 /tmp/ear-mvp-{action-http,action-evidence-fix,eight-steps}.json（作者读取并核对各自的退出码文件、日志末行与 junit 用例名，测试结果未复跑）；本文不新增用户授权记录，也不增加用户决定；未填写或改动任何 reviewed 字段；2026-11-18事实同步：HEAD84d2d0f已含origin/dev ba36543 / PR20及此前GPS；实际app/evidence/flat workspace、core profile；仓库外ear-real-deployment、ear-real-ui-fix、ear-gps-integration及ear-real-default-final的20261118产物；本轮实现授权及文档分工（功能测试读取产物而非重跑）
---

# ⑥ 客户端与 API

## 2026-11-18 当前状态（优先于下文历史批次措辞）

HEAD **`84d2d0f` 已包含 `origin/dev ba36543`（PR #20）及此前GPS**；不再处于697de3b/7f98132合并中。旧79e8dc3/2026-11-12的“第二批工作区未提交”仅是历史观察；本轮代码已提交并推送为85c8e5b至PR #16。保留③可信回执/`create_place`、v1.2、action-v2默认关闭与未知不自动重试；核心业务读取共17类（含rule_selection、gps_geocode），profile已增加extraction的gps_geocode读取授权，不自动修改已有库grant。

- **已有真实入口**：`GET /v1/evidence/search` 与 flat只读 `/workspace/`；另有 #23 的 `GET /v1/evidence`、`GET /v1/persons`（供 stdio MCP 使用，见“MCP”一节），两组证据查询并存。人物也可用 `/v1/records/person`。浏览页不提供纠正、审核、执行或恢复操作；普通API令牌本身不是新增的只读scope。
- **已有独立部署（初始为空，现已导入3段）**：vm-remote项目 `ear-real-sample-20261118`，8服务运行、7healthy，relay无健康探针。初始部署报告已用真实Chromium、无mock验证普通用户鉴权、17类空结果和空页面。旧ear-dev未动。Docker内网未实际发布host ports，回环可达性依赖私有有界用户态转发；临时用户单元不跨重启持久化，重启后须操作者人工恢复并复验，**不宣称完整可复现或自动恢复**。
- **真实最小链路已通过**：3段录音、1007条原始记录、125条需归属输入全部归属；重复导入0新增；真实Chromium无mock验证转写/检索/人物/历史。没有启动真实LLM/geocoder/外部动作，活动/产物仍为空。用户明确接受专用测试凭据具有额外写能力，实际请求限定GET；测试结束已撤销该身份并关闭专用源连接。
- **最新已读验证**：默认全套exit0，**3209 passed/862 skipped/2 warnings**；GPS定向真实PG/NATS **10 passed**（两个八步均通过，但输入/模型/效果合成），另有347内存与926契约/API，范围重叠不相加。**代码85c8e5b远端四开关CI已成功：4058 passed/13 skipped/2 warnings，Node144通过**（run44342822642）。
- **UI复审**：两个P2已修，Node144通过、Chromium5个合成检查通过（mock fetch，非线上验收）。最终API-only重部署报告 `/tmp/ear-real-ui-deployment-final-20261118.json` 已核对完成，83项HTTP/镜像检查和真实Chromium无mock验证通过，修复资产已在线。初始化材料保留；启动helper仍拒绝变化后的冻结镜像，更新回执采纳与重启恢复尚未完成，见恢复交接。
- **#23 的 stdio MCP 已随 dev 合入**，含写流程工具，与下述只读优先提案（proposed）不一致，待负责人决定。见[只读优先MCP提案](../../docs/proposals/mcp-interface.md)；实际证据、旧失败、重部署终态、访问及重启边界统一见[事实与恢复交接](../../ai-docs/plans/real-sample-web-mcp.md)。不是完整MVP或生产验收。

用户与管理员进入系统的服务端入口：读取时间线、活动、Markdown 产物、人物、地点、规则和建议；把用户操作写成结构化反馈（`feedback`）；把管理员操作写成插件管理反馈（`plugin_management_feedback`）并查询①写出的管理结果；执行已批准的外部动作并回写反馈；提供轻量插件管理页面/API。⑥只写反馈，不做推断，不直接改③④的业务数据，也不写插件登记和权限（那是①的单写职责）。

- [设计 v1.2](../../docs/design/design-v1.2.md)（第 5 节反馈、第 7.6 节外部动作）、[模块间信号](../../docs/design/signals-supplement.md)（第 3.5、6.5、6.6 节）
- [动态插件 MVP](../../docs/design/plugins-mvp.md)（管理页与异步反馈）、[工作分解 v1.2](../../docs/design/work-breakdown-v1.2.md)（⑥一节，含验收标准）
- [实现说明与验证记录](ai-docs/implementation.md)；[全局实施计划](../../ai-docs/plans/mvp-implementation.md)；[①当前状态](../storage/ai-docs/progress.md)；[⑦大模型入口](../llm_gateway/README.md)

## 网页大模型设置（显式部署启用）

在插件管理页进入 `/model-settings/`；配置 `model_settings_file` 后，管理员可查看、修改保存的模型 ID。默认使用管理员 Bearer；测试免登录模式默认不启用 `GET/PUT /v1/settings/model`。仅在显式回环管理员模式额外开启 `test_no_auth_model_settings: true` 时，网页才自动连入这两个端点，无需 Bearer。两种模式都复核①当前管理权限；免登录写请求仍要求匹配配置的同源 Origin。更新按修订号比较并原子写入；旧修订返回409，需要重新读取。保存不发起模型请求；由操作者重启⑦后读取新值，页面不把保存值当作正在运行的配置。第一版固定上游，网页不编辑地址或 API key。配置和验证边界见[⑦部署说明](../llm_gateway/README.md#直连-new-api可选)。

## 本地测试免登录（2026-11-18 用户明确授权）

`test_no_auth` 是严格布尔配置，默认 `false`，原有 Bearer 验证默认不变。
开启后，⑥忽略调用者 Authorization；绑定选择只来自受保护部署配置与端点固定映射。
`test_no_auth_role` 仅允许 `user` / `admin`，默认 `user`；`admin` 仅在 `test_no_auth=true` 时有效。
在免登录配置中，绑定的客户端 `token` 可以省略；`storage_token` 仍必需。正常认证仍要求每个绑定提供客户端令牌。能进入免登录实例的本地进程或 SSH 隧道使用者可使用开放的管理和业务接口；保持回环隔离，不转发给 LAN 或公网。
普通用户模式仍要求唯一user绑定且所有请求使用它，管理端点仍403。管理员测试模式必须
**恰好一个user绑定和一个admin绑定**，缺失或歧义拒绝启动；按本轮用户明确授权暴露两项预配置能力：

| 端点 | 管理员测试模式使用的现有绑定 |
|---|---|
| `/v1/me` | 配置的admin会话，向①复核身份；不代表业务权限已归给管理员 |
| `/v1/plugins`、所有`/v1/management/*` | 可信admin（①仍复核管理权限） |
| 所有业务路由（列表/详情/历史/检索/反馈/预览/动作） | 唯一普通user，仍受其当前类型授权约束 |

这是仅限localhost测试服务的显式端点映射，不是身份冒充、grant合并或403后的fallback。
所选绑定失败即失败，不尝试另一绑定；不能从请求头/参数选择绑定，不创建身份、不改①授权。
业务反馈的主体、来源与幂等键使用实际普通绑定；管理操作由实际管理员凭据接受①审计。
默认认证模式按Bearer绑定原样处理，不做双映射。

- 必须设置明确的回环 `public_origin`（127.0.0.1、localhost 或 ::1），不接受公网/LAN地址。
  实际监听/转发也必须保持回环或隔离内网；配置校验不替代部署网络隔离。
- 请求 Host 须与该 origin 的主机和端口一致，拒绝 DNS 重绑定主机；仅健康检查额外允许
  容器回环 `127.0.0.1:8086`。带 Origin 的请求仍须完全同源；跨站/同站非同源的
  Sec-Fetch-Site 被拒绝。所有非 GET/HEAD/OPTIONS 请求都须携带匹配的 Origin，命令行写请求亦然。
- `GET /v1/session` 仍仅公开返回 `{"test_no_auth": true/false}`，不返回凭据、绑定或角色。
  `/workspace/` 和 `/plugins/` 自动连接后，根据重新验证的 `/v1/me` 显示角色；无令牌输入或登录提示。
  普通用户不发管理请求；管理员自动读取插件目录/管理结果，管理操作仍需手动提交及既有确认。
- 免登录**不是只读 scope**：选定绑定原有的业务或管理写入口也不要求浏览器令牌。
  能访问回环端口的本地程序可在带正确 Origin 后提交获权操作；不要通过公网/LAN代理暴露。
  工作台本身仍只发 GET；开启管理员免登录不构成自动登记、启停、撤权或执行动作的授权。
- 内部身份、当前类型授权、管理员权限和撤权检查不变；管理员并未获得业务类型读取权。
  工作台业务请求明确使用既有普通绑定，管理请求使用既有可信管理员；界面显示
  “管理请求使用管理员，业务数据使用普通用户”，不展示内部凭据。
  所选内部凭据失效时失败关闭并提示部署方检查，而非换身份或索要浏览器令牌。
- 测试结束设回 `test_no_auth=false` 时，也须将 `test_no_auth_role` 恢复为 `user` 或删除该字段；
  默认认证重新生效，已配置的管理员绑定仍须使用其独立 API 令牌。

**隔离实例修复范围**：保留两个现有绑定和全部凭据，只更新API镜像实现上述映射；不修改
业务数据或grant。旧管理员单绑定实现使17类业务全部403，是已确认的可用性回归，旧失败
回执 `/tmp/ear-admin-noauth-20261118.json` 原样保留，不作为最终限制或验收通过依据。
修复后已实测17类无Bearer业务读取全部200、插件目录与管理结果200，真实Chromium可浏览转写/历史并进入管理区；详细范围见[实现说明](ai-docs/implementation.md)。
最终证据单独写入 `/tmp/ear-admin-noauth-final-20261118.json`，不改原初始化材料。

### 工作台每个页面做什么

| 页面 | 当前实际功能与边界 |
|---|---|
| 概览 | 检查⑥→①连通和17类数据的可读性、最早一条入口；不是全库总数或所有worker的健康报告。 |
| 证据时间线 | 按时间范围查看活动和不同原始证据流，分页与短语检索；搜索窗口最多24小时，有界扫描/截断会提示。 |
| 活动 | 查看划分结果、描述、关联证据、产物、动作建议及规则选择；不触发提取、不编辑活动。 |
| 产物 | 安全展示Markdown正文、关联记录、版本历史和差异；不生成、不编辑，不加载正文里的外部图片/链接。 |
| 人物 | 查看已归属人物（含未命名人物）、参考声纹、关联归属及版本历史；不改名、不纠正归属。 |
| 地点 | 查看参考GPS、已有逆地理编码地址与相对位置示意；无地图底图，不调用外部地图/地理编码。 |
| 规则 | 查看自然语言适用范围、提取指令、可选动作及历史版本；不编辑规则或重算历史。 |
| 动作建议 | 查看日历/提醒建议、状态和只读执行状态；不审核、不执行、不重试。 |
| 追溯 | 按类型和ID查看记录、历史、差异、引用及反馈状态；不冒充真实依赖图，公共API尚未暴露完整依赖/新鲜度。 |
| 插件管理（独立页） | 原有管理员可登记、授权、启停、管理凭据和历史任务；普通测试身份无此权限；显式管理员测试身份可进入，操作须手动确认。不是插件市场或进程控制台。 |

当前3段真实录音已导入；原始证据与归属可浏览，但没有启动真实LLM提取，因此活动/产物为空不等于连接失败。
免登录更新不导入新数据，不启动模型、地理编码或外部动作；实际更新与测试证据见
[实现说明](ai-docs/implementation.md)。

## 1. 历史批次状态（2026-11-07/12）

本节保留当时的交付与未测边界；“未提交/未部署/浏览器只对假①”等仅描述对应批次，不覆盖上面的2026-11-18真实小样本验收。

**部分交付，待审。这不是完整的 ⑥ MVP，也不是生产就绪。** 首批代码已在 dev 的历史中（git 记录里 8750a83，即 #11，是 HEAD 79e8dc3 的祖先；早先“功能分支 `api/gateway-mvp` 待合并”的说法已过时）。第二批已于 `697de3b` 提交至 PR #16（未合并）；以下旧“工作区”措辞保留为当时记录，不代表当前Git状态。

- 已有（79e8dc3）：只读 API、结构化反馈入口、管理员的插件管理接口与轻量页面、插件目录、受保护配置与命令行、容器镜像与可选 Compose 覆盖文件、一个离线的外部动作执行/核对切片。
- 第二批（工作区，未提交）：（1）**③ 命令的结果投影，`create_place` 已接**：79e8dc3 对所有非 extraction 命令都只返回 `pending_contract`；现在 ⑥ 受理 `attribution` 命令后返回 `integration: attribution_result_query`，并把 ③ 写的 `attribution_feedback_result` 只读投影为反馈状态，支持集为 `correct_attribution`、`confirm_attribution`、`add_reference`、`remove_reference`、`rename`、`create_place`；范围重新归属 `reattribute_range` **未做**（见第 3.4 节）；（2）**外部动作 action-v2**（`ExecutionPump`）：与旧的离线切片不是一回事。协议与可选的 HTTP 接线都在工作区：HTTP 由 `Settings.execution_action_v2`（默认 `false`）加私有 journal 加宿主显式注入的可信适配器启用，**`ear-api serve` 不注入适配器，所以默认与命令行服务上仍是旧的 501**，仓库里没有真实适配器（见第 3.6 节）。
- 未完成：外部动作的执行、“确认已执行”和“风险重试”在默认配置与命令行服务上仍返回 501（action-v2 的 HTTP 只在宿主显式启用并注入适配器时可用，见第 3.6 节）；`dependency` 命令（`reevaluate_related`）只被记录（`integration: pending_contract`），没有消费方；`reattribute_range` 未做；没有接真实日历或提醒事项，没有执行 worker。
- 验证有限：真实浏览器检查用的是内存**假①**；容器检查是合成容器。**2026-11-07 的记录（取证于功能分支，不是 79e8dc3 或当前工作区的结果）**：修复后的最终全套（四个开关全开，`/tmp/ear-67-full-v2.log`）退出 0，2067 passed、11 skipped、2 warnings，用时 1001.65 秒；最终静态检查（`/tmp/ear-67-static-final.log`）退出 0。**历史失败保留**：12:35 全套为 3 failed，14:26 静态检查因 `ruff format` 失败，均已修复并重跑，见第 4 节。这些都是合成/隔离环境的结果，不是生产验收，也不改变上面的未完成项。第二批的验证来源与日期见第 4 节末尾。
- 没有生产启动，没有真实模型或真实日历参与。

## 2. 目标与边界（定义在设计文档，此处不重复）

| 能力 | 边界 | 当前 |
|---|---|---|
| 读取：时间线、活动、md 产物、人物、地点、规则、建议 | 经①读取并服从其授权；不做推断；Markdown 按纯文本展示 | 已实现（17 个业务类型，含 `rule_selection` 与 `gps_geocode`），见第 3.4 节 |
| 用户反馈：纠正/确认、参考、命名、规则、手动提取、建议审核等 | 写 `feedback`（⑥单写）；由③④⑤按所属方执行；⑥只展示“处理中”与最终结果 | ④ 命令已接通；③ 的六种命令（含 `create_place`）经 `attribution_feedback_result` 投影结果（第二批，工作区）；`reattribute_range` 未做；⑤ 命令只记录，等契约 |
| 管理反馈与轻量插件管理页/API | ⑥验证管理员后写管理反馈，①异步处理并写结果；区分已受理/已生效、已启用/进程在线；凭据只经一次性领取交付 | 已实现；真实浏览器 34 项检查**只对内存假①**通过；API 层另有对真实回环①+隔离 PostgreSQL/NATS 的 26 项集成测试通过（管理处理步骤由测试显式驱动，合成 provider）；没有真实浏览器↔真实①的页面联调 |
| 外部动作执行（日历、提醒事项） | 只执行已批准且查过当前状态的建议；稳定动作标识；结果未知时停止自动重试并提示用户核对；结果写反馈，由④更新建议 | 只有离线切片；默认配置与命令行服务上 HTTP 仅“只读查证”可用（需代码注入可信适配器），执行/确认/风险重试 501。action-v2 在工作区有协议和可选的 HTTP 接线（默认关闭、宿主注入适配器），见第 3.6 节；无真实适配器 |
| 历史补处理与恢复选择 | 用户显式选择范围或补/跳，经管理反馈交①；读历史不等于批量生成授权 | 页面已实现；①侧执行与⑤调度未联调 |

不承诺完整时间线/地图/移动端客户端；当前已有轻量只读 `/workspace/` viewer。通用插件结果页、插件市场及自动安装启动仍未做。

## 3. 实际用法

### 3.1 前置条件

- ① 服务在线，并已为本服务签发 `api` 模块的凭据：普通用户绑定用普通 `api` 凭据；管理员绑定必须是①里带可信管理员角色的 `api` 凭据。凭据怎么签发、怎么授权，见[①部署指南](../storage/ai-docs/deployment.md)。⑥ 不创建身份、不迁移数据库、不读 `.env`。
- 每个绑定有两个互不相同的 43 位令牌：给调用 ⑥ 的人用的 API 令牌，和 ⑥ 调用①用的 `storage_token`。生成方式：`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`。所有令牌必须两两不同。

### 3.2 配置文件

JSON 文件，必须是当前用户拥有的普通文件、权限不含 group/other（如 0600）、单链接、不超过 64 KiB、不是符号链接、不含重复键；否则启动失败：

```json
{
  "storage_url": "<①服务的根地址，只含协议、主机和端口，如 http://127.0.0.1:端口>",
  "bindings": [
    {"subject": "alice", "role": "user",  "token": "<43位>", "storage_token": "<①签发的 api 凭据>"},
    {"subject": "ops",   "role": "admin", "token": "<43位>", "storage_token": "<①上带管理员角色的 api 凭据>"}
  ],
  "public_origin": "http://127.0.0.1:8086",
  "execution_journal": null
}
```

- `public_origin`：浏览器地址栏的来源必须与它完全一致，否则带 `Origin` 的请求得到 `cross_origin_denied`。
- `static_dir`：默认使用随包的 `static/`（含 `/plugins/` 与 flat `/workspace/` 页面）；显式写 `null` 可关闭页面。
- `execution_journal`：外部动作的私有恢复日志路径；不设则外部动作状态显示 `disabled`。
- `execution_action_v2`（严格布尔，默认 `false`）：只有同时设了 `execution_journal`、且宿主在代码里把可信适配器传给 `create_app(settings, adapter=…)` 才启用 action-v2 的 HTTP；配置文件里写 `true` 并不会让 `ear-api serve` 获得适配器，推进类路径仍是 501。见第 3.6 节。
- 可选：`timeout`（默认 10 秒，≤60）、`max_body_bytes`（默认 65536，上限 65536）、`body_timeout`（默认 10 秒，≤30）。
- 最多 64 个绑定，`subject` 不得重复。

### 3.3 命令

```bash
# 先设置 EAR_API_CONFIG 为 ⑥ 配置文件的绝对路径（受保护文件，见上文配置说明）；未设置会直接报错退出。
(
  set -e
  : "${EAR_API_CONFIG:?先设置 EAR_API_CONFIG 为 ⑥ 配置文件的绝对路径}"
  uv run --frozen ear-api check  --config "$EAR_API_CONFIG"    # 只校验配置，不连①
  uv run --frozen ear-api health --config "$EAR_API_CONFIG"    # 连①，输出 ready/unavailable，退出码 0/1
  uv run --frozen ear-api serve  --config "$EAR_API_CONFIG" --host 127.0.0.1 --port 8086    # 默认值，需要时直接改
)
```

`--config` 也可用环境变量 `EAR_API_CONFIG`。`serve` 固定单 worker（外部动作日志要求）。失败时只输出 `{"code":"api_startup_or_health_failed"}`，不回显配置、令牌或异常文本。

容器：`deploy/Dockerfile.api`；可选覆盖文件 `deploy/compose.api-gateway.yaml`（profile `api-gateway`，需显式提供受保护的配置文件路径 `EAR_API_CONFIG_FILE`，其文件须为 UID 10001、权限 0600）。覆盖文件不做迁移、不初始化身份；新建命名卷会复制 UID 10001/0700，已有命名卷不会自动修复权限，权限不对时服务拒绝启动。⑦网关见[⑦入口](../llm_gateway/README.md)。

### 3.4 接口一览

除静态页面外，所有响应是 JSON，错误形如 `{"code": "..."}`，不含密钥、请求体或内部异常。默认认证：`Authorization: Bearer <API 令牌>`，只认请求头；显式本地测试模式例外见上文。

| 方法与路径 | 谁可用 | 说明 |
|---|---|---|
| `GET /health/live`、`GET /health/ready` | 无需认证 | 存活；就绪（范围仅为 ⑥ 到 ① 的连通） |
| `GET /v1/session` | 无需认证 | 最小测试模式标志，无凭据/身份信息 |
| `GET /v1/me` | 任一绑定 | 当前主体与角色（会先向①复核身份） |
| `GET /v1/records/{type}` | 任一绑定 | 分页列出（`limit`、`cursor`、`start`、`end`、`include_superseded`）；类型按下述核心登记表规则推导，集合外返回 404 `not_found` |
| `GET /v1/evidence/search` | 任一绑定 | 有界字面证据查询：`start`、`end`、`q`、`limit`、`cursor`；UTC Z非空≤24h，q长1–256，limit默认10且1–10；当前授权、live_best_effort与截断可见；不是硬deadline或完整搜索保证 |
| `GET /v1/timeline` | 任一绑定 | 等同 activity 列表 |
| `GET /v1/records/{type}/{id}`（可加 `?version=`） | 任一绑定 | 单条 |
| `GET /v1/records/{type}/{id}/history` | 任一绑定 | 版本历史（`after_version`、`limit`） |
| `POST /v1/feedback` | 任一绑定 | 提交反馈，见下 |
| `GET /v1/feedback/{id}/status` | 任一绑定 | 反馈处理状态 |
| `POST /v1/previews/reextract`、`GET /v1/previews/person/{id}` | 任一绑定 | 只读预览，不写任何数据 |
| `GET /v1/actions/{suggestion_id}/execution`、`POST .../recovery` | 任一绑定（读权限由①判定） | 外部动作状态与恢复操作；默认走旧切片，启用 action-v2 后的行为见第 3.6 节 |
| `POST /v1/actions/{suggestion_id}/prepare`、`POST .../pump`（可选action-v2） | 任一绑定（读权限由①判定） | 只在 action-v2 已启用时可用，否则（含命令行服务）501；见第 3.6 节 |
| `GET /v1/plugins` | 仅 admin | 插件目录（①只读元数据；`online` 恒为 `null`，不是“在线”） |
| `POST /v1/management/requests` | 仅 admin | 提交管理反馈，返回 202 与 `feedback_id`、`result_id`（已受理 ≠ 已生效）；请求体由①的 `ManagementInput` 校验 |
| `POST /v1/management/credentials/{feedback_id}/claim` | 仅 admin | 一次性领取凭据，空请求体；第二次被拒 |
| `GET /v1/management/results`、`/results/{id}`、`/history/jobs/{id}` | 仅 admin | 管理结果与历史补处理任务状态 |
| `/plugins/` | 页面本身无需认证 | 插件管理页；必须带结尾斜杠，`/plugins` 返回 404 |
| `/workspace/` | 页面本身无需认证；默认业务读取须普通API令牌，测试模式自动连接 | flat三文件只读viewer；不加载旧workspace目录prototype；不提供写反馈或外部动作按钮 |

可浏览业务类型从 `ear_contracts.DEFAULT_REGISTRY`（随代码发布的核心登记表，也是 `MemoryStorage` 的默认登记表）推导：仅纳入 `kind` 为 `raw_record`、`semantic`、`rule` 的核心类型，并排除 `plugin_management_feedback`。登记表暂无管理类型标志，⑥ 用 `MANAGEMENT_TYPES` 显式排除该类型，因为插件管理反馈走管理接口；所有 `system_record` 都不纳入。当前结果在原有15类基础上增加两类：静态核心补充 `rule_selection`（`CORE_MVP_BUSINESS_ADDITIONS`，不在随代码发布的登记表里，见 `business.py`）和 `gps_geocode`（见[已采纳 GPS 提案](../../docs/proposals/gps-reverse-geocoding.md)），共17类，列表、单条及历史接口共用此集合，权限仍由①检查。

此处不在运行时请求①的 `/v1/registry`，也不自动纳入动态插件类型；这两项仅为未来可选方向，须另行作出产品决策。

**提交反馈**（202）：`request_id` 为调用方生成的 UUIDv7；同一请求重试必须复用同一 `request_id` 且内容相同，反馈 id 由主体与 `request_id` 确定性推出，重放不会产生第二条。

```bash
# 先设置：EAR_API_URL（⑥ 根地址，形如 http(s)://主机[:端口]）、EAR_PERSON_ID（要改名的人物 id，须是你自己数据里的真实 id）；缺少的会逐项询问。
# 令牌用 read -rs 输入（不回显、不进 history），经 stdin 交给 curl，不出现在命令行参数里。
# 重试同一请求：export EAR_REQUEST_ID=上次打印的值，复用同一 request_id 且内容相同。
# `attribution` 命令的结果投影与 `create_place` 见下文；`dependency` 命令仍只记录 `pending_contract`（见第 2 节）。
(
  for v in EAR_API_URL EAR_PERSON_ID; do
    if [ -z "${!v:-}" ]; then read -r -p "$v: " "$v"; fi
    [ -n "${!v}" ] || { echo "缺少 $v" >&2; exit 1; }
  done
  read -rs -p "调用方令牌（不回显）: " TOKEN; echo
  export EAR_PERSON_ID
  : "${EAR_REQUEST_ID:=$(uv run --frozen python -c 'from ear_contracts.base import new_id; print(new_id())')}"
  export EAR_REQUEST_ID
  echo "request_id=$EAR_REQUEST_ID" >&2
  body=$(uv run --frozen python -c 'import json, os; print(json.dumps({"request_id": os.environ["EAR_REQUEST_ID"], "module": "attribution", "command": {"kind": "rename", "target_id": os.environ["EAR_PERSON_ID"], "name": "新名字"}}, ensure_ascii=False))')
  curl -sS -X POST "$EAR_API_URL/v1/feedback" \
    -H @- -H 'Content-Type: application/json' --data-binary "$body" <<< "Authorization: Bearer $TOKEN"
)
```

- `module` 为 `extraction` 时，`command` 沿用 ④ 既有的 `Command`，返回 `integration: "existing_command"`，状态来自对应的 `extraction_run`。
- `module` 为 `attribution` 或 `dependency` 时，只接受 `correct_attribution`、`confirm_attribution`、`add_reference`、`remove_reference`、`rename`、`create_place`、`reattribute_range`（属 attribution）和 `reevaluate_related`（属 dependency）。
  - **`attribution` 命令（第二批集成能力）**：受理时返回 `integration: "attribution_result_query"`，业务状态恒为 `pending`；它只表示 ⑥ 有结果投影能力，**不表示 ③ 的 worker 在线或已经部署上线**。状态查询读 ③ 写的 `attribution_feedback_result`（可信 core 单写类型）：没有回执仍是 `pending`；受理（`MarkProcessed`、原始反馈被领取）不等于成功；成功或拒绝来自该回执，拒绝只回显白名单内的错误码，其余统一为 `attribution_rejected`；回执畸形或有歧义返回 503，没有该类型的读授权返回 403（授权被撤销后也是，旧登记表上的受理与重放仍可用）。⑥ 不自授权，不改部署。
  - **`create_place`**：请求体为 `name`（1–256 字符）与 `gps_ids`（非空、至多 100 个、无重复、UUIDv7）；③ 在一个受围栏的批次里新建地点，并把这些 GPS 点归属到它（沿用已有归属对象的 ID，移除旧参考，新版本带完整依赖字段），`expected_version` 须为空，受保护的归属拒绝。成功回执的元数据只含 `new_target_id`、`subject_ids`、`attribution_ids`，三者各 1–100 个唯一 UUIDv7、等长，且 `subject_ids` 与存储的 `gps_ids` 顺序一致；不透传名字、提示词或令牌。
  - **`reattribute_range` 未做**：⑥ 仍按请求模型受理，但不在投影支持集内；③ 写 `unsupported_command` 拒绝回执，⑥ 不把它说成成功。
  - 历史请求信封不变：受理写入的原始反馈里 `integration` 仍是 `pending_contract`（为了不改变请求指纹与已受理请求的重放），对外的 `attribution_result_query` 是响应与状态里的字段；真实 PostgreSQL 的重放测试把实际批次信封与手写的 79e8dc3 信封逐一对比过。
  - **`dependency` 命令**：写入后返回 `integration: "pending_contract"`，状态一直是 `pending`——**没有任何模块消费它**。
- 状态里的 `status_scope` 固定为“反馈处理，不是外部执行”；受理或入队不是业务完成。

### MCP（Codex 与 Claude Code）

在仓库根目录启动客户端。仓库提供 Claude Code 的 `.mcp.json` 和 Codex 的 `.codex/config.toml`，使用同一启动命令，服务名都是 `ear`。Codex 需信任本项目，Claude Code 首次使用时按客户端提示启用。启动命令为 `uv run --frozen --package ear-api ear-mcp`。stdio 使用 stdout 传协议消息。

Codex 项目配置可使用：

```toml
[mcp_servers.ear]
command = "uv"
args = ["run", "--frozen", "--package", "ear-api", "ear-mcp"]
env_vars = ["EAR_API_URL", "EAR_API_TOKEN"]
```


按 §3.1–3.3 启动①和⑥。在启动 MCP 客户端的环境中仅设置 `EAR_API_URL`（默认 `http://127.0.0.1:8086`）和 `EAR_API_TOKEN`（⑥普通用户绑定的调用方令牌）。MCP 不读取 `deploy/.env`，文件缺失也能启动；不要把整个 dotenv 导入 MCP。远程⑥使用 HTTPS。重连 MCP 后可用；尚未配置令牌时可发现工具，数据调用会提示缺少凭据。

| 工具 | 输入与返回 |
|---|---|
| `find_evidence` | UTC 半开区间、可选原话（原样子串）与条数（默认 5，最多 10）。返回带稳定 ID 的转写摘录、音频文件引用与相对秒数、局部说话人、声纹和可唯一确定的当前人物归属。无声纹或归属时相应字段为空。每次最多检查 100 条转写，空页也要检查 `next_cursor`，保持原查询条件续查。 |
| `find_person` | 按完整姓名查当前人物，返回 ID，支持游标。同名时让用户明确目标。 |
| `new_request_id` | 为一次新纠正分配 UUIDv7，调用方保存供重试。 |
| `correct_attribution` | 用户确认后，传声纹 ID、原人物 ID、新人物 ID 和请求 ID；同一操作重试复用全部参数。纠正对象是声纹，同一声纹可能对应多条转写。 |
| `feedback_status` | 用受理回执中的反馈 ID 查处理状态。③的纠正显示 `attribution_result_query`；③尚未写入结果时为 `pending`，①的登记表没有 `attribution_feedback_result` 时返回 503。 |

查询对应⑥的 `GET /v1/evidence` 和 `GET /v1/persons`，沿用调用方对①的权限。录音位置依据②的 `audio_ref.file_ref`（相对 Octopus 数据根目录）；实际回听需要客户端能访问对应文件。MCP 只读取已入库的转写；手机收件、解析和常驻入库沿用各模块部署。

### 3.5 插件管理页

`/plugins/` 提供：令牌只放内存、普通用户不发任何管理请求；已受理 ≠ 已生效，已启用 ≠ 进程在线（在线状态始终显示“未知”）；凭据领取需手动确认且同一反馈只尝试一次；历史补处理范围无默认值，按 UTC 半开区间提交；恢复选择（补/跳）必须明确并带修订号；状态轮询有次数、并发和连续失败上限。CSP 为 `script-src 'self'; style-src 'self'`，无内联脚本和外部资源。纯逻辑测试：在 `services/api/` 下运行 `node --test tests/ui/core.test.js tests/ui/page.test.js`（Node 24 不接受目录参数）。

### 3.6 外部动作：旧离线切片与可选的 action-v2（协议加 HTTP）

**默认（未启用 action-v2）是旧的离线切片，HTTP 上仍是 501**（79e8dc3 的行为；作者读 `app.py`、`cli.py`，默认配置与命令行服务仍如此）：

- `GET /v1/actions/{id}/execution` 读建议的当前状态与本地日志状态；`execution_enabled` 恒为 `false`。
- `POST /v1/actions/{id}/recovery` 的 `operation` 为 `verify`、`confirm_executed`、`retry`：只有 `verify` 且部署代码注入了可信适配器和日志时才会执行**只读查证**（从不调用 `execute`）。`retry` 不带 `acknowledge_duplicate_risk` 得到 409；其余情形一律 501（`extraction_execution_recovery_protocol_pending`）。
- `ear-api serve` 不注入适配器，所以命令行起的服务上 `verify` 也是 501。真正执行动作的 worker、真实日历适配器都没有接线。
- 离线切片内部：先写本地日志意图再调用适配器；结果未知或崩溃后只查证、不重发；日志必须保留，不可为恢复而清空。细节见[实现说明](ai-docs/implementation.md)。

**action-v2 协议（已集成，默认关闭）**：`ear_api.execution.ExecutionPump` 与 ④ 侧的状态流转（[④ README](../extraction/README.md)）配套，是模块内的附加 payload，没有改公共契约。HTTP 接线见下一段。

- 流程：审核仍走 `POST /v1/feedback`，等到真正 `approved`（“已受理”不算）；`prepare(suggestion_id, expected_version)` 冻结动作并暂存意图与 start 反馈；④ 的 consumer 把建议改为 `executing`；`pump` 观察到这个确切的版本、状态与回执后，先把意图持久化进私有 journal，再查证、至多执行一次适配器；适配器结果经反馈回 ④，`pump` 在确切的所有方状态下才把投递标为已应用。`unknown` 不自动重试、不换标识；`reconcile` 只查证，查不到也保持 `unknown`；`confirm_executed` 需 `acknowledge_executed=True`，来源记为用户、不接受外部标识；`retry` 需 `acknowledge_duplicate_risk=True` 并带两个版本号，沿用同一 `action_key`、attempt 加一，并重做一次所有方握手。
- 适配器必须由宿主显式注入，`client.module == "api"`，声明可靠的幂等，稳定命名，返回绑定同一 `action_key` 与哈希的类型化结果。**仓库里没有真实适配器。**适配器的返回值逐字段严格校验（对实例字段做 `ExternalResult.model_validate(…, strict=True)`）：非 `ExternalResult`、带额外字段、枚举或字段类型非法、键或哈希不符、`succeeded` 缺真实外部标识，以及借 `model_construct`、`model_copy(update=…)` 绕过校验得到的实例，加上任何异常，都记为 `unknown`，不当作成功或确定失败（审查项 EAR-ACTION-REVIEW-001，已修，2026-11-12T06:37Z 复测）；只有明确的 `not_executed`，或带 `retry_acknowledged` 的规范化 `unknown` 且已通过确切的所有方握手，才允许执行。
- journal 是单所有者的私有 SQLite（0600，生命周期独占文件锁，`synchronous=EXTRA`）：存完整的待发 Command、稳定反馈身份与所有方确认；旧 JSON 行不改写地加载；更新按旧字节做 CAS；动作键、哈希、批准版本与适配器不可变；未知时绝不清空或重置。空的替换 journal 不能认领已 `executing` 的建议。
- 与旧切片的关系：旧的 `ExecutionService` 拒绝 v2 的 journal 与建议状态，`ExecutionPump` 拒绝旧行，**不要把旧的 `process()` 接到外部 worker**。
- 单所有者、仅限本地文件系统；没有通用的外部恰好一次承诺；没有后台 worker（HTTP 回合是显式、有界的），没有对 `extraction_run` 拒绝原因的轮询。

**action-v2 的 HTTP 接线（可选，默认关闭）**：启用要同时满足三点：`Settings.execution_action_v2=true`（默认 `false`）、已设的 `execution_journal`（私有绝对路径，不自动创建，重启间保留）、宿主在代码里调用 `create_app(settings, adapter=<可信适配器>)`。缺一则不启用，推进类路径仍 501。`ear-api serve` 只调 `create_app(settings)`，**不注入适配器，命令行服务不会进入 v2**；不起后台线程、没有自动执行 worker，操作者或客户端显式调用，并另行运行真实的 ④ consumer 推进状态。

| 路由 | 作用 |
|---|---|
| `GET /v1/actions/{id}/execution` | 只读：不查证、不写反馈、不推进；返回 `expected_version`、`expected_revision`、所有方建议状态、本地执行状态、反馈投递状态与可用的恢复操作；不披露动作正文、键、哈希或外部标识 |
| `POST /v1/actions/{id}/prepare` | 体 `{expected_version}`；只冻结意图并提交 start 反馈；拒绝未审核、过期或已开始的建议；不查证、不执行；202 |
| `POST /v1/actions/{id}/pump` | 体 `{expected_version, expected_revision}`；在同一把 journal 锁内核对两个版本后推进**一个**有界回合（所有方握手、持久意图、至多一次适配器调用）；202 |
| `POST /v1/actions/{id}/recovery` | 体带两个版本号和 `operation`：`verify`/`reconcile`（只查证，查不到也不自动重试）、`confirm_executed`（须 `acknowledge_executed=true`）、`retry`（须 `acknowledge_duplicate_risk=true`，同一 `action_key`、attempt 加一，再做一次所有方握手）；202 |

- **不绕过审核**：审核仍是 `POST /v1/feedback` 的 `review_suggestion`，`prepare`、`pump`、`recovery` 都要求所有方状态已真正 `approved`；“反馈已受理”或排队的 `extraction_run` 不是批准，也不是执行授权。恢复前须先让待处理反馈被 ④ 真正应用并同步（显式 `pump`）。
- **请求边界**：沿用既有的有界严格 JSON 解码（大小、时间、深度、成员数限制，拒绝重复键与重复查询参数）、严格整数与布尔、按操作封闭的模型；不接受请求里的 URL、provider、键、动作、结果、回执或外部标识；不公开 `action_result`。
- **授权**：每个合法请求都用该请求绑定的凭据重新向 ① 认证，并复核对建议的当前读访问（不使用宿主注入的服务客户端）；无读授权返回 404 `not_found`（① 的 id 读取隐藏，不是 403），令牌无效或已撤销返回 401；查证期间被撤权，核心在查证后会重新读取。
- **并发**：并行 pump 只有一个推进，其余版本冲突；两个版本号不符返回 409，绝不变基重执行。
- **验证口径**：合成适配器；真实 ①/⑥ 回环 HTTP 与临时 PostgreSQL/NATS 加真实的 ④ consumer，见第 4 节；没有真实日历、真实 provider 或真实模型。适配器须自己保证可靠幂等并设有限的 I/O 超时，HTTP 回合有界，但不会强制取消正在执行的适配器。

### 3.7 独立测试实例的只读查看

本机建立并保持SSH隧道（只转发API，不增加权限）：

```bash
ssh -N -L18186:127.0.0.1:18186 vm-remote
```

在本机打开 `http://127.0.0.1:18186/workspace/`。本轮按用户授权开启本地测试免登录，
刷新页面后自动连接，不需要从 `clients.json` 取令牌。原私有凭据文件保留不变，
不得打印/上传；若操作者将 `test_no_auth` 恢复为 `false` 并复位 `test_no_auth_role`，页面恢复原有内存令牌连接流程。
管理员测试会话下，业务请求固定使用普通绑定浏览现有真实样本，管理请求使用管理员；不是模拟数据，也不是管理员新增业务授权。

远端回环18186依赖transient用户态转发，不是Docker已发布端口；主机重启后不会自动保留，须操作者人工重建所需运行/转发状态再验证，不能通过重跑初始化、删卷或借管理员令牌解决。具体边界见[交接§3](../../ai-docs/plans/real-sample-web-mcp.md#3-独立空运行时已部署但没有真实数据)。只读查看不启动导入、LLM、geocoder或动作。

## 4. 验证（第一部分为 2026-11-07 功能分支的取证记录，第二部分为第二批的来源记录；均不得当作完成证明）

下表是 2026-11-07 功能分支上的记录，**不是 79e8dc3 或当前工作区的结果**；其中标“当前”的旧说法已改为“2026-11-07 最终”。

| 检查 | 结果 | 不能说明什么 |
|---|---|---|
| **全套测试·2026-11-07 最终**（四个开关全开，14:09 起、14:26 结束，`ear-67-full-v2.log`） | **通过**：退出 0；2067 passed，11 skipped，2 warnings，用时 1001.65 秒（16 分 41 秒） | 合成/隔离环境（临时 PostgreSQL/NATS、备份往返、部署烟测；镜像 `ear-{storage,api,gateway}:ear-67-acceptance-fixes-20261107`）。11 个跳过：`test_causality_context_guard.py` 5 个、`test_llm_preflight.py` 6 个，原因均为需要认证 PostgreSQL。不是生产验收；不含真实浏览器↔真实①、真实模型、真实日历 |
| **静态检查·2026-11-07 最终**（`ear-67-static-final`，14:36） | 退出 0：`ruff check`、`ruff format --check`（169 files already formatted）、`pyright`（0 errors）、`uv lock --check`、`tools/check_docs.py` 与 `--base origin/dev`（均为 36 个 Markdown 文件通过）、`git diff --check`、Node UI 测试（37 项全部通过） | 只是静态检查与页面纯逻辑测试，不是运行验收；`check_docs` 只查结构与本地链接，不代表人工审定 |
| **全套测试·历史失败（保留）**（同样四个开关全开，12:35 日志） | **3 failed**，2058 passed，11 skipped，2 warnings，用时 14 分 39 秒 | 没有通过。2 个失败是部署烟测的运行脚本漏设 `EAR_STORAGE_TEST_IMAGE`；1 个是离线备份往返：旧 storage 镜像里 root 拥有的 `/app/runtime.py` 权限 0600，UID 10001 读不了，迁移退出。已按根因修复并先做了定向验证，随后由上面“2026-11-07 最终”行的全套（14:26，退出 0）取代；本行作为历史失败保留。11 个跳过都是需要认证 PostgreSQL 的 `memory` 参数化用例 |
| **静态检查·历史失败（保留）**（`ear-67-static-v2`，14:26） | 退出 1：`ruff check` 通过；`ruff format --check` 要求重排 `services/llm_gateway/README.md` 里的 Python 示例代码块（1 file would be reformatted，168 files already formatted）；链上其后各项当次未运行 | 仅格式问题。已按 ruff 格式重排该代码块（只改 Markdown），14:36 整条链重跑退出 0 |
| 真实 Chromium 153.0.8010.12 + 真实 `create_app` + 回环**假①** | 34 项通过，0 失败 | 假①是内存合成存储，一次性领取由夹具模拟；没有真实①管理处理器、PostgreSQL、NATS。失败重试说明：首轮因临时夹具缺 `expected_revision` 只改了 /tmp 脚本后重跑，首轮记录保留 |
| 合成容器（非 root 镜像、受保护密钥文件、静态页、健康检查、持久恢复） | 20 passed，1 warning，40.92 秒 | 合成数据，不是生产；没有付费模型、没有真实日历 |
| 独立复核（针对修复后的 ⑦ 网关、插件目录与预检） | 旧的 3 项必须修复均已关闭，范围内剩余 0 项 | 只覆盖该范围，不是全仓或生产验收；未跑 PostgreSQL/NATS/Docker/备份与真实 Octopus |

更详细的命令、数字与出处见[实现说明第 5 节](ai-docs/implementation.md)。

**第二批的来源记录（均为 2026-11-12，合成数据；作者未复跑，只做 `pytest --collect-only` 与文件哈希核对；工作区仍在变，不是当前结果）**：

- 04:14Z（api-place 摘要，基线 79e8dc3 加当时工作区）：`create_place` 定向 177 passed（161 项纯边界加 16 项真实回环 HTTP 与临时 PostgreSQL，其中 4 项经真实 Relay/NATS 订阅与真实 ③ 服务，118.12 s）；`services/api/tests` 372 passed；浏览器纯逻辑 37 passed；该报告里的 4 个文件哈希与当前一致。真实回环覆盖：⑥ 的 `create_place` → ① HTTP/PG → Outbox/Relay/NATS → ③ → ⑥ 的成功投影；所选 GPS 的顺序与已有归属 ID 保留、旧参考移除、目标参考与完整依赖字段、用户保护检查；同一请求重放与重复投递不产生第二个地点或回执，同一请求 ID 下改变 GPS 顺序被拒；受保护的选择与旧的 `user_protected` 缺失或为 null 被原子拒绝；没有 worker 订阅时保持 `pending`，首次订阅不会静默消费旧历史（回放测试里曾因 `from_beginning=True` 被存储正确拒绝，改为只对新反馈）；读授权收窄、无权写回执、原始被领取回执、畸形所有方回执、结果读授权被撤销。
- 04:20Z（action-protocol 摘要）：新协议测试 84 passed（⑥ 与 ④ 各一个文件，内存存储与私有 SQLite）；API 加 ④ 共 533 passed。该报告当时 HTTP 未接，已被下面 04:42Z 与 06:37Z 的记录取代，只作历史。
- 04:42Z（action-http 摘要）：HTTP 接线。⑥ 与 ④ 全部测试 589 passed（55.00 s）；真实 `test_action_protocol_runtime.py` 加 `test_extraction_v12_runtime.py` 9 passed（187.66 s，PG+NATS）；全仓静态退出 0。覆盖：`prepare`、`pump` 配真实的所有方批准、开始与结果状态流转；已受理的反馈和排队的 run 不授权副作用；GET 在所有方确认前后都只读；合成适配器 `execute` 内断言 journal 里已有所有方确认与持久意图；畸形、超大、重复键的 JSON 与禁止字段；两个版本号的并发检查，并行 HTTP pump 只有一个推进、只有一次效果；`unknown` 不自动重试，查证明确缺失仍是 `unknown`；查证成功、用户声明、风险重试三条路径；反馈受理回执丢失后同一稳定反馈在 API 重启后重放；效果之后 journal 写失败加 API 生命周期重开后只查证；真实的无授权、授权被撤、令牌撤销与查证期间撤销；命令行从不注入适配器。
- 06:37Z（action-evidence-fix 摘要）：适配器证据校验失败关闭的修复（见第 3.6 节）。API 全量加 `services/extraction/tests/test_execution_protocol.py` **649 passed**（49.38 s，无 PG/NATS）；`test_action_protocol_runtime.py` **24 passed**（347.77 s，PG+NATS）；原先复现的 3 个回归用例红转绿；`ruff`、`ruff format --check`、`pyright`、`uv lock --check --offline`、`check_docs` 与 `--base origin/dev`、`git diff --check`、Node UI 37 项均退出 0。这是动作协议最新的代码；它与 04:42Z 的 589、9 是不同时间的快照与范围，新增回归与总数重叠，**不相加**。
- 06:19Z（eight-steps 摘要）：⑥ 的动作路径是 2 个合成八步用例的一部分：真实 API 的审核、`prepare`、`pump`，经真实的 ④ consumer 推进到 `executed`，每个用例恰好 1 次 `execute`；`lookup-recovery` 用例里适配器效果落地后超时，状态为 `unknown`，重开 API 与 journal 后经 HTTP `verify` 查证、④ 应用为 `executed`。平台是认证的回环 HTTP 加临时 PostgreSQL/NATS 与独立 worker 进程，不是 Compose；这 2 项在 06:24Z 的证据修复**之前**运行，没有在修复之后重跑。
- 作者 04:31Z 的 `--collect-only`：`services/api/tests` 385 项，`services/storage/tests/test_api_attribution_runtime.py` 177 项（只收集）。
- 无开关（外部开关全关）的整仓快照里 PG/NATS 测试被跳过，不是真实服务证据。新的四开关全量验收由主 agent 委派，运行中，**结果未出，不预填**，补入[交接第 7 节](../../ai-docs/plans/mvp-runtime-handoff.md)。

## 5. 未完成与待跨模块契约（历史记录与当前边界）

下面真实浏览器联调欠项指当时的插件管理/处理链路；2026-11-18新增的真实空库只读workspace浏览已通过，不等于管理页、⑤处理或真实数据链路全部验证。

- **外部动作执行/确认/风险重试经 HTTP（默认与命令行服务上仍是 501）**：action-v2 协议和可选的 HTTP 接线已在工作区，见第 3.6 节；它只在宿主显式启用并注入可信适配器时可用，仓库里没有真实适配器、真实 provider 和执行 worker。该协议是模块内的附加 payload，**没有冻结为公共契约**。没有轮询 `extraction_run` 拒绝原因，没有通用的外部恰好一次，没有强杀、断电或多主机验证。
- **③⑤ 反馈契约**：`attribution` 命令的结果投影按 ③ 现有回执的形状在 ⑥ 的边界上校验，在共享契约发布之前这是临时做法，内容仍需 ③ 负责人与 ① 负责人、I 一起定；`dependency` 命令（`reevaluate_related`）的结构化内容与处理反馈尚无契约，也没有消费方；契约只增不删。
- **外部标识保存位置**、插件建议如何进入核心审核与执行、反馈回哪个所属方：待契约（设计第 14 节待同步项）。
- 未做：`reattribute_range`；真实日历/提醒事项适配器、执行 worker 的接线、真实浏览器页面与真实①、⑤调度的联调（API 层对真实①的 26 项集成测试已通过，浏览器 34 项只对假①，均为 2026-11-07 的记录）、生产部署。2026-11-07 的全套验收属合成/隔离环境（见第 4 节），不含上述联调项，也不覆盖第二批。

## 6. 谁来决定什么

- **普通工程选择，由实施方决定并在 PR 描述里列明，按普通代码评审由审核者审阅**：管理员认证（部署配置里的令牌绑定，①逐次复核可信管理员角色）、执行日志的单 worker 私有本地文件模型、静态页与 CSP 细节、接口分页和限额。这是仓库规则（根 README 第 5 节），不是用户已经给出的“无需批准”，本文也不替用户批准其中任何一项。
- **仍需对应负责人确认的跨模块契约**：第 5 节前三项。
- **设计文档已定的产品语义，不属于待批事项**：外部动作须“建议→用户审核→执行”；历史范围补处理须用户显式选择；不改 accepted 的语义。
- 代码依赖事实（供评审知悉，不改变任何语义）：`ear-api` 依赖 `ear-extraction`（复用 ④ 的 `Command`、`Action`、`Interval`）和 `ear-storage`（复用 ① 的 `ManagementInput`）做输入校验，④ 或 ① 修改这些模型时 ⑥ 的校验会随之变化。
