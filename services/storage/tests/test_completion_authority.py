"""Authoritative receipts, ordinary exact names, grant revocation and legacy leases."""

from contextlib import nullcontext
from typing import LiteralString, cast

import pytest
from ear_contracts import DataTypeSpec, new_id
from ear_storage import PostgresStorage, bootstrap, migrate, schema
from ear_storage.auth import AuthOperator
from ear_storage.schema import connection, writer
from ear_storage.service import StorageService
from ear_storage.snapshots import SnapshotUnavailable
from ear_storage_client import (
    Create,
    InvalidRequest,
    MemoryStorage,
    PermissionDenied,
    PutSystemRecord,
    reevaluation_result,
)
from ear_storage_client.remote import RemoteReader, RemoteStorageClient
from ear_storage_client.remote_errors import SnapshotUnavailable as RemoteSnapshotUnavailable

from .test_completion import CTX, SYS, capable_registry, deps, finish, mark, publish
from .test_completion import setup as setup
from .test_http_postgres import live_server


@pytest.mark.parametrize("mode", ["memory", "pg", "http"])
def test_shape_valid_ordinary_exact_name_is_not_completion(request, mode):
    registry = capable_registry().extend(
        DataTypeSpec("sample_completion", "system_record", "sample")
    )
    issued = None
    if mode == "memory":
        store = MemoryStorage(registry)
        manager = nullcontext(store.client("sample"))
        server = nullcontext(None)
    else:
        dsn = request.getfixturevalue("pg_dsn")
        migrate(dsn)
        bootstrap(dsn, registry)
        store = PostgresStorage(dsn)
        service = StorageService(dsn)
        issued = AuthOperator(dsn).issue_principal("sample", read_types=["sample_completion"])
        server = live_server(service) if mode == "http" else nullcontext(None)
        manager = nullcontext(service.client(issued.token))
    event, target = new_id(), new_id()
    payload = finish(event, target).result.model_dump(mode="json")
    with server as url:
        if mode == "http":
            assert issued is not None and url is not None
            manager = RemoteStorageClient(issued.token, url)
        with manager as client:
            client.write([PutSystemRecord("sample_completion", event, payload)], SYS).check()
            record = client.get_system_record(event)
            assert record is not None and record.payload == payload
            assert reevaluation_result(client, event) is None
            assert store.client("sample").get_reevaluation_result(event) is None
            with client.snapshot() as snap:
                assert snap.get_system_record(event) is not None
                assert snap.get_reevaluation_result(event) is None
            client.write(
                [PutSystemRecord("sample_completion", event, {"ordinary": True}, 1)], SYS
            ).check()
            assert reevaluation_result(client, event) is None


def test_reader_without_authority_never_falls_back_to_raw():
    class LegacyReader:
        def get_system_record(self, id):
            pytest.fail("raw system record is not completion authority")

    with pytest.raises(InvalidRequest, match="authoritative completion reads unavailable"):
        reevaluation_result(LegacyReader(), new_id())  # type: ignore[arg-type]


def test_finish_snapshot_and_grant_revocation(setup):
    store, client, target, source, operator, issued, dsn = setup
    event = publish(store, target, source)
    op = finish(event, target)
    with client.snapshot() as before:
        assert before.get_reevaluation_result(event) is None
        client.write([op, deps(target), mark(event)], CTX).check()
        assert client.get_reevaluation_result(event) == op.result
        assert store.client("extraction").get_reevaluation_result(event) == op.result
        assert before.get_reevaluation_result(event) is None
        with client.snapshot() as after:
            assert after.get_reevaluation_result(event) == op.result
            if operator is not None:
                direct = StorageService(dsn).client(issued.token)
                assert direct.get_reevaluation_result(event) == op.result
                operator.set_grants(issued.principal_id, read_types=["artifact", "rule"])
                assert direct.get_reevaluation_result(event) is None
                assert client.get_reevaluation_result(event) is None
                with pytest.raises(PermissionDenied):
                    after.get_reevaluation_result(event)
                with client.snapshot() as hidden:
                    assert hidden.get_reevaluation_result(event) is None
                    with connection(dsn) as conn:
                        assert conn.execute(
                            "SELECT count(*) FROM storage.snapshot_items WHERE snapshot_id=%s "
                            "AND kind='completion'",
                            (hidden.snapshot_id,),
                        ).fetchone() == (0,)


@pytest.mark.postgres
@pytest.mark.parametrize("version", [8, 9])
def test_legacy_lease_cannot_report_false_absence(pg_dsn, monkeypatch, version):
    with connection(pg_dsn) as conn, writer(conn):
        for revision in range(1, version + 1):
            for statement in getattr(schema, "DDL" if revision == 1 else f"DDL_V{revision}"):
                conn.execute(cast(LiteralString, statement))
        conn.execute("INSERT INTO storage.meta VALUES (true,%s,0)", (version,))
    with monkeypatch.context() as patch:
        patch.setattr(schema, "SCHEMA_VERSION", version)
        bootstrap(pg_dsn, capable_registry())
        store = PostgresStorage(pg_dsn)
        target, source = new_id(), new_id()
        store.client("extraction").write(
            [Create("artifact", target, {}), Create("rule", source, {})], SYS
        ).check()
        event = publish(store, target, source)
        issued = AuthOperator(pg_dsn).issue_principal(
            "extraction", read_types=["artifact", "rule", "extraction_completion"]
        )
        lease = StorageService(pg_dsn).create_snapshot(issued.token)
    migrate(pg_dsn)
    service, store = StorageService(pg_dsn), PostgresStorage(pg_dsn)
    old = service.client(issued.token).read_snapshot(lease.snapshot_id)
    record = old.get(target)
    assert record is not None and record.version == 1
    assert old.get_system_record(event) is None
    with pytest.raises(SnapshotUnavailable, match="lacks completion index"):
        old.get_reevaluation_result(event)
    op = finish(event, target)
    store.client("extraction").write([op, deps(target), mark(event)], CTX).check()
    with live_server(service) as url, RemoteStorageClient(issued.token, url) as client:
        remote_old = RemoteReader(client, lease.snapshot_id)
        record = remote_old.get(target)
        assert record is not None and record.version == 1
        assert remote_old.get_system_record(event) is None
        with pytest.raises(RemoteSnapshotUnavailable, match="snapshot unavailable"):
            reevaluation_result(remote_old, event)
        assert client.get_reevaluation_result(event) == op.result
        with client.snapshot() as fresh:
            assert fresh.get_reevaluation_result(event) == op.result
            # Live metadata disappearance must not change the captured receipt.
            with connection(pg_dsn) as conn:
                conn.execute("DELETE FROM storage.request_completions WHERE event_id=%s", (event,))
            assert client.get_system_record(event) is not None
            assert client.get_reevaluation_result(event) is None
            assert fresh.get_reevaluation_result(event) == op.result
