"""Real PostgreSQL, sync SDK, ASGI unit transport and actual loopback TCP/Uvicorn."""

from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from ear_contracts import Cause, SemanticRecord, VersionRef, new_id
from ear_storage.http import create_app
from ear_storage.schema import connection
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    DependencyInput,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    StorageClient,
    Supersede,
    TimeRange,
    Update,
    write_with_retry,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError, CommitUnknown, SnapshotUnavailable
from ear_storage_client.testing import make_raw
from ear_storage_client.wire import encode_request, encode_result
from fastapi.testclient import TestClient

from .test_auth import env as env
from .test_http import TestTransport
from .test_snapshots import expire
from .test_snapshots import seeded as seeded

pytestmark = pytest.mark.postgres
SYS = Context.system()


@contextmanager
def live_server(service):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service),
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_keep_alive=2,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "loopback HTTP server did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "test HTTP server failed to stop"


def test_every_reader_roundtrip_and_snapshot(seeded):
    dsn, store, service, _operator, issued, source, target, system, empty, event, raw = seeded
    local = service.client(issued.token)
    methods = [
        ("get", (raw.id,), {}),
        ("get", (target,), {}),
        ("get_version", (target, 1), {}),
        ("history", (target,), {}),
        ("get_many", ([raw.id, source, target, new_id()],), {}),
        ("get_system_record", (system,), {}),
        ("system_history", (system,), {}),
        ("query", (Query("activity", limit=1),), {}),
        ("query", (Query("activity", where={"n": 2}, contains={"tags": "yes"}),), {}),
        ("query", (Query("voiceprint", time=TimeRange(raw.event_start, raw.event_end)),), {}),
        ("count", (Query("activity"),), {}),
        ("dependency_set", (target, 1), {}),
        ("dependency_set", (empty,), {}),
        ("dependencies_of", (target,), {}),
        ("dependents_of", (source,), {"only_current_dependents": False}),
        ("stale_dependencies", ([source],), {"only_current_dependents": False}),
        ("is_processed", ("extraction.worker", event), {}),
    ]
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as remote,
    ):
        typed: StorageClient = remote  # Static Protocol conformance checked by pyright.
        assert typed.module == "extraction"
        assert remote.registry == local.registry
        for name, args, kwargs in methods:
            assert getattr(remote, name)(*args, **kwargs) == getattr(local, name)(*args, **kwargs)
        with remote.snapshot() as snap:
            sid = snap.snapshot_id
            for name, args, kwargs in methods:
                assert getattr(snap, name)(*args, **kwargs) == getattr(local, name)(*args, **kwargs)
            with ThreadPoolExecutor(1) as pool:
                pool.submit(
                    store.client("extraction").write,
                    [Update("activity", target, 2, {"n": 99})],
                    SYS,
                ).result().check()
            old, current = snap.get(target), remote.get(target)
            assert old is not None and old.payload["n"] == 2
            assert current is not None and current.payload["n"] == 99
            assert snap.query(Query("activity", limit=1)).next_cursor is not None
        assert sid is not None
        with pytest.raises(SnapshotUnavailable):
            remote.read_snapshot(sid).get(target)
        with connection(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM storage.snapshot_leases").fetchone() == (0,)

        def plan(reader):
            current = reader.get(target)
            assert current is not None
            return [Update("activity", target, current.version, {"n": 100})]

        write_with_retry(remote, plan, SYS).check()
        current = remote.get(target)
        assert current is not None and current.payload["n"] == 100


def test_live_all_commands_fields_numeric_and_request_identity(env):
    _dsn, _store, service, operator = env
    reads = ["activity", "person", "voiceprint", "extraction_run"]
    principals = {
        module: operator.issue_principal(module, read_types=reads)
        for module in ("adapter", "attribution", "extraction", "dependency")
    }
    with live_server(service) as url:
        clients = {module: RemoteStorageClient(p.token, url) for module, p in principals.items()}
        try:
            source, target, system, event = (new_id() for _ in range(4))
            raw = make_raw("voiceprint", {"nested": [1, 1.0, True]}, start="2026-10-30T00:00:00Z")
            clients["adapter"].write([AppendRaw(raw)], SYS).check()
            clients["attribution"].write(
                [Create("person", source, {"x": [1, {"v": 1.0}]})], SYS
            ).check()
            sdk = clients["extraction"]
            ref = VersionRef(data_type="person", id=source, version=1)
            ops = [
                Create("activity", target, {"module": "attribution", "nested": [1, 1.0, True]}),
                RegisterDependencies("activity", target, [DependencyInput(ref, ("x",))]),
                PutSystemRecord("extraction_run", system, {"state": "done"}),
                MarkProcessed("extraction.http", event),
            ]
            rid = new_id()
            result = sdk.write(ops, SYS, request_id=rid).check()
            assert sdk.write(ops, SYS, request_id=rid) == result
            record = sdk.get(target)
            assert isinstance(record, SemanticRecord) and record.writer == "extraction"
            assert [type(x) for x in record.payload["nested"]] == [int, float, bool]
            group = sdk.dependency_set(target)
            assert group is not None and group.dependencies[0].fields == ("x",)
            clients["attribution"].write(
                [Update("person", source, 1, {"x": [1.0, {"v": 1.0}]})], SYS
            ).check()
            assert sdk.stale_dependencies([source])[0].fields == ("x",)
            conflict = sdk.write(
                [RegisterDependencies("activity", target, [DependencyInput(ref, ("x",))])], SYS
            )
            assert conflict.conflict is not None and conflict.conflict.reason == "stale_dependency"
            with pytest.raises(InvalidRequest):
                sdk.write([Update("activity", target, 1, {})], SYS, request_id=rid)
            clients["dependency"].write(
                [
                    PublishReevaluation(
                        "activity",
                        target,
                        Cause(data_type="person", data_id=source, old_version=1, new_version=2),
                        "synthetic change",
                    )
                ],
                SYS.deeper(),
            ).check()
            sdk.write(
                [
                    Update("activity", target, 1, {"n": 2}),
                    PutSystemRecord("extraction_run", system, {"state": "updated"}, 1),
                ],
                SYS,
            ).check()
            sdk.write([Supersede("activity", target, 2)], SYS).check()
            current = sdk.get(target)
            assert isinstance(current, SemanticRecord) and current.superseded
            assert len(sdk.system_history(system)) == 2
            assert sdk.is_processed("extraction.http", event)
        finally:
            for client in clients.values():
                client.close()


class LoseCommittedResponse(httpx.BaseTransport):
    """Client-side fault-injection proxy, forwarding to a REAL TCP HTTP transport."""

    def __init__(self):
        self.inner = httpx.HTTPTransport(retries=0)
        self.lost = None
        self.replay = None

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        response.read()
        if request.url.path == "/v1/batches":
            if self.lost is None:
                assert response.status_code == 200
                self.lost = response.content
                response.close()
                raise httpx.ReadError(
                    "simulated connection loss after server committed", request=request
                )
            self.replay = response.content
        return response

    def close(self):
        self.inner.close()


def test_live_lost_commit_same_id_replay_exact_no_duplicate_events(env):
    _dsn, store, service, operator = env
    issued = operator.issue_principal("extraction", read_types=["activity"])
    proxy = LoseCommittedResponse()
    with (
        live_server(service) as url,
        RemoteStorageClient(issued.token, url, transport=proxy) as sdk,
    ):
        rid, id = new_id(), new_id()
        ops = [Create("activity", id, {"synthetic": True})]
        with pytest.raises(CommitUnknown) as exc:
            sdk.write(ops, SYS, request_id=rid)
        assert exc.value.request_id == rid
        seq = store.commit_seq
        assert store.get(id) is not None
        receipt = sdk.write(ops, SYS, request_id=rid).check()
        assert proxy.lost == proxy.replay == encode_result(receipt).encode()
        assert store.commit_seq == seq
        assert len([event for _, events in store.outbox() for event in events]) == 1
        assert len(sdk.history(id)) == 1


def test_permissions_no_existence_or_identity_spoof_and_revoked_cache(seeded):
    _dsn, _store, service, operator, _issued, source, target, system, _empty, event, _raw = seeded
    issued = operator.issue_principal("extraction")
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
    ):
        assert sdk.module == "extraction"
        assert "person" not in sdk.registry.data_types
        assert sdk.get(target) == sdk.get(new_id()) is None
        assert sdk.get_version(target, 1) is None
        assert sdk.history(target) == []
        assert sdk.get_many([source, target]) == {}
        assert sdk.get_system_record(system) is None
        assert sdk.system_history(system) == []
        assert sdk.dependency_set(target) is None
        assert sdk.dependencies_of(target) == []
        assert sdk.dependents_of(source) == []
        assert sdk.stale_dependencies([source]) == []
        assert not sdk.is_processed("attribution", event)
        for query in (Query("person"), Query("nonexistent"), Query("activity")):
            with pytest.raises(PermissionDenied):
                sdk.query(query)
            with pytest.raises(PermissionDenied):
                sdk.count(query)
        with pytest.raises(PermissionDenied):
            sdk.write([Create("person", new_id(), {"module": "attribution"})], SYS)
        value = json.loads(encode_request([], SYS))
        value.update(request_id=new_id(), module="attribution")
        response = http.post(
            "/v1/batches", json=value, headers={"Authorization": "Bearer " + issued.token}
        )
        assert response.status_code == 422
        operator.revoke(issued.principal_id)
        for operation in (
            lambda: sdk.module,
            lambda: sdk.registry,
            lambda: sdk.get(target),
            lambda: sdk.write([], SYS),
        ):
            with pytest.raises(AuthenticationError):
                operation()


def test_snapshot_expiry_foreign_owner_grant_revoke_and_credential_revoke(seeded):
    dsn, _store, service, operator, issued, _source, target, _system, _empty, _event, _raw = seeded
    other = operator.issue_principal("extraction", read_types=["activity"])
    with (
        TestClient(create_app(service)) as http,
        RemoteStorageClient(issued.token, "http://test", transport=TestTransport(http)) as sdk,
        RemoteStorageClient(other.token, "http://test", transport=TestTransport(http)) as foreign,
    ):
        lease = sdk.create_snapshot()
        sid = lease["snapshot_id"]
        assert sdk.read_snapshot(sid).get(target) is not None
        with pytest.raises(SnapshotUnavailable):
            foreign.read_snapshot(sid).get(target)
        operator.set_grants(issued.principal_id, read_types=[])
        with pytest.raises(PermissionDenied):
            sdk.read_snapshot(sid).get(target)
        operator.set_grants(issued.principal_id, read_types=["activity"])
        expire(dsn, sid)
        with pytest.raises(SnapshotUnavailable, match="snapshot unavailable"):
            sdk.read_snapshot(sid).get(target)
        with sdk.snapshot() as snap:
            operator.revoke(issued.principal_id)
            with pytest.raises(AuthenticationError):
                snap.get(target)
