---
audience: ai
doc-type: guide
based-on: 设计 v1.2；信号补充；插件 MVP；工作分解 v1.2；origin/dev 376294d 的 contracts、ear_storage_client 与 ear_storage 代码；services/api 的实际源码与测试（逐文件阅读；2026-11-07 取自功能分支 api/gateway-mvp，其后 #11 已合入 dev，2026-11-12 对照 HEAD 79e8dc3 与工作区）及 deploy/Dockerfile.api、deploy/compose.api-gateway.yaml；2026-11-07 本机取证记录（/tmp，非仓库文件）：ear-67-browser-state.json、ear-api-gateway-deploy-state.json、ear-67-postfix-review.json、ear-67-final-fixes.json、ear-67-full-acceptance.log、ear-67-acceptance-fixes.json、ear-67-full-v2.log（及 .exit）、ear-67-static-v2.log、ear-67-static-final.log（及 .exit）；用户对⑥⑦实现与文档同步的授权；2026-11-12 增补依据：execution_http.py、execution.py、app.py、config.py、cli.py 源码，仓库外摘要 /tmp/ear-mvp-{action-http,action-evidence-fix,eight-steps}.json（作者读取并核对各自的退出码文件、日志末行与 junit 用例名，测试结果未复跑），git 记录（8750a83 即 #11 是 HEAD 79e8dc3 的祖先）；本文不新增用户授权记录，也不增加用户决定
---

# ⑥ 实现说明与验证记录（api/gateway-mvp）

## 0. 状态与使用

- **部分交付，待审。** 首批代码在 dev 的历史里（8750a83，即 #11，是 HEAD 79e8dc3 的祖先；“功能分支待合并”的旧说法已过期）；第二批（action-v2 的 HTTP 接线、③ 命令结果投影等）在工作区，未提交、未提 PR、未部署，见第 3A 节。第 1–8 节的 2026-11-07 内容描述首批；本页不是完成报告，不得据此宣称 ⑥ 是完整 MVP。面向人的用法见[⑥入口](../README.md)。
- 基线 HEAD 79e8dc3（含 #11）；根 `pyproject.toml` 的 uv workspace 已包含 `services/api` 和 `services/llm_gateway`。2026-11-07 的取证文件取自功能分支 `api/gateway-mvp`，不是当前基线的结果。
- 取证文件在 `/tmp`，不属于仓库，可能丢失；本页把数字和范围抄录在第 5 节并标明出处。重新运行得到的新结果优先于本页。
- **当前验收（第 5 节）**：修复后的最终全套（四个开关全开，`/tmp/ear-67-full-v2.log`）退出 0，2067 passed、11 skipped、2 warnings，用时 1001.65 秒；最终静态检查（`/tmp/ear-67-static-final.log`）退出 0。**历史失败保留在第 5 节**：12:35 全套 3 failed，14:26 静态检查因 `ruff format` 失败；均已修复并重跑。仍是合成/隔离环境，不是生产验收。
- 不改 accepted 的语义；不碰③④分支；契约增补须①负责人与 I 审核（只增不删）。

## 1. 代码导航（`services/api/src/ear_api/`）

| 文件 | 内容 |
|---|---|
| `config.py` | `Binding`（subject、role=user/admin、`token`、`storage_token`，均 43 位 base64url）；`Settings`（storage_url、bindings、public_origin、static_dir 默认随包 `static/`、execution_journal、`execution_action_v2`（严格布尔，默认 false，第二批）、timeout、max_body_bytes、body_timeout）；`authenticate`（sha256 摘要 + `hmac.compare_digest` 遍历全部绑定）；令牌两两不同 |
| `cli.py` | `ear-api serve|check|health`；`load_settings` 只读受保护配置（`O_NOFOLLOW`、普通文件、无 group/other 权限、属主为当前用户、单链接、≤64 KiB、读前后元数据不变、重复键拒绝）；`serve` 固定 `workers=1`、`access_log=False`；失败只输出 `{"code":"api_startup_or_health_failed"}` |
| `app.py` | `create_app(settings, *, upstream, execution, adapter)`；全部路由；`redirect_slashes=False`；`settings.static_dir` 非空时挂载 `/plugins`；`execution_journal` 由 lifespan 拥有并关闭；`execution` 与 `adapter`/`execution_journal` 不能同时给（`ambiguous_execution_configuration`），`adapter` 必须配 `execution_journal`（`execution_journal_required`）；第二批：`settings.execution_action_v2` 且传入了 `adapter` 才启用 v2 的 HTTP（`app.state.execution_http_enabled`），`execution_action_v2` 与注入的 `execution` 同给、或把 `ExecutionPump` 当作 `execution` 传入都会失败（`action_v2_requires_explicit_adapter_and_journal`）；`ear-api serve` 只调 `create_app(settings)`，不注入适配器 |
| `boundary.py` | ASGI 中间件：请求体大小与截止时间、不含密钥的错误体（含框架层错误）、`Origin` 与 `public_origin` 校验（`cross_origin_denied`） |
| `upstream.py` | `Upstream`：每次使用前 `_verify`（配置存在 → 向①取当前身份 → 拒绝两个绑定解析到同一①主体）；`client(binding)` 返回用该绑定 `storage_token` 的 `RemoteStorageClient`；`plugin_directory`（仅 admin，只调 `list_plugin_directory`，不回退业务登记表）；`management`；`ready`；`GatewayError(status, code)` |
| `business.py` | `BUSINESS_TYPES`（从随代码发布的核心 `DEFAULT_REGISTRY` 推导，当前 15 类；筛选规则见[读取 API](../README.md#34-接口一览)）；`feedback_identity`（由主体与 `request_id` 确定性推出反馈 id 和时间）；`submit_feedback`；`feedback_status`；`preview_reextract`；`management_result` |
| `models.py` | `FeedbackInput`、`PendingCommand`（8 种 kind，本地记录约定，不声称③⑤消费）；`load_json_object`（有界、拒绝重复键和非有限数）、`bound_json` |
| `execution.py` | 离线 `ExecutionService`（`handle_event`、`process`、`verify`、`status`）、`ActionAdapter` 协议（`lookup`、`execute`，`reliable_idempotency`）、`ExternalResult`（`succeeded`/`not_executed`/`unknown`）、`PROTOCOL_BLOCKER = "extraction_execution_recovery_protocol_pending"`；第二批：`ExecutionPump`（v2 的 `prepare`、`pump`、`reconcile`、`confirm_executed`、`retry`、`recovery_request`，见第 3A 节）。**源码的模块 docstring 仍写“HTTP is NOT wired to it”，已与 `execution_http.py` 不符**（注释，未改） |
| `execution_http.py`（第二批） | action-v2 的 HTTP 辅助：`PrepareInput`、`PumpInput`、`VerifyInput`/`ConfirmInput`/`RetryInput`（`operation` 判别联合，封闭模型、严格整数与布尔）、`projection`（只读 GET 投影）、`pump_once`、`recover`；路由本身在 `app.py` |
| `attribution_results.py`（第二批） | ⑥ 对 ③ 命令的结果投影：`INTEGRATION = "attribution_result_query"`，`SUPPORTED` 为 `correct_attribution`、`confirm_attribution`、`add_reference`、`remove_reference`、`rename`、`create_place`；只读 ③ 写的 `attribution_feedback_result`，回执畸形或有歧义返回 503，读授权缺失 403 |
| `execution_journal.py` | `ExecutionJournal`：私有 SQLite 恢复事实，仅 Linux 本地文件系统，持有 OS 文件锁，不是④的权威业务状态 |
| `static/` | `index.html`、`app.js`、`core.js`（无 DOM 的纯逻辑）、`styles.css` |

测试在 `services/api/tests/`：`conftest.py`（回环 TCP 假存储）、`test_api.py`、`test_execution.py`、`test_hardening.py`；第二批新增 `test_execution_protocol.py`（`ExecutionPump` 与证据校验）、`test_execution_http_v2.py`（HTTP 接线）；`tests/ui/core.test.js`、`tests/ui/page.test.js`（Node 纯逻辑与假 DOM）。动作的真实运行测试在 `services/storage/tests/test_action_protocol_runtime.py`（需 `EAR_RUN_POSTGRES_TESTS=1` 与 `EAR_RUN_NATS_TESTS=1`，临时容器、回环 HTTP、真实 ④ consumer、合成适配器），④ 侧在 `services/extraction/tests/test_execution_protocol.py`；合成八步在仓库根 `tests/test_mvp_eight_steps_runtime.py`。部署烟测在仓库根 `tests/test_api_gateway_deployment.py`。

依赖（`pyproject.toml`，均为 workspace 源）：`ear-contracts`、`ear-storage-client`、`ear-storage`（取 `ManagementInput`）、`ear-extraction`（取 `Command`、`Action`、`Interval`）、`fastapi`、`httpx`、`uvicorn`。

## 2. 请求流程与不变量

1. 边界中间件限制体积与时长，统一错误体；`GET`/`POST` 参数只接受各路由声明的名称（`params`），超出即拒。
2. `authenticated(request, admin=False)`：Bearer 只从请求头取一份；无匹配 → 401 `unauthenticated`；`admin=True` 而绑定不是 admin → 403 `admin_required`。角色来自配置绑定，不来自请求体。
3. 用户读写走 `remote.client(binding)`：先 `_verify`，再用该绑定的 `storage_token` 建①客户端。⑥ 不缓存权限；撤权后下一次调用由①拒绝。管理接口另由①复核“模块为 api 且带可信管理员角色”。
4. 反馈：`submit_feedback` 写 `feedback` 原始记录（`AppendRaw`，`Context.feedback(fid)`，`request_id=fid`），结果数量或 id 不符 → 503 `acceptance_unknown_replay_identical_request`（用同一请求重放）。非 extraction 命令的 payload 带 `integration: "pending_contract"`。
5. 反馈状态：只在恰好一个 `extraction_run` 关联该根反馈时投影其状态；从不调用 `is_processed`（受理/入队不是业务完成）。
6. 管理：`ManagementInput` 校验后经 `Upstream.management` 转发到①；只回传 `status`、`feedback_id`、`result_id`；凭据只能一次性领取，页面同一反馈只尝试一次。
7. 外部动作：`GET .../execution` 在返回任何本地日志状态前先用当前调用方令牌读取建议（无权则 404）；`POST .../recovery` 见第 3 节。

## 3. 外部动作切片（离线）

- `ExecutionService.process`：对 `approved` 且 `active` 的建议，用 `(suggestion_id, version, action_hash)` 派生稳定的 `action_key` 和回执；**先把 `executing` 意图写入私有日志，再调用适配器**；进程在调用中死亡留下 `executing`，恢复时只 `lookup`，结果 `unknown` 或恢复期不再执行；建议版本、动作哈希或状态变化 → `delivery="blocked"`。
- `verify(suggestion_id, expected_revision)`：只对已有意图做查证，**永远不调用 `execute`**；日志修订号不符则拒绝。
- 终态反馈沿用④既有的终态反馈协议写回；④尚不能表示“执行中/未知/用户核对”。
- HTTP：`execution_enabled` 恒 `false`；`verification_enabled` 仅当 `create_app` 被代码注入了日志和可信适配器；`ear-api serve` 不注入，所以命令行服务上 `verify` 也走 501。`retry` 无 `acknowledge_duplicate_risk` → 409 `duplicate_risk_confirmation_required`；`confirm_executed`、带确认的 `retry`、无适配器的 `verify` → 501 `extraction_execution_recovery_protocol_pending`。
- 日志必须跨重启保留，不可为“恢复”清空或重置；没有租约授权重复执行；`workers=1` 与文件锁共同保证单 owner。这是实施方的普通工程选择，在 PR 描述里列明、按代码评审调整；这不是用户已给出的批准。
- 未做：真实适配器（日历、提醒事项）、worker 接线、外部标识保存位置的契约。④ 的执行/恢复协议（action-v2）已在工作区实现，见第 3A 节。

## 3A. action-v2：协议与 HTTP 接线（第二批，工作区，未提交；2026-11-12）

人读的路由、请求体与默认行为见[⑥入口第 3.6 节](../README.md)，这里只补代码导航与不变量；本节不新增产品规则。

- **启用条件在 `create_app`**：`v2_enabled = settings.execution_action_v2 and adapter is not None`。配置开关关闭时 `recovery` 走旧的 `RecoveryInput` 分支，`prepare`/`pump` 在读到建议之后返回 501 `PROTOCOL_BLOCKER`；开关打开而没有适配器时，`prepare`、`pump`、`recovery` 同样在读到建议之后返回 501，`GET .../execution` 仍返回旧投影（`execution_enabled: false`）。v2 已启用但 journal 缺失，返回 `JournalUnavailable`。`ear-api serve` 不注入适配器，命令行服务上永远不是 v2。
- **`ExecutionPump`**（`execution.py`，继承 `ExecutionService`）：`prepare(suggestion_id, expected_version)` 冻结动作并暂存 start 反馈；`_sync` 把待发反馈投递给所有方，并核对确切的所有方版本、状态、回执与动作哈希；`pump(suggestion_id)` 是**一个有界回合**：先 `_sync`，有待发命令、被阻塞或已是 `unknown`/`succeeded`/`failed` 就直接返回；`executing` 状态（只有崩溃才会残留）只 `lookup`；所有方是 `approved` 而 journal 还没暂存 start 时补一次 `start`；否则先 `lookup`，只有 `not_executed` 或带 `retry_acknowledged` 的规范化 `unknown` 才继续，随后**重新读取建议并核对所有方版本、状态、回执、动作**，再把 `executing` 意图写入 journal，最后才调用一次 `execute`。`reconcile`、`confirm_executed`、`retry` 都要两个版本号（`retry` 另要 `acknowledge_duplicate_risk`）；`process` 只是 `pump` 的别名，旧签名的 `verify` 一律 409 `execution_recovery_requires_both_versions`。旧的 `ExecutionService.process` 遇到 v2 的 journal 行或带 `execution` 的建议，抛 409 `execution_protocol_pump_required`。
- **证据校验**（`_evidence`，审查项 EAR-ACTION-REVIEW-001，已修）：适配器返回值必须是 `ExternalResult` 且 `__pydantic_extra__` 为空，再对 `dict(vars(value))` 做 `ExternalResult.model_validate(…, strict=True)`；这样 `model_construct`、`model_copy(update=…)` 造出的未校验实例、额外字段、错误类型都会被拒，而不是把实例或 `model_dump()` 当作已验证数据。键与哈希须等于 journal 条目，`succeeded` 须带外部标识；任何异常或校验失败都规范化成同一条目的 `unknown`。旧的 `ExecutionService` 与 v2 共用这段校验；`ExternalResult.outcome` 没有新增枚举值。
- **`execution_http.py`**：`projection` 在 journal 锁内先读建议（无权即 404）再披露本地状态，不查证、不写反馈；`pump_once` 在**同一把** journal 锁内核对 `expected_version`（409 `suggestion_version_changed`）与 `expected_revision`（409 `execution_revision_conflict`，journal 里没有条目则 409 `execution_not_started`）后调用 `service.pump`；`recover` 把 `verify`/`reconcile`/`confirm_executed`/`retry` 分发到 `ExecutionPump`。推进类路由返回 202 与新的 `projection`。
- **请求与授权**：体经既有的 `body(request)`（有界严格 JSON、重复键拒绝）与封闭的 Pydantic 模型；每个路由先 `authenticated`、再 `remote.client(binding)` 用**本次请求**的绑定读建议，没有读授权时不泄露存在性（404）；不使用宿主注入的服务客户端。
- **测试**：`services/api/tests/test_execution_protocol.py`（协议、证据绕过与回归）、`test_execution_http_v2.py`（路由、严格体、并发、授权）；`services/storage/tests/test_action_protocol_runtime.py`（真实 PostgreSQL/NATS、回环 HTTP、真实 ④ consumer，含真实的无授权、授权撤销与查证期间撤销）；证据修复的回归用例在协议与运行时两个文件里，两者与全量总数重叠。数字与时点见[⑥入口第 4 节](../README.md)与[交接第 6 节](../../../ai-docs/plans/mvp-runtime-handoff.md)。
- **边界**：只有合成适配器；`ear-api serve` 不接适配器；不起后台线程；journal 单所有者、本地文件系统；没有强杀、断电与多主机验证；没有 `extraction_run` 拒绝原因的轮询；没有真实日历、provider 或模型。

## 4. 实施切片状态（对照上一版计划的 A–F）

| 切片 | 状态 | 说明 |
|---|---|---|
| A 骨架与身份 | 已实现 | 包已入 workspace；FastAPI；绑定认证；不读 `.env`；不自报模块身份；不直连数据库 |
| B 读取 API | 已实现 | 15 个业务类型，经①读取，`consistency: "live"` |
| C 用户反馈 | 部分 | ④ 命令接通；③ 命令（含 `create_place`，不含 `reattribute_range`）经 `attribution_feedback_result` 投影结果（第二批，工作区）；⑤ 命令（`reevaluate_related`）只记录（`pending_contract`），等契约 |
| D 管理反馈与轻量页 | 已实现，联调有限 | 页面 + 管理接口；真实浏览器 34 项只对假①；API 层对真实①+隔离 PostgreSQL/NATS 另有 26 项集成测试通过；无真实浏览器↔真实①的页面联调 |
| E 外部动作执行 | 离线切片，默认与命令行服务上 HTTP 仍 501；action-v2 协议与可选 HTTP 在工作区（第 3A 节） | 无真实适配器；命令行不注入适配器；不是完成 |
| F 历史补处理/恢复选择 | 页面已实现 | 只转发用户显式选择；①侧执行与⑤调度未联调 |

## 5. 验证记录（2026-11-07 首批取证；数字抄录，出处标明。第二批的验证见[⑥入口第 4 节](../README.md)与交接，不在本节）

运行命令：

```bash
uv run pytest services/api/tests                            # 回环假存储
(cd services/api && node --test tests/ui/core.test.js tests/ui/page.test.js)   # Node 24，不接受目录参数
# 部署烟测：显式开启，镜像由调用方先构建；缺任一镜像变量会失败并指出缺哪个，不会静默跳过
EAR_RUN_API_GATEWAY_DEPLOY_TESTS=1 EAR_API_TEST_IMAGE=… EAR_GATEWAY_TEST_IMAGE=… EAR_STORAGE_TEST_IMAGE=… \
  uv run pytest tests/test_api_gateway_deployment.py
```

提交前检查沿用[全局实施计划第 5 节](../../../ai-docs/plans/mvp-implementation.md)。

| 记录 | 结果 | 范围与限制 |
|---|---|---|
| **当前**：全套四开关（`ear-67-full-v2.{sh,log,exit}`，14:09 起、14:26 结束） | **通过**，退出 0：2067 passed，11 skipped，2 warnings，1001.65 秒（0:16:41）；`uv run pytest -q -rs`，`timeout` 3000 秒 | `EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS`、`EAR_RUN_API_GATEWAY_DEPLOY_TESTS` 均为 1；镜像 `ear-{storage,api,gateway}:ear-67-acceptance-fixes-20261107`，Compose 用 `/tmp/ear-deploy-cli/docker-compose`。11 个跳过：`test_causality_context_guard.py` 5 个、`test_llm_preflight.py` 6 个，原因均为需要认证 PostgreSQL（`-rs` 汇总）。2 个 warning：starlette 的 httpx 弃用，和 `test_auth_is_header_only_and_no_admin_fallback` 里的逐请求 cookies 弃用。合成/隔离环境，不是生产验收；不含真实浏览器↔真实①、真实模型、真实日历 |
| **当前**：静态检查（`ear-67-static-final.{sh,log,exit}`，14:36） | 退出 0：`ruff check .`、`ruff format --check .`（169 files already formatted）、`pyright`（0 errors）、`uv lock --check`、`tools/check_docs.py`、`tools/check_docs.py --base origin/dev`（均为 36 个 Markdown 文件）、`git diff --check`、`node --test`（`core.test.js` + `page.test.js`：37 tests，37 pass，0 fail） | 只是静态检查与页面纯逻辑测试 |
| **历史失败（保留）**：全套四开关（`ear-67-full-acceptance.log`，12:35） | **3 failed**，2058 passed，11 skipped，2 warnings，879.49 秒 | **未通过**。失败：部署烟测 `[custom]`、`[builtin-octopus]` 因运行脚本漏设 `EAR_STORAGE_TEST_IMAGE` 在断言处失败；`test_real_offline_roundtrip` 因旧 storage 镜像里 root 拥有、权限 0600 的 `/app/runtime.py` 对 UID 10001 不可读，迁移退出、`init-identities` 未执行。11 个跳过均为需要认证 PostgreSQL 的 `memory` 参数化用例。2 个 warning 是 starlette 的 httpx 弃用和 `test_auth_is_header_only_and_no_admin_fallback` 里的逐请求 cookies 弃用 |
| **历史失败（保留）**：静态检查（`ear-67-static-v2.{log,exit}`，14:26） | 退出 1：`ruff check` 通过，`ruff format --check` 判定 `services/llm_gateway/README.md` 的 Python 示例代码块需要重排（`1 file would be reformatted, 168 files already formatted`）；链上其后各项当次未运行 | 仅格式；已按 ruff 格式重排该代码块（只改 Markdown），14:36 整条链重跑退出 0 |
| 失败的定向修复（`ear-67-acceptance-fixes.json`，13:53，phase=verified） | 修复前 4 failed/2 passed → 修复后部署目标 3 passed（140 秒），回归 24 passed，静态检查退出 0；镜像源码校验 storage 51、api 40、gateway 36 个文件无差异 | 修复：storage 的非密钥运行代码显式 `COPY --chmod=0444`，缺镜像变量的诊断指明变量名；未移除任何安全检查。记录原文：全套“未在此重跑，由主会话重跑四开关验收”——重跑结果见本表“当前”行（14:26，退出 0） |
| 无外部服务全套（`ear-67-final-fixes.json`，12:07 快照） | 2072 项：1447 passed，625 skipped，0 失败（网关 194、api 257、部署 17 passed/3 skipped） | 625 个跳过不是验收；该快照早于上两行 |
| 隔离真实 PostgreSQL/NATS（同上，12:07） | 79 项：73 passed，6 skipped（`test_api_gateway_integration` 26、`test_llm_preflight` 47 passed/6 skipped） | 临时 postgres:17-alpine / nats:2.11-alpine 夹具与合成 provider，无生产服务 |
| 修复后定向复核（`ear-67-postfix-review.json`，12:24） | 269 项：263 passed，6 skipped，23 deselected（网关 194、SDK 14、api 31、storage 24）；另有 20 个独立探针全部通过 | 外部服务关闭；结论：旧的 3 项必须修复均关闭，范围内 0 个未关闭 |
| 真实浏览器（`ear-67-browser-state.json`） | Chromium 153.0.8010.12 + 真实 `create_app` + 回环**假①**：34 项通过，0 失败 | 假①是内存合成存储，一次性领取由夹具模拟；无真实①管理处理器、PostgreSQL、NATS；截图只在登出后拍摄；首轮因临时夹具缺 `expected_revision` 只改 /tmp 脚本后重跑，首轮记录保留 |
| 合成容器（`ear-api-gateway-deploy-state.json`） | 20 passed，1 warning，40.92 秒（非 root、受保护密钥、静态页、健康检查、持久恢复） | 合成数据；既有命名卷权限不自动修复（拒绝启动）。该文件还保留了更早一轮的启动失败记录，已由后续续作修复：网关入口脚本曾为 root:root 0600、UID 10001 读不了；测试夹具曾漏实现 `/v1/llm/call-id-check` |

复核中确认的行为（有合成证据，出处 `ear-67-postfix-review.json`）：插件目录只对真实 `api`+管理员开放，投影不含 payload、原始扩展、令牌或凭据，不放宽业务读权限，`online` 恒 `null`；普通用户目录 403；撤管理员后同一对象下一次调用被拒。

**未验证**：真实①管理处理器与 PostgreSQL/NATS 下的浏览器端到端；真实日历/提醒事项；生产部署；多 worker（本服务设计即单 worker）。

本页不再引用旧版记录的“交叉验证 51 项/变异 14 个”等数字：它们属于更早一轮，本轮未重新核对。旧版的“JS 测试 37 项”已由 14:36 静态检查重新取得（37 tests，37 pass）。

## 6. 偏差与更正

- 旧版称 `services/api` 不在 workspace、入口缺失、后端“由并行会话编写”——均已过期并更正。
- 旧版把管理员认证、日志并发模型列为“待负责人决定”，按仓库规则它们是实施方的普通工程选择，在 PR 描述里列明、按代码评审调整；这不是用户已给出的批准，本文不替用户批准。
- `ear-api` 对 `ear-extraction`、`ear-storage` 的依赖是代码层耦合（复用模型做输入校验），不改变语义；评审时可议，按仓库规则不属于需要负责人决定的跨模块契约，本文不替用户批准。

## 7. 待跨模块契约（不含普通工程选择）

- ④ 的执行/恢复协议（执行中、未知、用户核对、确认已执行、风险重试）：action-v2 是 ④/⑥ 的模块内附加 payload，已在工作区实现并经合成验证，**没有冻结为公共契约**；要成为共享契约须经 ① 负责人与 I 审核。默认配置与命令行服务上目前仍是 501。
- `feedback` 中 ⑤ 命令（`reevaluate_related`）的结构化内容与处理反馈，以及 ③ 命令结果投影形状成为共享契约；审核/执行反馈类型；外部动作稳定标识的存放位置；插件建议如何进入核心审核与执行。契约增补须①负责人与 I 审核。

## 8. 不得做

不直接写④建议或任何非 `api` 类型；不用请求体里的 `admin` 之类字段授权；不信任自报模块名；不在日志、反馈或响应中暴露凭据；不把受理当生效；不把结果未知当失败自动重试；不为恢复而清空执行日志；不把 `pending_contract` 说成已被⑤消费（③ 命令有结果投影，但受理写入的信封里 `integration` 仍是 `pending_contract`）；真实数据与凭据不进仓库。

## 9. 刷新时机

全套验收重跑后更新第 5 节；真实适配器接入或 action-v2 的默认启用方式确定后更新第 3、3A、4 节并重新评估 501；新的四开关全量验收结果由主 agent 补入[交接第 7 节](../../../ai-docs/plans/mvp-runtime-handoff.md)；同步[全局实施计划第 6 节](../../../ai-docs/plans/mvp-implementation.md)。
