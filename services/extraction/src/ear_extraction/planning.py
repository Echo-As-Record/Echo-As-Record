"""Model-driven plans: compute outside transactions, commit through ① afterwards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from ear_contracts import SemanticRecord, VersionRef, new_id
from ear_storage_client import (
    Create,
    Reader,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
)

from ear_extraction.artifacts import create_artifact, dependencies, validate_generated
from ear_extraction.domain import (
    Blocked,
    Command,
    Generated,
    Interval,
    Rule,
    Selection,
)
from ear_extraction.long_input import generate, segment
from ear_extraction.persistence import range_of, records, semantic
from ear_extraction.rules import eligible
from ear_extraction.segmentation import check_refs, inputs
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
    data = inputs(reader, interval)
    result = segment(ask, interval, data.raw)
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
    rules = records(reader, "rule")
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
        available = [r for r in rules if eligible(Rule.model_validate(r.payload), aid)]
        if not available:
            continue
        selected: Selection = ask(
            "selection",
            {
                "activity": activity.model_dump(mode="json"),
                **{**current.public(), "evidence": []},
                "rules": [r.model_dump(mode="json") for r in available],
            },
            Selection,
        )
        if len(set(selected.rule_ids)) != len(selected.rule_ids):
            raise ValueError("duplicate_selected_rules")
        by_id = {r.id: r for r in available}
        if not set(selected.rule_ids) <= by_id.keys():
            raise ValueError("unknown_selected_rule")
        for rid in selected.rule_ids:
            rule = by_id[rid]
            generated = generate(
                ask,
                {
                    "activity": activity.model_dump(mode="json"),
                    "rule": rule.model_dump(mode="json"),
                    **current.public(),
                },
            )
            validate_generated(generated, Rule.model_validate(rule.payload), current, rejudge=False)
            ops.extend(create_artifact(new_id(), aid, activity, rule, generated, current, run_id))
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
                },
                event_start=interval.start,
                event_end=interval.end,
            ),
            RegisterDependencies(
                "artifact",
                id,
                [
                    VersionRef(data_type="activity", id=activity.id, version=activity.version),
                    VersionRef(data_type="rule", id=rule_id, version=version),
                    *[
                        VersionRef(data_type="attribution", id=a.id, version=a.version)
                        for a in data.attributions
                    ],
                ],
            ),
            *create_suggestion(id, 1, result, interval),
        ]
    )
    return ops, {"artifact_id": id}


def rejudge_plan(
    reader: Reader, command: Command, ask: Ask
) -> tuple[list[WriteOp], dict, int, Literal["changed", "unchanged"]]:
    artifact = semantic(reader, command.target_id, "artifact")
    activity = semantic(reader, artifact.payload["activity_id"], "activity")
    rule = semantic(reader, artifact.payload["rule_id"], "rule")
    if rule.version != artifact.payload["rule_version"]:
        # Shared contract is intentionally not weakened here.
        raise Blocked("historical_rule_dependency_unsupported")
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
    changed = not result.unchanged and result.body_md != artifact.payload["body_md"]
    ops: list[WriteOp] = []
    if changed:
        ops.append(
            Update(
                "artifact",
                artifact.id,
                artifact.version,
                {
                    **artifact.payload,
                    "body_md": result.body_md,
                    "evidence_ids": result.evidence_ids,
                    "action_reason": result.action_reason,
                },
            )
        )
        ops.extend(pending_replacements(reader, {artifact.id}))
        ops.extend(create_suggestion(artifact.id, artifact.version + 1, result, range_of(activity)))
    else:
        # A declared unchanged result cannot preserve now-invalid identity references.
        check_refs(artifact.payload["body_md"], data.entities)
        ops.append(Update("artifact", artifact.id, artifact.version, artifact.payload))
    ops.append(RegisterDependencies("artifact", artifact.id, dependencies(activity, rule, data)))
    outcome = "changed" if changed else "unchanged"
    return ops, {"artifact_id": artifact.id, "outcome": outcome}, artifact.version, outcome
