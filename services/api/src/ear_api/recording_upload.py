"""HTTP adapter for the explicitly configured, single-owner recording spool."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import aclosing
from datetime import UTC, datetime
from tempfile import TemporaryFile
from typing import BinaryIO

from ear_adapter.recording_intake import (
    IntakeConflict,
    IntakeInvalid,
    IntakeStorageError,
    Receipt,
    RecordingIntake,
)
from ear_contracts import is_id
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from ear_api.config import Settings
from ear_api.upstream import GatewayError

_TYPES = {
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/mp4": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
}
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def upload_metadata(request: Request) -> tuple[str, str, int | None]:
    if request.scope["query_string"]:
        raise GatewayError(422, "invalid_input")
    times = request.headers.getlist("x-recording-started-at")
    if not times or times == [""]:
        raise GatewayError(422, "recording_start_required")
    if len(times) != 1 or _TIMESTAMP.fullmatch(times[0]) is None or times[0].endswith("-00:00"):
        raise GatewayError(422, "invalid_recording_start")
    try:
        start = datetime.fromisoformat(times[0]).astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        raise GatewayError(422, "invalid_recording_start") from None
    media = _TYPES.get(request.headers.get("content-type", "").lower())
    if media is None:
        raise GatewayError(415, "recording_media_required")
    length = request.headers.get("content-length")  # framing already checked in Boundary
    return start, media, int(length) if length is not None else None


async def stage_body(
    request: Request, target: BinaryIO, declared: int | None, settings: Settings
) -> None:
    """Stage bounded chunks on disk; do not publish a partially received request."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.recording_upload_timeout
    limit = settings.recording_upload_max_bytes
    size = 0
    try:
        async with aclosing(request.stream()) as chunks:
            while True:
                # Bound total receive time, including the time elapsed while staging.
                # Do not cancel an active file write and then close its temporary file.
                if loop.time() >= deadline:
                    raise TimeoutError
                try:
                    async with asyncio.timeout_at(deadline):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    break
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise GatewayError(413, "recording_too_large")
                if declared is not None and size > declared:
                    raise GatewayError(422, "invalid_input")
                if await run_in_threadpool(target.write, chunk) != len(chunk):
                    raise OSError("short recording staging write")
    except ClientDisconnect:
        raise GatewayError(400, "recording_upload_incomplete") from None
    except TimeoutError:
        raise GatewayError(408, "recording_upload_timeout") from None
    if declared is not None and size != declared:
        raise GatewayError(422, "invalid_input")
    if size == 0:
        raise GatewayError(422, "invalid_recording")


async def accept_recording(
    intake: RecordingIntake, request: Request, settings: Settings, verify_owner: Callable[[], None]
) -> Receipt:
    start, media, length = upload_metadata(request)
    if length is not None and length > settings.recording_upload_max_bytes:
        raise GatewayError(413, "recording_too_large")
    if length == 0:
        raise GatewayError(422, "invalid_recording")
    await run_in_threadpool(verify_owner)
    try:
        # Private, anonymous scratch on the API host, not a caller-named file.
        with TemporaryFile(mode="w+b", buffering=0) as staged:
            await stage_body(request, staged, length, settings)
            # Revocation during a slow upload must not publish the staged audio.
            await run_in_threadpool(verify_owner)
            return await run_in_threadpool(_accept, intake, staged, start, media)
    except IntakeConflict:
        raise GatewayError(409, "recording_conflict") from None
    except IntakeInvalid:
        raise GatewayError(422, "invalid_recording") from None
    except (IntakeStorageError, OSError):
        raise GatewayError(503, "recording_intake_unavailable") from None


def _accept(intake: RecordingIntake, staged: BinaryIO, start: str, media: str) -> Receipt:
    staged.seek(0)
    return intake.accept(staged, source="web", source_id=None, started_at=start, media_type=media)


def recording_status(intake: RecordingIntake, job_id: str) -> dict[str, object]:
    if not is_id(job_id):
        raise GatewayError(422, "invalid_input")
    try:
        job = intake.get(job_id)
    except IntakeStorageError:
        raise GatewayError(503, "recording_intake_unavailable") from None
    if job is None or job["state"] == "archived":  # phone Context isn't an audio job
        raise GatewayError(404, "not_found")
    return {key: job[key] for key in ("id", "state", "attempts", "error")}
