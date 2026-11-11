---
audience: ai
doc-type: guide
based-on: experiments/codex_baselines/README.md；services/adapters（ear_adapter 的 HttpEarSource、InboxAudioLocator、readiness）；codex-cli 0.155.1 与本机 Codex 源码 2026-09-18 快照（models.json 只有 text/image 输入）；OpenAI gpt-audio 模型页（Chat Completions input_audio）；负责人 2026-11-07 确认的 A1a/A1b/A2 分组、2026-11-10 拆出离线的 A1c；负责人 2026-11-11 改为开卷/闭卷端到端问答（README 第 1 节）；2026-11-10 A1c 真实运行的事件流；负责人本轮 exam 运行器任务授权；OpenAI 官方价格页 2026-11-11
---

# Codex 基线实验：实现说明

## 1. 代码

`src/ear_baselines/`

| 文件 | 内容 | 副作用 |
|---|---|---|
| `media.py` | `probe`（ffprobe，按音频流识别）、`normalize`（16 kHz 单声道，.m4a/.mp3/.wav）、`split`（ffmpeg segment） | 子进程、文件 |
| `cases.py` | `Case`、`CaseStore`（实验目录布局）、`discover`、`display_tz` | 文件 |
| `evidence_md.py` | `render_evidence`：旧 Ear Evidence → Markdown 时间线（纯函数） | 无 |
| `inventory.py` | `build_inventory` / `classify` / `run_state`：全集索引、失败归因、未入库音频；`render_index` 生成人读总览 | 只读 HTTP/SQLite/inbox；仅写私有实验输出 |
| `octopus.py` | `import_captures`：旧 Ear → case（音频 + A2 证据），复用 ② 的 `readiness` 与 `InboxAudioLocator` | 网络、文件 |
| `openai_audio.py` | `listen`：A1a，切片后逐片发 Chat Completions `input_audio`，`post` 可注入 | 网络、文件 |
| `player.py`、`player.html` | `timeline`（Evidence → 前端行，秒数相对录音开头）、`PlayerApp`（索引、详情缓存、搜索、备注、音频路径白名单）、`serve`（`ThreadingHTTPServer`，只绑 127.0.0.1，音频支持 Range）；页面是单文件原生 JS | 网络（旧 Ear）、读文件、写 `notes.json` |
| `codex_runner.py` | `prepare` / `codex_command` / `run`：建工作区、跑 `codex exec --json` | 子进程、文件 |
| `exam.py` | `prepare` / `workspace` / `run` / `report`：考卷、容器命令、生命周期与人工对照 | 文件、Docker/Colima 子进程 |
| `meter.py`、`openai_prices.json` | 标准库 HTTP/SSE 代理、音频时长、费用估算及预算锁 | OpenAI HTTP、ffprobe、计量日志 |
| `isolation.py` | 容器内路径/环境/旧 Ear TCP 与外网 TLS 自检 | 只读文件、网络 |
| `task_default.txt` | 默认目标 | 无 |
| `__main__.py` | `ear-baselines` 命令行 | 全部 |

## 2. 约定

- case_id：本地文件 `file-<sha256 前 16 位>`，Octopus `octopus-<capture_id>`。重复导入返回原 case，不重新转码。
- A2 渲染：只用 `original_text` / `original_label`；说话人标签按说话人分离作用域所在的音频块编号（`块N·S1`），跨块不合并；GPS 按（时刻、经纬度）去重；IMU 不渲染；旧 Ear 推断的手机事件标「手机·推断」。
- `a2_evidence.json` 是 pydantic 解析后的结果（`extra="ignore"`），不是旧 Ear 的完整原始响应。
- A1a：时刻用 `display_tz()`；没有录制时间（文件没有 `creation_time`）时用 `+时:分:秒` 偏移。每片的原始回复存 `a1a/reply_NNNN.json`，汇总在 `a1a/meta.json`。HTTP 429/5xx 重试 2 次。
- Codex：`--ignore-user-config --ignore-rules -s workspace-write`；`NETWORK_ROUTES`（只有 a1b）加 `sandbox_workspace_write.network_access=true`，没有命令行开关。a1b / a1c 输入相同：`input/recording.m4a` + `input/recording_info.md`（`recording_info()`：开始时间按 `display_tz()`、时长），提示要求中间产物留在 `work/`。stdin 为 `/dev/null`。失败或超时也写 `run.json`。

## 3. 环境变量

| 变量 | 用途 |
|---|---|
| `EAR_BASELINES_EXAM_PROXY` | `exam run` 容器内可用的宿主代理，如 `http://192.168.5.2:7897`（本机经 Clash 上外网） |
| `EAR_BASELINES_ROOT` | 实验目录，默认本目录 `local-data/`（git 忽略） |
| `EAR_BASELINES_EAR_DB` | inventory 的旧 Ear SQLite 路径，默认 `~/.octopus/local/service/herryying30/octopus-service-ear/db/ear.sqlite3`；连接强制 `mode=ro` + `query_only` |
| `EAR_BASELINES_TZ` | 显示时区（IANA），默认本机 |
| `EARbaseline_key` | 实验用 OpenAI key（A1a；exam 只交代理容器，Codex 收到一次性令牌）。负责人指定的名字，故意不用 `OPENAI_API_KEY`（codex CLI 看到它可能改走 API key 计费），代码里对 ruff SIM112 加了 noqa |
| `EARbaseline_base_url` | exam 只在 Codex/检查容器设置为 `http://meter:8787/v1`，不是宿主可覆盖的上游 |
| `CODEX_HOME` | exam 容器内为 `/codex-home`，宿主临时目录初始只复制 auth.json，结束删除 |
| `METER_TOKEN`、`METER_BUDGET` | 仅代理容器内的本次令牌、美元预算，运行器生成 |
| `EAR_BASELINES_AUDIO_MODEL`、`EAR_BASELINES_CHUNK_SECONDS` | A1a 模型（默认 `gpt-audio`）和切片秒数（默认 600） |
| `EAR_BASELINES_OPENAI_URL` | 换 Chat Completions 地址（兼容代理） |
| `EAR_BASELINES_CODEX_MODEL` | Codex 模型，默认 `gpt-6-astra` |
| `EAR_ADAPTER_EAR_URL`、`EAR_ADAPTER_EAR_TOKEN`、`EAR_ADAPTER_DATA_ROOT` | 与 ② 共用，见 [② 实现说明 5.2](../../../services/adapters/ai-docs/implementation.md) |

## 4. 验证

自动测试：`uv run pytest experiments/codex_baselines`（ffmpeg 合成音频 + ② 的合成旧 Ear 响应；不调用 OpenAI 和 Codex）。

2026-11-07 人工验证：

- 合成 A2 case 上真实跑了一次 `run a2`（`gpt-6-astra`、`low`）：退出码 0，写出 `answer.md`，67 秒，输入约 6.7 万 token（大部分是 Codex 自身提示词，已缓存）。
- 真实旧 Ear 导出 1 段 30 分钟录音（只看了条数）：7 个音频块，194 条发言、72 条环境声、1 个定位点、1 条手机状态。旧 Ear 返回的是 UTC，因此加了 `EAR_BASELINES_TZ`。
- 未验证：A1a 真实请求（当时没有 key）、iPhone `.qta` 文件（测试只覆盖 wav/mp3/flac/m4a）。

2026-11-10：

- 真实 30 分钟录音上的离线运行（当时路线名 a1b，`network=false`）：退出码 0，1057 秒。运行目录已移到 `runs/a1c/20261110T073252Z/`，`run.json` 的 `route` 改为 a1c 并加了 `note`。Codex 的做法和结论见 [README 第 4 节](../README.md)；它在 `/tmp` 下建过 `recording-uv-cache`（uv 缓存的 APFS 克隆）和 `prepare_recording_env.py`，运行结束后 `/tmp` 里已经没有这些文件；`work/`、`.venv` 被它自己删了。
- 随后跑了联网的 a1b（同一段录音，`runs/a1b/20261110T075109Z/`）：退出码 0，1740 秒。用 openai-whisper（small，再用 large-v3-turbo 复核），没有用本机 MOSS；事件流里的外部地址只有模型下载（openaipublic.azureedge.net）和一次 HuggingFace HEAD 请求。`work/` 约 13 GB（`cache-existing/` 9 GB 为 uv 缓存的 APFS 克隆，`turbo.pt`、`turbo_parts/`、`small*.pt`、`model_parts*/` 约 4 GB）。对比结论见 [README 第 4 节](../README.md)。
- 同一段录音的 a2（`runs/a2/20261110T084801Z/`）：退出码 0，113 秒，输入 107051 token（缓存 90112），输出 2491 token。三组对比见 [README 第 4 节](../README.md)。

### inventory（2026-11-11 负责人授权）

- canonical term 使用 `inventory`，复用 `Capture`、`readiness`、`InboxAudioLocator`、`display_tz`、`render_evidence`；没有新增同义架构层或删除既有名称。`readiness` 实际在 `ear_adapter/convert.py`，不是独立文件。
- `read_database` 在一个只读事务内读取 captures / pipeline_runs / pipeline_tasks / remote_jobs 的 payload；按 created_at 选择最新 run，以任务开始时间、run 更新时间、远端 last_processing_at 和 next_retry_at 辅助审计。不能用 immutable 连接规避 WAL，也不推测活跃数据库的进程状态。
- 感知重跑方法依据旧 Ear `ear/ingest/reprocess.py`、`ear/api/routes_recordings.py`：`POST /api/recordings/reprocess`，body 为 instance_id / record_id；有活动 run 时返回 already_running。疑似卡住须先人工核对，再用 `POST /api/runs/{run_id}/cancel`。本命令只输出说明，不执行。`routes_mvp.py` 的 analysis-rerun 仅调度下游任务。
- `rerunnable=yes` 表示具备所列前提后有明确重跑路径，不承诺当前远端在线；代码兼容、模型加载及未知故障用 unknown。原始错误保留在 failures，顶层 failure 是首个原因，failure_groups 覆盖全部原因并按录音去重。
- counts：speakers 按 (diarization_scope_id, local_speaker_id) 去重；gps 按时刻和经纬度去重；phone_state 计 context_snapshot；转写使用 original_text。日期表按开始日期统计整段时长。
- 真实执行受当前沙箱限制：本机 HTTP 连接返回 Operation not permitted，源 SQLite 的 mode=ro 连接也无法打开；没有生成真实全集索引。为核对 schema/错误字段，在无 WAL 且复制前后主文件 mtime 不变时读取了 `/tmp` 下的数据库副本，未复制音频、未写旧 Ear；副本观察不能代替完整 API + inbox 验证。
- 自动验证涵盖合成 Evidence 与临时 SQLite、六类分类、无语音例外、声纹授权缺失、过期运行/远端进展、历史失败隔离、同内容文件去重、显示时区、JSON/Markdown 输出与数据库/音频不变。真实验证的阻塞必须在交付中保留，不宣称成功。
- 本轮检查结果：ruff、ruff format --check、pyright、check_docs（含 `--base origin/dev`）通过；pytest 为 376 passed、1 error，错误是既有 extraction HTTP 测试绑定 localhost 被沙箱拒绝。inventory 13 项测试全部通过。`uv run ear-baselines inventory` 已尝试，HTTP 连接被拒绝；没有真实输出可供确认。临时数据库副本已删除。另移除了同时出现在工作区的 player.py 中无效的 `noqa: A002`，仅修复 lint。
- 随后在沙箱外真实运行（2026-11-11，Claude）：433 段，ready 384（163.5 小时）、failed 47、incomplete 2，其余 0，没有活动任务。第一次运行按本机时区 JST 显示，但 inbox 文件名显示录音在 UTC+8，所以 `deploy/.env` 设了 `EAR_BASELINES_TZ=Asia/Shanghai` 后重跑。随后改为：失败但有发言的录音也导出 `transcripts/`（22 段，只是说话人或环境声阶段失败），共 408 份；相应改了两个 inventory 测试。

### player（2026-11-11）

- 详情优先从旧 Ear 现取（成功结果缓存在内存），失败时退回 `transcripts/<id>.md`；搜索只扫 `transcripts/*.md`（跳过 `#` 开头的标题行）和备注，最多 300 条。音频只服务 `index.json` 里登记且存在的文件。
- 页面的定位链接 `#<id>@<秒数>`；搜索命中行用行内第一个 `HH:MM:SS` 换算偏移（跨午夜按 +24 小时）。
- 测试：`tests/test_player.py`（时间线、退回 Markdown、缓存、搜索与备注、音频白名单），不起 HTTP 服务。人工验证：在真实索引上浏览日期、搜索「上课」、点命中跳转并播放、同步高亮正常。

### exam（2026-11-11 实现，宿主实测通过）

- canonical terms 为 `exam`、`meter`、`isolation`，没有移除或重命名既有概念；`codex_runner.py` 保留不动，仅复用 `_usage` 读取事件流最后一次用量。实验不改变产品模块契约。
- 输入、命令和预算行为以 [README 第 3 节](../README.md#3-用法) 为准。题库当前使用 `reference_answer`；证据中 `clock` / `offset_s` / `quote` 只保留给评分，工作区仅输出 id/question。题号和录音 id 校验禁止路径跳转；不复制原始文件符号链接，工作区拒绝材料目录内符号链接。
- `prepare` 在临时目录完成校验与复制后替换目标；`force` 失败前保留旧考卷。音频保持原字节，不复用 case 中的归一化音频。分段前缀是音频文件名第一个 `__` 前的部分；传感文件支持该前缀后可选序号及完整音频 stem 两种匹配，必须有 context 和 imu。真实结构未验证：local-data 内没有原始 context，index 的 audio_path 在允许读取范围之外，未访问旧 Ear 文件或接口。
- Docker 构建上下文为实验目录；`.dockerignore` 只放行 Dockerfile 和代理/自检代码及价格表，防止真实数据和凭据进入构建上下文。镜像固定 Codex 0.155.1（本机 `codex --version`），Node 24.11.0 / Debian bookworm、uv 0.9.9；不自动构建镜像。
- Codex/自检容器共享命令构造器；仅两个 bind mount、四个显式环境变量。代理仅挂 `meter.jsonl`，通过唯一容器名加网络别名 `meter` 避免并发名称冲突。不发布宿主端口。宿主真实 key 不进入工作区或命令打印；日志不写请求/响应内容。Docker 管理员仍可 inspect 代理环境。
- 自检由镜像内 `isolation.py` 执行：对旧 Ear 端口（解析自 `EAR_ADAPTER_EAR_URL`）和 Octopus 网关端口 `31415`，逐个探测 `host.docker.internal`、`host.lima.internal`、`192.168.5.2`、默认网关和宿主局域网地址（`host_addresses()` 读 en0/en1，作为第二个参数传入），记录成败及错误类别；外网检查经 `HTTPS_PROXY`（若有）CONNECT 到 `api.openai.com` 并完成 TLS。防火墙默认开启：inspect 专用网和默认 bridge 取子网/网关，插入 DROP 规则，再用 `-I` 把放行代理端口的 ACCEPT 插到最前；已尝试插入的规则逆序删除（也覆盖命令超时但可能已生效的情况）。清理失败逐项保存，不能仅凭命令已发出宣称清理成功。`try/finally` 不覆盖 SIGKILL / 宿主崩溃。
- 代理：`EAR_BASELINES_EXAM_PROXY`（或 `--proxy`）是容器内可达的宿主代理地址，`proxy_env()` 给三个容器都加 `HTTPS_PROXY`/`HTTP_PROXY` 和 `NO_PROXY=meter,localhost,127.0.0.1`。计量代理的 `Meter(proxy=...)` 用 `HTTPSConnection.set_tunnel` 经代理连上游；测试里不读宿主的 `HTTPS_PROXY`。
- 临时 `CODEX_HOME` 建在运行目录的 `.codex-home-*`（Colima 默认只共享主目录，`/var/folders` 挂不进容器），不在 `/work` 下，`TemporaryDirectory` 结束即删。
- 预算锁覆盖完整上游请求及结算，避免多请求同批超支；SSE 数据逐行 flush，提取 Chat usage 或 Responses `response.usage`，Chat 流请求自动补 `stream_options.include_usage=true`。客户端断连后继续读上游结算；代理结束收到 SIGTERM 时停止接单并等待请求线程，宿主最多等待 130 秒后清理。强制终止/日志残缺需要人工核对实际账单。
- ffprobe 测 multipart `file`、递归 `input_audio.data` base64 的时长，临时文件随即删除。每行日志含模型、路径、时长、纯数值 usage（含 audio token）、估算/累计/超额费用、状态及 uncertain。请求体最高 512 MiB。未知模型取表内分项最高价，转写用官方估算分钟价；金额不是实际账单。Codex 订阅 token 与代理 API 花费分别展示。
- 官方价格页 Markdown 版在 2026-11-11 核验通过，JSON 记录出处和日期；Astra 用标准长上下文价格保守估计。缓存不打折；不计托管工具等独立费用，因此相关请求拒绝。没有 usage 的成功请求冻结剩余预算，并把未知费用与已知 token 费用区分标记。
- 测试入口 `tests/test_exam.py`：合成考卷、泄露排除、命令/脱敏、防火墙失败清理、答案验证及报告、合成 wav/base64/multipart、假 HTTP 上游的换令牌/SSE/并发预算。localhost bind 被沙箱拒绝时 HTTP 测试明确 skip（2026-11-11 已在宿主补跑通过）；不调 Docker、不访问外网。Docker、防火墙、代理的实测见 README 第 4 节试跑。
- 根 `pyproject.toml` 的 Pyright 和 pytest 递归发现排除 `local-data/`，避免遍历实验下载的模型 Python 环境；仍检查本实验全部源码和测试。此排除只涉及类型检查和测试发现，数据隐私仍依靠 `.gitignore` 和 `.dockerignore`。
