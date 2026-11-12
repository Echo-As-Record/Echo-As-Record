"""Additive local Command protocol; unchanged legacy terminal receipt meaning."""

import hashlib
import json
from typing import Literal

import pytest
from ear_contracts import new_id
from ear_extraction.domain import Action, ActionExecution, Blocked, Command
from ear_extraction.suggestions import execution_key, pending_replacements, suggestion_operations
from ear_storage_client import Context, Create, MemoryStorage
from pydantic import ValidationError

ACTION = {"kind": "reminder", "title": "Synthetic", "due_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def owner():
    client = MemoryStorage().client("extraction")
    sid = new_id()
    client.write(
        [
            Create(
                "action_suggestion",
                sid,
                {
                    "state": "approved",
                    "action": ACTION,
                    "artifact_id": new_id(),
                },
            )
        ],
        Context.system(),
    ).check()
    return client, sid


def command(
    sid,
    *,
    version=1,
    approved=1,
    operation: Literal["start", "result", "confirm_executed", "retry"] = "start",
    outcome: Literal["executing", "unknown", "executed", "failed"] = "executing",
    attempt=1,
):
    text = json.dumps(
        Action.model_validate(ACTION).model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return Command(
        kind="action_result",
        target_id=sid,
        expected_version=version,
        outcome=outcome,
        receipt=f"synthetic:{version}:{outcome}",
        execution=ActionExecution(
            operation=operation,
            action_key=execution_key(sid, approved, digest),
            action_hash=digest,
            approved_version=approved,
            attempt=attempt,
            acknowledge_duplicate_risk=operation == "retry",
            acknowledge_executed=operation == "confirm_executed",
        ),
    )


def apply(client, cmd):
    client.write(suggestion_operations(client, cmd), Context.system()).check()


@pytest.mark.parametrize("outcome", ["unknown", "executed", "failed"])
def test_start_and_results_are_exact_versioned(owner, outcome):
    client, sid = owner
    apply(client, command(sid))
    assert client.get(sid).payload["state"] == "executing"
    with pytest.raises(Blocked, match="version"):
        apply(client, command(sid))  # stale replay is not silently relaxed
    apply(client, command(sid, version=2, operation="result", outcome=outcome))
    assert client.get(sid).payload["state"] == outcome
    assert [r.payload["state"] for r in client.history(sid)] == ["approved", "executing", outcome]


@pytest.mark.parametrize("outcome", ["executed", "failed"])
def test_original_receipt_protocol_retains_old_semantics(owner, outcome):
    client, sid = owner
    cmd = Command(
        kind="action_result",
        target_id=sid,
        expected_version=1,
        outcome=outcome,
        receipt="legacy-receipt",
    )
    assert "execution" not in cmd.model_dump(mode="json")
    apply(client, cmd)
    assert client.get(sid).payload["state"] == outcome
    with pytest.raises(Blocked, match="version"):
        apply(client, cmd)
    same = cmd.model_copy(update={"expected_version": 2})
    assert suggestion_operations(client, same) == []


def test_legacy_receipt_cannot_complete_v2_claim(owner):
    client, sid = owner
    apply(client, command(sid))
    with pytest.raises(Blocked, match="protocol_required"):
        apply(
            client,
            Command(
                kind="action_result",
                target_id=sid,
                expected_version=2,
                outcome="executed",
                receipt="forged",
            ),
        )


@pytest.mark.parametrize("approved,attempt", [(2, 1), (1, 2)])
def test_initial_claim_cannot_rebase_approval_or_attempt(owner, approved, attempt):
    client, sid = owner
    with pytest.raises(Blocked, match="start_state"):
        apply(client, command(sid, approved=approved, attempt=attempt))


@pytest.mark.parametrize(
    "state", ["pending", "rejected", "executing", "unknown", "executed", "failed"]
)
def test_start_requires_approved_not_merely_valid_action(owner, state):
    client, _ = owner
    sid = new_id()
    client.write(
        [Create("action_suggestion", sid, {"state": state, "action": ACTION})], Context.system()
    ).check()
    with pytest.raises(Blocked, match="start_state"):
        apply(client, command(sid))


@pytest.mark.parametrize("operation", ["retry", "confirm_executed"])
def test_user_recovery_from_unknown_preserves_identity(owner, operation):
    client, sid = owner
    apply(client, command(sid))
    apply(client, command(sid, version=2, operation="result", outcome="unknown"))
    old = client.get(sid).payload["execution"]
    apply(
        client,
        command(
            sid,
            version=3,
            operation=operation,
            outcome="executing" if operation == "retry" else "executed",
            attempt=2 if operation == "retry" else 1,
        ),
    )
    new = client.get(sid).payload["execution"]
    assert new["action_key"] == old["action_key"]
    assert new["action_hash"] == old["action_hash"]
    assert new["approved_version"] == old["approved_version"]


@pytest.mark.parametrize(
    "operation,outcome,attempt",
    [
        ("retry", "executing", 2),
        ("confirm_executed", "executed", 1),
        ("result", "executed", 1),
        ("result", "unknown", 1),
    ],
)
def test_terminal_states_cannot_be_overwritten(owner, operation, outcome, attempt):
    client, sid = owner
    apply(client, command(sid))
    apply(client, command(sid, version=2, operation="result", outcome="executed"))
    with pytest.raises(Blocked, match="transition"):
        apply(
            client, command(sid, version=3, operation=operation, outcome=outcome, attempt=attempt)
        )


@pytest.mark.parametrize(
    "field,value",
    [("action_key", "ear_action_" + "0" * 64), ("action_hash", "0" * 64), ("attempt", 2)],
)
def test_wrong_identity_or_attempt_blocks(owner, field, value):
    client, sid = owner
    apply(client, command(sid))
    raw = command(sid, version=2, operation="result", outcome="executed").model_dump()
    raw["execution"][field] = value
    with pytest.raises(Blocked):
        apply(client, Command.model_validate(raw))


@pytest.mark.parametrize(
    "operation,ack",
    [("retry", "acknowledge_duplicate_risk"), ("confirm_executed", "acknowledge_executed")],
)
def test_recovery_requires_explicit_strict_ack(owner, operation, ack):
    _, sid = owner
    cmd = command(
        sid, operation=operation, outcome="executing" if operation == "retry" else "executed"
    )
    for value in (False, 1, "true", None):
        raw = cmd.model_dump()
        raw["execution"][ack] = value
        with pytest.raises(ValidationError):
            Command.model_validate(raw)


@pytest.mark.parametrize(
    "state", ["approved", "executing", "unknown", "executed", "failed", "rejected"]
)
def test_pending_replacements_preserve_every_reviewed_state(owner, state):
    client, sid = owner
    artifact_id = client.get(sid).payload["artifact_id"]
    pending = new_id()
    reviewed = new_id()
    client.write(
        [
            Create("action_suggestion", pending, {"state": "pending", "artifact_id": artifact_id}),
            Create("action_suggestion", reviewed, {"state": state, "artifact_id": artifact_id}),
        ],
        Context.system(),
    ).check()
    ops = pending_replacements(client, {artifact_id})
    assert len(ops) == 1
    client.write(ops, Context.system()).check()
    assert client.get(pending).superseded
    assert not client.get(reviewed).superseded


@pytest.mark.parametrize("version", [True, "1", 1.0, None])
def test_new_protocol_versions_are_strict_without_changing_legacy(owner, version):
    _, sid = owner
    raw = command(sid).model_dump()
    raw["expected_version"] = version
    with pytest.raises(ValidationError):
        Command.model_validate(raw)
