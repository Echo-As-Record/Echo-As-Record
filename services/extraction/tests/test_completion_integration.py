"""Synthetic ④→⑦→① plus real SDK Finish on the in-memory store."""

import hashlib
import json
from contextlib import nullcontext

import httpx
import pytest
from ear_contracts import (
    Cause,
    DataChanged,
    LLMCallRecord,
    ReevaluationRequested,
    TracedCause,
    is_id,
)
from ear_extraction.demo import DemoGateway, fixture, submit
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import COMPLETION_CONSUMER, CONSUMER, ExtractionService
from ear_extraction.persistence import records, semantic
from ear_llm_gateway import CallRequest, Gateway, GatewayClient, ProviderResult
from ear_llm_gateway.journal import Journal
from ear_storage_client import (
    AppendRaw,
    Context,
    MarkProcessed,
    PublishReevaluation,
    PutSystemRecord,
    Update,
)
from ear_storage_client.testing import make_raw, ts

RANGE = Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))


def prepare():
    storage, ids = fixture()
    client = storage.client("extraction")
    service = ExtractionService(client, DemoGateway())
    service.subscribe()
    submit(storage, Command(kind="rule_create", rule=Rule(scope="组会", instruction="整理待办")))
    service.drain()
    submit(storage, Command(kind="extract", interval=RANGE))
    service.drain()
    assert len(records(client, "activity")) == 1
    artifact = records(client, "artifact")[0]
    assert artifact.version == 1
    return storage, ids, artifact, service


def publish(storage, ids, artifact) -> ReevaluationRequested:
    api = storage.client("api")
    roots = [
        make_raw("feedback", {"module": "attribution", "test": i}, start="2026-11-05T10:00:00Z")
        for i in range(3)
    ]
    for feedback in roots:
        api.write([AppendRaw(feedback)], Context.feedback(feedback.id)).check()
    attribution = storage.client("attribution")
    attr = semantic(attribution, ids["attribution"], "attribution")
    person = semantic(attribution, ids["person"], "person")
    attribution.write(
        [
            Update("attribution", attr.id, 1, {**attr.payload, "test": "updated"}),
            Update("person", person.id, 1, {**person.payload, "name": "合成姓名"}),
        ],
        Context.feedback(roots[0].id),
    ).check()
    cause = Cause(data_type="attribution", data_id=attr.id, old_version=1, new_version=2)
    traces = (
        TracedCause(cause=cause, root_feedback_id=roots[1].id, depth=5),
        TracedCause(
            cause=Cause(data_type="person", data_id=person.id, old_version=1, new_version=2),
            root_feedback_id=roots[2].id,
            depth=2,
        ),
    )
    result = (
        storage.client("dependency")
        .write(
            [PublishReevaluation("artifact", artifact.id, cause, "合成多因重判")],
            Context(roots[0].id, 3, additional_causes=traces),
        )
        .check()
    )
    event = result.events[0]
    assert isinstance(event, ReevaluationRequested)
    return event


@pytest.mark.parametrize("changed", [False, True])
def test_finish_uses_real_request_id_and_context_on_gateway_and_storage(tmp_path, changed):
    storage, ids, artifact, first = prepare()
    client = first.storage
    event = publish(storage, ids, artifact)
    storage.run_until_idle()  # Duplicate delivery is enabled in the fixture.
    runs = [r for r in first.runs() if r.payload["event_id"] == event.event_id]
    assert len(runs) == 1
    admitted = runs[0]
    assert admitted.id != event.event_id and is_id(admitted.id)
    assert admitted.payload["request_event_id"] == event.event_id
    assert client.get_system_record(event.event_id) is None
    assert client.is_processed(CONSUMER, event.event_id)
    assert not client.is_processed(COMPLETION_CONSUMER, event.event_id)
    assert client.get_reevaluation_result(event.event_id) is None
    assert client.stale_dependencies([ids["attribution"]])

    class Provider:
        def complete(self, call, *, model, timeout):
            return ProviderResult(
                {"body_md": "合成新正文", "evidence_ids": [ids["transcript"]]}
                if changed
                else {"unchanged": True}
            )

    seen: list[CallRequest] = []
    writer = storage.client("llm_gateway")
    journal = Journal(str(tmp_path / "gateway.sqlite3"))
    gateway = Gateway(
        callers=lambda _: nullcontext(client),
        writer=writer,
        provider=Provider(),
        journal=journal,
        aliases={"default": "synthetic/model"},
    )

    def respond(request: httpx.Request):
        assert request.url.path == "/v1/complete"
        call = CallRequest.model_validate_json(request.content)
        seen.append(call)
        current = client.get_system_record(admitted.id)
        assert current is not None
        intent = current.payload["gateway_calls"][-1]
        assert intent["state"] == "sending" and intent["call_id"] == call.call_id
        assert (
            intent["request_fingerprint"]
            == hashlib.sha256(
                json.dumps(
                    call.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
        )
        assert "data" not in intent and "system" not in intent
        return httpx.Response(200, json=gateway.complete("t" * 43, call))

    before_events = len(storage.events)
    try:
        with GatewayClient(
            "https://synthetic.invalid",
            "t" * 43,
            system=SYSTEM,
            transport=httpx.MockTransport(respond),
        ) as http:
            # A different service instance consumes the persisted context.
            service = ExtractionService(client, http)
            service.drain()
            service.handle_event(event)
            service.drain()
    finally:
        journal.close()
    assert len(seen) == 1
    call = seen[0]
    assert call.request_event_id == event.event_id and call.run_id == admitted.id
    assert call.root_feedback_id == event.root_feedback_id and call.depth == event.depth == 3
    assert call.additional_causes == event.additional_causes
    audit_record = writer.get_system_record(call.call_id)
    assert audit_record is not None and audit_record.data_type == "llm_call"
    audit = LLMCallRecord.model_validate(audit_record.payload)
    assert audit.request_event_id == event.event_id and audit.depth == event.depth
    assert set(t.root_feedback_id for t in audit.additional_causes) == {
        event.root_feedback_id,
        *(t.root_feedback_id for t in event.additional_causes),
    }
    finish = client.get_reevaluation_result(event.event_id)
    assert finish is not None
    assert finish.additional_causes == event.additional_causes
    assert finish.request_event_id == event.event_id
    assert finish.root_feedback_id == event.root_feedback_id and finish.depth == event.depth
    assert (finish.before_version, finish.after_version) == (1, 2 if changed else 1)
    assert finish.outcome == ("changed" if changed else "unchanged")
    result = client.get_system_record(event.event_id)
    assert result is not None and result.data_type == "extraction_completion"
    assert client.is_processed(COMPLETION_CONSUMER, event.event_id)
    assert not client.stale_dependencies([ids["attribution"]])
    final = client.get_system_record(admitted.id)
    assert final is not None and final.payload["status"] == "succeeded"
    assert final.payload["gateway_calls"][0]["state"] == "returned"
    assert len(storage.events) == before_events + int(changed)
    if changed:
        update = storage.events[-1]
        assert isinstance(update, DataChanged)
        assert update.request_event_id == event.event_id
        assert update.additional_causes == event.additional_causes
        assert update.depth == event.depth


@pytest.mark.parametrize("redeliver_first", [False, True])
def test_legacy_request_id_collision_is_not_requeued(redeliver_first):
    storage, ids, artifact, service = prepare()
    client = service.storage
    event = publish(storage, ids, artifact)
    client.write(
        [
            PutSystemRecord(
                "extraction_run",
                event.event_id,
                {
                    "command": Command(kind="reevaluate", target_id=artifact.id).model_dump(
                        mode="json"
                    ),
                    "status": "queued",
                    "attempts": 0,
                    "scheduled": False,
                    "root_feedback_id": event.root_feedback_id,
                    "depth": event.depth,
                    "created_at": "2026-11-05T10:00:00Z",
                    "event_id": event.event_id,
                    "model_calls": [],
                },
            ),
            MarkProcessed(CONSUMER, event.event_id),
        ],
        Context.of(event),
    ).check()
    if redeliver_first:
        storage.run_until_idle()
    service.drain()
    storage.run_until_idle()
    legacy = client.get_system_record(event.event_id)
    assert legacy is not None and legacy.data_type == "extraction_run"
    assert legacy.payload["status"] == "blocked"
    assert legacy.payload["error"] == "legacy_completion_id_collision"
    assert "request_event_id" not in legacy.payload  # Old payload is not reinterpreted.
    assert legacy.payload["attempts"] == 0 and legacy.payload["model_calls"] == []
    assert len([r for r in service.runs() if r.payload["event_id"] == event.event_id]) == 1
    assert client.get_reevaluation_result(event.event_id) is None
    assert not client.is_processed(COMPLETION_CONSUMER, event.event_id)
    with pytest.raises(ValueError, match="legacy_completion_id_collision"):
        service.retry(legacy.id)


def test_feedback_cannot_impersonate_a_reevaluation_request():
    storage, _, artifact, service = prepare()
    id = submit(storage, Command(kind="reevaluate", target_id=artifact.id))
    service.drain()
    run = service.storage.get_system_record(id)
    assert run is not None
    assert run.payload["status"] == "failed"
    assert run.payload["error"] == "reevaluation_request_required"
    assert run.payload["gateway_calls"] == []
    with pytest.raises(ValueError, match="reevaluation_request_required"):
        service.retry(id)


def test_unknown_rejudge_leaves_admission_only_and_old_artifact():
    storage, ids, artifact, first = prepare()
    client = first.storage
    event = publish(storage, ids, artifact)
    storage.run_until_idle()

    class Unknown:
        count = 0

        def complete(self, call):
            self.count += 1
            raise TimeoutError()

    gateway = Unknown()
    service = ExtractionService(client, gateway)
    service.drain()
    admitted = next(r for r in service.runs() if r.payload["event_id"] == event.event_id)
    assert admitted.payload["status"] == "blocked"
    assert admitted.payload["error"] == "gateway_outcome_unknown"
    assert client.is_processed(CONSUMER, event.event_id)
    assert not client.is_processed(COMPLETION_CONSUMER, event.event_id)
    assert client.get_reevaluation_result(event.event_id) is None
    assert semantic(client, artifact.id, "artifact").version == 1
    assert client.stale_dependencies([ids["attribution"]])
    service.handle_event(event)
    service.drain()
    with pytest.raises(ValueError, match="gateway_outcome_unknown"):
        service.retry(admitted.id)
    assert gateway.count == 1


@pytest.mark.parametrize("status", ["failed", "succeeded", "blocked", "superseded"])
def test_terminal_legacy_request_id_is_never_rewritten_or_rekeyed(status):
    storage, ids, artifact, service = prepare()
    event = publish(storage, ids, artifact)
    service.storage.write(
        [
            PutSystemRecord(
                "extraction_run",
                event.event_id,
                {
                    "command": Command(kind="reevaluate", target_id=artifact.id).model_dump(
                        mode="json"
                    ),
                    "status": status,
                    "attempts": 1,
                    "scheduled": False,
                    "root_feedback_id": event.root_feedback_id,
                    "depth": event.depth,
                    "created_at": "2026-11-05T10:00:00Z",
                    "event_id": event.event_id,
                    "model_calls": [{"purpose": "legacy"}],
                },
            ),
            MarkProcessed(CONSUMER, event.event_id),
        ],
        Context.of(event),
    ).check()
    before = service.storage.get_system_record(event.event_id)
    service.handle_event(event)
    service.drain()
    assert service.storage.get_system_record(event.event_id) == before
    assert len([r for r in service.runs() if r.payload["event_id"] == event.event_id]) == 1
    assert service.storage.get_reevaluation_result(event.event_id) is None
    assert not service.storage.is_processed(COMPLETION_CONSUMER, event.event_id)
