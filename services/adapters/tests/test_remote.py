"""远程错误策略的离线回归；不使用网络或 Docker。"""

from unittest.mock import Mock

import pytest
from ear_adapter import AdapterConfig, AlreadyIngested, Failed, Ingested, Pending, Runner
from ear_adapter.runtime import READ_TYPES
from ear_adapter.testing import FakeLocator, FakeSource, evidence_json
from ear_storage_client import MemoryStorage, PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown, ResourceLimit, ServiceUnavailable


def runner(storage):
    return Runner(
        source=FakeSource({"cap-1": evidence_json()}),
        storage=storage,
        locator=FakeLocator(),
        config=AdapterConfig(),
    )


def test_commit_unknown_replays_same_objects(monkeypatch):
    memory = MemoryStorage()
    local = memory.client("adapter")
    remote = Mock(spec=RemoteStorageClient)
    remote.get.side_effect = local.get
    remote.query.side_effect = local.query
    calls = []
    receipt = None

    def write(ops, context, *, request_id):
        nonlocal receipt
        calls.append((ops, context, request_id))
        if receipt is None:
            receipt = local.write(ops, context)
            raise CommitUnknown(request_id)
        assert ops is calls[0][0] and context is calls[0][1]
        assert request_id == calls[0][2]
        return receipt

    remote.write.side_effect = write
    monkeypatch.setattr("ear_adapter.runtime.time.sleep", lambda _: None)
    assert isinstance(runner(remote).run_once().outcomes[0], Ingested)
    assert len(calls) == 2 and calls[0][2]
    events = len(memory.events)
    assert isinstance(runner(remote).run_once().outcomes[0], AlreadyIngested)
    assert len(memory.events) == events == 13


@pytest.mark.parametrize(
    "error,kind",
    [
        (ServiceUnavailable(), Pending),
        (ResourceLimit(), Failed),
        (CommitUnknown("receipt"), Pending),
    ],
)
def test_remote_write_errors(error, kind, monkeypatch):
    local = MemoryStorage().client("adapter")
    remote = Mock(spec=RemoteStorageClient)
    remote.get.side_effect = local.get
    remote.query.side_effect = local.query
    remote.write.side_effect = error
    sleeps = []
    monkeypatch.setattr("ear_adapter.runtime.time.sleep", sleeps.append)
    r = runner(remote)
    outcome = r.run_once().outcomes[0]
    assert isinstance(outcome, kind)
    assert not r.done
    if isinstance(error, CommitUnknown):
        assert remote.write.call_count == 3
        assert [c.kwargs["request_id"] for c in remote.write.call_args_list][1:] == ["receipt"] * 2
        assert sleeps == [0.25, 0.5]
    if isinstance(error, ResourceLimit):
        assert isinstance(outcome, Failed)
        assert "字节" in outcome.reason and "不拆批" in outcome.reason


def test_read_grants_fail_before_source_or_write():
    remote = Mock(spec=RemoteStorageClient)
    remote.query.side_effect = PermissionDenied("access denied")
    with pytest.raises(PermissionDenied, match="read_types"):
        runner(remote).run_once()
    remote.get.assert_not_called()
    remote.write.assert_not_called()
    assert len(READ_TYPES) == 7


def test_storage_unavailable_before_scan():
    remote = Mock(spec=RemoteStorageClient)
    remote.query.side_effect = ServiceUnavailable()
    assert runner(remote).run_once().storage_error
    remote.write.assert_not_called()


def test_stop_finishes_current_capture():
    local = MemoryStorage().client("adapter")
    r = runner(local)
    r.source = FakeSource({"cap-1": evidence_json(), "cap-2": evidence_json()})
    calls = 0

    def stop():
        nonlocal calls
        calls += 1
        return calls > 1

    assert len(r.run_once(should_stop=stop).outcomes) == 1


def test_unknown_exhausted_then_next_round_finds_commit(monkeypatch):
    memory = MemoryStorage()
    local = memory.client("adapter")
    remote = Mock(spec=RemoteStorageClient)
    remote.get.side_effect = local.get
    remote.query.side_effect = local.query
    calls = []

    def write(ops, context, *, request_id):
        calls.append((ops, context, request_id))
        if len(calls) == 1:
            local.write(ops, context).check()
        else:
            assert ops is calls[0][0] and context is calls[0][1]
            assert request_id == calls[0][2]
        raise CommitUnknown(request_id)

    remote.write.side_effect = write
    monkeypatch.setattr("ear_adapter.runtime.time.sleep", lambda _: None)
    r = runner(remote)
    assert isinstance(r.run_once().outcomes[0], Pending)
    assert not r.done
    assert isinstance(r.run_once().outcomes[0], AlreadyIngested)
    assert len(calls) == 3 and len(memory.events) == 13


def test_storage_get_unavailable_recovers_next_round():
    local = MemoryStorage().client("adapter")
    remote = Mock(spec=RemoteStorageClient)
    remote.query.side_effect = local.query
    remote.get.side_effect = ServiceUnavailable()
    r = runner(remote)
    assert isinstance(r.run_once().outcomes[0], Pending)
    remote.write.assert_not_called()
    remote.get.side_effect = local.get
    remote.write.side_effect = lambda ops, context, **kwargs: local.write(ops, context)
    assert isinstance(r.run_once().outcomes[0], Ingested)
