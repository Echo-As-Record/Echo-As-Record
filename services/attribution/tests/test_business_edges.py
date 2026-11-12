import pytest
from ear_attribution import BusinessPlanner, MatchConfig
from ear_attribution.feedback import feedback_result_id
from ear_attribution.payloads import DomainError
from ear_attribution.state import read_fence
from ear_contracts import new_id
from ear_storage_client import Context, Create, MarkProcessed, Query, Update

from .support import CONFIG, append, attr, feedback, present, request, setup, voice


@pytest.mark.parametrize(
    "cosine,gps",
    [
        (1.01, 10),
        (-1.01, 10),
        (True, 10),
        (float("nan"), 10),
        (0.9, -1),
        (0.9, float("inf")),
        (0.9, False),
    ],
)
def test_configuration_rejects_unsafe_thresholds(cosine, gps):
    with pytest.raises(ValueError):
        MatchConfig(cosine, gps)


def test_existing_reference_wins_even_against_earlier_identical_candidate():
    storage, client, service = setup()
    x, y = append(storage), append(storage)
    service.handle(x)
    # Seed an explicit synthetic user-created entity with a reference before y is consumed.
    target = new_id()
    with client.snapshot() as reader:
        fence = read_fence(reader)
    fence.changed = True
    client.write(
        [fence.operation(), Create("person", target, {"name": None, "references": [y.data_id]})],
        Context.system(),
    ).check()
    service.handle(y)
    assert attr(client, y).payload["target_id"] == target
    assert len(client.dependencies_of(attr(client, y).id)) == 2


def test_empty_references_never_match_and_candidate_pagination_is_complete():
    storage, client, service = setup()
    x = append(storage)
    service.handle(x)
    target = attr(client, x).payload["target_id"]
    # More than one query page of empty entities is still a real candidate read set.
    with client.snapshot() as reader:
        fence = read_fence(reader)
    fence.changed = True
    ops = [Create("person", new_id(), {"name": None, "references": []}) for _ in range(101)]
    client.write([fence.operation(), *ops], Context.system()).check()
    y = append(storage)
    service.handle(y)
    assert attr(client, y).payload["target_id"] == target
    assert len(client.dependencies_of(attr(client, y).id)) == 102


def test_reference_owner_unique_rejection_rolls_back_entire_batch():
    storage, client, service = setup()
    e = append(storage)
    service.handle(e)
    collision = new_id()
    result = client.write(
        [
            Create("person", collision, {"references": [e.data_id]}),
            MarkProcessed("attribution", new_id()),
        ],
        Context.system(),
    )
    assert present(result.conflict).reason == "member_taken"
    assert client.get(collision) is None
    assert client.count(Query("person")) == 1


def test_feedback_reference_and_attribution_ops_roll_back_on_late_collision():
    storage, client, service = setup()
    x, y = append(storage), append(storage, payload=voice((0, 1)))
    service.handle(x)
    service.handle(y)
    a, b = attr(client, x), attr(client, y)
    event = append(
        storage,
        "feedback",
        {
            "module": "attribution",
            "command": {
                "kind": "correct_attribution",
                "subject_id": x.data_id,
                "previous_target_id": a.payload["target_id"],
                "new_target_id": b.payload["target_id"],
            },
        },
    )
    planner = BusinessPlanner(CONFIG)
    with client.snapshot() as reader:
        ops = present(planner.feedback(reader, present(reader.get(event.data_id)), event))
    # Conflict late in the transaction, after reference changes/attribution/result were attempted.
    result = client.write(
        [*ops, Create("person", b.payload["target_id"], {})], Context.feedback(event.data_id)
    )
    assert present(result.conflict).reason == "id_exists"
    assert present(client.get(a.id)).payload == a.payload
    assert present(client.get(a.payload["target_id"])).payload["references"] == [x.data_id]
    assert client.get_system_record(feedback_result_id(event.data_id)) is None
    service.handle(event)
    assert present(client.get(a.id)).payload["target_id"] == b.payload["target_id"]


def test_user_can_explicitly_correct_again_but_auto_cannot_override():
    storage, client, service = setup()
    x, y = append(storage), append(storage, payload=voice((0, 1)))
    service.handle(x)
    service.handle(y)
    a, b = attr(client, x), attr(client, y)
    p, q = a.payload["target_id"], b.payload["target_id"]
    first = feedback(
        storage,
        service,
        {
            "kind": "correct_attribution",
            "subject_id": x.data_id,
            "previous_target_id": p,
            "new_target_id": q,
        },
    )
    second = feedback(
        storage,
        service,
        {
            "kind": "correct_attribution",
            "subject_id": x.data_id,
            "previous_target_id": q,
            "new_target_id": p,
        },
    )
    current = attr(client, x)
    assert current.payload["target_id"] == p
    assert current.payload["protection_feedback_id"] == first.data_id
    assert (
        present(client.get_system_record(feedback_result_id(second.data_id))).payload["status"]
        == "succeeded"
    )
    feedback(
        storage, service, {"kind": "remove_reference", "subject_id": x.data_id, "target_id": p}
    )
    r = request(storage, current)
    service.handle(r)
    assert attr(client, x).payload["target_id"] == p
    assert client.count(Query("person")) == 2  # no new automatic person for a protected subject


def test_invalid_pair_feedback_rejected_without_creating_bad_reference():
    storage, client, service = setup()
    x = append(storage)
    gps = append(storage, "gps_point")
    service.handle(x)
    service.handle(gps)
    a = attr(client, x)
    f = feedback(
        storage,
        service,
        {
            "kind": "correct_attribution",
            "subject_id": x.data_id,
            "previous_target_id": a.payload["target_id"],
            "new_target_id": attr(client, gps).payload["target_id"],
        },
    )
    assert (
        present(client.get_system_record(feedback_result_id(f.data_id))).payload["error_code"]
        == "target_unavailable"
    )
    assert attr(client, x).version == a.version


def test_invalid_candidate_blocks_new_match_and_terminal_failed_finish_is_visible():
    storage, client, service = setup()
    x = append(storage)
    service.handle(x)
    a = attr(client, x)
    bad = append(storage, payload=voice((0, 0)))
    person = present(client.get(a.payload["target_id"]))
    with client.snapshot() as reader:
        fence = read_fence(reader)
    fence.changed = True
    client.write(
        [
            fence.operation(),
            Update(
                "person", person.id, person.version, {**person.payload, "references": [bad.data_id]}
            ),
        ],
        Context.system(),
    ).check()
    e = append(storage)
    with pytest.raises(DomainError, match="invalid_embedding"):
        service.handle(e)
    assert client.count(Query("person")) == 1
    assert not client.is_processed("attribution", e.event_id)
    r = request(storage, a)
    service.handle(r)
    result = present(client.get_reevaluation_result(r.event_id))
    assert result.outcome == "failed" and result.error_code == "invalid_embedding"
    assert present(client.get(a.id)).version == a.version
    assert client.is_processed("attribution", r.event_id)
    assert client.stale_dependencies([person.id])  # failure does not pretend fresh dependencies


def test_unchanged_plan_accepts_name_only_change_but_not_reference_change():
    storage, client, service = setup()
    x = append(storage)
    service.handle(x)
    y = append(storage)
    planner = BusinessPlanner(CONFIG)
    with client.snapshot() as reader:
        ops = planner.new_record(reader, present(reader.get(y.data_id)), y)
    target = attr(client, x).payload["target_id"]
    feedback(storage, service, {"kind": "rename", "target_id": target, "name": "Only synthetic"})
    client.write([*ops, MarkProcessed("attribution", y.event_id)], Context.of(y)).check()
    assert attr(client, y).payload["target_id"] == target
    assert not client.stale_dependencies([target])
