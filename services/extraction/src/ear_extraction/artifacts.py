"""Validation and transactional plans for Markdown artifacts."""

from __future__ import annotations

from ear_contracts import SemanticRecord, VersionRef
from ear_storage_client import Create, RegisterDependencies, WriteOp

from ear_extraction.domain import Activity, Generated, Rule
from ear_extraction.persistence import ref
from ear_extraction.segmentation import Inputs, check_refs
from ear_extraction.suggestions import create_suggestion


def validate_generated(result: Generated, rule: Rule, data: Inputs, *, rejudge: bool) -> None:
    if result.unchanged:
        if not rejudge or result.body_md or result.evidence_ids or result.action:
            raise ValueError("invalid_unchanged_result")
        return
    if not result.body_md.strip() or not result.evidence_ids:
        raise ValueError("artifact_requires_text_and_evidence")
    if not set(result.evidence_ids) <= {r.id for r in data.raw}:
        raise ValueError("unknown_evidence_reference")
    check_refs(result.body_md, data.entities)
    if result.action is not None:
        if result.action.kind != rule.action:
            raise ValueError("action_not_allowed_by_rule")
        check_refs(result.action.title + result.action.notes, data.entities)
    elif rule.action and not result.action_reason.strip():
        raise ValueError("missing_action_requires_reason")


def dependencies(
    activity: SemanticRecord | VersionRef, rule: SemanticRecord, data: Inputs
) -> list[VersionRef]:
    activity_ref = ref(activity) if isinstance(activity, SemanticRecord) else activity
    return [activity_ref, ref(rule), *[ref(a) for a in data.attributions]]


def create_artifact(
    id: str,
    activity_id: str,
    activity: Activity,
    rule: SemanticRecord,
    result: Generated,
    data: Inputs,
    run_id: str,
) -> list[WriteOp]:
    payload = {
        "activity_id": activity_id,
        "rule_id": rule.id,
        "rule_version": rule.version,
        "body_md": result.body_md,
        "evidence_ids": result.evidence_ids,
        "action_reason": result.action_reason,
        "run_id": run_id,
    }
    deps = dependencies(VersionRef(data_type="activity", id=activity_id, version=1), rule, data)
    return [
        Create("artifact", id, payload, event_start=activity.start, event_end=activity.end),
        RegisterDependencies("artifact", id, deps),
        *create_suggestion(id, 1, result, activity),
    ]
