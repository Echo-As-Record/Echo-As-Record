"""Rule feedback plans. Changes never implicitly trigger historical extraction."""

from ear_contracts import new_id
from ear_storage_client import Create, Reader, Supersede, Update, WriteOp

from ear_extraction.domain import Blocked, Command, Rule
from ear_extraction.persistence import semantic


def rule_operations(reader: Reader, command: Command) -> list[WriteOp]:
    if command.kind == "rule_create":
        assert command.rule is not None
        return [Create("rule", new_id(), command.rule.model_dump(mode="json"))]
    current = semantic(reader, command.target_id, "rule")
    if current.version != command.expected_version:
        raise Blocked("rule_version_changed")
    if command.kind == "rule_delete":
        return [Supersede("rule", current.id, current.version, skip_history=True)]
    assert command.rule is not None
    return [
        Update(
            "rule",
            current.id,
            current.version,
            command.rule.model_dump(mode="json"),
            skip_history=True,
        )
    ]


def eligible(rule: Rule, activity_id: str) -> bool:
    return rule.enabled and (rule.activity_id is None or rule.activity_id == activity_id)
