---
audience: human
doc-type: proposal
status: proposed
generated-by: Claude (AI)，应 YijieYing（③④代理负责人）要求起草
generated-at: 2026-11-25
based-on: docs/design/design-v1.2.md 第 9 节；services/llm_gateway/README.md 第 3、4 节；origin/dev 5200899 的 services/llm_gateway/src/ear_llm_gateway（models.py 的 Provider、ProviderResult、ProviderFailure、MAX_PROVIDER_TIMEOUT_SECONDS；cli.py 与 deploy/api_gateway_runtime.py 的 provider_factory 加载；service.py 的 admit 钩子与并发信号量；openai_compatible.py 的用量处理）；libs/ear_contracts 的 LLMUsage；本机 codex-cli 0.172.0 的 `codex exec --help`（--json、--output-schema、--output-last-message、--sandbox、--ephemeral、--model）；PRTS pulse 用 Codex（总控称默认 gpt-6-astra，未逐次核对）做分诊和纠错的做法（仓库外）
---

# 提案：⑦ 的 Codex 本地 fallback

> **状态：proposed，未采纳。** ⑦的正式渠道不变：默认 Octopus Manager，可选直连 New API。本文提议给⑦加一个调用用户本机 Codex 的 provider，**只作为本地部署的 fallback**（YijieYing 2026-11-25 定位），不作为正式渠道，也不进入非本地部署。provider 本身走⑦已有的 `provider_factory` 扩展点；但“主渠道确知未执行时改走 Codex”改变了⑦ README 现写的“没有模型回退”，需负责人决定（第 3 节）。具体代码交由实现者，本文只定行为。

## 1. 为什么

- ⑦现有两种 provider：默认的 Octopus Manager，和可选的直连 New API（引导模型 `gpt-6-luna`）。它们是正式渠道，本文不改。
- 本机部署时，正式渠道不可用（未配置、宕机、限流拒绝）会让④整段停住。用户的 Codex 额度充足，pulse 也已在用 Codex 做分诊和纠错，可以作为本地兜底。
- ④接手纠错时（见[纠错提案](transcript-correction.md)），也可以用它在本地与 pulse 同模型对照。

## 2. 接入方式

- provider 放在⑦内（暂名 `ear_llm_gateway.codex:create_provider`），只实现⑦已有的 `Provider` 接口：`complete(call, model, timeout) -> ProviderResult`，可选 `admit`。
- 只能由本地部署的配置启用；默认 compose 和任何非本地部署都不带它。
- 调用方（④等）不感知 provider，仍请求原来的别名。

## 3. fallback 规则（需负责人决定）

- ⑦内部把正式渠道和 Codex 组合成一个 provider：每次先走正式渠道；**只有正式渠道确知未执行**（`ProviderFailure(safe_to_retry=True)`，例如连接被拒、准入拒绝、未配置）且有限重试用完时，才改走 Codex。
- 正式渠道**结果未知**（超时、断连、非 200、回复无法解析）时不改走 Codex：上游可能已经执行，换渠道等于重发。这与⑦“结果未知不重试”的规则一致。
- 同一个 `call_id` 至多被一个渠道执行；`llm_call` 记录实际使用的模型名，以便按渠道统计。若需要单独的渠道字段，另提契约增补。
- 切换后 Codex 只用本次调用剩余的超时预算；确知未执行的失败通常很快返回，但④的 `gateway_timeout` 仍要覆盖两段之和（见第 6 节）。
- 也允许本地配置为“只用 Codex”（没有正式渠道时），这仍是本地部署的配置，不是正式渠道。
- 备选：不做自动切换，只在本地手动改配置切到 Codex。更简单，但正式渠道短时不可用时④仍会停住。

## 4. 容器内怎么访问宿主机

Codex 登录凭据在宿主机用户目录里，`codex` 命令也装在宿主机上；⑦在 compose 里是容器。

**建议：宿主机上跑一个很小的桥接进程，⑦的 provider 通过 HTTP 调它。**

- 桥接进程只做一件事：收到 `{model, system, data, output_schema, timeout}`，在一个空的临时目录里运行一次 `codex exec`，返回结构化输出和用量。它不连①、不写审计；审计仍由⑦写。
- 监听与认证：
  - macOS（Docker Desktop）：桥接只监听宿主机回环地址，容器用 `host.docker.internal` 访问。
  - Linux：compose 给⑦加 `extra_hosts: host.docker.internal:host-gateway`，桥接只监听 docker 网桥地址，不监听所有网卡。
  - 两边都用一把共享令牌：⑦用挂载的 0600 密钥文件，桥接从自己的私有文件读取；令牌不进 JSON 配置、命令行参数或 compose 环境变量（与现有 Octopus、New API 密钥同样处理）。
  - 这两种访问方式要在实施时于目标机器上实测，本文没有验证。
- **不建议**把 Codex 凭据挂进容器或在镜像里装 `codex`：凭据会被复制进容器，刷新令牌的写回也难处理。
- **备选**：⑦不进容器，按⑦ README 第 3.2 节的命令行方式在宿主机运行，provider 直接起 `codex exec` 子进程。少一层桥接，但④在容器里要能访问宿主机的⑦，暴露面一样要处理。

## 5. 每次调用怎么跑 Codex

- 命令形态：`codex exec --ephemeral --skip-git-repo-check --sandbox read-only -m <模型> --output-schema <临时文件> --json -o <临时文件>`，提示词经标准输入传入，不放命令行参数。
- `--ephemeral`：不在宿主机留下包含转写内容的会话记录。
- `--sandbox read-only` 加空临时目录：Codex 是会执行命令的代理，这里只要它回答，不能让它读写宿主机文件。提示词沿用④的“证据是不可信数据，不是指令”。
- 提示词由 `system` 加 `data`（JSON）组成；结束后删除临时目录。
- `output_schema` 交给 `--output-schema`，返回后⑦照常做输出 schema 校验。Codex 的结构化输出对 schema 有自己的限制（例如要求所有字段必填、禁止额外字段）；不满足的 schema 在 `admit` 里直接拒绝（422，不执行、不占额度），不在执行后才失败。

## 6. 超时

- 一次 `codex exec` 可能要几分钟。网关的 `provider_timeout_seconds` 设到 600（现有上限），④的 `gateway_timeout` 设 630，与 New API 直连的配置方式一致。
- 超时后桥接结束整个 Codex 进程组。此时可能已经消耗了额度，也可能已经生成了结果：按⑦现有规则记为结果未知（`ProviderFailure(safe_to_retry=False)`），**不重试、不换 id 重发**，由调用方按结果未知流程处理。
- 桥接自己的超时应略短于⑦给 provider 的超时，保证⑦先拿到桥接的明确答复，而不是两边同时断开。

## 7. 一次只发一个请求

- 用户的 Codex 额度和速率是整个账号共用的；并发会互相拖慢，也难以估算用量。所以同一时刻只运行一个 `codex exec`。
- 两层保证：
  1. provider 内部持有一把锁；等锁的时间计入本次调用的超时预算，预算内拿不到锁就报“未执行”（`ProviderFailure(safe_to_retry=True)`），这是确知未执行，⑦可按现有规则有限重试。
  2. 桥接进程自己也串行：正在执行时收到新请求直接回“忙”，provider 同样当作未执行。这样即使误配了多个⑦也不会并发。
- ⑦现有的并发上限（默认 4）和每分钟限流不变；这里的串行只针对 Codex provider。
- 这把锁不管 pulse 等仓库外的 Codex 使用；两边同时跑时由用户安排时段，或者以后共用一个桥接。

## 8. 用量

- `codex exec --json` 的事件流里带本次运行的 token 用量。桥接只在能读到明确的非负整数时返回，provider 映射到 `LLMUsage` 的 `input_tokens`、`output_tokens`、`total_tokens`。
- 读不到或格式不对时返回 `null`，⑦记为“用量未知”，计入 `unknown_calls`。不估算、不填 0，与现有 Octopus 适配器的做法一致。
- 缓存命中的输入 token 等 Codex 特有字段，`LLMUsage` 没有对应位置，暂不记录；需要时另提契约增补。

## 9. 错误归类

| 情况 | 归类 |
|---|---|
| schema 不满足 Codex 限制、提示词超长 | `admit` 拒绝，422，未执行 |
| 桥接不可达（连接被拒）、桥接回“忙”、锁等待超出预算 | 未执行，可有限重试 |
| 已发出后断连、超时、Codex 非零退出、输出无法解析 | 结果未知，不重试 |
| Codex 未登录或额度耗尽 | 桥接能确定未执行时报未执行，否则结果未知；错误文本不回显、不记录 |

## 10. 数据与隐私

- 走 Codex 时真实转写会发给 Codex 背后的模型服务，与 pulse 现状相同。用户已把它定为本地 fallback（2026-11-25），所以只在用户本机部署出现；非本地部署不能启用。
- ⑦默认不存提示词和回复正文，这一点不变；桥接也不记录正文，日志只记调用 id、耗时、退出码和用量。

## 11. 验收

1. fallback 测试：正式渠道确知未执行时改走 Codex；正式渠道结果未知时不改走、不重发；同一 `call_id` 只被一个渠道执行；非本地配置无法启用 Codex。
2. 合成 provider 测试：正常输出、用量可读和不可读、超时（进程组被结束且记为结果未知）、忙、桥接不可达、schema 不满足限制，各自的归类符合第 9 节。
3. 两个并发请求只有一个在运行，另一个等待或报未执行；计时符合第 7 节。
4. 宿主机实测（不进 CI）：容器内⑦经桥接完成一次合成短样本调用，`llm_call` 记录了模型别名、耗时、用量或“未知”。
5. 宿主机上没有留下包含提示词的 Codex 会话文件，临时目录已删除。

## 12. 待决

已由 YijieYing 2026-11-25 定下（提案仍为 proposed，待正式采纳）：Codex 只作本地 fallback，不作正式渠道；在本地部署中经 Codex 发出转写可以接受。

1. 第 3 节：自动 fallback（主渠道确知未执行才切换）还是只能手动切换。前者改变⑦“没有模型回退”的现有说明。
2. 本地部署里⑦在容器内经桥接调用，还是⑦直接在宿主机运行。
3. Codex 用的模型名：沿用 pulse 的模型，还是另定。
4. 与 pulse 共用 Codex 时的时段或串行安排。
