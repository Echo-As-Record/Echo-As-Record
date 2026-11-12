import math

import pytest
from ear_attribution import AttributionService, BusinessPlanner, MatchConfig
from ear_attribution.feedback import feedback_result_id
from ear_attribution.matching import distance_m, vector
from ear_attribution.payloads import DomainError, read_attribution
from ear_attribution.state import STATE_ID
from ear_contracts import DEFAULT_REGISTRY, DataChanged, RawRecord, SemanticRecord, new_id
from ear_storage_client import (
    Context,
    Create,
    MarkProcessed,
    MemoryStorage,
    Query,
    RegisterDependencies,
    Supersede,
    Update,
)

from .support import CONFIG, append, attr, feedback, present, request, setup, voice


def test_unknown_person_repeat_no_automatic_reference_growth():
    storage, client, service = setup()
    first = append(storage)
    service.handle(first)
    a = attr(client, first)
    person = present(client.get(a.payload["target_id"]))
    assert person.payload == {"name": None, "display_number": 1, "references": [first.data_id]}
    assert a.payload["user_protected"] is False
    assert a.payload["subject_type"] == "voiceprint"
    assert a.event_start == present(client.get(first.data_id)).event_start
    service.handle(first)
    second = append(storage)
    service.handle(second)
    assert attr(client, second).payload["target_id"] == person.id
    assert present(client.get(person.id)).version == 1
    assert client.count(Query("person")) == 1
    assert client.count(Query("attribution")) == 2
    assert {d.depends_on.id for d in client.dependencies_of(a.id)} == {person.id}


def test_gps_unknown_singleton_restart_and_distance():
    storage, client, service = setup()
    first, second = append(storage, "gps_point"), append(storage, "gps_point")
    service.handle(first)
    AttributionService.business(client, config=CONFIG).handle(second)
    target = attr(client, first).payload["target_id"]
    assert attr(client, second).payload["target_id"] == target
    assert client.count(Query("place")) == 1
    assert present(client.get(target)).payload == {
        "name": None,
        "references": [],
        "is_unknown": True,
    }
    # Synthetic existing user-created place. Real create_place feedback is out of this slice.
    place = new_id()
    state = present(client.get_system_record(STATE_ID))
    from ear_storage_client import PutSystemRecord

    client.write(
        [
            Create("place", place, {"name": "Synthetic", "references": [first.data_id]}),
            PutSystemRecord(
                "attribution_state",
                STATE_ID,
                {**state.payload, "reference_revision": state.payload["reference_revision"] + 1},
                expected_revision=state.revision,
            ),
        ],
        Context.system(),
    ).check()
    near = append(storage, "gps_point", {"latitude": 0.0001, "longitude": 0.0})
    service.handle(near)
    assert attr(client, near).payload["target_id"] == place
    far = append(storage, "gps_point", {"latitude": 1.0, "longitude": 0.0})
    service.handle(far)
    assert attr(client, far).payload["target_id"] == target
    assert distance_m((0, 179.9999), (0, -179.9999)) < 30


@pytest.mark.parametrize("payload", [voice(model="other/v1"), voice((1, 0, 0)), voice((0, 1))])
def test_only_same_model_and_dimension_compare(payload):
    storage, client, service = setup()
    a, b = append(storage), append(storage, payload=payload)
    service.handle(a)
    service.handle(b)
    assert attr(client, a).payload["target_id"] != attr(client, b).payload["target_id"]
    deps = client.dependencies_of(attr(client, b).id)
    assert len(deps) == 2  # even the incompatible candidate's references were read
    assert all(d.fields == ("references",) for d in deps)


def test_configurable_threshold_and_all_actual_candidates():
    storage, client, service = setup()
    x, y = append(storage), append(storage, payload=voice((0, 1)))
    service.handle(x)
    service.handle(y)
    z = append(storage, payload=voice((1, 1)))
    relaxed = AttributionService.business(client, config=MatchConfig(0.7, 100))
    relaxed.handle(z)
    deps = client.dependencies_of(attr(client, z).id)
    assert len(deps) == 2
    assert client.count(Query("person")) == 2
    # deterministic ties: lowest entity id, independent of name
    ids = [attr(client, event).payload["target_id"] for event in (x, y)]
    assert attr(client, z).payload["target_id"] == min(ids)


@pytest.mark.parametrize(
    "payload",
    [
        voice((0, 0)),
        voice(()),
        {**voice(), "model_version": None},
        {**voice(), "embedding": {"values": [1, 2], "shape": [1, 2]}},
        {**voice(), "embedding": {"values": [True, 0], "shape": [2]}},
    ],
)
def test_invalid_embedding_not_unknown_person_or_ack(payload):
    storage, client, service = setup()
    event = append(storage, payload=payload)
    with pytest.raises(DomainError):
        service.handle(event)
    assert client.count(Query("person")) == 0
    assert client.count(Query("attribution")) == 0
    assert not client.is_processed("attribution", event.event_id)
    assert client.get_system_record(STATE_ID) is None


def test_nonfinite_vector_and_scale_are_validated_without_storage_json_coercion():
    storage, client, _ = setup()
    raw = present(client.get(append(storage).data_id))
    assert isinstance(raw, RawRecord)
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(DomainError):
            vector(raw.model_copy(update={"payload": voice((value, 1))}))
    for value in (1e300, 1e-300):
        assert vector(raw.model_copy(update={"payload": voice((value, value))}))[
            1
        ] == pytest.approx((2**-0.5, 2**-0.5))


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 91, "longitude": 0},
        {"latitude": False, "longitude": 0},
        {"latitude": 0, "longitude": 181},
    ],
)
def test_invalid_gps_does_not_create_unknown(payload):
    storage, client, service = setup()
    event = append(storage, "gps_point", payload)
    with pytest.raises(DomainError):
        service.handle(event)
    assert client.count(Query("place")) == 0


def test_correction_atomic_protection_reference_gate_extensions_and_receipt():
    storage, client, service = setup()
    first, second = append(storage), append(storage, payload=voice((0, 1)))
    service.handle(first)
    service.handle(second)
    a, b = attr(client, first), attr(client, second)
    old, new = a.payload["target_id"], b.payload["target_id"]
    # Existing extension survives full-payload Update.
    client.write(
        [
            Update("attribution", a.id, a.version, {**a.payload, "extension": {"keep": True}}),
            RegisterDependencies(
                "attribution", a.id, [d.depends_on for d in client.dependencies_of(a.id)]
            ),
        ],
        Context.system(),
    ).check()
    count = len(storage.events)
    event = feedback(
        storage,
        service,
        {
            "kind": "correct_attribution",
            "subject_id": first.data_id,
            "previous_target_id": old,
            "new_target_id": new,
        },
    )
    a = attr(client, first)
    assert a.payload["target_id"] == new and a.payload["user_protected"] is True
    assert a.payload["protection_feedback_id"] == event.data_id
    assert a.payload["extension"] == {"keep": True}
    assert present(client.get(old)).payload["references"] == []
    assert first.data_id in present(client.get(new)).payload["references"]
    changed = [
        e for e in storage.events[count:] if isinstance(e, DataChanged) and e.operation == "updated"
    ]
    assert len(changed) == 3
    assert all(e.skip_history == (e.data_type == "person") for e in changed)
    assert all((e.root_feedback_id, e.depth) == (event.data_id, 0) for e in changed)
    receipt = present(client.get_system_record(feedback_result_id(event.data_id)))
    assert receipt.payload["status"] == "succeeded"
    assert receipt.payload["after_version"] == a.version
    assert receipt.id != event.data_id
    snapshot = len(storage.events)
    service.handle(event)
    assert len(storage.events) == snapshot


def test_confirm_remove_reference_cannot_unlock_and_no_change_finish():
    storage, client, service = setup()
    event = append(storage)
    service.handle(event)
    a = attr(client, event)
    target = a.payload["target_id"]
    confirmation = feedback(
        storage,
        service,
        {"kind": "confirm_attribution", "subject_id": event.data_id, "target_id": target},
    )
    a = attr(client, event)
    version = a.version
    feedback(
        storage,
        service,
        {"kind": "confirm_attribution", "subject_id": event.data_id, "target_id": target},
    )
    assert attr(client, event).version == version
    assert attr(client, event).payload["protection_feedback_id"] == confirmation.data_id
    feedback(
        storage,
        service,
        {"kind": "remove_reference", "subject_id": event.data_id, "target_id": target},
    )
    assert present(client.get(target)).payload["references"] == []
    request_event = request(storage, a)
    before = len(storage.events)
    service.handle(request_event)
    assert len(storage.events) == before  # Finish/state/dependencies are not business events
    assert attr(client, event).version == version
    assert attr(client, event).payload["user_protected"] is True
    result = present(client.get_reevaluation_result(request_event.event_id))
    assert result.outcome == "unchanged"
    assert result.before_version == result.after_version == version
    assert client.dependencies_of(a.id)[0].depends_on.version == present(client.get(target)).version
    service.handle(request_event)
    assert len(storage.events) == before


def test_automatic_unchanged_finish_and_name_does_not_stale_references():
    storage, client, service = setup()
    event = append(storage)
    service.handle(event)
    a = attr(client, event)
    feedback(
        storage,
        service,
        {"kind": "rename", "target_id": a.payload["target_id"], "name": "Synthetic name"},
    )
    assert client.stale_dependencies([a.payload["target_id"]]) == []
    r = request(storage, a)
    count = len(storage.events)
    service.handle(r)
    assert present(client.get_reevaluation_result(r.event_id)).outcome == "unchanged"
    assert present(client.get(a.id)).version == 1
    assert len(storage.events) == count
    assert client.dependencies_of(a.id)[0].fields == ("references",)


def test_automatic_changed_finish_after_reference_correction():
    storage, client, service = setup()
    x, y, z = append(storage), append(storage, payload=voice((0, 1))), append(storage)
    for e in (x, y, z):
        service.handle(e)
    a, b, c = attr(client, x), attr(client, y), attr(client, z)
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
    r = request(storage, c)
    service.handle(r)
    assert present(client.get(c.id)).payload["target_id"] == b.payload["target_id"]
    assert present(client.get_reevaluation_result(r.event_id)).outcome == "changed"
    assert present(client.get(c.id)).payload["user_protected"] is False
    assert all(d.fields == ("references",) for d in client.dependencies_of(c.id))


def test_legacy_readable_but_not_automatically_unlocked():
    storage, client, service = setup()
    event = append(storage)
    service.handle(event)
    a = attr(client, event)
    old = {key: a.payload[key] for key in ("subject_id", "target_type", "target_id")}
    client.write(
        [Update("attribution", a.id, 1, old), RegisterDependencies("attribution", a.id, [])],
        Context.system(),
    ).check()
    legacy = present(client.get(a.id))
    assert isinstance(legacy, SemanticRecord)
    assert read_attribution(client, legacy).protected is None
    r = request(storage, legacy)
    service.handle(r)
    result = present(client.get_reevaluation_result(r.event_id))
    assert result.reason_code == "legacy_protection_unknown"
    assert result.disposition == "kept_protected"
    assert present(client.get(a.id)).payload == old


def test_terminated_attribution_and_protected_terminated_target_finish():
    storage, client, service = setup()
    event = append(storage)
    service.handle(event)
    a = attr(client, event)
    feedback(
        storage,
        service,
        {
            "kind": "confirm_attribution",
            "subject_id": event.data_id,
            "target_id": a.payload["target_id"],
        },
    )
    person = present(client.get(a.payload["target_id"]))
    client.write([Supersede("person", person.id, person.version)], Context.system()).check()
    r = request(storage, a)
    service.handle(r)
    assert present(client.get_reevaluation_result(r.event_id)).disposition == "kept_protected"
    current = present(client.get(a.id))
    client.write([Supersede("attribution", a.id, current.version)], Context.system()).check()
    r = request(storage, a)
    service.handle(r)
    assert present(client.get_reevaluation_result(r.event_id)).disposition == "terminated"


def test_stale_explicit_correction_rejected_no_partial_changes():
    storage, client, service = setup()
    first, second = append(storage), append(storage, payload=voice((0, 1)))
    service.handle(first)
    service.handle(second)
    a, b = attr(client, first), attr(client, second)
    event = feedback(
        storage,
        service,
        {
            "kind": "correct_attribution",
            "subject_id": first.data_id,
            "previous_target_id": b.payload["target_id"],
            "new_target_id": b.payload["target_id"],
        },
    )
    assert (
        present(client.get_system_record(feedback_result_id(event.data_id))).payload["error_code"]
        == "previous_target_conflict"
    )
    assert attr(client, first).version == a.version
    event = feedback(
        storage,
        service,
        {
            "kind": "confirm_attribution",
            "subject_id": first.data_id,
            "target_id": a.payload["target_id"],
            "expected_version": 9,
        },
    )
    assert (
        present(client.get_system_record(feedback_result_id(event.data_id))).payload["error_code"]
        == "expected_version_conflict"
    )


@pytest.mark.parametrize("choice", [True, False])
def test_reference_history_choice_and_conflicting_add_rejected(choice):
    storage, client, service = setup()
    a, b = append(storage), append(storage, payload=voice((0, 1)))
    service.handle(a)
    service.handle(b)
    person = attr(client, a).payload["target_id"]
    event = feedback(
        storage,
        service,
        {
            "kind": "remove_reference",
            "subject_id": a.data_id,
            "target_id": person,
            "rejudge_history": choice,
        },
    )
    changes = [
        e
        for e in storage.events
        if isinstance(e, DataChanged)
        and e.root_feedback_id == event.data_id
        and e.data_type == "person"
    ]
    assert len(changes) == 1 and isinstance(changes[0], DataChanged)
    assert changes[0].skip_history is not choice
    event = feedback(
        storage,
        service,
        {
            "kind": "add_reference",
            "subject_id": a.data_id,
            "target_id": attr(client, b).payload["target_id"],
            "rejudge_history": choice,
        },
    )
    assert (
        present(client.get_system_record(feedback_result_id(event.data_id))).payload["status"]
        == "rejected"
    )


def test_unsupported_feedback_is_explicit_not_success_and_foreign_ignored():
    storage, client, service = setup()
    event = feedback(storage, service, {"kind": "reattribute_range", "interval": None})
    assert (
        present(client.get_system_record(feedback_result_id(event.data_id))).payload["error_code"]
        == "unsupported_command"
    )
    foreign = append(
        storage, "feedback", {"module": "dependency", "command": {"kind": "reevaluate_related"}}
    )
    service.handle(foreign)
    assert not client.is_processed("attribution", foreign.event_id)


def test_registry_not_silently_patched():
    with pytest.raises(LookupError):
        AttributionService.business(MemoryStorage().client("attribution"), config=CONFIG)
    assert "attribution_state" not in DEFAULT_REGISTRY
    assert not DEFAULT_REGISTRY.get("attribution").supports_reevaluation


def test_same_raw_concurrent_plans_atomic_no_orphan_then_reread():
    storage, client, service = setup()
    event = append(storage)
    planner = BusinessPlanner(CONFIG)
    with client.snapshot() as reader:
        p1 = planner.new_record(reader, present(reader.get(event.data_id)), event)
        p2 = planner.new_record(reader, present(reader.get(event.data_id)), event)
    client.write([*p1, MarkProcessed("attribution", event.event_id)], Context.system()).check()
    result = client.write([*p2, MarkProcessed("attribution", event.event_id)], Context.system())
    assert present(result.conflict) is not None
    service.handle(event)
    assert client.count(Query("person")) == client.count(Query("attribution")) == 1
    assert client.count(Query("place")) == 1


def test_empty_candidate_plan_rejected_after_new_person_then_retry_reuses_match():
    storage, client, service = setup()
    first, second = append(storage), append(storage)
    planner = BusinessPlanner(CONFIG)
    with client.snapshot() as reader:
        stale = planner.new_record(reader, present(reader.get(second.data_id)), second)
    service.handle(first)
    rejected = client.write(stale, Context.system())
    assert present(rejected.conflict).reason == "id_exists"  # singleton initialization CAS
    service.handle(second)
    assert client.count(Query("person")) == 1
    third = append(storage, payload=voice((0, 1)))
    fourth = append(storage, payload=voice((0, 1)))
    with client.snapshot() as reader:
        stale = planner.new_record(reader, present(reader.get(fourth.data_id)), fourth)
    service.handle(third)
    assert present(client.write(stale, Context.system()).conflict).reason == "revision_mismatch"
    service.handle(fourth)
    assert client.count(Query("person")) == 2
    assert present(client.get_system_record(STATE_ID)).payload["next_person_number"] == 3
