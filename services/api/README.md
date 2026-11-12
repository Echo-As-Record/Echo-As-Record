---
audience: human
doc-type: reference
based-on: 设计 v1.2、信号补充、插件 MVP、工作分解 v1.2；origin/dev 376294d 的 contracts 与①代码；api/gateway-mvp 功能分支（待合并）中 services/api 的实际源码（config、cli、app、boundary、business、models、upstream、execution、execution_journal、static）与 deploy 覆盖文件；2026-11-07 本机取证记录（均在 /tmp，不属于仓库）：ear-67-browser-state.json、ear-api-gateway-deploy-state.json、ear-67-postfix-review.json、ear-67-full-acceptance.log、ear-67-acceptance-fixes.json、ear-67-full-v2.log（及 .exit）、ear-67-static-v2.log、ear-67-static-final.log（及 .exit）；用户对⑥⑦实现与文档同步的授权；2026-11-12 增补依据：services/api 工作区未提交的源码（attribution_results.py、business.py、execution.py、execution_journal.py、app.py、config.py、execution_http.py，作者读源码与 git diff，不运行测试）、services/attribution 的 create_place.py 与 feedback.py、services/extraction 的 domain.py 与 suggestions.py（只读核对）、仓库外摘要 /tmp/ear-mvp-api-place.json 与 /tmp/ear-mvp-action-protocol.json（作者读取，其中的测试结果作者未复跑，只用 pytest --collect-only 与文件哈希核对）、git 记录（8750a83 即 #11 是 HEAD 79e8dc3 的祖先）；2026-11-12 action-v2 HTTP 与证据校验增补依据：execution_http.py、execution.py、app.py、config.py、cli.py 源码与仓库外摘要 /tmp/ear-mvp-{action-http,action-evidence-fix,eight-steps}.json（作者读取并核对各自的退出码文件、日志末行与 junit 用例名，测试结果未复跑）；本文不新增用户授权记录，也不增加用户决定；未填写或改动任何 reviewed 字段
---

# ⑥ 客户端与 API

用户与管理员进入系统的服务端入口：读取时间线、活动、Markdown 产物、人物、地点、规则和建议；把用户操作写成结构化反馈（`feedback`）；把管理员操作写成插件管理反馈（`plugin_management_feedback`）并查询①写出的管理结果；执行已批准的外部动作并回写反馈；提供轻量插件管理页面/API。⑥只写反馈，不做推断，不直接改③④的业务数据，也不写插件登记和权限（那是①的单写职责）。

- [设计 v1.2](../../docs/design/design-v1.2.md)（第 5 节反馈、第 7.6 节外部动作）、[模块间信号](../../docs/design/signals-supplement.md)（第 3.5、6.5、6.6 节）
- [动态插件 MVP](../../docs/design/plugins-mvp.md)（管理页与异步反馈）、[工作分解 v1.2](../../docs/design/work-breakdown-v1.2.md)（⑥一节，含验收标准）
- [实现说明与验证记录](ai-docs/implementation.md)；[全局实施计划](../../ai-docs/plans/mvp-implementation.md)；[①当前状态](../storage/ai-docs/progress.md)；[⑦大模型入口](../llm_gateway/README.md)

## 1. 状态

**部分交付，待审。这不是完整的 ⑥ MVP，也不是生产就绪。** 首批代码已在 dev 的历史中（git 记录里 8750a83，即 #11，是 HEAD 79e8dc3 的祖先；早先“功能分支 `api/gateway-mvp` 待合并”的说法已过时）。第二批在工作区，未提交、未提 PR、未部署。

- 已有（79e8dc3）：只读 API、结构化反馈入口、管理员的插件管理接口与轻量页面、插件目录、受保护配置与命令行、容器镜像与可选 Compose 覆盖文件、一个离线的外部动作执行/核对切片。
- 第二批（工作区，未提交）：（1）**③ 命令的结果投影，`create_place` 已接**：79e8dc3 对所有非 extraction 命令都只返回 `pending_contract`；现在 ⑥ 受理 `attribution` 命令后返回 `integration: attribution_result_query`，并把 ③ 写的 `attribution_feedback_result` 只读投影为反馈状态，支持集为 `correct_attribution`、`confirm_attribution`、`add_reference`、`remove_reference`、`rename`、`create_place`；范围重新归属 `reattribute_range` **未做**（见第 3.4 节）；（2）**外部动作 action-v2**（`ExecutionPump`）：与旧的离线切片不是一回事。协议与可选的 HTTP 接线都在工作区：HTTP 由 `Settings.execution_action_v2`（默认 `false`）加私有 journal 加宿主显式注入的可信适配器启用，**`ear-api serve` 不注入适配器，所以默认与命令行服务上仍是旧的 501**，仓库里没有真实适配器（见第 3.6 节）。
- 未完成：外部动作的执行、“确认已执行”和“风险重试”在默认配置与命令行服务上仍返回 501（action-v2 的 HTTP 只在宿主显式启用并注入适配器时可用，见第 3.6 节）；`dependency` 命令（`reevaluate_related`）只被记录（`integration: pending_contract`），没有消费方；`reattribute_range` 未做；没有接真实日历或提醒事项，没有执行 worker。
- 验证有限：真实浏览器检查用的是内存**假①**；容器检查是合成容器。**2026-11-07 的记录（取证于功能分支，不是 79e8dc3 或当前工作区的结果）**：修复后的最终全套（四个开关全开，`/tmp/ear-67-full-v2.log`）退出 0，2067 passed、11 skipped、2 warnings，用时 1001.65 秒；最终静态检查（`/tmp/ear-67-static-final.log`）退出 0。**历史失败保留**：12:35 全套为 3 failed，14:26 静态检查因 `ruff format` 失败，均已修复并重跑，见第 4 节。这些都是合成/隔离环境的结果，不是生产验收，也不改变上面的未完成项。第二批的验证来源与日期见第 4 节末尾。
- 没有生产启动，没有真实模型或真实日历参与。

## 2. 目标与边界（定义在设计文档，此处不重复）

| 能力 | 边界 | 当前 |
|---|---|---|
| 读取：时间线、活动、md 产物、人物、地点、规则、建议 | 经①读取并服从其授权；不做推断；Markdown 按纯文本展示 | 已实现（15 个业务类型），见第 3.4 节 |
| 用户反馈：纠正/确认、参考、命名、规则、手动提取、建议审核等 | 写 `feedback`（⑥单写）；由③④⑤按所属方执行；⑥只展示“处理中”与最终结果 | ④ 命令已接通；③ 的六种命令（含 `create_place`）经 `attribution_feedback_result` 投影结果（第二批，工作区）；`reattribute_range` 未做；⑤ 命令只记录，等契约 |
| 管理反馈与轻量插件管理页/API | ⑥验证管理员后写管理反馈，①异步处理并写结果；区分已受理/已生效、已启用/进程在线；凭据只经一次性领取交付 | 已实现；真实浏览器 34 项检查**只对内存假①**通过；API 层另有对真实回环①+隔离 PostgreSQL/NATS 的 26 项集成测试通过（管理处理步骤由测试显式驱动，合成 provider）；没有真实浏览器↔真实①的页面联调 |
| 外部动作执行（日历、提醒事项） | 只执行已批准且查过当前状态的建议；稳定动作标识；结果未知时停止自动重试并提示用户核对；结果写反馈，由④更新建议 | 只有离线切片；默认配置与命令行服务上 HTTP 仅“只读查证”可用（需代码注入可信适配器），执行/确认/风险重试 501。action-v2 在工作区有协议和可选的 HTTP 接线（默认关闭、宿主注入适配器），见第 3.6 节；无真实适配器 |
| 历史补处理与恢复选择 | 用户显式选择范围或补/跳，经管理反馈交①；读历史不等于批量生成授权 | 页面已实现；①侧执行与⑤调度未联调 |

不做：完整时间线/地图/移动端客户端、通用插件结果页、插件市场及自动安装启动。

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
- `static_dir`：默认使用随包的 `static/`（即 `/plugins/` 页面）；显式写 `null` 可关闭页面。
- `execution_journal`：外部动作的私有恢复日志路径；不设则外部动作状态显示 `disabled`。
- `execution_action_v2`（工作区，严格布尔，默认 `false`）：只有同时设了 `execution_journal`、且宿主在代码里把可信适配器传给 `create_app(settings, adapter=…)` 才启用 action-v2 的 HTTP；配置文件里写 `true` 并不会让 `ear-api serve` 获得适配器，推进类路径仍是 501。见第 3.6 节。
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

所有响应是 JSON，错误形如 `{"code": "..."}`，不含密钥、请求体或内部异常。认证：`Authorization: Bearer <API 令牌>`，只认请求头。

| 方法与路径 | 谁可用 | 说明 |
|---|---|---|
| `GET /health/live`、`GET /health/ready` | 无需认证 | 存活；就绪（范围仅为 ⑥ 到 ① 的连通） |
| `GET /v1/me` | 任一绑定 | 当前主体与角色（会先向①复核身份） |
| `GET /v1/records/{type}` | 任一绑定 | 分页列出（`limit`、`cursor`、`start`、`end`、`include_superseded`）；类型按下述核心登记表规则推导，集合外返回 404 `not_found` |
| `GET /v1/timeline` | 任一绑定 | 等同 activity 列表 |
| `GET /v1/records/{type}/{id}`（可加 `?version=`） | 任一绑定 | 单条 |
| `GET /v1/records/{type}/{id}/history` | 任一绑定 | 版本历史（`after_version`、`limit`） |
| `POST /v1/feedback` | 任一绑定 | 提交反馈，见下 |
| `GET /v1/feedback/{id}/status` | 任一绑定 | 反馈处理状态 |
| `POST /v1/previews/reextract`、`GET /v1/previews/person/{id}` | 任一绑定 | 只读预览，不写任何数据 |
| `GET /v1/actions/{suggestion_id}/execution`、`POST .../recovery` | 任一绑定（读权限由①判定） | 外部动作状态与恢复操作；默认走旧切片，启用 action-v2 后的行为见第 3.6 节 |
| `POST /v1/actions/{suggestion_id}/prepare`、`POST .../pump`（工作区） | 任一绑定（读权限由①判定） | 只在 action-v2 已启用时可用，否则（含命令行服务）501；见第 3.6 节 |
| `GET /v1/plugins` | 仅 admin | 插件目录（①只读元数据；`online` 恒为 `null`，不是“在线”） |
| `POST /v1/management/requests` | 仅 admin | 提交管理反馈，返回 202 与 `feedback_id`、`result_id`（已受理 ≠ 已生效）；请求体由①的 `ManagementInput` 校验 |
| `POST /v1/management/credentials/{feedback_id}/claim` | 仅 admin | 一次性领取凭据，空请求体；第二次被拒 |
| `GET /v1/management/results`、`/results/{id}`、`/history/jobs/{id}` | 仅 admin | 管理结果与历史补处理任务状态 |
| `/plugins/` | 页面本身无需认证 | 插件管理页；必须带结尾斜杠，`/plugins` 返回 404 |

可浏览业务类型从 `ear_contracts.DEFAULT_REGISTRY`（随代码发布的核心登记表，也是 `MemoryStorage` 的默认登记表）推导：仅纳入 `kind` 为 `raw_record`、`semantic`、`rule` 的核心类型，并排除 `plugin_management_feedback`。登记表暂无管理类型标志，⑥ 用 `MANAGEMENT_TYPES` 显式排除该类型，因为插件管理反馈走管理接口；所有 `system_record` 都不纳入。当前结果与原有 15 类完全相同，列表、单条及历史接口共用此集合，权限仍由①检查。

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
  - **`attribution` 命令（第二批，工作区）**：受理时返回 `integration: "attribution_result_query"`，业务状态恒为 `pending`；它只表示 ⑥ 有结果投影能力，**不表示 ③ 的 worker 在线或已经部署上线**。状态查询读 ③ 写的 `attribution_feedback_result`（可信 core 单写类型）：没有回执仍是 `pending`；受理（`MarkProcessed`、原始反馈被领取）不等于成功；成功或拒绝来自该回执，拒绝只回显白名单内的错误码，其余统一为 `attribution_rejected`；回执畸形或有歧义返回 503，没有该类型的读授权返回 403（授权被撤销后也是，旧登记表上的受理与重放仍可用）。⑥ 不自授权，不改部署。
  - **`create_place`**：请求体为 `name`（1–256 字符）与 `gps_ids`（非空、至多 100 个、无重复、UUIDv7）；③ 在一个受围栏的批次里新建地点，并把这些 GPS 点归属到它（沿用已有归属对象的 ID，移除旧参考，新版本带完整依赖字段），`expected_version` 须为空，受保护的归属拒绝。成功回执的元数据只含 `new_target_id`、`subject_ids`、`attribution_ids`，三者各 1–100 个唯一 UUIDv7、等长，且 `subject_ids` 与存储的 `gps_ids` 顺序一致；不透传名字、提示词或令牌。
  - **`reattribute_range` 未做**：⑥ 仍按请求模型受理，但不在投影支持集内；③ 写 `unsupported_command` 拒绝回执，⑥ 不把它说成成功。
  - 历史请求信封不变：受理写入的原始反馈里 `integration` 仍是 `pending_contract`（为了不改变请求指纹与已受理请求的重放），对外的 `attribution_result_query` 是响应与状态里的字段；真实 PostgreSQL 的重放测试把实际批次信封与手写的 79e8dc3 信封逐一对比过。
  - **`dependency` 命令**：写入后返回 `integration: "pending_contract"`，状态一直是 `pending`——**没有任何模块消费它**。
- 状态里的 `status_scope` 固定为“反馈处理，不是外部执行”；受理或入队不是业务完成。

### 3.5 插件管理页

`/plugins/` 提供：令牌只放内存、普通用户不发任何管理请求；已受理 ≠ 已生效，已启用 ≠ 进程在线（在线状态始终显示“未知”）；凭据领取需手动确认且同一反馈只尝试一次；历史补处理范围无默认值，按 UTC 半开区间提交；恢复选择（补/跳）必须明确并带修订号；状态轮询有次数、并发和连续失败上限。CSP 为 `script-src 'self'; style-src 'self'`，无内联脚本和外部资源。纯逻辑测试：在 `services/api/` 下运行 `node --test tests/ui/core.test.js tests/ui/page.test.js`（Node 24 不接受目录参数）。

### 3.6 外部动作：旧离线切片与可选的 action-v2（协议加 HTTP）

**默认（未启用 action-v2）是旧的离线切片，HTTP 上仍是 501**（79e8dc3 的行为；作者读 `app.py`、`cli.py`，默认配置与命令行服务仍如此）：

- `GET /v1/actions/{id}/execution` 读建议的当前状态与本地日志状态；`execution_enabled` 恒为 `false`。
- `POST /v1/actions/{id}/recovery` 的 `operation` 为 `verify`、`confirm_executed`、`retry`：只有 `verify` 且部署代码注入了可信适配器和日志时才会执行**只读查证**（从不调用 `execute`）。`retry` 不带 `acknowledge_duplicate_risk` 得到 409；其余情形一律 501（`extraction_execution_recovery_protocol_pending`）。
- `ear-api serve` 不注入适配器，所以命令行起的服务上 `verify` 也是 501。真正执行动作的 worker、真实日历适配器都没有接线。
- 离线切片内部：先写本地日志意图再调用适配器；结果未知或崩溃后只查证、不重发；日志必须保留，不可为恢复而清空。细节见[实现说明](ai-docs/implementation.md)。

**action-v2 协议（工作区，未提交）**：`ear_api.execution.ExecutionPump` 与 ④ 侧的状态流转（[④ README](../extraction/README.md)）配套，是模块内的附加 payload，没有改公共契约。HTTP 接线见下一段。

- 流程：审核仍走 `POST /v1/feedback`，等到真正 `approved`（“已受理”不算）；`prepare(suggestion_id, expected_version)` 冻结动作并暂存意图与 start 反馈；④ 的 consumer 把建议改为 `executing`；`pump` 观察到这个确切的版本、状态与回执后，先把意图持久化进私有 journal，再查证、至多执行一次适配器；适配器结果经反馈回 ④，`pump` 在确切的所有方状态下才把投递标为已应用。`unknown` 不自动重试、不换标识；`reconcile` 只查证，查不到也保持 `unknown`；`confirm_executed` 需 `acknowledge_executed=True`，来源记为用户、不接受外部标识；`retry` 需 `acknowledge_duplicate_risk=True` 并带两个版本号，沿用同一 `action_key`、attempt 加一，并重做一次所有方握手。
- 适配器必须由宿主显式注入，`client.module == "api"`，声明可靠的幂等，稳定命名，返回绑定同一 `action_key` 与哈希的类型化结果。**仓库里没有真实适配器。**适配器的返回值逐字段严格校验（对实例字段做 `ExternalResult.model_validate(…, strict=True)`）：非 `ExternalResult`、带额外字段、枚举或字段类型非法、键或哈希不符、`succeeded` 缺真实外部标识，以及借 `model_construct`、`model_copy(update=…)` 绕过校验得到的实例，加上任何异常，都记为 `unknown`，不当作成功或确定失败（审查项 EAR-ACTION-REVIEW-001，已修，2026-11-12T06:37Z 复测）；只有明确的 `not_executed`，或带 `retry_acknowledged` 的规范化 `unknown` 且已通过确切的所有方握手，才允许执行。
- journal 是单所有者的私有 SQLite（0600，生命周期独占文件锁，`synchronous=EXTRA`）：存完整的待发 Command、稳定反馈身份与所有方确认；旧 JSON 行不改写地加载；更新按旧字节做 CAS；动作键、哈希、批准版本与适配器不可变；未知时绝不清空或重置。空的替换 journal 不能认领已 `executing` 的建议。
- 与旧切片的关系：旧的 `ExecutionService` 拒绝 v2 的 journal 与建议状态，`ExecutionPump` 拒绝旧行，**不要把旧的 `process()` 接到外部 worker**。
- 单所有者、仅限本地文件系统；没有通用的外部恰好一次承诺；没有后台 worker（HTTP 回合是显式、有界的），没有对 `extraction_run` 拒绝原因的轮询。

**action-v2 的 HTTP 接线（可选，默认关闭；工作区）**：启用要同时满足三点：`Settings.execution_action_v2=true`（默认 `false`）、已设的 `execution_journal`（私有绝对路径，不自动创建，重启间保留）、宿主在代码里调用 `create_app(settings, adapter=<可信适配器>)`。缺一则不启用，推进类路径仍 501。`ear-api serve` 只调 `create_app(settings)`，**不注入适配器，命令行服务不会进入 v2**；不起后台线程、没有自动执行 worker，操作者或客户端显式调用，并另行运行真实的 ④ consumer 推进状态。

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

## 5. 未完成与待跨模块契约

- **外部动作执行/确认/风险重试经 HTTP（默认与命令行服务上仍是 501）**：action-v2 协议和可选的 HTTP 接线已在工作区，见第 3.6 节；它只在宿主显式启用并注入可信适配器时可用，仓库里没有真实适配器、真实 provider 和执行 worker。该协议是模块内的附加 payload，**没有冻结为公共契约**。没有轮询 `extraction_run` 拒绝原因，没有通用的外部恰好一次，没有强杀、断电或多主机验证。
- **③⑤ 反馈契约**：`attribution` 命令的结果投影按 ③ 现有回执的形状在 ⑥ 的边界上校验，在共享契约发布之前这是临时做法，内容仍需 ③ 负责人与 ① 负责人、I 一起定；`dependency` 命令（`reevaluate_related`）的结构化内容与处理反馈尚无契约，也没有消费方；契约只增不删。
- **外部标识保存位置**、插件建议如何进入核心审核与执行、反馈回哪个所属方：待契约（设计第 14 节待同步项）。
- 未做：`reattribute_range`；真实日历/提醒事项适配器、执行 worker 的接线、真实浏览器页面与真实①、⑤调度的联调（API 层对真实①的 26 项集成测试已通过，浏览器 34 项只对假①，均为 2026-11-07 的记录）、生产部署。2026-11-07 的全套验收属合成/隔离环境（见第 4 节），不含上述联调项，也不覆盖第二批。

## 6. 谁来决定什么

- **普通工程选择，由实施方决定并在 PR 描述里列明，按普通代码评审由审核者审阅**：管理员认证（部署配置里的令牌绑定，①逐次复核可信管理员角色）、执行日志的单 worker 私有本地文件模型、静态页与 CSP 细节、接口分页和限额。这是仓库规则（根 README 第 5 节），不是用户已经给出的“无需批准”，本文也不替用户批准其中任何一项。
- **仍需对应负责人确认的跨模块契约**：第 5 节前三项。
- **设计文档已定的产品语义，不属于待批事项**：外部动作须“建议→用户审核→执行”；历史范围补处理须用户显式选择；不改 accepted 的语义。
- 代码依赖事实（供评审知悉，不改变任何语义）：`ear-api` 依赖 `ear-extraction`（复用 ④ 的 `Command`、`Action`、`Interval`）和 `ear-storage`（复用 ① 的 `ManagementInput`）做输入校验，④ 或 ① 修改这些模型时 ⑥ 的校验会随之变化。
