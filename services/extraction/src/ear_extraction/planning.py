"""Model-driven plans: compute outside transactions, commit through ① afterwards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ear_contracts import SemanticRecord, VersionRef, new_id
from ear_storage_client import (
    AssertTypeRevision,
    Create,
    Reader,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
)

from ear_extraction.artifacts import dependencies, validate_generated
from ear_extraction.domain import (
    Blocked,
    Command,
    Generated,
    Interval,
    Rule,
)
from ear_extraction.long_input import generate, segment
from ear_extraction.persistence import range_of, records, semantic
from ear_extraction.reevaluation import ReevaluationPlan, terminate, unavailable
from ear_extraction.rules import eligible
from ear_extraction.segmentation import inputs, raw_inputs
from ear_extraction.selection import create_selection
from ear_extraction.suggestions import create_suggestion, pending_replacements

Ask = Callable[[str, dict[str, Any], type], Any]


def preview_reextract(reader: Reader, interval: Interval) -> dict:
    activities = records(reader, "activity", interval)
    for a in activities:
        r = range_of(a)
        if r.start < interval.start or r.end > interval.end:
            raise Blocked("partial_activity_overlap_select_full_activity")
    ids = {a.id for a in activities}
    artifacts = [r for r in records(reader, "artifact") if r.payload.get("activity_id") in ids]
    modified = {
        r.payload["activity_id"] for r in artifacts if r.version > 1 or r.payload.get("manual")
    }
    suggestions = [
        r
        for r in records(reader, "action_suggestion")
        if r.payload.get("artifact_id") in {a.id for a in artifacts}
        and r.payload.get("state") == "pending"
    ]
    return {
        "versions": {r.id: r.version for r in [*activities, *artifacts, *suggestions]},
        "modified_activities": len(modified),
    }


def extract_plan(
    reader: Reader, command: Command, run_id: str, ask: Ask
) -> tuple[list[WriteOp], dict]:
    assert command.interval is not None
    interval = command.interval
    result = segment(ask, interval, raw_inputs(reader, interval))
    ops: list[WriteOp] = []
    if command.kind == "reextract":
        preview = preview_reextract(reader, interval)
        if preview["versions"] != command.preview:
            raise Blocked("reextract_preview_changed")
        assert command.preview is not None
        for id, version in command.preview.items():
            r = reader.get(id)
            assert isinstance(r, SemanticRecord)
            ops.append(Supersede(r.data_type, id, version))
        # Preserve the public preview's original version-set meaning (⑥ also
        # computes it). Selections are internal derived state, not user edits;
        # retire their current snapshot versions with CAS in this same batch.
        activity_ids = {r.id for r in records(reader, "activity", interval)}
        ops.extend(
            Supersede("rule_selection", r.id, r.version)
            for r in records(reader, "rule_selection")
            if r.payload.get("activity_id") in activity_ids
        )
    for activity in result.activities:
        # Keep segmentation independent from attribution and only validate its raw references.
        if "[[" in activity.title_md + activity.description_md:
            raise ValueError("segmentation_must_use_anonymous_titles")
        aid = new_id()
        ops.append(
            Create(
                "activity",
                aid,
                activity.model_dump(mode="json"),
                event_start=activity.start,
                event_end=activity.end,
            )
        )
        current = inputs(reader, activity)
        ops.append(RegisterDependencies("activity", aid, []))
        ops.extend(create_selection(reader, aid, activity, current, run_id, ask))
    return ops, {"empty_reason": result.empty_reason, "activities": len(result.activities)}


def apply_plan(
    reader: Reader, command: Command, run_id: str, ask: Ask
) -> tuple[list[WriteOp], dict]:
    activity = semantic(reader, command.target_id, "activity")
    interval = range_of(activity)
    data = inputs(reader, interval)
    ops: list[WriteOp] = []
    if command.kind == "extract_once":
        assert command.rule is not None
        rule_model = command.rule.model_copy(update={"activity_id": activity.id})
        rule_id, version = new_id(), 1
        rule_payload = rule_model.model_dump(mode="json")
        ops.append(Create("rule", rule_id, rule_payload))
    else:
        rule = semantic(reader, command.rule_id, "rule")
        rule_id, version, rule_payload = rule.id, rule.version, rule.payload
        rule_model = Rule.model_validate(rule_payload)
        if not eligible(rule_model, activity.id):
            raise Blocked("rule_not_enabled_for_activity")
    result = generate(
        ask,
        {
            "activity": activity.model_dump(mode="json"),
            "rule": {"id": rule_id, "version": version, "payload": rule_payload},
            **data.public(),
        },
    )
    validate_generated(result, rule_model, data, rejudge=False)
    id = new_id()
    ops.extend(
        [
            Create(
                "artifact",
                id,
                {
                    "activity_id": activity.id,
                    "rule_id": rule_id,
                    "rule_version": version,
                    "body_md": result.body_md,
                    "evidence_ids": result.evidence_ids,
                    "action_reason": result.action_reason,
                    "run_id": run_id,
                    "manual": True,
                    "source": "manual",
                },
                event_start=interval.start,
                event_end=interval.end,
            ),
            RegisterDependencies(
                "artifact",
                id,
                dependencies(
                    activity, VersionRef(data_type="rule", id=rule_id, version=version), data
                ),
            ),
            *create_suggestion(id, 1, result, interval),
        ]
    )
    return ops, {"artifact_id": id}


def rejudge_plan(reader: Reader, command: Command, ask: Ask) -> ReevaluationPlan:
    artifact = reader.get(command.target_id) if command.target_id else None
    if not isinstance(artifact, SemanticRecord):
        return unavailable(None, "artifact_missing")
    if artifact.superseded:
        return unavailable(artifact, "artifact_terminated")
    guard = AssertTypeRevision("rule", reader.type_revision("rule"))
    rule = reader.get(artifact.payload["rule_id"])
    if not isinstance(rule, SemanticRecord) or rule.superseded:
        return terminate(
            artifact, "rule_unavailable", [guard, *pending_replacements(reader, {artifact.id})]
        )
    activity = reader.get(artifact.payload["activity_id"])
    if not isinstance(activity, SemanticRecord) or activity.superseded:
        return terminate(
            artifact, "activity_unavailable", [guard, *pending_replacements(reader, {artifact.id})]
        )
    data = inputs(reader, range_of(activity))
    result: Generated = ask(
        "reevaluation",
        {
            "activity": activity.model_dump(mode="json"),
            "old_artifact": artifact.payload,
            "rule": rule.model_dump(mode="json"),
            "change_description": command.description,
            **data.public(),
        },
        Generated,
    )
    validate_generated(result, Rule.model_validate(rule.payload), data, rejudge=True)
    rule_changed = rule.version != artifact.payload["rule_version"]
    if result.unchanged and rule_changed:
        # A new rule may change action policy while leaving Markdown identical.
        # Require a complete result rather than copying old pending actions.
        raise ValueError("changed_rule_requires_complete_result")
    # Keep old evidence/text byte-for-byte when unchanged. No reverse parsing of
    # legacy markers and no rewriting historical rows. Rule provenance is current.
    payload = {**artifact.payload, "rule_version": rule.version}
    body_changed = not result.unchanged and result.body_md != artifact.payload["body_md"]
    if body_changed or (rule_changed and not result.unchanged):
        payload.update(
            body_md=result.body_md,
            evidence_ids=result.evidence_ids,
            action_reason=result.action_reason,
        )
    changed = payload != artifact.payload
    ops: list[WriteOp] = [guard, Update("artifact", artifact.id, artifact.version, payload)]
    if changed:
        ops.extend(pending_replacements(reader, {artifact.id}))
        ops.extend(create_suggestion(artifact.id, artifact.version + 1, result, range_of(activity)))
    ops.append(RegisterDependencies("artifact", artifact.id, dependencies(activity, rule, data)))
    outcome = "changed" if changed else "unchanged"
    return ReevaluationPlan(
        ops,
        {"artifact_id": artifact.id, "outcome": outcome},
        artifact.version,
        artifact.version + int(changed),
        outcome,
    )
