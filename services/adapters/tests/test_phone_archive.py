"""Synthetic inbox -> adapter -> memory storage -> time-window CLI readback.

The recognizer is a deterministic Evidence fixture, not a real model invocation.
"""

import hashlib
import importlib.util
import io
import json
import sys
import wave
from pathlib import Path

from ear_adapter import AdapterConfig, InboxAudioLocator, Ingested, Runner
from ear_adapter.testing import FakeSource, evidence_json
from ear_ingest.app import create_app
from ear_storage_client import MemoryStorage
from fastapi.testclient import TestClient


def test_phone_archive_without_preview(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EAR_TRANSCRIPTION_URL", raising=False)
    monkeypatch.delenv("EAR_INGEST_EVENT_URL", raising=False)
    monkeypatch.delenv("EAR_INGEST_NOTIFY_URL", raising=False)
    monkeypatch.setenv("EAR_INGEST_TOKEN", "synthetic")
    monkeypatch.setenv("EAR_INGEST_INBOX_ROOT", str(tmp_path / "inbox/brain"))
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    with TestClient(create_app()) as client:
        reply = client.post(
            "/api/v1/record",
            headers={"Authorization": "Bearer synthetic", "X-Device-Id": "ios-test"},
            data={
                "name": "Synthetic",
                "text": "",
                "type": "richVoice",
                "source": "local",
                "group_id": "GROUP-1",
                "timestamp": "2026-10-26T10:55:38+09:00",
                "segment_meta": '{"segmentIndex":0}',
                "segment_start_ms": "0",
                "segment_end_ms": "1800000",
            },
            files={"file": ("segment.wav", audio.getvalue(), "audio/wav")},
        )
        assert reply.status_code == 200
    evidence = evidence_json()
    capture = evidence["evidence"][0]["payload"]
    capture["record_id"] = reply.json()["record_id"]
    capture["audio_ref"].update(
        checksum="sha256:" + hashlib.sha256(audio.getvalue()).hexdigest(),
        size_bytes=len(audio.getvalue()),
    )
    storage = MemoryStorage()
    report = Runner(
        source=FakeSource({"cap-1": evidence}),
        storage=storage.client("adapter"),
        locator=InboxAudioLocator(tmp_path),
        config=AdapterConfig(),
    ).run_once()
    assert isinstance(report.outcomes[0], Ingested)
    script = Path(__file__).parents[1] / "scripts/read_transcripts.py"
    spec = importlib.util.spec_from_file_location("read_transcripts", script)
    assert spec and spec.loader
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)

    class Connection:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return storage.client("adapter")

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(reader, "RemoteStorageClient", Connection)
    monkeypatch.setenv("EAR_ADAPTER_STORAGE_TOKEN", "synthetic")
    monkeypatch.setenv("EAR_ADAPTER_STORAGE_URL", "http://unused.invalid")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "read_transcripts",
            "--device",
            "ios-test",
            "--start",
            "2026-10-26T10:56:00+09:00",
            "--end",
            "2026-10-26T10:57:03+09:00",
        ],
    )
    reader.main()
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 2
    assert all(storage.client("adapter").get(row["id"]) for row in rows)
    assert all(row["audio_ref_id"] and row["local_speaker_id"] for row in rows)
