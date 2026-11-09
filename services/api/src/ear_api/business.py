"""Feedback admission, bounded historical preview and truthful result projection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ear_contracts import NewRawRecord, RawRecord, SemanticRecord, Source, SystemRecord
from ear_extraction.domain import Interval
from ear_storage_client import AppendRaw, Context, Query, TimeRange
from ear_storage_client.protocol import Reader
from ear_storage_client.remote import RemoteStorageClient

from ear_api.config import Binding
from ear_api.models import FeedbackInput
from ear_api.upstream import GatewayError

BUSINESS_TYPES = frozenset(
    {
        "activity",
        "artifact",
        "person",
        "place",
        "rule",
        "action_suggestion",
        "attribution",
        "voiceprint",
        "gps_point",
        "transcript_segment",
        "phone_info",
        "audio_ref",
        "ambient_sound",
        "local_speaker",
        "feedback",
    }
)


def feedback_identity(subject: str, request_id: str) -> tuple[str, datetime]:
    # Preserve UUIDv7 time; domain-separate principals. No clock, randomness, or
    # mutable server state in the envelope, so byte-identical replay survives restart.
    raw = UUID(request_id).int
    timestamp = raw >> 80
    digest = int.from_bytes(
        hashlib.sha256(("ear-api-feedback-v1:" + subject + ":" + request_id).encode()).digest()[:10]
    )
    value = timestamp << 80 | (digest & ((1 << 76) - 1)) | (7 << 76)
    value = (value & ~(3 << 62)) | (2 << 62)
    return str(UUID(int=value)), datetime.fromtimestamp(timestamp / 1000, tz=UTC)


def submit_feedback(
    client: RemoteStorageClient,
    binding: Binding,
    body: FeedbackInput,
) -> dict[str, Any]:
    command = body.normalized_command()
    fid, when = feedback_identity(binding.subject, body.request_id)
    payload: dict[str, Any] = {"module": body.module, "command": command}
    if body.module != "extraction":
        payload["integration"] = "pending_contract"
    raw = NewRawRecord(
        id=fid,
        data_type="feedback",
        event_start=when,
        event_end=when,
        source=Source(device="api_" + binding.subject, channel="api", user="default"),
        adapter_version="api-v1",
        dedup_key=binding.subject + ":" + body.request_id,
        payload=payload,
    )
    result = client.write([AppendRaw(raw)], Context.feedback(fid), request_id=fid).check()
    if len(result.results) != 1 or result[0].id != fid:
        raise GatewayError(503, "acceptance_unknown_replay_identical_request")
    return {
        "status": "accepted",
        "feedback_id": fid,
        "root_feedback_id": fid,
        "business_status": "pending",
        "integration": "existing_command" if body.module == "extraction" else "pending_contract",
    }


def feedback_status(client: RemoteStorageClient, fid: str) -> dict[str, Any]:
    record = client.get(fid)
    if not isinstance(record, RawRecord) or record.data_type != "feedback":
        raise GatewayError(404, "not_found")
    result: dict[str, Any] = {
        "feedback_id": fid,
        "status": "pending",
        "completion_source": None,
        "status_scope": "feedback_processing_not_external_execution",
        "integration": record.payload.get("integration", "existing_command"),
    }
    if record.payload.get("module") != "extraction":
        return result
    runs = client.query(Query("extraction_run", where={"root_feedback_id": fid}, limit=2))
    if len(runs.items) != 1 or runs.next_cursor is not None:
        return result
    run = runs.items[0]
    state = run.payload.get("status")
    if state in {
        "queued",
        "running",
        "retry_wait",
        "waiting_attribution",
        "succeeded",
        "blocked",
        "failed",
    }:
        result.update(status=state, completion_source="extraction_run", run_id=run.id)
    # Never call is_processed: admission/MarkProcessed is NOT business completion.
    return result


def preview_reextract(reader: Reader, interval: Interval) -> dict[str, Any]:
    remaining = 1000

    def scan(dtype: str, time: TimeRange | None = None) -> list[SemanticRecord]:
        nonlocal remaining
        records: list[SemanticRecord] = []
        cursor = None
        for _ in range(11):
            page = reader.query(Query(dtype, time=time, limit=100, cursor=cursor))
            remaining -= len(page.items)
            if remaining < 0:
                raise GatewayError(413, "preview_too_large")
            records.extend(r for r in page.items if isinstance(r, SemanticRecord))
            cursor = page.next_cursor
            if cursor is None:
                return records
        raise GatewayError(413, "preview_too_large")

    activities = scan("activity", TimeRange(interval.start, interval.end))
    for activity in activities:
        if (
            activity.event_start is None
            or activity.event_end is None
            or (activity.event_start < interval.start or activity.event_end > interval.end)
        ):
            raise GatewayError(409, "select_full_activity")
    ids = {a.id for a in activities}
    artifacts = [r for r in scan("artifact") if r.payload.get("activity_id") in ids]
    artifact_ids = {a.id for a in artifacts}
    suggestions = [
        r
        for r in scan("action_suggestion")
        if r.payload.get("artifact_id") in artifact_ids and r.payload.get("state") == "pending"
    ]
    # Identical to ④'s current preview algorithm, within a single bounded snapshot.
    modified = {
        r.payload["activity_id"] for r in artifacts if r.version > 1 or r.payload.get("manual")
    }
    return {
        "versions": {r.id: r.version for r in [*activities, *artifacts, *suggestions]},
        "modified_activities": len(modified),
    }


def management_result(record: SystemRecord) -> dict[str, Any]:
    allowed = {
        "feedback_id",
        "request_id",
        "module",
        "action",
        "status",
        "stage",
        "error_code",
        "enabled",
        "resources_ready",
        "ever_enabled",
        "revision",
        "need_recovery_decision",
        "read_types",
        "subscribe_types",
        "history_job_id",
        "history_execution_status",
        "recovery_feedback_id",
        "target_feedback_id",
        "credential_claim_required",
    }
    import re

    payload = {k: v for k, v in record.payload.items() if k in allowed}
    if payload.get("error_code") is not None and (
        not isinstance(payload["error_code"], str)
        or re.fullmatch(r"[a-zA-Z0-9_]{1,100}", payload["error_code"]) is None
    ):
        payload["error_code"] = "management_failed"
    payload["online"] = None
    return {
        "id": record.id,
        "data_type": record.data_type,
        "revision": record.revision,
        "updated_at": record.updated_at.isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
