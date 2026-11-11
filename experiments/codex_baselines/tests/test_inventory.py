"""合成 Evidence、临时数据库和 inbox；不连接真实旧 Ear。"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from ear_adapter.ear_api import CaptureEvidence
from ear_adapter.testing import AUDIO_BYTES, AUDIO_FILE, FakeSource, evidence_json
from ear_baselines import inventory
from ear_baselines.media import MediaError, MediaInfo

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 11, tzinfo=UTC)


def evidence(state="available"):
    data = evidence_json()
    data["availability"][0]["state"] = state
    return CaptureEvidence.model_validate(data)


@pytest.mark.parametrize(
    ("state", "audio", "active", "errors", "expected"),
    [
        ("available", True, False, False, "ready"),
        ("missing", True, False, False, "incomplete"),
        ("partial", True, False, False, "failed"),
        ("failed", True, False, False, "failed"),
        ("available", True, True, True, "processing"),
        ("available", True, False, True, "failed"),
        ("available", False, True, True, "audio_missing"),
    ],
)
def test_classification(state, audio, active, errors, expected):
    result = inventory.classify(
        evidence(state),
        Path("test.m4a") if audio else None,
        [{"stage": "speech"}] if active else [],
        [inventory.failure("speech", "ConnectError")] if errors else [],
    )
    assert result[0] == expected


def test_readiness_exceptions():
    data = evidence_json()
    data["availability"][0]["state"] = "empty"
    data["availability"][2]["state"] = "missing"
    assert inventory.classify(CaptureEvidence.model_validate(data), Path("a"), [], [])[0] == "ready"
    data["availability"].append({"type": "speaker_embedding", "state": "available"})
    for item in data["evidence"]:
        if item["ref"]["type"] == "speaker_resolution":
            item["payload"]["embedding_ref"] = None
    status, missing, _ = inventory.classify(CaptureEvidence.model_validate(data), Path("a"), [], [])
    assert status == "incomplete"
    assert "授权" in missing[0]


def test_activity_and_stale_remote_progress():
    old = (NOW - timedelta(days=5)).isoformat()
    run = {
        "capture_id": "c",
        "pipeline_run_id": "r",
        "status": "running",
        "created_at": old,
        "updated_at": old,
    }
    task = {"stage": "speech", "status": "running", "started_at": old, "idempotency_key": "k"}
    active, errors = inventory.run_state(run, [task], {}, NOW, TZ)
    assert not active and "48" in errors[0]["reason"]
    assert errors[0]["rerunnable"] == "unknown"
    assert "/cancel" in errors[0]["rerun_how"]
    active, errors = inventory.run_state(
        run, [task], {"k": {"last_processing_at": NOW.isoformat()}}, NOW, TZ
    )
    assert len(active) == 1 and not errors
    assert active[0]["since"].endswith("+08:00")
    run["status"] = "succeeded"
    assert inventory.run_state(run, [task], {}, NOW, TZ) == ([], [])


def database(path, data):
    with sqlite3.connect(path) as db:
        for table in ("captures", "pipeline_runs", "pipeline_tasks", "remote_jobs"):
            db.execute(f"CREATE TABLE {table} (payload TEXT)")
            for item in data.get(table, []):
                db.execute(f"INSERT INTO {table} VALUES (?)", (json.dumps(item),))


def test_inventory_json_transcripts_orphans_and_history(tmp_path, monkeypatch):
    root = tmp_path / "data"
    audio = root / AUDIO_FILE.path
    audio.parent.mkdir(parents=True)
    audio.write_bytes(AUDIO_BYTES)
    orphan = audio.parent / "unknown.extension"
    orphan.write_bytes(b"another audio")
    duplicate = audio.parent / "duplicate"
    duplicate.write_bytes(b"another audio")
    metadata = audio.parent / "record.json"
    metadata.write_text("{}")

    def probe(path):
        if path == metadata:
            raise MediaError("not audio")
        return MediaInfo(1800, "aac", 16000, 1, "mp4", None)

    monkeypatch.setattr(inventory, "probe", probe)
    data = evidence_json()
    run = {
        "capture_id": "cap-1",
        "pipeline_run_id": "run-1",
        "status": "succeeded",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    db = tmp_path / "ear.db"
    database(
        db,
        {
            "captures": [data["evidence"][0]["payload"]],
            "pipeline_runs": [run],
            "pipeline_tasks": [
                {
                    "pipeline_run_id": "old",
                    "stage": "speaker",
                    "status": "failed",
                    "error": {"message": "load_failed"},
                }
            ],
        },
    )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    out = tmp_path / "output"
    result = inventory.build_inventory(FakeSource({"cap-1": data}), root, db, out, TZ, now=NOW)
    assert result == json.loads((out / "index.json").read_text())
    assert set(inventory.STATUSES) <= result["totals"].keys()
    assert result["totals"]["ready"] == 1
    assert result["totals"]["not_ingested"] == 1
    assert result["totals"]["ready_hours"] == 0.5
    recording, extra = result["recordings"]
    assert recording["counts"] == {
        "speech_segments": 3,
        "transcript_chars": 15,
        "speakers": 3,
        "audio_events": 1,
        "gps": 1,
        "phone_state": 1,
    }
    assert recording["started_at"] == "2026-10-26T09:55:38+08:00"
    assert extra["capture_id"] is None and extra["id"].startswith("file-")
    assert len(extra["audio_paths"]) == 2
    assert extra["started_at_basis"].startswith("inbox")
    assert not result["failure_groups"] and not result["running_now"]
    assert len(result["unrecognized_files"]) == 1
    assert "合成发言一" in (out / "transcripts/cap-1.md").read_text()
    assert "2026-10-26" in (out / "index.md").read_text()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert audio.read_bytes() == AUDIO_BYTES
    with (
        sqlite3.connect(f"file:{db}?mode=ro", uri=True) as ro,
        pytest.raises(sqlite3.OperationalError),
    ):
        ro.execute("DELETE FROM captures")


def test_failure_groups_and_current_run(tmp_path, monkeypatch):
    root = tmp_path / "data"
    audio = root / AUDIO_FILE.path
    audio.parent.mkdir(parents=True)
    audio.write_bytes(AUDIO_BYTES)
    monkeypatch.setattr(inventory, "probe", lambda _: MediaInfo(1800, "aac", 16000, 1, "mp4", None))
    data = evidence_json()
    db = tmp_path / "ear.db"
    database(
        db,
        {
            "captures": [data["evidence"][0]["payload"]],
            "pipeline_runs": [
                {
                    "capture_id": "cap-1",
                    "pipeline_run_id": "run-1",
                    "status": "degraded",
                    "created_at": NOW.isoformat(),
                    "updated_at": NOW.isoformat(),
                }
            ],
            "pipeline_tasks": [
                {
                    "pipeline_run_id": "run-1",
                    "stage": "speech",
                    "status": "failed",
                    "error": {"message": "ConnectError"},
                }
                for _ in range(2)
            ],
        },
    )
    result = inventory.build_inventory(FakeSource({"cap-1": data}), root, db, tmp_path / "out", TZ)
    assert result["totals"]["failed"] == 1
    assert result["failure_groups"][0]["count"] == 1
    assert result["recordings"][0]["readiness"] == "Ready"
    # 失败录音只要有转写也导出，便于收听和出题
    assert [p.name for p in (tmp_path / "out/transcripts").iterdir()] == ["cap-1.md"]


def test_incomplete_transcript_and_stale_output_cleanup(tmp_path, monkeypatch):
    root = tmp_path / "data"
    audio = root / AUDIO_FILE.path
    audio.parent.mkdir(parents=True)
    audio.write_bytes(AUDIO_BYTES)
    monkeypatch.setattr(inventory, "probe", lambda _: MediaInfo(1800, "aac", 16000, 1, "mp4", None))
    data = evidence_json()
    data["availability"][0]["state"] = "missing"
    db = tmp_path / "ear.db"
    database(db, {"captures": [data["evidence"][0]["payload"]]})
    out = tmp_path / "out"
    source = FakeSource({"cap-1": data})
    result = inventory.build_inventory(source, root, db, out, TZ)
    assert result["totals"]["incomplete"] == 1
    transcript = out / "transcripts/cap-1.md"
    assert transcript.exists()
    data["availability"][0]["state"] = "failed"
    result = inventory.build_inventory(source, root, db, out, TZ)
    assert result["totals"]["failed"] == 1
    assert transcript.exists()  # 失败但有转写：保留
    data["evidence"] = [i for i in data["evidence"] if i["ref"]["type"] != "speech_segment"]
    result = inventory.build_inventory(source, root, db, out, TZ)
    assert result["totals"]["failed"] == 1
    assert not transcript.exists()  # 没有转写了：清理上次的文件


def test_output_rejects_source_directory(tmp_path):
    with pytest.raises(ValueError, match="输出必须"):
        inventory.build_inventory(FakeSource({}), tmp_path, tmp_path / "none.db", tmp_path, TZ)
