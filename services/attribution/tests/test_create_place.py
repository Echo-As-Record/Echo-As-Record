"""Synthetic bounded commands: atomicity, protection, CAS and additive compatibility."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from ear_api.models import FeedbackInput
from ear_attribution import AttributionService, BusinessPlanner
from ear_attribution.feedback import feedback_result_id
from ear_attribution.state import STATE_ID, read_fence
from ear_contracts import DataChanged, SemanticRecord, new_id
from ear_storage_client import (
    Context,
    Create,
    InvalidRequest,
    MarkProcessed,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    Update,
)

from .support import CONFIG, append, attr, feedback, present, request, setup


def receipt(client, event):
    return present(client.get_system_record(feedback_result_id(event.data_id))).payload


def command_event(storage, ids, **extra):
    return append(
        storage,
        "feedback",
        {
            "module": "attribution",
            "command": {"kind": "create_place", "name": "Synthetic place", "gps_ids": ids, **extra},
        },
    )


def plan(client, event):
    with client.snapshot() as reader:
        return present(
            BusinessPlanner(CONFIG).feedback(reader, present(reader.get(event.data_id)), event)
        )


def seed_place(client, refs, **extra):
    with client.snapshot() as reader:
        fence = read_fence(reader)
    fence.changed = True
    id = new_id()
    client.write(
        [
            fence.operation(),
            *fence.initial_ops,
            Create(
                "place",
                id,
                {"name": "Synthetic old", "references": refs, "is_unknown": False, **extra},
            ),
        ],
        Context.system(),
    ).check()
    return id


def edit_attribution(client, record, payload):
    client.write(
        [
            Update("attribution", record.id, record.version, payload),
            RegisterDependencies(
                "attribution", record.id, [d.depends_on for d in client.dependencies_of(record.id)]
            ),
        ],
        Context.system(),
    ).check()


def test_create_before_raw_delivery_normalized_api_shape_and_repeat():
    storage, client, service = setup()
    a, b = append(storage, "gps_point"), append(storage, "gps_point")
    command = FeedbackInput(
        request_id=new_id(),
        module="attribution",
        command={
            "kind": "create_place",
            "name": " Synthetic place ",
            "gps_ids": [b.data_id, a.data_id],
        },
    ).normalized_command()
    event = feedback(storage, service, command)
    p = receipt(client, event)
    assert p["schema_version"] == 1 and p["status"] == "succeeded" and p["error_code"] is None
    assert set(p) == {
        "schema_version",
        "feedback_id",
        "command_kind",
        "status",
        "error_code",
        "completed_at",
        "new_target_id",
        "subject_ids",
        "attribution_ids",
    }
    assert p["subject_ids"] == [b.data_id, a.data_id]
    target = present(client.get(p["new_target_id"]))
    assert target.payload == {
        "name": " Synthetic place ",
        "references": p["subject_ids"],
        "is_unknown": False,
    }
    assert p["attribution_ids"] == [attr(client, b).id, attr(client, a).id]
    for e in (b, a):
        current = attr(client, e)
        assert current.version == 1 and current.payload["user_protected"] is True
        assert current.payload["protection_feedback_id"] == event.data_id
        deps = client.dependencies_of(current.id)
        assert len(deps) == 1
        assert deps[0].depends_on.id == target.id and deps[0].depends_on.version == 1
        assert set(deps[0].fields or ()) == {"references", "is_unknown"}
    state = present(client.get_system_record(STATE_ID))
    assert state.payload["reference_revision"] == 1 and state.payload["next_person_number"] == 1
    unknown = present(client.get(state.payload["unknown_place_id"]))
    assert unknown.payload["references"] == [] and unknown.payload["is_unknown"] is True
    before = len(storage.events)
    for e in (event, a, b):
        service.handle(e)
    # Same feedback, different delivery ID must also reuse its durable receipt.
    service.handle(event.model_copy(update={"event_id": new_id()}))
    assert len(storage.events) == before
    assert client.count(Query("place")) == 2 and client.count(Query("attribution")) == 2
    assert receipt(client, event) == p


def test_move_multiple_old_references_same_batch_preserves_fields_history_and_deps():
    storage, client, service = setup()
    a, b, c, keep = [append(storage, "gps_point") for _ in range(4)]
    old1 = seed_place(client, [a.data_id, b.data_id, keep.data_id], extension={"keep": [1]})
    old2 = seed_place(client, [c.data_id])
    for e in (a, b, c):
        service.handle(e)
    old_attr = attr(client, a)
    edit_attribution(client, old_attr, {**old_attr.payload, "extension": {"preserved": True}})
    old_attr = attr(client, a)
    state = present(client.get_system_record(STATE_ID))
    client.write(
        [
            PutSystemRecord(
                "attribution_state",
                STATE_ID,
                {**state.payload, "extension": ["state"]},
                expected_revision=state.revision,
            )
        ],
        Context.system(),
    ).check()
    before = len(storage.events)
    event = feedback(
        storage,
        service,
        {
            "kind": "create_place",
            "name": "New synthetic",
            "gps_ids": [c.data_id, a.data_id, b.data_id],
        },
    )
    p = receipt(client, event)
    assert p["status"] == "succeeded"
    assert present(client.get(old1)).payload == {
        "name": "Synthetic old",
        "is_unknown": False,
        "references": [keep.data_id],
        "extension": {"keep": [1]},
    }
    assert present(client.get(old1)).version == 2  # aggregate, not one update per GPS
    assert present(client.get(old2)).payload["references"] == []
    current = attr(client, a)
    assert current.id == old_attr.id and current.version == old_attr.version + 1
    assert current.payload["extension"] == {"preserved": True}
    assert client.get_version(old_attr.id, old_attr.version) == old_attr
    after_state = present(client.get_system_record(STATE_ID))
    assert after_state.payload["extension"] == ["state"]
    assert after_state.payload["reference_revision"] == state.payload["reference_revision"] + 1
    assert after_state.payload["next_person_number"] == state.payload["next_person_number"]
    changes = [
        e
        for e in storage.events[before:]
        if isinstance(e, DataChanged) and e.data_type != "feedback"
    ]
    assert all((e.root_feedback_id, e.depth) == (event.data_id, 0) for e in changes)
    assert all(e.skip_history == (e.data_type == "place") for e in changes)
    for e in (a, b, c):
        deps = client.dependencies_of(attr(client, e).id)
        assert len(deps) == 1 and deps[0].depends_on.id == p["new_target_id"]
    assert not client.stale_dependencies([p["new_target_id"]])


@pytest.mark.parametrize("name", [None, True, 3, "", " \n", "x" * 257])
def test_bad_name_no_business_writes(name):
    storage, client, service = setup()
    e = append(storage, "gps_point")
    event = command_event(storage, [e.data_id], name=name)
    service.handle(event)
    assert receipt(client, event)["error_code"] == "invalid_name"
    assert client.count(Query("place")) == 0 and client.get_system_record(STATE_ID) is None


@pytest.mark.parametrize(
    "ids", [None, "text", [], [None], [True], [12], [{}], ["bad"], [new_id() for _ in range(101)]]
)
def test_bad_gps_list_no_business_writes(ids):
    storage, client, service = setup()
    event = command_event(storage, ids)
    service.handle(event)
    assert receipt(client, event)["error_code"] == "invalid_gps_ids"
    assert client.count(Query("place")) == 0 and client.count(Query("attribution")) == 0
    assert client.get_system_record(STATE_ID) is None
    assert client.is_processed("attribution", event.event_id)


def test_duplicate_gps_rejected_not_silently_deduplicated():
    storage, client, service = setup()
    e = append(storage, "gps_point")
    event = command_event(storage, [e.data_id, e.data_id])
    service.handle(event)
    assert receipt(client, event)["error_code"] == "duplicate_gps_ids"
    assert client.count(Query("place")) == 0


@pytest.mark.parametrize("bad", ["missing", "voiceprint", "semantic", "coordinates"])
def test_mixed_selection_invalid_last_item_rolls_back_everything(bad):
    storage, client, service = setup()
    e = append(storage, "gps_point")
    service.handle(e)
    original = attr(client, e)
    id = new_id()
    if bad == "voiceprint":
        id = append(storage).data_id
    elif bad == "semantic":
        id = original.payload["target_id"]
    elif bad == "coordinates":
        id = append(storage, "gps_point", {"latitude": 91, "longitude": 0}).data_id
    before = present(client.get_system_record(STATE_ID))
    event = command_event(storage, [e.data_id, id])
    service.handle(event)
    assert (
        receipt(client, event)["error_code"]
        == {
            "missing": "invalid_subject",
            "voiceprint": "subject_type_mismatch",
            "semantic": "invalid_subject",
            "coordinates": "invalid_coordinates",
        }[bad]
    )
    assert attr(client, e) == original and client.count(Query("place")) == 1
    assert client.get_system_record(STATE_ID) == before
    assert "new_target_id" not in receipt(client, event)


@pytest.mark.parametrize("guard", [0, 1, True, "1", {}, []])
def test_nonnull_single_version_guard_is_not_silently_ignored(guard):
    storage, client, service = setup()
    e = append(storage, "gps_point")
    event = command_event(storage, [e.data_id], expected_version=guard)
    service.handle(event)
    assert receipt(client, event)["error_code"] == "invalid_expected_version"
    assert client.count(Query("place")) == 0


@pytest.mark.parametrize("protection", [True, None, "missing", "invalid"])
def test_protection_conflict_entire_selection_rejected(protection):
    storage, client, service = setup()
    a, b = append(storage, "gps_point"), append(storage, "gps_point")
    for e in (a, b):
        service.handle(e)
    previous = attr(client, b)
    payload = {**previous.payload, "user_protected": protection}
    if protection == "missing":
        payload.pop("user_protected")
    edit_attribution(client, previous, payload)
    previous = attr(client, b)
    before = present(client.get_system_record(STATE_ID))
    event = command_event(storage, [a.data_id, b.data_id])
    service.handle(event)
    assert receipt(client, event)["error_code"] == (
        "protected_attribution_conflict"
        if protection is True
        else "invalid_protection"
        if protection == "invalid"
        else "legacy_protection_unknown"
    )
    assert attr(client, b) == previous and attr(client, a).version == 1
    assert client.count(Query("place")) == 1 and client.get_system_record(STATE_ID) == before


def test_selection_protection_survives_reference_removal_and_rejudge():
    storage, client, service = setup()
    e = append(storage, "gps_point")
    created = feedback(
        storage, service, {"kind": "create_place", "name": "Synthetic", "gps_ids": [e.data_id]}
    )
    target = receipt(client, created)["new_target_id"]
    feedback(
        storage, service, {"kind": "remove_reference", "subject_id": e.data_id, "target_id": target}
    )
    current = attr(client, e)
    r = request(storage, current)
    service.handle(r)
    assert attr(client, e) == current
    assert present(client.get_reevaluation_result(r.event_id)).outcome == "unchanged"
    second = feedback(
        storage, service, {"kind": "create_place", "name": "Other", "gps_ids": [e.data_id]}
    )
    assert receipt(client, second)["error_code"] == "protected_attribution_conflict"
    assert attr(client, e).payload["protection_feedback_id"] == created.data_id
    assert client.count(Query("place")) == 2


def test_unknown_with_refs_rejected_not_repaired_or_moved():
    storage, client, service = setup()
    e = append(storage, "gps_point")
    service.handle(e)
    unknown = present(client.get(attr(client, e).payload["target_id"]))
    client.write(
        [
            Update(
                "place", unknown.id, unknown.version, {**unknown.payload, "references": [e.data_id]}
            )
        ],
        Context.system(),
    ).check()
    before = present(client.get_system_record(STATE_ID))
    event = command_event(storage, [e.data_id])
    service.handle(event)
    assert receipt(client, event)["error_code"] == "invalid_unknown_place"
    assert present(client.get(unknown.id)).payload["references"] == [e.data_id]
    assert client.count(Query("place")) == 1 and client.get_system_record(STATE_ID) == before


@pytest.mark.parametrize("late_fault", ["collision", "missing_dependencies"])
def test_late_failure_no_half_write_including_fence_receipt_and_marker(late_fault):
    storage, client, service = setup()
    e = append(storage, "gps_point")
    old = seed_place(client, [e.data_id])
    service.handle(e)
    original = attr(client, e)
    state = present(client.get_system_record(STATE_ID))
    event = command_event(storage, [e.data_id])
    ops = plan(client, event)
    target = next(op.id for op in ops if isinstance(op, Create) and op.data_type == "place")
    before = len(storage.events)
    if late_fault == "collision":
        result = client.write(
            [*ops, MarkProcessed("attribution", event.event_id), Create("place", old, {})],
            Context.feedback(event.data_id),
        )
        assert present(result.conflict).reason == "id_exists"
    else:
        ops = [op for op in ops if not isinstance(op, RegisterDependencies)]
        with pytest.raises(InvalidRequest):
            client.write(
                [*ops, MarkProcessed("attribution", event.event_id)],
                Context.feedback(event.data_id),
            )
    assert client.get(target) is None and client.get_system_record(STATE_ID) == state
    assert attr(client, e) == original
    assert present(client.get(old)).payload["references"] == [e.data_id]
    assert client.get_system_record(feedback_result_id(event.data_id)) is None
    assert not client.is_processed("attribution", event.event_id)
    assert len(storage.events) == before
    service.handle(event)
    assert receipt(client, event)["status"] == "succeeded"


def test_maximum_100_points_and_complete_dependencies():
    storage, client, service = setup()
    events = [append(storage, "gps_point") for _ in range(100)]
    event = command_event(storage, [e.data_id for e in events])
    service.handle(event)
    p = receipt(client, event)
    assert p["status"] == "succeeded" and len(p["attribution_ids"]) == 100
    assert len(set(p["attribution_ids"])) == 100
    for id in p["attribution_ids"]:
        assert len(client.dependencies_of(id)) == 1


def test_reference_owner_scan_paginates():
    storage, client, service = setup()
    e = append(storage, "gps_point")
    with client.snapshot() as reader:
        fence = read_fence(reader)
    ids = sorted(new_id() for _ in range(105))
    fence.changed = True
    client.write(
        [
            fence.operation(),
            *fence.initial_ops,
            *[
                Create(
                    "place",
                    id,
                    {
                        "name": "Synthetic",
                        "is_unknown": False,
                        "references": [e.data_id] if id == ids[-1] else [],
                    },
                )
                for id in ids
            ],
        ],
        Context.system(),
    ).check()
    service.handle(e)
    event = command_event(storage, [e.data_id])
    service.handle(event)
    assert receipt(client, event)["status"] == "succeeded"
    assert present(client.get(ids[-1])).payload["references"] == []


@pytest.mark.parametrize("which_first", ["create", "automatic"])
def test_create_and_automatic_match_stale_plans_conflict_and_recompute(which_first):
    storage, client, service = setup()
    a, b = append(storage, "gps_point"), append(storage, "gps_point")
    service.handle(a)
    event = command_event(storage, [a.data_id])
    creation = plan(client, event)
    with client.snapshot() as reader:
        automatic = BusinessPlanner(CONFIG).new_record(reader, present(reader.get(b.data_id)), b)
    first, second = (creation, automatic) if which_first == "create" else (automatic, creation)
    context1 = Context.feedback(event.data_id) if which_first == "create" else Context.of(b)
    context2 = Context.of(b) if which_first == "create" else Context.feedback(event.data_id)
    client.write(first, context1).check()
    result = client.write(second, context2)
    if which_first == "create":
        assert present(result.conflict).reason == "revision_mismatch"
        assert client.query(Query("attribution", where={"subject_id": b.data_id})).items == ()
        service.handle(b)
        assert attr(client, b).payload["target_id"] == receipt(client, event)["new_target_id"]
    else:
        # Automatic assignment of another point did not change the candidate set.
        result.check()
        assert receipt(client, event)["status"] == "succeeded"


@pytest.mark.parametrize("race", ["same_feedback", "overlap", "disjoint"])
@pytest.mark.parametrize("initialized", [False, True])
def test_concurrent_workers_fenced_no_duplicate_or_orphan(race, initialized):
    storage, client, service = setup()
    a, b = append(storage, "gps_point"), append(storage, "gps_point")
    if initialized:
        service.handle(a)
    first = command_event(storage, [a.data_id])
    second = (
        first
        if race == "same_feedback"
        else command_event(storage, [b.data_id if race == "disjoint" else a.data_id])
    )
    events = [first, second]
    barrier = Barrier(2)
    planner = BusinessPlanner(CONFIG)
    calls = [0, 0]

    def worker(index):
        def feedback_plan(reader, record, event):
            ops = planner.feedback(reader, record, event)
            calls[index] += 1
            if calls[index] == 1:
                barrier.wait(timeout=10)
            return ops

        runtime = AttributionService(
            client,
            new_record=planner.new_record,
            feedback=feedback_plan,
            reevaluation=planner.reevaluation,
        )
        runtime.handle(events[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, [0, 1]))
    statuses = [receipt(client, e)["status"] for e in events]
    assert sorted(statuses) == (
        ["rejected", "succeeded"] if race == "overlap" else ["succeeded", "succeeded"]
    )
    assert client.count(Query("place")) == (3 if race == "disjoint" else 2)
    assert client.count(Query("attribution")) == (2 if race == "disjoint" else 1)
    if race == "overlap":
        assert {receipt(client, e)["error_code"] for e in events} == {
            None,
            "protected_attribution_conflict",
        }
    assert all(client.is_processed("attribution", e.event_id) for e in events)
    assert all(
        isinstance(client.get(id), SemanticRecord)
        for e in events
        for id in receipt(client, e).get("attribution_ids", [])
    )


@pytest.mark.parametrize("initialized", [True, False])
def test_create_races_automatic_assignment_of_same_unassigned_gps(initialized):
    storage, client, service = setup()
    if initialized:
        service.handle(append(storage, "gps_point"))
    gps = append(storage, "gps_point")
    event = command_event(storage, [gps.data_id])
    stale = plan(client, event)
    service.handle(gps)
    automatic = attr(client, gps)
    result = client.write(stale, Context.feedback(event.data_id))
    assert present(result.conflict).reason in {"id_exists", "unique_field_taken"}
    assert client.get_system_record(feedback_result_id(event.data_id)) is None
    assert client.count(Query("place")) == 1
    service.handle(event)
    assert receipt(client, event)["status"] == "succeeded"
    assert attr(client, gps).id == automatic.id and attr(client, gps).version == 2
    assert attr(client, gps).payload["user_protected"] is True
    assert client.count(Query("place")) == 2


@pytest.mark.parametrize("damage", ["malformed_refs", "unknown_impostor", "bad_unknown_flag"])
def test_malformed_candidate_state_fails_closed_without_initializing_state(damage):
    storage, client, service = setup()
    gps = append(storage, "gps_point")
    payload = {"name": "Synthetic damaged", "references": [], "is_unknown": False}
    if damage == "malformed_refs":
        payload["references"] = ["bad"]
    else:
        payload["is_unknown"] = True if damage == "unknown_impostor" else 1
    client.write([Create("place", new_id(), payload)], Context.system()).check()
    event = command_event(storage, [gps.data_id])
    service.handle(event)
    assert receipt(client, event)["error_code"] == (
        "invalid_references" if damage == "malformed_refs" else "invalid_unknown_place"
    )
    assert client.get_system_record(STATE_ID) is None
    assert client.count(Query("attribution")) == 0 and client.count(Query("place")) == 1
