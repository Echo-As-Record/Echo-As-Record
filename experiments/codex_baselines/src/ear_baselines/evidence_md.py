"""A2：把旧 Ear 一段录音的 Evidence 渲染成一份按时间排序的 Markdown，交给 Codex 读。

纯函数，不读网络和文件。只用模型原始输出（original_text / original_label），不用旧 Ear 里
用户纠正过的值，和 ② 适配器的取舍一致；旧 Ear 推断出的手机事件保留，但标明「推断」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from ear_adapter.ear_api import (
    AudioChunk,
    AudioEvent,
    CaptureEvidence,
    ContextEvent,
    ContextSnapshot,
    SensorObservation,
    SpeakerResolution,
    SpeechSegment,
)


@dataclass(frozen=True, slots=True)
class _Line:
    start: datetime
    end: datetime | None
    order: int
    """同一时刻的排序：手机状态、位置、环境声、发言。"""
    tag: str
    text: str


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict | list):
        return str(value)
    return str(value)


def _fmt_state(state: dict[str, Any]) -> str:
    return "，".join(f"{k}={_fmt_value(v)}" for k, v in sorted(state.items()))


def _fmt_event(event: ContextEvent) -> str:
    d = event.details
    if {"before", "after"} <= d.keys():
        return f"{event.event_type}：{_fmt_value(d['before'])} → {_fmt_value(d['after'])}"
    return f"{event.event_type}：{_fmt_state(d)}"


def _speaker_labels(evidence: CaptureEvidence) -> dict[str, int]:
    chunks = sorted(evidence.of_type("audio_chunk", AudioChunk), key=lambda c: c.started_at)
    return {c.audio_chunk_id: i for i, c in enumerate(chunks, start=1)}


def render_evidence(evidence: CaptureEvidence, tz: tzinfo | None = None) -> str:
    """tz：显示用的时区；None 时沿用旧 Ear 返回的时区。"""
    tz = tz or evidence.started_at.tzinfo
    chunk_index = _speaker_labels(evidence)
    lines: list[_Line] = []

    for c in evidence.of_type("context_snapshot", ContextSnapshot):
        lines.append(_Line(c.observed_at, None, 0, "手机", f"状态：{_fmt_state(c.state)}"))
    for e in evidence.of_type("context_event", ContextEvent):
        tag = "手机·推断" if e.inferred else "手机"
        lines.append(_Line(e.occurred_at, None, 0, tag, _fmt_event(e)))

    seen_gps: set[tuple[datetime, float, float]] = set()
    for o in evidence.of_type("sensor_observation", SensorObservation):
        if o.location is None:
            continue  # IMU 等只有原始文件引用，没有可读内容
        key = (o.observed_at, o.location.latitude, o.location.longitude)
        if key in seen_gps:
            continue
        seen_gps.add(key)
        acc = f"（±{o.location.accuracy_m:g} m）" if o.location.accuracy_m is not None else ""
        lines.append(
            _Line(
                o.observed_at,
                None,
                1,
                "位置",
                f"{o.location.latitude:.5f}, {o.location.longitude:.5f}{acc}",
            )
        )

    for a in evidence.of_type("audio_event", AudioEvent):
        lines.append(_Line(a.started_at, a.ended_at, 2, "环境声", a.original_label))

    segments = evidence.of_type("speech_segment", SpeechSegment)
    # 说话人分离的作用域通常就是音频块；按发言所在的块编号，声纹结果也用同一编号。
    scope_index = {s.diarization_scope_id: chunk_index.get(s.audio_chunk_id) for s in segments}

    def speaker_label(scope: str, speaker: str) -> str:
        index = scope_index.get(scope) or chunk_index.get(scope)
        return f"块{index if index is not None else '?'}·{speaker}"

    for s in segments:
        speaker = speaker_label(s.diarization_scope_id, s.local_speaker_id)
        overlap = "（与他人重叠）" if s.overlap else ""
        lines.append(_Line(s.started_at, s.ended_at, 3, speaker, s.original_text + overlap))

    lines.sort(key=lambda x: (x.start, x.order, x.tag, x.text))

    def local(t: datetime) -> datetime:
        return t.astimezone(tz) if tz is not None else t

    start = local(evidence.started_at)
    end = local(evidence.ended_at) if evidence.ended_at else None
    offset = start.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "未知时区"
    span = f"{start:%Y-%m-%d %H:%M:%S} – {end:%H:%M:%S}" if end else f"{start:%Y-%m-%d %H:%M:%S} 起"

    voiced = {
        speaker_label(r.diarization_scope_id, r.local_speaker_id)
        for r in evidence.of_type("speaker_resolution", SpeakerResolution)
        if r.embedding_ref is not None
    }
    availability = "，".join(f"{a.type}={a.state}" for a in evidence.availability)

    out = [
        "# 录音证据（旧 Ear 处理结果）",
        "",
        f"- 录音时间：{span}（{offset}，下面的时刻都是这个时区）",
        f"- 音频块：{len(chunk_index)} 个；说话人标签只在同一块内有效，"
        "「块1·S1」和「块2·S1」不一定是同一个人",
        f"- 有声纹向量的说话人：{'、'.join(sorted(voiced)) if voiced else '无'}"
        "（向量本身不在这里）",
        f"- 各阶段结果：{availability}",
        "- 来源：转写和说话人分离来自 MOSS，环境声来自 PANNs（AudioSet 标签），"
        "位置和手机状态来自手机传感；都是模型或传感器的原始输出，可能有错",
        "",
        "## 时间线",
        "",
    ]
    current_day = None
    for line in lines:
        s = local(line.start)
        if s.date() != current_day:
            current_day = s.date()
            out.append(f"### {current_day.isoformat()}")
            out.append("")
        when = f"{s:%H:%M:%S}"
        if line.end is not None and line.end - line.start >= timedelta(seconds=1):
            when += f"–{local(line.end):%H:%M:%S}"
        out.append(f"- {when} [{line.tag}] {line.text}")
    if not lines:
        out.append("（没有任何转写、环境声或传感数据）")
    out.append("")
    return "\n".join(out)
