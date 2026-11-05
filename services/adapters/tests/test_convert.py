from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from ear_adapter import AdapterConfig, Blocked, Ready, Waiting, capture_records, readiness
from ear_adapter.ear_api import CaptureEvidence, Embedding
from ear_adapter.ids import deterministic_id
from ear_adapter.testing import AUDIO_FILE, embedding_json
from ear_contracts import NewRawRecord, is_id

MakeEvidence = Callable[..., dict[str, Any]]


def _convert(data: dict[str, Any], config: AdapterConfig) -> list[NewRawRecord]:
    evidence = CaptureEvidence.model_validate(data)
    embeddings = {
        rid: Embedding.model_validate(embedding_json(rid)) for rid in ("run-1-res-1", "run-1-res-3")
    }
    return capture_records(evidence, audio_file=AUDIO_FILE, embeddings=embeddings, config=config)


def _by_type(records: list[NewRawRecord]) -> dict[str, list[NewRawRecord]]:
    out: dict[str, list[NewRawRecord]] = {}
    for r in records:
        out.setdefault(r.data_type, []).append(r)
    return out


def test_deterministic_id_is_uuid7_and_stable() -> None:
    at = datetime(2026, 10, 26, 1, 55, 38, tzinfo=UTC)
    a = deterministic_id(at, "voiceprint", "ear-audio/0.1.0", "k")
    assert is_id(a)
    assert a == deterministic_id(at, "voiceprint", "ear-audio/0.1.0", "k")
    assert a != deterministic_id(at, "voiceprint", "ear-audio/0.1.0", "k2")
    assert a != deterministic_id(at, "voiceprint", "ear-audio/0.2.0", "k")
    assert int(a.replace("-", "")[:12], 16) == int(at.timestamp() * 1000)


def test_readiness(make_evidence: MakeEvidence) -> None:
    data = make_evidence()
    assert isinstance(readiness(CaptureEvidence.model_validate(data)), Ready)

    data["analyzable"] = False
    assert isinstance(readiness(CaptureEvidence.model_validate(data)), Waiting)

    data = make_evidence()
    data["availability"][2]["state"] = "missing"
    assert isinstance(readiness(CaptureEvidence.model_validate(data)), Waiting)

    # 降级结果（旧 Ear 某阶段任务失败，例如模型加载失败被锁住）不导入
    for state in ("failed", "partial"):
        data["availability"][2]["state"] = state
        blocked = readiness(CaptureEvidence.model_validate(data))
        assert isinstance(blocked, Blocked)
        assert "降级" in blocked.reason

    # 旧 Ear 有声纹，但 token 没有声纹授权，引用被隐藏：拦下，不导入没有声纹的一批
    data = make_evidence()
    data["availability"].append({"type": "speaker_embedding", "state": "available"})
    assert isinstance(readiness(CaptureEvidence.model_validate(data)), Ready)
    for item in data["evidence"]:
        if item["ref"]["type"] == "speaker_resolution":
            item["payload"]["embedding_ref"] = None
    hidden = readiness(CaptureEvidence.model_validate(data))
    assert isinstance(hidden, Blocked)
    assert "embedding_readers" in hidden.reason

    # 没人说话：speech 为 empty 时 speaker_resolution 为 missing，仍是完整结果
    data = make_evidence()
    data["availability"][0]["state"] = "empty"
    data["availability"][2]["state"] = "missing"
    assert isinstance(readiness(CaptureEvidence.model_validate(data)), Ready)

    # 传感、上下文阶段缺失不影响导入
    data = make_evidence()
    data["availability"].append({"type": "sensor_observation", "state": "missing"})
    assert isinstance(readiness(CaptureEvidence.model_validate(data)), Ready)


def test_record_counts(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    records = _convert(make_evidence(), config)
    assert Counter(r.data_type for r in records) == {
        "audio_ref": 1,
        "local_speaker": 3,  # 局部说话人只在一个音频块内有意义：chk-1 的 S1、S2，chk-2 的 S1
        "voiceprint": 2,  # S2 语音太少，没有声纹
        "transcript_segment": 3,
        "ambient_sound": 1,
        "gps_point": 1,  # 重复的定位点去重，IMU 跳过
        "phone_info": 2,  # 一条快照 + 一条非推断的状态变化；推断出的事件跳过
    }


def test_times_are_utc(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    records = _by_type(_convert(make_evidence(), config))
    audio = records["audio_ref"][0]
    assert audio.event_start == datetime(2026, 10, 26, 1, 55, 38, tzinfo=UTC)
    assert audio.event_end == datetime(2026, 10, 26, 2, 25, 38, tzinfo=UTC)
    gps = records["gps_point"][0]
    assert gps.event_start == gps.event_end == datetime(2026, 10, 26, 1, 55, 40, tzinfo=UTC)


def test_payloads_and_references(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    records = _by_type(_convert(make_evidence(), config))
    audio = records["audio_ref"][0]
    assert audio.payload["file_ref"] == AUDIO_FILE.to_json()
    speakers = {r.id: r for r in records["local_speaker"]}
    voiceprints = {r.id: r for r in records["voiceprint"]}
    transcripts = records["transcript_segment"]
    transcript_ids = {r.id for r in transcripts}

    # 转写用模型原始输出，不用旧 Ear 里用户纠正过的文本
    assert [t.payload["text"] for t in transcripts] == ["合成发言一", "合成发言二", "合成发言三"]
    for t in transcripts:
        assert t.payload["audio_ref_id"] == audio.id
        assert t.payload["local_speaker_id"] in speakers
    # 没有声纹的说话人：voiceprint_id 为 None
    assert [t.payload["voiceprint_id"] is None for t in transcripts] == [False, True, False]
    for v in voiceprints.values():
        assert v.payload["local_speaker_id"] in speakers
        assert set(v.payload["transcript_segment_ids"]) <= transcript_ids
        assert v.payload["embedding"]["values"] == [0.1, 0.2, 0.3]
    for t in transcripts:
        assert t.payload["voiceprint_id"] is None or t.payload["voiceprint_id"] in voiceprints
    assert {r.payload["kind"] for r in records["phone_info"]} == {"snapshot", "event"}


def test_conversion_is_deterministic(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    assert _convert(make_evidence(), config) == _convert(make_evidence(), config)


def test_missing_embedding_is_an_error(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    evidence = CaptureEvidence.model_validate(make_evidence())
    with pytest.raises(ValueError, match="缺少声纹向量"):
        capture_records(evidence, audio_file=AUDIO_FILE, embeddings={}, config=config)


def test_not_ready_capture_is_rejected(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    data = make_evidence()
    data["analyzable"] = False
    with pytest.raises(ValueError, match="尚不能导入"):
        _convert(data, config)
