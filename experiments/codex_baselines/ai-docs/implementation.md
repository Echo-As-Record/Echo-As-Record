---
audience: ai
doc-type: guide
based-on: experiments/codex_baselines/README.md；services/adapters（ear_adapter 的 HttpEarSource、InboxAudioLocator、readiness）；codex-cli 0.155.1 与本机 Codex 源码 2026-09-18 快照（models.json 只有 text/image 输入）；OpenAI gpt-audio 模型页（Chat Completions input_audio）；负责人 2026-11-07 确认的 A1a/A1b/A2 分组
---

# Codex 基线实验：实现说明

## 1. 代码

`src/ear_baselines/`

| 文件 | 内容 | 副作用 |
|---|---|---|
| `media.py` | `probe`（ffprobe，按音频流识别）、`normalize`（16 kHz 单声道，.m4a/.mp3/.wav）、`split`（ffmpeg segment） | 子进程、文件 |
| `cases.py` | `Case`、`CaseStore`（实验目录布局）、`discover`、`display_tz` | 文件 |
| `evidence_md.py` | `render_evidence`：旧 Ear Evidence → Markdown 时间线（纯函数） | 无 |
| `octopus.py` | `import_captures`：旧 Ear → case（音频 + A2 证据），复用 ② 的 `readiness` 与 `InboxAudioLocator` | 网络、文件 |
| `openai_audio.py` | `listen`：A1a，切片后逐片发 Chat Completions `input_audio`，`post` 可注入 | 网络、文件 |
| `codex_runner.py` | `prepare` / `codex_command` / `run`：建工作区、跑 `codex exec --json` | 子进程、文件 |
| `task_default.txt` | 默认目标 | 无 |
| `__main__.py` | `ear-baselines` 命令行 | 全部 |

## 2. 约定

- case_id：本地文件 `file-<sha256 前 16 位>`，Octopus `octopus-<capture_id>`。重复导入返回原 case，不重新转码。
- A2 渲染：只用 `original_text` / `original_label`；说话人标签按说话人分离作用域所在的音频块编号（`块N·S1`），跨块不合并；GPS 按（时刻、经纬度）去重；IMU 不渲染；旧 Ear 推断的手机事件标「手机·推断」。
- `a2_evidence.json` 是 pydantic 解析后的结果（`extra="ignore"`），不是旧 Ear 的完整原始响应。
- A1a：时刻用 `display_tz()`；没有录制时间（文件没有 `creation_time`）时用 `+时:分:秒` 偏移。每片的原始回复存 `a1a/reply_NNNN.json`，汇总在 `a1a/meta.json`。HTTP 429/5xx 重试 2 次。
- Codex：`--ignore-user-config --ignore-rules -s workspace-write`，`--network` 时加 `sandbox_workspace_write.network_access=true`。stdin 为 `/dev/null`。失败或超时也写 `run.json`。

## 3. 环境变量

| 变量 | 用途 |
|---|---|
| `EAR_BASELINES_ROOT` | 实验目录，默认 `~/.ear-baselines` |
| `EAR_BASELINES_TZ` | 显示时区（IANA），默认本机 |
| `OPENAI_API_KEY` | A1a |
| `EAR_BASELINES_AUDIO_MODEL`、`EAR_BASELINES_CHUNK_SECONDS` | A1a 模型（默认 `gpt-audio`）和切片秒数（默认 600） |
| `EAR_BASELINES_OPENAI_URL` | 换 Chat Completions 地址（兼容代理） |
| `EAR_BASELINES_CODEX_MODEL` | Codex 模型，默认 `gpt-6-astra` |
| `EAR_ADAPTER_EAR_URL`、`EAR_ADAPTER_EAR_TOKEN`、`EAR_ADAPTER_DATA_ROOT` | 与 ② 共用，见 [② 实现说明 5.2](../../../services/adapters/ai-docs/implementation.md) |

## 4. 验证

自动测试：`uv run pytest experiments/codex_baselines`（ffmpeg 合成音频 + ② 的合成旧 Ear 响应；不调用 OpenAI 和 Codex）。

2026-11-07 人工验证：

- 合成 A2 case 上真实跑了一次 `run a2`（`gpt-6-astra`、`low`）：退出码 0，写出 `answer.md`，67 秒，输入约 6.7 万 token（大部分是 Codex 自身提示词，已缓存）。
- 真实旧 Ear 导出 1 段 30 分钟录音（只看了条数）：7 个音频块，194 条发言、72 条环境声、1 个定位点、1 条手机状态。旧 Ear 返回的是 UTC，因此加了 `EAR_BASELINES_TZ`。
- 未验证：A1a 真实请求（没有 `OPENAI_API_KEY`）、A1b 真实运行、iPhone `.qta` 文件（测试只覆盖 wav/mp3/flac/m4a）。
