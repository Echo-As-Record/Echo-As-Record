"""Opt-in full-capture recognition through the existing one-shot offline launcher.

Use serially with ``ingest_native`` and a distinct adapter +profile. No successful
prefix is published on failure. Read ``last_report`` after an actual recognition
attempt, not as a storage receipt (e.g. dedup can skip this provider entirely).
Only the original capture's audio_ref is published by the native pipeline.

Use trusted private scratch with room for up to 128 MiB of source and 57 MiB of
PCM. Neither scratch fd nor a source pathname reaches the model launcher. Give
the one-shot launcher the same OS isolation/limits as for short recognition.
This runs one subprocess per window; non-silent windows may cold-start models.
Cutting at fixed boundaries loses acoustic context, and does not link speakers.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
import wave
import weakref
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import pairwise
from typing import BinaryIO, Literal

from ear_contracts import format_utc

from ear_adapter.convert import FileRef
from ear_adapter.ear_api import AudioChunk, SpeechSegment
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import AmbientEvent, ASRResult, SpeakerResult, _asr, _speakers
from ear_adapter.recognition_decode import FFmpegDecoder, RecognitionDecodeError, _duration
from ear_adapter.recognition_process import (
    RecognitionProcess,
    RecognitionProcessError,
    _PartialRecognition,
    _RecognitionStages,
    _Staged,
)

_RATE = 16_000
_WINDOW = _RATE * 30
# Bound decoded aggregate results before retention. These are not guarantees
# about storage limits (the wire representation and deployment config differ).
_MAX_REPLY_BYTES = 8 << 20
_MAX_RECORDS = 2_000  # Includes the single audio_ref and derived records.

Failure = Literal["normalization", "partial_chunk", "worker", "deadline", "result_limit", "results"]
ChunkStatus = Literal["unprocessed", "complete", "partial", "failed"]


@dataclass(frozen=True, slots=True)
class ChunkReport:
    chunk_id: str
    first_frame: int
    end_frame: int  # Exclusive; offsets into normalized source, never a padded WAV.
    started_at: datetime
    ended_at: datetime
    status: ChunkStatus = "unprocessed"
    # None means unknown, not zero speech. Missing ECAPA embeddings are degraded,
    # not a coverage gap; native keeps those speakers and their transcripts.
    segments: int | None = None
    missing_voiceprints: int | None = None


@dataclass(frozen=True, slots=True)
class LongRecognitionReport:
    source_media_type: str
    started_at: datetime
    ended_at: datetime
    source_codec: str | None
    normalized_frames: int | None
    chunks: tuple[ChunkReport, ...]
    failure: Failure | None

    @property
    def gaps(self) -> tuple[tuple[datetime, datetime], ...]:
        """Ranges without verified recognition, including tolerated clock rounding."""
        if not self.chunks:
            return ((self.started_at, self.ended_at),)
        gaps = tuple((c.started_at, c.ended_at) for c in self.chunks if c.status != "complete")
        end = self.chunks[-1].ended_at
        return (*gaps, (end, self.ended_at)) if end < self.ended_at else gaps

    @property
    def degraded(self) -> bool:
        return bool(self.failure or self.gaps or any(c.missing_voiceprints for c in self.chunks))


class LongRecognitionError(RecognitionProcessError):
    """Fixed message with a private coverage report, no worker output or file path."""

    def __init__(self, report: LongRecognitionReport) -> None:
        super().__init__(f"long recognition incomplete: {report.failure}")
        self.report = report


def _windows(frames: int, capture: CaptureInput) -> list[ChunkReport]:
    # Do not pad or discard a tail below the old worker's one-second minimum.
    # Borrow only the frames needed from the preceding window.
    cuts = [*range(0, frames, _WINDOW), frames]
    if len(cuts) > 2 and (tail := cuts[-1] - cuts[-2]) < _RATE:
        cuts[-2] -= _RATE - tail
    source = (
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
    key = hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()

    def at(frame: int) -> datetime:
        return capture.started_at + _duration(frame)

    return [
        ChunkReport(
            f"chunk_{key}_{lo}",
            lo,
            hi,
            at(lo),
            min(at(hi), capture.ended_at),
        )
        for lo, hi in pairwise(cuts)
    ]


def _window_wav(pcm: BinaryIO, frames: int) -> bytes:
    n = 2 * frames
    samples = pcm.read(n)
    if len(samples) != n:
        raise RecognitionDecodeError("audio normalization rejected")
    with io.BytesIO() as wav:
        with wave.open(wav, "wb") as writer:
            writer.setparams((1, 2, _RATE, frames, "NONE", "not compressed"))
            writer.writeframes(samples)
        return wav.getvalue()


def _scoped_id(scope: str, raw: str) -> str:
    return hashlib.sha256(f"{scope}:{raw}".encode("ascii")).hexdigest()


def _scoped(
    scope: str,
    asr: ASRResult,
    speakers: tuple[SpeakerResult, ...],
    ambient: tuple[AmbientEvent, ...],
) -> tuple[ASRResult, tuple[SpeakerResult, ...], tuple[AmbientEvent, ...]]:
    refs = {s.speech_segment_id: _scoped_id(scope, s.speech_segment_id) for s in asr.segments}
    # The raw model label is preserved as a label, not an identity. In native
    # ingest the unique local speaker identity is (diarization_scope_id, label).
    checked = ASRResult(
        "complete",
        tuple(c.model_copy(update={"audio_chunk_id": scope}) for c in asr.chunks),
        tuple(
            s.model_copy(
                update={
                    "speech_segment_id": refs[s.speech_segment_id],
                    "audio_chunk_id": scope,
                    "diarization_scope_id": scope,
                }
            )
            for s in asr.segments
        ),
    )
    resolutions = []
    for r in speakers:
        embedding = r.embedding
        if embedding is not None and embedding.provenance is not None:
            provenance = dict(embedding.provenance)
            provenance["speech_segment_ids"] = [refs[s] for s in provenance["speech_segment_ids"]]
            embedding = embedding.model_copy(update={"provenance": provenance})
        resolutions.append(
            replace(
                r,
                resolution_id=_scoped_id(scope, r.resolution_id),
                diarization_scope_id=scope,
                evidence_segment_ids=tuple(refs[s] for s in r.evidence_segment_ids),
                embedding=embedding,
            )
        )
    return (
        checked,
        tuple(resolutions),
        tuple(replace(e, audio_event_id=_scoped_id(scope, e.audio_event_id)) for e in ambient),
    )


@dataclass(slots=True)
class _Decoded:
    audio: BinaryIO
    media_type: str
    size: int
    pcm: BinaryIO
    frames: int
    codec: str
    deadline: float
    used: bool = False


class ChunkedRecognitionProvider(_RecognitionStages):
    """Up to 31 minutes, at most 62 sequential one-shot calls and one storage batch.

    ``timeout`` bounds normalization and model work together (seconds, default
    7200); each child is still limited by its own transport timeout. OS process
    creation, regular-file I/O, and job teardown need operator-side hard limits.
    ``scratch_dir`` optionally selects a trusted absolute private disk directory;
    otherwise Python uses the system temporary directory for unnamed files.
    """

    def __init__(
        self,
        process: RecognitionProcess,
        decoder: FFmpegDecoder,
        *,
        timeout: float = 7200,
        scratch_dir: str | None = None,
    ) -> None:
        if type(timeout) not in (int, float) or not 0 < timeout <= 14_400:
            raise ValueError("invalid long recognition timeout")
        if scratch_dir is not None and (not os.path.isabs(scratch_dir) or "\0" in scratch_dir):
            raise ValueError("invalid long recognition scratch directory")
        super().__init__()
        self._process = process
        self._decoder = decoder
        self._timeout = timeout
        self._scratch_dir = scratch_dir
        self.last_report: LongRecognitionReport | None = None
        self._decoded: _Decoded | None = None

    @contextmanager
    def decoded_duration(
        self, audio: BinaryIO, *, media_type: str, size: int
    ) -> Iterator[timedelta]:
        """Decode once, then recognize against this media duration within the scope.

        Bind PCM to this borrowed stream and the derived duration. Do not stage it
        across retries, job exits, or failed capture construction.
        """
        if self._decoded is not None:
            raise RecognitionDecodeError("audio normalization stage mismatch")
        self._pending = None
        self.last_report = None
        deadline = time.monotonic() + self._timeout
        with tempfile.TemporaryFile(dir=self._scratch_dir) as pcm:
            frames, codec = self._decoder._long_pcm(
                audio,
                pcm,
                media_type=media_type,
                size=size,
                span=None,
                deadline=deadline,
                scratch_dir=self._scratch_dir,
            )
            self._decoded = _Decoded(audio, media_type, size, pcm, frames, codec, deadline)
            try:
                yield _duration(frames)
            finally:
                self._decoded = None
                self._pending = None

    def asr(self, audio: BinaryIO, capture: CaptureInput) -> ASRResult:
        self._pending = None
        self.last_report = None
        stream = weakref.ref(audio, self._discard)
        decoded = self._decoded
        deadline = time.monotonic() + self._timeout if decoded is None else decoded.deadline
        windows: list[ChunkReport] = []
        chunks: list[AudioChunk] = []
        segments: list[SpeechSegment] = []
        speakers: list[SpeakerResult] = []
        ambient: list[AmbientEvent] = []
        frames = codec = None
        failure: Failure | None = "normalization"
        nbytes, nrecords = 0, 1
        try:
            span = capture.ended_at - capture.started_at
            with ExitStack() as stack:
                if decoded is None:
                    pcm = stack.enter_context(tempfile.TemporaryFile(dir=self._scratch_dir))
                    frames, codec = self._decoder._long_pcm(
                        audio,
                        pcm,
                        media_type=capture.media_type,
                        size=capture.file_ref.size,
                        span=span,
                        deadline=deadline,
                        scratch_dir=self._scratch_dir,
                    )
                else:
                    if (
                        decoded.used
                        or decoded.audio is not audio
                        or audio.closed
                        or audio.tell() != 0
                        or decoded.media_type != capture.media_type
                        or decoded.size != capture.file_ref.size
                        or _duration(decoded.frames) != span
                    ):
                        raise RecognitionDecodeError("audio normalization stage mismatch")
                    decoded.used = True
                    pcm, frames, codec = decoded.pcm, decoded.frames, decoded.codec
                windows = _windows(frames, capture)
                pcm.seek(0)
                failure = None
                for i, window in enumerate(windows):
                    if time.monotonic() >= deadline:
                        failure = "deadline"
                        break
                    part = _window_wav(pcm, window.end_frame - window.first_frame)
                    descriptor = replace(
                        capture,
                        started_at=window.started_at,
                        ended_at=window.ended_at,
                        media_type="audio/wav",
                        file_ref=FileRef("chunk.wav", hashlib.sha256(part).hexdigest(), len(part)),
                    )
                    try:
                        with io.BytesIO(part) as wav:
                            a, s, e, size = self._process._recognize(
                                wav, descriptor, deadline=deadline
                            )
                    except _PartialRecognition:
                        windows[i] = replace(window, status="partial")
                        failure = "partial_chunk"
                        break
                    except RecognitionProcessError:
                        windows[i] = replace(window, status="failed")
                        failure = "deadline" if time.monotonic() >= deadline else "worker"
                        break
                    missing = sum(r.embedding is None for r in s)
                    windows[i] = replace(
                        window,
                        status="complete",
                        segments=len(a.segments),
                        missing_voiceprints=missing,
                    )
                    nbytes += size
                    nrecords += len(a.segments) + 2 * len(s) - missing + len(e)
                    if nbytes > _MAX_REPLY_BYTES or nrecords > _MAX_RECORDS:
                        failure = "result_limit"
                        break
                    a, s, e = _scoped(window.chunk_id, a, s, e)
                    chunks.extend(a.chunks)
                    segments.extend(a.segments)
                    speakers.extend(s)
                    ambient.extend(e)
        except (RecognitionDecodeError, OSError):
            failure = "deadline" if time.monotonic() >= deadline else "normalization"

        if failure is None:
            try:
                result, _ = _asr(ASRResult("complete", tuple(chunks), tuple(segments)), capture)
                checked = _speakers(tuple(speakers), result)  # Includes cross-window vector space.
            except ValueError:
                failure = "results"
            else:
                if time.monotonic() < deadline:
                    self._pending = _Staged(
                        stream,
                        capture,
                        result if result.segments else None,
                        checked,
                        tuple(ambient),
                    )
                    self.last_report = self._report(capture, frames, codec, windows, None)
                    return result
                failure = "deadline"
        self.last_report = self._report(capture, frames, codec, windows, failure)
        raise LongRecognitionError(self.last_report)

    @staticmethod
    def _report(
        capture: CaptureInput,
        frames: int | None,
        codec: str | None,
        windows: list[ChunkReport],
        failure: Failure | None,
    ) -> LongRecognitionReport:
        return LongRecognitionReport(
            capture.media_type,
            capture.started_at,
            capture.ended_at,
            codec,
            frames,
            tuple(windows),
            failure,
        )
