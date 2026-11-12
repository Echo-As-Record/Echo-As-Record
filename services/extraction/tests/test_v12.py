"""Accepted v1.2 business slice: synthetic data/models, real SDK atomic batches."""

from typing import Literal

import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, SemanticRecord, TracedCause, new_id
from ear_extraction.demo import DemoGateway, fixture, fixture_registry, submit
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.orchestration import COMPLETION_CONSUMER, ExtractionService, reevaluation_run_id
from ear_extraction.persistence import records
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    MemoryStorage,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
    Update,
)
from ear_storage_client.testing import make_raw, ts

RANGE = Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))


class Model(DemoGateway):
    def __init__(self):
        self.calls = []
        self.selected = None
        self.unchanged = False
        self.mutate = lambda call: None
        self.invalid = False

    def complete(self, call):
        self.calls.append(call)
        self.mutate(call)
        if call.purpose == "selection" and self.selected is not None:
            return {"rule_ids": self.selected}
        if call.purpose == "reevaluation":
            if self.invalid:
                return {"body_md": "invalid without evidence"}
            if self.unchanged:
                return {"unchanged": True}
        return super().complete(call)


class Env:
    def __init__(self):
        self.store, self.ids = fixture()
        self.client = self.store.client("extraction")
        self.model = Model()
        self.service = ExtractionService(self.client, self.model)
        self.service.subscribe()

    def command(self, command):
        id = submit(self.store, command)
        self.service.drain()
        run = self.client.get_system_record(id)
        assert run is not None
        return run

    def rule(self, name="A", action: Literal["calendar", "reminder"] | None = "reminder"):
        self.command(
            Command(kind="rule_create", rule=Rule(scope=name, instruction=name, action=action))
        )
        return next(r for r in records(self.client, "rule") if r.payload["scope"] == name)

    def extract(self):
        result = self.command(Command(kind="extract", interval=RANGE))
        assert result.payload["status"] == "succeeded", result.payload
        return records(self.client, "artifact")

    def request(self, target, *, run=True, context=None):
        attr = self.client.get(self.ids["attribution"])
        assert isinstance(attr, SemanticRecord)
        result = (
            self.store.client("dependency")
            .write(
                [
                    PublishReevaluation(
                        target.data_type,
                        target.id,
                        Cause(
                            data_type="attribution",
                            data_id=attr.id,
                            old_version=None,
                            new_version=attr.version,
                        ),
                        "synthetic input change",
                    )
                ],
                context or Context.system().deeper(),
            )
            .check()
        )
        event = result.events[0]
        self.store.run_until_idle()
        if run:
            self.service.drain()
        return event

    def finish(self, event):
        result = self.client.get_reevaluation_result(event.event_id)
        assert result is not None, self.client.get_system_record(
            reevaluation_run_id(event.event_id)
        )
        assert self.client.is_processed(COMPLETION_CONSUMER, event.event_id)
        return result

    def rename(self, **fields):
        writer = self.store.client("attribution")
        person = writer.get(self.ids["person"])
        assert isinstance(person, SemanticRecord)
        writer.write(
            [Update("person", person.id, person.version, {**person.payload, **fields})],
            Context.system(),
        ).check()


@pytest.fixture
def env():
    return Env()


def test_fixture_spec_does_not_modify_default_registry():
    assert "rule_selection" not in DEFAULT_REGISTRY
    spec = fixture_registry().data_types["rule_selection"]
    assert (spec.kind, spec.writer, spec.unique_fields) == (
        "semantic",
        "extraction",
        ("activity_id",),
    )
    assert spec.requires_dependencies and spec.supports_reevaluation
    assert not DEFAULT_REGISTRY.data_types["artifact"].requires_dependencies


def test_missing_registration_blocks_before_model():
    store = MemoryStorage()
    model = Model()
    service = ExtractionService(store.client("extraction"), model)
    service.subscribe()
    id = submit(store, Command(kind="extract", interval=RANGE))
    service.drain()
    assert service.runs()[0].id == id
    assert service.runs()[0].payload["error"] == "rule_selection_registration_required"
    assert not model.calls


def test_raw_segmentation_plain_markdown_and_actual_name_dependencies(env):
    env.rule()
    artifact = env.extract()[0]
    selection = records(env.client, "rule_selection")[0]
    segment = next(c for c in env.model.calls if c.purpose == "segmentation")
    assert set(segment.data) == {"interval", "evidence"}
    assert env.ids["person"] not in str(segment.data)
    assert "合成人物甲" in artifact.payload["body_md"]
    assert "[[person:" not in artifact.payload["body_md"]
    for call in env.model.calls:
        for entity in call.data.get("entities", []):
            assert set(entity["payload"]) == {"name"}
    for target in (artifact, selection):
        deps = env.client.dependencies_of(target.id)
        assert {d.depends_on.data_type for d in deps} == {
            "activity",
            "rule",
            "person",
            "attribution",
        }
        assert next(d for d in deps if d.depends_on.data_type == "person").fields == ("name",)
    assert env.client.dependency_set(records(env.client, "activity")[0].id).empty
    env.rename(references=[])
    assert not env.client.stale_dependencies([env.ids["person"]])
    env.rename(name="合成新名字")
    assert {d.dependent.data_type for d in env.client.stale_dependencies([env.ids["person"]])} == {
        "artifact",
        "rule_selection",
    }
    event = env.request(artifact)
    assert env.finish(event).outcome == "changed"
    assert "合成新名字" in env.client.get(artifact.id).payload["body_md"]
    assert env.client.get_version(artifact.id, 1) == artifact
    assert records(env.client, "activity")[0].version == 1


def test_missing_name_is_depended_on_for_first_naming(env):
    person = env.client.get(env.ids["person"])
    env.store.client("attribution").write(
        [Update("person", person.id, 1, {"references": []})], Context.system()
    ).check()
    env.rule()
    artifact = env.extract()[0]
    assert "未命名人物" in artifact.payload["body_md"]
    env.rename(name="第一次命名")
    assert any(d.dependent.id == artifact.id for d in env.client.stale_dependencies([person.id]))


def test_all_interval_attributions_even_uncited_are_registered(env):
    gps, place, attr = new_id(), new_id(), new_id()
    env.store.client("adapter").write(
        [
            AppendRaw(
                make_raw("gps_point", {"lat": 0, "lon": 0}, id=gps, start="2026-11-05T09:30:00Z")
            )
        ],
        Context.system(),
    ).check()
    env.store.client("attribution").write(
        [
            Create("place", place, {"name": "合成地点", "references": []}),
            Create(
                "attribution", attr, {"subject_id": gps, "target_type": "place", "target_id": place}
            ),
        ],
        Context.system(),
    ).check()
    env.rule()
    artifact = env.extract()[0]
    assert gps not in artifact.payload["evidence_ids"]
    deps = env.client.dependencies_of(artifact.id)
    assert {d.depends_on.id for d in deps if d.depends_on.data_type == "attribution"} == {
        attr,
        env.ids["attribution"],
    }
    assert next(d for d in deps if d.depends_on.id == place).fields == ("name",)


def test_zero_rules_still_saves_selection_and_new_rule_does_not_scan_history(env):
    assert env.extract() == []
    selection = records(env.client, "rule_selection")[0]
    assert selection.payload["rule_ids"] == []
    rule = env.rule()
    assert not records(env.client, "artifact")
    assert env.client.get(selection.id).version == 1
    event = env.request(selection)
    assert env.finish(event).outcome == "changed"
    assert records(env.client, "artifact")[0].payload["rule_id"] == rule.id
    assert env.client.get(selection.id).version == 2


def test_selection_add_exit_still_and_manual_protection(env):
    first, still = env.rule("first"), env.rule("still")
    artifacts = env.extract()
    automatic_first = next(a for a in artifacts if a.payload["rule_id"] == first.id)
    automatic_still = next(a for a in artifacts if a.payload["rule_id"] == still.id)
    activity = records(env.client, "activity")[0]
    manual_run = env.command(Command(kind="apply_rule", target_id=activity.id, rule_id=first.id))
    manual = env.client.get(manual_run.payload["detail"]["artifact_id"])
    approved = next(
        s
        for s in records(env.client, "action_suggestion")
        if s.payload["artifact_id"] == automatic_first.id
    )
    env.command(
        Command(
            kind="review_suggestion", target_id=approved.id, expected_version=1, decision="approved"
        )
    )
    new = env.rule("new")
    env.command(
        Command(
            kind="rule_update",
            target_id=still.id,
            expected_version=1,
            rule=Rule(scope="still", instruction="changed instruction", action="reminder"),
        )
    )
    env.model.selected = [still.id, new.id]
    before_calls = len(env.model.calls)
    event = env.request(records(env.client, "rule_selection")[0])
    assert env.finish(event).outcome == "changed"
    assert env.client.get(automatic_first.id).superseded
    assert env.client.get(manual.id) == manual
    assert env.client.get(automatic_still.id) == automatic_still
    assert env.client.get(approved.id).payload["state"] == "approved"
    assert [c.purpose for c in env.model.calls[before_calls:]] == ["selection", "generation"]
    assert len([a for a in records(env.client, "artifact") if a.payload["rule_id"] == new.id]) == 1
    for a in records(env.client, "artifact"):
        assert all(
            d.depends_on.data_type != "rule_selection" for d in env.client.dependencies_of(a.id)
        )
    # Manual protection is ONLY against automatic selection; deletion of its own rule terminates it.
    env.command(Command(kind="rule_delete", target_id=first.id, expected_version=1))
    event = env.request(manual)
    assert env.finish(event).disposition == "terminated"
    assert env.client.get(manual.id).superseded


def test_unchanged_selection_refreshes_deps_without_recomputing_artifact(env):
    rule = env.rule()
    artifact = env.extract()[0]
    selection = records(env.client, "rule_selection")[0]
    env.command(
        Command(
            kind="rule_update",
            target_id=rule.id,
            expected_version=1,
            rule=Rule(scope="A", instruction="updated", action="reminder"),
        )
    )
    env.rename(name="另一个合成名字")
    count = len(env.model.calls)
    event = env.request(selection)
    finish = env.finish(event)
    assert finish.outcome == "unchanged" and finish.before_version == finish.after_version == 1
    assert env.client.get(artifact.id) == artifact
    assert [c.purpose for c in env.model.calls[count:]] == ["selection"]
    assert all(
        d.dependent.data_type == "artifact"
        for d in env.client.stale_dependencies([rule.id, env.ids["person"]])
    )
    assert finish.extra_causes[0].new_version == 2


@pytest.mark.parametrize("mutation", ["add", "edit_unselected", "delete"])
def test_rule_collection_race_rejects_entire_selection_batch_without_resend(env, mutation):
    rule = env.rule()
    other = env.rule("unselected")
    env.model.selected = [rule.id]
    artifact = env.extract()[0]
    selection = records(env.client, "rule_selection")[0]
    fired = False

    def mutate(call):
        nonlocal fired
        if call.purpose != "selection" or fired:
            return
        fired = True
        if mutation == "add":
            ops = [
                Create(
                    "rule", new_id(), Rule(scope="new", instruction="new").model_dump(mode="json")
                )
            ]
        elif mutation == "edit_unselected":
            ops = [Update("rule", other.id, 1, {**other.payload, "instruction": "changed"})]
        else:
            ops = [Supersede("rule", other.id, 1)]
        env.client.write(ops, Context.system()).check()

    env.model.mutate = mutate
    event = env.request(selection)
    run = env.client.get_system_record(reevaluation_run_id(event.event_id))
    assert (
        run.payload["status"] == "blocked"
        and run.payload["error"] == "gateway_result_not_committed"
    )
    assert env.client.get_reevaluation_result(event.event_id) is None
    assert env.client.get(selection.id) == selection and env.client.get(artifact.id) == artifact
    calls = len(env.model.calls)
    with pytest.raises(ValueError, match="gateway_result_not_committed"):
        env.service.retry(run.id)
    env.service.handle_event(event)
    env.service.drain()
    assert len(env.model.calls) == calls


def test_current_rule_provenance_preserves_all_roots_and_original_task_fields(env):
    rule = env.rule()
    artifact = env.extract()[0]
    roots = []
    for n in (2, 3):
        run = env.command(
            Command(
                kind="rule_update",
                target_id=rule.id,
                expected_version=n - 1,
                rule=Rule(scope="A", instruction=f"revision {n}", action="reminder"),
            )
        )
        roots.append(run.payload["root_feedback_id"])
    extra = TracedCause(
        cause=Cause(data_type="person", data_id=env.ids["person"], old_version=None, new_version=1),
        root_feedback_id="system",
        depth=4,
    )
    event = env.request(artifact, context=Context("system", 2, additional_causes=(extra,)))
    finish = env.finish(event)
    assert finish.outcome == "changed"
    assert finish.extra_causes == (
        Cause(data_type="rule", data_id=rule.id, old_version=1, new_version=3),
    )
    assert set(roots) <= {t.root_feedback_id for t in finish.additional_causes}
    assert extra in finish.additional_causes
    assert (finish.root_feedback_id, finish.depth) == ("system", 2)
    run = env.client.get_system_record(reevaluation_run_id(event.event_id))
    assert run.payload["additional_causes"] == [extra.model_dump(mode="json")]
    assert run.payload["request_cause"] == event.cause.model_dump(mode="json")
    assert run.payload["execution_additional_causes"] != run.payload["additional_causes"]
    call = env.model.calls[-1]
    assert (
        call.request_event_id == event.event_id
        and call.additional_causes == finish.additional_causes
    )
    assert call.data["rule"]["version"] == 3
    assert env.client.get(artifact.id).payload["rule_version"] == 3
    assert env.client.get_version(artifact.id, 1) == artifact


@pytest.mark.parametrize("reviewed", ["approved", "rejected", "executed", "failed"])
def test_deleted_rule_terminates_artifact_pending_only_without_model(env, reviewed):
    rule = env.rule()
    artifact = env.extract()[0]
    pending = records(env.client, "action_suggestion")[0]
    preserved_id = new_id()
    env.client.write(
        [Create("action_suggestion", preserved_id, {**pending.payload, "state": reviewed})],
        Context.system(),
    ).check()
    preserved = env.client.get(preserved_id)
    env.command(Command(kind="rule_delete", target_id=rule.id, expected_version=1))
    count = len(env.model.calls)
    event = env.request(artifact)
    finish = env.finish(event)
    assert (finish.outcome, finish.disposition, finish.before_version, finish.after_version) == (
        "not_applicable",
        "terminated",
        1,
        2,
    )
    assert finish.reason_code == "rule_unavailable" and finish.extra_causes
    assert env.client.get(artifact.id).superseded and env.client.get(pending.id).superseded
    assert env.client.get(preserved_id) == preserved
    assert env.client.get_version(artifact.id, 1) == artifact
    assert len(env.model.calls) == count
    env.service.handle_event(event)
    env.service.drain()
    assert env.client.get(artifact.id).version == 2


def test_already_terminated_target_finishes_not_applicable(env):
    env.rule()
    artifact = env.extract()[0]
    event = env.request(artifact, run=False)
    env.client.write([Supersede("artifact", artifact.id, 1)], Context.system()).check()
    env.service.drain()
    finish = env.finish(event)
    assert finish.outcome == "not_applicable" and finish.disposition == "terminated"
    assert finish.before_version == finish.after_version == 2


def test_failed_finish_known_invalid_output_is_atomic_and_terminal(env):
    env.rule()
    artifact = env.extract()[0]
    env.model.invalid = True
    event = env.request(artifact)
    finish = env.finish(event)
    assert finish.outcome == "failed" and finish.error_code == "extraction_failed"
    assert finish.before_version == finish.after_version == 1
    assert env.client.get(artifact.id) == artifact
    with pytest.raises(ValueError, match="terminal_request_requires_new_request"):
        env.service.retry(reevaluation_run_id(event.event_id))


def test_failed_finish_preflight_budget_has_no_gateway_call(env):
    env.rule()
    artifact = env.extract()[0]
    event = env.request(artifact, run=False)
    count = len(env.model.calls)
    ExtractionService(env.client, env.model, max_input_chars=1).drain()
    assert env.finish(event).outcome == "failed"
    assert len(env.model.calls) == count


def test_legacy_artifact_and_old_command_remain_readable_without_migration(env):
    env.rule()
    artifact = env.extract()[0]
    legacy = {k: v for k, v in artifact.payload.items() if k not in {"source", "manual"}}
    legacy["body_md"] = f"[[person:{env.ids['person']}]] legacy text"
    deps = env.client.dependencies_of(artifact.id)
    from ear_storage_client import DependencyInput

    env.client.write(
        [
            Update("artifact", artifact.id, 1, legacy),
            RegisterDependencies(
                "artifact", artifact.id, [DependencyInput(d.depends_on, d.fields) for d in deps]
            ),
        ],
        Context.system(),
    ).check()
    current = env.client.get(artifact.id)
    event = env.request(current, run=False)
    run = env.client.get_system_record(reevaluation_run_id(event.event_id))
    old_command = dict(run.payload["command"])
    old_command.pop("target_type", None)
    env.client.write(
        [
            PutSystemRecord(
                "extraction_run",
                run.id,
                {**run.payload, "command": old_command},
                expected_revision=run.revision,
            )
        ],
        Context.of(event),
    ).check()
    env.model.unchanged = True
    env.service.drain()
    assert env.finish(event).outcome == "unchanged"
    assert env.client.get(artifact.id) == current


def test_selection_uniqueness_and_missing_dependencies_are_enforced(env):
    env.extract()
    selection = records(env.client, "rule_selection")[0]
    from ear_storage_client import InvalidRequest

    with pytest.raises(InvalidRequest):
        env.client.write(
            [Update("rule_selection", selection.id, 1, selection.payload)], Context.system()
        )
    result = env.client.write(
        [
            Create("rule_selection", new_id(), selection.payload),
            RegisterDependencies("rule_selection", selection.id, []),
        ],
        Context.system(),
    )
    assert not result.ok


@pytest.mark.parametrize("target_type", ["artifact", "rule_selection"])
def test_valid_output_write_rejection_remains_blocked_not_failed_finish(
    env, monkeypatch, target_type
):
    from ear_storage_client import FinishReevaluation, InvalidRequest

    env.rule()
    env.extract()
    target = records(env.client, target_type)[0]
    event = env.request(target, run=False)
    original = env.client.write

    def reject(ops, context):
        if any(isinstance(op, FinishReevaluation) and op.result.outcome != "failed" for op in ops):
            raise InvalidRequest("synthetic write rejected after a valid model response")
        return original(ops, context)

    monkeypatch.setattr(env.client, "write", reject)
    env.service.drain()
    run = env.client.get_system_record(reevaluation_run_id(event.event_id))
    assert (
        run.payload["status"] == "blocked"
        and run.payload["error"] == "gateway_result_not_committed"
    )
    assert env.client.get_reevaluation_result(event.event_id) is None
    assert env.client.get(target.id) == target
    count = len(env.model.calls)
    with pytest.raises(ValueError, match="gateway_result_not_committed"):
        env.service.retry(run.id)
    env.service.drain()
    assert len(env.model.calls) == count


def test_rule_edit_cannot_reuse_unchanged_to_recreate_old_action(env):
    rule = env.rule()
    artifact = env.extract()[0]
    old_suggestion = records(env.client, "action_suggestion")[0]
    env.command(
        Command(
            kind="rule_update",
            target_id=rule.id,
            expected_version=1,
            rule=Rule(scope="A", instruction="no action", action=None),
        )
    )
    env.model.unchanged = True
    event = env.request(artifact)
    assert env.finish(event).outcome == "failed"
    assert env.client.get(artifact.id) == artifact
    assert env.client.get(old_suggestion.id) == old_suggestion
    # A fresh request with a complete output applies the new rule, even with identical Markdown.
    env.model.unchanged = False
    event = env.request(artifact)
    assert env.finish(event).outcome == "changed"
    assert env.client.get(artifact.id).payload["body_md"] == artifact.payload["body_md"]
    assert env.client.get(artifact.id).payload["rule_version"] == 2
    assert env.client.get(old_suggestion.id).superseded
    assert not records(env.client, "action_suggestion")


def test_selection_deletion_without_model_terminates_only_automatic_pending(env):
    rule = env.rule()
    artifact = env.extract()[0]
    selection = records(env.client, "rule_selection")[0]
    suggestion = records(env.client, "action_suggestion")[0]
    env.command(Command(kind="rule_delete", target_id=rule.id, expected_version=1))
    count = len(env.model.calls)
    event = env.request(selection)
    assert env.finish(event).outcome == "changed"
    assert env.client.get(selection.id).payload["rule_ids"] == []
    assert env.client.get(artifact.id).superseded and env.client.get(suggestion.id).superseded
    assert len(env.model.calls) == count


def test_reextract_retires_old_selection_without_touching_history(env):
    env.rule()
    env.extract()
    selection = records(env.client, "rule_selection")[0]
    preview = env.service.preview_reextract(RANGE)
    assert selection.id not in preview["versions"]  # Existing API preview remains compatible.
    run = env.command(Command(kind="reextract", interval=RANGE, preview=preview["versions"]))
    assert run.payload["status"] == "succeeded"
    assert env.client.get(selection.id).superseded
    assert env.client.get_version(selection.id, 1) == selection
    assert len(records(env.client, "rule_selection")) == 1
    assert records(env.client, "rule_selection")[0].id != selection.id


def test_exhausted_pre_model_error_finishes_failed_without_output(env, monkeypatch):
    env.rule()
    artifact = env.extract()[0]
    event = env.request(artifact, run=False)
    count = len(env.model.calls)

    def unavailable_snapshot():
        raise ConnectionError("synthetic transient snapshot error")

    monkeypatch.setattr(env.client, "snapshot", unavailable_snapshot)
    ExtractionService(env.client, env.model, max_attempts=1).drain()
    assert env.finish(event).outcome == "failed"
    assert env.client.get(artifact.id) == artifact
    assert len(env.model.calls) == count


def test_added_target_field_does_not_change_legacy_feedback_fingerprints():
    command = Command(kind="extract", interval=RANGE)
    assert "target_type" not in command.model_dump(mode="json")
    assert "target_type" not in command.model_dump_json()
    id = new_id()
    old = Command(kind="reevaluate", target_id=id)
    explicit = Command(kind="reevaluate", target_id=id, target_type="artifact")
    assert old.model_dump(mode="json") == explicit.model_dump(mode="json")
    assert "target_type" not in old.model_dump(mode="json")
    selection = Command(kind="reevaluate", target_id=id, target_type="rule_selection")
    assert selection.model_dump(mode="json")["target_type"] == "rule_selection"


def test_discovered_rule_roots_pass_real_gateway_preflight_and_audit(env, tmp_path):
    from contextlib import nullcontext

    import httpx
    from ear_extraction.model_io import SYSTEM
    from ear_llm_gateway import CallRequest, Gateway, GatewayClient, ProviderResult
    from ear_llm_gateway.journal import Journal

    rule = env.rule()
    artifact = env.extract()[0]
    edited = env.command(
        Command(
            kind="rule_update",
            target_id=rule.id,
            expected_version=1,
            rule=Rule(scope="A", instruction="new instruction", action="reminder"),
        )
    )
    event = env.request(artifact, run=False)

    class Provider:
        def complete(self, call, *, model, timeout):
            return ProviderResult(
                {
                    "body_md": "合成新正文",
                    "evidence_ids": [env.ids["transcript"]],
                    "action_reason": "synthetic no action",
                }
            )

    journal = Journal(str(tmp_path / "gateway.sqlite3"))
    seen = []
    writer = env.store.client("llm_gateway")
    engine = Gateway(
        callers=lambda _: nullcontext(env.client),
        writer=writer,
        provider=Provider(),
        journal=journal,
        aliases={"default": "synthetic/model"},
    )

    def respond(request):
        call = CallRequest.model_validate_json(request.content)
        seen.append(call)
        return httpx.Response(200, json=engine.complete("t" * 43, call))

    try:
        with GatewayClient(
            "https://synthetic.invalid",
            "t" * 43,
            system=SYSTEM,
            transport=httpx.MockTransport(respond),
        ) as gateway:
            ExtractionService(env.client, gateway).drain()
    finally:
        journal.close()
    finish = env.finish(event)
    assert finish.outcome == "changed" and len(seen) == 1
    call = seen[0]
    assert call.additional_causes == finish.additional_causes
    assert edited.payload["root_feedback_id"] in {
        t.root_feedback_id for t in call.additional_causes
    }
    audit = writer.get_system_record(call.call_id)
    assert audit is not None and audit.data_type == "llm_call"
    assert audit.payload["request_event_id"] == event.event_id
    assert edited.payload["root_feedback_id"] in {
        t["root_feedback_id"] for t in audit.payload["additional_causes"]
    }
