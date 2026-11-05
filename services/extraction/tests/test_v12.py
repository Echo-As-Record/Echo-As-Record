"""Authorized v1.2 migration behavior over the existing shared contract."""

import pytest
from ear_contracts import SemanticRecord
from ear_extraction.demo import DemoGateway, fixture, submit
from ear_extraction.domain import Command, Rule
from ear_extraction.orchestration import ExtractionService
from ear_extraction.testing.scenario import run_scenario
from ear_storage_client import Context, Update

from .test_extraction import RANGE, extract, items, rejudge, run
from .test_extraction import env as env


@pytest.mark.parametrize("fault", [None, "http_503", "invalid_output"])
def test_mock_http_event_integration(fault):
    report = run_scenario(fault=fault)
    assert report["status"] == "passed"
    assert all(report["checks"].values())
    assert report["external_actions"] == 1


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
def test_rule_invalidated_supersedes_only_pending(env, approved, deleted):
    extract(env)
    artifact = items(env, "artifact")[0]
    suggestion = items(env, "action_suggestion")[0]
    rule = items(env, "rule")[0]
    if approved:
        submit(
            env[0],
            Command(
                kind="review_suggestion",
                target_id=suggestion.id,
                expected_version=1,
                decision="approved",
            ),
        )
        env[4].drain()
    submit(
        env[0],
        Command(
            kind="rule_delete" if deleted else "rule_update",
            target_id=rule.id,
            expected_version=1,
            rule=None if deleted else Rule(scope="组会", instruction="待办", enabled=False),
        ),
    )
    env[4].drain()
    assert items(env, "artifact")[0].version == 1  # Editing alone never sweeps history.
    call_count = len(env[2].calls)
    id = rejudge(env, artifact)
    assert run(env, id).payload["detail"]["outcome"] == "not_applicable"
    assert run(env, id).payload["detail"]["extra_causes"][0]["reason"] == "rule_unavailable"
    assert not items(env, "artifact")
    assert len(env[2].calls) == call_count  # No model needed for a deleted rule.
    current = env[0].client("extraction").get(suggestion.id)
    assert isinstance(current, SemanticRecord)
    assert current.superseded is not approved
    if approved:
        assert current.payload["state"] == "approved"


def test_rename_updates_plain_markdown_and_entity_dependencies(env):
    extract(env)
    artifact = items(env, "artifact")[0]
    client = env[0].client("attribution")
    person = client.get(env[1]["person"])
    client.write(
        [Update("person", person.id, person.version, {**person.payload, "name": "合成新名称"})],
        Context.system(),
    ).check()
    stale = client.stale_dependencies([person.id])
    assert [d.dependent.id for d in stale] == [artifact.id]
    id = rejudge(env, artifact)
    assert run(env, id).payload["status"] == "succeeded"
    assert "合成新名称" in items(env, "artifact")[0].payload["body_md"]
    assert not client.stale_dependencies([person.id])
    calls = env[2].calls
    assert all("attributions" not in c.data for c in calls if c.purpose == "segmentation")
    assert all(set(e["payload"]) == {"name"} for c in calls for e in c.data.get("entities", []))


def test_entity_update_during_generation_is_not_committed(env):
    changed = False

    def mutate(call, output):
        nonlocal changed
        if call.purpose == "generation" and not changed:
            changed = True
            client = env[0].client("attribution")
            person = client.get(env[1]["person"])
            client.write(
                [
                    Update(
                        "person", person.id, person.version, {**person.payload, "name": "并发改名"}
                    )
                ],
                Context.system(),
            ).check()
        return output

    env[2].mutate = mutate
    id = extract(env)
    assert run(env, id).payload["error"] == "storage_conflict"
    assert not items(env, "artifact")
    env[3].advance()
    env[4].drain()
    assert "并发改名" in items(env, "artifact")[0].payload["body_md"]


def test_current_rule_requires_full_result_and_preserves_previous_on_failure(env):
    extract(env)
    artifact, rule = items(env, "artifact")[0], items(env, "rule")[0]
    submit(
        env[0],
        Command(
            kind="rule_update",
            target_id=rule.id,
            expected_version=1,
            rule=Rule(scope="组会", instruction="新内容"),
        ),
    )
    env[4].drain()
    env[2].unchanged = True
    id = rejudge(env, artifact)
    assert run(env, id).payload["status"] == "failed"
    assert items(env, "artifact")[0] == artifact
    env[2].unchanged = False
    env[4].retry(id)
    env[4].drain()
    assert run(env, id).payload["status"] == "succeeded"
    assert items(env, "artifact")[0].payload["rule_version"] == 2


def test_action_date_change_updates_pending_suggestion_even_with_same_markdown(env):
    extract(env)
    artifact, suggestion = items(env, "artifact")[0], items(env, "action_suggestion")[0]

    def mutate(call, output):
        if call.purpose == "reevaluation":
            output["action"]["due_at"] = "2026-11-07T09:00:00Z"
        return output

    env[2].mutate = mutate
    id = rejudge(env, artifact)
    assert run(env, id).payload["detail"]["outcome"] == "changed"
    current = items(env, "artifact")[0]
    assert current.payload["body_md"] == artifact.payload["body_md"]
    assert current.version == 2
    assert items(env, "action_suggestion")[0].id != suggestion.id
    assert items(env, "action_suggestion")[0].payload["action"]["due_at"] == "2026-11-07T09:00:00Z"


@pytest.mark.parametrize("capture", [False, True])
def test_model_body_capture_is_explicit(capture):
    storage, _ = fixture()
    service = ExtractionService(
        storage.client("extraction"), DemoGateway(), capture_model_content=capture
    )
    service.subscribe()
    submit(storage, Command(kind="rule_create", rule=Rule(scope="组会", instruction="待办")))
    service.drain()
    id = submit(storage, Command(kind="extract", interval=RANGE))
    service.drain()
    current = service.storage.get_system_record(id)
    assert current is not None
    for call in current.payload["model_calls"]:
        assert ("input" in call) == capture
        assert ("output" in call) == capture
        assert call["duration_ms"] >= 0


def test_failed_attempt_metadata_and_completion_are_preserved(env):
    env[2].failure = TimeoutError("must not be recorded")
    id = extract(env)
    first = run(env, id)
    assert first.payload["model_calls"][0]["status"] == "failed"
    assert not env[0].client("extraction").is_processed("extraction.completed", id)
    env[2].failure = None
    env[3].advance()
    env[4].drain()
    final = run(env, id)
    assert final.payload["model_calls"][0] == first.payload["model_calls"][0]
    assert final.payload["completed_at"]
    assert "must not be recorded" not in str(final.payload)
    assert env[0].client("extraction").is_processed("extraction.completed", id)


def test_run_id_collision_is_explicit_instead_of_silently_dropping_request(env):
    from ear_contracts import Cause, ReevaluationRequested, new_id
    from ear_storage_client import PutSystemRecord

    event_id = new_id()
    env[0].client("dependency").write(
        [PutSystemRecord("propagation_record", event_id, {"status": "waiting"})],
        Context.system(),
    ).check()
    event = ReevaluationRequested(
        event_id=event_id,
        occurred_at=RANGE.end,
        target_type="artifact",
        target_id=new_id(),
        cause=Cause(
            data_type="attribution", data_id=env[1]["attribution"], old_version=1, new_version=2
        ),
        change_description="模拟纠正",
        root_feedback_id="system",
        depth=1,
    )
    with pytest.raises(ValueError, match="extraction_run_id_collision"):
        env[4].handle_event(event)
