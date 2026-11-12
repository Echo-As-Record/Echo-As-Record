"""First cross-module loop, NOT the eight-step MVP acceptance.

Synthetic Evidence -> actual adapter/attribution/extraction/dependency -> Finish.
Platform: MemoryStorage. Model: deterministic fake, NOT gateway HTTP or real LLM.
Feedback uses the API input model, NOT the HTTP authentication/status endpoints.
No external actions run. Includes independent v1.2 rule selection; no deployment.
"""

from dataclasses import replace
from typing import Literal

import pytest
from ear_adapter import AdapterConfig, AlreadyIngested, Ingested, Runner
from ear_adapter.ear_api import Embedding
from ear_adapter.testing import FakeLocator, FakeSource, embedding_json, evidence_json
from ear_api.models import FeedbackInput
from ear_attribution import AttributionService, MatchConfig
from ear_attribution.feedback import feedback_result_id
from ear_contracts import (
    DEFAULT_REGISTRY,
    DataTypeSpec,
    ReevaluationRequested,
    Registry,
    SemanticRecord,
    new_id,
)
from ear_dependency import DependencyService
from ear_dependency.domain import Step
from ear_extraction.demo import DemoGateway
from ear_extraction.domain import Call
from ear_extraction.orchestration import ExtractionService
from ear_storage_client import AppendRaw, Context, MemoryStorage, Query
from ear_storage_client.testing import make_raw


class SelectedSource(FakeSource):
    def list_captures(self):
        raise AssertionError("explicit sample must never enumerate captures")

    def read_embedding(self, resolution_id: str) -> Embedding:
        self.embedding_reads += 1
        data = embedding_json(resolution_id)
        data["values"] = [1.0, 0.0, 0.0] if resolution_id.endswith("-1") else [0.0, 1.0, 0.0]
        return Embedding.model_validate(data)


class SyntheticModel(DemoGateway):
    def __init__(self):
        self.calls: list[Call] = []
        self.subject: str | None = None

    def complete(self, call: Call) -> dict:
        self.calls.append(call)
        if call.purpose not in {"generation", "reevaluation"}:
            return super().complete(call)
        selected = next(
            r for r in call.data["attributions"] if r["payload"]["subject_id"] == self.subject
        )
        instruction = call.data["rule"]["payload"]["instruction"]
        body = (
            "Synthetic unchanged note."
            if instruction == "fixed"
            else "Synthetic owner: " + selected["payload"]["target_id"]
        )
        if call.purpose == "reevaluation" and body == call.data["old_artifact"]["body_md"]:
            return {"unchanged": True}
        result = {"body_md": body, "evidence_ids": [call.data["evidence"][0]["id"]]}
        if instruction != "fixed":
            result["action"] = {"kind": "reminder", "title": body, "due_at": "2026-11-06T09:00:00Z"}
        return result


def capable_registry() -> Registry:
    specs = dict(DEFAULT_REGISTRY.data_types)
    for name in ("attribution", "artifact"):
        specs[name] = replace(specs[name], requires_dependencies=True, supports_reevaluation=True)
    return Registry(
        specs, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
    ).extend_core(
        DataTypeSpec("attribution_state", "system_record", "attribution"),
        DataTypeSpec("attribution_feedback_result", "system_record", "attribution"),
        DataTypeSpec(
            "rule_selection",
            "semantic",
            "extraction",
            unique_fields=("activity_id",),
            requires_dependencies=True,
            supports_reevaluation=True,
        ),
    )


def feedback(
    storage: MemoryStorage,
    module: Literal["extraction", "attribution", "dependency"],
    command: dict,
) -> str:
    normalized = FeedbackInput(
        request_id=new_id(), module=module, command=command
    ).normalized_command()
    record = make_raw(
        "feedback",
        {"module": module, "command": normalized},
        start="2026-11-05T10:00:00Z",
        channel="api",
    )
    storage.client("api").write([AppendRaw(record)], Context.feedback(record.id)).check()
    return record.id


def rows(storage: MemoryStorage, kind: str) -> list[SemanticRecord]:
    result = storage.client("api").query(Query(kind)).items
    assert all(isinstance(r, SemanticRecord) for r in result)
    return [r for r in result if isinstance(r, SemanticRecord)]


@pytest.mark.parametrize("rebuild_scheduler", [False, True])
def test_import_correction_and_real_owners_finish_in_memory(rebuild_scheduler):
    storage = MemoryStorage(capable_registry(), duplicate_delivery=True)
    attribution = AttributionService.business(
        storage.client("attribution"), config=MatchConfig(cosine_threshold=0.9, gps_threshold_m=100)
    )
    attribution.subscribe()
    source = SelectedSource({"cap-1": evidence_json()})

    def importer():
        return Runner(
            source=source,
            storage=storage.client("adapter"),
            locator=FakeLocator(),
            config=AdapterConfig(),
        )

    assert isinstance(importer().run_once(capture_ids=["cap-1"]).outcomes[0], Ingested)
    storage.run_until_idle()
    assert not storage.dead_letters
    assert len(rows(storage, "person")) == 2
    assert len(rows(storage, "place")) == 1
    assert len(rows(storage, "attribution")) == 3
    event_count = len(storage.events)
    assert isinstance(importer().run_once(capture_ids=["cap-1"]).outcomes[0], AlreadyIngested)
    assert len(storage.events) == event_count

    attrs = [a for a in rows(storage, "attribution") if a.payload["target_type"] == "person"]
    original = attrs[0]
    target = next(a.payload["target_id"] for a in attrs if a.id != original.id)
    assert target != original.payload["target_id"]
    model = SyntheticModel()
    model.subject = original.payload["subject_id"]
    extraction = ExtractionService(storage.client("extraction"), model)
    extraction.subscribe()
    for instruction in ("owner", "fixed"):
        feedback(
            storage,
            "extraction",
            {
                "kind": "rule_create",
                "rule": {
                    "scope": "synthetic meeting",
                    "instruction": instruction,
                    "action": "reminder" if instruction == "owner" else None,
                },
            },
        )
        storage.run_until_idle()
        extraction.drain()
    feedback(
        storage,
        "extraction",
        {
            "kind": "extract",
            "interval": {
                "start": "2026-10-26T00:00:00Z",
                "end": "2026-10-27T00:00:00Z",
            },
        },
    )
    storage.run_until_idle()
    extraction.drain()
    storage.run_until_idle()
    assert all(r.payload["status"] == "succeeded" for r in extraction.runs())
    [activity] = rows(storage, "activity")
    before = {r.id: r for r in rows(storage, "artifact")}
    assert len(before) == 2
    [selection] = rows(storage, "rule_selection")
    assert set(selection.payload["rule_ids"]) == {r.payload["rule_id"] for r in before.values()}
    [old_suggestion] = rows(storage, "action_suggestion")

    scheduler = DependencyService(storage.client("dependency"), full_visibility=True)
    handled_by: list[DependencyService] = []

    def dispatch(event):
        handled_by.append(scheduler)
        scheduler.handle(event)

    storage.client("dependency").subscribe("dependency.changes", dispatch)
    root = feedback(
        storage,
        "attribution",
        {
            "kind": "correct_attribution",
            "subject_id": original.payload["subject_id"],
            "previous_target_id": original.payload["target_id"],
            "new_target_id": target,
            "expected_version": original.version,
        },
    )
    storage.run_until_idle()
    receipt = storage.client("api").get_system_record(feedback_result_id(root))
    assert receipt is not None and receipt.payload["status"] == "succeeded"
    corrected = storage.client("api").get(original.id)
    assert corrected is not None and corrected.payload["user_protected"] is True
    assert corrected.payload["target_id"] == target
    scheduler.tick()
    previous_scheduler = scheduler
    before_rebuild = len(handled_by)
    if rebuild_scheduler:
        # Only the scheduler object is reconstructed; the memory store survives.
        scheduler = DependencyService(storage.client("dependency"), full_visibility=True)
    for _ in range(10):
        storage.run_until_idle()
        extraction.drain()
        storage.run_until_idle()
        scheduler.tick()
    assert not storage.dead_letters
    if rebuild_scheduler:
        assert scheduler is not previous_scheduler
        assert handled_by[before_rebuild:]
        assert all(instance is scheduler for instance in handled_by[before_rebuild:])
    assert rows(storage, "activity") == [activity]
    after = {r.id: r for r in rows(storage, "artifact")}
    for ident, old in before.items():
        if old.payload["body_md"] == "Synthetic unchanged note.":
            assert after[ident].version == old.version
            assert after[ident].payload["body_md"] == old.payload["body_md"]
        else:
            assert after[ident].version == old.version + 1
            assert after[ident].payload["body_md"] == "Synthetic owner: " + target
        inspector = storage.client("dependency")
        deps = inspector.dependencies_of(ident)
        assert deps
        stale = inspector.stale_dependencies([d.depends_on.id for d in deps])
        assert not any(d.dependent.id == ident for d in stale)
    retired = storage.client("api").get(old_suggestion.id)
    assert isinstance(retired, SemanticRecord) and retired.superseded
    [new_suggestion] = rows(storage, "action_suggestion")
    assert new_suggestion.id != old_suggestion.id
    assert new_suggestion.payload["state"] == "pending"  # Never executed.
    requests = [e for e in storage.events if isinstance(e, ReevaluationRequested)]
    expected_targets = {**{ident: "artifact" for ident in before}, selection.id: "rule_selection"}
    assert {r.target_id: r.target_type for r in requests} == expected_targets
    assert len(requests) == len(expected_targets)
    outcomes = set()
    for request in requests:
        result = storage.client("dependency").get_reevaluation_result(request.event_id)
        assert result is not None
        outcomes.add(result.outcome)
        assert result.root_feedback_id == root
        assert result.request_event_id == request.event_id
    assert outcomes == {"changed", "unchanged"}
    calls = [c for c in model.calls if c.request_event_id is not None]
    assert len(calls) == len(expected_targets)
    assert len({c.call_id for c in calls}) == len(calls)
    purposes = {c.request_event_id: c.purpose for c in calls}
    assert all(
        purposes[r.event_id]
        == ("selection" if r.target_type == "rule_selection" else "reevaluation")
        for r in requests
    )
    assert {c.request_event_id for c in calls} == {e.event_id for e in requests}
    assert all(c.root_feedback_id == root and c.depth > 0 for c in calls)
    steps = [r for r in scheduler.records() if isinstance(r, Step)]
    assert len(steps) == len(expected_targets)
    assert {r.request_event_id for r in steps} == {e.event_id for e in requests}
    assert {r.target_id for r in steps} == set(expected_targets)
    assert all(r.state == "done" for r in steps)
    count = len(model.calls)
    for request in requests:
        extraction.handle_event(request)
    extraction.drain()
    scheduler.tick()
    assert len(model.calls) == count
