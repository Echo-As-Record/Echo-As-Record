from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import time
import wave
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest
from ear_adapter import recognition_decode as decode
from ear_adapter import recognition_long as long
from ear_adapter.config import AdapterConfig
from ear_adapter.convert import FileRef
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import ingest_native, native_audio_ref_id
from ear_adapter.recognition_decode import FFmpegDecoder
from ear_adapter.recognition_long import ChunkedRecognitionProvider, LongRecognitionError
from ear_adapter.recognition_process import RecognitionProcess
from ear_adapter.runtime import AlreadyIngested, Failed, Ingested
from ear_contracts import DataChanged, RawRecord
from ear_storage_client import MemoryStorage

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux recognition transport")
START = datetime(2030, 1, 1, 23, 59, 45, 123_456, tzinfo=UTC)
RATE = 16_000
CFG = AdapterConfig(channel="octopus", adapter_version="ear-native/0.1.0+long-v1-fixture")

# Deterministic one-shot stand-in. Deliberately reuse the exact same upstream
# identifiers across windows, including speaker label and provenance references.
_WORKER = r"""
import io
import json
import sys
import wave
from datetime import datetime, timedelta
from pathlib import Path

log, mode, _, start, _, end = sys.argv[1:]
a = datetime.fromisoformat(start)
b = datetime.fromisoformat(end)
origin = datetime.fromisoformat('2030-01-01T23:59:45.123456Z')
with wave.open(io.BytesIO(sys.stdin.buffer.read()), 'rb') as wav:
    n = wav.getnframes()
    data = wav.readframes(n)
    assert wav.getframerate() == 16000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
    assert 16000 <= n <= 480000 and len(data) == 2 * n
assert start.endswith('Z') and end.endswith('Z')
assert abs((b-a).total_seconds() - n / 16000) <= 0.001
with Path(log).open('a') as f:
    print(f'{start} {end} {n}', file=f)
if mode == 'partial' and a != origin:
    print('{"status":"partial"}')
    sys.exit(3)
if mode == 'error' and a != origin:
    print('{"status":"failed"}')
    sys.exit(2)
if mode == 'slow' and a != origin:
    __import__('time').sleep(10)
if data.count(0) == len(data):
    print(json.dumps(dict(status='complete', asr=dict(status='complete', chunks=[], segments=[]),
                          speakers=[], ambient=[])))
    sys.exit(0)
if mode == 'frame-pattern':
    frame = round((a-origin).total_seconds() * 16000)
    indices = (0, n//2, n-1)
    assert all(int.from_bytes(data[2*i:2*i+2], 'little') == (frame+i) % 32767 + 1
               for i in indices)
ch = dict(audio_chunk_id='repeated', started_at=start, ended_at=end, model_version='moss@fixture')
seg = dict(ch, speech_segment_id='segment', local_speaker_id='speaker_0',
           diarization_scope_id='repeated', original_text='synthetic', language=None, overlap=False)
vector = dict(model_version='ecapa@fixture', shape=[192], dtype='float32',
              values=[1.0]+[0.0]*191,
              provenance=dict(preprocessing='ear-speaker-concat-pcm16-mono16k-v1',
                              normalization='l2', speech_segment_ids=['segment']))
if mode == 'vector-space' and a != origin:
    vector['model_version'] = 'different@fixture'
if mode == 'degraded' and a != origin:
    vector = None
spk = dict(resolution_id='resolution', local_speaker_id='speaker_0',
           diarization_scope_id='repeated', evidence_segment_ids=['segment'], embedding=vector,
           sample_quality=0.8 if vector is not None else None)
event = dict(audio_event_id='ambient', started_at=start, ended_at=end,
             model_version='panns@fixture',
             taxonomy='audioset-527', original_label='tone')
print(json.dumps(dict(status='complete', asr=dict(status='complete', chunks=[ch], segments=[seg]),
                      speakers=[spk], ambient=[event])))
"""


def _capture(path: Path, end: datetime) -> CaptureInput:
    with path.open("rb") as inp:
        digest = hashlib.file_digest(inp, "sha256").hexdigest()
    return CaptureInput(
        "synthetic-capture",
        "synthetic-phone",
        "synthetic-record",
        "synthetic-group",
        START,
        end,
        "audio/mp4" if path.suffix == ".m4a" else "audio/wav",
        FileRef(path.name, digest, path.stat().st_size),
    )


def _wav(path: Path, frames: int, *, silent: bool = False, pattern: bool = False) -> None:
    block = b"\0\0" if silent else b"\x01\x02"
    with wave.open(str(path), "wb") as f:
        f.setparams((1, 2, RATE, frames, "NONE", "not compressed"))
        for start in range(0, frames, RATE):
            n = min(RATE, frames - start)
            f.writeframesraw(
                b"".join(((i % 32767) + 1).to_bytes(2, "little") for i in range(start, start + n))
                if pattern
                else block * n
            )


def _provider(
    tmp_path: Path, *, mode: str = "ok", timeout: float = 120, ffmpeg: FFmpegDecoder | None = None
) -> ChunkedRecognitionProvider:
    return ChunkedRecognitionProvider(
        RecognitionProcess(
            (sys.executable, "-I", "-B", "-c", _WORKER, str(tmp_path / "calls"), mode),
            timeout=20,
        ),
        ffmpeg or FFmpegDecoder("/never/ffmpeg", "/never/ffprobe"),
        timeout=timeout,
        scratch_dir=str(tmp_path),
    )


def _ingest(
    tmp_path: Path,
    capture: CaptureInput,
    provider: ChunkedRecognitionProvider,
    store: MemoryStorage,
):
    return ingest_native(
        capture, root=tmp_path, provider=provider, storage=store.client("adapter"), config=CFG
    )


def _records(store: MemoryStorage) -> dict[str, list[RawRecord]]:
    client = store.client("adapter")
    events = [e for e in store.events if isinstance(e, DataChanged)]
    assert len({e.batch_id for e in events}) == 1
    by_type: dict[str, list[RawRecord]] = {}
    for event in events:
        record = client.get(event.data_id)
        assert isinstance(record, RawRecord)
        by_type.setdefault(record.data_type, []).append(record)
    return by_type


def test_rebalanced_windows_keep_every_frame_and_global_time(tmp_path: Path) -> None:
    path = tmp_path / "pattern.wav"
    n = 60 * RATE + 7
    _wav(path, n, pattern=True)
    media = _capture(path, START + timedelta(seconds=60, microseconds=437))
    provider = _provider(tmp_path, mode="frame-pattern")
    store = MemoryStorage()
    assert isinstance(_ingest(tmp_path, media, provider, store), Ingested)
    report = provider.last_report
    assert report is not None and not report.degraded and not report.gaps
    assert report.normalized_frames == n and report.source_codec == "pcm_s16le"
    assert [c.end_frame - c.first_frame for c in report.chunks] == [30 * RATE, 29 * RATE + 7, RATE]
    assert report.chunks[-1].ended_at == media.ended_at
    records = _records(store)
    audio = records["audio_ref"][0]
    assert audio.id == native_audio_ref_id(media, CFG)
    assert audio.payload["file_ref"] == media.file_ref.to_json()
    speakers = {speaker.id: speaker for speaker in records["local_speaker"]}
    voices = {voice.id: voice for voice in records["voiceprint"]}
    transcripts = records["transcript_segment"]
    assert len(speakers) == len(voices) == len(transcripts) == 3
    for t, c in zip(sorted(transcripts, key=lambda r: r.event_start), report.chunks, strict=True):
        assert t.payload["model_version"] == "moss@fixture"
        assert (t.event_start, t.event_end) == (c.started_at, c.ended_at)
        assert t.payload["upstream"]["audio_chunk_id"] == c.chunk_id
        assert t.payload["audio_ref_id"] == audio.id
        s = speakers[t.payload["local_speaker_id"]]
        assert s.payload["label"] == "speaker_0"
        assert s.payload["upstream"]["diarization_scope_id"] == c.chunk_id
        v = voices[t.payload["voiceprint_id"]]
        assert v.payload["transcript_segment_ids"] == [t.id]
        assert v.payload["embedding"]["provenance"]["speech_segment_ids"] == [
            t.payload["upstream"]["speech_segment_id"]
        ]
    assert isinstance(_ingest(tmp_path, media, provider, store), AlreadyIngested)
    assert len((tmp_path / "calls").read_text().splitlines()) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["calls", "pattern.wav"]


@pytest.mark.parametrize(
    "mode, failure, states",
    [
        ("partial", "partial_chunk", ["complete", "partial", "unprocessed"]),
        ("error", "worker", ["complete", "failed", "unprocessed"]),
        ("slow", "deadline", ["complete", "failed", "unprocessed"]),
        ("vector-space", "results", ["complete", "complete", "complete"]),
    ],
)
def test_failure_never_publishes_verified_prefix(
    tmp_path: Path, mode: str, failure: str, states: list[str]
) -> None:
    path = tmp_path / "input.wav"
    _wav(path, 61 * RATE)
    capture = _capture(path, START + timedelta(seconds=61))
    provider = _provider(tmp_path, mode=mode, timeout=2 if mode == "slow" else 120)
    store = MemoryStorage()
    assert isinstance(_ingest(tmp_path, capture, provider, store), Failed)
    report = provider.last_report
    assert report is not None and report.degraded and report.failure == failure
    assert [c.status for c in report.chunks] == states
    assert report.chunks[0].segments == 1
    assert report.gaps == tuple(
        (c.started_at, c.ended_at) for c in report.chunks if c.status != "complete"
    )
    assert all(c.segments is None for c in report.chunks if c.status != "complete")
    assert not store.events
    assert store.client("adapter").get(native_audio_ref_id(capture, CFG)) is None


def test_combined_result_budget_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "input.wav"
    _wav(path, 61 * RATE)
    capture = _capture(path, START + timedelta(seconds=61))
    provider = _provider(tmp_path)
    monkeypatch.setattr(long, "_MAX_RECORDS", 8)
    store = MemoryStorage()
    assert isinstance(_ingest(tmp_path, capture, provider, store), Failed)
    assert provider.last_report is not None and provider.last_report.failure == "result_limit"
    assert [c.status for c in provider.last_report.chunks] == [
        "complete",
        "complete",
        "unprocessed",
    ]
    assert not store.events


def test_missing_voiceprint_and_clock_slop_remain_visible(tmp_path: Path) -> None:
    path = tmp_path / "input.wav"
    _wav(path, 31 * RATE)
    capture = _capture(path, START + timedelta(seconds=31, microseconds=500))
    provider = _provider(tmp_path, mode="degraded")
    store = MemoryStorage()
    assert isinstance(_ingest(tmp_path, capture, provider, store), Ingested)
    report = provider.last_report
    assert report is not None and report.failure is None and report.degraded
    assert report.gaps == ((START + timedelta(seconds=31), capture.ended_at),)
    assert [c.missing_voiceprints for c in report.chunks] == [0, 1]
    records = _records(store)
    assert len(records["voiceprint"]) == 1
    assert sum(t.payload["voiceprint_id"] is None for t in records["transcript_segment"]) == 1


def test_silence_is_complete_and_audio_ref_only(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    _wav(path, 31 * RATE, silent=True)
    capture = _capture(path, START + timedelta(seconds=31))
    provider = _provider(tmp_path)
    store = MemoryStorage()
    assert isinstance(_ingest(tmp_path, capture, provider, store), Ingested)
    assert set(_records(store)) == {"audio_ref"}
    report = provider.last_report
    assert report is not None and not report.degraded and len(report.chunks) == 2
    assert all(c.status == "complete" and c.segments == 0 for c in report.chunks)


def test_capture_over_limit_and_clock_mismatch_never_start_worker(tmp_path: Path) -> None:
    path = tmp_path / "input.wav"
    _wav(path, 31 * RATE)
    media = _capture(path, START + timedelta(seconds=31))
    provider = _provider(tmp_path)
    for end in (START + timedelta(minutes=31, microseconds=1), START + timedelta(seconds=30)):
        capture = replace(media, ended_at=end)
        with path.open("rb") as f, pytest.raises(LongRecognitionError) as result:
            provider.asr(f, capture)
        assert result.value.report.failure == "normalization"
        assert result.value.report.gaps == ((START, end),)
    assert not (tmp_path / "calls").exists()


def _assert_full_phone_capture(tmp_path: Path, path: Path, decoder: FFmpegDecoder | None) -> None:
    capture = _capture(path, START + timedelta(minutes=30))
    provider = _provider(tmp_path, ffmpeg=decoder)
    store = MemoryStorage()
    start = time.monotonic()
    outcome = _ingest(tmp_path, capture, provider, store)
    print(f"30-minute {path.suffix} chunked E2E: {time.monotonic() - start:.3f} s")
    assert isinstance(outcome, Ingested)
    assert outcome.written == Counter(
        audio_ref=1, transcript_segment=60, local_speaker=60, voiceprint=60, ambient_sound=60
    )
    report = provider.last_report
    assert report is not None and not report.degraded and len(report.chunks) == 60
    assert (
        report.normalized_frames == 1800 * RATE and report.chunks[-1].ended_at == capture.ended_at
    )
    assert report.source_codec == ("aac" if decoder is not None else "pcm_s16le")
    assert all(c.end_frame - c.first_frame == 30 * RATE for c in report.chunks)
    assert len((tmp_path / "calls").read_text().splitlines()) == 60
    audio = _records(store)["audio_ref"]
    assert len(audio) == 1 and audio[0].payload["file_ref"] == capture.file_ref.to_json()


def test_full_30minute_wav_e2e(tmp_path: Path) -> None:
    path = tmp_path / "phone.wav"
    _wav(path, 1800 * RATE)
    _assert_full_phone_capture(tmp_path, path, None)


def test_full_30minute_phone_m4a_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("requires local FFmpeg and ffprobe")
    for command in (ffmpeg, ffprobe):
        protocols = subprocess.check_output(
            [command, "-hide_banner", "-protocols"], text=True, timeout=5
        )
        if "fd" not in protocols.split():
            pytest.skip("requires FFmpeg fd protocol")
    path = tmp_path / "phone.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=sample_rate=16000",
            "-t",
            "1800",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            str(path),
        ],
        check=True,
        timeout=60,
        env={**os.environ, "AV_LOG_FORCE_NOCOLOR": "1"},
    )
    seen = []
    run_to = decode._run_to

    def track_call(
        command: tuple[str, ...],
        source: BinaryIO,
        cap: int,
        deadline: float,
        *,
        sink: BinaryIO | None,
    ) -> tuple[bytearray, int]:
        assert fcntl.fcntl(source, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        assert all(str(path) not in arg for arg in command)
        seen.append((command[0], sink is not None))
        return run_to(command, source, cap, deadline, sink=sink)

    monkeypatch.setattr(decode, "_run_to", track_call)
    _assert_full_phone_capture(tmp_path, path, FFmpegDecoder(ffmpeg, ffprobe, timeout=60))
    assert seen == [(ffprobe, False), (ffmpeg, True)]
