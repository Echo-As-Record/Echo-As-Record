"""有界显式导入：无列表、无真实源/音频/服务；所有数据合成。"""

from unittest.mock import Mock

import pytest
from ear_adapter import AdapterConfig, AlreadyIngested, Failed, Ingested, Pending, Runner
from ear_adapter.runtime import READ_TYPES
from ear_adapter.selection import InvalidCaptureSelection, capture_selection
from ear_adapter.source import SourceProtocolError
from ear_adapter.testing import FakeLocator, FakeSource, evidence_json
from ear_contracts import DataChanged
from ear_storage_client import MemoryStorage, PermissionDenied
from ear_storage_client.remote_errors import ServiceUnavailable


def evidence(ident):
    data = evidence_json(ident + "-run")
    data["capture_id"] = data["evidence"][0]["payload"]["capture_id"] = ident
    data["evidence"][0]["payload"]["record_id"] = ident + "-record"
    return data


class SelectedSource(FakeSource):
    def __init__(self):
        super().__init__({ident: evidence(ident) for ident in ("a", "b", "c")})
        self.reads = []

    def list_captures(self):
        raise AssertionError("explicit selection must never enumerate")

    def read_capture(self, capture_id):
        self.reads.append(capture_id)
        return super().read_capture(capture_id)


def runner(source, storage, locator=None):
    return Runner(
        source=source,
        storage=storage,
        locator=locator if locator is not None else FakeLocator(),
        config=AdapterConfig(),
    )


@pytest.mark.parametrize("ids", [["a"], ["b", "a", "b"]])
def test_explicit_selection_without_list_restart_idempotent(ids):
    source = SelectedSource()
    memory = MemoryStorage()
    client = memory.client("adapter")
    locator = Mock(wraps=FakeLocator())
    selected = list(dict.fromkeys(ids))
    r = runner(source, client, locator)
    first = r.run_once(capture_ids=ids)
    assert [o.capture_id for o in first.outcomes] == selected
    assert all(isinstance(o, Ingested) for o in first.outcomes)
    assert source.reads == selected
    assert all(isinstance(event, DataChanged) for event in memory.events)
    batches = {event.batch_id for event in memory.events if isinstance(event, DataChanged)}
    assert len(batches) == len(selected)
    events, embedding_reads, audio_reads = (
        len(memory.events),
        source.embedding_reads,
        locator.locate.call_count,
    )
    assert r.run_once(capture_ids=ids).outcomes == []
    assert source.reads == selected
    second = runner(source, client, locator).run_once(capture_ids=ids)
    assert [o.capture_id for o in second.outcomes] == selected
    assert all(isinstance(o, AlreadyIngested) for o in second.outcomes)
    assert source.reads == selected * 2
    assert len(memory.events) == events
    assert source.embedding_reads == embedding_reads
    assert locator.locate.call_count == audio_reads


@pytest.mark.parametrize(
    "selection",
    [
        [],
        (),
        "a",
        b"a",
        [""],
        [" "],
        ["\t"],
        ["."],
        [".."],
        ["/tmp/x"],
        ["x/y"],
        ["x\\y"],
        ["a\x00b"],
        ["a\x7fb"],
        ["a\u202eb"],
        [None],
        [1],
        ["ok", "bad\nsentinel-private-body"],
    ],
)
def test_invalid_selection_before_any_io(selection):
    source, storage, locator = Mock(), Mock(), Mock()
    with pytest.raises(InvalidCaptureSelection) as exc:
        runner(source, storage, locator).run_once(capture_ids=selection)
    assert "sentinel-private-body" not in str(exc.value)
    assert not source.mock_calls and not storage.mock_calls and not locator.mock_calls


def test_none_means_scan_and_selection_is_stable():
    assert capture_selection(None) is None
    assert capture_selection(["b", "a", "b"]) == ("b", "a")
    source = Mock(wraps=FakeSource({"a": evidence("a")}))
    runner(source, MemoryStorage().client("adapter")).run_once()
    source.list_captures.assert_called_once()


def test_selection_frozen_before_preflight():
    ids = ["a"]
    local = MemoryStorage().client("adapter")
    storage = Mock(wraps=local)

    def query(value):
        ids[:] = ["c"]
        return local.query(value)

    storage.query.side_effect = query
    source = SelectedSource()
    runner(source, storage).run_once(capture_ids=ids)
    assert source.reads == ["a"]


@pytest.mark.parametrize("denied_type", READ_TYPES)
def test_every_read_permission_precedes_source(denied_type):
    local = MemoryStorage().client("adapter")
    storage = Mock(wraps=local)
    source, locator = Mock(), Mock()

    def query(value):
        if value.data_type == denied_type:
            raise PermissionDenied("sentinel-private-body")
        return local.query(value)

    storage.query.side_effect = query
    with pytest.raises(PermissionDenied, match="read_types"):
        runner(source, storage, locator).run_once(capture_ids=["a"])
    assert not source.mock_calls and not locator.mock_calls
    storage.get.assert_not_called()
    storage.write.assert_not_called()


def test_unavailable_storage_precedes_source():
    storage, source = Mock(), Mock()
    storage.query.side_effect = ServiceUnavailable()
    assert runner(source, storage).run_once(capture_ids=["a"]).storage_error
    assert not source.mock_calls


def test_pending_retries_only_selected_ids():
    source = SelectedSource()
    r = runner(source, MemoryStorage().client("adapter"))
    source.down = True
    assert isinstance(r.run_once(capture_ids=["a", "a"]).outcomes[0], Pending)
    assert not r.done
    source.down = False
    assert isinstance(r.run_once(capture_ids=["a"]).outcomes[0], Ingested)
    assert source.reads == ["a", "a"]


@pytest.mark.parametrize("analyzable", [True, False])
@pytest.mark.parametrize("outer,inner", [("b", "a"), ("a", "b"), ("b", "b")])
def test_request_outer_inner_match_before_any_other_io(outer, inner, analyzable):
    data = evidence("a")
    data["capture_id"] = outer
    data["evidence"][0]["payload"]["capture_id"] = inner
    data["analyzable"] = analyzable
    source = Mock(wraps=FakeSource({"a": data}))
    storage = Mock(wraps=MemoryStorage().client("adapter"))
    locator = Mock()
    with pytest.raises(SourceProtocolError):
        runner(source, storage, locator).run_once(capture_ids=["a"])
    source.list_captures.assert_not_called()
    source.read_embedding.assert_not_called()
    storage.get.assert_not_called()
    storage.write.assert_not_called()
    locator.locate.assert_not_called()


@pytest.mark.parametrize("count", [0, 2])
def test_capture_count_still_blocked(count):
    data = evidence("a")
    data["evidence"] = [data["evidence"][0]] * count
    locator = Mock()
    report = runner(FakeSource({"a": data}), MemoryStorage().client("adapter"), locator).run_once(
        capture_ids=["a"]
    )
    assert isinstance(report.outcomes[0], Failed)
    locator.locate.assert_not_called()


@pytest.mark.parametrize("where", ["outer", "capture", "speech_segment", "speaker_resolution"])
def test_pydantic_errors_do_not_reveal_body(where):
    data = evidence("a")
    if where == "outer":
        data["started_at"] = "sentinel-private-body"
    else:
        item = next(e for e in data["evidence"] if e["ref"]["type"] == where)
        item["payload"] = {"sentinel-private-body": "sentinel-credential"}
    storage = MemoryStorage()
    with pytest.raises(SourceProtocolError) as exc:
        runner(FakeSource({"a": data}), storage.client("adapter")).run_once(capture_ids=["a"])
    import traceback

    message = "".join(traceback.format_exception(exc.value))
    assert "sentinel" not in message and "ValidationError" not in message
    assert not storage.events


@pytest.mark.parametrize(
    "state,kind", [("failed", Failed), ("partial", Failed), ("missing", Pending)]
)
def test_explicit_mode_does_not_bypass_readiness(state, kind):
    source = SelectedSource()
    source.captures["a"]["availability"][0]["state"] = state
    locator = Mock()
    r = runner(source, MemoryStorage().client("adapter"), locator)
    assert isinstance(r.run_once(capture_ids=["a"]).outcomes[0], kind)
    locator.locate.assert_not_called()
    assert source.embedding_reads == 0
    assert not r.done


def test_stop_does_not_start_next_selected_capture():
    source = SelectedSource()
    r = runner(source, MemoryStorage().client("adapter"))
    report = r.run_once(capture_ids=["a", "b"], should_stop=lambda: bool(source.reads))
    assert [o.capture_id for o in report.outcomes] == ["a"]
    assert isinstance(report.outcomes[0], Ingested)
