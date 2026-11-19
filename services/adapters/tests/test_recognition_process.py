from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import sys
import time
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from ear_adapter.config import AdapterConfig
from ear_adapter.convert import FileRef
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import RecognitionProvider, ingest_native, native_audio_ref_id
from ear_adapter.recognition_process import RecognitionProcess, RecognitionProcessError
from ear_adapter.runtime import AlreadyIngested, Failed, Ingested
from ear_contracts import DataChanged, RawRecord, format_utc
from ear_storage_client import MemoryStorage

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux worker transport")
START = datetime(2000, 1, 1, microsecond=123456, tzinfo=UTC)
END = START + timedelta(seconds=1)
CFG = AdapterConfig(channel="fixture", adapter_version="ear-native/0.1.0+synthetic")
DIAGNOSTIC = "SYNTHETIC_PRIVATE_BODY"

# No import of the legacy bridge or any model. Marker files contain synthetic
# launch counts / group member PIDs only, never timestamps, argv or audio.
_FIXTURE = r"""
import datetime as dt
import io
import json
import os
import signal
import sys
import time
import wave

mode, journal, fixture_wire, fd, *flags = sys.argv[1:]
assert flags[::2] == ["--started-at", "--ended-at"] and len(flags) == 4
assert all(value.endswith("Z") for value in flags[1::2])
assert not (set(os.environ) - {"LC_CTYPE"}) and os.getcwd() == "/"
assert os.getpid() == os.getsid(0) == os.getpgrp()
assert sys.flags.isolated and sys.dont_write_bytecode
if fd != "none":
    try:
        os.fstat(int(fd))
    except OSError:
        pass
    else:
        raise AssertionError("unexpected inherited descriptor")
with open(journal, "a") as file:
    file.write("run\n")
if mode == "no-io":
    time.sleep(20)
if mode == "early":
    os._exit(0)
if mode == "flood":
    while True:
        os.write(1, b"x" * 65536)
raw = sys.stdin.buffer.read()
with wave.open(io.BytesIO(raw)) as wav:
    assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
    pcm = wav.readframes(wav.getnframes())
    assert len(raw) == len(pcm) + 44 and b"SYNTHETIC_PRIVATE_BODY" not in raw
if mode == "closed-stdout":
    os.close(1)
    time.sleep(20)
if mode in {"orphan-pipe", "fork-hang"}:
    pid = os.fork()
    if not pid:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(20)
        os._exit(0)
    with open(journal + ".pid", "w") as file:
        file.write(str(pid))
    if mode == "fork-hang":
        time.sleep(20)
    os._exit(0)
print("SYNTHETIC_PRIVATE_BODY", file=sys.stderr)
if mode == "wire":
    sys.stdout.write(fixture_wire)
    sys.exit(0)
if mode == "invalid-utf8":
    os.write(1, b"\xff")
    sys.exit(0)
start = dt.datetime.fromisoformat(flags[1])
end = min(dt.datetime.fromisoformat(flags[3]), start + dt.timedelta(seconds=len(pcm) / 32000))
span = dict(started_at=flags[1], ended_at=end.isoformat().replace("+00:00", "Z"))
result = dict(status="complete", asr=dict(status="complete", chunks=[], segments=[]),
              speakers=[], ambient=[])
if any(pcm) or mode == "false-silence":
    result["asr"]["chunks"].append(dict(audio_chunk_id="chunk", **span, model_version="fixture"))
    result["ambient"].append(dict(audio_event_id="event", **span, model_version="fixture",
                                  original_label="synthetic sound", taxonomy="audioset-527"))
    if mode != "no-speech":
        result["asr"]["segments"].append(dict(audio_chunk_id="chunk", **span,
            model_version="fixture", speech_segment_id="s1", language="en", overlap=False,
            diarization_scope_id="chunk", local_speaker_id="A",
            original_text=f"synthetic [{pcm[0]}] quoted \"text\" backslash \\"))
        embedding = dict(model_version="fixture", shape=[192], dtype="float32",
                         values=[1.0] + [0.0] * 191, provenance=dict(normalization="l2",
                             preprocessing="ear-speaker-concat-pcm16-mono16k-v1",
                             speech_segment_ids=["s1"]))
        result["speakers"].append(dict(resolution_id="speaker", diarization_scope_id="chunk",
            local_speaker_id="A", evidence_segment_ids=["s1"],
            embedding=embedding if mode != "no-vector" else None,
            sample_quality=0.5 if mode != "no-vector" else None))
if mode == "failed" or (mode == "retry" and pcm[0] == 2):
    print(json.dumps(result))  # A valid-looking payload cannot override the exit status.
    sys.exit(2)
if mode == "partial":
    print('{"status":"partial","stage":"asr","asr":{}}')
    sys.exit(3)
print(json.dumps(result))
"""


def _wav(
    *, marker: int = 1, frames: int = 16_000, rate: int = 16_000, channels: int = 1, width: int = 2
) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as stream:
        stream.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        stream.writeframes(bytes((marker,)) * frames * channels * width)
    return bio.getvalue()


def _add_chunk(wav: bytes, tag: bytes, content: bytes) -> bytes:
    raw = wav[8:] + struct.pack("<4sI", tag, len(content)) + content + b"\0" * (len(content) % 2)
    return b"RIFF" + struct.pack("<I", len(raw)) + raw


def _capture(root: Path, wav: bytes, *, name: str = "a", duration: int = 1000) -> CaptureInput:
    path = root / f"private-synthetic-{name}.wav"
    path.write_bytes(wav)
    return CaptureInput(
        f"private-cap-{name}",
        "private-device",
        f"private-record-{name}",
        "private-group",
        START.astimezone(timezone(timedelta(hours=-5))),
        START + timedelta(milliseconds=duration),
        "audio/wav",
        FileRef(path.name, hashlib.sha256(wav).hexdigest(), len(wav)),
    )


def _command(
    root: Path, mode: str, wire: str = "unused", *, fd: int | None = None
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-B",
        "-c",
        _FIXTURE,
        mode,
        str(root / "runs"),
        wire,
        "none" if fd is None else str(fd),
    )


def _ingest(root: Path, capture: CaptureInput, provider: RecognitionProvider, store: MemoryStorage):
    return ingest_native(
        capture, root=root, provider=provider, storage=store.client("adapter"), config=CFG
    )


def _binary(root: Path, capture: CaptureInput) -> io.BytesIO:
    return io.BytesIO((root / capture.file_ref.path).read_bytes())


def test_atomic_native_read_dedup_and_distinct_successive_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    first = _capture(tmp_path, _add_chunk(_wav(), b"LIST", DIAGNOSTIC.encode()))
    second = _capture(tmp_path, _wav(marker=2), name="b", duration=1001)
    monkeypatch.setenv("EAR_ADAPTER_STORAGE_TOKEN", "synthetic-unused-secret")
    parent_file = tmp_path / "synthetic-secret"
    parent_file.write_bytes(b"synthetic unused credential")
    store = MemoryStorage()
    client = store.client("adapter")
    with parent_file.open("rb") as handle:
        os.set_inheritable(handle.fileno(), True)
        provider: RecognitionProvider = RecognitionProcess(
            _command(tmp_path, "ok", fd=handle.fileno())
        )
        for index, capture in enumerate((first, second), start=1):
            ingested = _ingest(tmp_path, capture, provider, store)
            assert isinstance(ingested, Ingested) and ingested.deduplicated == 0
            events = [e for e in store.events[-5:] if isinstance(e, DataChanged)]
            assert len(events) == 5 and len({e.batch_id for e in events}) == 1
            records: dict[str, RawRecord] = {}
            for event in events:
                record = client.get(event.data_id)
                assert isinstance(record, RawRecord)
                records[record.data_type] = record
            audio = records.pop("audio_ref")
            assert audio.id == native_audio_ref_id(capture, CFG)
            assert all(r.payload["audio_ref_id"] == audio.id for r in records.values())
            transcript = records["transcript_segment"]
            voice, speaker = records["voiceprint"], records["local_speaker"]
            assert f"synthetic [{index}]" in transcript.payload["text"]
            assert transcript.event_start == START and transcript.event_end == END
            assert voice.payload["transcript_segment_ids"] == [transcript.id]
            assert transcript.payload["voiceprint_id"] == voice.id
            assert voice.payload["local_speaker_id"] == speaker.id
            assert isinstance(_ingest(tmp_path, capture, provider, store), AlreadyIngested)
    assert len(store.events) == 10
    assert (tmp_path / "runs").read_text().splitlines() == ["run", "run"]
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize(
    "mode, marker, types",
    [
        ("ok", 0, {"audio_ref"}),
        ("no-speech", 1, {"audio_ref", "ambient_sound"}),
        ("no-vector", 1, {"audio_ref", "transcript_segment", "local_speaker", "ambient_sound"}),
    ],
)
def test_empty_stages(
    tmp_path: Path,
    mode: str,
    marker: int,
    types: set[str],
) -> None:
    store, capture = MemoryStorage(), _capture(tmp_path, _wav(marker=marker))
    outcome = _ingest(tmp_path, capture, RecognitionProcess(_command(tmp_path, mode)), store)
    assert isinstance(outcome, Ingested) and set(outcome.written) == types
    assert len(store.events) == len(types)
    assert (tmp_path / "runs").read_text() == "run\n"


def test_stage_is_scoped_to_exact_stream_and_capture_and_cannot_be_reused(tmp_path: Path) -> None:
    capture = _capture(tmp_path, _wav())
    provider = RecognitionProcess(_command(tmp_path, "ok"))
    with _binary(tmp_path, capture) as audio, _binary(tmp_path, capture) as other:
        result = provider.asr(audio, capture)
        audio.seek(0)
        with pytest.raises(RecognitionProcessError, match="stage mismatch"):
            provider.speakers(other, capture, result)
        with pytest.raises(RecognitionProcessError, match="stage mismatch"):
            provider.speakers(audio, capture, result)
        result = provider.asr(audio, capture)
        audio.seek(0)
        with pytest.raises(RecognitionProcessError, match="stage mismatch"):
            provider.speakers(audio, replace(capture), result)
        result = provider.asr(audio, capture)
        with pytest.raises(RecognitionProcessError, match="stage mismatch"):
            provider.speakers(audio, capture, result)
        audio.seek(0)
        result = provider.asr(audio, capture)
        audio.seek(0)
        with pytest.raises(RecognitionProcessError, match="stage mismatch"):
            provider.speakers(audio, capture, replace(result, segments=()))
        result = provider.asr(audio, capture)
        audio.seek(0)
        assert provider.speakers(audio, capture, result)
        assert provider.ambient(audio, capture)
        with pytest.raises(RecognitionProcessError, match="stage mismatch"):
            provider.ambient(audio, capture)


def test_failed_next_capture_cannot_reuse_an_abandoned_result(tmp_path: Path) -> None:
    first = _capture(tmp_path, _wav())
    bad = _capture(tmp_path, _wav(marker=2), name="b")
    provider = RecognitionProcess(_command(tmp_path, "retry"))
    with _binary(tmp_path, first) as audio, _binary(tmp_path, bad) as other:
        asr = provider.asr(audio, first)
        with pytest.raises(RecognitionProcessError, match="worker failed"):
            provider.asr(other, bad)
        audio.seek(0)
        with pytest.raises(RecognitionProcessError, match="stage mismatch"):
            provider.speakers(audio, first, asr)
    assert isinstance(_ingest(tmp_path, first, provider, MemoryStorage()), Ingested)
    assert (tmp_path / "runs").read_text().splitlines() == ["run", "run", "run"]


@pytest.mark.parametrize("mode", ["failed", "partial", "invalid-utf8", "false-silence", "early"])
def test_worker_failure_never_publishes_or_logs_worker_body(
    tmp_path: Path,
    mode: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    duration = 30 if mode == "early" else 1
    capture = _capture(
        tmp_path,
        _wav(marker=0 if mode == "false-silence" else 1, frames=duration * 16_000),
        duration=duration * 1000,
    )
    store = MemoryStorage()
    outcome = _ingest(tmp_path, capture, RecognitionProcess(_command(tmp_path, mode)), store)
    assert isinstance(outcome, Failed) and outcome.reason == "native ASR failed or incomplete"
    assert (
        not store.events and store.client("adapter").get(native_audio_ref_id(capture, CFG)) is None
    )
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize(
    "mode, timeout, error",
    [
        ("no-io", 0.5, "timed out"),
        ("closed-stdout", 0.5, "timed out"),
        ("flood", 10.0, "output limit"),
        ("orphan-pipe", 10.0, "left stdout open"),
        ("fork-hang", 1.0, "timed out"),
    ],
)
def test_bounded_process_io_and_group_teardown(
    tmp_path: Path,
    mode: str,
    timeout: float,
    error: str,
) -> None:
    capture = _capture(tmp_path, _wav(frames=480_000), duration=30_000)
    provider = RecognitionProcess(_command(tmp_path, mode), timeout=timeout)
    start = time.monotonic()
    with _binary(tmp_path, capture) as audio, pytest.raises(RecognitionProcessError, match=error):
        provider.asr(audio, capture)
    assert time.monotonic() - start < 3
    if mode in {"orphan-pipe", "fork-hang"}:
        pid = int((tmp_path / "runs.pid").read_text())
        status = Path(f"/proc/{pid}/status")
        until = time.monotonic() + 1
        while time.monotonic() < until:
            try:
                if "State:\tZ" in status.read_text():
                    break
            except FileNotFoundError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("synthetic process group member survived cleanup")


def test_timeout_is_atomic_under_native_ingest(tmp_path: Path) -> None:
    store, capture = MemoryStorage(), _capture(tmp_path, _wav())
    provider = RecognitionProcess(_command(tmp_path, "no-io"), timeout=0.25)
    outcome = _ingest(tmp_path, capture, provider, store)
    assert isinstance(outcome, Failed) and outcome.reason == "native ASR failed or incomplete"
    assert (
        not store.events and store.client("adapter").get(native_audio_ref_id(capture, CFG)) is None
    )


@pytest.mark.parametrize(
    "wav, duration",
    [
        (_wav(rate=8000), 1000),
        (_wav(channels=2), 1000),
        (_wav(width=1), 1000),
        (_wav(frames=15_999), 1000),
        (_wav(frames=480_001), 30_000),
        (_wav(), 1002),
        (_wav() + _wav(), 1000),
        (_wav()[:-1], 1000),
        (_wav() + bytes(1 << 20), 1000),
        (_add_chunk(_wav(), b"data", b"abc"), 1000),
        (_add_chunk(_wav(), b"ZZZZ", b"abc"), 1000),
    ],
)
def test_invalid_wav_never_starts_worker(tmp_path: Path, wav: bytes, duration: int) -> None:
    capture = _capture(tmp_path, wav, duration=duration)
    store = MemoryStorage()
    assert isinstance(
        _ingest(tmp_path, capture, RecognitionProcess(_command(tmp_path, "ok")), store), Failed
    )
    assert not (tmp_path / "runs").exists() and not store.events


def _wire() -> dict[str, Any]:
    span = {"started_at": format_utc(START), "ended_at": format_utc(END)}
    chunk = dict(audio_chunk_id="chunk", model_version="fixture", **span)
    segment = dict(
        chunk,
        speech_segment_id="s1",
        original_text="synthetic",
        language=None,
        local_speaker_id="A",
        diarization_scope_id="chunk",
        overlap=False,
    )
    embedding = dict(
        model_version="fixture",
        shape=[192],
        dtype="float32",
        values=[1.0] + [0.0] * 191,
        provenance=dict(
            preprocessing="ear-speaker-concat-pcm16-mono16k-v1",
            normalization="l2",
            speech_segment_ids=["s1"],
        ),
    )
    speaker = dict(
        resolution_id="rid",
        diarization_scope_id="chunk",
        local_speaker_id="A",
        evidence_segment_ids=["s1"],
        embedding=embedding,
        sample_quality=1,
    )
    return dict(
        status="complete",
        asr=dict(status="complete", chunks=[chunk], segments=[segment]),
        speakers=[speaker],
        ambient=[],
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("status", "partial"),
        ("asr.extra", DIAGNOSTIC),
        ("asr.chunks.0.started_at", 42),
        ("asr.chunks.0.ended_at", "2000-01-01T00:00:01.123456+00:00"),
        ("asr.segments.0.overlap", 0),
        ("asr.segments.0.language", False),
        ("asr.segments.0.original_text", "x" * 16_385),
        ("asr.segments.0.original_text", "\ud800"),
        ("asr.segments.0.audio_chunk_id", "wrong-chunk"),
        ("asr.segments", [None] * 1025),
        ("speakers", []),
        ("speakers.0.evidence_segment_ids", ["s1", "s1"]),
        ("speakers.0.evidence_segment_ids", ["missing"]),
        ("speakers.0.sample_quality", True),
        ("speakers.0.sample_quality", 1.01),
        ("speakers.0.embedding.shape", [192.0]),
        ("speakers.0.embedding.values", [True] + [0.0] * 191),
        ("speakers.0.embedding.values", [2.0] * 192),
        ("speakers.0.embedding.provenance.extra", DIAGNOSTIC),
        ("speakers.0.embedding.provenance.speech_segment_ids", ["missing"]),
        ("ambient", {}),
        ("ambient", [None] * 1025),
    ],
)
def test_closed_and_bounded_json_fields(tmp_path: Path, field: str, value: Any) -> None:
    payload = _wire()
    keys = field.split(".")
    container: Any = payload
    for key in keys[:-1]:
        container = container[int(key) if key.isdigit() else key]
    container[keys[-1]] = value
    _expect_wire_rejected(tmp_path, json.dumps(payload))


def _expect_wire_rejected(tmp_path: Path, wire: str) -> None:
    capture = _capture(tmp_path, _wav())
    provider = RecognitionProcess(_command(tmp_path, "wire", wire))
    with _binary(tmp_path, capture) as audio:
        with pytest.raises(
            RecognitionProcessError, match=r"^recognition worker result rejected$"
        ) as exc:
            provider.asr(audio, capture)
        assert exc.value.__context__ is None and exc.value.__cause__ is None


@pytest.mark.parametrize(
    "bad_wire",
    [
        lambda w: w.replace('"overlap": false', '"overlap": false, "overl\\u0061p": true'),
        lambda w: w.replace('"sample_quality": 1', '"sample_quality": NaN'),
        lambda w: w.replace('"sample_quality": 1', '"sample_quality": 1e999'),
        lambda w: "[" * 2000 + w + "]" * 2000,
        lambda w: w + w,
        lambda w: w.replace('"language": null, ', ""),
    ],
)
def test_reject_malformed_json(tmp_path: Path, bad_wire) -> None:
    _expect_wire_rejected(tmp_path, bad_wire(json.dumps(_wire())))


def test_launch_failure_is_sanitized(tmp_path: Path) -> None:
    capture = _capture(tmp_path, _wav())
    provider = RecognitionProcess((str(tmp_path / DIAGNOSTIC),))
    with _binary(tmp_path, capture) as audio:
        with pytest.raises(
            RecognitionProcessError, match=r"^recognition worker process rejected$"
        ) as exc:
            provider.asr(audio, capture)
        assert exc.value.__context__ is None


@pytest.mark.parametrize("command", [(), ("relative",), ("/bin/true", "--started-at=duplicate")])
def test_invalid_argv(command: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match=r"^invalid recognition process configuration$"):
        RecognitionProcess(command)
