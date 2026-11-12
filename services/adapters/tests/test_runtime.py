from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from ear_adapter import (
    AdapterConfig,
    AlreadyIngested,
    Failed,
    InboxAudioLocator,
    Ingested,
    Pending,
    Runner,
    capture_records,
    ingest_capture,
)
from ear_adapter.ear_api import Capture, CaptureEvidence
from ear_adapter.testing import AUDIO_BYTES, AUDIO_FILE, AUDIO_SHA, FakeLocator, FakeSource
from ear_contracts import DataChanged
from ear_storage_client import AppendRaw, Context, MemoryStorage, PermissionDenied, Query
from ear_storage_client.testing import make_raw

MakeEvidence = Callable[..., dict[str, Any]]
REPO = Path(__file__).resolve().parents[3]
RAW_SCHEMA = json.loads((REPO / "contracts/records/raw-record.schema.json").read_text())


def _runner(
    storage: MemoryStorage, source: FakeSource, config: AdapterConfig, *, found: bool = True
) -> Runner:
    return Runner(
        source=source, storage=storage.client("adapter"), locator=FakeLocator(found), config=config
    )


def _count(storage: MemoryStorage, data_type: str) -> int:
    return storage.client("adapter").count(Query(data_type))


def test_ingest_writes_one_atomic_batch(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    storage = MemoryStorage()
    report = _runner(storage, FakeSource({"cap-1": make_evidence()}), config).run_once()
    [outcome] = report.outcomes
    assert isinstance(outcome, Ingested)
    assert sum(outcome.written.values()) == 13
    batches = {e.batch_id for e in storage.events if isinstance(e, DataChanged)}
    assert len(batches) == 1
    # 每条都通过 contracts 里的 raw-record Schema
    reader = storage.client("adapter")
    for data_type in outcome.written:
        for item in reader.query(Query(data_type, limit=1000)).items:
            jsonschema.validate(item.model_dump(mode="json"), RAW_SCHEMA)


def test_voiceprint_event_arrives_after_its_local_speaker(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    """③ 收到「新增声纹」时，声纹引用的局部说话人和录音都已经能读到。"""
    storage = MemoryStorage(duplicate_delivery=True)
    attribution = storage.client("attribution")
    seen: list[str] = []

    def on_event(event: object) -> None:
        if isinstance(event, DataChanged) and event.data_type == "voiceprint":
            voiceprint = attribution.get(event.data_id)
            assert voiceprint is not None
            assert attribution.get(voiceprint.payload["local_speaker_id"]) is not None
            assert attribution.get(voiceprint.payload["audio_ref_id"]) is not None
            seen.append(event.data_id)

    attribution.subscribe("attribution", on_event)
    _runner(storage, FakeSource({"cap-1": make_evidence()}), config).run_once()
    storage.run_until_idle()
    assert len(set(seen)) == 2


def test_rescan_after_restart_writes_nothing(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    storage = MemoryStorage()
    source = FakeSource({"cap-1": make_evidence()})
    _runner(storage, source, config).run_once()
    events = len(storage.events)
    reads = source.embedding_reads
    # 新 Runner 等于进程重启：进程内的记录没了，靠 ① 里已有的 audio_ref 判断
    report = _runner(storage, source, config).run_once()
    assert [type(o) for o in report.outcomes] == [AlreadyIngested]
    assert len(storage.events) == events
    assert source.embedding_reads == reads  # 已导入的录音不再取声纹


def test_same_runner_skips_done_captures(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    storage = MemoryStorage()
    runner = _runner(storage, FakeSource({"cap-1": make_evidence()}), config)
    runner.run_once()
    assert runner.run_once().outcomes == []


def test_reprocessed_capture_is_not_imported_again(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    """旧 Ear 重新处理后 Evidence id 全变了；同一 adapter_version 下不重复导入。"""
    storage = MemoryStorage()
    _runner(storage, FakeSource({"cap-1": make_evidence("run-1")}), config).run_once()
    report = _runner(storage, FakeSource({"cap-1": make_evidence("run-2")}), config).run_once()
    assert [type(o) for o in report.outcomes] == [AlreadyIngested]
    assert _count(storage, "transcript_segment") == 3


def test_degraded_run_is_not_imported_until_reprocessed(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    """旧 Ear 模型加载失败被锁住时 run 仍会跑完，但声纹阶段失败。

    不导入；旧 Ear 修好并重新处理后，下一轮导入。
    """
    degraded = make_evidence("run-1")
    degraded["availability"][2]["state"] = "failed"
    source = FakeSource({"cap-1": degraded})
    storage = MemoryStorage()
    runner = _runner(storage, source, config)
    first = runner.run_once()
    assert isinstance(first.outcomes[0], Failed)
    assert "降级" in first.outcomes[0].reason
    assert _count(storage, "audio_ref") == 0

    source.captures["cap-1"] = make_evidence("run-2")  # 旧 Ear 重新处理成功
    second = runner.run_once()
    assert isinstance(second.outcomes[0], Ingested)
    assert _count(storage, "transcript_segment") == 3


def test_new_adapter_version_imports_a_new_batch(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    storage = MemoryStorage()
    _runner(storage, FakeSource({"cap-1": make_evidence("run-1")}), config).run_once()
    newer = AdapterConfig(adapter_version="ear-audio/0.2.0")
    report = _runner(storage, FakeSource({"cap-1": make_evidence("run-2")}), newer).run_once()
    assert isinstance(report.outcomes[0], Ingested)
    assert _count(storage, "transcript_segment") == 6  # 旧批次保留


def test_gps_shared_by_adjacent_captures_is_deduplicated(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    storage = MemoryStorage()
    first = make_evidence()
    second = make_evidence("run-b")
    capture = second["evidence"][0]["payload"]
    capture["record_id"] = "rec-2"
    second["capture_id"] = capture["capture_id"] = "cap-2"
    report = _runner(storage, FakeSource({"cap-1": first, "cap-2": second}), config).run_once()
    assert [type(o) for o in report.outcomes] == [Ingested, Ingested]
    second_outcome = report.outcomes[1]
    assert isinstance(second_outcome, Ingested)
    assert second_outcome.deduplicated == 3  # GPS 点、手机快照、状态变化和第一段相同
    assert _count(storage, "gps_point") == 1


def test_pending_and_failed_are_reported(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    waiting = make_evidence()
    waiting["analyzable"] = False
    failed = make_evidence()
    failed["availability"][0]["state"] = "failed"
    waiting["capture_id"] = waiting["evidence"][0]["payload"]["capture_id"] = "w"
    failed["capture_id"] = failed["evidence"][0]["payload"]["capture_id"] = "f"
    storage = MemoryStorage()
    runner = _runner(storage, FakeSource({"w": waiting, "f": failed}), config)
    summary = runner.run_once().summary()
    assert summary["captures"] == {"Pending": 1, "Failed": 1}
    failed_captures = summary["failed"]
    assert isinstance(failed_captures, list)
    assert [c for c, _ in failed_captures] == ["f"]
    assert _count(storage, "audio_ref") == 0


def test_missing_audio_file_is_pending(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    storage = MemoryStorage()
    source = FakeSource({"cap-1": make_evidence()})
    report = _runner(storage, source, config, found=False).run_once()
    assert isinstance(report.outcomes[0], Pending)
    # 文件出现后下一轮导入
    report = _runner(storage, source, config).run_once()
    assert isinstance(report.outcomes[0], Ingested)


def test_ear_unavailable_is_retried_later(
    make_evidence: MakeEvidence, config: AdapterConfig
) -> None:
    storage = MemoryStorage()
    source = FakeSource({"cap-1": make_evidence()})
    runner = _runner(storage, source, config)
    source.down = True
    report = runner.run_once()
    assert report.source_error is not None and report.outcomes == []
    source.down = False
    assert isinstance(runner.run_once().outcomes[0], Ingested)


def test_conflict_marks_capture_failed(make_evidence: MakeEvidence, config: AdapterConfig) -> None:
    """本批某条数据的 id 已被别的数据占用（理论上不会发生）：整批不写，报告为 Failed。"""
    storage = MemoryStorage()
    source = FakeSource({"cap-1": make_evidence()})
    evidence = source.read_capture("cap-1")
    embeddings = {r: source.read_embedding(r) for r in ("run-1-res-1", "run-1-res-3")}
    records = capture_records(evidence, audio_file=AUDIO_FILE, embeddings=embeddings, config=config)
    transcript = next(r for r in records if r.data_type == "transcript_segment")
    blocker = make_raw(
        "transcript_segment",
        {},
        start="2026-10-26T00:00:00Z",
        id=transcript.id,
        dedup_key="something-else",
    )
    storage.client("adapter").write([AppendRaw(blocker)], Context.system()).check()
    outcome = ingest_capture(
        "cap-1",
        source=source,
        storage=storage.client("adapter"),
        locator=FakeLocator(),
        config=config,
    )
    assert isinstance(outcome, Failed) and "id_exists" in outcome.reason
    assert _count(storage, "audio_ref") == 0


def test_only_adapter_may_write_raw_records(config: AdapterConfig) -> None:
    storage = MemoryStorage()
    record = make_raw("voiceprint", {}, start="2026-10-26T00:00:00Z")
    with pytest.raises(PermissionDenied):
        storage.client("attribution").write([AppendRaw(record)], Context.system())


def test_inbox_locator_matches_size_and_hash(tmp_path: Path, make_evidence: MakeEvidence) -> None:
    capture = CaptureEvidence.model_validate(make_evidence()).of_type("capture", Capture)[0]
    group = tmp_path / "inbox/brain/ios-test/2026-10-26/phone/10-55-38__GROUP-1"
    group.mkdir(parents=True)
    (group / "segment_0001_asr.txt").write_bytes(b"x" * len(AUDIO_BYTES))  # 大小相同、哈希不同
    (group / "segment_0001.m4a").write_bytes(AUDIO_BYTES)
    found = InboxAudioLocator(tmp_path).locate(capture)
    assert found is not None
    assert found.path == "inbox/brain/ios-test/2026-10-26/phone/10-55-38__GROUP-1/segment_0001.m4a"
    assert found.sha256 == AUDIO_SHA
    (group / "segment_0001.m4a").write_bytes(b"changed")
    assert InboxAudioLocator(tmp_path).locate(capture) is None


def test_inbox_locator_ignores_date_directory(tmp_path: Path, make_evidence: MakeEvidence) -> None:
    """日期目录是手机当地日期（例如在 UTC+8 时区），可能和录音的 UTC 日期不同。"""
    capture = CaptureEvidence.model_validate(make_evidence()).of_type("capture", Capture)[0]
    group = tmp_path / "inbox/brain/ios-test/2026-10-27/phone/00-55-38__GROUP-1"
    group.mkdir(parents=True)
    (group / "segment_0001.m4a").write_bytes(AUDIO_BYTES)
    found = InboxAudioLocator(tmp_path).locate(capture)
    assert found is not None
    assert found.path.startswith("inbox/brain/ios-test/2026-10-27/")
