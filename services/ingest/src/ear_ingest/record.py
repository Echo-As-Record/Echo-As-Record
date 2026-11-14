"""Validate the phone subset of the Brain multipart contract."""

import json
import re
from datetime import datetime, timedelta
from typing import Any

from starlette.datastructures import FormData, UploadFile

TYPES = {"richVoice", "phoneContext", "phoneIMU", "phoneNotes"}


def component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) or value in {".", ".."}:
        raise ValueError("Invalid path component")
    return value


def filename(value: str) -> str:
    # Reject paths instead of silently discarding directory components as Brain does.
    if "/" in value or "\\" in value or "\x00" in value or value in {".", ".."}:
        raise ValueError("Invalid upload filename")
    name = re.sub(r'[/:*?"<>|\r\n\t]+', "_", value.strip())
    name = re.sub(r"\s+", " ", name).strip().strip(".") or "file"
    if len(name.encode()) > 150:
        raise ValueError("Upload filename too long")
    return name


def parse(form: FormData) -> tuple[dict[str, Any], UploadFile]:
    payload: dict[str, Any] = {}
    upload = None
    allowed = {
        "name",
        "text",
        "type",
        "source",
        "context",
        "group_id",
        "segment_meta",
        "segment_start_ms",
        "segment_end_ms",
        "timestamp",
        "file",
    }
    seen: set[str] = set()
    for key, value in form.multi_items():
        if key not in allowed or key in seen:
            raise ValueError("Unknown or repeated field")
        seen.add(key)
        if key == "file" and isinstance(value, UploadFile):
            upload = value
        elif isinstance(value, str) and key != "file":
            payload[key] = value
        else:
            raise ValueError("Invalid multipart field")
    required = allowed - {"context", "file"}
    if not required <= payload.keys() or upload is None:
        raise ValueError("Missing required phone fields or file")
    if payload["type"] not in TYPES or not payload["source"]:
        raise ValueError("Invalid record type or source")
    component(payload["group_id"])
    filename(upload.filename or "file")
    for key in ("segment_meta", "context", "text"):
        raw = payload.get(key, "")
        if raw.strip().startswith(("{", "[")):
            try:
                payload[key] = json.loads(raw)
            except ValueError:
                if key == "segment_meta":
                    raise ValueError("Invalid segment_meta JSON") from None
    meta = payload["segment_meta"]
    if not isinstance(meta, dict):
        raise ValueError("segment_meta must be an object")
    index = meta.get("segmentIndex")
    if type(index) is not int or index < 0 or index > 2**31 - 1:
        raise ValueError("Invalid segmentIndex")
    for key, alias in (("segment_start_ms", "segmentStartMs"), ("segment_end_ms", "segmentEndMs")):
        raw = payload[key]
        if not re.fullmatch(r"[0-9]{1,15}", raw):
            raise ValueError("Invalid segment offset")
        if alias in meta and (type(meta[alias]) is not int or meta[alias] != int(raw)):
            raise ValueError("Segment offsets disagree")
    start, end = int(payload["segment_start_ms"]), int(payload["segment_end_ms"])
    # Octopus sends session-wide marks.json as phoneNotes with index/start/end all zero.
    session_notes = payload["type"] == "phoneNotes" and index == start == end == 0
    if end <= start and not session_notes:
        raise ValueError("Segment end must follow start")
    timestamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    payload["timestamp"] = timestamp.isoformat()
    # Validate overflow before touching the inbox.
    layout(payload)
    return payload, upload


def layout(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    timestamp = datetime.fromisoformat(payload["timestamp"]).astimezone()
    start = (timestamp + timedelta(milliseconds=int(payload["segment_start_ms"]))).astimezone()
    end = (timestamp + timedelta(milliseconds=int(payload["segment_end_ms"]))).astimezone()
    index = payload["segment_meta"]["segmentIndex"]
    interval = f"from_{start:%H-%M-%S}_to_{end:%H-%M-%S}"
    stem = f"segment_{index}_{payload['type']}_{interval}"
    attachment = f"segment_{index}_{interval}"
    return f"{timestamp:%Y-%m-%d}", f"{timestamp:%H-%M-%S}__{payload['group_id']}", stem, attachment
