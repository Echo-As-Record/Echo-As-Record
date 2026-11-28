"""Bound the reference reads without changing matching decisions or dependencies."""

from unittest.mock import Mock

import pytest
from ear_attribution.matching import MatchConfig, match
from ear_attribution.payloads import DomainError
from ear_contracts import RawRecord, SemanticRecord, new_id
from ear_storage_client import Context, Create, Page, PermissionDenied, Reader

from .support import CONFIG, append, present, setup, voice


@pytest.mark.parametrize("grouped", [False, True])
def test_batch_reads_cross_candidate_and_query_page_boundaries(grouped):
    config = CONFIG if grouped else MatchConfig(0.9, 100)
    storage, client, _service = setup()
    known = [append(storage).data_id for _ in range(131)]
    subject_id = append(storage).data_id
    owners = [new_id() for _ in known]
    no_refs = new_id()
    client.write(
        [
            Create("person", owner, {"references": [source]})
            for owner, source in zip(owners, known, strict=True)
        ]
        + [Create("person", no_refs, {"references": []})],
        Context.system(),
    ).check()
    with client.snapshot() as snap:
        subject = snap.get(subject_id)
        assert isinstance(subject, RawRecord)
        reader = Mock(spec=Reader, wraps=snap)
        result = match(reader, subject, config, new_id())
        assert (result.diagnostic is not None) == grouped
        assert result.target_id == min(owners)
        assert [d.id for d in result.dependencies] == sorted([*owners, no_refs])
        assert {d.id: d.fields for d in result.dependencies} == {
            **dict.fromkeys(owners, ("name", "references") if grouped else ("references",)),
            no_refs: ("references",),
        }
        assert [len(c.args[0]) for c in reader.get_many.call_args_list] == [32, 32, 32, 32, 3]
        assert {i for c in reader.get_many.call_args_list for i in c.args[0]} == set(known)
        reader.get.assert_not_called()
        # The explicit owner must still win against every equally good candidate.
        explicit = snap.get(known[-1])
        assert isinstance(explicit, RawRecord)
        direct = match(snap, explicit, config, new_id())
        assert direct.target_id == owners[-1]
        assert direct.diagnostic is None
        assert all(d.fields == ("references",) for d in direct.dependencies)


@pytest.mark.parametrize(
    ("second", "code"),
    [
        ("absent", "invalid_subject"),
        ("wrong_type", "reference_type_mismatch"),
        ("shared", "reference_owner_conflict"),
    ],
)
def test_bulk_read_does_not_treat_bad_references_as_no_match(second, code):
    storage, client, _service = setup()
    first = append(storage, payload=voice()).data_id
    other = {
        "absent": new_id(),
        "wrong_type": append(storage, "gps_point").data_id,
        "shared": first,
    }[second]
    subject_id = append(storage).data_id
    seed = new_id()
    client.write([Create("person", seed, {"references": [first]})], Context.system()).check()
    entity = client.get(seed)
    assert isinstance(entity, SemanticRecord)
    # Simulate a corrupt/unavailable source view without bypassing production writes.
    candidates = (
        entity,
        entity.model_copy(update={"id": new_id(), "payload": {"references": [other]}}),
    )
    with client.snapshot() as snap:
        subject = present(snap.get(subject_id))
        assert isinstance(subject, RawRecord)
        reader = Mock(spec=Reader, wraps=snap)
        reader.query.return_value = Page(candidates, None)
        with pytest.raises(DomainError, match=f"^{code}$"):
            match(reader, subject, CONFIG, new_id())
        reader.get.assert_not_called()


@pytest.mark.parametrize("later_error", ["invalid_candidate", "denied"])
def test_earlier_missing_reference_is_reported_before_later_error(later_error):
    storage, client, _service = setup()
    lost = new_id()
    existing = append(storage).data_id
    owner = new_id()
    client.write([Create("person", owner, {"references": [existing]})], Context.system()).check()
    entity = client.get(owner)
    subject = client.get(existing)
    assert isinstance(entity, SemanticRecord) and isinstance(subject, RawRecord)
    bad_first = entity.model_copy(update={"payload": {"references": [lost]}})
    bad_later = entity.model_copy(update={"id": new_id(), "payload": {"references": False}})
    other = entity.model_copy(update={"id": new_id()})
    with client.snapshot() as snap:
        reader = Mock(spec=Reader, wraps=snap)
        reader.query.return_value = Page(
            (bad_first, bad_later if later_error == "invalid_candidate" else other), None
        )
        if later_error == "denied":
            reader.get_many.side_effect = PermissionDenied("access denied")
        with pytest.raises(DomainError, match=r"^invalid_subject$"):
            match(reader, subject, CONFIG, new_id())
        if later_error == "denied":
            reader.get.assert_called_once_with(lost)


@pytest.mark.parametrize("fallback", [PermissionDenied, None])
def test_permission_fallback_respects_batch_order_and_does_not_mask_snapshot_loss(fallback):
    from ear_storage_client.remote_errors import SnapshotUnavailable

    storage, client, _service = setup()
    first, second, query = (append(storage).data_id for _ in range(3))
    client.write(
        [Create("person", new_id(), {"references": [first, second]})], Context.system()
    ).check()
    with client.snapshot() as snap:
        subject = snap.get(query)
        assert isinstance(subject, RawRecord)
        reader = Mock(spec=Reader, wraps=snap)
        if fallback is None:
            reader.get_many.side_effect = SnapshotUnavailable("expired synthetic snapshot")
            with pytest.raises(SnapshotUnavailable):
                match(reader, subject, CONFIG, new_id())
            reader.get.assert_not_called()
        else:
            reader.get_many.side_effect = fallback("batch denied")
            reader.get.side_effect = [snap.get(first), PermissionDenied("second denied")]
            with pytest.raises(PermissionDenied, match="second denied"):
                match(reader, subject, CONFIG, new_id())
            assert [c.args[0] for c in reader.get.call_args_list] == [first, second]


@pytest.mark.parametrize("aggregation,top_k", [("max", None), ("top_k_mean", 2)])
def test_production_aggregation_combines_references_for_one_person(aggregation, top_k):
    storage, client, _service = setup()
    config = MatchConfig(0.8, 100, 0.0, 0.8, 0.0, aggregation, top_k, True)
    a = [append(storage, payload=voice(v)).data_id for v in [(1, 0), (0.6, 0.8)]]
    b = append(storage, payload=voice((0.9, 0.43589))).data_id
    query = append(storage).data_id
    first, second = new_id(), new_id()
    client.write(
        [Create("person", first, {"references": a}), Create("person", second, {"references": [b]})],
        Context.system(),
    ).check()
    with client.snapshot() as snap:
        subject = snap.get(query)
        assert isinstance(subject, RawRecord)
        result = match(snap, subject, config, new_id())
    assert result.target_id == (first if aggregation == "max" else second)
    diagnostic = present(result.diagnostic)
    assert diagnostic.accepted
    assert len(diagnostic.ranked) == 2
    assert diagnostic.margin == pytest.approx(0.1, abs=1e-6)
