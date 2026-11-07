"""Synthetic security regressions; no model/network/integration opt-ins."""

import os
import sqlite3
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from ear_contracts import LLMCallRecord, new_id
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.models import GatewayError, ProviderResult
from ear_storage_client import AppendRaw, Context, Create, PermissionDenied, PutSystemRecord
from ear_storage_client.protocol import StorageClient
from ear_storage_client.testing import make_raw

from .test_gateway import HEADERS, Harness


def row_for(journal, call_id, module):
    row = journal.get(call_id, module)
    assert row is not None
    return row


def seed(h, kind, call_id):
    if kind == "raw":
        h.store.client("adapter").write(
            [AppendRaw(make_raw("gps_point", {}, start=datetime.now(UTC), id=call_id))],
            Context.system(),
        ).check()
    elif kind == "system":
        h.store.client("extraction").write(
            [PutSystemRecord("extraction_run", call_id, {})], Context.system()
        ).check()
    else:
        h.store.client("extraction").write(
            [Create("rule" if kind == "rule" else "artifact", call_id, {})], Context.system()
        ).check()


@pytest.mark.parametrize("kind", ["raw", "semantic", "rule", "system"])
@pytest.mark.parametrize("hidden", [False, True])
def test_all_id_classes_rejected_before_provider(tmp_path, kind, hidden):
    h = Harness(tmp_path / "audit.db")
    try:
        call = h.call()
        seed(h, kind, call["call_id"])
        if hidden:

            class FilteredWriter:
                def get(self, _id):
                    return None

                def get_system_record(self, _id):
                    return None

                def __getattr__(self, name):
                    return getattr(h.writer, name)

            h.gateway.writer = cast(StorageClient, FilteredWriter())
        response = h.post(call)
        assert response.status_code == 409
        assert not h.provider.calls
        assert h.journal.get(call["call_id"], "extraction") is None
    finally:
        h.close()


def test_missing_trusted_check_fails_closed(tmp_path):
    h = Harness(tmp_path / "audit.db")
    try:

        class OldWriter:
            call_id_occupied = None

            def __getattr__(self, name):
                return getattr(h.writer, name)

        h.gateway.writer = cast(StorageClient, OldWriter())
        assert h.post().status_code == 503
        assert not h.provider.calls
    finally:
        h.close()


def test_collision_race_quarantines_without_losing_audit_or_blocking_recovery(tmp_path, caplog):
    h = Harness(tmp_path / "audit.db")
    try:
        call = h.call()

        def racing_provider(*_args, **_kwargs):
            seed(h, "semantic", call["call_id"])
            return ProviderResult({"answer": "synthetic"})

        h.provider.complete = racing_provider
        response = h.post(call)
        assert response.status_code == 409
        assert response.json()["code"] == "record_conflict"
        row = row_for(h.journal, call["call_id"], "extraction")
        assert row["phase"] == "quarantined"
        assert LLMCallRecord.model_validate_json(row["record"]).status == "success"
        assert "SENSITIVE_PROMPT" not in row["record"]
        assert h.gateway.recover_pending() == 0
        response = h.client.post(f"/v1/calls/{call['call_id']}/reconcile", headers=HEADERS)
        assert response.status_code == 409
        assert "quarantin" in caplog.text
        h.journal.close()
        h.journal = Journal(str(tmp_path / "audit.db"))
        h.gateway.journal = h.journal
        assert h.gateway.recover_pending() == 0
        assert row_for(h.journal, call["call_id"], "extraction")["record"] == row["record"]
    finally:
        h.close()


def test_pending_cas_failure_never_returns_success(tmp_path):
    h = Harness(tmp_path / "audit.db")
    try:
        call = h.call()

        def lost_owner(*_args, **_kwargs):
            with h.journal._transaction() as conn:
                conn.execute("UPDATE calls SET phase='pending' WHERE id=?", (call["call_id"],))
            return ProviderResult({"answer": "synthetic"})

        h.provider.complete = lost_owner
        assert h.post(call).status_code == 503
        assert h.writer.get_system_record(call["call_id"]) is None
    finally:
        h.close()


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_no_second_owner(tmp_path, alias):
    path = tmp_path / "audit.db"
    journal = Journal(str(path))
    other = tmp_path / "alias.db"
    try:
        if alias == "symlink":
            other.symlink_to(path)
        elif alias == "hardlink":
            os.link(path, other)
        else:
            other = path
        with pytest.raises(ValueError):
            second = Journal(str(other))
            second.close()
    finally:
        journal.close()


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_unsafe_database_rejected(tmp_path, mode):
    path = tmp_path / "audit.db"
    path.touch(mode=mode)
    path.chmod(mode)
    with pytest.raises(ValueError):
        journal = Journal(str(path))
        journal.close()
    assert path.stat().st_size == 0


def test_unsafe_directory_rejected(tmp_path):
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(ValueError):
        journal = Journal(str(directory / "audit.db"))
        journal.close()
    assert not (directory / "audit.db").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "hardlink"])
def test_unsafe_sidecar_rejected_without_modification(tmp_path, suffix, unsafe):
    path = tmp_path / "audit.db"
    sidecar = tmp_path / (path.name + suffix)
    target = tmp_path / "target"
    target.write_bytes(b"synthetic sentinel")
    target.chmod(0o600)
    if unsafe == "symlink":
        sidecar.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(target, sidecar)
    else:
        sidecar.write_bytes(b"synthetic sentinel")
        sidecar.chmod(0o644)
    with pytest.raises(ValueError):
        journal = Journal(str(path))
        journal.close()
    assert target.read_bytes() == b"synthetic sentinel"
    assert sidecar.read_bytes() == b"synthetic sentinel"


def test_init_failure_releases_inode_lock(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.db")
    original = sqlite3.connect

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic")

    monkeypatch.setattr(sqlite3, "connect", broken)
    with pytest.raises((ValueError, sqlite3.Error)):
        Journal(path)
    monkeypatch.setattr(sqlite3, "connect", original)
    journal = Journal(path)
    journal.close()
    journal.close()


def test_trusted_check_restricted_to_gateway(tmp_path):
    h = Harness(tmp_path / "audit.db")
    try:
        with pytest.raises(PermissionDenied):
            h.store.client("extraction").call_id_occupied(new_id())
    finally:
        h.close()


def test_saved_transition_cas(tmp_path):
    journal = Journal(str(tmp_path / "audit.db"))
    try:
        with pytest.raises(GatewayError):
            journal.saved(new_id())
    finally:
        journal.close()


def test_legacy_pending_collision_does_not_block_healthy_audit(tmp_path):
    h = Harness(tmp_path / "audit.db")
    try:
        now = datetime.now(UTC)
        record = LLMCallRecord(
            caller_module="extraction",
            purpose="recovery",
            model="synthetic",
            root_feedback_id="system",
            depth=0,
            started_at=now,
            ended_at=now,
            status="failed",
            error_code="provider_outcome_unknown",
        )
        bad, good = new_id(), new_id()
        for cid in (bad, good):
            h.journal.reserve(cid, record, global_rpm=5, caller_rpm=5)
            h.journal.pending(cid, record)
        seed(h, "raw", bad)
        before = row_for(h.journal, bad, "extraction")
        assert h.gateway.recover_pending() == 1
        after = row_for(h.journal, bad, "extraction")
        assert after == {**before, "phase": "quarantined"}
        assert row_for(h.journal, good, "extraction")["phase"] == "saved"
        assert not h.provider.calls
    finally:
        h.close()


@pytest.mark.parametrize("change", ["replace", "hardlink", "directory", "sidecar"])
def test_runtime_identity_and_permissions_fail_closed(tmp_path, change):
    h = Harness(tmp_path / "audit.db")
    try:
        if change == "replace":
            (tmp_path / "audit.db").rename(tmp_path / "retained.db")
            (tmp_path / "audit.db").touch(mode=0o600)
        elif change == "hardlink":
            os.link(tmp_path / "audit.db", tmp_path / "alias.db")
        elif change == "directory":
            tmp_path.chmod(0o755)
        else:
            (tmp_path / "audit.db-wal").chmod(0o644)
        assert h.post().status_code == 503
        assert not h.provider.calls
    finally:
        tmp_path.chmod(0o700)
        h.close()


def test_wrong_owner_and_symlink_parent_rejected(tmp_path, monkeypatch):
    path = tmp_path / "audit.db"
    path.touch(mode=0o600)
    with monkeypatch.context() as m:
        m.setattr(os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(ValueError):
            Journal(str(path))
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        Journal(str(alias / "audit.db"))
    journal = Journal(str(path))
    for file in tmp_path.glob("audit.db*"):
        assert file.stat().st_mode & 0o077 == 0
    journal.close()


def test_storage_occupancy_ignores_read_acl_but_checks_live_identity():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from ear_storage.service import AuthenticatedClient

    calls = []

    class FakeClient:
        module = "llm_gateway"

        @contextmanager
        def _session(self):
            def execute(sql, params):
                calls.append((sql, params))
                return SimpleNamespace(fetchone=lambda: (1,))

            yield (
                SimpleNamespace(execute=execute),
                SimpleNamespace(module=self.module, read_types=frozenset()),
                None,
            )

    client = FakeClient()
    cid = new_id()
    assert AuthenticatedClient.call_id_occupied(cast(Any, client), cid)
    assert calls[0][1] == (cid,)
    client.module = "extraction"
    with pytest.raises(PermissionDenied):
        AuthenticatedClient.call_id_occupied(cast(Any, client), cid)
    assert len(calls) == 1


def test_storage_http_occupancy_and_remote_closed_response(tmp_path):
    import httpx
    from ear_storage.http import create_app
    from ear_storage_client import MemoryStorage
    from ear_storage_client.remote import RemoteStorageClient
    from fastapi.testclient import TestClient

    store = MemoryStorage()

    class Service:
        module = "llm_gateway"

        def client(self, _token):
            return store.client(self.module)

    service = Service()
    with TestClient(create_app(cast(Any, service))) as http:

        def transport(request):
            response = http.request(
                request.method, request.url.path, headers=request.headers, content=request.read()
            )
            return httpx.Response(response.status_code, content=response.content, request=request)

        with RemoteStorageClient(
            "a" * 43, "http://testserver", transport=httpx.MockTransport(transport)
        ) as remote:
            cid = new_id()
            assert remote.call_id_occupied(cid) is False
            store.client("extraction").write([Create("rule", cid, {})], Context.system()).check()
            assert remote.call_id_occupied(cid) is True
            service.module = "extraction"
            with pytest.raises(PermissionDenied):
                remote.call_id_occupied(cid)
        service.module = "llm_gateway"
        assert (
            http.post(
                "/v1/llm/call-id-check", headers=HEADERS, json={"call_id": new_id(), "extra": True}
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "body", [{"occupied": 0}, {"occupied": None}, {}, {"occupied": False, "extra": "synthetic"}]
)
def test_remote_occupancy_requires_exact_boolean(body):
    import httpx
    from ear_storage_client.remote import RemoteStorageClient
    from ear_storage_client.remote_errors import ProtocolError

    with (
        RemoteStorageClient(
            "a" * 43,
            "http://testserver",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        ) as remote,
        pytest.raises(ProtocolError),
    ):
        remote.call_id_occupied(new_id())


def test_hidden_competing_llm_call_is_retained_and_quarantined(tmp_path):
    h = Harness(tmp_path / "audit.db")
    try:
        cid = new_id()
        now = datetime.now(UTC)
        record = LLMCallRecord(
            caller_module="extraction",
            purpose="original",
            model="synthetic",
            root_feedback_id="system",
            depth=0,
            started_at=now,
            ended_at=now,
            status="success",
        )
        competitor = record.model_copy(update={"purpose": "competitor"})
        h.writer.write(
            [PutSystemRecord("llm_call", cid, competitor.model_dump(mode="json"))], Context.system()
        ).check()

        class FilteredWriter:
            def get_system_record(self, _id):
                return None

            def __getattr__(self, name):
                return getattr(h.writer, name)

        h.gateway.writer = cast(StorageClient, FilteredWriter())
        h.journal.reserve(cid, record, global_rpm=5, caller_rpm=5)
        h.journal.pending(cid, record)
        assert h.gateway.recover_pending() == 0
        assert row_for(h.journal, cid, "extraction")["phase"] == "quarantined"
        assert (
            LLMCallRecord.model_validate_json(row_for(h.journal, cid, "extraction")["record"])
            == record
        )
        stored = h.writer.get_system_record(cid)
        assert stored is not None and stored.payload["purpose"] == "competitor"
    finally:
        h.close()


def test_unclean_process_exit_recovers_private_wal_without_provider_replay(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "crash.db"
    cid = new_id()
    now = datetime.now(UTC)
    record = LLMCallRecord(
        caller_module="extraction",
        purpose="crash",
        model="synthetic",
        root_feedback_id="system",
        depth=0,
        started_at=now,
        ended_at=now,
        status="failed",
        error_code="provider_outcome_unknown",
    )
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            """
import os, sys
from ear_contracts import LLMCallRecord
from ear_llm_gateway.journal import Journal
journal = Journal(sys.argv[1])
journal.reserve(sys.argv[2], LLMCallRecord.model_validate_json(sys.argv[3]),
                global_rpm=5, caller_rpm=5)
os._exit(0)
""",
            str(path),
            cid,
            record.model_dump_json(),
        ],
        check=True,
    )
    assert (tmp_path / "crash.db-wal").exists()
    for item in tmp_path.glob("crash.db*"):
        assert item.stat().st_mode & 0o077 == 0
    journal = Journal(str(path))
    try:
        row = row_for(journal, cid, "extraction")
        assert row["phase"] == "pending"
        recovered = LLMCallRecord.model_validate_json(row["record"])
        assert recovered.error_code == "provider_outcome_unknown"
        assert recovered.request_text is None and recovered.response_text is None
    finally:
        journal.close()
