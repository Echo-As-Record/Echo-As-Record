---
audience: human
doc-type: guide
---

# Codex 基线实验（开卷 / 闭卷）

研究用的对照实验，**不是产品模块**：不经过 ① 存储和 ⑦ 大模型入口，不受 ② 适配器「只做确定计算」的限制，所以放在 `experiments/` 而不是 `services/adapters/`。产品链路的规则见 [设计 v1.2](../../docs/design/design-v1.2.md)。

## 1. 实验设计

2026-11-11 负责人确定：用**同一套问题**、**同一批录音**，端到端比较三种做法。每组都是「给问题 → 得到回答」，回答和人工核对过的标准答案比较。

| 组 | Codex 能用什么 | Codex 不能用什么 | 本目录负责 |
|---|---|---|---|
| **开卷** | 闭卷的全部条件，加上旧 Ear 对全部录音的完整结果（MOSS 转写、局部说话人、PANNs 环境声、GPS、手机状态，即 `inventory` 导出的内容） | — | 是 |
| **闭卷** | 同一批录音的原始音频和原始传感、手机数据（来自 Brain inbox，不含任何处理结果）；可以联网、下载和搜索模型与工具，可以用 `EARbaseline_key` 调 OpenAI（包括能直接听音频的模型），其余办法都可以 | 本机旧 Ear 的任何数据（数据库、Evidence API、处理结果）；不能出沙箱 | 是 |
| **总线链路** | 本仓库 ①～⑦ 的完整链路整理出的内容，用来搜索、回答同一套问题 | — | 否，另行实验 |

- **给 Codex 的提示**：开卷告诉它「可以使用旧 Ear 的全量结果」；闭卷告诉它「可以上网，不能出沙箱，除此之外可以用一切办法」。两组对材料的描述用同一段话（例如录音里不只有说话，还有环境声；另附定位和手机状态），不告诉它用什么工具。
- **开卷包含闭卷的全部条件**（原始材料、联网、key），两组只差「有没有旧 Ear 的结果」。本轮运行器任务已明确采用这一条件。
- **问题**：由负责人和 AI 出，每题附证据索引（录音 id、时刻），标准答案由负责人听录音核对后才算数。题目在任何一组运行前定稿，不再修改。2026-11-11 的第一版草稿（20 题，未核对）在实验目录 `questions/questions-v0.md`：18 题由 Codex 从旧 Ear 转写起草，2 道地点题由 Claude 根据定位聚类补充；备用题和起草说明在同目录 `draft-2026-11-11.*`、`research/`。手机只在每组录音开始时记一次定位（与各段录音中位差约 22 分钟），地点题只能精确到「开始录音时在哪」。
- **回答格式**：每题给出答案、依据（录音 id 和时刻）、把握程度；没有依据时写「无法回答」。

### 闭卷的隔离要求：已实现（2026-11-11 实测通过）

`exam run` 对开卷和闭卷使用相同的 Docker 隔离。容器只挂工作区 `/work` 和新建的临时 `/codex-home`（初始只含宿主 `~/.codex/auth.json` 的副本）；不挂主目录、旧 Ear、缓存或考卷答案。显式传入的环境变量只有 `CODEX_HOME`、一次性 `EARbaseline_key`、`EARbaseline_base_url`、`TZ`，以及配置了宿主代理时的 `HTTPS_PROXY` 等代理变量。开卷额外复制 `ear/`，闭卷没有。单段录音的旧 `run` 命令保持原行为，不具备这些隔离保证。

每次新建专用 Docker 网络；代理使用网络别名 `meter`（容器名带运行随机后缀，避免并行实验冲突），真实 OpenAI key 只传给代理。运行 Codex 前用相同镜像、网络、挂载和环境检查 `/Users`、`~/.octopus` 不存在、无 `EAR_ADAPTER*` 环境变量、无法连接宿主旧 Ear 端口，以及 `api.openai.com` 的 DNS/TLS 可用。端口从宿主 `EAR_ADAPTER_EAR_URL` 解析；未设置时明确记录跳过端口检查。任一实际检查失败即中止。

防火墙默认开启（`--no-firewall` 关闭）：在 Colima 的 `DOCKER-USER` 链按该网络子网丢弃到 `192.168.5.0/24`（Colima 下这就是宿主回环）、专用网络网关、默认 bridge 的 host-gateway 和宿主局域网地址（en0/en1）的流量。本机只能经本地代理（Clash，`127.0.0.1:7897`）上外网，所以 `EAR_BASELINES_EXAM_PROXY=http://192.168.5.2:7897` 时再插一条放行规则，只放行到该代理端口；Codex、自检和计量代理容器都通过 `HTTPS_PROXY` 走它，计量代理用 CONNECT 隧道连 OpenAI。结束、失败、超时及 Python 中断均通过 `finally` 清理容器、网络和规则，清理失败写入 `run.json`，需人工处理。进程被 SIGKILL 或宿主崩溃无法保证清理（`colima ssh -- sudo iptables -S DOCKER-USER` 可查残留）。临时 `CODEX_HOME` 放在运行目录里（Colima 只共享主目录），不在 `/work` 下，结束即删。

2026-11-11 实测：不加防火墙时容器能连上宿主旧 Ear 端口（经 `host.docker.internal` / `192.168.5.2`），加上后所有宿主地址都连不上、经代理能访问 OpenAI（见第 4 节试跑）。经宿主代理访问 `127.0.0.1` / `localhost` / 宿主局域网地址上的旧 Ear 端口均返回 502（代理不转发到宿主回环）；Octopus 网关（`31415`，宿主所有网卡监听）经代理能连上，但接口要求登录（401），容器里没有它的凭据。这是剩下的已知边界。

### 2026-11-10 的试跑（A1a / A1b / A1c / A2）

开卷/闭卷之前，在单段录音、单个固定目标（整理时间线并提出确认问题）上试跑过四种做法，结果见第 4 节的单段录音试跑：

| 试跑 | 输入 | 对应现在的 |
|---|---|---|
| A1a | 原始音频先交给 OpenAI `gpt-audio` 写听写记录，再交给 Codex（没跑，当时没有 key） | 并入闭卷：闭卷可以自己调用能听音频的模型 |
| A1b | 原始音频 + 开始时间、时长；沙箱允许联网 | 闭卷的雏形（没有隔离读取） |
| A1c | 同 A1b，沙箱禁止联网 | 不再单独做 |
| A2 | 单段录音的旧 Ear 证据时间线 | 开卷的雏形（只有一段） |

**为什么不是「Codex 直接听」**：2026-11 本机的 Codex CLI（0.155）只支持图片附件；源码（2026-09-18 快照）里所有 Codex 模型声明的输入都只有文本和图片。MCP 工具返回音频的通道已经写好，但被模型能力挡住。

## 2. 样本

- **旧 Ear 的 Octopus 录音**（共同样本）：`ear-baselines inventory` 盘点全集。2026-11-11 晚的结果：493 段，2026-07-14～11-11；422 段结果完整（175.9 小时），47 段失败（多数只是说话人或环境声阶段失败，转写可用），22 段旧 Ear 正在处理，2 段感知未开始。失败原因和重跑方法见 `index.md`。
- **任意格式的本地录音**：iPhone 语音备忘录、wav、mp3、flac、ogg/opus、视频里的音轨等，按内容（ffprobe）识别，不看扩展名，统一转成 16 kHz 单声道。只能用于闭卷，因为旧 Ear 只读 Brain inbox。

真实录音和全部输出默认在本目录 `local-data/`（git 忽略），也可用 `EAR_BASELINES_ROOT` 指定；不得加入版本控制。

## 3. 用法

需要 ffmpeg 和已登录的 `codex` 命令行。凭据和地址放在 `deploy/.env`（模板见 [.env.example](../../deploy/.env.example)）。

```bash
set -a && . deploy/.env && set +a

uv run ear-baselines inventory                  # 全集只读盘点（不复制音频、不重跑）
uv run ear-baselines player                     # 本机录音浏览器 http://127.0.0.1:8790/
uv run ear-baselines octopus --limit 5          # Octopus 录音 → case（含 A2 证据）
uv run ear-baselines import ~/Downloads/memos/  # 任意录音 → case
uv run ear-baselines listen --all               # A1a：gpt-audio 听写（需要 EARbaseline_key）
uv run ear-baselines run a2 --all               # 用 Codex 跑一条路线：a1a / a1b / a1c / a2
uv run ear-baselines list                       # 每个 case 能跑哪些路线、已经跑了几次
```

### 开卷 / 闭卷考卷

在本目录构建镜像（由 Claude 在可用 Docker 的宿主执行）：

```bash
docker build -f docker/Dockerfile -t ear-baselines-exam:0.155.1 .
export EAR_BASELINES_EXAM_PROXY=http://192.168.5.2:7897   # 本机经 Clash 上外网；EARbaseline_key 在 ~/.zshrc
uv run ear-baselines exam prepare first --questions local-data/questions/questions-v0.json --ids B04,B05
uv run ear-baselines exam run closed first --budget-usd 5 --model gpt-6-astra --effort medium --timeout-h 4 --dry-run
uv run ear-baselines exam run closed first --budget-usd 5
uv run ear-baselines exam run open first --budget-usd 5
uv run ear-baselines exam report first
```

全局 `--root DIR` 放在 `exam` 前。`prepare` 的 `--extra-recordings id1,id2` 追加干扰录音，`--force` 替换同名考卷（包括已有运行）。题库为 `{"questions":[{"id":"B04","question":"题目","reference_answer":"参考答案","evidence":[{"recording_id":"r1"}]}]}`；也接受题目数组。证据其他字段原样留在评分答案中。缺转写或同段 context/imu 原始文件即报错，失败的准备不会覆盖已有考卷。

`<root>/exams/<名字>/` 保存来源路径/哈希、题号和录音集合的 `meta.json`、仅评分使用的 `answers_key.json`，以及 `material/`。录音从 inventory 的 `audio_path` 复制原始 `.m4a`，按同段文件名前缀收集 `__context_*.jsonl.gz` / `__imu_*.jsonl.gz`（兼容中间序号），不复制 ASR 和 phoneContext/phoneIMU/richVoice 上传元数据。`info.json` 的开始时间来自 index，统一为 `+08:00`。2026-11-11 Claude 核验：定位（`latitude`/`longitude`/`locationAccuracy`/`locationTimestamp`）、电量、耳机、网络等手机状态都在 `__context_*.jsonl.gz` 的 `context` 字段里；偶尔本段的 context 记在紧随其后的短分段里（从本段结束时刻开始），`prepare` 会一并找到。同目录的 `*_embedding*`（声纹向量）也是处理结果，不复制。

每次运行输出到 `runs/<open|closed>/<UTC 时间>/`：工作区、`events.jsonl`、`isolation.json`、`meter.jsonl`、`run.json`，格式正确的 `answers.json` 另存一份。缺失、题号/证据/把握格式错误会记录为 `missing_or_invalid`。`--dry-run` 只准备工作区及运行记录，打印脱敏命令，不访问 Docker、不读取 auth.json。`report` 对比两组最近一次非 dry-run 的结果及花费，留人工评分列，不自动评分。

预算按[价格表](src/ear_baselines/openai_prices.json)估算；2026-11-11 已核对 [OpenAI 官方价格页](https://developers.openai.com/api/docs/pricing.md)。音频模型计文本/音频 token，转写按音频分钟数估算；未知模型按表内各项最高价。Astra 保守采用标准长上下文价格，缓存不打折。串行结算允许最后一次请求超额，后续返回 429。上游未返回 usage 或连接中断导致费用未知时标记 `uncertain` 并冻结剩余预算。兼容同步 Responses、Chat Completions、音频转写/翻译和 SSE；不支持有独立费用的 hosted tools、后台任务、Realtime 和 Fast/Ultrafast 请求。

`inventory [--out DIR]` 默认输出到 `<EAR_BASELINES_ROOT>/inventory/`：`index.json` 给前端，`index.md` 给人，`transcripts/<id>.md` 保存 ready/incomplete 以及失败但有转写的录音的 Evidence 时间线。输出位于本目录 git 忽略的 `local-data/` 或指定的私有目录，不写旧 Ear 数据目录。只读 Evidence API、旧 Ear SQLite 和 Brain inbox，最多并发 8 个 Evidence 请求，不调用处理接口、不复制或移动录音。SQLite 默认使用本机旧 Ear 路径，可用 `EAR_BASELINES_EAR_DB` 指定其他只读数据库。

盘点按音频 SHA-256 对照全部 capture；未入库的同内容文件合为一段，全部路径保存在 `audio_paths`。状态依次考虑缺失音频、活动任务、最新 run 的失败、② `readiness`；历史 run 的错误不影响当前成功结果。超过 48 小时没有进展且没有未来重试计划的活动任务标为疑似卡住，需人工核实；这是盘点的审计阈值，不是旧 Ear 超时契约。失败分组按每个原因去重录音，同一段可以进入多个组。

GPS/手机状态缺失不阻止 ready，与适配器一致。数据库失败、音频无法探测或 Evidence/数据库 run 不一致会阻止 ready；JSON 保留 `readiness` 和 `missing` 供复核。未入库文件优先用容器录制时间，否则用 inbox 路径时间（假定为显示时区），最后用 mtime，并标明 `started_at_basis`；无法识别的文件列入 `unrecognized_files`，其中可能有损坏音频。API/数据库读取失败会终止，不能把未查到的结果当作零段。

`player [--port 8790]` 是给人听录音用的本机浏览器（只监听 127.0.0.1）：左边按日期列出 `inventory` 里的录音（可筛「可用 / 全部 / 有问题」），中间是当天各段的时间和转写开头，右边播放并同步显示转写、环境声、位置；点一句话跳到那里播放。可以全文搜索转写，给每段写备注（存在 `inventory/notes.json`）。链接 `http://127.0.0.1:8790/#<录音 id>@<秒数>` 直接定位，出题时的证据索引用这个格式。转写从旧 Ear 现取，旧 Ear 不在线时显示 `inventory` 保存的时间线。旧 Ear 自带的界面在 `<EAR_ADAPTER_EAR_URL>/ui/`，偏重证据追溯和人物归属。

旧 Ear 由 Octopus 托管，每次重启端口都会变；连不上时先查新端口，再改 `deploy/.env` 的 `EAR_ADAPTER_EAR_URL`。

每次运行在 `<case>/runs/<路线>/<时间>/` 下保存 Codex 的工作区、`answer.md`、事件流（含工具调用，可以看 A1b / A1c 怎么处理音频）和 `run.json`（模型、推理强度、耗时、token 用量）。

- 目标默认是 [task_default.txt](src/ear_baselines/task_default.txt)：整理活动时间线，并提出想向录音主人确认的问题。`--task` 可换。
- 给模型看的时刻按 `EAR_BASELINES_TZ`（如 `Asia/Shanghai`，默认本机时区）显示。旧 Ear 有时返回 UTC，不换算的话模型会把早上 7 点当成实际时刻。
- 为减少变量，Codex 运行时不加载用户的 config 和 rules，模型和推理强度显式传入（默认 `gpt-6-astra`、`medium`）。`~/.codex/AGENTS.md` 仍会加载，`run.json` 里记录了它是否存在。
- 是否联网由路线决定：只有 A1b 允许联网，录音可能因此被 Codex 传到外部服务。A1b / A1c 的提示要求把中间产物（转写稿、脚本）留在 `work/`，便于复查。

## 4. 实测记录

### 2026-11-11 开卷 / 闭卷首次试跑（`exam`，7 题、8 段录音）

小预算试跑，验证端到端流程和费用，不是正式对比。题目选自已核对的 `questions-v0`：B04、B05、B07（具体会议内容）、B09、B10（小事实）、B13（时间定位）、B17（无法回答）；材料就是这些题的证据录音，共 8 段、3.9 小时，没有干扰录音。两组同时跑，`gpt-6-astra`、`medium`，隔离自检都通过，结束后容器、网络和防火墙规则都清理干净。

| | 开卷（预算 $4） | 闭卷（预算 $6） |
|---|---|---|
| 用时 | 2.5 分钟 | 19.6 分钟 |
| Codex token（输入 / 输出） | 26 万 / 2500 | 485 万（98% 命中缓存）/ 1.1 万 |
| OpenAI 花费（计量代理估算） | $0（只读 `ear/`，没调接口） | $1.65 |
| 怎么做的 | 直接检索旧 Ear 转写 | 先用 whisper-1 转写全部录音，再对关键片段用 gpt-4o-transcribe / mini 复听；没有听环境声，也没用定位 |
| 结果 | 7 题都与参考答案一致 | 7 题都与参考答案一致；两道「无法回答」类都正确拒答 |

结论（只此一次，不能下定论）：

- **这套题区分不出两组。** 题目都写明了日期，材料只有证据录音，都是「听清楚说了什么」的问题；闭卷把录音全转写一遍就能答。要拉开差距，需要跨很多天的习惯和汇总题、大量干扰录音、依赖环境声和定位的题，以及下一阶段的交互场景（第 7 节）。
- **闭卷的费用比预估低。** 实际约 $0.43 / 录音小时（只转写、按需复听）；按此 $21 约够 49 小时，$100 约够 230 小时，全部 176 小时可用录音转写一遍约 $75。若题目需要听环境声，费用会上升。
- 闭卷用时约为开卷 8 倍、Codex token 约 19 倍；计量代理把 OpenAI 调用串行化，闭卷的用时偏长。
- 计量代理记录有 7 次 400（上传格式或大小不被接受），Codex 自己换了切片方式重试。

### 2026-11-10 单段录音试跑（A1a / A1b / A1c / A2，同一段 30 分钟录音）

#### A1c：Codex 实际怎么处理音频

2026-11-10 在一段真实的 30 分钟 Octopus 录音上跑了一次（当时这组还叫 A1b，拆分后改记为 A1c；`gpt-6-astra`、`medium`）。用时 17.6 分钟，写出了答案；输入约 467 万 token（98% 命中缓存），输出约 1.4 万 token。

Codex 的做法：

1. 在本机找 ASR 工具：ffmpeg、whisper、mlx_whisper、FunASR 等都查过，没有现成能用的。
2. 在 `~/.cache/huggingface` 找到 **MOSS-Transcribe-Diarize** 模型（与旧 Ear 用的是同一个模型），在 `~/.cache/uv` 找到 torch、transformers 的已解包文件。离线安装失败后，自己写脚本把缓存里的包链接进新的虚拟环境，在本机加载了 MOSS。
3. 按 2 分钟一段转写整段录音，并算了每段音量；写完初稿后，对听不准的片段截出更短的窗口重新转写、核对措辞。
4. 结束时把 `work/`（转写稿、脚本）和虚拟环境都删了，只留下 `answer.md`。之后在提示里要求保留 `work/`。

由此得到的结论：

- **A1c 的转写质量和 A2 基本同源。** 两者都用 MOSS；差别在切片和质量控制方式（Codex 自己切 2 分钟 vs 旧 Ear 的管线），以及 A1c 没有声纹、环境声、GPS 和手机状态。A1c 测到的是「Codex 自己搭转写流程」的能力，而不是「听音频」的能力。
- **结果依赖本机缓存。** 换一台没有缓存 MOSS 的机器，A1c 很可能转不出来。要比较「纯粹靠 agent 自己想办法」，新 `exam` 的容器不挂宿主缓存，已实测读不到主目录，旧试跑命令保持原样。
- **沙箱只限制写、不限制读。** 为了找工具，Codex 列过主目录、`~/Projects`、`~/.cache` 等目录（看到了无关文件的文件名，没有打开它们）。

#### 同一段录音的 A1b（联网）

同一天紧接着跑了联网的 A1b（同样 `gpt-6-astra`、`medium`）。用时 29 分钟，大部分时间花在下载模型上；输入约 527 万 token（98% 命中缓存），输出约 2 万 token。

- **没有找到、也没有去找本机的 MOSS**，而是联网装了 openai-whisper：先用 `small` 模型转写，再下载 `large-v3-turbo` 复核。下载慢时改成自己写的分段并行下载。
- 联网只用于下载模型和依赖（openaipublic 的 Whisper 模型、一次 HuggingFace 查询），事件流里没有上传录音的命令。
- 这次按要求保留了 `work/`：转写稿、7 段证据音频摘录、脚本和处理说明（`work/README.md`）。但 `work/` 有约 13 GB：其中 9 GB 是它用 `cp -c` 克隆的 uv 缓存（APFS 克隆，实际占用远小于此），模型文件约 3 GB 是实打实的。

两份答案对比（同一段录音，只此一例，不能下结论）：

| | A1c（离线，本机 MOSS） | A1b（联网，Whisper） |
|---|---|---|
| 时间线 | 分成 4 段：开头无法判断、机场广播、安全警示、安检及物品提示 | 只给 1 段，整段写「具体活动无法判断」 |
| 末段约 9 分钟 | 识别出安检、禁带物品等提示 | 没有得到可靠文字，作为信息缺口；Whisper 出现重复套话（幻觉） |
| 确认问题 | 6 个，按时间段对应 | 7 个，大多针对整段 |
| 可复查性 | 中间文件被删 | 转写稿和证据音频都保留 |

在这段嘈杂的中文机场录音上，MOSS 的转写明显比 Whisper 有用。两组都没有把广播内容当成录音主人的行程，也都标出了无法判断的地方。

#### 同一段录音的 A2（三组对比）

随后在同一段录音上跑了 A2（`gpt-6-astra`、`medium`）：用时 1.9 分钟，输入约 10.7 万 token、输出约 2500 token，成本是 A1b / A1c 的约 2%。

| | A2（旧 Ear 证据） | A1c（离线，本机 MOSS） | A1b（联网，Whisper） |
|---|---|---|---|
| 用时 / 输入 token | 1.9 分钟 / 10.7 万 | 17.6 分钟 / 467 万 | 29 分钟 / 527 万 |
| 时间线分段 | 7 段 | 4 段 | 1 段 |
| 用到的线索 | 转写、局部说话人、环境声标签（车辆、柜门、冲水、风噪等）、手机状态、一个定位点 | 只有转写 | 只有转写 |
| 末段 | 安检提示，另外认出一段提到亲属称呼的谈话 | 安检提示 | 缺口 |
| 确认问题 | 7 个，大多针对具体时段和证据来源（例如「冲水声是不是你」） | 6 个，按时间段 | 7 个，大多针对整段 |

A2 能分得更细，主要靠环境声标签和说话人标签，这些是 A1b / A1c 拿不到的；但它对这些标签都标了「可能出错、不能单独证明」，没有直接写成活动。证据里唯一的定位点比录音开始早 25 分钟，A2 正确地没有把它当成全程位置。

只有一段录音，三组都没有打分，以上只是定性观察。

#### 旧 Ear 的 ASR

旧 Ear 自己的 ASR 当时状态正常：远端 MOSS（`lain-linux:8000`）API 就绪、worker 按需休眠、队列为空；旧 Ear 共 433 段录音。A2 可以直接使用。

## 5. 数据外传

开卷和闭卷均允许联网。模型调用音频接口时，原始录音会经计量代理发送到 OpenAI；代理不持久化音频或文本内容，只写请求计量字段。也可能上传到其他外部服务；各组都会把录音内容或转写发给 Codex 背后的模型。录音含真实人名和对话，伦理审查尚未定（[总规范 第 10 节](../../README.md#10-数据与凭据)）。用真实录音跑之前，先确认允许外传的范围。

## 6. 尚未决定

- **怎么打分。** 每题与核对过的标准答案比较；按题型分开统计（具体事实、时间地点、固定习惯、推断、无法回答）；评分时隐去组别。评分脚本还没有。
- **出题偏差。** 根据旧 Ear 转写出的题天然偏向开卷：只考旧 Ear 听到的内容，旧 Ear 的转写错误也可能变成标准答案。需要负责人按记忆补题，并听录音核对答案。
- 每组跑几次（建议至少 3 次）；闭卷处理全部录音的时间（试跑约 5 分钟 / 录音小时，费用见第 4 节）。
- 正式题库怎么拉开差距：跨天习惯和汇总题、干扰录音、环境声和定位题（见第 4 节试跑结论）。
- `exam` 的隔离 2026-11-11 已实测；剩下的已知边界是 Octopus 网关（需登录）经宿主代理可达。旧 `run` 子命令继续只支持单段录音试跑。

## 7. 下一阶段：交互场景（2026-11-11 负责人提出）

目前的题都是**基于音频分析正确性的事实问题**：录音里发生了什么、什么时候、在哪里。下一阶段要比较的是 agent 和用户交互的场景，三条线的设计原则不变（同一批录音、同一套任务、端到端看结果）：

- **用户更新声纹归属**：用户告诉系统「这段里某个声音是谁」或纠正已有归属后，后续回答能不能用上，改动能不能传到别的录音。
- **agent 向用户提问**：agent 在不确定时主动问用户什么、问得是否必要且好回答，得到回答后结论是否改对。
- **发提醒**：从录音里发现的约定、截止时间、待办，能不能在合适的时候提醒用户，且不误报。

这几类都需要模拟或真实的用户回合，评分方法和测试脚本另行设计；当前的 `exam` 只做单轮问答。

工程细节见 [ai-docs/implementation.md](ai-docs/implementation.md)。
