"""A1a：把原始音频交给能直接听音频的模型（默认 OpenAI gpt-audio，Chat Completions 的
input_audio），让它写「听写记录」，再把记录交给 Codex。

下游统一用 Codex，A1a 和 A2 的差别只在「感知」这一步：模型自己听 vs 旧 Ear 的管线。
长录音按 EAR_BASELINES_CHUNK_SECONDS（默认 600 秒）切片，逐片请求，按顺序拼起来。

凭据：EARbaseline_key（放在 shell 配置如 ~/.zshrc，不进仓库）。故意不用 OPENAI_API_KEY：codex CLI
看到它可能改走 API key 计费。录音会上传到 OpenAI，真实录音能否外传要先按团队的数据规定确认。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from ear_baselines import media
from ear_baselines.cases import (
    A1A_LISTENING,
    AUDIO_NAME,
    Case,
    CaseStore,
    display_tz,
    write_json,
)

DEFAULT_MODEL = "gpt-audio"
DEFAULT_CHUNK_SECONDS = 600.0
API_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = """你在听一个人随身录下的日常录音。
任务是把听到的内容如实记下来，供之后整理这个人的活动。
只记录音频里实际听到的，不猜测、不补全；听不清就写「听不清」。"""

CHUNK_PROMPT = """这是一段录音的第 {index}/{total} 段，从 {start} 到 {end}。
请写这一段的听写记录（Markdown），包括：
1. 逐句转写：每句一行，格式「- 时刻 [说话人] 内容」。时刻用上面的时间体系；说话人按声音区分，
   记为 A、B、C…（只在本段内有效），能听出是录音者本人的标「本人」。
2. 非语音声音：每条一行，「- 时刻 [声音] 描述」，例如键盘、车流、餐具、广播、音乐。
3. 一两句话概括这一段的场景（在哪类地方、在做什么），并说明依据。
没有任何人声或声音时，说明「本段基本无声」。"""

Post = Callable[[dict[str, Any]], dict[str, Any]]


def _http_post(api_key: str, timeout: float = 600.0) -> Post:
    def post(body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            os.environ.get("EAR_BASELINES_OPENAI_URL", API_URL),
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI 返回 {exc.code}：{detail}") from exc
        raise AssertionError("unreachable")

    return post


def _clock(case: Case, offset_s: float, tz: tzinfo) -> str:
    if case.started_at is not None:
        return (case.started_at + timedelta(seconds=offset_s)).astimezone(tz).strftime("%H:%M:%S")
    total = round(offset_s)
    return f"+{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _header(case: Case, model: str, chunk_seconds: float, tz: tzinfo) -> list[str]:
    if case.started_at is not None:
        start: datetime = case.started_at.astimezone(tz)
        offset = start.strftime("%z")
        when = f"{start:%Y-%m-%d %H:%M:%S}（UTC{offset[:3]}:{offset[3:]}，下面的时刻都是这个时区）"
    else:
        when = "未知（文件里没有录制时间；下面的时刻是相对录音开头的偏移，写作 +时:分:秒）"
    return [
        "# 录音听写记录（模型直接听原始音频）",
        "",
        f"- 录音开始：{when}",
        f"- 时长：{case.duration_s / 60:.1f} 分钟",
        f"- 听写模型：{model}；按 {chunk_seconds / 60:g} 分钟一段分别听写，"
        "说话人 A、B… 只在同一段内有效",
        "- 这是模型的听写结果，可能有错听和漏听",
        "",
    ]


def listen(
    store: CaseStore,
    case: Case,
    *,
    post: Post | None = None,
    model: str | None = None,
    chunk_seconds: float | None = None,
    tz: tzinfo | None = None,
) -> Path:
    """为一个 case 生成 a1a_listening.md，并把每段的请求元数据和原始回复存到 a1a/。"""
    model = model or os.environ.get("EAR_BASELINES_AUDIO_MODEL", DEFAULT_MODEL)
    chunk_seconds = chunk_seconds or float(
        os.environ.get("EAR_BASELINES_CHUNK_SECONDS", DEFAULT_CHUNK_SECONDS)
    )
    if post is None:
        key = os.environ.get("EARbaseline_key")  # noqa: SIM112 负责人指定的名字
        if not key:
            raise RuntimeError("需要环境变量 EARbaseline_key（放在 deploy/.env）")
        post = _http_post(key)

    tz = tz or display_tz()
    d = store.dir(case.case_id)
    work = d / "a1a"
    chunks = media.split(d / AUDIO_NAME, work / "chunks", chunk_seconds, suffix=".mp3")
    out = _header(case, model, chunk_seconds, tz)
    usage: list[Any] = []
    for i, chunk in enumerate(chunks):
        start_s = i * chunk_seconds
        end_s = min(case.duration_s, start_s + chunk_seconds)
        prompt = CHUNK_PROMPT.format(
            index=i + 1,
            total=len(chunks),
            start=_clock(case, start_s, tz),
            end=_clock(case, end_s, tz),
        )
        body = {
            "model": model,
            "modalities": ["text"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(chunk.read_bytes()).decode(),
                                "format": "mp3",
                            },
                        },
                    ],
                },
            ],
        }
        reply = post(body)
        write_json(work / f"reply_{i + 1:04d}.json", reply)
        usage.append(reply.get("usage"))
        text = reply["choices"][0]["message"].get("content") or "（模型没有返回文字）"
        out += [
            f"## 第 {i + 1} 段：{_clock(case, start_s, tz)} – {_clock(case, end_s, tz)}",
            "",
            text,
            "",
        ]
    write_json(
        work / "meta.json",
        {"model": model, "chunk_seconds": chunk_seconds, "chunks": len(chunks), "usage": usage},
    )
    path = d / A1A_LISTENING
    path.write_text("\n".join(out), encoding="utf-8")
    return path
