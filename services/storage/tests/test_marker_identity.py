"""P1 marker provenance is independent of P2 broker availability."""

import pytest
from ear_contracts import new_id
from ear_storage_client import Context, Create, InvalidRequest, MarkProcessed, PermissionDenied

from .test_auth import env as env

pytestmark = pytest.mark.postgres
SYS = Context.system()


def test_reserved_marker_without_nats_requires_actual_authorized_event(env):
    _, store, service, operator = env
    owner = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    other = operator.issue_principal(
        "extraction", read_types=["activity"], subscribe_types=["activity"]
    )
    name = "extraction.reserved"
    event = (
        store.client("extraction").write([Create("activity", new_id(), {})], SYS).check().events[0]
    )
    operator.reserve_consumer_identity(name, owner.principal_id)
    operator.reserve_consumer_identity(name, owner.principal_id)
    for method in (operator.reserve_consumer_identity, operator.adopt_consumer_markers):
        with pytest.raises(InvalidRequest):
            method(name, other.principal_id)
    client = service.client(owner.token)
    for issued, event_id in [(other, event.event_id), (owner, new_id())]:
        with pytest.raises(PermissionDenied):
            service.client(issued.token).write([MarkProcessed(name, event_id)], SYS)
    client.write([MarkProcessed(name, event.event_id)], SYS).check()
    assert (
        client.write([MarkProcessed(name, event.event_id)], SYS).conflict.reason
        == "already_processed"
    )
    with client.snapshot() as mine, service.client(other.token).snapshot() as foreign:
        assert mine.is_processed(name, event.event_id)
        assert not foreign.is_processed(name, event.event_id)
    operator.set_grants(owner.principal_id, read_types=["activity"])
    with pytest.raises(PermissionDenied):
        client.write([MarkProcessed(name, event.event_id)], SYS)


def test_legacy_read_policy_and_explicit_adoption_do_not_create_progress(env):
    _, store, service, operator = env
    owner = operator.issue_principal("extraction")
    other = operator.issue_principal("extraction")
    foreign = operator.issue_principal("api")
    name, event = "extraction.legacy", new_id()
    store.client("extraction").write([MarkProcessed(name, event)], SYS).check()
    assert service.client(owner.token).is_processed(name, event)
    assert not service.client(foreign.token).is_processed(name, event)
    assert not service.client(owner.token).is_processed(name, new_id())
    with pytest.raises(InvalidRequest):
        operator.reserve_consumer_identity(name, owner.principal_id)
    operator.adopt_consumer_markers(name, owner.principal_id)
    assert service.client(owner.token).is_processed(name, event)
    assert not service.client(other.token).is_processed(name, event)
    with pytest.raises(PermissionDenied):
        service.client(owner.token).write([MarkProcessed(name, new_id())], SYS)
