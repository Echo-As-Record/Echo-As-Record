---
audience: ai
doc-type: guide
based-on: 设计 v1.2 第 9 节；信号补充；插件 MVP；工作分解 v1.2；origin/dev 376294d 的 llm_call 契约、ear_contracts.calls 与①代码；api/gateway-mvp 功能分支（待合并）中 services/llm_gateway 的实际源码与测试（逐文件阅读）、①侧 preflight.py/remote.py/service.py/http.py 在该分支上的改动、deploy/Dockerfile.gateway 与 compose 覆盖文件；Octopus 适配器固定的源码提交 b3c7cd5dd950a79e94533b114619082a542eac66（源码在维护者本机 /home/aytony/octopus，不在本仓库）；2026-11-07 本机取证记录（/tmp，非仓库文件）：ear-67-postfix-review.json、ear-67-final-fixes.json、ear-api-gateway-deploy-state.json、ear-67-full-acceptance.log、ear-67-acceptance-fixes.json、ear-67-full-v2.log（及 .exit）、ear-67-static-v2.log、ear-67-static-final.log（及 .exit）；用户对⑥⑦实现与文档同步的授权
---

# ⑦ 实现说明与验证记录（api/gateway-mvp）

## 0. 状态与使用

- **部分交付，待审，功能分支待合并。** 本页描述功能分支 `api/gateway-mvp` 上已有的 ⑦ 代码和取证到的验证结果；不是完成报告，不得据此宣称 ⑦ 是完整 MVP。面向人的用法见[⑦入口](../README.md)。
- 基线 dev 376294d；`services/llm_gateway/` 全部文件、①侧的预检/占用检查/插件目录改动、`deploy/Dockerfile.gateway`、compose 覆盖文件都在功能分支 `api/gateway-mvp`，待合并，尚未合入 dev。根 `pyproject.toml` 的 workspace 现已包含 `services/llm_gateway`（旧版“不含”的说法已过期）。
- **验证方式是源码核对 + 合成回放，没有真实模型调用，没有对运行中 Octopus 的 wire 验证。** 不是 exactly-once。journal 是单 worker 私有本地文件。Octopus 的 usage 是合成估算值，记录里记为未知。
- 取证文件在 `/tmp`，不属于仓库；本页把数字和范围抄录在第 6 节并标明出处。重新运行的新结果优先。**当前验收见第 6 节**：修复后的最终全套（`/tmp/ear-67-full-v2.log`）退出 0，2067 passed、11 skipped、2 warnings，用时 1001.65 秒；最终静态检查（`/tmp/ear-67-static-final.log`）退出 0；12:35 的 3 failed 与 14:26 的静态检查失败作为历史失败保留。
- 不改 accepted 的语义；不碰③④分支；契约增补须①负责人与 I 审核（只增不删）。

## 1. 代码导航（`services/llm_gateway/src/ear_llm_gateway/`）

| 文件 | 内容 |
|---|---|
| `models.py` | `CallRequest`（封闭模型；构造时用 `LLMCallRecord` 复核起因字段）、`ProviderResult(output, usage)`、`ProviderFailure(safe_to_retry)`、`Provider` 与 `RequestVerifier` 协议、`GatewayError(status, code)` |
| `service.py` | `Gateway`（`complete`、`reconcile`、`stats`、`recover_batch`、`recover_pending`）、`RecoveryBatch(processed, saved, quarantined)`；构造参数默认：`global_rpm=60`、`caller_rpm=20`、`concurrency=4`、`attempts=2`（1–3）、`timeout=60`（≤120 秒）、`max_response_bytes=1 MiB`、`body_policy` 缺省为关闭、`verifier` 缺省用①SDK 预检 |
| `journal.py` | `Journal`：SQLite（WAL）单 owner 持久栅栏；表 `calls(id, module, phase, request_id, record)` 与 `buckets(name, at, count)`；`reserve`、`pending`、`get`、`pending_ids`、`saved`、`quarantine`、`quarantined_count`、`close` |
| `http.py` | `create_app(gateway, *, max_body_bytes=256 KiB, body_timeout=5)`：`BodyLimit` 中间件（413/415/408/400）、`GET /health/live`、`POST /v1/complete`、`POST /v1/calls/{id}/reconcile`、`GET /v1/stats`；无文档/OpenAPI；不回显请求体或异常 |
| `client.py` | `GatewayClient(base_url, token, *, model, system, timeout=90, max_response_bytes=2 MiB)`：`complete`（只返回 `output`）、`reconcile`、`stats`；不读代理环境、不跟随重定向、不自动重试；`endpoint()` 校验根地址 |
| `cli.py` | `ear-llm-gateway`：`--storage-url`、`--journal`、`--provider-factory`（默认 Octopus）、`--aliases`、`--host`、`--port`；启动先 `recover_batch` 直到无可处理项，再 `uvicorn workers=1` |
| `octopus.py` | `OctopusProvider`、`create_provider`（读 `EAR_OCTOPUS_ORIGIN`/`TOKEN`/`MODELS`/`SERVICE`）、`SOURCE_COMMIT` |
| `output_json.py` | `strict_json`、`encode`、`OutputSchema`（受限子集，见第 4 节） |

测试在 `services/llm_gateway/tests/`：`test_gateway.py`、`test_journal_hardening.py`、`test_octopus.py`、`test_final_fixes.py`。部署相关：`deploy/Dockerfile.gateway`、`deploy/compose.api-gateway.yaml`、`deploy/compose.gateway-octopus.yaml`、`deploy/compose.gateway-provider.yaml`、`deploy/api_gateway_runtime.py`（文件挂载的容器入口，不初始化身份或 schema），部署烟测 `tests/test_api_gateway_deployment.py`。

依赖：`ear-contracts`、`ear-storage-client`、`fastapi`、`uvicorn`、`httpx`（不依赖④、⑥或①服务包）。④现直接依赖此 workspace 包并注入 `GatewayClient(..., system=SYSTEM)`，保留自身 `ModelGateway` Protocol；接入限制见[④实现说明](../../extraction/ai-docs/implementation.md)。

## 2. `complete` 的执行顺序（以 `service.py` 为准）

1. 别名不在 `aliases` → 422 `unknown_model_alias`（不回退模型）。
2. `callers(token)` 用调用方令牌建①客户端（每次请求新建、用后关闭，不缓存）。
3. `_check`：①认证的 `module` 必须等于请求的 `module`（`caller_mismatch`）；带 `request_event_id` 时调用方客户端的 `preflight_llm_call(LLMCallPreflight(...))`（缺该能力 → 503 `request_preflight_unavailable`；①返回 `InvalidRequest` → 403 `invalid_call_context`）并取回规范化后的全部起因；若部署注入了 `verifier` 则改走它并由网关自行复核根/深度/全部继承起因；不带请求时 `depth` 必须为 0（`request_context_required`）；所有根必须是真实 `feedback`，起因版本必须是真实带类型的版本。
4. `OutputSchema(call.output_schema)` 编译失败 → 422 `invalid_schema`；provider 的 `admit`（Octopus 只做 `_prepare`：确定性的体积/配置检查）失败 → 422 `invalid_provider_request`。这两步在占用 id、配额和任何 HTTP 之前。
5. 本地 journal 已有该 `call_id` → 409 `duplicate_call_id`（不泄露他人记录）。
6. 写者客户端的 `call_id_occupied(call_id)`：缺该能力或结果不是字面 `bool` 或抛异常 → 503 `call_id_check_unavailable`（**绝不回退到可能被 ACL 隐藏的普通读取**）；`True` → 409。这是时间点检查，不是跨 provider 的原子预占，之后冲突的 id 走隔离。
7. 非阻塞获取并发槽，失败 → 429 `concurrency_limited`。
8. `_execute`：`journal.reserve`（id 唯一、全局/调用方每分钟窗口计数，超限 429 `rate_limited`；写入 `active`）→ 至多 `attempts` 次尝试，**每次尝试前重新 `_check`**（撤权 → 记录为 `caller_access_revoked`；预检不可用 → `caller_preflight_unavailable`）→ `provider.complete`：输出必须是对象、序列化后 ≤ `max_response_bytes`、`usage` 必须是 `LLMUsage` 或 `None`；`ProviderFailure(safe_to_retry=True)` 才重试，其余按 `provider_outcome_unknown` 停止，不重试。
9. 构造终态 `LLMCallRecord`（`success`/`failed`、耗时、用量；正文仅当部署策略开启且调用方在范围内，按字节截断）→ `journal.pending` → `_persist`（以稳定 request_id 经写者客户端 `PutSystemRecord` 写①）→ **释放结果前再 `_check` 一次**。失败返回 502 与记录里的 `error_code`；成功返回 `{"call_id","output","usage","record_saved":true}`。

写①失败：返回 503 `record_pending`，待写记录留在 journal，用**同一 request_id 与载荷**补写，不再调用模型。`reconcile` 只处理本模块自己的调用，补写后返回状态；回复不缓存（`response_recoverable: false`）。

## 3. journal 与恢复

- 一个服务拥有一个 journal：独占 `flock`，`workers=1`，副本数 1；路径必须绝对、不含 `..`，目录/文件须私有（拒绝符号链接、他人所有、权限过宽的目录/数据库/辅助文件）。丢失或替换它就丢失发送前去重；没有自动过期、删除、重放模型调用或回复缓存。
- 阶段：`active` →（终态记录构造后）`pending` → `saved`；永久冲突为 `quarantined`。崩溃后 `active` 变为“结果未知”的待写审计记录，**不重发**（真实子进程 `os._exit` 的私有 WAL 恢复测试覆盖）。
- SQLite 初始化后绑定 `-wal`/`-shm` 的设备号与 inode；活跃辅助文件缺失或被替换 → 拒绝（检查覆盖读取、BEGIN 前、COMMIT 前后），不自动修复；干净重开会重新绑定，不假设跨重启 inode 不变。
- `recover_batch(limit=100)` 返回 `RecoveryBatch(processed, saved, quarantined)`；`recover_pending` 兼容旧接口只返回 `saved`；CLI 按 `processed` 循环，空批终止，①暂不可用则中止启动。
- 隔离规则：①报告永久 `id_exists` 或内容不一致的 `llm_call` → 隔离；权限被拒/请求无效的待写记录，只有占用检查返回**字面 `True`** 才隔离，`False`、`None`、`1`、异常都保持 `pending` 并 503 `record_pending`。隔离保留完整原记录和 request_id，不计为已保存，需要运维调查，没有自动处置工具。
- journal 与①不是一个事务；限流窗口按墙钟，回拨不会重置配额，重启后限额预占仍在。

## 4. 输出 schema 子集（`output_json.py`）

只支持受限子集：无环的本地 `$defs` 引用、`anyOf`、对象/数组/标量约束；固定的 UUIDv7 模式和 `uuid`/`date-time` 格式（带日历校验；不接受闰秒）；调用方给的正则**从不编译或执行**；外部/递归引用、未知关键字/格式/模式一律在任何 HTTP 前失败（422 `invalid_schema`），不占用 id、配额或尝试。它不是完整 JSON Schema，也不替业务校验（如区间有序）负责。已有回归：未改动的 ④ `Segmentation`、`Selection`、`Generated` 的 `model_json_schema` 经 `Call → GatewayClient → HTTP → 合成 Octopus` 的有效/无效 UUID 与日期用例。

## 5. Octopus 适配器（`octopus.py`）

- 固定到 Octopus 源码提交 `b3c7cd5dd950a79e94533b114619082a542eac66`（读过的文件：`octopus/main.py`、`octopus/llm/{router,router_models,chat_service,llm}.py`、`octopus/router/remote_access/{middleware,auth_helpers}.py`、`octopus/llm/provider/{openai_provider,litellm_provider}.py`）。`SOURCE_COMMIT` 常量与 `test_octopus.py` 只断言这个字符串；**运行时不核对对端版本，也没有对运行中的 Octopus 验证**。
- 请求：`POST /api/v1/llm/chat/v1/chat/completions`；`Authorization` 是 Manager owner 访问令牌，不是上游模型 API key；`models` 把网关解析后的模型 ID 映射到 Octopus 的 provider **类型枚举**（线协议无法按名字选 provider）；不转发调用方头/URL/密钥；HTTP 仅允许回环地址，其余必须 HTTPS；`service` 形如 `owner/service`，只用于归属。
- 限额（构造参数）：响应 ≤ 1 MiB（上限 4 MiB）、请求 ≤ 256 KiB（上限 1 MiB）、`max_tokens` 4096（上限 32768）。
- **用量**：Manager 合成 usage（prompt 恒 0、completion 为估算）。适配器校验形状（非负整数；`prompt_tokens == 0` 且 `total_tokens == completion_tokens`，偏离即视为协议漂移），然后**丢弃**，返回 `usage=None`；`llm_call.usage` 因此为未知，`stats` 把这些调用计入各字段的 `unknown_calls`，不当实测用量。
- Manager 会剥掉上游的 finish/refusal 信息，我们的 JSON/schema 检查无法恢复这些信号；Manager 内部 SDK 重试与出口/代理配置不受本适配器控制。
- 自定义 provider：`--provider-factory` 或容器 JSON 的 `provider_factory`；`octopus` 与自定义工厂不能同时给。

## 6. 验证记录（2026-11-07 取证；数字抄录，出处标明）

运行命令：

```bash
uv run pytest services/llm_gateway/tests               # 默认不连外部服务，provider 为合成
uv run pytest libs/ear_storage_client/tests/test_plugin_directory.py services/storage/tests/test_llm_preflight.py
# 真实 PostgreSQL/NATS（隔离临时容器）：EAR_RUN_POSTGRES_TESTS=1 EAR_RUN_NATS_TESTS=1，规则见①接口说明第 7 节
```

| 记录 | 结果 | 范围与限制 |
|---|---|---|
| **当前**：全套四开关（`ear-67-full-v2.{sh,log,exit}`，14:09 起、14:26 结束） | **通过**，退出 0：2067 passed，11 skipped，2 warnings，1001.65 秒（0:16:41）；`uv run pytest -q -rs`，`timeout` 3000 秒 | 四个开关均为 1；镜像 `ear-{storage,api,gateway}:ear-67-acceptance-fixes-20261107`。11 个跳过：`test_causality_context_guard.py` 5 个、`test_llm_preflight.py` 6 个，原因均为需要认证 PostgreSQL。合成/隔离环境；网关测试没有真实模型调用，不是生产验收 |
| **当前**：静态检查（`ear-67-static-final.{sh,log,exit}`，14:36） | 退出 0：`ruff check .`、`ruff format --check .`（169 files already formatted）、`pyright`（0 errors）、`uv lock --check`、`tools/check_docs.py` 与 `--base origin/dev`、`git diff --check`、`node --test` | 只是静态检查 |
| **历史失败（保留）**：静态检查（`ear-67-static-v2.{log,exit}`，14:26） | 退出 1：`ruff format --check` 要求重排 `services/llm_gateway/README.md` 第 3.4 节的 Python 示例代码块（`1 file would be reformatted, 168 files already formatted`）；链上其后各项当次未运行 | 仅格式；已按 ruff 格式重排该代码块（只改 Markdown），14:36 整条链重跑退出 0 |
| **历史失败（保留）**：全套四开关（`ear-67-full-acceptance.log`，12:35） | **3 failed**，2058 passed，11 skipped，2 warnings，879.49 秒 | **未通过**。3 个失败都不在网关测试内：部署烟测 `[custom]`/`[builtin-octopus]` 漏设 `EAR_STORAGE_TEST_IMAGE`；`test_real_offline_roundtrip` 因旧 storage 镜像里 root 拥有、权限 0600 的 `/app/runtime.py` 对 UID 10001 不可读。11 个跳过均为需要认证 PostgreSQL 的 `memory` 参数化用例 |
| 失败的定向修复（`ear-67-acceptance-fixes.json`，13:53） | 部署目标 3 passed、回归 24 passed、静态检查退出 0、镜像源码校验无差异（storage 51、api 40、gateway 36） | 记录原文：全套“未在此重跑，由主会话重跑”——重跑结果见本表“当前”行（14:26，退出 0） |
| 独立复核（`ear-67-postfix-review.json`，12:24） | 旧 3 项必须修复均关闭，范围内剩余 0 项；定向 269 项：263 passed，6 skipped，23 deselected（网关 194、SDK 14、api 31、storage 24），另 20 个探针通过 | 外部服务关闭；不是全仓/生产验收；未跑 PostgreSQL/NATS/Docker/备份、真实 Octopus 或 provider |
| 无外部服务全套（`ear-67-final-fixes.json`，12:07 快照） | 2072 项：1447 passed，625 skipped，0 失败（网关 194） | 625 个跳过不是验收 |
| 隔离真实 PostgreSQL/NATS（同上，12:07） | 73 passed，6 skipped（`test_api_gateway_integration` 26；`test_llm_preflight` 47 passed/6 skipped） | 临时 postgres:17-alpine / nats:2.11-alpine 与合成 provider |
| 合成容器（`ear-api-gateway-deploy-state.json`） | 20 passed，1 warning，40.92 秒（自定义 provider 与内置 Octopus 适配器两种形态） | 合成数据；无付费模型；既有命名卷权限不自动修复。较早一轮曾因入口脚本 root:root 0600 与夹具缺 `/v1/llm/call-id-check` 启动失败，已在续作中修复 |

旧的 3 项必须修复及其关闭证据（`ear-67-postfix-review.json`）：

- **F1 输出 schema**：只允许固定 UUIDv7 模式与 `uuid`/`date-time` 格式，不编译调用方正则；认证与上下文检查之后、占用/配额/预占之前编译 schema。新增 4 个反例并断言占用检查未被调用、calls/buckets 为零。
- **F2 WAL/SHM 辅助文件身份**：4 个“移走/同内容替换”用例均 503 且 provider 调用为 0，6 个事务边界注入用例通过；checkpoint 与干净重开重新绑定通过。
- **F3 恢复批次**：`RecoveryBatch` 区分 processed/saved/quarantined；100 个冲突 + 1 个健康记录、纯 100 个隔离有界终止、批末或次批①不可用阻止服务，均通过且 provider 为 0；五态占用判定见第 3 节。

仍保留的限制（复核明确“不是新需求”）：占用检查不是跨 provider 的原子预占；本地单 journal 栅栏不等于 provider exactly-once；不要求支持任意 JSON Schema；不防恶意同 UID 的任意检查/使用竞态、卷丢失；“无剩余必须修复项”不代表全仓或生产验收。

**未验证**：真实模型/生产 wire；对端 Octopus 版本；多副本；限额/重试/超时的部署入口；③⑤和插件接入；④已采用 GatewayClient，仅以 MockTransport 验证客户端边界，恢复与重判关联未完成。

## 7. ①侧依赖（均在功能分支 `api/gateway-mvp`，待合并，不在 dev 376294d）

网关要求①提供：`POST /v1/llm/call-id-check`（仅 `llm_gateway`，全类别占用检查）与 `POST /v1/llm/preflight`（调用方令牌，点时检查，不创建租约、不触发管理 reconcile）。语义与签名见[①接口说明第 6 节](../../storage/ai-docs/interface.md)。网关在缺任一入口的旧①上 fail-closed。

## 8. 偏差与更正

- 旧版称入口缺失、后端“由并行会话编写”、journal“待评审”——均已过期；journal 的单 worker 私有模型是实现者决定的普通工程选择，按代码评审调整，**不需要用户批准**。
- 旧版把调用方鉴权机制列为“不得自行定义”：现状是沿用调用方自己的①令牌，同属普通工程选择。
- 旧版“记录写失败的补偿策略待决”：现状是 journal 待写重放 + 隔离，行为见第 3 节。

## 9. 待决（需要对应负责人确认，不含普通工程选择）

- 正文保留期与作用域（①也无 TTL）；Octopus 复用边界与生产对接版本；各业务模块接入网关的方式（会改变其行为的部分）；限额/重试/超时是否需要部署入口。
- 契约：对外调用接口目前只是本模块的本地契约，不声称等同 Octopus 线协议；若要写入 `contracts/`，须①负责人与 I 审核（只增不删）。

## 10. 不得做

不调用真实模型进入 CI；不把凭据、API key、未开启的正文写入记录或日志；不发事件；不改写已提交的 `llm_call`；不绕过①直接保存；不在占用检查缺失时回退普通读取；不自动重试结果未知的调用；不自动换 `call_id` 重发；不删除或重置 journal 来“恢复”；真实数据与凭据不进仓库。

## 11. 刷新时机

全套验收重跑后更新第 6 节；真实模型/生产 wire 验证后更新第 0、5 节；合并进 dev 后删除“功能分支待合并”的说法；同步[全局实施计划第 6 节](../../../ai-docs/plans/mvp-implementation.md)。
