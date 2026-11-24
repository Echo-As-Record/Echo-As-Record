"""One-shot offline recognition bridge; based on old Ear eef64ce and native_pipeline.py.

Use the trusted pinned venv's Python with ``-I -B``, an empty environment, a
private disposable TMPDIR, read-only mounts for a pristine old source export
and trusted model assets, and OS-enforced no egress (including local services).
Do not mount credentials, production data/config or caches. MOSS executes
snapshot Python; ECAPA/PANNs load trusted checkpoint formats. Neither offline
flags, the source spot-check nor Python audit hooks are a sandbox or model
attestation. The launcher must verify the whole export/venv/model bundle, limit
memory/CPU, enforce a hard process deadline and remove scratch even on kill.

Post-normalization only: stdin must be exactly one bounded RIFF/WAVE PCM16LE
mono 16 kHz file, 1–30 seconds. No decoding, resampling, scan or concatenated
inputs. Start/end are aware RFC 3339; duration must match PCM within 1 ms.
Output ends at the lesser of the frame-derived end and capture end. Provider
millisecond endpoints may round up by <1 ms and are clipped to that end.
Exact all-zero PCM is a complete empty result, without importing providers.

Stdout: one JSON object. Exit 0 = complete (asr/speakers/ambient); 3 = partial
ASR (no usable data); 2 = failed (fixed stage, no data). Only consume a bounded,
complete JSON response after a clean exit 0. A hard timeout, crash or malformed
response is failure. Keep stdout private: it contains transcripts/voiceprints.
Provide a public model-profile label that changes with the exact asset bundle;
no local path, runtime identity or capture ID is emitted. This is not wired to
native ingestion; a caller must reconstruct the native Python result types.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import io
import json
import math
import os
import re
import signal
import struct
import sys
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal, NoReturn, Protocol, TypedDict

PIN = "eef64ce933fc06e9a5e441a546cb2265f1394ef7"
RATE = 16_000
MAX_MS = 30_000
MAX_WAV = 2 * RATE * 30 + 65_536
TIMEOUT = 240
MAX_ROWS = 1024
MAX_JSON = 4 * 1024 * 1024
ECAPA_DIM = 192
_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)")
# SHA-256 of selected git-show blobs, not of the deployed worktree files.
_SOURCE = {
    "models/audio.py": "1d26679cfb5e5c54b111e7802e1ade23c984474ec7e87792986f46221096f873",
    "models/coverage.py": "23f9a22090931cb618b4ad692839b1e8a1057370f36a4fa56041e960bcd60db8",
    "models/processing.py": "d5a5dfd66d9859e584890d868b367845612f958314b3e5af6887404cb5d83195",
    "models/speaker.py": "12964f3d2d578c03988b25776ed13b127c49c171781d755d982289399d5a7333",
    "providers/speech_moss.py": (
        "baaca18fb7bb505cbe2122ead0756ad48f8f58f8a798f40791bd5d60df180655"
    ),
    "providers/speech_moss_bounded.py": (
        "18751de314a7af992812d9bf534017fdc4c63a2d687fa5650515dba39f776e48"
    ),
    "providers/speech_moss_loading.py": (
        "6b582d21de7b6fbe68b90f892d2049f467b7ed70fd915362c8c7e07fff10e416"
    ),
    "providers/speaker_anonymous.py": (
        "a5c8a191a57ba5dddd565cd8453e6af20459b35db6d06d68daa6d3535eb5d80e"
    ),
    "providers/speaker_ecapa_encoder.py": (
        "460be29fbef2b2c0f58403de54af03d66bb2759faa098d3e2f6b3321679f2647"
    ),
    "providers/environment_panns.py": (
        "f85ba9ff2c61baa01fbb807a87ad100c79e24b9f6ce93cb2d8e6d2cb8aeccbf7"
    ),
    "providers/environment_windows.py": (
        "0c34583123a0c73f4efeea535454639f3e2372cecc9f53837a49df55b4ed06f7"
    ),
}

Stage = Literal["input", "setup", "asr", "speakers", "ambient", "output"]


class ChunkJSON(TypedDict):
    audio_chunk_id: str
    started_at: str
    ended_at: str
    model_version: str


class SegmentJSON(ChunkJSON):
    speech_segment_id: str
    original_text: str
    language: str | None
    local_speaker_id: str
    diarization_scope_id: str
    overlap: bool


class ASRJSON(TypedDict):
    status: Literal["complete", "partial"]
    chunks: list[ChunkJSON]
    segments: list[SegmentJSON]


class EmbeddingJSON(TypedDict):
    model_version: str
    shape: list[int]
    dtype: Literal["float32"]
    values: list[float]
    provenance: dict[str, str | list[str]]


class SpeakerJSON(TypedDict):
    resolution_id: str
    diarization_scope_id: str
    local_speaker_id: str
    evidence_segment_ids: list[str]
    embedding: EmbeddingJSON | None
    sample_quality: float | None


class AmbientJSON(TypedDict):
    audio_event_id: str
    started_at: str
    ended_at: str
    original_label: str
    taxonomy: str
    model_version: str


class Complete(TypedDict):
    status: Literal["complete"]
    asr: ASRJSON
    speakers: list[SpeakerJSON]
    ambient: list[AmbientJSON]


class Partial(TypedDict):
    status: Literal["partial"]
    stage: Literal["asr"]
    asr: ASRJSON


class Failed(TypedDict):
    status: Literal["failed"]
    stage: Stage


class Rejected(ValueError):
    """Invalid input or invalid provider result; safe to discard the batch."""


def _require(ok: bool) -> None:
    if not ok:
        raise Rejected


def _utc(value: str) -> datetime:
    _require(_TIME.fullmatch(value) is not None and not value.endswith("-00:00"))
    return datetime.fromisoformat(value).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _id(value: Any) -> str:
    _require(type(value) is str and _ID.fullmatch(value) is not None and value not in (".", ".."))
    return value


def _text(value: Any, limit: int = 16_384) -> str:
    _require(type(value) is str and 0 < len(value) <= limit and bool(value.strip()))
    _require("\0" not in value)
    value.encode("utf-8")
    return value


def _max_list(value: Any) -> list[Any]:
    _require(type(value) is list and len(value) <= MAX_ROWS)
    return value


@dataclass(frozen=True)
class Input:
    pcm: bytes
    start: datetime
    end: datetime
    duration_ms: int
    chunk_id: str

    def span(self, row: Any) -> tuple[str, str]:
        lo, hi = row.start_offset_ms, row.end_offset_ms
        _require(type(lo) is int and type(hi) is int and 0 <= lo < hi <= self.duration_ms)
        left = self.start + timedelta(milliseconds=lo)
        right = min(self.start + timedelta(milliseconds=hi), self.end)
        _require(left < right)
        return _iso(left), _iso(right)


def parse_input(stream: BinaryIO, started_at: str, ended_at: str) -> Input:
    start, capture_end = _utc(started_at), _utc(ended_at)
    raw = stream.read(MAX_WAV + 1)
    _require(44 <= len(raw) <= MAX_WAV and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE")
    _require(struct.unpack_from("<I", raw, 4)[0] == len(raw) - 8)
    pos, formatted, pcm = 12, False, None
    while pos < len(raw):
        _require(pos + 8 <= len(raw))
        tag, size = struct.unpack_from("<4sI", raw, pos)
        left = pos + 8
        pos = left + size + size % 2
        _require(pos <= len(raw))
        if tag == b"fmt ":
            _require(not formatted and size == 16)
            _require(struct.unpack_from("<HHIIHH", raw, left) == (1, 1, RATE, RATE * 2, 2, 16))
            formatted = True
        elif tag == b"data":
            _require(formatted and pcm is None and 2 * RATE <= size <= 2 * RATE * 30)
            _require(size % 2 == 0)
            pcm = raw[left : left + size]
        else:
            # Recognize benign common metadata, but never pass it to model readers.
            _require(tag in (b"LIST", b"JUNK", b"bext", b"iXML", b"fact", b"PAD "))
    if pcm is None:
        raise Rejected
    frames = len(pcm) // 2
    end = start + timedelta(microseconds=round(frames * 1_000_000 / RATE))
    _require(abs(end - capture_end) <= timedelta(milliseconds=1))
    digest = hashlib.sha256(pcm)
    digest.update(_iso(start).encode("ascii"))
    digest.update(_iso(capture_end).encode("ascii"))
    # Native record IDs add the caller's capture identity, not just this local key.
    return Input(
        pcm, start, min(end, capture_end), (frames + 15) // 16, "chunk_" + digest.hexdigest()
    )


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(pcm)


def _speaker_wav(path: Path, pcm: bytes, segments: list[Any]) -> None:
    parts = []
    for row in _max_list(segments):
        lo, hi = row.start_offset_ms, row.end_offset_ms
        if type(lo) is not int or type(hi) is not int:
            raise Rejected
        block = pcm[lo * 32 : hi * 32]
        _require(0 <= lo < hi <= (len(pcm) + 31) // 32 and bool(block))
        parts.append(block)
    _require(bool(parts) and sum(map(len, parts)) <= len(pcm) + 2 * RATE * 30)
    _write_wav(path, b"".join(parts))


class Providers(Protocol):
    async def speech(self) -> Any: ...
    async def speakers(self, segments: list[Any]) -> list[Any]: ...
    def vector(self, ref: Any) -> list[float]: ...
    async def ambient(self) -> list[Any]: ...


def _version(stage: str, profile: str) -> str:
    return f"{stage}@{PIN[:7]}+{profile}"


def _segments(result: Any, audio: Input, version: str) -> tuple[bool, list[SegmentJSON]]:
    coverage = result.coverage
    total, processed = coverage.total_duration_ms, coverage.processed_duration_ms
    _require(type(total) is int and total == audio.duration_ms)
    _require(type(processed) is int and 0 <= processed <= total)
    gaps = _max_list(coverage.gaps)
    if gaps or processed != total:
        return False, []
    _require(coverage.scene_eligible is True and coverage.processed_ratio == 1.0)
    rows: list[SegmentJSON] = []
    seen = set()
    for s in _max_list(result.segments):
        sid = _id(s.speech_segment_id)
        _require(sid not in seen and s.audio_chunk_id == s.diarization_scope_id == audio.chunk_id)
        seen.add(sid)
        lo, hi = audio.span(s)
        _require(type(s.overlap) is bool)
        rows.append(
            SegmentJSON(
                audio_chunk_id=audio.chunk_id,
                speech_segment_id=sid,
                started_at=lo,
                ended_at=hi,
                original_text=_text(s.text),
                language=None if s.language is None else _text(s.language, 64),
                local_speaker_id=_id(s.local_speaker_id),
                diarization_scope_id=audio.chunk_id,
                overlap=s.overlap,
                model_version=version,
            )
        )
    return True, rows


def _refs(value: Any, expected: set[str]) -> list[str]:
    refs = [_id(x) for x in _max_list(value)]
    _require(bool(refs) and len(set(refs)) == len(refs) and set(refs) <= expected)
    return refs


def _speakers(
    provider: Providers,
    rows: list[Any],
    segments: list[SegmentJSON],
    audio: Input,
    version: str,
) -> list[SpeakerJSON]:
    expected: dict[str, set[str]] = {}
    for segment in segments:
        expected.setdefault(segment["local_speaker_id"], set()).add(segment["speech_segment_id"])
    checked: list[SpeakerJSON] = []
    seen = set()
    for row in _max_list(rows):
        rid, label = _id(row.resolution_id), _id(row.local_speaker_id)
        _require(rid not in seen and row.diarization_scope_id == audio.chunk_id)
        seen.add(rid)
        allowed = expected.pop(label, None)
        if allowed is None:
            raise Rejected
        refs = _refs(row.evidence_segment_ids, allowed)
        _require(set(refs) == allowed)
        quality = row.sample_quality
        embedding = None
        if row.embedding_status == "generated":
            _require(row.embedding_ref is not None and row.embedding_reason is None)
            _require(type(quality) in (int, float) and 0 <= quality <= 1)
            p = row.embedding_provenance
            if p is None:
                raise Rejected
            _require(p.dimension == ECAPA_DIM and p.dtype == "float32")
            _require(p.model_version == row.model_version and p.normalization == "l2")
            _require(p.preprocessing == "ear-speaker-concat-pcm16-mono16k-v1")
            selected = _refs(p.speech_segment_ids, allowed)
            values = provider.vector(row.embedding_ref)
            _require(type(values) is list and len(values) == ECAPA_DIM)
            _require(all(type(x) is float and math.isfinite(x) for x in values))
            _require(math.isclose(math.hypot(*values), 1.0, abs_tol=1e-5))
            embedding = EmbeddingJSON(
                model_version=version,
                shape=[ECAPA_DIM],
                dtype="float32",
                values=values,
                provenance={
                    "preprocessing": p.preprocessing,
                    "normalization": "l2",
                    "speech_segment_ids": selected,
                },
            )
        else:
            _require(row.embedding_status == "not_generated")
            _require(row.embedding_reason in ("insufficient_speech", "quality_rejected"))
            _require(
                row.embedding_ref is None and row.embedding_provenance is None and quality is None
            )
        checked.append(
            SpeakerJSON(
                resolution_id=rid,
                diarization_scope_id=audio.chunk_id,
                local_speaker_id=label,
                evidence_segment_ids=refs,
                embedding=embedding,
                sample_quality=quality,
            )
        )
    _require(not expected)
    return checked


def _events(rows: list[Any], audio: Input, version: str) -> list[AmbientJSON]:
    checked: list[AmbientJSON] = []
    seen = set()
    for row in _max_list(rows):
        eid = _id(row.audio_event_id)
        _require(eid not in seen and row.audio_chunk_id == audio.chunk_id)
        _require(row.taxonomy == "audioset-527")
        seen.add(eid)
        lo, hi = audio.span(row)
        checked.append(
            AmbientJSON(
                audio_event_id=eid,
                started_at=lo,
                ended_at=hi,
                original_label=_text(row.label, 256),
                taxonomy="audioset-527",
                model_version=version,
            )
        )
    return checked


async def recognize(
    audio: Input, profile: str, make_providers: Callable[[], Providers]
) -> Complete | Partial | Failed:
    """Injected fake providers need only the audited result shape, not old Ear."""
    stage: Stage = "setup"
    try:
        _id(profile)
        if not audio.pcm.strip(b"\0"):
            return Complete(
                status="complete",
                asr=ASRJSON(status="complete", chunks=[], segments=[]),
                speakers=[],
                ambient=[],
            )
        provider = make_providers()
        stage = "asr"
        av = _version("moss", profile)
        result = await provider.speech()
        complete, segments = _segments(result, audio, av)
        if not complete:
            return Partial(
                stage="asr", status="partial", asr=ASRJSON(status="partial", chunks=[], segments=[])
            )
        stage = "speakers"
        speakers = _speakers(
            provider,
            await provider.speakers(result.segments) if segments else [],
            segments,
            audio,
            _version("ecapa", profile),
        )
        stage = "ambient"
        ambient = _events(await provider.ambient(), audio, _version("panns", profile))
        chunk = ChunkJSON(
            audio_chunk_id=audio.chunk_id,
            started_at=_iso(audio.start),
            ended_at=_iso(audio.end),
            model_version=av,
        )
        return Complete(
            status="complete",
            asr=ASRJSON(status="complete", chunks=[chunk], segments=segments),
            speakers=speakers,
            ambient=ambient,
        )
    except Exception:
        return Failed(status="failed", stage=stage)


@dataclass(frozen=True)
class Assets:
    source: Path
    moss: Path
    ecapa: Path
    panns_checkpoint: Path
    panns_labels: Path
    device: str


def _absolute(path: Path) -> Path:
    _require(path.is_absolute())
    return path.resolve(strict=True)


def _read_small(path: Path, max_size: int) -> bytes:
    _require(path.is_file())
    with path.open("rb") as file:
        data = file.read(max_size + 1)
    _require(0 < len(data) <= max_size)
    return data


def _verify_source(directory: Path) -> Path:
    root = _absolute(directory)
    for relative, expected in _SOURCE.items():
        path = (root / "ear" / relative).resolve(strict=True)
        _require(path == root / "ear" / relative)
        _require(hashlib.sha256(_read_small(path, 1024 * 1024)).hexdigest() == expected)
    return root


def _require_launch() -> None:
    _require(bool(sys.flags.isolated) and sys.dont_write_bytecode)
    _require(
        not any(
            m.split(".", 1)[0]
            in {
                "ear",
                "huggingface_hub",
                "transformers",
                "speechbrain",
                "panns_inference",
            }
            for m in sys.modules
        )
    )


def _offline(root: Path) -> None:
    # Preserve no ambient application settings/proxies or credentials. Launch with
    # env already empty: clearing here cannot undo Python/loader startup effects.
    os.environ.clear()
    os.environ.update(
        HOME=str(root),
        TMPDIR=str(root),
        XDG_CACHE_HOME=str(root / "cache"),
        HF_HOME=str(root / "hf"),
        TORCH_HOME=str(root / "torch"),
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
        DO_NOT_TRACK="1",
    )
    tempfile.tempdir = str(root)
    os.chdir(root)

    def guard(event: str, args: tuple[object, ...]) -> None:
        # asyncio's internal socketpair is harmless. This only blocks common
        # Python paths, not native extensions; the OS isolation is mandatory.
        if event.startswith(("socket.connect", "socket.get", "socket.send")) or event in {
            "socket.bind",
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.posix_spawn",
        }:
            raise PermissionError("offline worker")

    sys.addaudithook(guard)


class LegacyProviders:
    """Translate the two artifact operations the pinned providers actually need."""

    def __init__(self, audio: Input, assets: Assets, root: Path) -> None:
        _require_launch()
        source = _verify_source(assets.source)
        moss, ecapa = _absolute(assets.moss), _absolute(assets.ecapa)
        checkpoint, labels = _absolute(assets.panns_checkpoint), _absolute(assets.panns_labels)
        _require((moss / "config.json").is_file() and (ecapa / "hyperparams.yaml").is_file())
        _require(checkpoint.is_file() and checkpoint.stat().st_size > 0)
        label_bytes = _read_small(labels, 128 * 1024)
        lines = label_bytes.splitlines()
        _require(len(lines) == 528 and lines[0].startswith(b"index,mid,"))
        _require(all(line.startswith(f"{index},".encode()) for index, line in enumerate(lines[1:])))
        panns_data = root / "panns_data"
        panns_data.mkdir(mode=0o700)
        # PaNNs reads the labels at import from HOME; prevent its download fallback.
        (panns_data / "class_labels_indices.csv").write_bytes(label_bytes)

        sys.path.insert(0, str(source))
        old_audio = importlib.import_module("ear.models.audio")
        context = importlib.import_module("ear.models.processing")
        self._request = importlib.import_module("ear.models.speaker").SpeakerResolutionRequest
        moss_module = importlib.import_module("ear.providers.speech_moss")
        speaker_module = importlib.import_module("ear.providers.speaker_anonymous")
        encoder_module = importlib.import_module("ear.providers.speaker_ecapa_encoder")
        panns_module = importlib.import_module("ear.providers.environment_panns")
        self._np = importlib.import_module("numpy")
        self._artifact = old_audio.ArtifactRef
        self._audio_path = root / "input.wav"
        self._vectors: dict[str, list[float]] = {}
        _write_wav(self._audio_path, audio.pcm)
        self._source_ref = self._make_ref(
            "input", self._audio_path.read_bytes(), "audio/wav", "temporary"
        )
        self._context = context.ProcessingContext(
            pipeline_run_id="recognition",
            task_id="recognition",
            idempotency_key=audio.chunk_id,
            requested_at=audio.start,
        )
        self._chunk = old_audio.AudioChunk(
            audio_chunk_id=audio.chunk_id,
            capture_id=audio.chunk_id,
            audio_ref=self._source_ref,
            started_at=audio.start,
            ended_at=audio.start + timedelta(milliseconds=audio.duration_ms),
            format=old_audio.AudioFormat(
                codec="pcm_s16le", sample_rate_hz=RATE, channels=1, sample_width_bits=16
            ),
            source_refs=[old_audio.EvidenceRef(type="capture", id=audio.chunk_id)],
            model_version="pcm16-16k",
            created_at=audio.start,
        )

        class PcmSpeakers(speaker_module.AnonymousSpeakerResolutionModule):
            async def _cut_speaker_audio(
                self,
                chunk: Any,
                source: Path,
                local_speaker_id: str,
                selected: list[Any],
            ) -> Path:
                # Reuse selection/quality/embedding. Only replace ffmpeg cut/concat.
                directory = Path(tempfile.mkdtemp(prefix="speaker-", dir=root))
                path = directory / "speaker.wav"
                _speaker_wav(path, audio.pcm, selected)
                return path

        class NonemptyPanns(panns_module.PannsAudioEventModule):
            def _infer_unmanaged(self, source: Path) -> Any:
                framewise = super()._infer_unmanaged(source)
                if framewise is None:
                    raise Rejected
                _require(len(framewise.shape) == 2)
                _require(framewise.shape[0] > 0)
                return framewise

        options = dict(device=assets.device, inference_timeout=TIMEOUT, local_files_only=True)
        self._moss = moss_module.MossSpeechUnderstandingModule(
            self,
            model_id=str(moss),
            inference_window_ms=MAX_MS,
            **options,
        )
        encoder = encoder_module.EcapaSpeakerEncoder(savedir=ecapa, **options)
        self._speakers = PcmSpeakers(self, encoder, inference_timeout=TIMEOUT)
        self._panns = NonemptyPanns(
            self,
            checkpoint_path=str(checkpoint),
            inference_window_ms=MAX_MS,
            **options,
        )

    def _make_ref(self, key: str, data: bytes, media_type: str, retention: str) -> Any:
        return self._artifact(
            artifact_id=key,
            uri=f"worker:{key}",
            media_type=media_type,
            size_bytes=len(data),
            checksum="sha256:" + hashlib.sha256(data).hexdigest(),
            created_at=datetime.now(UTC),
            retention_class=retention,
        )

    def resolve_path(self, ref: Any) -> Path:
        _require(ref == self._source_ref)
        return self._audio_path

    async def put(self, source_path: str, *, media_type: str, retention_class: str) -> Any:
        path = Path(source_path)
        _require(path.parent == self._audio_path.parent and path.suffix == ".npy")
        _require(not path.is_symlink() and media_type == "application/octet-stream")
        _require(retention_class == "derived" and len(self._vectors) < MAX_ROWS)
        raw = _read_small(path, 4096)
        # Pin float32/192 and reject archives/pickle or oversized declared arrays.
        # Old provider writes an np.save vector of this exact dtype and shape.
        with io.BytesIO(raw) as handle:
            _require(self._np.lib.format.read_magic(handle) == (1, 0))
            shape, fortran, dtype = self._np.lib.format.read_array_header_1_0(handle)
            _require(shape == (ECAPA_DIM,) and not fortran and dtype.str == "<f4")
            _require(len(raw) - handle.tell() == ECAPA_DIM * 4)
            vector = self._np.frombuffer(raw, dtype=dtype, offset=handle.tell()).tolist()
        key = f"vector-{len(self._vectors)}"
        self._vectors[key] = vector
        return self._make_ref(key, raw, media_type, "derived")

    async def speech(self) -> Any:
        return await self._moss.process(self._chunk, self._context)

    async def speakers(self, segments: list[Any]) -> list[Any]:
        return await self._speakers.resolve(
            self._request(audio_chunk=self._chunk, segments=segments), self._context
        )

    def vector(self, ref: Any) -> list[float]:
        return self._vectors.pop(ref.artifact_id)

    async def ambient(self) -> list[Any]:
        return await self._panns.detect(self._chunk, self._context)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise Rejected


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(description=__doc__.split("\n", 1)[0] if __doc__ else None, allow_abbrev=False)
    parser.add_argument(
        "--offline-isolated",
        action="store_true",
        required=True,
        help="assert that the launcher has enforced offline isolation",
    )
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--ended-at", required=True)
    parser.add_argument("--model-profile", required=True)
    for asset in ("source", "moss", "ecapa", "panns-checkpoint", "panns-labels"):
        parser.add_argument(f"--{asset}", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    return parser.parse_args(argv)


def _encode(result: Complete | Partial | Failed) -> str:
    output = []
    size = 0
    for part in json.JSONEncoder(
        ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).iterencode(result):
        size += len(part)
        _require(size + 1 <= MAX_JSON)
        output.append(part)
    return "".join(output) + "\n"


def main(argv: list[str] | None = None) -> int:
    try:
        args = _args(argv)
        _id(args.model_profile)
        # SIGALRM's default action terminates even when a native inference thread
        # will not return to asyncio. The launcher must bound/clean the job too.
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(TIMEOUT)
        audio = parse_input(sys.stdin.buffer, args.started_at, args.ended_at)
    except (ValueError, OSError, OverflowError):
        print(_encode(Failed(status="failed", stage="input")), end="")
        return 2

    assets = Assets(
        args.source, args.moss, args.ecapa, args.panns_checkpoint, args.panns_labels, args.device
    )
    # This CLI is one-shot: never expose legacy library prints/logs, including
    # C-level output. Keep one private stdout descriptor for the protocol only.
    with os.fdopen(os.dup(1), "w", encoding="ascii") as stdout:
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        try:
            with tempfile.TemporaryDirectory(prefix="ear-local-recognition-") as temp:
                root = Path(temp).resolve()
                _offline(root)
                result = asyncio.run(
                    recognize(
                        audio, args.model_profile, lambda: LegacyProviders(audio, assets, root)
                    )
                )
        except Exception:
            result = Failed(status="failed", stage="setup")
        try:
            payload = _encode(result)
        except (ValueError, TypeError):
            result = Failed(status="failed", stage="output")
            payload = _encode(result)
        stdout.write(payload)
    signal.alarm(0)
    return {"complete": 0, "partial": 3, "failed": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
