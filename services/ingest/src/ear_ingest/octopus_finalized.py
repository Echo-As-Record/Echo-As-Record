"""Local stdin-only boundary for a trusted Octopus finalization producer."""

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

from ear_ingest import native
from ear_ingest.record import component

MANIFEST_BYTES = 8192
CHUNK = 128 << 10
MAX_EVENT_AGE = timedelta(hours=24)
_FIELDS = {
    "event",
    "device",
    "group_id",
    "segment_index",
    "type",
    "media_type",
    "started_at",
    "ended_at",
    "finalized_at",
    "size",
    "sha256",
}


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class FinalizedScope:
    device: str
    cutoff: datetime

    @classmethod
    def configured(cls) -> "FinalizedScope":
        device = os.environ.get("EAR_INGEST_OCTOPUS_DEVICE_ID", "")
        cutoff = os.environ.get("EAR_INGEST_OCTOPUS_FINALIZED_AFTER", "")
        if not device or not cutoff:
            raise ValueError("Octopus finalized input is not enabled")
        return cls(component(device).lower(), native.utc_datetime(cutoff))


@dataclass(frozen=True, slots=True)
class FinalizedEvent:
    device: str
    group: str
    index: int
    kind: str
    media: str
    start: datetime
    end: datetime | None
    finalized: datetime
    size: int
    sha256: str

    def in_window(self, scope: FinalizedScope, now: datetime, lookback: timedelta) -> bool:
        return (
            scope.cutoff <= (self.end or self.start) <= self.finalized <= now
            and now - self.finalized <= lookback
        )


def read_event(stream: BinaryIO, scope: FinalizedScope, limit: int) -> FinalizedEvent:
    line = stream.readline(MANIFEST_BYTES + 1)
    if len(line) > MANIFEST_BYTES or not line.endswith(b"\n"):
        raise ValueError("Expected one bounded finalized event manifest line")
    event = json.loads(line.decode("utf-8"), object_pairs_hook=_unique)
    if (
        not isinstance(event, dict)
        or event.keys() != _FIELDS
        or event["event"] != "record.finalized.v1"
    ):
        raise ValueError("Invalid finalized event manifest")
    device, group, kind, media, started, ended, finalized = (
        event[k]
        for k in (
            "device",
            "group_id",
            "type",
            "media_type",
            "started_at",
            "ended_at",
            "finalized_at",
        )
    )
    if not all(
        isinstance(field, str) for field in (device, group, kind, media, started, finalized)
    ):
        raise ValueError("Invalid finalized event fields")
    device, group = component(device).lower(), component(group)
    if device != scope.device or kind not in native.KINDS:
        raise ValueError("Record outside configured Octopus scope")
    index, size, expected_hash = event["segment_index"], event["size"], event["sha256"]
    if (
        type(index) is not int
        or not 0 <= index <= 2**31 - 1
        or type(size) is not int
        or not 0 < size <= limit
        or not isinstance(expected_hash, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
    ):
        raise ValueError("Invalid finalized identity or byte count")
    start, final_time = native.utc_datetime(started), native.utc_datetime(finalized)
    if ended is None and kind == "phoneNotes" and index == 0:
        end = None
    elif isinstance(ended, str):
        end = native.utc_datetime(ended)
        if end <= start:
            raise ValueError("Invalid finalized recording interval")
    else:
        raise ValueError("Expected finalized recording end time")
    if (end or start) > final_time:
        raise ValueError("Recording ends after finalization")
    return FinalizedEvent(
        device, group, index, kind, media, start, end, final_time, size, expected_hash
    )


def copy_payload(stream: BinaryIO, checked: BinaryIO, event: FinalizedEvent) -> None:
    digest = hashlib.sha256()
    remaining = event.size
    while remaining:
        chunk = stream.read(min(remaining, CHUNK))
        if not chunk:
            raise ValueError("Truncated finalized payload")
        digest.update(chunk)
        checked.write(chunk)
        remaining -= len(chunk)
    if stream.read(1) or digest.hexdigest() != event.sha256:
        raise ValueError("Finalized payload does not match manifest")


def publish(stream: BinaryIO) -> dict:
    options = native.config()
    if options is None:
        raise ValueError("Octopus finalized input is not enabled")
    root, limit = options
    scope = FinalizedScope.configured()
    now = datetime.now(UTC)
    event = read_event(stream, scope, limit)
    if not event.in_window(scope, now, MAX_EVENT_AGE):
        raise ValueError("Record outside configured finalization window")

    from ear_adapter.recording_intake import RecordingIntake

    # Verify the complete immutable payload before making any durable acceptance.
    with tempfile.TemporaryFile() as checked:
        copy_payload(stream, checked, event)
        checked.seek(0)
        return native.accept(
            RecordingIntake(root),
            checked,
            root=root,
            device=event.device,
            group=event.group,
            index=event.index,
            record_type=event.kind,
            start=event.start,
            end=event.end,
            media=event.media,
        )


def main() -> int:
    try:
        result = publish(sys.stdin.buffer)
    except (ValueError, OverflowError, RecursionError):
        print("Invalid, conflicting, or out-of-scope Octopus finalized record", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, sqlite3.Error):
        print("Octopus finalized record was not confirmed by native intake", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0
