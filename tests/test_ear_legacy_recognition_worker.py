from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import struct
import subprocess
import sys
import wave
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any

import pytest
from ear_adapter.convert import AmbientEvent, FileRef
from ear_adapter.ear_api import AudioChunk, Embedding, SpeechSegment
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import ASRResult, SpeakerResult, _ambient, _asr, _speakers

if TYPE_CHECKING:
    import ear_legacy_recognition_worker as worker
else:
    script = Path(__file__).resolve().parents[1] / "tools/ear_legacy_recognition_worker.py"
    spec = importlib.util.spec_from_file_location("ear_legacy_recognition_worker", script)
    assert spec and spec.loader
    worker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = worker
    spec.loader.exec_module(worker)

START = "2026-11-18T07:05:01.123456-05:00"
END = "2026-11-18T12:05:03.123456Z"
PROFILE = "synthetic-test"


def _wav(frames=32_000, *, silence=False, rate=16_000, channels=1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        f.writeframes((b"\0\0" if silence else b"\x10\x03") * frames * channels)
    return out.getvalue()


def _stream(raw: bytes) -> io.BytesIO:
    return io.BytesIO(raw)


def _append_chunk(raw: bytes, chunk: bytes) -> bytes:
    combined = raw + chunk
    return b"RIFF" + struct.pack("<I", len(combined) - 8) + combined[8:]


def _audio(raw: bytes | None = None, *, end: str = END) -> worker.Input:
    return worker.parse_input(_stream(_wav() if raw is None else raw), START, end)


class Fake:
    def __init__(self, audio: worker.Input) -> None:
        cid = audio.chunk_id
        self.calls: list[str] = []
        self.rows = [
            NS(
                speech_segment_id="seg1",
                audio_chunk_id=cid,
                diarization_scope_id=cid,
                local_speaker_id="S1",
                start_offset_ms=125,
                end_offset_ms=1500,
                text="synthetic 转写",
                language=None,
                overlap=False,
            ),
            NS(
                speech_segment_id="seg2",
                audio_chunk_id=cid,
                diarization_scope_id=cid,
                local_speaker_id="S2",
                start_offset_ms=1500,
                end_offset_ms=audio.duration_ms,
                text="synthetic",
                language="en",
                overlap=False,
            ),
        ]
        self.coverage = NS(
            total_duration_ms=audio.duration_ms,
            processed_duration_ms=audio.duration_ms,
            processed_ratio=1.0,
            gaps=[],
            scene_eligible=True,
        )
        self.resolutions = [
            NS(
                resolution_id="spk1",
                diarization_scope_id=cid,
                local_speaker_id="S1",
                evidence_segment_ids=["seg1"],
                sample_quality=0.8,
                embedding_status="generated",
                embedding_reason=None,
                embedding_ref="vector",
                model_version="/private/snapshot",
                embedding_provenance=NS(
                    model_version="/private/snapshot",
                    dimension=192,
                    dtype="float32",
                    normalization="l2",
                    preprocessing="ear-speaker-concat-pcm16-mono16k-v1",
                    speech_segment_ids=["seg1"],
                ),
            ),
            NS(
                resolution_id="spk2",
                diarization_scope_id=cid,
                local_speaker_id="S2",
                evidence_segment_ids=["seg2"],
                sample_quality=None,
                embedding_status="not_generated",
                embedding_reason="insufficient_speech",
                embedding_ref=None,
                embedding_provenance=None,
                model_version="/private/snapshot",
            ),
        ]
        self.values = [1.0] + [0.0] * 191
        self.events = [
            NS(
                audio_event_id="event1",
                audio_chunk_id=cid,
                taxonomy="audioset-527",
                label="Wind",
                start_offset_ms=0,
                end_offset_ms=100,
            )
        ]

    async def speech(self) -> Any:
        self.calls.append("asr")
        return NS(segments=self.rows, coverage=self.coverage)

    async def speakers(self, segments: list[Any]) -> list[Any]:
        self.calls.append("speakers")
        assert segments is self.rows
        return self.resolutions

    def vector(self, ref: Any) -> list[float]:
        self.calls.append("vector")
        assert ref == "vector"
        return self.values

    async def ambient(self) -> list[Any]:
        self.calls.append("ambient")
        return self.events


def _recognize(audio: worker.Input, provider: worker.Providers) -> Any:
    return asyncio.run(worker.recognize(audio, PROFILE, lambda: provider))


def test_complete_wire_is_accepted_by_native_stage_validators() -> None:
    audio = _audio(end="2026-11-18T12:05:03.123000Z")
    fake = Fake(audio)
    result = _recognize(audio, fake)
    assert fake.calls == ["asr", "speakers", "vector", "ambient"]
    wire = worker._encode(result)
    assert "/private" not in wire
    got = json.loads(wire)
    assert got["status"] == "complete"
    capture = CaptureInput(
        "cap",
        "device",
        "record",
        "group",
        audio.start,
        audio.end,
        "audio/wav",
        FileRef("sample.wav", "0" * 64, 64_044),
    )
    asr = ASRResult(
        got["asr"]["status"],
        tuple(AudioChunk.model_validate(c) for c in got["asr"]["chunks"]),
        tuple(SpeechSegment.model_validate(s) for s in got["asr"]["segments"]),
    )
    checked, _ = _asr(asr, capture)
    assert checked.segments[0].original_text == "synthetic 转写"
    assert checked.segments[0].started_at == audio.start + timedelta(milliseconds=125)
    assert checked.segments[1].ended_at == capture.ended_at
    speakers = tuple(
        SpeakerResult(
            row["resolution_id"],
            row["diarization_scope_id"],
            row["local_speaker_id"],
            tuple(row["evidence_segment_ids"]),
            Embedding.model_validate(row["embedding"]) if row["embedding"] else None,
            row["sample_quality"],
        )
        for row in got["speakers"]
    )
    assert _speakers(speakers, checked) == speakers
    assert speakers[1].embedding is None
    assert speakers[0].embedding and speakers[0].embedding.shape == (192,)
    events = tuple(
        AmbientEvent(
            **(row | {key: datetime.fromisoformat(row[key]) for key in ("started_at", "ended_at")})
        )
        for row in got["ambient"]
    )
    assert _ambient(events, capture) == events
    assert asdict(events[0])["original_label"] == "Wind"


def test_recognized_no_speech_still_runs_ambient() -> None:
    audio = _audio()
    fake = Fake(audio)
    fake.rows = []
    got = _recognize(audio, fake)
    assert fake.calls == ["asr", "ambient"]
    assert got["status"] == "complete"
    assert got["asr"]["segments"] == got["speakers"] == []
    assert len(got["ambient"]) == 1


def test_eligible_partial_is_not_a_complete_result() -> None:
    audio = _audio()
    fake = Fake(audio)
    fake.coverage.gaps = [NS(reason="secret")]
    fake.coverage.processed_duration_ms -= 1
    assert _recognize(audio, fake) == {
        "status": "partial",
        "stage": "asr",
        "asr": {"status": "partial", "chunks": [], "segments": []},
    }
    assert fake.calls == ["asr"]


@pytest.mark.parametrize(
    ("problem", "stage"),
    [
        ("bool_offset", "asr"),
        ("segment_scope", "asr"),
        ("duplicate_segment", "asr"),
        ("inconsistent_coverage", "asr"),
        ("missing_speaker", "speakers"),
        ("wrong_evidence", "speakers"),
        ("invalid_reason", "speakers"),
        ("missing_embedding", "speakers"),
        ("nonfinite", "speakers"),
        ("zero", "speakers"),
        ("bad_taxonomy", "ambient"),
        ("event_outside", "ambient"),
        ("duplicate_event", "ambient"),
    ],
)
def test_invalid_stage_drops_everything(problem: str, stage: str) -> None:
    audio = _audio()
    fake = Fake(audio)
    match problem:
        case "bool_offset":
            fake.rows[0].start_offset_ms = True
        case "segment_scope":
            fake.rows[0].diarization_scope_id = "another-chunk"
        case "duplicate_segment":
            fake.rows.append(fake.rows[0])
        case "inconsistent_coverage":
            fake.coverage.total_duration_ms += 1
        case "missing_speaker":
            fake.resolutions.pop()
        case "wrong_evidence":
            fake.resolutions[0].evidence_segment_ids = ["seg2"]
        case "invalid_reason":
            fake.resolutions[1].embedding_reason = "timeout"
        case "missing_embedding":
            fake.resolutions[0].embedding_ref = None
        case "nonfinite":
            fake.values[5] = float("nan")
        case "zero":
            fake.values[0] = 0.0
        case "bad_taxonomy":
            fake.events[0].taxonomy = "another-taxonomy"
        case "event_outside":
            fake.events[0].end_offset_ms = audio.duration_ms + 1
        case "duplicate_event":
            fake.events.append(fake.events[0])
    assert _recognize(audio, fake) == {"status": "failed", "stage": stage}


def test_provider_exception_is_sanitized_and_discards_asr() -> None:
    class Broken(Fake):
        async def ambient(self) -> list[Any]:
            raise RuntimeError("/private/model/path secret")

    audio = _audio()
    assert _recognize(audio, Broken(audio)) == {"status": "failed", "stage": "ambient"}


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        _wav()[:-1],
        _wav() + b"x",
        _wav() + _wav(),
        _wav(channels=2),
        _wav(rate=8000),
        _wav(15_999),
        _wav(480_001),
        _append_chunk(_wav(), b"data\x02\x00\x00\x00\x00\x00"),
    ],
)
def test_invalid_input(raw: bytes) -> None:
    with pytest.raises(worker.Rejected):
        _audio(raw)


def test_ancillary_chunks_and_rounded_tail() -> None:
    raw = _wav(32_001)
    meta = _append_chunk(raw, b"JUNK\x03\x00\x00\x00abc\x00")
    audio = _audio(meta)
    assert audio == _audio(raw)
    assert audio.duration_ms == 2001
    assert audio.span(NS(start_offset_ms=1999, end_offset_ms=2001))[1] == END
    with pytest.raises(worker.Rejected):
        _audio(raw, end="2026-11-18T12:05:03.122000Z")
    with pytest.raises(worker.Rejected):
        _audio(raw, end="2026-11-18T12:05:03.123456")


def test_portable_pcm_speaker_slice_without_external_decoder(tmp_path: Path) -> None:
    pcm = bytes(range(256)) * 125 + b"\x23\x42"
    path = tmp_path / "speaker.wav"
    worker._speaker_wav(
        path,
        pcm,
        [NS(start_offset_ms=1, end_offset_ms=3), NS(start_offset_ms=999, end_offset_ms=1001)],
    )
    with wave.open(str(path), "rb") as f:
        assert (f.getnchannels(), f.getsampwidth(), f.getframerate()) == (1, 2, 16_000)
        assert f.readframes(f.getnframes()) == pcm[32:96] + pcm[999 * 32 :]


def test_source_spotcheck_rejects_changed_or_symlinked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"fixture"
    monkeypatch.setattr(worker, "_SOURCE", {"audited.py": hashlib.sha256(content).hexdigest()})
    pkg = tmp_path / "ear"
    pkg.mkdir()
    path = pkg / "audited.py"
    path.write_bytes(content)
    assert worker._verify_source(tmp_path) == tmp_path
    path.write_bytes(b"changed")
    with pytest.raises(worker.Rejected):
        worker._verify_source(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(content)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(worker.Rejected):
        worker._verify_source(tmp_path)


def test_json_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "MAX_JSON", 8)
    with pytest.raises(worker.Rejected):
        worker._encode({"status": "failed", "stage": "input"})


@pytest.mark.parametrize(
    ("silent", "opt_in", "options", "code", "status"),
    [
        (
            True,
            True,
            ["--cpu-threads", "2", "--moss-cpu-fp32"],
            0,
            {
                "status": "complete",
                "asr": {"status": "complete", "chunks": [], "segments": []},
                "speakers": [],
                "ambient": [],
            },
        ),
        (False, True, [], 2, {"status": "failed", "stage": "setup"}),
        (True, False, [], 2, {"status": "failed", "stage": "input"}),
        (True, True, ["--cpu-threads", "0"], 2, {"status": "failed", "stage": "input"}),
        (True, True, ["--cpu-threads", "5"], 2, {"status": "failed", "stage": "input"}),
        (
            True,
            True,
            ["--device", "cuda", "--cpu-threads", "1"],
            2,
            {"status": "failed", "stage": "input"},
        ),
        (
            True,
            True,
            ["--device", "cuda", "--moss-cpu-fp32"],
            2,
            {"status": "failed", "stage": "input"},
        ),
    ],
)
def test_cli_no_assets_or_old_imports(
    tmp_path: Path,
    silent: bool,
    opt_in: bool,
    options: list[str],
    code: int,
    status: dict[str, Any],
) -> None:
    command = [
        sys.executable,
        "-I",
        "-B",
        str(worker.__file__),
        "--started-at",
        START,
        "--ended-at",
        END,
        "--model-profile",
        PROFILE,
        *options,
    ]
    for asset in ("source", "moss", "ecapa", "panns-checkpoint", "panns-labels"):
        command += [f"--{asset}", str(tmp_path / "missing")]
    if opt_in:
        command.append("--offline-isolated")
    result = subprocess.run(
        command,
        env={"TMPDIR": str(tmp_path)},
        input=_wav(silence=silent),
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == code
    assert result.stderr == b""
    assert result.stdout.count(b"\n") == 1
    assert json.loads(result.stdout) == status
    assert list(tmp_path.iterdir()) == []


def test_offline_cache_and_cpu_environment_are_private(tmp_path: Path) -> None:
    # Run in a separate process: the worker changes cwd/env and installs an audit hook.
    check = (
        "import json, os, runpy, sys; from pathlib import Path; "
        "runpy.run_path(sys.argv[1])['_offline'](Path(sys.argv[2]), 2); "
        "print(json.dumps(dict(os.environ)))"
    )
    response = subprocess.run(
        [sys.executable, "-I", "-B", "-c", check, str(worker.__file__), str(tmp_path)],
        env={"TORCHINDUCTOR_CACHE_DIR": "/outside", "OMP_NUM_THREADS": "88", "LEAK": "fake"},
        capture_output=True,
        check=True,
        timeout=10,
    )
    env = json.loads(response.stdout)
    assert env["TORCHINDUCTOR_CACHE_DIR"] == str(tmp_path / "inductor")
    assert env["NUMBA_CACHE_DIR"] == str(tmp_path / "numba")
    assert env["MPLCONFIGDIR"] == str(tmp_path / "matplotlib")
    assert env["OMP_NUM_THREADS"] == env["MKL_NUM_THREADS"] == "2"
    assert env["OPENBLAS_NUM_THREADS"] == env["NUMBA_NUM_THREADS"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1" and "LEAK" not in env


@pytest.mark.parametrize("tuned", [False, True])
def test_audited_constructor_and_request_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tuned: bool
) -> None:
    """Exercise actual bridge wiring with doubles, not imports from the legacy venv."""
    audio = _audio()
    fake = Fake(audio)
    for row in fake.resolutions:
        row.embedding_status = "not_generated"
        row.embedding_reason = "quality_rejected"
        row.embedding_ref = row.embedding_provenance = row.sample_quality = None
    root = tmp_path / "scratch"
    root.mkdir()
    moss = tmp_path / "moss"
    moss.mkdir()
    (moss / "config.json").touch()
    ecapa = tmp_path / "ecapa"
    ecapa.mkdir()
    (ecapa / "hyperparams.yaml").touch()
    checkpoint = tmp_path / "panns.pth"
    checkpoint.write_bytes(b"fixture-not-a-model")
    labels = tmp_path / "labels.csv"
    labels.write_text("index,mid,display_name\n" + "".join(f"{i},x,label\n" for i in range(527)))

    tuning_calls: list[tuple[str, int]] = []
    conversions: list[str] = []

    class FakeModel:
        dtype = "bf16"

        def float(self):
            conversions.append(self.dtype)
            self.dtype = "fp32"
            return self

    class Moss:
        def __init__(
            self,
            store,
            *,
            model_id,
            device,
            inference_timeout,
            local_files_only,
            inference_window_ms,
        ):
            assert model_id == str(moss) and device == "cpu" and local_files_only is True
            assert inference_timeout == worker.TIMEOUT and inference_window_ms == worker.MAX_MS
            self.store = store
            self.model = FakeModel()

        def _load(self):
            return self.model, object(), NS(type="cpu"), "test-version"

        async def process(self, chunk, context):
            for _ in range(2):
                assert self._load()[0].dtype == ("fp32" if tuned else "bf16")
            assert context.idempotency_key == chunk.audio_chunk_id == audio.chunk_id
            assert self.store.resolve_path(chunk.audio_ref) == root / "input.wav"
            assert chunk.format.sample_rate_hz == 16_000
            assert chunk.started_at == audio.start
            return await fake.speech()

    class Encoder:
        def __init__(self, *, savedir, device, inference_timeout, local_files_only):
            assert savedir == ecapa and device == "cpu" and local_files_only is True
            assert inference_timeout == worker.TIMEOUT

    class Speaker:
        def __init__(self, store, encoder, *, inference_timeout):
            assert isinstance(encoder, Encoder) and inference_timeout == worker.TIMEOUT

        async def _cut_speaker_audio(self, chunk, source, speaker, segments) -> Path:
            raise AssertionError("the bridge must supply its own PCM slicer")

        async def resolve(self, request, context):
            assert request.audio_chunk.audio_chunk_id == context.idempotency_key
            clip = await self._cut_speaker_audio(
                request.audio_chunk, root / "input.wav", "S1", request.segments[:1]
            )
            with wave.open(str(clip), "rb") as f:
                assert f.getnframes() == 1375 * 16
            return await fake.speakers(fake.rows)

    class Panns:
        def __init__(
            self,
            store,
            *,
            checkpoint_path,
            device,
            inference_timeout,
            local_files_only,
            inference_window_ms,
        ):
            assert checkpoint_path == str(checkpoint) and device == "cpu" and local_files_only
            assert inference_timeout == worker.TIMEOUT and inference_window_ms == worker.MAX_MS
            assert (
                root / "panns_data/class_labels_indices.csv"
            ).read_bytes() == labels.read_bytes()

        async def detect(self, chunk, context):
            assert chunk.audio_chunk_id == context.idempotency_key
            return await fake.ambient()

    doubles = {
        "ear.models.audio": NS(ArtifactRef=NS, AudioChunk=NS, AudioFormat=NS, EvidenceRef=NS),
        "ear.models.processing": NS(ProcessingContext=NS),
        "ear.models.speaker": NS(SpeakerResolutionRequest=NS),
        "ear.providers.speech_moss": NS(MossSpeechUnderstandingModule=Moss),
        "ear.providers.speaker_anonymous": NS(AnonymousSpeakerResolutionModule=Speaker),
        "ear.providers.speaker_ecapa_encoder": NS(EcapaSpeakerEncoder=Encoder),
        "ear.providers.environment_panns": NS(PannsAudioEventModule=Panns),
        "numpy": NS(),
        "torch": NS(
            float32="fp32",
            set_num_threads=lambda n: tuning_calls.append(("intra", n)),
            set_num_interop_threads=lambda n: tuning_calls.append(("inter", n)),
        ),
    }
    monkeypatch.setattr(worker.importlib, "import_module", doubles.__getitem__)
    monkeypatch.setattr(worker.sys, "path", worker.sys.path.copy())
    monkeypatch.setattr(worker, "_require_launch", lambda: None)
    monkeypatch.setattr(worker, "_verify_source", lambda path: path)
    assets = worker.Assets(
        tmp_path, moss, ecapa, checkpoint, labels, "cpu", 2 if tuned else None, tuned
    )
    result = asyncio.run(
        worker.recognize(audio, PROFILE, lambda: worker.LegacyProviders(audio, assets, root))
    )
    assert result["status"] == "complete"
    assert fake.calls == ["asr", "speakers", "ambient"]
    assert tuning_calls == ([("intra", 2), ("inter", 1)] if tuned else [])
    assert conversions == (["bf16"] if tuned else [])
