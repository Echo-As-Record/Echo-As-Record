"""Validation and transactional plans for Markdown artifacts."""

from __future__ import annotations

from ear_contracts import SemanticRecord, VersionRef
from ear_storage_client import Create, DependencyInput, RegisterDependencies, WriteOp

from ear_extraction.domain import Activity, Generated, Rule
from ear_extraction.persistence import ref
from ear_extraction.segmentation import Inputs
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
    plain_markdown(result.body_md)
    if result.action is not None:
        if result.action.kind != rule.action:
            raise ValueError("action_not_allowed_by_rule")
        plain_markdown(result.action.title + result.action.notes)
    elif rule.action and not result.action_reason.strip():
        raise ValueError("missing_action_requires_reason")


def plain_markdown(text: str) -> None:
    # New output is plain Markdown. Old persisted text/unchanged results are not
    # revalidated or rewritten, and identities are never inferred from the text.
    if "[[person:" in text or "[[place:" in text:
        raise ValueError("legacy_entity_marker_in_new_output")


def dependencies(
    activity: SemanticRecord | VersionRef, rule: SemanticRecord | VersionRef, data: Inputs
) -> list[VersionRef | DependencyInput]:
    activity_ref = ref(activity) if isinstance(activity, SemanticRecord) else activity
    rule_ref = ref(rule) if isinstance(rule, SemanticRecord) else rule
    return [activity_ref, rule_ref, *input_dependencies(data)]


def input_dependencies(data: Inputs) -> list[VersionRef | DependencyInput]:
    # ALL interval attributions, including those not cited in the generated text.
    return [
        *[ref(a) for a in data.attributions],
        *[DependencyInput(ref(e), ("name",)) for e in data.entities],
    ]


def create_artifact(
    id: str,
    activity_id: str,
    activity: Activity,
    rule: SemanticRecord,
    result: Generated,
    data: Inputs,
    run_id: str,
    *,
    activity_version: int = 1,
) -> list[WriteOp]:
    payload = {
        "activity_id": activity_id,
        "rule_id": rule.id,
        "rule_version": rule.version,
        "body_md": result.body_md,
        "evidence_ids": result.evidence_ids,
        "action_reason": result.action_reason,
        "run_id": run_id,
        "manual": False,
        "source": "automatic",
    }
    deps = dependencies(
        VersionRef(data_type="activity", id=activity_id, version=activity_version), rule, data
    )
    return [
        Create("artifact", id, payload, event_start=activity.start, event_end=activity.end),
        RegisterDependencies("artifact", id, deps),
        *create_suggestion(id, 1, result, activity),
    ]
