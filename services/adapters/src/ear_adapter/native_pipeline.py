"""Raw-file recognition core. No Evidence API, model loading, CLI or discovery."""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Literal, Protocol

from ear_contracts import NewRawRecord, RawRecord, format_utc
from ear_storage_client import InvalidRequest, StorageClient
from ear_storage_client.remote_errors import ServiceUnavailable

from ear_adapter.config import ADAPTER_VERSION, AdapterConfig
from ear_adapter.convert import AmbientEvent, SpeakerResult, audio_records
from ear_adapter.ear_api import AudioChunk, Embedding, SpeechSegment
from ear_adapter.ids import deterministic_id
from ear_adapter.native_channels import CaptureInput, ChannelInputError, verified_audio
from ear_adapter.runtime import AlreadyIngested, Failed, Outcome, Pending, append_capture_batch

__all__ = [
    "NATIVE_ADAPTER_VERSION",
    "ASRResult",
    "AmbientEvent",
    "RecognitionProvider",
    "SpeakerResult",
    "ingest_native",
    "native_audio_ref_id",
    "prepare_native_records",
]

NATIVE_ADAPTER_VERSION = "ear-native/0.1.0"
_PROFILE = re.compile(
    re.escape(NATIVE_ADAPTER_VERSION) + r"(?:\+[A-Za-z0-9][A-Za-z0-9._-]{0,127})?"
)
_CHANNEL = re.compile(r"[a-z][a-z0-9_]*")
_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
# Cover the final check and publication for threads in this process. The generic
# StorageClient has no conditional AppendRaw; schedule one writer per identity
# across processes (per-row dedup alone cannot guard differing output batches).
_PUBLISH = Lock()


@dataclass(frozen=True, slots=True)
class ASRResult:
    """Absolute aware times. Complete with zero segments means verified no speech.

    A provider must report partial decoding as partial, not as a complete result
    with fewer/no segments. Each diarization scope is confined to one chunk.
    """

    status: Literal["complete", "partial"]
    chunks: tuple[AudioChunk, ...] = ()
    segments: tuple[SpeechSegment, ...] = ()


class RecognitionProvider(Protocol):
    """Trusted synchronous provider; each call gets the same borrowed stream at byte zero.

    Do not close/retain the stream, reopen an input path, or mutate returned results.
    Raise on any failed/partial speaker or ambient stage. Ambient () is valid;
    speakers must return exactly one result per ASR (scope, label), even when a
    usable embedding could not be extracted (embedding=None). References in a
    speaker result are to the supplied ASR segments for that speaker only.
    IDs/labels used for grouping are 1–128 ASCII letters, digits, dots, hyphens
    or underscores (not '.' or '..'); namespace them within a capture. Embeddings
    use one model/version and dimension per batch and finite, nonzero vectors.
    """

    def asr(self, audio: BinaryIO, capture: CaptureInput) -> ASRResult: ...

    def speakers(
        self, audio: BinaryIO, capture: CaptureInput, asr: ASRResult
    ) -> tuple[SpeakerResult, ...]: ...

    def ambient(self, audio: BinaryIO, capture: CaptureInput) -> tuple[AmbientEvent, ...]: ...


class _InvalidResult(ValueError):
    """The stage failed or returned a result outside its contract."""


def _config(config: AdapterConfig) -> AdapterConfig:
    version = config.adapter_version
    if version == ADAPTER_VERSION:
        version = NATIVE_ADAPTER_VERSION
    if not isinstance(version, str) or _PROFILE.fullmatch(version) is None:
        raise ValueError("invalid native adapter profile")
    if not isinstance(config.channel, str) or _CHANNEL.fullmatch(config.channel) is None:
        raise ValueError("invalid native channel")
    _text(config.user)
    return replace(config, adapter_version=version)


def _input_key(capture: CaptureInput, config: AdapterConfig) -> str:
    # Canonical tuple avoids ambiguous delimiter concatenation. Include every
    # provider-visible descriptor field and the stored relative reference, not
    # the private absolute root. Bind that root consistently for each channel.
    fields = (
        config.channel,
        config.user,
        capture.device_id,
        capture.capture_id,
        capture.record_id,
        capture.group_id,
        format_utc(capture.started_at),
        format_utc(capture.ended_at),
        capture.media_type,
        capture.file_ref.path,
        capture.file_ref.sha256,
        capture.file_ref.size,
    )
    encoded = json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode()
    return "ear-native:input-v1:" + hashlib.sha256(encoded).hexdigest()


def native_audio_ref_id(capture: CaptureInput, config: AdapterConfig) -> str:
    """The atomic batch marker's ID, without reading the file or calling a provider."""
    cfg = _config(config)
    return deterministic_id(
        capture.started_at, "audio_ref", cfg.adapter_version, _input_key(capture, cfg)
    )


def _id(value: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None or value in {".", ".."}:
        raise _InvalidResult


def _text(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise _InvalidResult
    value.encode("utf-8")  # Reject unpaired surrogates before storage encoding.


def _range(start: datetime, end: datetime, lower: datetime, upper: datetime) -> None:
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.utcoffset() is None
        or end.utcoffset() is None
        or not lower <= start.astimezone(UTC) < end.astimezone(UTC) <= upper
    ):
        raise _InvalidResult


def _asr(result: ASRResult, capture: CaptureInput) -> tuple[ASRResult, dict[str, AudioChunk]]:
    if (
        type(result) is not ASRResult
        or result.status != "complete"
        or type(result.chunks) is not tuple
        or type(result.segments) is not tuple
    ):
        raise _InvalidResult
    chunks: dict[str, AudioChunk] = {}
    for chunk in result.chunks:
        if type(chunk) is not AudioChunk:
            raise _InvalidResult
        _id(chunk.audio_chunk_id)
        _text(chunk.model_version)
        _range(chunk.started_at, chunk.ended_at, capture.started_at, capture.ended_at)
        if chunk.audio_chunk_id in chunks:
            raise _InvalidResult
        chunks[chunk.audio_chunk_id] = chunk.model_copy(
            update={
                "started_at": chunk.started_at.astimezone(UTC),
                "ended_at": chunk.ended_at.astimezone(UTC),
            }
        )
    segments: set[str] = set()
    checked = []
    scope_to_chunk: dict[str, str] = {}
    for segment in result.segments:
        if type(segment) is not SpeechSegment:
            raise _InvalidResult
        _id(segment.speech_segment_id)
        _id(segment.audio_chunk_id)
        _id(segment.diarization_scope_id)
        _id(segment.local_speaker_id)
        _text(segment.original_text)
        _text(segment.model_version)
        if segment.language is not None:
            _text(segment.language)
        if type(segment.overlap) is not bool or segment.speech_segment_id in segments:
            raise _InvalidResult
        chunk = chunks.get(segment.audio_chunk_id)
        if (
            chunk is None
            or scope_to_chunk.setdefault(segment.diarization_scope_id, segment.audio_chunk_id)
            != segment.audio_chunk_id
        ):
            raise _InvalidResult
        _range(segment.started_at, segment.ended_at, chunk.started_at, chunk.ended_at)
        segments.add(segment.speech_segment_id)
        checked.append(
            segment.model_copy(
                update={
                    "started_at": segment.started_at.astimezone(UTC),
                    "ended_at": segment.ended_at.astimezone(UTC),
                }
            )
        )
    return ASRResult("complete", tuple(chunks.values()), tuple(checked)), chunks


def _number(value: object) -> bool:
    return (type(value) is float or type(value) is int) and math.isfinite(value)


def _embedding(embedding: Embedding) -> Embedding:
    if type(embedding) is not Embedding:
        raise _InvalidResult
    _text(embedding.model_version)
    values, shape = embedding.values, embedding.shape
    if (
        type(values) is not list
        or not values
        or type(shape) is not tuple
        or len(shape) != 1
        or type(shape[0]) is not int
        or shape[0] != len(values)
        or embedding.dtype not in ("float16", "float32", "float64")
        or not all(_number(v) for v in values)
        or not any(values)
    ):
        raise _InvalidResult
    provenance = embedding.provenance
    if provenance is not None:
        # Copy and require that metadata survives actual JSON encoding. No NaN,
        # NUL, private file handles or other non-JSON data may reach the SDK.
        if type(provenance) is not dict:
            raise _InvalidResult
        wire = json.dumps(provenance, allow_nan=False, ensure_ascii=False, sort_keys=True)
        wire.encode("utf-8")
        if r"\u0000" in wire:
            raise _InvalidResult
        provenance = json.loads(wire)
    return Embedding(
        model_version=embedding.model_version,
        shape=shape,
        dtype=embedding.dtype,
        values=list(values),
        provenance=provenance,
    )


def _speakers(results: tuple[SpeakerResult, ...], asr: ASRResult) -> tuple[SpeakerResult, ...]:
    if type(results) is not tuple:
        raise _InvalidResult
    by_speaker: dict[tuple[str, str], set[str]] = {}
    for s in asr.segments:
        by_speaker.setdefault((s.diarization_scope_id, s.local_speaker_id), set()).add(
            s.speech_segment_id
        )
    ids: set[str] = set()
    checked = []
    vector_space: tuple[str, tuple[int, ...]] | None = None
    for r in results:
        if type(r) is not SpeakerResult:
            raise _InvalidResult
        _id(r.resolution_id)
        _id(r.diarization_scope_id)
        _id(r.local_speaker_id)
        if r.resolution_id in ids or type(r.evidence_segment_ids) is not tuple:
            raise _InvalidResult
        ids.add(r.resolution_id)
        allowed = by_speaker.pop((r.diarization_scope_id, r.local_speaker_id), None)
        refs: set[str] = set()
        for ref in r.evidence_segment_ids:
            _id(ref)
            if allowed is None or ref not in allowed or ref in refs:
                raise _InvalidResult
            refs.add(ref)
        if allowed is None or (r.sample_quality is not None and not _number(r.sample_quality)):
            raise _InvalidResult
        if r.embedding is not None:
            if not refs:
                raise _InvalidResult
            embedding = _embedding(r.embedding)
            space = (embedding.model_version, embedding.shape)
            if vector_space is not None and space != vector_space:
                raise _InvalidResult
            vector_space = space
            r = replace(r, embedding=embedding)
        checked.append(r)
    if by_speaker:
        raise _InvalidResult
    return tuple(checked)


def _ambient(events: tuple[AmbientEvent, ...], capture: CaptureInput) -> tuple[AmbientEvent, ...]:
    if type(events) is not tuple:
        raise _InvalidResult
    ids: set[str] = set()
    checked = []
    for event in events:
        if type(event) is not AmbientEvent:
            raise _InvalidResult
        _id(event.audio_event_id)
        _range(event.started_at, event.ended_at, capture.started_at, capture.ended_at)
        _text(event.original_label)
        _text(event.taxonomy)
        _text(event.model_version)
        if event.audio_event_id in ids:
            raise _InvalidResult
        ids.add(event.audio_event_id)
        checked.append(
            replace(
                event,
                started_at=event.started_at.astimezone(UTC),
                ended_at=event.ended_at.astimezone(UTC),
            )
        )
    return tuple(checked)


class NativePreparationFailed(ValueError):
    """A native provider or conversion failed without exposing its inputs."""


def prepare_native_records(
    capture: CaptureInput,
    audio: BinaryIO,
    *,
    provider: RecognitionProvider,
    config: AdapterConfig,
) -> list[NewRawRecord]:
    """Build a native batch from a verified private stream; never touch storage.

    The caller owns the stream and must validate it against the capture first.
    """
    stage = "ASR"
    try:
        cfg = _config(config)
        key = _input_key(capture, cfg)
        audio.seek(0)
        asr = provider.asr(audio, capture)
        asr, chunks = _asr(asr, capture)
        stage = "speaker"
        speakers = ()
        if asr.segments:
            audio.seek(0)
            speakers = _speakers(provider.speakers(audio, capture, asr), asr)
        stage = "ambient"
        audio.seek(0)
        events = _ambient(provider.ambient(audio, capture), capture)
        stage = "conversion"
        records = audio_records(
            config=cfg,
            device_id=capture.device_id,
            ckey=key,
            started_at=capture.started_at,
            ended_at=capture.ended_at,
            audio_file=capture.file_ref,
            media_type=capture.media_type,
            upstream={
                "service": "ear-native",
                "capture_id": capture.capture_id,
                "record_id": capture.record_id,
                "group_id": capture.group_id,
            },
            chunks=chunks,
            segments=asr.segments,
            resolutions=speakers,
            events=events,
        )
        if audio.closed:
            raise _InvalidResult
        return records
    except Exception:
        # Providers can include paths, text or vendor responses in errors.
        raise NativePreparationFailed(f"native {stage} failed or incomplete") from None


def ingest_native(
    capture: CaptureInput,
    *,
    root: Path,
    provider: RecognitionProvider,
    storage: StorageClient,
    config: AdapterConfig,
) -> Outcome:
    """Verify one raw file, run recognition serially and append one atomic batch.

    Use SourceChannel.channel_id in config.channel. root is the file_ref base;
    the operator must keep that private layout stable for downstream consumers.
    The channel supplies a descriptor snapshot; this function does not reread its
    manifest. A pin and revalidation are not a filesystem transaction: trusted read-only
    mounts are required, and later path/file replacement cannot be prevented.
    Default config uses NATIVE_ADAPTER_VERSION; use a distinct +profile suffix
    when any model, settings or segmentation rules change intentionally.
    """
    try:
        cfg = _config(config)
    except ValueError:
        return Failed(capture.capture_id, "invalid native adapter configuration")
    key = _input_key(capture, cfg)
    marker_id = deterministic_id(capture.started_at, "audio_ref", cfg.adapter_version, key)

    def existing() -> Outcome | None:
        old = storage.get(marker_id)
        if old is None:
            return None
        if not isinstance(old, RawRecord) or (
            old.data_type,
            old.adapter_version,
            old.dedup_key,
        ) != ("audio_ref", cfg.adapter_version, key):
            return Failed(capture.capture_id, "native batch marker mismatch")
        return AlreadyIngested(capture.capture_id)

    def can_retry() -> bool:
        try:
            with verified_audio(root, capture):
                pass
        except ChannelInputError:
            return False
        return True

    try:
        initial = existing()  # Before any provider work; not a held storage snapshot.
        with ExitStack() as publication:
            with verified_audio(root, capture) as audio:
                if initial is not None:
                    return initial
                try:
                    records = prepare_native_records(capture, audio, provider=provider, config=cfg)
                except NativePreparationFailed as exc:
                    return Failed(capture.capture_id, str(exc))
                publication.enter_context(_PUBLISH)
                winner = existing()
                if winner is not None:
                    return winner
                # Normal context exit checks the same pin after the last storage read.
            return append_capture_batch(
                capture.capture_id,
                records,
                storage=storage,
                can_retry=can_retry,
                include_conflict_reason=False,
            )
    except ChannelInputError:
        return Failed(capture.capture_id, "native audio input verification failed")
    except ServiceUnavailable:
        return Pending(capture.capture_id, "① 暂时不可用")
    except InvalidRequest:
        return Failed(capture.capture_id, "① rejected native batch")
