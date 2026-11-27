"""Local stdin-only boundary for a trusted Octopus finalization producer."""

import hashlib
import json
import os
import re
import sys
import tempfile
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


def publish(stream: BinaryIO) -> dict:
    options = native.config()
    device_scope = os.environ.get("EAR_INGEST_OCTOPUS_DEVICE_ID", "")
    enabled_after = os.environ.get("EAR_INGEST_OCTOPUS_FINALIZED_AFTER", "")
    if options is None or not device_scope or not enabled_after:
        raise ValueError("Octopus finalized input is not enabled")
    root, limit = options
    allowed_device = component(device_scope).lower()
    cutoff = native.utc_datetime(enabled_after)
    now = datetime.now(UTC)

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
    if device != allowed_device or kind not in native.KINDS:
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
    if not cutoff <= (end or start) <= final_time <= now or now - final_time > MAX_EVENT_AGE:
        raise ValueError("Record outside configured finalization window")

    from ear_adapter.recording_intake import RecordingIntake

    # Verify the complete immutable payload before making any durable acceptance.
    with tempfile.TemporaryFile() as checked:
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = stream.read(min(remaining, CHUNK))
            if not chunk:
                raise ValueError("Truncated finalized payload")
            digest.update(chunk)
            checked.write(chunk)
            remaining -= len(chunk)
        if stream.read(1) or digest.hexdigest() != expected_hash:
            raise ValueError("Finalized payload does not match manifest")
        checked.seek(0)
        return native.accept(
            RecordingIntake(root),
            checked,
            root=root,
            device=device,
            group=group,
            index=index,
            record_type=kind,
            start=start,
            end=end,
            media=media,
        )


def main() -> int:
    try:
        result = publish(sys.stdin.buffer)
    except (ValueError, OverflowError, RecursionError):
        print("Invalid, conflicting, or out-of-scope Octopus finalized record", file=sys.stderr)
        return 2
    except (OSError, RuntimeError):
        print("Octopus finalized record was not confirmed by native intake", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0
