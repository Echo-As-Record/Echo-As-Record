"""Synthetic integration only: actual SDK, actual adapter conversion, no live services."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast

import httpx
import pytest
from ear_adapter.config import AdapterConfig
from ear_adapter.convert import capture_records
from ear_adapter.ear_api import CaptureEvidence, Embedding
from ear_adapter.testing import AUDIO_FILE, embedding_json, evidence_json
from ear_api.models import FeedbackInput
from ear_attribution import AttributionService, BusinessPlanner, CommitJournal
from ear_attribution.feedback import feedback_result_id
from ear_attribution.state import STATE_ID
from ear_contracts import (
    Cause,
    ReevaluationRequested,
    TracedCause,
    new_id,
)
from ear_extraction.domain import Interval
from ear_extraction.segmentation import inputs
from ear_storage_client import (
    AppendRaw,
    Context,
    InvalidRequest,
    MarkProcessed,
    PublishReevaluation,
    Query,
    RegisterDependencies,
    StorageClient,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown
from ear_storage_client.testing import ts
from ear_storage_client.wire import decode_request, encode_result

from .support import CONFIG, append, attr, feedback, present, request, setup, voice


def test_actual_adapter_conversion_to_extraction_read_and_legacy_compatibility():
    storage, client, service = setup()
    service.subscribe()
    raw = capture_records(
        CaptureEvidence.model_validate(evidence_json()),
        audio_file=AUDIO_FILE,
        embeddings={
            r: Embedding.model_validate(embedding_json(r)) for r in ("run-1-res-1", "run-1-res-3")
        },
        config=AdapterConfig(),
    )
    storage.client("adapter").write([AppendRaw(r) for r in raw], Context.system()).check()
    storage.run_until_idle()
    assert not storage.dead_letters
    assert client.count(Query("voiceprint")) == 2
    assert client.count(Query("local_speaker")) == 3  # one speaker has no embedding, intentionally
    assert client.count(Query("person")) == 1
    assert client.count(Query("attribution")) == 3
    interval = Interval(start=ts("2026-10-26T00:00:00Z"), end=ts("2026-10-27T00:00:00Z"))
    evidence = inputs(client, interval)
    assert len(evidence.attributions) == 3
    assert {e.data_type for e in evidence.entities} == {"person", "place"}
    for a in evidence.attributions:
        legacy = {k: a.payload[k] for k in ("subject_id", "target_type", "target_id")}
        client.write(
            [
                Update("attribution", a.id, a.version, legacy),
                RegisterDependencies(
                    "attribution", a.id, [d.depends_on for d in client.dependencies_of(a.id)]
                ),
            ],
            Context.system(),
        ).check()
    assert len(inputs(client, interval).attributions) == 3


def test_actual_api_normalized_feedback_with_null_fields():
    storage, client, service = setup()
    e = append(storage)
    service.handle(e)
    a = attr(client, e)
    command = FeedbackInput(
        request_id=new_id(),
        module="attribution",
        command={
            "kind": "confirm_attribution",
            "subject_id": e.data_id,
            "target_id": a.payload["target_id"],
        },
    ).normalized_command()
    assert command["expected_version"] is None and command["name"] is None
    event = append(
        storage,
        "feedback",
        {"module": "attribution", "command": command, "integration": "pending_contract"},
    )
    service.handle(event)
    assert (
        present(client.get_system_record(feedback_result_id(event.data_id))).payload["status"]
        == "succeeded"
    )


def test_two_workers_initializing_reread_conflict_without_orphans():
    storage, client, _ = setup()
    events = [append(storage), append(storage)]
    barrier = Barrier(2)
    planner = BusinessPlanner(CONFIG)
    calls = [0, 0]

    def worker(index):
        def plan(reader, record, event):
            ops = planner.new_record(reader, record, event)
            calls[index] += 1
            if calls[index] == 1:
                barrier.wait(timeout=5)
            return ops

        service = AttributionService(
            client, new_record=plan, feedback=planner.feedback, reevaluation=planner.reevaluation
        )
        service.handle(events[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, (0, 1)))
    assert sorted(calls) == [1, 2]
    assert client.count(Query("person")) == 1
    assert client.count(Query("place")) == 1
    assert client.count(Query("attribution")) == 2
    assert present(client.get_system_record(STATE_ID)).payload["next_person_number"] == 2


def test_same_feedback_concurrent_workers_use_receipt_not_id_exists_bypass():
    storage, client, service = setup()
    e = append(storage)
    service.handle(e)
    a = attr(client, e)
    event = append(
        storage,
        "feedback",
        {
            "module": "attribution",
            "command": {
                "kind": "rename",
                "target_id": a.payload["target_id"],
                "name": "Synthetic name",
            },
        },
    )
    barrier = Barrier(2)
    planner = BusinessPlanner(CONFIG)
    calls = [0, 0]

    def worker(index):
        def plan(reader, record, event):
            ops = planner.feedback(reader, record, event)
            calls[index] += 1
            if calls[index] == 1:
                barrier.wait(timeout=5)
            return ops

        runtime = AttributionService(
            client, new_record=planner.new_record, feedback=plan, reevaluation=planner.reevaluation
        )
        runtime.handle(event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, (0, 1)))
    assert present(client.get(a.payload["target_id"])).version == 2
    assert client.count(Query("attribution_feedback_result")) == 1
    assert client.is_processed("attribution", event.event_id)


def test_stale_candidate_reference_plan_rejected_and_new_plan_recomputes():
    storage, client, service = setup()
    x, y, z = append(storage), append(storage, payload=voice((0, 1))), append(storage)
    service.handle(x)
    service.handle(y)
    a, b = attr(client, x), attr(client, y)
    planner = BusinessPlanner(CONFIG)
    with client.snapshot() as reader:
        stale = planner.new_record(reader, present(reader.get(z.data_id)), z)
    feedback(
        storage,
        service,
        {
            "kind": "correct_attribution",
            "subject_id": x.data_id,
            "previous_target_id": a.payload["target_id"],
            "new_target_id": b.payload["target_id"],
        },
    )
    result = client.write(stale, Context.system())
    assert present(result.conflict).reason == "revision_mismatch"
    assert client.count(Query("attribution")) == 2
    service.handle(z)
    assert attr(client, z).payload["target_id"] == b.payload["target_id"]


def test_multicause_finish_retains_all_roots_and_request():
    storage, client, service = setup()
    x, y = append(storage), append(storage, payload=voice((0, 1)))
    service.handle(x)
    service.handle(y)
    a, b = attr(client, x), attr(client, y)
    cause = Cause(
        data_type="person", data_id=b.payload["target_id"], old_version=None, new_version=1
    )
    extra = TracedCause(cause=cause, root_feedback_id=new_id(), depth=2)
    context = Context(new_id(), 2, additional_causes=(extra,))
    event = (
        storage.client("dependency")
        .write(
            [
                PublishReevaluation(
                    "attribution",
                    a.id,
                    Cause(
                        data_type="person",
                        data_id=a.payload["target_id"],
                        old_version=None,
                        new_version=1,
                    ),
                    "synthetic multiple causes",
                    additional_causes=(extra,),
                )
            ],
            context,
        )
        .check()
        .events[0]
    )
    assert isinstance(event, ReevaluationRequested)
    service.handle(event)
    result = present(client.get_reevaluation_result(event.event_id))
    assert result.additional_causes == (extra,)
    assert (result.root_feedback_id, result.depth) == (context.root_feedback_id, 2)
    assert result.request_event_id == event.event_id


@pytest.mark.parametrize("omit", ["marker", "dependencies", "persisted_request"])
def test_finish_rejects_incomplete_or_forged_batches_atomically(omit):
    storage, client, service = setup()
    e = append(storage)
    service.handle(e)
    a = attr(client, e)
    event = request(storage, a)
    assert isinstance(event, ReevaluationRequested)
    if omit == "persisted_request":
        event = event.model_copy(update={"event_id": new_id()})
    planner = BusinessPlanner(CONFIG)
    with client.snapshot() as reader:
        ops = planner.reevaluation(reader, a, event)
    if omit == "dependencies":
        ops = [op for op in ops if not isinstance(op, RegisterDependencies)]
    if omit != "marker":
        ops.append(MarkProcessed("attribution", event.event_id))
    with pytest.raises(InvalidRequest):
        client.write(ops, Context.of(event))
    assert client.get_reevaluation_result(event.event_id) is None
    assert not client.is_processed("attribution", event.event_id)


def test_framework_rejects_marker_only_reevaluation():
    storage, client, service = setup()
    e = append(storage)
    service.handle(e)
    r = request(storage, attr(client, e))
    planner = BusinessPlanner(CONFIG)
    runtime = AttributionService(
        client,
        new_record=planner.new_record,
        feedback=planner.feedback,
        reevaluation=lambda reader, record, event: [],
    )
    with pytest.raises(ValueError, match="FinishReevaluation"):
        runtime.handle(r)
    assert not client.is_processed("attribution", r.event_id)


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_commit_restart_replays_identical_wire_before_any_replan(tmp_path, committed):
    storage, client, _ = setup()
    event = append(storage)
    requests = []
    receipts = {}

    def transport(req: httpx.Request):
        assert req.url.path == "/v1/batches"
        requests.append(req.content)
        data = json.loads(req.content)
        rid = data.pop("request_id")
        if rid not in receipts:
            if len(requests) == 1 and not committed:
                raise httpx.ReadTimeout("synthetic loss before commit")
            ops, context = decode_request(json.dumps(data))
            receipts[rid] = encode_result(client.write(ops, context))
        if len(requests) == 1:
            raise httpx.ReadTimeout("synthetic loss after commit")
        return httpx.Response(200, text=receipts[rid])

    remote = RemoteStorageClient(
        "T" * 43, "http://synthetic.invalid", transport=httpx.MockTransport(transport)
    )

    class Client:
        # Real MemoryReader for snapshots, real RemoteStorageClient for batch wire/retries.
        def __getattr__(self, name):
            return getattr(client, name)

        def write(self, ops, context, *, request_id):
            return remote.write(ops, context, request_id=request_id)

    journal = CommitJournal(tmp_path / "journal", namespace="synthetic-instance")
    runtime = AttributionService.business(
        cast(StorageClient, Client()), config=CONFIG, journal=journal
    )
    with pytest.raises(CommitUnknown):
        runtime.handle(event)
    assert journal.pending(event.event_id) is not None
    assert client.count(Query("person")) == int(committed)
    journal.close()
    journal = CommitJournal(tmp_path / "journal", namespace="synthetic-instance")
    runtime = AttributionService.business(
        cast(StorageClient, Client()), config=CONFIG, journal=journal
    )

    def forbidden(reader, record, event):
        pytest.fail("unknown commit must replay before inspecting current state/replanning")

    runtime.new_record = forbidden
    runtime.handle(event)
    assert len(requests) == 2 and requests[0] == requests[1]
    assert client.count(Query("person")) == 1
    assert client.count(Query("attribution")) == 1
    assert journal.pending(event.event_id) is None
    journal.close()
    remote.close()


def test_journal_private_exclusive_and_deployment_bound(tmp_path):
    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="one")
    with pytest.raises(BlockingIOError):
        CommitJournal(directory, namespace="one")
    journal.close()
    with pytest.raises(ValueError, match="namespace"):
        CommitJournal(directory, namespace="two")
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        CommitJournal(directory, namespace="one")
