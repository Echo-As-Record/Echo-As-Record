"""纯函数：一段录音的 Evidence → 统一输入格式的原始数据。

不读写 ①、不发网络请求、不读文件；需要的外部数据（声纹向量、音频文件位置）由 runtime 先取好传入。
各类型 payload 的字段说明见 ai-docs/payloads.md。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ear_contracts import NewRawRecord, Source, format_utc

from ear_adapter.config import AdapterConfig
from ear_adapter.ear_api import (
    AudioChunk,
    AudioEvent,
    CaptureEvidence,
    ContextEvent,
    ContextSnapshot,
    Embedding,
    SensorObservation,
    SpeakerResolution,
    SpeechSegment,
)
from ear_adapter.ear_api import Capture as EarCapture
from ear_adapter.ids import deterministic_id

AUDIO_STAGES = ("speech_segment", "audio_event", "speaker_resolution")
"""这些感知阶段都成功跑完（available 或 empty）才导入；failed / partial 是降级结果，不导入。
例外：没有语音时旧 Ear 把 speaker_resolution 标成 missing，这也算完整。
传感和手机上下文有多少导多少。"""


@dataclass(frozen=True, slots=True)
class FileRef:
    path: str
    """相对于 AdapterConfig.data_root，用 / 分隔。"""
    sha256: str
    size: int

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class Ready:
    capture: EarCapture


@dataclass(frozen=True, slots=True)
class Waiting:
    """暂时不能导入，之后会自动重试。"""

    reason: str


@dataclass(frozen=True, slots=True)
class Blocked:
    """需要人处理（例如旧 Ear 的某个阶段失败、结果降级）。

    每轮仍会重读，旧 Ear 重新处理成功后自动导入。
    """

    reason: str


Readiness = Ready | Waiting | Blocked


def utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def readiness(evidence: CaptureEvidence) -> Readiness:
    """判断这段录音现在能不能导入。只导入第一次完整跑完的结果（见 config.ADAPTER_VERSION）。"""
    captures = evidence.of_type("capture", EarCapture)
    if len(captures) != 1:
        return Blocked(f"Evidence 里应有 1 条 capture，实际 {len(captures)} 条")
    if not evidence.analyzable or evidence.ended_at is None:
        return Waiting("感知尚未开始或录音尚未结束")
    states = {a.type: a.state for a in evidence.availability}
    for stage in AUDIO_STAGES:
        state = states.get(stage)
        if state in ("failed", "partial"):
            # 旧 Ear 的某个阶段任务没成功（例如模型加载失败被锁住），
            # run 仍会「跑完」，但结果是降级的。
            # 不导入：同一 adapter_version 只导入一次，导入降级结果就再也换不回来。每轮都会重读，
            # 旧 Ear 修好并重新处理后自动导入。
            what = "失败" if state == "failed" else "部分失败"
            return Blocked(f"旧 Ear 的 {stage} 阶段{what}（降级结果，等待旧 Ear 重新处理）")
        if (
            stage == "speaker_resolution"
            and state == "missing"
            and states.get("speech_segment") == "empty"
        ):
            continue  # 没人说话，旧 Ear 不做说话人分析：完整结果，只是没有转写
        if state not in ("available", "empty"):
            return Waiting(f"{stage} 阶段尚未完成（{state}）")
    resolutions = evidence.of_type("speaker_resolution", SpeakerResolution)
    if states.get("speaker_embedding") == "available" and not any(
        r.embedding_ref for r in resolutions
    ):
        # 旧 Ear 对没有 embedding_readers 授权的服务隐藏 embedding_ref。照常导入会得到
        # 一批没有声纹的数据，而且同一版本不会再导入：必须拦下。
        return Blocked("旧 Ear 有声纹但看不到引用：② 的 token 缺少 embedding_readers 授权")
    return Ready(captures[0])


def capture_key(capture: EarCapture) -> str:
    """一段录音在所有处理批次里都不变的键：设备 + 上游记录 id + 音频哈希。"""
    return f"{capture.instance_id}:{capture.record_id}:{capture.audio_ref.sha256}"


def audio_ref_id(capture: EarCapture, config: AdapterConfig) -> str:
    """这段录音的 audio_ref 的 id。它已存在，说明这段录音已整批导入过。"""
    return deterministic_id(
        utc(capture.started_at), "audio_ref", config.adapter_version, capture_key(capture)
    )


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class _Builder:
    def __init__(self, capture: EarCapture, config: AdapterConfig) -> None:
        self.config = config
        self.source = Source(device=capture.instance_id, channel=config.channel, user=config.user)
        self.records: list[NewRawRecord] = []
        self._keys: set[tuple[str, str]] = set()

    def id_for(self, data_type: str, key: str, start: datetime) -> str:
        return deterministic_id(utc(start), data_type, self.config.adapter_version, key)

    def add(
        self,
        data_type: str,
        key: str,
        start: datetime,
        end: datetime,
        payload: Mapping[str, Any],
    ) -> str:
        record_id = self.id_for(data_type, key, start)
        if (data_type, key) in self._keys:
            return record_id
        self._keys.add((data_type, key))
        self.records.append(
            NewRawRecord(
                id=record_id,
                data_type=data_type,
                event_start=utc(start),
                event_end=utc(end),
                source=self.source,
                adapter_version=self.config.adapter_version,
                dedup_key=key,
                payload=dict(payload),
            )
        )
        return record_id


def _span(items: Iterable[SpeechSegment]) -> tuple[datetime, datetime] | None:
    items = list(items)
    if not items:
        return None
    return min(s.started_at for s in items), max(s.ended_at for s in items)


def capture_records(
    evidence: CaptureEvidence,
    *,
    audio_file: FileRef,
    embeddings: Mapping[str, Embedding],
    config: AdapterConfig,
) -> list[NewRawRecord]:
    """把一段录音转成一批原始数据，顺序为 audio_ref、local_speaker、voiceprint、
    transcript_segment、ambient_sound、gps_point、phone_info。

    embeddings：resolution_id → 声纹向量，必须包含所有带 embedding_ref 的说话人结果。
    """
    state = readiness(evidence)
    if not isinstance(state, Ready):
        raise ValueError(f"录音尚不能导入：{state.reason}")
    capture = state.capture
    assert evidence.ended_at is not None
    capture_end = capture.ended_at or evidence.ended_at
    ckey = capture_key(capture)
    b = _Builder(capture, config)

    chunks = {c.audio_chunk_id: c for c in evidence.of_type("audio_chunk", AudioChunk)}
    segments = sorted(
        evidence.of_type("speech_segment", SpeechSegment),
        key=lambda s: (s.started_at, s.speech_segment_id),
    )
    resolutions = sorted(
        evidence.of_type("speaker_resolution", SpeakerResolution), key=lambda r: r.resolution_id
    )
    events = sorted(
        evidence.of_type("audio_event", AudioEvent), key=lambda e: (e.started_at, e.audio_event_id)
    )

    audio_id = b.add(
        "audio_ref",
        ckey,
        capture.started_at,
        capture_end,
        {
            "file_ref": audio_file.to_json(),
            "media_type": capture.audio_ref.media_type,
            "upstream": {
                "service": "ear",
                "capture_id": capture.capture_id,
                "record_id": capture.record_id,
                "group_id": capture.group_id,
                "pipeline_run_id": evidence.pipeline_run_id,
            },
        },
    )

    # 局部说话人：只在一个分离作用域（旧 Ear 的音频块）内有意义。
    by_speaker: dict[tuple[str, str], list[SpeechSegment]] = {}
    for s in segments:
        by_speaker.setdefault((s.diarization_scope_id, s.local_speaker_id), []).append(s)
    segment_by_id = {s.speech_segment_id: s for s in segments}
    speaker_keys = set(by_speaker) | {
        (r.diarization_scope_id, r.local_speaker_id) for r in resolutions
    }
    speaker_ids: dict[tuple[str, str], str] = {}
    for scope, label in sorted(speaker_keys):
        span = _span(by_speaker.get((scope, label), ()))
        if span is None:
            chunk = chunks.get(scope)
            span = (
                (chunk.started_at, chunk.ended_at) if chunk else (capture.started_at, capture_end)
            )
        speaker_ids[(scope, label)] = b.add(
            "local_speaker",
            f"{ckey}:speaker:{scope}:{label}",
            span[0],
            span[1],
            {
                "audio_ref_id": audio_id,
                "label": label,
                "upstream": {"diarization_scope_id": scope},
            },
        )

    # 转写段 id 先算出来，声纹 payload 要引用它们。
    def transcript_key(s: SpeechSegment) -> str:
        return f"{ckey}:transcript:{s.speech_segment_id}"

    transcript_ids = {
        s.speech_segment_id: b.id_for("transcript_segment", transcript_key(s), s.started_at)
        for s in segments
    }

    voiceprint_by_speaker: dict[tuple[str, str], str] = {}
    for r in resolutions:
        if r.embedding_ref is None:
            continue  # 语音太少或质量不合格，旧 Ear 没有生成声纹
        embedding = embeddings.get(r.resolution_id)
        if embedding is None:
            raise ValueError(f"缺少声纹向量：{r.resolution_id}")
        speaker = (r.diarization_scope_id, r.local_speaker_id)
        span = _span(segment_by_id[i] for i in r.evidence_segment_ids if i in segment_by_id)
        if span is None:
            speaker_record = next(x for x in b.records if x.id == speaker_ids[speaker])
            span = (speaker_record.event_start, speaker_record.event_end)
        voiceprint_id = b.add(
            "voiceprint",
            f"{ckey}:voiceprint:{r.resolution_id}",
            span[0],
            span[1],
            {
                "local_speaker_id": speaker_ids[speaker],
                "audio_ref_id": audio_id,
                "embedding": {
                    "values": embedding.values,
                    "shape": list(embedding.shape),
                    "dtype": embedding.dtype,
                    "provenance": embedding.provenance,
                },
                "sample_quality": r.sample_quality,
                "transcript_segment_ids": [
                    transcript_ids[i] for i in r.evidence_segment_ids if i in transcript_ids
                ],
                "model_version": embedding.model_version,
                "upstream": {"resolution_id": r.resolution_id},
            },
        )
        # 旧 Ear 每个局部说话人只有一个说话人结果；万一有多个，转写段链接第一个。
        voiceprint_by_speaker.setdefault(speaker, voiceprint_id)

    for s in segments:
        speaker = (s.diarization_scope_id, s.local_speaker_id)
        b.add(
            "transcript_segment",
            transcript_key(s),
            s.started_at,
            s.ended_at,
            {
                "text": s.original_text,
                "language": s.language,
                "local_speaker_id": speaker_ids[speaker],
                "voiceprint_id": voiceprint_by_speaker.get(speaker),
                "overlap": s.overlap,
                "audio_ref_id": audio_id,
                "model_version": s.model_version,
                "upstream": {
                    "speech_segment_id": s.speech_segment_id,
                    "audio_chunk_id": s.audio_chunk_id,
                },
            },
        )

    for e in events:
        b.add(
            "ambient_sound",
            f"{ckey}:ambient:{e.audio_event_id}",
            e.started_at,
            e.ended_at,
            {
                "label": e.original_label,
                "taxonomy": e.taxonomy,
                "audio_ref_id": audio_id,
                "model_version": e.model_version,
                "upstream": {"audio_event_id": e.audio_event_id},
            },
        )

    device = capture.instance_id
    observations = sorted(
        evidence.of_type("sensor_observation", SensorObservation),
        key=lambda o: (o.observed_at, o.sensor_observation_id),
    )
    for o in observations:
        if o.location is None:
            continue  # IMU 等不在 MVP 的原始数据类型里，原始数据留在上游文件
        at = utc(o.observed_at)
        loc = o.location
        b.add(
            "gps_point",
            f"{device}:gps:{format_utc(at)}:{loc.latitude}:{loc.longitude}",
            at,
            at,
            {
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "accuracy_m": loc.accuracy_m,
                "upstream": {"sensor_observation_id": o.sensor_observation_id},
            },
        )

    snapshots = sorted(
        evidence.of_type("context_snapshot", ContextSnapshot),
        key=lambda c: (c.observed_at, c.context_snapshot_id),
    )
    for c in snapshots:
        at = utc(c.observed_at)
        b.add(
            "phone_info",
            f"{device}:phone_info:snapshot:{format_utc(at)}:{_digest(c.state)}",
            at,
            at,
            {
                "kind": "snapshot",
                "state": c.state,
                "upstream": {"context_snapshot_id": c.context_snapshot_id},
            },
        )
    context_events = sorted(
        evidence.of_type("context_event", ContextEvent),
        key=lambda c: (c.occurred_at, c.context_event_id),
    )
    for c in context_events:
        if c.inferred:
            continue  # 旧 Ear 推断出来的事件不是确定计算的结果，不算原始数据
        at = utc(c.occurred_at)
        b.add(
            "phone_info",
            f"{device}:phone_info:event:{format_utc(at)}:{c.event_type}:{_digest(c.details)}",
            at,
            at,
            {
                "kind": "event",
                "event_type": c.event_type,
                "details": c.details,
                "upstream": {"context_event_id": c.context_event_id},
            },
        )

    return b.records
