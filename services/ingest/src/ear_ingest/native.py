"""Map live phone records to the shared durable intake, not to the legacy inbox."""

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from ear_ingest.record import component

if TYPE_CHECKING:
    from ear_adapter.recording_intake import RecordingIntake

SOURCE = "phone"
DEFAULT_MAX_BYTES = 128 << 20
MAX_BYTES = 512 << 20
KINDS = {
    "richVoice": "audio",
    "phoneContext": "context",
    "phoneIMU": "imu",
    "phoneNotes": "notes",
}
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")


def config() -> tuple[Path, int] | None:
    """Shared destination and byte limit, also used by the finalized event CLI."""
    value = os.environ.get("EAR_INGEST_NATIVE_ROOT", "")
    if not value:
        return None
    if any(os.environ.get(k) for k in ("EAR_INGEST_EVENT_URL", "EAR_INGEST_NOTIFY_URL")):
        raise RuntimeError("EAR_INGEST_NATIVE_ROOT is incompatible with legacy notification URLs")
    root = Path(value).expanduser()
    if not root.is_absolute() or not value.strip():
        raise RuntimeError("EAR_INGEST_NATIVE_ROOT must be an absolute path")
    return root, max_bytes()


@dataclass(frozen=True, slots=True)
class PhoneConfig:
    root: Path
    limit: int
    device: str
    after: datetime


def phone_config() -> PhoneConfig | None:
    """Only the HTTP listeners need the operator-verified session-clock source."""
    options = config()
    if options is None:
        return None
    root, limit = options
    try:
        device = component(os.environ.get("EAR_INGEST_NATIVE_PHONE_DEVICE_ID", "")).lower()
    except ValueError:
        raise RuntimeError(
            "EAR_INGEST_NATIVE_PHONE_DEVICE_ID must be one valid device ID"
        ) from None
    try:
        after = utc_datetime(os.environ.get("EAR_INGEST_NATIVE_PHONE_AFTER", ""))
    except (ValueError, OverflowError):
        raise RuntimeError(
            "EAR_INGEST_NATIVE_PHONE_AFTER must be a timestamp with a known offset"
        ) from None
    return PhoneConfig(root, limit, device, after)


def max_bytes() -> int:
    raw_limit = os.environ.get("EAR_INGEST_NATIVE_MAX_BYTES", str(DEFAULT_MAX_BYTES))
    if not re.fullmatch(r"[0-9]{1,9}", raw_limit) or not 0 < int(raw_limit) <= MAX_BYTES:
        raise RuntimeError(f"EAR_INGEST_NATIVE_MAX_BYTES must be in 1..{MAX_BYTES}")
    return int(raw_limit)


def utc_datetime(value: str) -> datetime:
    if not _TIMESTAMP.fullmatch(value) or value.endswith("-00:00"):
        raise ValueError("Expected an ISO 8601 timestamp with a known offset")
    return datetime.fromisoformat(value).astimezone(UTC)


def phone_times(payload: dict[str, Any]) -> tuple[datetime, datetime | None]:
    anchor = utc_datetime(payload["timestamp"])
    start = int(payload["segment_start_ms"])
    end = int(payload["segment_end_ms"])
    # Session-wide notes have no end; the phone protocol sends two zero offsets.
    return anchor + timedelta(milliseconds=start), (
        anchor + timedelta(milliseconds=end) if end > start else None
    )


def accept(
    intake: "RecordingIntake",
    stream: BinaryIO,
    *,
    root: Path,
    device: str,
    group: str,
    index: int,
    record_type: str,
    start: datetime,
    end: datetime | None,
    media: str,
) -> dict:
    from ear_adapter.recording_intake import IntakeConflict, IntakeInvalid, phone_source_id

    media = media.strip().lower() or "application/octet-stream"
    if not _MEDIA_TYPE.fullmatch(media) or (
        record_type == "richVoice" and not media.startswith("audio/")
    ):
        raise ValueError("Invalid media type for phone record")
    # The components cannot contain the separator. The fixed-width digest keeps the
    # shared source ID bounded and does not expose the raw device or group string.
    kind = KINDS[record_type]
    try:
        if kind in {"audio", "context"}:
            source_id = phone_source_id(device, group, index, kind)
        else:
            # The current intake rejects unsupported sidecar kinds; preserve the wire identity.
            identity = f"{device.lower()}:{group}:{index}:{kind}"
            source_id = "phone-v1:" + hashlib.sha256(identity.encode("ascii")).hexdigest()
        receipt = intake.accept(
            stream,
            source=SOURCE,
            source_id=source_id,
            started_at=start.isoformat().replace("+00:00", "Z"),
            ended_at=end.isoformat().replace("+00:00", "Z") if end is not None else None,
            media_type=media,
            kind=kind,
        )
    except IntakeConflict:
        raise NativeConflict("Phone record identity conflicts with an accepted upload") from None
    except IntakeInvalid:
        raise ValueError("Invalid native recording") from None
    # Preserve the response shape and an absolute diagnostic path. Native intake
    # has no per-record Brain JSON; the path names the actual shared spool instead.
    return {"ok": True, "record_id": receipt.id, "path": str(root)}


class NativeConflict(ValueError):
    pass
