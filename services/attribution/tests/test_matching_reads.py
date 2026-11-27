"""Bound the reference reads without changing matching decisions or dependencies."""

from unittest.mock import Mock

import pytest
from ear_attribution.matching import match
from ear_attribution.payloads import DomainError
from ear_contracts import RawRecord, SemanticRecord, new_id
from ear_storage_client import Context, Create, Page, PermissionDenied, Reader

from .support import CONFIG, append, present, setup, voice


def test_batch_reads_cross_candidate_and_query_page_boundaries():
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
        result = match(reader, subject, CONFIG, new_id())
        assert result.target_id == min(owners)
        assert [d.id for d in result.dependencies] == sorted([*owners, no_refs])
        assert all(d.fields == ("references",) for d in result.dependencies)
        assert [len(c.args[0]) for c in reader.get_many.call_args_list] == [32, 32, 32, 32, 3]
        assert {i for c in reader.get_many.call_args_list for i in c.args[0]} == set(known)
        reader.get.assert_not_called()
        # The explicit owner must still win against every equally good candidate.
        explicit = snap.get(known[-1])
        assert isinstance(explicit, RawRecord)
        assert match(snap, explicit, CONFIG, new_id()).target_id == owners[-1]


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
