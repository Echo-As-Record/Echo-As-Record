---
audience: human
doc-type: guide
---

# Codex 基线实验（A1 / A2）

研究用的对照实验，**不是产品模块**：不经过 ① 存储和 ⑦ 大模型入口，不受 ② 适配器「只做确定计算」的限制，所以放在 `experiments/` 而不是 `services/adapters/`。产品链路的规则见 [设计 v1.2](../../docs/design/design-v1.2.md)。

## 1. 要回答的问题

同一批录音、同一个目标，比较三种做法让 agent 理解一个人的一段时间有多好：

| 组 | 给 Codex 的输入 | 本目录负责 |
|---|---|---|
| A1a | 原始音频先交给能直接听音频的模型（OpenAI `gpt-audio`），它写的「听写记录」 | 是 |
| A1b | 原始音频文件本身，加一个固定目标；Codex 自己决定怎么处理 | 是 |
| A2 | 旧 Ear 的处理结果（MOSS 转写、局部说话人、PANNs 环境声、GPS、手机状态）渲染成的时间线 | 是 |
| 总线链路 | 本仓库 ①～⑦ 的完整链路 | 否，另行实验 |

A1a 和 A2 下游都是 Codex，差别只在「感知」这一步；A1b 测的是 agent 自己处理原始材料的能力。

**为什么 A1 不是「Codex 直接听」**：2026-11 本机的 Codex CLI（0.155）只支持图片附件；源码（2026-09-18 快照）里所有 Codex 模型声明的输入都只有文本和图片。MCP 工具返回音频的通道已经写好，但被模型能力挡住。以后有能听音频的 Codex 模型时，A1b 可以改成直接附音频。

## 2. 样本

- **Octopus 录音**（共同样本）：从旧 Ear 取已完整处理的录音，同时得到原始音频（A1 用）和结构化证据（A2 用）。完整性标准与 ② 适配器相同。
- **任意格式的本地录音**：iPhone 语音备忘录、wav、mp3、flac、ogg/opus、视频里的音轨等，按内容（ffprobe）识别，不看扩展名，统一转成 16 kHz 单声道。只能走 A1，因为旧 Ear 只读 Brain inbox。以后需要时再加一个导入目录送进旧 Ear。

真实录音和全部输出都在仓库外的实验目录（默认 `~/.ear-baselines`），不进仓库。

## 3. 用法

需要 ffmpeg 和已登录的 `codex` 命令行。凭据和地址放在 `deploy/.env`（模板见 [.env.example](../../deploy/.env.example)）。

```bash
set -a && . deploy/.env && set +a

uv run ear-baselines octopus --limit 5          # Octopus 录音 → case（含 A2 证据）
uv run ear-baselines import ~/Downloads/memos/  # 任意录音 → case
uv run ear-baselines listen --all               # A1a：gpt-audio 听写（需要 OPENAI_API_KEY）
uv run ear-baselines run a2 --all               # 用 Codex 跑一条路线：a1a / a1b / a2
uv run ear-baselines list                       # 每个 case 能跑哪些路线、已经跑了几次
```

每次运行在 `<case>/runs/<路线>/<时间>/` 下保存 Codex 的工作区、`answer.md`、事件流（含工具调用，可以看 A1b 怎么处理音频）和 `run.json`（模型、推理强度、耗时、token 用量）。

- 目标默认是 [task_default.txt](src/ear_baselines/task_default.txt)：整理活动时间线，并提出想向录音主人确认的问题。`--task` 可换。
- 给模型看的时刻按 `EAR_BASELINES_TZ`（如 `Asia/Shanghai`，默认本机时区）显示。旧 Ear 有时返回 UTC，不换算的话模型会把早上 7 点当成实际时刻。
- 为减少变量，Codex 运行时不加载用户的 config 和 rules，模型和推理强度显式传入（默认 `gpt-6-astra`、`medium`）。`~/.codex/AGENTS.md` 仍会加载，`run.json` 里记录了它是否存在。
- 沙箱默认禁止联网。`--network` 允许 A1b 自己安装 ASR 工具，但录音可能因此被传到外部服务。

## 4. 数据外传

A1a 把录音上传到 OpenAI；三条路线都会把录音内容或转写发给 Codex 背后的模型。录音含真实人名和对话，伦理审查尚未定（[总规范 第 10 节](../../README.md#10-数据与凭据)）。用真实录音跑之前，先确认允许外传的范围。

## 5. 尚未决定

- **怎么打分。** 候选：(1) 对每段录音预先写好问题和标准答案，比较回答正确率；(2) 由人或另一个模型评价「想确认的问题」的质量（是否问到真正不确定、回答后会改变结果的地方）；(3) 由录音主人对时间线逐条标对错。目前只保存答案，还没有评分脚本。
- 用哪些录音、多少段；A1a 的切片长度（默认 10 分钟）是否影响结果。
- A2 目前只用旧 Ear 的 Evidence，没有用它的场景和时间线（Timeline API）。

工程细节见 [ai-docs/implementation.md](ai-docs/implementation.md)。
