"""create_place against isolated authenticated PostgreSQL/HTTP, synthetic data only.

Uses the existing opt-in Docker fixture, never ear-dev or an external DSN. Registry
extensions are fixture-local; no production/core registry or API projection edits.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from ear_attribution import AttributionService, BusinessPlanner, CommitJournal
from ear_attribution.feedback import feedback_result_id
from ear_attribution.state import STATE_ID, read_fence
from ear_contracts import SemanticRecord, new_id
from ear_storage_client import (
    AppendRaw,
    ConflictError,
    Context,
    Create,
    MarkProcessed,
    PermissionDenied,
    Query,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown
from ear_storage_client.testing import make_raw

from .test_attribution_runtime import CONFIG, worker
from .test_attribution_runtime import attribution_runtime as attribution_runtime

pytestmark = [pytest.mark.postgres, pytest.mark.nats]


def gps(rt):
    raw = make_raw("gps_point", {"latitude": 0, "longitude": 0}, start="2026-11-05T09:00:00Z")
    return rt.clients["adapter"].write([AppendRaw(raw)], Context.system()).check().events[0]


def command(rt, ids):
    raw = make_raw(
        "feedback",
        {
            "module": "attribution",
            "command": {"kind": "create_place", "name": "Synthetic place", "gps_ids": ids},
        },
        start="2026-11-05T09:00:00Z",
    )
    return rt.clients["api"].write([AppendRaw(raw)], Context.feedback(raw.id)).check().events[0]


def receipt(rt, event):
    record = rt.clients["api"].get_system_record(feedback_result_id(event.data_id))
    assert record is not None
    return record.payload


def test_create_place_authorized_atomic_moves_and_unauthorized_batch_rejected(attribution_runtime):
    rt = attribution_runtime
    client = rt.clients["owner"]
    a, b, c = gps(rt), gps(rt), gps(rt)
    old = new_id()
    with client.snapshot() as reader:
        fence = read_fence(reader)
    fence.changed = True
    client.write(
        [
            fence.operation(),
            *fence.initial_ops,
            Create(
                "place",
                old,
                {
                    "name": "Synthetic old",
                    "references": [a.data_id, b.data_id],
                    "is_unknown": False,
                    "extension": {"keep": True},
                },
            ),
        ],
        Context.system(),
    ).check()
    service, journal = worker(rt)
    try:
        service.handle(a)
        service.handle(b)
        original = client.query(Query("attribution")).items
        assert len(original) == 2
        event = command(rt, [b.data_id, a.data_id, c.data_id])
        with client.snapshot() as reader:
            raw = reader.get(event.data_id)
            assert raw is not None
            ops = BusinessPlanner(CONFIG).feedback(reader, raw, event)
        assert ops is not None
        # Correct payload/observed versions cannot authorize an API principal to write ③.
        with pytest.raises(PermissionDenied):
            rt.clients["api"].write(
                [*ops, MarkProcessed("attribution", event.event_id)],
                Context.feedback(event.data_id),
            )
        assert rt.clients["api"].get_system_record(feedback_result_id(event.data_id)) is None
        assert client.count(Query("place")) == 2
        assert not client.is_processed("attribution", event.event_id)
        service.handle(event)
        p = receipt(rt, event)
        assert p["schema_version"] == 1 and p["status"] == "succeeded"
        assert p["subject_ids"] == [b.data_id, a.data_id, c.data_id]
        target = client.get(p["new_target_id"])
        assert target is not None and target.payload["references"] == p["subject_ids"]
        previous = client.get(old)
        assert previous is not None and previous.version == 2
        assert previous.payload["references"] == [] and previous.payload["extension"] == {
            "keep": True
        }
        for subject, id in zip(p["subject_ids"], p["attribution_ids"], strict=True):
            current = client.get(id)
            assert isinstance(current, SemanticRecord)
            assert current.payload["subject_id"] == subject and current.payload["user_protected"]
            assert current.payload["protection_feedback_id"] == event.data_id
            deps = client.dependencies_of(id)
            assert len(deps) == 1 and deps[0].depends_on.id == target.id
            assert set(deps[0].fields or ()) == {"references", "is_unknown"}
        assert not client.stale_dependencies([target.id])
        for record in original:
            assert client.get_version(record.id, record.version) == record
        service.handle(event)
        service.handle(c)  # delayed raw delivery cannot undo manual selection
        assert receipt(rt, event) == p and client.count(Query("place")) == 3
        conflict = command(rt, [a.data_id])
        service.handle(conflict)
        assert receipt(rt, conflict)["error_code"] == "protected_attribution_conflict"
        assert client.count(Query("place")) == 3
        state = client.get_system_record(STATE_ID)
        assert state is not None
        unknown = client.get(state.payload["unknown_place_id"])
        assert unknown is not None and unknown.payload["references"] == []
    finally:
        journal.close()


@pytest.mark.parametrize("committed", [False, True])
def test_create_place_unknown_commit_replays_exact_batch_before_replanning(
    attribution_runtime, committed
):
    rt = attribution_runtime
    a, b = gps(rt), gps(rt)
    event = command(rt, [a.data_id, b.data_id])
    service, journal = worker(rt)
    rt.fault.committed, rt.fault.armed = committed, True
    try:
        with pytest.raises(CommitUnknown):
            service.handle(event)
        assert journal.pending(event.event_id) is not None
        assert rt.clients["owner"].count(Query("place")) == (2 if committed else 0)
        assert rt.clients["owner"].count(Query("attribution")) == (2 if committed else 0)
    finally:
        journal.close()
    service, journal = worker(rt)
    try:

        def forbidden(reader, record, event):
            pytest.fail("must replay stored request identity/body before replanning")

        service.feedback = forbidden
        service.handle(event)
        assert len(rt.fault.bodies) == 2 and rt.fault.bodies[0] == rt.fault.bodies[1]
        assert journal.pending(event.event_id) is None
        p = receipt(rt, event)
        assert p["status"] == "succeeded" and len(p["attribution_ids"]) == 2
        assert rt.clients["owner"].count(Query("place")) == 2
        assert rt.clients["owner"].count(Query("attribution")) == 2
        service.handle(event)
        assert len(rt.fault.bodies) == 2
    finally:
        journal.close()


@pytest.mark.parametrize("same_feedback", [True, False])
def test_real_concurrent_create_initialization_no_orphans(
    attribution_runtime, tmp_path, same_feedback
):
    rt = attribution_runtime
    a = gps(rt)
    first = command(rt, [a.data_id])
    events = [first, first if same_feedback else command(rt, [a.data_id])]
    barrier = Barrier(2)
    calls = [0, 0]
    redeliveries = [0, 0]
    planner = BusinessPlanner(CONFIG)

    def run(index):
        with RemoteStorageClient(rt.caller.token, rt.url) as client:
            journal = CommitJournal(tmp_path / f"worker-{index}", namespace="synthetic-commands")
            try:

                def plan(reader, record, event):
                    ops = planner.feedback(reader, record, event)
                    calls[index] += 1
                    if calls[index] == 1:
                        barrier.wait(timeout=15)
                    return ops

                service = AttributionService(
                    client,
                    new_record=planner.new_record,
                    feedback=plan,
                    reevaluation=planner.reevaluation,
                    journal=journal,
                )
                try:
                    service.handle(events[index])
                except ConflictError as exc:
                    # HTTP redacts conflict object identity. Existing Transactions
                    # cannot classify this startup id_exists and deliberately fails
                    # closed; the bus NACK/redelivery boundary must retry it instead.
                    # Do not weaken transaction/journal safety for this command.
                    assert exc.conflict.reason == "id_exists"
                    assert exc.conflict.index == 0
                    assert journal.pending(events[index].event_id) is None
                    redeliveries[index] += 1
                    service.handle(events[index])
            finally:
                journal.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, (0, 1)))
    statuses = sorted(receipt(rt, e)["status"] for e in events)
    assert statuses == (["succeeded", "succeeded"] if same_feedback else ["rejected", "succeeded"])
    client = rt.clients["owner"]
    assert client.count(Query("place")) == 2 and client.count(Query("attribution")) == 1
    assert all(client.is_processed("attribution", e.event_id) for e in events)

    assert sum(redeliveries) <= 1
