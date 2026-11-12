"""Read-only projection of ③'s v1 receipts, never a consumer/liveness probe.

The runtime deliberately has no ear_attribution dependency or derived-ID copy.
Until a shared contract is published, validate the producer's existing shape at
this boundary. Only the trusted core single-writer type is authoritative; a
MarkProcessed, raw feedback, similarly named plugin type or body is not a result.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ear_contracts import RawRecord, SystemRecord, is_id
from ear_contracts.registry import UnknownDataTypeError
from ear_storage_client import InvalidRequest, Query
from ear_storage_client.remote import RemoteStorageClient

from ear_api.models import MAX_REVISION, PENDING_FIELDS
from ear_api.upstream import GatewayError

RESULT_TYPE = "attribution_feedback_result"
INTEGRATION = "attribution_result_query"
SUPPORTED = frozenset(
    {
        "correct_attribution",
        "confirm_attribution",
        "add_reference",
        "remove_reference",
        "rename",
        "create_place",
    }
)
# Do not echo an arbitrary "error_code": even an alphanumeric string may be a token.
PUBLIC_ERRORS = frozenset(
    {
        "unsupported_command",
        "invalid_command",
        "invalid_expected_version",
        "expected_version_conflict",
        "invalid_name",
        "unknown_place_cannot_be_renamed",
        "unknown_place_cannot_be_reference_target",
        "attribution_pending",
        "attribution_target_conflict",
        "previous_target_conflict",
        "invalid_history_choice",
        "reference_owner_conflict",
        "reference_type_mismatch",
        "invalid_subject",
        "target_unavailable",
        "invalid_references",
        "duplicate_attribution",
        "invalid_attribution",
        "subject_type_mismatch",
        "target_type_mismatch",
        "invalid_protection",
        "invalid_embedding",
        "invalid_coordinates",
        "invalid_attribution_state",
        "invalid_unknown_place",
        # Bounded create_place producer (including its payload/state helpers).
        "invalid_gps_ids",
        "duplicate_gps_ids",
        "protected_attribution_conflict",
        "legacy_protection_unknown",
        "invalid_candidate",
    }
)
ID_FIELDS = ("subject_id", "previous_target_id", "new_target_id", "attribution_id")
VERSION_FIELDS = ("before_version", "after_version")
ARRAY_FIELDS = ("subject_ids", "attribution_ids")
MAX_PLACE_SUBJECTS = 100


def invalid_result() -> GatewayError:
    return GatewayError(503, "attribution_result_invalid")


def valid_id_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_PLACE_SUBJECTS
        and all(is_id(item) for item in value)
        and len(set(value)) == len(value)
    )


def project_receipt(record: SystemRecord, feedback: RawRecord, kind: str) -> dict[str, Any]:
    p = record.payload
    if (
        record.data_type != RESULT_TYPE
        or record.writer != "attribution"
        or not is_id(record.id)
        or type(record.revision) is not int
        or not 1 <= record.revision <= MAX_REVISION
        or type(p.get("schema_version")) is not int
        or p["schema_version"] != 1
        or p.get("feedback_id") != feedback.id
        or p.get("command_kind") != kind
        or p.get("status") not in ("succeeded", "rejected")
        or "error_code" not in p
    ):
        raise invalid_result()
    completed = p.get("completed_at")
    if (
        not isinstance(completed, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", completed
        )
        is None
    ):
        raise invalid_result()
    try:
        datetime.fromisoformat(completed)
    except ValueError:
        raise invalid_result() from None

    metadata = {}
    for field in ID_FIELDS:
        if field in p:
            if not is_id(p[field]):
                raise invalid_result()
            metadata[field] = p[field]
    for field in VERSION_FIELDS:
        if field in p:
            if type(p[field]) is not int or not 1 <= p[field] <= MAX_REVISION:
                raise invalid_result()
            metadata[field] = p[field]

    for field in ARRAY_FIELDS:
        if field in p:
            if not valid_id_list(p[field]):
                raise invalid_result()
            metadata[field] = list(p[field])

    error = p["error_code"]
    if p["status"] == "succeeded":
        if kind not in SUPPORTED or error is not None:
            raise invalid_result()
        command = feedback.payload["command"]
        if kind == "create_place":
            # Read the historical normalized input as stored. Do not revalidate it
            # through the closed public-input model or change its replay fingerprint.
            name, gps_ids = command.get("name"), command.get("gps_ids")
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 256
                or not valid_id_list(gps_ids)
                or command.get("expected_version") is not None
                # The actual v1 producer has no scalar attribution/version metadata.
                or set(metadata) != {"new_target_id", *ARRAY_FIELDS}
                or p["subject_ids"] != gps_ids
                or len(p["attribution_ids"]) != len(gps_ids)
            ):
                raise invalid_result()
        elif kind == "rename":
            if set(metadata) != {"new_target_id"} or p["new_target_id"] != command.get("target_id"):
                raise invalid_result()
        else:
            if set(metadata) != set(ID_FIELDS + VERSION_FIELDS):
                raise invalid_result()
            target = command.get("new_target_id" if kind == "correct_attribution" else "target_id")
            previous = (
                command.get("previous_target_id") if kind == "correct_attribution" else target
            )
            if (
                p["subject_id"] != command.get("subject_id")
                or p["new_target_id"] != target
                # Removing a reference can legitimately leave the current target elsewhere.
                or (kind != "remove_reference" and p["previous_target_id"] != previous)
                or not p["before_version"] <= p["after_version"] <= p["before_version"] + 1
                or (
                    kind in {"add_reference", "remove_reference"}
                    and p["before_version"] != p["after_version"]
                )
            ):
                raise invalid_result()
    else:
        if not isinstance(error, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}", error) is None:
            raise invalid_result()
        error = error if error in PUBLIC_ERRORS else "attribution_rejected"
        # Rejections are not business changes. Never expose incidental/private metadata.
        metadata = {}
    return {
        "status": p["status"],
        "completion_source": RESULT_TYPE,
        "result_id": record.id,
        "result_revision": record.revision,
        "command_kind": kind,
        "completed_at": completed,
        "error_code": error,
        "metadata": metadata,
    }


def attribution_status(client: RemoteStorageClient, feedback: RawRecord) -> dict[str, Any]:
    command = feedback.payload.get("command")
    kind = command.get("kind") if isinstance(command, dict) else None
    if not isinstance(kind, str) or kind not in PENDING_FIELDS or kind == "reevaluate_related":
        raise invalid_result()
    # Query FIRST: registry is ACL-filtered. An absent entry may mean revoked read
    # access, not an absent consumer. Let actual PermissionDenied propagate as 403.
    # No registry/grant mutation, unbounded scan, private helper or marker fallback.
    try:
        page = client.query(Query(RESULT_TYPE, where={"feedback_id": feedback.id}, limit=2))
    except (UnknownDataTypeError, InvalidRequest):
        raise GatewayError(503, "attribution_result_unavailable") from None
    registry = client.registry
    spec = registry.data_types.get(RESULT_TYPE)
    if spec is None:
        raise GatewayError(503, "attribution_result_unavailable")
    if (
        spec.writer != "attribution"
        or spec.kind != "system_record"
        or RESULT_TYPE not in registry.core_types
    ):
        raise invalid_result()
    if len(page.items) > 1 or page.next_cursor is not None:
        raise invalid_result()
    if not page.items:
        return {"integration": INTEGRATION}
    record = page.items[0]
    if not isinstance(record, SystemRecord):
        raise invalid_result()
    return {"integration": INTEGRATION, **project_receipt(record, feedback, kind)}
