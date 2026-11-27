from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from ear_adapter import recognition_decode as decode
from ear_adapter import recognition_process as process
from ear_adapter.config import AdapterConfig
from ear_adapter.convert import FileRef
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import AmbientEvent, ASRResult, ingest_native, native_audio_ref_id
from ear_adapter.recognition_decode import (
    FFmpegDecoder,
    NormalizingRecognitionProvider,
    RecognitionDecodeError,
)
from ear_adapter.runtime import AlreadyIngested, Failed, Ingested
from ear_contracts import RawRecord
from ear_storage_client import MemoryStorage

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux decoder transport")
START = datetime(2000, 1, 1, tzinfo=UTC)
MARKER = "SYNTHETIC_PRIVATE"
CFG = AdapterConfig(channel="octopus", adapter_version="ear-native/0.1.0+decode-fixture")
DECODER = FFmpegDecoder("/trusted/ffmpeg", "/trusted/ffprobe")


def _box(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, tag) + payload


def _fake_media(*, extra: bytes = b"") -> bytes:
    return _box(b"ftyp", b"M4A \0\0\0\0") + _box(b"mdat", b"x" * 17 + extra) + _box(b"moov", b"x")


def _capture(
    raw: bytes, *, ms: int = 1000, media: str = "audio/mp4", name: str = "m4a"
) -> CaptureInput:
    return CaptureInput(
        "capture",
        "device",
        "record",
        "group",
        START,
        START + timedelta(milliseconds=ms),
        media,
        FileRef(f"{MARKER}.{name}", hashlib.sha256(raw).hexdigest(), len(raw)),
    )


def _wav(pcm: bytes, *, rate: int = 16_000, channels: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        writer.writeframes(pcm)
    return output.getvalue()


def _probe(*, aac: bool = True) -> tuple[dict, list[dict]]:
    stream = dict(
        codec_name="aac" if aac else "alac",
        profile="LC",
        codec_type="audio",
        channels=1,
        sample_rate="16000",
        time_base="1/16000",
        start_pts=0,
        duration_ts=16_000,
    )
    packets: list[dict[str, Any]] = [
        dict(pts=1024 * i, duration=min(1024, 16000 - 1024 * i), pos=str(25 + i), size="1")
        for i in range(-1 if aac else 0, 16)
    ]
    if aac:
        packets[0]["side_data_list"] = [
            dict(
                side_data_type="Skip Samples",
                skip_samples=1024,
                discard_padding=0,
            )
        ]
    else:
        packets[0].update(pos="24", size="2")
    return stream, packets


def _mock_run(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    streams: list[dict],
    packets: list[dict],
    pcm: bytes,
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    deadlines: list[float] = []

    def run(argv: tuple[str, ...], source: BinaryIO, cap: int, deadline: float) -> bytearray:
        calls.append(argv)
        deadlines.append(deadline)
        source.seek(0)
        assert source.read() == raw
        assert fcntl.fcntl(source, getattr(fcntl, "F_GET_SEALS", 1024 + 10)) == 0xF
        assert not any(MARKER in arg or arg.startswith("http") for arg in argv)
        for key, value in (
            ("-f", "mov"),
            ("-format_whitelist", "mov"),
            ("-protocol_whitelist", "fd"),
            ("-max_streams", "1"),
            ("-enable_drefs", "0"),
            ("-use_absolute_path", "0"),
        ):
            assert argv[argv.index(key) + 1] == value
        if argv[0] == "/trusted/ffprobe":
            assert argv[-1] == "fd:" and cap == decode._PROBE_CAP
            return bytearray(json.dumps(dict(streams=streams, packets=packets)).encode())
        assert argv[0] == "/trusted/ffmpeg" and deadlines[-1] == deadlines[-2]
        assert argv[argv.index("-codec_whitelist") + 1] == streams[0]["codec_name"]
        assert argv[argv.index("-i") + 1] == "fd:"
        assert argv[argv.index("-map") + 1] == "0:a:0"
        assert argv[-3:] == ("-f", "s16le", "pipe:1")
        assert len(pcm) <= cap
        assert not {"-t", "-to", "-shortest", "-frames:a", "-fs"}.intersection(argv)
        return bytearray(pcm)

    monkeypatch.setattr(decode, "_run", run)
    return calls


@pytest.mark.parametrize("tail", (0, 384))
def test_decode_trims_only_packet_declared_aac_padding(monkeypatch: pytest.MonkeyPatch, tail: int):
    raw = _fake_media()
    stream, packets = _probe()
    audio = io.BytesIO(raw)
    samples = b"\x01\x02" * 16_000
    calls = _mock_run(monkeypatch, raw, [stream], packets, samples + b"\x03\x04" * tail)
    assert DECODER.decode(audio, _capture(raw)) == _wav(samples)
    assert not audio.closed and audio.tell() == len(raw) and len(calls) == 2


@pytest.mark.parametrize("frames", (15_999, 16_001, 16_383))
def test_unexplained_pcm_lengths_rejected(monkeypatch: pytest.MonkeyPatch, frames: int):
    raw = _fake_media()
    stream, packets = _probe()
    _mock_run(monkeypatch, raw, [stream], packets, b"\0\0" * frames)
    with pytest.raises(RecognitionDecodeError, match="normalization rejected"):
        DECODER.decode(io.BytesIO(raw), _capture(raw))


@pytest.mark.parametrize(
    "fault",
    (
        "second-stream",
        "codec",
        "profile",
        "clock",
        "starts-late",
        "long",
        "priming",
        "packet-gap",
        "packet-overlap",
        "unaccounted-bytes",
        "internal-short",
        "external-offset",
    ),
)
def test_probe_rejects_unsupported_or_partial_timeline(monkeypatch: pytest.MonkeyPatch, fault: str):
    raw = _fake_media(extra=b"!" if fault == "unaccounted-bytes" else b"")
    stream, packets = _probe()
    streams = [stream, stream] if fault == "second-stream" else [stream]
    ms = 1000
    if fault == "codec":
        stream["codec_name"] = "mp3"
    elif fault == "profile":
        stream["profile"] = "HE-AAC"
    elif fault == "clock":
        ms += 2
    elif fault == "starts-late":
        stream["start_pts"] = 1
    elif fault == "long":
        stream["duration_ts"], ms = 480_001, 30_000
    elif fault == "priming":
        del packets[0]["side_data_list"]
    elif fault == "packet-gap":
        packets[2]["pts"] += 1
    elif fault == "packet-overlap":
        packets[2]["pos"] = packets[1]["pos"]
    elif fault == "internal-short":
        packets[2]["duration"] -= 1
    elif fault == "external-offset":
        packets[0]["pos"] = "99999"
    calls = _mock_run(monkeypatch, raw, streams, packets, b"")
    with pytest.raises(RecognitionDecodeError):
        DECODER.decode(io.BytesIO(raw), _capture(raw, ms=ms))
    assert len(calls) == 1


def test_alac_no_trim(monkeypatch: pytest.MonkeyPatch):
    raw = _fake_media()
    stream, packets = _probe(aac=False)
    pcm = b"\x01\x00" * 16_000
    _mock_run(monkeypatch, raw, [stream], packets, pcm)
    assert DECODER.decode(io.BytesIO(raw), _capture(raw)) == _wav(pcm)


def test_checked_wav_and_input_preflight(monkeypatch: pytest.MonkeyPatch):
    def never(*_):
        pytest.fail("unexpected process")

    monkeypatch.setattr(decode, "_run", never)
    wav = _wav(b"\x01\x00" * 16_000)
    info = struct.pack("<4sI8s", b"LIST", 8, b"metadata")
    with_info = b"RIFF" + struct.pack("<I", len(wav) + len(info) - 8) + wav[8:] + info
    cap = _capture(with_info, media="audio/x-wav", name="wav")
    assert DECODER.decode(io.BytesIO(with_info), cap) == wav
    raw = _fake_media()
    rejects = (
        (with_info, replace(cap, ended_at=cap.ended_at + timedelta(milliseconds=2))),
        (with_info[:-1], cap),
        (wav, _capture(wav)),  # unsupported bytes under a claimed MOV media type
        (raw + _box(b"moof", b"x"), _capture(raw + _box(b"moof", b"x"))),
        (raw + b"!", _capture(raw)),
        (raw, _capture(raw, ms=30_001)),
        (raw, _capture(raw, ms=999)),
        (raw, _capture(raw, media="audio/aac", name="aac")),
        (raw, replace(_capture(raw), file_ref=FileRef("x.m4a", "a" * 64, decode._INPUT_CAP + 1))),
    )
    for payload, capture in rejects:
        with pytest.raises(RecognitionDecodeError):
            DECODER.decode(io.BytesIO(payload), capture)
    moved = io.BytesIO(raw)
    moved.seek(1)
    with pytest.raises(RecognitionDecodeError):
        DECODER.decode(moved, _capture(raw))


@pytest.mark.parametrize(
    "script,cap,budget,message",
    (
        ("os.write(1,b'x' * 65536); time.sleep(10)", 32, 1.0, "output limit"),
        ("os.close(1); time.sleep(10)", 32, 0.2, "timed out"),
        ("sys.stderr.write('SYNTHETIC_PRIVATE'); sys.exit(3)", 32, 1.0, "decoder failed"),
        ("if os.fork() == 0: time.sleep(10)", 32, 1.0, "left stdout open"),
    ),
)
def test_process_transport_fails_closed(
    script: str, cap: int, budget: float, message: str, capfd: pytest.CaptureFixture[str]
):
    with os.fdopen(os.memfd_create(""), "w+b", buffering=0) as source:
        command = (sys.executable, "-I", "-B", "-c", "import os,sys,time\n" + script)
        start = time.monotonic()
        with pytest.raises(RecognitionDecodeError, match=message):
            decode._run(command, source, cap, start + budget)
    assert time.monotonic() - start < 2
    assert capfd.readouterr() == ("", "")


def test_real_process_transport_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(MARKER, "credential")
    with (tmp_path / "private").open("wb") as private:
        os.set_inheritable(private.fileno(), True)
        script = f"""import os, sys
assert not (set(os.environ) - {{'LC_CTYPE'}}) and os.getcwd() == '/'
assert os.getpid() == os.getsid(0) == os.getpgrp()
assert not os.path.exists('/proc/self/fd/{private.fileno()}')
assert os.read(0, 32) == b'hello'
assert os.lseek(0, 0, os.SEEK_SET) == 0
assert os.read(0, 32) == b'hello'
assert sys.flags.isolated and sys.dont_write_bytecode
os.write(1, b'exact')
"""
        with os.fdopen(os.memfd_create(""), "w+b", buffering=0) as source:
            source.write(b"hello")
            cmd = (sys.executable, "-I", "-B", "-c", script)
            assert decode._run(cmd, source, 5, time.monotonic() + 1) == b"exact"


def test_native_with_recognition_process_preserves_original_ref_and_dedup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    raw = _fake_media()
    capture = _capture(raw)
    (tmp_path / capture.file_ref.path).write_bytes(raw)
    stream, packets = _probe()
    samples = b"\x01\x00" * 16_000
    calls = _mock_run(monkeypatch, raw, [stream], packets, samples)
    provider = NormalizingRecognitionProvider(
        process.RecognitionProcess(("/trusted/worker",)), DECODER
    )
    span = dict(started_at="2000-01-01T00:00:00Z", ended_at="2000-01-01T00:00:01Z")
    model_calls = []

    def fake_model(argv: tuple[str, ...], wav: bytes, _: float) -> bytearray:
        model_calls.append(argv)
        assert wav == _wav(samples)
        assert argv == (
            "/trusted/worker",
            "--started-at",
            span["started_at"],
            "--ended-at",
            span["ended_at"],
        )
        row = dict(**span, audio_chunk_id="chunk", model_version="fixture")
        seg = dict(
            **row,
            speech_segment_id="seg",
            original_text="synthetic",
            language=None,
            local_speaker_id="s",
            diarization_scope_id="chunk",
            overlap=False,
        )
        speaker = dict(
            resolution_id="speaker",
            local_speaker_id="s",
            diarization_scope_id="chunk",
            evidence_segment_ids=["seg"],
            embedding=None,
            sample_quality=None,
        )
        return bytearray(
            json.dumps(
                dict(
                    status="complete",
                    asr=dict(status="complete", chunks=[row], segments=[seg]),
                    speakers=[speaker],
                    ambient=[],
                )
            ).encode()
        )

    monkeypatch.setattr(process, "_run", fake_model)
    store = MemoryStorage()
    client = store.client("adapter")

    def ingest(cap: CaptureInput):
        return ingest_native(cap, root=tmp_path, provider=provider, storage=client, config=CFG)

    assert isinstance(ingest(capture), Ingested)
    marker = client.get(native_audio_ref_id(capture, CFG))
    assert isinstance(marker, RawRecord)
    assert marker.payload["file_ref"] == capture.file_ref.to_json()
    assert marker.payload["media_type"] == capture.media_type
    assert marker.payload["upstream"] == {
        "service": "ear-native",
        "capture_id": capture.capture_id,
        "record_id": capture.record_id,
        "group_id": capture.group_id,
    }
    assert (marker.event_start, marker.event_end) == (capture.started_at, capture.ended_at)
    assert isinstance(ingest(capture), AlreadyIngested)
    assert len(calls) == 2 and len(model_calls) == 1 and provider._pending is None

    mismatch = replace(capture, ended_at=capture.ended_at + timedelta(milliseconds=50))
    assert isinstance(ingest(mismatch), Failed)
    assert client.get(native_audio_ref_id(mismatch, CFG)) is None
    assert len(calls) == 3 and len(model_calls) == 1


def test_wrapper_only_passes_private_descriptor_and_discards_wrong_stage(
    monkeypatch: pytest.MonkeyPatch,
):
    raw = _fake_media()
    capture = _capture(raw)
    original = io.BytesIO(raw)
    stream, packets = _probe()
    samples = b"\0\0" * 16_000
    _mock_run(monkeypatch, raw, [stream], packets, samples)
    seen: list[BinaryIO] = []
    descriptor: list[CaptureInput] = []

    expected = capture

    class FakeProvider:
        def asr(self, audio: BinaryIO, capture: CaptureInput) -> ASRResult:
            seen.append(audio)
            descriptor.append(capture)
            assert audio.tell() == 0 and audio.read() == _wav(samples)
            assert (
                replace(capture, media_type=expected.media_type, file_ref=expected.file_ref)
                == expected
            )
            assert (
                capture.file_ref.to_json()
                == FileRef(
                    "normalized.wav", hashlib.sha256(_wav(samples)).hexdigest(), len(_wav(samples))
                ).to_json()
            )
            return ASRResult("complete")

        def speakers(self, audio: BinaryIO, capture: CaptureInput, asr: ASRResult):
            pytest.fail("no speakers expected")

        def ambient(self, audio: BinaryIO, capture: CaptureInput) -> tuple[AmbientEvent, ...]:
            assert audio is seen[-1] and capture is descriptor[-1] and audio.tell() == 0
            return ()

    wrapped = NormalizingRecognitionProvider(FakeProvider(), DECODER)
    assert wrapped.asr(original, capture) == ASRResult("complete")
    with pytest.raises(RecognitionDecodeError, match="stage mismatch"):
        wrapped.ambient(io.BytesIO(raw), capture)
    assert seen[0].closed and not original.closed
    original.seek(0)
    wrapped.asr(original, capture)
    original.seek(0)
    assert wrapped.ambient(original, capture) == ()
    assert seen[1].closed and not original.closed


@pytest.mark.parametrize(
    "codec,rate,ms,channels",
    [
        ("aac", 16_000, 1000, 1),
        ("aac", 48_000, 30_000, 1),
        ("aac", 44_100, 1123, 2),
        ("alac", 44_100, 1120, 1),
        ("aac", 48_000, 30_020, 1),
    ],
)
def test_optional_ffmpeg_synthetic_roundtrip(codec: str, rate: int, ms: int, channels: int):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("requires local FFmpeg and ffprobe")
    for binary in (ffmpeg, ffprobe):
        protocols = subprocess.check_output(
            (binary, "-protocols"), stderr=subprocess.DEVNULL, env={}, timeout=5
        )
        if b"fd" not in protocols.split():
            pytest.skip("requires seekable fd protocol")
    encoders = subprocess.check_output(
        (ffmpeg, "-encoders"), stderr=subprocess.DEVNULL, env={}, timeout=5
    )
    if not any(line.split()[1:2] == [codec.encode()] for line in encoders.splitlines()):
        pytest.skip("requires native synthetic fixture encoder")
    with os.fdopen(os.memfd_create(""), "w+b", buffering=0) as fixture:
        subprocess.run(
            (
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate={rate}:duration={ms / 1000}",
                "-ac",
                str(channels),
                "-c:a",
                codec,
                "-f",
                "ipod",
                "fd:",
            ),
            stdout=fixture,
            stderr=subprocess.DEVNULL,
            env={},
            timeout=10,
            check=True,
        )
        fixture.seek(0)
        raw = fixture.read(decode._INPUT_CAP + 1)
    decoder = FFmpegDecoder(ffmpeg, ffprobe)
    if ms > 30_000:
        with pytest.raises(RecognitionDecodeError):
            decoder.decode(io.BytesIO(raw), _capture(raw, ms=30_000))
        return
    decoded = decoder.decode(io.BytesIO(raw), _capture(raw, ms=ms))
    with wave.open(io.BytesIO(decoded)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == ms * 16
        assert any(wav.readframes(wav.getnframes()))
        assert len(decoded) == 44 + 32 * ms
    with pytest.raises(RecognitionDecodeError):
        decoder.decode(io.BytesIO(raw), _capture(raw, ms=ms - 3 if ms > 1000 else 1003))


def test_spooled_m4a_without_end_uses_one_validated_decode(monkeypatch, tmp_path):
    from ear_adapter.recognition_long import ChunkedRecognitionProvider
    from ear_adapter.recording_intake import RecordingIntake
    from ear_adapter.recording_worker import NativeRecordingPreparation, NativeRecordingPublication
    from ear_storage_client import Query

    raw = _fake_media()
    stream, packets = _probe()
    called = []

    def run(command, source, cap, deadline, *, sink=None):
        source.seek(0)
        assert source.read() == raw
        assert fcntl.fcntl(source, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        called.append((command[0], deadline))
        if sink is None:
            data = bytearray(json.dumps({"streams": [stream], "packets": packets}).encode())
            assert len(data) < cap
            return data, len(data)
        # Decoder emits the final partial AAC packet with padding. This must be
        # validated and trimmed before the duration or model WAV is produced.
        assert cap == 2 * 16_384
        sink.write(b"\0\0" * 16_000 + b"\xff\xff" * 384)
        return bytearray(), cap

    monkeypatch.setattr(decode, "_run_to", run)
    model = process.RecognitionProcess(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            """
import sys
assert sys.argv[1:] == ['--started-at', '2000-01-01T00:00:00Z',
                        '--ended-at', '2000-01-01T00:00:01Z']
wav = sys.stdin.buffer.read()
assert len(wav) == 32044 and not any(wav[44:])
print('{"status":"complete","asr":{"status":"complete","chunks":[],"segments":[]},'
      '"speakers":[],"ambient":[]}')
""",
        )
    )
    provider = ChunkedRecognitionProvider(model, DECODER, scratch_dir=str(tmp_path))
    intake = RecordingIntake(tmp_path / "spool")
    job = intake.accept(
        io.BytesIO(raw),
        source="web",
        source_id=None,
        started_at=START.isoformat(),
        media_type="audio/mp4",
    )
    storage = MemoryStorage().client("adapter")
    with intake.worker() as worker:
        assert worker.run_one(
            NativeRecordingPreparation(
                provider,
                CFG,
                normalized_duration=lambda job, audio: provider.decoded_duration(
                    audio, media_type=job.media_type, size=job.size
                ),
            ),
            NativeRecordingPublication(storage),
        )
    status = intake.get(job.id)
    assert status is not None and status["state"] == "succeeded"
    (ref,) = storage.query(Query("audio_ref")).items
    assert (ref.event_start, ref.event_end) == (START, START + timedelta(seconds=1))
    assert [name for name, _ in called] == ["/trusted/ffprobe", "/trusted/ffmpeg"]
    assert called[0][1] == called[1][1]
