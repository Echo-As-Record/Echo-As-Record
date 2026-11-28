"""Bindings from the recording spool worker to the native adapter and storage SDK."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import BinaryIO

from ear_contracts import format_utc
from ear_storage_client import AppendRaw, Context, InvalidRequest, PermissionDenied, StorageClient
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError, ResourceLimit
from ear_storage_client.wire import decode_request, encode_request

from ear_adapter.config import AdapterConfig
from ear_adapter.convert import FileRef
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import RecognitionProvider, prepare_native_records
from ear_adapter.recording_intake import (
    DECODED_END_KEY,
    MEDIA_END_TOLERANCE,
    PublicationRejected,
    RecordingJob,
)


@dataclass(frozen=True, slots=True)
class NativeRecordingPreparation:
    """Pure recognition/conversion callback for RecordingWorker.run_one.

    Configure a fixed channel and device label for the shared intake, not per retry
    or uploader. The start comes from intake; the end always comes from decoded
    media. Supply scoped normalization to reuse the samples in recognition, or
    a trusted duration callback that validates the media. A supplied phone end
    is only an assertion against the decoded end.
    """

    provider: RecognitionProvider
    config: AdapterConfig
    duration: Callable[[RecordingJob, BinaryIO], timedelta] | None = None
    device_id: str = "recording_intake"
    normalized_duration: (
        Callable[[RecordingJob, BinaryIO], AbstractContextManager[timedelta]] | None
    ) = field(default=None, kw_only=True)

    def __call__(self, job: RecordingJob, audio: BinaryIO) -> dict[str, object]:
        start = datetime.fromisoformat(job.started_at)
        span: AbstractContextManager[timedelta]
        if self.normalized_duration is not None:
            span = self.normalized_duration(job, audio)
        elif self.duration is not None:
            span = nullcontext(self.duration(job, audio))
        else:
            raise ValueError("recording duration probe required")
        with span as duration:
            if not isinstance(duration, timedelta) or duration <= timedelta(0):
                raise ValueError("invalid recording duration")
            end = start + duration
            if job.ended_at is not None and (
                abs(datetime.fromisoformat(job.ended_at) - end) > MEDIA_END_TOLERANCE
            ):
                raise ValueError("recording end does not match decoded media")
            capture = CaptureInput(
                capture_id=job.id,
                group_id=job.id,
                record_id=job.id,
                device_id=self.device_id,
                started_at=start,
                ended_at=end,
                media_type=job.media_type,
                file_ref=FileRef(job.relative_path, job.sha256, job.size),
            )
            records = prepare_native_records(
                capture, audio, provider=self.provider, config=self.config
            )
        return {
            DECODED_END_KEY: format_utc(end),
            "native_batch": encode_request([AppendRaw(r) for r in records], Context.system()),
        }


@dataclass(frozen=True, slots=True)
class NativeRecordingPublication:
    """Submit the single saved batch once, after the spool worker's durable fence."""

    storage: StorageClient

    def __call__(self, job: RecordingJob, result: dict[str, object]) -> None:
        wire = result.get("native_batch")
        if not isinstance(wire, str):
            raise PublicationRejected("recording publication missing a prepared batch")
        try:
            ops, context = decode_request(wire)
        except InvalidRequest:
            raise PublicationRejected("recording publication has an invalid batch") from None
        if (
            not ops
            or not isinstance(ops[0], AppendRaw)
            or ops[0].record.data_type != "audio_ref"
            or context != Context.system()
            or not all(isinstance(op, AppendRaw) for op in ops)
        ):
            raise PublicationRejected("recording publication has an invalid batch")
        # Do not use ingest_native()/append_capture_batch() here. Those helpers may
        # retry a request in-process and return an ambiguous Pending. With no prior
        # attempt, a definite rejection is terminal; all unknowns must propagate.
        storage = self.storage
        try:
            if isinstance(storage, RemoteStorageClient):
                outcome = storage.write(ops, context, request_id=job.id)
            else:
                outcome = storage.write(ops, context)
        except (AuthenticationError, InvalidRequest, PermissionDenied, ResourceLimit):
            raise PublicationRejected("recording publication rejected") from None
        if not outcome.ok:
            raise PublicationRejected("recording publication rejected")
        if len(outcome.results) != len(ops) or any(
            not isinstance(op, AppendRaw)
            or ack.index != i
            or ack.outcome not in ("written", "deduplicated")
            or ack.id != op.record.id
            or ack.data_type != op.record.data_type
            for i, (ack, op) in enumerate(zip(outcome.results, ops, strict=True))
        ):
            raise RuntimeError("recording publication could not verify receipt")
