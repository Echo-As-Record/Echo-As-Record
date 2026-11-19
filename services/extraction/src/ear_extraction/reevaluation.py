"""Bounded reevaluation plans and discovered rule provenance, never invented roots."""

from dataclasses import dataclass, field
from typing import Literal

from ear_contracts import Cause, SemanticRecord, TracedCause
from ear_storage_client import Reader, Supersede, WriteOp

from ear_extraction.domain import Command


@dataclass
class ReevaluationPlan:
    ops: list[WriteOp]
    detail: dict
    before: int | None
    after: int | None
    outcome: Literal["changed", "unchanged", "not_applicable"]
    reason_code: str | None = None
    disposition: Literal["terminated", "unavailable", "kept_protected"] | None = None


def unavailable(target: SemanticRecord | None, reason: str) -> ReevaluationPlan:
    version = target.version if target else None
    return ReevaluationPlan(
        [],
        {"outcome": "not_applicable", "reason": reason},
        version,
        version,
        "not_applicable",
        reason,
        "terminated" if target and target.superseded else "unavailable",
    )


def terminate(target: SemanticRecord, reason: str, ops: list[WriteOp]) -> ReevaluationPlan:
    return ReevaluationPlan(
        [Supersede(target.data_type, target.id, target.version), *ops],
        {"outcome": "not_applicable", "reason": reason},
        target.version,
        target.version + 1,
        "not_applicable",
        reason,
        "terminated",
    )


@dataclass
class Discoveries:
    causes: list[Cause] = field(default_factory=list)
    traces: list[TracedCause] = field(default_factory=list)


def discover_rules(reader: Reader, command: Command) -> Discoveries:
    """Inspect immutable rule history so intermediate edits/roots are not lost.

    Original request context remains untouched. These are independently traced
    discoveries, not causes attributed to the feedback that requested reevaluation.
    """
    found = Discoveries()
    target = reader.get(command.target_id) if command.target_id else None
    if not isinstance(target, SemanticRecord) or target.superseded:
        return found
    if command.target_type == "artifact":
        previous = {target.payload["rule_id"]: target.payload["rule_version"]}
    else:
        previous = {
            d.depends_on.id: d.depends_on.version
            for d in reader.dependencies_of(target.id)
            if d.depends_on.data_type == "rule"
        }
        from ear_extraction.persistence import records

        for rule in records(reader, "rule"):
            previous.setdefault(rule.id, 0)
    for id, version in sorted(previous.items()):
        history = [r for r in reader.history(id) if r.data_type == "rule" and r.version > version]
        if not history:
            continue
        cause = Cause(
            data_type="rule",
            data_id=id,
            old_version=version or None,
            new_version=history[-1].version,
        )
        found.causes.append(cause)
        # The cause summarizes the observed delta. Every contributing historical
        # root is retained separately; never assign it the request's original root.
        for rule in history:
            found.traces.append(
                TracedCause(
                    cause=cause, root_feedback_id=rule.root_feedback_id, depth=rule.depth + 1
                )
            )
            found.traces.extend(
                t.model_copy(update={"depth": t.depth + 1}) for t in rule.additional_causes
            )
    return found
