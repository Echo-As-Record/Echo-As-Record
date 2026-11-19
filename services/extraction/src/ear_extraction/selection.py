"""Versioned per-activity selection; artifacts deliberately never depend on it."""

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

from ear_extraction.artifacts import create_artifact, input_dependencies, validate_generated
from ear_extraction.domain import Activity, Rule, Selection
from ear_extraction.long_input import generate
from ear_extraction.persistence import range_of, records, ref
from ear_extraction.reevaluation import ReevaluationPlan, terminate, unavailable
from ear_extraction.rules import eligible
from ear_extraction.segmentation import Inputs, inputs
from ear_extraction.suggestions import pending_replacements

Ask = Callable[[str, dict[str, Any], type], Any]


def choose(reader: Reader, activity: Activity, activity_id: str, data: Inputs, ask: Ask):
    # The caller owns one snapshot for BOTH revision and candidate reads.
    revision = reader.type_revision("rule")
    candidates = records(reader, "rule")
    available = [r for r in candidates if eligible(Rule.model_validate(r.payload), activity_id)]
    selected = Selection(rule_ids=[])
    if available:
        selected = ask(
            "selection",
            {
                "activity": activity.model_dump(mode="json"),
                **{**data.public(), "evidence": []},
                "rules": [r.model_dump(mode="json") for r in available],
            },
            Selection,
        )
    if len(set(selected.rule_ids)) != len(selected.rule_ids):
        raise ValueError("duplicate_selected_rules")
    if not set(selected.rule_ids) <= {r.id for r in available}:
        raise ValueError("unknown_selected_rule")
    return sorted(selected.rule_ids), candidates, AssertTypeRevision("rule", revision)


def selection_dependencies(activity: VersionRef, candidates: list[SemanticRecord], data: Inputs):
    return [activity, *[ref(r) for r in candidates], *input_dependencies(data)]


def generate_selected(
    activity_id: str,
    activity: Activity,
    version: int,
    rule: SemanticRecord,
    data: Inputs,
    run_id: str,
    ask: Ask,
) -> list[WriteOp]:
    result = generate(
        ask,
        {
            "activity": activity.model_dump(mode="json"),
            "rule": rule.model_dump(mode="json"),
            **data.public(),
        },
    )
    validate_generated(result, Rule.model_validate(rule.payload), data, rejudge=False)
    return create_artifact(
        new_id(), activity_id, activity, rule, result, data, run_id, activity_version=version
    )


def create_selection(
    reader: Reader, aid: str, activity: Activity, data: Inputs, run_id: str, ask: Ask
) -> list[WriteOp]:
    selected, candidates, guard = choose(reader, activity, aid, data, ask)
    id = new_id()
    ops: list[WriteOp] = [
        guard,
        Create(
            "rule_selection",
            id,
            {"activity_id": aid, "rule_ids": selected},
            event_start=activity.start,
            event_end=activity.end,
        ),
        RegisterDependencies(
            "rule_selection",
            id,
            selection_dependencies(
                VersionRef(data_type="activity", id=aid, version=1), candidates, data
            ),
        ),
    ]
    by_id = {r.id: r for r in candidates}
    for rid in selected:
        ops.extend(generate_selected(aid, activity, 1, by_id[rid], data, run_id, ask))
    return ops


def reselect_plan(reader: Reader, id: str, run_id: str, ask: Ask) -> ReevaluationPlan:
    selection = reader.get(id)
    if not isinstance(selection, SemanticRecord):
        return unavailable(None, "selection_missing")
    if selection.superseded:
        return unavailable(selection, "selection_terminated")
    activity = reader.get(selection.payload["activity_id"])
    if not isinstance(activity, SemanticRecord) or activity.superseded:
        return terminate(selection, "activity_unavailable", [])
    model = Activity.model_validate(activity.payload)
    data = inputs(reader, range_of(activity))
    selected, candidates, guard = choose(reader, model, activity.id, data, ask)
    old = set(selection.payload["rule_ids"])
    payload = {**selection.payload, "rule_ids": selected}
    ops: list[WriteOp] = [
        guard,
        Update("rule_selection", id, selection.version, payload),
        RegisterDependencies(
            "rule_selection", id, selection_dependencies(ref(activity), candidates, data)
        ),
    ]
    exiting = old - set(selected)
    artifacts = [
        a
        for a in records(reader, "artifact")
        if a.payload.get("activity_id") == activity.id
        and a.payload.get("rule_id") in exiting
        and not a.payload.get("manual")
        and a.payload.get("source") != "manual"
    ]
    ops.extend(Supersede("artifact", a.id, a.version) for a in artifacts)
    ops.extend(pending_replacements(reader, {a.id for a in artifacts}))
    by_id = {r.id: r for r in candidates}
    for rid in sorted(set(selected) - old):
        ops.extend(
            generate_selected(activity.id, model, activity.version, by_id[rid], data, run_id, ask)
        )
    # Still selected means NO artifact generation/update, even when its rule changed.
    changed = payload != selection.payload
    return ReevaluationPlan(
        ops,
        {"outcome": "changed" if changed else "unchanged", "selected_rule_ids": selected},
        selection.version,
        selection.version + int(changed),
        "changed" if changed else "unchanged",
    )
