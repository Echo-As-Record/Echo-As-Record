"""测试与开发用：合成的旧 Ear Evidence API 响应（不含真实数据）和测试替身。

形状按旧 Ear 的 ear/models/evidence_read.py；时间用 +09:00，用来检查 UTC 换算。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

from ear_adapter.convert import FileRef
from ear_adapter.ear_api import Capture, CaptureEvidence, CaptureSummary, Embedding
from ear_adapter.source import SourceUnavailable

AUDIO_BYTES = b"synthetic m4a bytes"
AUDIO_SHA = hashlib.sha256(AUDIO_BYTES).hexdigest()


def _item(kind: str, ident: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": {"type": kind, "id": ident},
        "capture_id": "cap-1",
        "status": "available",
        "payload": payload,
    }


def _artifact(ident: str, sha: str = AUDIO_SHA, size: int = len(AUDIO_BYTES)) -> dict[str, Any]:
    return {
        "artifact_id": ident,
        "checksum": f"sha256:{sha}",
        "size_bytes": size,
        "media_type": "audio/mp4",
        "created_at": "2026-10-26T12:00:00+09:00",
    }


def _segment(
    ident: str,
    chunk: str,
    speaker: str,
    start: str,
    end: str,
    text: str,
    corrected: str | None = None,
) -> dict[str, Any]:
    return _item(
        "speech_segment",
        ident,
        {
            "speech_segment_id": ident,
            "audio_chunk_id": chunk,
            "start_offset_ms": 0,
            "end_offset_ms": 1000,
            "started_at": start,
            "ended_at": end,
            "text": corrected or text,
            "original_text": text,
            "language": "zh",
            "diarization_scope_id": chunk,
            "local_speaker_id": speaker,
            "asr_confidence": 0.9,
            "overlap": False,
            "model_version": "moss/test",
            "created_at": end,
        },
    )


def _resolution(
    ident: str, chunk: str, speaker: str, segments: list[str], embedded: bool
) -> dict[str, Any]:
    return _item(
        "speaker_resolution",
        ident,
        {
            "resolution_id": ident,
            "diarization_scope_id": chunk,
            "local_speaker_id": speaker,
            "evidence_segment_ids": segments,
            "embedding_ref": _artifact(f"emb-{ident}", "0" * 64, 768) if embedded else None,
            "embedding_status": "generated" if embedded else "not_generated",
            "sample_quality": 0.8 if embedded else None,
            "model_version": "ecapa/test",
            "created_at": "2026-10-26T12:00:00+09:00",
        },
    )


def evidence_json(run: str = "run-1") -> dict[str, Any]:
    """一段 30 分钟的录音：两个音频块、三个局部说话人（其中一个没有声纹）。"""
    return {
        "view": "current",
        "capture_id": "cap-1",
        "snapshot_id": None,
        "input_digest": "d",
        "pipeline_run_id": run,
        "run_revision": 1,
        "projection_version": 2,
        "started_at": "2026-10-26T10:55:38+09:00",
        "ended_at": "2026-10-26T11:25:38+09:00",
        "analyzable": True,
        "availability": [
            {"type": "speech_segment", "state": "available"},
            {"type": "audio_event", "state": "available"},
            {"type": "speaker_resolution", "state": "available"},
            {"type": "context_event", "state": "missing"},
        ],
        "evidence": [
            _item(
                "capture",
                "cap-1",
                {
                    "capture_id": "cap-1",
                    "source": "brain",
                    "instance_id": "ios-test",
                    "record_id": "rec-1",
                    "group_id": "GROUP-1",
                    "started_at": "2026-10-26T10:55:38+09:00",
                    "ended_at": "2026-10-26T11:25:38+09:00",
                    "processing_status": "completed",
                    "audio_ref": _artifact("art-audio"),
                    "sensor_artifact_refs": [],
                    "source_record_ref": _artifact("art-record", "1" * 64, 10),
                },
            ),
            _item(
                "audio_chunk",
                "chk-1",
                {
                    "audio_chunk_id": "chk-1",
                    "capture_id": "cap-1",
                    "started_at": "2026-10-26T10:55:38+09:00",
                    "ended_at": "2026-10-26T11:10:38+09:00",
                    "model_version": "clip/test",
                },
            ),
            _item(
                "audio_chunk",
                "chk-2",
                {
                    "audio_chunk_id": "chk-2",
                    "capture_id": "cap-1",
                    "started_at": "2026-10-26T11:10:38+09:00",
                    "ended_at": "2026-10-26T11:25:38+09:00",
                    "model_version": "clip/test",
                },
            ),
            _segment(
                f"{run}-seg-1",
                "chk-1",
                "S1",
                "2026-10-26T10:56:00+09:00",
                "2026-10-26T10:56:05+09:00",
                "合成发言一",
                corrected="用户纠正过的文本",
            ),
            _segment(
                f"{run}-seg-2",
                "chk-1",
                "S2",
                "2026-10-26T10:57:00+09:00",
                "2026-10-26T10:57:02+09:00",
                "合成发言二",
            ),
            _segment(
                f"{run}-seg-3",
                "chk-2",
                "S1",
                "2026-10-26T11:12:00+09:00",
                "2026-10-26T11:12:09+09:00",
                "合成发言三",
            ),
            _resolution(f"{run}-res-1", "chk-1", "S1", [f"{run}-seg-1"], embedded=True),
            _resolution(f"{run}-res-2", "chk-1", "S2", [f"{run}-seg-2"], embedded=False),
            _resolution(f"{run}-res-3", "chk-2", "S1", [f"{run}-seg-3"], embedded=True),
            _item(
                "audio_event",
                f"{run}-evt-1",
                {
                    "audio_event_id": f"{run}-evt-1",
                    "audio_chunk_id": "chk-1",
                    "start_offset_ms": 0,
                    "end_offset_ms": 3000,
                    "started_at": "2026-10-26T10:58:00+09:00",
                    "ended_at": "2026-10-26T10:58:03+09:00",
                    "label": "Keyboard",
                    "original_label": "Keyboard",
                    "confidence": 0.7,
                    "taxonomy": "audioset",
                    "model_version": "panns/test",
                    "created_at": "2026-10-26T12:00:00+09:00",
                },
            ),
            _item(
                "sensor_observation",
                "obs-gps-1",
                {
                    "sensor_observation_id": "obs-gps-1",
                    "capture_id": "cap-1",
                    "kind": "location",
                    "observed_at": "2026-10-26T10:55:40+09:00",
                    "artifact_ref": _artifact("a1"),
                    "location": {"latitude": 35.0, "longitude": 139.0, "accuracy_m": 12.0},
                },
            ),
            # 同一个定位点在相邻文件里出现两次（开始、结束快照重叠），应去重成一条。
            _item(
                "sensor_observation",
                "obs-gps-2",
                {
                    "sensor_observation_id": "obs-gps-2",
                    "capture_id": "cap-1",
                    "kind": "location",
                    "observed_at": "2026-10-26T10:55:40+09:00",
                    "artifact_ref": _artifact("a2"),
                    "location": {"latitude": 35.0, "longitude": 139.0, "accuracy_m": 12.0},
                },
            ),
            _item(
                "sensor_observation",
                "obs-imu-1",
                {
                    "sensor_observation_id": "obs-imu-1",
                    "capture_id": "cap-1",
                    "kind": "imu",
                    "observed_at": "2026-10-26T10:55:38+09:00",
                    "artifact_ref": _artifact("a3"),
                    "location": None,
                },
            ),
            _item(
                "context_snapshot",
                "ctx-1",
                {
                    "context_snapshot_id": "ctx-1",
                    "capture_id": "cap-1",
                    "observed_at": "2026-10-26T10:55:38+09:00",
                    "state": {"battery_percent": 80, "is_charging": False, "is_locked": True},
                    "artifact_ref": _artifact("a4"),
                    "source_line_index": 0,
                },
            ),
            _item(
                "context_event",
                "cev-1",
                {
                    "context_event_id": "cev-1",
                    "capture_id": "cap-1",
                    "event_type": "lock",
                    "occurred_at": "2026-10-26T11:00:00+09:00",
                    "details": {"kind": "boolean", "field": "lock", "before": True, "after": False},
                    "confidence": 1.0,
                    "inferred": False,
                    "time_uncertainty_ms": 0,
                    "artifact_ref": _artifact("a5"),
                    "source_line_index": 1,
                    "model_version": "x",
                },
            ),
            _item(
                "context_event",
                "cev-2",
                {
                    "context_event_id": "cev-2",
                    "capture_id": "cap-1",
                    "event_type": "music",
                    "occurred_at": "2026-10-26T11:05:00+09:00",
                    "details": {
                        "kind": "boolean",
                        "field": "music",
                        "before": False,
                        "after": True,
                    },
                    "confidence": 0.6,
                    "inferred": True,
                    "time_uncertainty_ms": 5000,
                    "artifact_ref": _artifact("a6"),
                    "source_line_index": 2,
                    "model_version": "x",
                },
            ),
        ],
    }


def embedding_json(resolution_id: str) -> dict[str, Any]:
    return {
        "ref": {"type": "speaker_resolution", "id": resolution_id},
        "capture_id": "cap-1",
        "artifact": _artifact("emb", "0" * 64, 768),
        "model_version": "ecapa/test",
        "evidence_refs": [],
        "shape": [3],
        "dtype": "float32",
        "values": [0.1, 0.2, 0.3],
        "provenance": {"normalization": "l2"},
    }


AUDIO_FILE = FileRef(
    path="inbox/brain/ios-test/2026-10-26/phone/10-55-38__GROUP-1/segment_0001.m4a",
    sha256=AUDIO_SHA,
    size=len(AUDIO_BYTES),
)


class FakeSource:
    def __init__(self, captures: dict[str, dict[str, Any]]) -> None:
        self.captures = captures
        self.down = False
        self.embedding_reads = 0

    def list_captures(self) -> Iterator[CaptureSummary]:
        if self.down:
            raise SourceUnavailable("连不上旧 Ear")
        for ident in self.captures:
            yield CaptureSummary(capture_id=ident)

    def read_capture(self, capture_id: str) -> CaptureEvidence:
        if self.down:
            raise SourceUnavailable("连不上旧 Ear")
        return CaptureEvidence.model_validate(self.captures[capture_id])

    def read_embedding(self, resolution_id: str) -> Embedding:
        self.embedding_reads += 1
        return Embedding.model_validate(embedding_json(resolution_id))


class FakeLocator:
    def __init__(self, found: bool = True) -> None:
        self.found = found

    def locate(self, capture: Capture) -> FileRef | None:
        return AUDIO_FILE if self.found else None
