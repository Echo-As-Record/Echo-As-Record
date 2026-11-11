---
audience: human
doc-type: reference
based-on: 设计 v1.2 第 9 节、信号补充、插件 MVP、工作分解 v1.2；origin/dev 376294d 的 contracts 与①代码；api/gateway-mvp 功能分支（待合并）中 services/llm_gateway 的实际源码（models、service、journal、http、client、cli、octopus、output_json）与 deploy/Dockerfile.gateway、compose 覆盖文件、api_gateway_runtime.py；Octopus 适配器固定的源码提交 b3c7cd5dd950a79e94533b114619082a542eac66；2026-11-07 本机取证记录（/tmp，非仓库文件）：ear-67-postfix-review.json、ear-67-final-fixes.json、ear-api-gateway-deploy-state.json、ear-67-full-acceptance.log、ear-67-acceptance-fixes.json、ear-67-full-v2.log（及 .exit）、ear-67-static-v2.log、ear-67-static-final.log（及 .exit）；用户对⑥⑦实现与文档同步的授权
---

# ⑦ 大模型入口

所有模块和插件调用大模型的统一入口：集中换模型、限流和重试；每次调用必须带调用方模块和 `root_feedback_id`（重判调用另带请求 id），缺少调用方的调用被拒绝；⑦单写调用记录 `llm_call`，经①保存，不发事件。默认只保存 metadata（用途、用量、耗时、关联信息），提示词/回复正文只有部署方显式开启才保存，不影响业务模块保存自己的产物正文。设计上基于 Octopus 已有接入部分独立为 docker 服务，后端对接反代平台。

- [设计 v1.2](../../docs/design/design-v1.2.md)（第 9 节）、[模块间信号](../../docs/design/signals-supplement.md)、[动态插件 MVP](../../docs/design/plugins-mvp.md)（第 8 节）
- [工作分解 v1.2](../../docs/design/work-breakdown-v1.2.md)（⑦一节，含验收标准）
- [实现说明与验证记录](ai-docs/implementation.md)；[全局实施计划](../../ai-docs/plans/mvp-implementation.md)；[①当前状态](../storage/ai-docs/progress.md)；[①SDK 与预检接口](../storage/ai-docs/interface.md)；[⑥入口](../api/README.md)

## 1. 状态

**部分交付，待审，功能分支待合并。这不是完整的 ⑦ MVP，也不是生产就绪。**

- 已有：网关 HTTP 服务；用调用方自己的①令牌验证调用方身份；重判请求上下文的①预检；call_id 占用检查；单 worker 私有 journal（防重发、限流预占、崩溃恢复）；经①写 `llm_call` 终态记录；固定到 Octopus 源码提交 `b3c7cd5dd950a79e94533b114619082a542eac66` 的 Octopus 适配器；输出 schema 子集校验；Python 客户端；命令行与容器镜像、可选 Compose 覆盖文件。
- 验证方式：**源码核对 + 合成回放**（假传输层、假 provider、合成数据）。**没有真实模型调用，没有对运行中的 Octopus 做生产 wire 验证。**
- **不是 exactly-once**：journal 只是本地单进程的防重发栅栏，不保证 provider 侧恰好一次，也不是多副本去重。
- **用量未知**：Octopus Manager 返回的 usage 是合成值（prompt 恒 0、completion 为估算），适配器丢弃它，成功记录的用量记为“未知”，统计里计入 `unknown_calls`，绝不当作实测。
- **当前验收**：修复后的最终全套（四个外部开关全开，`/tmp/ear-67-full-v2.log`）退出 0，2067 passed、11 skipped、2 warnings，用时 1001.65 秒；最终静态检查（`/tmp/ear-67-static-final.log`）退出 0。**历史失败保留**（第 5 节）：12:35 全套 3 failed（原因在部署/备份测试环境，不在网关测试内）；14:26 静态检查因 `ruff format` 要求重排第 3.4 节的 Python 示例代码块而失败；均已修复并重跑。验证方式仍是源码核对 + 合成回放 + 隔离环境，没有真实模型调用，不是生产验收。
- 所有代码都在功能分支 `api/gateway-mvp`，**待合并**，基线 dev 376294d，尚未合入 dev。④已采用 GatewayClient（仅客户端接入，恢复与重判关联未完成）；③⑤和插件尚未接入网关；正文保留期与清理未做。

## 2. 边界

- 调用方身份以可信凭据验证：网关拿调用方在 `Authorization` 里的①令牌建①客户端，`module` 以①认证结果为准，请求体里的 `module` 必须与之相同，否则 403 `caller_mismatch`。网关自己写记录用另一把 `llm_gateway` 令牌。
- 默认不保存正文；开启由部署方决定，调用请求不能自行开启；不得把凭据或 API key 放进记录。命令行/容器没有开启正文保留的入口，所以经它们启动的网关从不保存提示词或回复正文。
- 不拥有业务产物（调用方自己保存）和传播记录（⑤）；不发事件。
- CI 使用录制/合成的回复，不调用真实模型。
- 待定：正文保留期与作用域；逐次尝试日志粒度只是实施建议，未批准为强制。
- Octopus 在其自己的仓库维护。⑦ 不导入 Octopus 代码，只按固定源码提交里读到的 Manager 聊天接口写适配器（路径 `/api/v1/llm/chat/v1/chat/completions`）；代码里的常量和测试只断言这个提交号字符串，**不会在运行时核对对端 Octopus 的版本**，生产对接时的版本核对未做。

## 3. 实际用法

### 3.1 前置条件

- ① 在线，且带两个新入口：`POST /v1/llm/call-id-check`（仅 `llm_gateway` 可用）和 `POST /v1/llm/preflight`（见[①接口说明](../storage/ai-docs/interface.md)）。**这两个入口目前只存在于本功能分支（待合并）的①改动里，不在 dev 376294d。** 旧版①缺其一时网关拒绝服务相应调用（503 `call_id_check_unavailable` / `request_preflight_unavailable`），不回退到普通读取。
- ① 已给 `llm_gateway` 签发写入凭据（43 位令牌），并已 `bootstrap` 出 `llm_call` 类型。
- 调用方模块各有自己的①令牌；调用方需要能读它声明的全部起因类型（预检会复核）。

### 3.2 启动

命令行（仓库根，`uv sync` 之后；bash，Linux）。整段放进子 shell：配置和令牌只存在于这个子 shell 和网关进程的环境里，不会留在你当前的 shell；不写死任何真实地址、令牌或模型；缺少的非机密配置会逐项询问（已 `export` 的变量直接使用），令牌用 `read -rs` 输入，不回显、不进 history：

```bash
(
  umask 077
  for v in EAR_STORAGE_URL EAR_OCTOPUS_ORIGIN EAR_OCTOPUS_SERVICE EAR_MODEL_ID EAR_PROVIDER_KIND EAR_GATEWAY_STATE_DIR; do
    if [ -z "${!v:-}" ]; then read -r -p "$v: " "$v"; fi
    [ -n "${!v}" ] || { echo "缺少 $v" >&2; exit 1; }
  done
  case "$EAR_GATEWAY_STATE_DIR/" in
    */../*) echo "EAR_GATEWAY_STATE_DIR 不得含 .." >&2; exit 1 ;;
    /*) ;;
    *) echo "EAR_GATEWAY_STATE_DIR 须为绝对路径" >&2; exit 1 ;;
  esac
  mkdir -p -- "$EAR_GATEWAY_STATE_DIR"
  [ "$(stat -c '%a %u' -- "$EAR_GATEWAY_STATE_DIR")" = "700 $(id -u)" ] \
    || { echo "状态目录须为当前用户的 0700 目录；本命令不会修改既有目录的权限" >&2; exit 1; }
  read -rs -p "llm_gateway 的①令牌（43 位，不回显）: " EAR_GATEWAY_STORAGE_TOKEN; echo
  read -rs -p "Octopus Manager owner 访问令牌（不是上游模型 API key，不回显）: " EAR_OCTOPUS_TOKEN; echo
  export EAR_MODEL_ID EAR_PROVIDER_KIND
  EAR_OCTOPUS_MODELS=$(uv run --frozen python -c 'import json, os; print(json.dumps({os.environ["EAR_MODEL_ID"]: os.environ["EAR_PROVIDER_KIND"]}))')
  EAR_GATEWAY_ALIASES=$(uv run --frozen python -c 'import json, os; print(json.dumps({"default": os.environ["EAR_MODEL_ID"]}))')
  export EAR_GATEWAY_STORAGE_TOKEN EAR_OCTOPUS_TOKEN EAR_OCTOPUS_ORIGIN EAR_OCTOPUS_SERVICE EAR_OCTOPUS_MODELS
  exec uv run --frozen ear-llm-gateway \
    --storage-url "$EAR_STORAGE_URL" \
    --journal "$EAR_GATEWAY_STATE_DIR/gateway.sqlite3" \
    --aliases "$EAR_GATEWAY_ALIASES" \
    --host 127.0.0.1 --port 8077
)
```

变量都由你设置，文档不提供默认值：

| 变量 | 含义 |
|---|---|
| `EAR_STORAGE_URL` | ①根地址，形如 `http(s)://主机[:端口]`，不带路径、查询或用户信息（`client.endpoint` 会拒绝） |
| `EAR_OCTOPUS_ORIGIN` | Octopus 地址；仅本机回环允许 http |
| `EAR_OCTOPUS_SERVICE` | `owner/service` 归属标识，不是认证 |
| `EAR_MODEL_ID` | Octopus 上的模型 ID；命令把它同时写进 `EAR_OCTOPUS_MODELS` 和别名 `default` |
| `EAR_PROVIDER_KIND` | provider 类型枚举，取值见 `ear_llm_gateway.octopus.create_provider` |
| `EAR_GATEWAY_STATE_DIR` | 网关专用状态目录（绝对路径、不含 `..`），journal 是其下的 `gateway.sqlite3`；命令只会新建 0700 目录，既有目录必须已是当前用户的 0700 |

- 末行 `--host 127.0.0.1 --port 8077` 是默认值，不是占位符，需要时直接改；绑定非回环地址前先自行评估网络暴露（网关自身不提供 TLS）。
- 命令只启动服务：先连①并回放待写审计，不调用模型；之后收到 `/v1/complete` 才会按你配置的 Octopus 与模型发起调用。
- `stat -c` 是 GNU 写法；macOS 把它换成 `stat -f '%Lp %u'`。同一用户可从 `/proc/<pid>/environ` 读到网关进程环境，不要把令牌写进命令行参数。

- `--aliases` 把调用方可用的模型别名映射到模型 ID；别名不在表里 → 422 `unknown_model_alias`；没有“模型回退”。
- `--journal` 必须是绝对路径且不含 `..`；其目录和文件须为当前用户私有，符号链接、他人所有或权限过宽会使启动失败。
- `--provider-factory pkg.module:factory` 可换成自己的 provider（默认 `ear_llm_gateway.octopus:create_provider`）。
- 启动时先回放未写入①的审计记录（每批 100 条，直到没有可处理项）；①不可用就不开始服务，不会悄悄积压缺失记录。固定单 worker。
- 失败时只输出一行固定提示，不回显配置、令牌或异常文本。

容器：`deploy/Dockerfile.gateway`；可选覆盖文件 `deploy/compose.api-gateway.yaml`（profile `api-gateway`）。覆盖文件把 ⑥ 和 ⑦ 一起声明，必须显式提供三份受保护文件（UID 10001、权限 0600）：`EAR_API_CONFIG_FILE`（⑥的配置，见[⑥入口](../api/README.md)）、`EAR_GATEWAY_CONFIG_FILE`（JSON：`storage_url`、`aliases`，以及内置适配器的 `octopus={origin, models, service}` 或自定义 `provider_factory`，两者不能同时给）、`EAR_GATEWAY_STORAGE_TOKEN_FILE`；用内置 Octopus 适配器时再叠加 `deploy/compose.gateway-octopus.yaml` 并提供 `EAR_OCTOPUS_TOKEN_FILE`；自定义 provider 代码目录用 `deploy/compose.gateway-provider.yaml` 的 `EAR_GATEWAY_PROVIDER_DIR`（只读绑定）。Octopus 令牌不能写进 JSON 或 Compose 环境变量。网关端口 8077 只在内部网络，默认不对外发布；副本数固定 1（journal 文件锁只保证单 owner，不是多副本故障转移）；健康检查只是存活探测（启动恢复完成后才监听），不代表 provider 或①当前可用。新建命名卷会复制 UID 10001/0700，既有卷权限不自动修复。覆盖文件不做迁移和身份初始化。

### 3.3 HTTP 接口

认证：`Authorization: Bearer <调用方的①令牌>`（43 位，只认请求头）。错误体形如 `{"code": "..."}`，不含请求体、凭据或异常文本。

| 方法与路径 | 说明 |
|---|---|
| `GET /health/live` | 存活 |
| `POST /v1/complete` | 发起一次逻辑调用，成功返回 `{"call_id","output","usage","record_saved":true}`；`usage` 未知时为 `null` |
| `POST /v1/calls/{call_id}/reconcile` | 只对本模块自己的调用：把尚未写入①的审计记录补写，返回 `{"call_id","record_saved","status","error_code","response_recoverable":false}`；回复不缓存，不能取回 |
| `GET /v1/stats?root_feedback_id=` | 本模块的调用数、失败数和各用量字段的 `known_tokens`/`unknown_calls`，按根反馈分组；合并调用对每个涉及的根各计一次 |

请求体（`CallRequest`，未知字段拒绝）：`call_id`（UUIDv7，**由调用方保存**）、`module`、`model`（别名，默认 `default`）、`purpose`（snake_case）、`root_feedback_id`、`depth`、可选 `request_event_id`、`additional_causes`、`run_id`、`system`、`data`（对象）、`output_schema`（见第 4 节）。重判调用必须带 `request_event_id`（指向真实已提交的请求）；不带时 `depth` 必须为 0（否则 403 `request_context_required`）。

主要状态码：401 未认证；403 `caller_mismatch`/`invalid_call_context`/`access_denied`；404 `call_not_found`；409 `duplicate_call_id`/`call_in_progress`/`record_conflict`；422 `unknown_model_alias`/`invalid_schema`/`invalid_provider_request`；429 `rate_limited`/`concurrency_limited`；502 provider 失败（`provider_rejected`、`provider_outcome_unknown`、`caller_access_revoked`、`caller_preflight_unavailable`）；503 `record_pending`/`call_id_check_unavailable`/`request_preflight_unavailable`/`gateway_unavailable`。

#### 鉴权与预检（以源码为准：网关 `http.py`/`service.py`，①的 `http.py`/`service.py`）

- **令牌**：只认恰好一个 `Authorization: Bearer <43 位 [A-Za-z0-9_-]>` 请求头。网关为每个请求用该令牌建一个①客户端，`module` 取自①对令牌的认证结果；请求体 `module` 与之不同 → 403 `caller_mismatch`。网关自己写 `llm_call`、查占用用的是另一把 `llm_gateway` 令牌。
- **带 `request_event_id` 的调用（重判）**：命令行/容器的组合下，网关用**调用方自己的令牌**调①的 `POST /v1/llm/preflight`；①要求 `caller_module` 等于认证出的模块，先复核全部声明起因的读权限，再披露请求细节。①判请求无效 → 403 `invalid_call_context`；SDK 没有该入口 → 503 `request_preflight_unavailable`。预检只是点时检查，不是授权票据。
- **不带 `request_event_id` 的调用**：不调预检；`depth` 必须为 0（否则 403 `request_context_required`）；网关用调用方令牌读非 `system` 的根（须存在且为 `feedback`）和每个 `additional_causes` 的版本（须存在且类型相符），否则 403 `invalid_call_context`。
- **call_id 占用**：每次新调用前，网关用 `llm_gateway` 令牌调 `POST /v1/llm/call-id-check`；①只允许 `llm_gateway` 调用，只查全局 id 是否已被任一类别占用，不读载荷。缺入口、调用失败或结果不是字面布尔 → 503 `call_id_check_unavailable`（不回退普通读取）；`True` → 409 `duplicate_call_id`。点时检查，不是原子预占。

### 3.4 Python 客户端

下面是完整可运行的合成示例（只含虚构数据，不含真实地址、令牌或模型）：

```python
"""合成示例：只含虚构数据。

运行前设置环境变量：
  EAR_GATEWAY_URL    网关根地址（http(s)://主机[:端口]，不带路径）
  EAR_CALLER_MODULE  你持有①令牌的模块名（必须与令牌所属模块一致）
  EAR_CALL_STORE     你自己的私有状态目录（绝对路径；不存在则以 0700 创建）
调用方的①令牌运行时用 getpass 输入（不回显，不进环境变量或 shell history）。
注意：运行即向该网关发一次真实请求；网关后面若是真实模型，就会产生一次真实调用。
"""

import getpass
import json
import os
from pathlib import Path

from ear_contracts.base import new_id
from ear_llm_gateway.client import GatewayClient
from ear_llm_gateway.models import GatewayError

STORE = Path(os.environ["EAR_CALL_STORE"])
STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
info = STORE.stat()
if info.st_uid != os.getuid() or info.st_mode & 0o077:
    raise SystemExit("EAR_CALL_STORE 必须是当前用户私有目录（0700）")


def save(name: str, value: dict) -> None:
    """原子写入：临时文件 + fsync + os.replace + fsync 目录。"""
    tmp = STORE / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STORE / f"{name}.json")
    dir_fd = os.open(STORE, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def build_request(call_id: str) -> dict:
    return {
        "call_id": call_id,
        "module": os.environ["EAR_CALLER_MODULE"],
        "model": "default",  # 网关 --aliases 里的别名
        "purpose": "demo_segmentation",  # snake_case
        "root_feedback_id": "system",  # 没有用户反馈起因时用 system，且 depth 必须为 0
        "depth": 0,
        "system": "你是只输出 JSON 的测试助手，不要输出其他文字。",
        "data": {
            "segments": [
                {"start_ms": 0, "end_ms": 4000, "text": "合成示例：今天天气不错。"},
                {"start_ms": 4000, "end_ms": 9000, "text": "合成示例：我们去公园散步。"},
                {"start_ms": 9000, "end_ms": 12000, "text": "合成示例：然后回家吃饭。"},
            ]
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "boundaries_ms": {"type": "array", "items": {"type": "integer", "minimum": 0}},
            },
            "required": ["boundaries_ms"],
            "additionalProperties": False,
        },
    }


def main() -> None:
    token = getpass.getpass("调用方的①令牌（43 位，不回显）: ")
    call_id = new_id()  # 调用方生成；Python 3.12 标准库没有 uuid7
    request = build_request(call_id)
    state = {"call_id": call_id, "business_key": "demo-1", "status": "prepared"}
    with GatewayClient(os.environ["EAR_GATEWAY_URL"], token) as client:
        save(call_id, state)  # 先落盘，再发送
        try:
            output = client.complete(request)
        except GatewayError as exc:
            # 超时、断连或非 200：模型可能已经执行。用同一个 call_id 对账，绝不换新 id 重来。
            save(call_id, {**state, "status": "unknown", "http_status": exc.status})
            try:
                receipt = client.reconcile(call_id)
            except GatewayError as again:
                raise SystemExit(
                    f"对账失败（HTTP {again.status}），稍后用同一 call_id 再对账：{call_id}"
                )
            save(call_id, {**state, "status": "reconciled", "receipt": receipt})
            raise SystemExit(f"结果未知，已对账（回复不可取回）：{receipt}")
    save(f"{call_id}.output", output)  # 演示用“业务存储”；真实流程写你自己的业务存储
    save(call_id, {**state, "status": "done"})
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

`complete` 只返回 `output`；`GatewayClient` 把所有非 200 都折叠成 `gateway_request_failed`（只保留 HTTP 状态），把传输失败折叠成 503 `gateway_outcome_unknown`，所以拿不准时一律先 `reconcile`。`call_id` 的生成与保存建议：

1. **生成**：用 `ear_contracts.base.new_id()`（UUIDv7，小写，格式见 `ID_PATTERN`）。`complete` 在没给 `call_id` 时也会自己 `new_id()`，但那样你拿不到 id，无法对账——可恢复的流程必须自己生成。
2. **一次业务操作一个 id**：把 `call_id` 与你的业务键绑定；重试同一个业务操作要复用它，不要在重试循环里调用 `new_id()`。
3. **发送前原子落盘**（临时文件 + fsync + `os.replace` + fsync 目录，如上），状态 `prepared`；之后按结果改为 `done` / `unknown` / `reconciled`。状态文件只存 id、业务键、状态和时间，不存令牌、提示词正文或模型回复。
4. **回复不缓存**：先把 `output` 写进你自己的业务存储，再标 `done`。崩溃在两步之间则回复拿不回来（`reconcile` 只补审计记录）；是否用**新**的逻辑调用（新 id）重新生成，是业务决定，不是自动重试。
5. **重启后**：遍历状态目录，对 `prepared` / `unknown` 的 id 调 `reconcile`。`prepared` 但从未发出（崩在落盘和发送之间）会得到 404（该网关 journal 里没有这个 id）；若 journal 没有丢失，可以由人决定沿用同一个 id 重发。
6. 对同一个 id 再次 `complete` 会得到 409（`duplicate_call_id` / `call_in_progress`），这不是可自动重试的错误。

**可恢复的流程必须自己保存 `call_id`**；未给 `call_id` 时每次调用都是新的逻辑调用。传输错误可能意味着模型已执行：用同一个 `call_id` 调 `reconcile`，**绝不自动换一个 id 重来**。

### 3.5 运维须知

- journal 文件（SQLite，WAL）必须跨重启保留；丢失或替换它就丢了发送前的去重。没有自动过期、删除或重放模型调用。
- 崩溃后在途调用成为“结果未知”的审计记录，不会重发；待写记录用同一 request_id 与载荷补写①。
- **隔离只在确认冲突时发生（以 `service.py` 的 `_persist` 为准）**：待写记录补写①时，①报告永久 `id_exists`，或①里已有同 id 的 `llm_call` 且内容不一致（`record_conflict`）→ 隔离。若①返回的是**权限被拒或请求无效**，不能直接认定冲突（被 ACL 隐藏的同 id 记录也会触发这类错误）：网关再问一次 `call_id_occupied`，**只有返回字面 `True` 才隔离**；返回 `False`、非布尔值、①缺该入口或调用抛异常，都保持待写（`pending`），对外 503 `record_pending`，下次对账或恢复批次重试。其他错误同样保持待写。
- 隔离的含义：保留完整原记录和 request_id，不计为已保存、不改名、不再调模型；之后对该 id 的对账返回 409 `record_conflict`；隔离不会让启动恢复无限循环（见实现说明 F3），每个恢复批次会记一条 `audit_quarantine_requires_resolution` 错误日志（含数量）。没有自动处置工具，需要运维调查。
- 运行中的 WAL/SHM 辅助文件被移走或替换会使服务返回 503 并拒绝新调用。

## 4. 保证与不保证

| 保证 | 不保证 |
|---|---|
| 缺调用方、调用方与令牌不符、无请求上下文的重判调用在调用 provider 前被拒绝 | 对已被外部模型接受的请求撤权后取消它（只在每次尝试前和释放结果前复核） |
| 只有 provider 明确声明“执行前被拒”的失败才重试（默认最多 2 次，上限 3）；超时、断连、格式错误回复、适配器异常一律视为结果未知，不重试 | provider 侧恰好一次；多个独立 journal 的分布式去重；跨 provider 的原子预占 |
| 每个逻辑调用至多写一条终态 `llm_call`；写①失败保留待写记录，同一 request_id 重放 | 恶意同 UID 进程的任意检查/使用竞态；整卷丢失；真实上游 SDK 的行为 |
| 输出 schema 仅支持受限子集，不支持的写法在任何 HTTP 前返回 422 且不占用 id、配额 | 完整 JSON Schema；业务校验（如区间有序）仍由调用方负责 |
| 默认限流：全局每分钟 60、每调用方每分钟 20，并发 4；限流预占落在 journal，重启后不重置 | 可配置的限额入口（命令行/容器当前使用默认值） |

## 5. 验证（2026-11-07 取证；不得当作完成证明）

| 检查 | 结果 | 不能说明什么 |
|---|---|---|
| **全套测试·当前**（四个外部开关全开，14:09 起、14:26 结束，`ear-67-full-v2.log`） | **通过**：退出 0；2067 passed，11 skipped，2 warnings，用时 1001.65 秒 | 合成/隔离环境；11 个跳过：`test_causality_context_guard.py` 5 个、`test_llm_preflight.py` 6 个，原因均为需要认证 PostgreSQL。网关测试没有真实模型调用，不是生产验收 |
| **静态检查·当前**（`ear-67-static-final`，14:36） | 退出 0：`ruff check`、`ruff format --check`、`pyright`、`uv lock --check`、`tools/check_docs.py`（含 `--base origin/dev`）、`git diff --check`、`node --test` | 只是静态检查 |
| **静态检查·历史失败（保留）**（`ear-67-static-v2`，14:26） | 退出 1：`ruff format --check` 要求重排本页第 3.4 节的 Python 示例代码块（1 file would be reformatted）；链上其后各项当次未运行 | 仅格式问题。已重排该代码块（只改 Markdown），14:36 整条链重跑退出 0 |
| **全套测试·历史失败（保留）**（四个外部开关全开，12:35 日志） | **3 failed**，2058 passed，11 skipped，2 warnings | 没有通过；3 个失败都在部署烟测/离线备份往返（缺镜像变量；旧 storage 镜像入口文件权限），不在网关测试内；已定向修复，并由上面“当前”行 14:26 的全套重跑取代 |
| 独立复核（`ear-67-postfix-review.json`） | 网关旧的 3 项必须修复（输出 schema 校验、WAL/SHM 辅助文件身份、恢复批次与隔离）均已关闭，范围内剩余 0 项；定向 263 passed/6 skipped/23 deselected，另有 20 个探针通过 | 外部服务关闭；不是全仓或生产验收；未跑真实 Octopus、provider |
| 合成容器（`ear-api-gateway-deploy-state.json`） | 20 passed，1 warning（内置 Octopus 适配器与自定义 provider 两种形态，非 root、受保护密钥、持久恢复） | 合成数据；没有付费模型 |
| 隔离真实 PostgreSQL/NATS（①侧预检与 api-gateway 集成，12:07） | 73 passed，6 skipped | 临时容器夹具与合成 provider |

实现细节、文件清单与逐项出处见[实现说明](ai-docs/implementation.md)。

## 6. 未完成与待决

- 真实模型调用与生产 wire 验证；对端 Octopus 版本核对；生产部署。
- ③⑤和插件接入网关、④恢复与重判关联接入；各调用方的 `call_id` 持久化与对账流程。
- 正文保留期、作用域与清理（①侧也无 TTL）。
- 限额、重试次数、超时的部署入口（目前为代码默认值）。
- 全套验收已取得最终结果（第 5 节），但属合成/隔离环境，不含上述各项。

## 7. 谁来决定什么

- **普通工程选择，实现者自行决定，不需要用户批准**：调用方鉴权（沿用调用方①令牌）、journal 的单 worker 私有 SQLite 模型与隔离策略、限流默认值、重试上限。评审按普通代码评审意见调整。
- **仍需对应负责人确认**：正文保留期与作用域；Octopus 复用边界与生产对接版本；各业务模块的接入方式（会改变其行为的部分）。
- **设计已定、不属于待批事项**：所有大模型调用必须经 ⑦ 并带调用方模块和 `root_feedback_id`；默认只存 metadata；不改 accepted 的语义。
