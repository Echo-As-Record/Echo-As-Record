"""Behavioral acceptance tests using shared storage and deterministic model responses."""

from datetime import timedelta

import pytest
from ear_contracts import Cause, new_id
from ear_extraction.demo import DemoGateway, fixture, submit
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.orchestration import ExtractionService, reevaluation_run_id
from ear_extraction.persistence import records
from ear_extraction.scheduler import uncovered
from ear_storage_client import (
    Context,
    Create,
    PublishReevaluation,
    PutSystemRecord,
    Update,
)
from ear_storage_client.testing import ts

RANGE = Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))


class Clock:
    def __init__(self):
        self.now = ts("2026-11-05T10:00:00Z")

    def __call__(self):
        return self.now

    def advance(self, seconds=31):
        self.now += timedelta(seconds=seconds)


class Gateway(DemoGateway):
    def __init__(self):
        self.calls = []
        self.mutate = None
        self.failure = None
        self.unchanged = False

    def complete(self, call):
        self.calls.append(call)
        if self.failure:
            raise self.failure
        if self.unchanged and call.purpose == "reevaluation":
            return {"unchanged": True}
        result = super().complete(call)
        if self.mutate:
            return self.mutate(call, result)
        return result


@pytest.fixture
def env():
    storage, ids = fixture()
    gateway, clock = Gateway(), Clock()
    service = ExtractionService(storage.client("extraction"), gateway, clock=clock)
    service.subscribe()
    submit(
        storage,
        Command(
            kind="rule_create", rule=Rule(scope="组会", instruction="明确待办", action="reminder")
        ),
    )
    service.drain()
    return storage, ids, gateway, clock, service


def extract(env):
    storage, _, _, _, service = env
    run_id = submit(storage, Command(kind="extract", interval=RANGE))
    service.drain()
    return run_id


def run(env, id):
    return env[0].client("extraction").get_system_record(id)


def items(env, kind):
    return records(env[0].client("extraction"), kind)


def rejudge(env, artifact, *, version=2):
    storage, ids, _, _, service = env
    event_id = new_id()
    storage.client("dependency").write(
        [
            PublishReevaluation(
                "artifact",
                artifact.id,
                Cause(
                    data_type="attribution",
                    data_id=ids["attribution"],
                    old_version=version - 1,
                    new_version=version,
                ),
                "测试归属修正",
                event_id=event_id,
            )
        ],
        Context.system().deeper(),
    ).check()
    storage.run_until_idle()
    service.drain()
    return reevaluation_run_id(event_id)


def test_manual_extraction_and_duplicate_delivery(env):
    id = extract(env)
    assert run(env, id).payload["status"] == "succeeded"
    assert len(items(env, "activity")) == len(items(env, "artifact")) == 1
    assert len(items(env, "action_suggestion")) == 1
    artifact = items(env, "artifact")[0]
    deps = env[0].client("extraction").dependencies_of(artifact.id)
    assert {d.depends_on.data_type for d in deps} == {"activity", "rule", "attribution"}
    assert all(c.module == "extraction" and c.root_feedback_id != "system" for c in env[2].calls)
    env[4].drain()
    env[0].run_until_idle()
    assert len(items(env, "artifact")) == 1


def test_suggestion_approval_and_receipt(env):
    extract(env)
    suggestion = items(env, "action_suggestion")[0]
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
    assert items(env, "action_suggestion")[0].payload["state"] == "approved"
    submit(
        env[0],
        Command(
            kind="action_result",
            target_id=suggestion.id,
            expected_version=2,
            outcome="executed",
            receipt="synthetic-receipt",
        ),
    )
    env[4].drain()
    assert items(env, "action_suggestion")[0].payload["state"] == "executed"


def test_execution_before_approval_blocked(env):
    extract(env)
    id = submit(
        env[0],
        Command(
            kind="action_result",
            target_id=items(env, "action_suggestion")[0].id,
            expected_version=1,
            outcome="executed",
            receipt="test",
        ),
    )
    env[4].drain()
    assert run(env, id).payload["status"] == "blocked"
    assert items(env, "action_suggestion")[0].payload["state"] == "pending"


def test_rejudge_changed_preserves_activity_and_approved_suggestions(env):
    storage, ids, _, _, service = env
    extract(env)
    artifact, activity = items(env, "artifact")[0], items(env, "activity")[0]
    suggestion = items(env, "action_suggestion")[0]
    submit(
        storage,
        Command(
            kind="review_suggestion",
            target_id=suggestion.id,
            expected_version=1,
            decision="approved",
        ),
    )
    service.drain()
    p2 = new_id()
    storage.client("attribution").write(
        [
            Create("person", p2, {"name": "合成人物乙", "references": []}),
            Update(
                "attribution",
                ids["attribution"],
                1,
                {"subject_id": ids["voice"], "target_type": "person", "target_id": p2},
            ),
        ],
        Context.system(),
    ).check()
    id = rejudge(env, artifact)
    assert run(env, id).payload["detail"]["outcome"] == "changed"
    assert items(env, "activity")[0].id == activity.id
    assert items(env, "artifact")[0].version == 2
    assert p2 in items(env, "artifact")[0].payload["body_md"]
    assert {r.payload["state"] for r in items(env, "action_suggestion")} == {"approved", "pending"}


def test_rejudge_unchanged_refreshes_dependencies(env):
    storage, ids, gateway, _, _ = env
    extract(env)
    artifact = items(env, "artifact")[0]
    current = storage.client("attribution").get(ids["attribution"])
    storage.client("attribution").write(
        [Update("attribution", current.id, 1, {**current.payload, "reason": "确认归属"})],
        Context.system(),
    ).check()
    gateway.unchanged = True
    id = rejudge(env, artifact)
    assert run(env, id).payload["detail"]["outcome"] == "unchanged"
    assert items(env, "artifact")[0].version == 1
    assert not storage.client("dependency").stale_dependencies([ids["attribution"]])


def test_historical_rule_conflict_is_explicit(env):
    extract(env)
    rule = items(env, "rule")[0]
    submit(
        env[0],
        Command(
            kind="rule_update",
            target_id=rule.id,
            expected_version=1,
            rule=Rule(scope="新范围", instruction="新要求"),
        ),
    )
    env[4].drain()
    assert items(env, "artifact")[0].version == 1
    id = rejudge(env, items(env, "artifact")[0])
    assert run(env, id).payload["error"] == "historical_rule_dependency_unsupported"
    assert items(env, "artifact")[0].version == 1


@pytest.mark.parametrize("kind", ["apply_rule", "extract_once"])
def test_apply_rule_appends_without_segmenting(env, kind):
    extract(env)
    before_calls = len(env[2].calls)
    command = Command(
        kind=kind,
        target_id=items(env, "activity")[0].id,
        rule_id=items(env, "rule")[0].id,
        rule=Rule(scope="仅本次", instruction="整理笔记") if kind == "extract_once" else None,
    )
    id = submit(env[0], command)
    env[4].drain()
    assert run(env, id).payload["status"] == "succeeded"
    assert len(items(env, "artifact")) == 2
    assert len(items(env, "activity")) == 1
    assert [c.purpose for c in env[2].calls[before_calls:]] == ["generation"]


def test_reextract_failure_keeps_previous_results(env):
    extract(env)
    artifact = items(env, "artifact")[0]
    preview = env[4].preview_reextract(RANGE)
    env[2].failure = ValueError("bad model response")
    id = submit(env[0], Command(kind="reextract", interval=RANGE, preview=preview["versions"]))
    env[4].drain()
    assert run(env, id).payload["status"] == "blocked"
    assert run(env, id).payload["error"] == "gateway_outcome_unknown"
    assert items(env, "artifact")[0].id == artifact.id


def test_reextract_atomically_replaces_pending_results(env):
    extract(env)
    old = items(env, "artifact")[0]
    preview = env[4].preview_reextract(RANGE)
    id = submit(env[0], Command(kind="reextract", interval=RANGE, preview=preview["versions"]))
    env[4].drain()
    assert run(env, id).payload["status"] == "succeeded"
    assert items(env, "artifact")[0].id != old.id
    assert env[0].client("extraction").get(old.id).superseded
    assert len(items(env, "action_suggestion")) == 1


def test_stale_reextract_preview_and_partial_overlap(env):
    extract(env)
    with pytest.raises(Exception, match="partial_activity_overlap"):
        env[4].preview_reextract(Interval(start=ts("2026-11-05T09:30:00Z"), end=RANGE.end))
    id = submit(env[0], Command(kind="reextract", interval=RANGE, preview={}))
    env[4].drain()
    assert run(env, id).payload["error"] == "reextract_preview_changed"


@pytest.mark.parametrize("fault", ["unknown_reference", "bad_time", "unknown_rule", "no_evidence"])
def test_invalid_model_outputs_never_publish(env, fault):
    def mutate(call, result):
        if fault == "unknown_reference" and call.purpose == "generation":
            result["body_md"] = f"[[person:{new_id()}]] 的待办"
        if fault == "bad_time" and call.purpose == "segmentation":
            result["activities"][0]["end"] = "2026-11-06T00:00:00Z"
        if fault == "unknown_rule" and call.purpose == "selection":
            result["rule_ids"] = [new_id()]
        if fault == "no_evidence" and call.purpose == "generation":
            result["evidence_ids"] = []
        return result

    env[2].mutate = mutate
    id = extract(env)
    assert run(env, id).payload["status"] == "failed"
    assert not items(env, "artifact")
    assert not items(env, "activity")


def test_missing_attribution_waits_then_times_out(env):
    from ear_storage_client import AppendRaw
    from ear_storage_client.testing import make_raw

    env[0].client("adapter").write(
        [AppendRaw(make_raw("gps_point", {"lat": 0}, start="2026-11-05T09:30:00Z"))],
        Context.system(),
    ).check()
    id = extract(env)
    assert run(env, id).payload["status"] == "waiting_attribution"
    env[3].advance(1801)
    env[4].drain()
    assert run(env, id).payload["status"] == "failed"
    assert not items(env, "artifact")


def test_gateway_timeout_is_blocked_not_transient_retry(env):
    env[2].failure = TimeoutError()
    id = extract(env)
    assert run(env, id).payload["status"] == "blocked"
    assert run(env, id).payload["error"] == "gateway_outcome_unknown"
    count = len(env[2].calls)
    env[2].failure = None
    env[3].advance()
    env[4].drain()
    with pytest.raises(ValueError, match="gateway_outcome_unknown"):
        env[4].retry(id)
    assert len(env[2].calls) == count
    assert not items(env, "artifact")


def test_storage_conflict_does_not_publish_stale_artifact(env):
    original = env[0].client("attribution").get(env[1]["attribution"])
    fired = False

    def mutate(call, result):
        nonlocal fired
        if call.purpose == "generation" and not fired:
            fired = True
            env[0].client("attribution").write(
                [
                    Update(
                        "attribution",
                        original.id,
                        1,
                        {**original.payload, "reason": "new revision"},
                    )
                ],
                Context.system(),
            ).check()
        return result

    env[2].mutate = mutate
    id = extract(env)
    assert run(env, id).payload["error"] == "gateway_result_not_committed"
    assert run(env, id).payload["status"] == "blocked"
    assert not items(env, "activity")
    count = len(env[2].calls)
    env[3].advance()
    env[4].drain()
    with pytest.raises(ValueError, match="gateway_result_not_committed"):
        env[4].retry(id)
    assert len(env[2].calls) == count
    assert not items(env, "artifact")


def test_recovery_after_admission_and_expired_claim(env):
    storage, _, gateway, clock, service = env
    id = submit(storage, Command(kind="extract", interval=RANGE))
    current = run(env, id)
    storage.client("extraction").write(
        [
            PutSystemRecord(
                "extraction_run",
                id,
                {**current.payload, "status": "running", "lease_until": "2026-11-05T10:00:30Z"},
                expected_revision=current.revision,
            )
        ],
        Context.system(),
    ).check()
    recovered = ExtractionService(storage.client("extraction"), gateway, clock=clock)
    recovered.drain()
    assert not items(env, "artifact")
    clock.advance()
    recovered.drain()
    assert run(env, id).payload["status"] == "succeeded"
    service.drain()
    assert len(items(env, "artifact")) == 1


def test_schedule_reserves_pending_and_skips_success(env):
    ids = env[4].schedule(RANGE)
    assert len(ids) == 1
    assert env[4].schedule(RANGE) == []
    env[4].drain()
    assert run(env, ids[0]).payload["status"] == "succeeded"
    assert env[4].schedule(RANGE) == []


def test_failed_schedule_before_call_is_explicitly_retryable(env):
    # Budget validation occurs before creating a call intent: this is safe to retry.
    limited = ExtractionService(
        env[0].client("extraction"), env[2], clock=env[3], max_input_chars=10
    )
    id = limited.schedule(RANGE)[0]
    limited.drain()
    assert run(env, id).payload["gateway_calls"] == []
    assert limited.schedule(RANGE) == [id]
    env[4].retry(id)
    env[4].drain()
    assert run(env, id).payload["status"] == "succeeded"


def test_interval_subtraction_merges_overlap():
    start = ts("2026-11-05T00:00:00Z")

    def interval(a, b):
        return Interval(start=start + timedelta(hours=a), end=start + timedelta(hours=b))

    assert uncovered(interval(0, 24), [interval(1, 4), interval(3, 6), interval(9, 10)]) == [
        interval(0, 1),
        interval(6, 9),
        interval(10, 24),
    ]


def test_day_range_and_budget_failure(env):
    day = Interval(start=ts("2026-11-05T00:00:00Z"), end=ts("2026-11-06T00:00:00Z"))
    id = submit(env[0], Command(kind="extract", interval=day))
    env[4].drain()
    assert run(env, id).payload["status"] == "succeeded"
    limited = ExtractionService(
        env[0].client("extraction"), env[2], clock=env[3], max_input_chars=10
    )
    id = submit(env[0], Command(kind="extract", interval=day))
    limited.drain()
    assert run(env, id).payload["status"] == "failed"


def test_zero_rules_is_valid_success(env):
    rule = items(env, "rule")[0]
    submit(env[0], Command(kind="rule_delete", target_id=rule.id, expected_version=1))
    env[4].drain()
    id = extract(env)
    assert run(env, id).payload["status"] == "succeeded"
    assert not items(env, "artifact")
    assert len(items(env, "activity")) == 1


def test_scheduled_job_rechecks_manual_coverage(env):
    scheduled = env[4].schedule(RANGE)[0]
    manual = submit(env[0], Command(kind="extract", interval=RANGE))
    env[4].process(manual)
    env[4].process(scheduled)
    assert run(env, scheduled).payload["status"] == "superseded"
    assert len(items(env, "activity")) == 1


def test_bounded_long_input_through_real_model_port(env):
    from ear_storage_client import AppendRaw
    from ear_storage_client.testing import make_raw

    env[0].client("adapter").write(
        [
            AppendRaw(
                make_raw(
                    "transcript_segment",
                    {"text": "合成测试内容" * 100},
                    start=f"2026-11-05T09:{minute:02d}:00Z",
                    end=f"2026-11-05T09:{minute:02d}:30Z",
                )
            )
            for minute in range(2, 30, 2)
        ],
        Context.system(),
    ).check()
    service = ExtractionService(
        env[0].client("extraction"), env[2], clock=env[3], max_input_chars=6500
    )
    id = submit(env[0], Command(kind="extract", interval=RANGE))
    service.drain()
    assert run(env, id).payload["status"] == "succeeded"
    purposes = [c.purpose for c in env[2].calls]
    assert "merge_activities" in purposes
    assert "consolidation" in purposes
    assert len(items(env, "activity")) == 1
