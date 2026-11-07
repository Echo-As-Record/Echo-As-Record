"""EAR67 regressions: real extraction schemas, live sidecars and startup pagination."""

import json
import sqlite3
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from ear_contracts import LLMCallRecord, new_id
from ear_extraction.domain import Call, Generated, Segmentation, Selection
from ear_llm_gateway import GatewayClient
from ear_llm_gateway.models import GatewayError
from ear_llm_gateway.output_json import OutputSchema, encode

from .test_gateway import TOKEN, Harness
from .test_journal_hardening import row_for, seed
from .test_octopus import envelope, replay


def example(model) -> dict[str, Any]:
    cid = new_id()
    if model is Selection:
        return {"rule_ids": [cid]}
    if model is Segmentation:
        return {
            "activities": [
                {
                    "start": "2026-11-07T10:00:00Z",
                    "end": "2026-11-07T11:00:00Z",
                    "title_md": "synthetic",
                    "evidence_ids": [cid],
                }
            ]
        }
    return {
        "body_md": "synthetic",
        "evidence_ids": [cid],
        "action": {"kind": "reminder", "title": "synthetic", "due_at": "2026-11-07T11:00:00Z"},
    }


@pytest.mark.parametrize(
    "model,invalid",
    [
        (model, invalid)
        for model in (Segmentation, Selection, Generated)
        for invalid in (None, "uuid", "date")
        if not (model is Selection and invalid == "date")
    ],
)
def test_real_extraction_schema_through_client_and_adapter(tmp_path, model, invalid):
    output = example(model)
    model.model_validate(output)
    if invalid == "uuid":
        target = output["activities"][0] if model is Segmentation else output
        target["rule_ids" if model is Selection else "evidence_ids"] = ["not-a-uuid"]
    elif invalid == "date":
        if model is Segmentation:
            output["activities"][0]["start"] = "2026-02-30T10:00:00Z"
        else:
            output["action"]["due_at"] = "2026-11-07T25:00:00Z"
    adapter, requests, _ = replay(raw=encode(envelope(json.dumps(output))))
    h = Harness(tmp_path / "audit.db", provider=adapter)
    try:

        def wire(request):
            response = h.client.request(
                request.method, request.url.path, headers=request.headers, content=request.read()
            )
            return httpx.Response(response.status_code, content=response.content)

        request = Call(
            purpose="synthetic",
            root_feedback_id="system",
            depth=0,
            run_id=new_id(),
            data={},
            output_schema=model.model_json_schema(),
        )
        with GatewayClient(
            "http://testserver", TOKEN, transport=httpx.MockTransport(wire)
        ) as client:
            if invalid:
                with pytest.raises(GatewayError) as error:
                    client.complete(request)
                assert error.value.status == 502
            else:
                assert client.complete(request) == output
        assert len(requests) == 1
    finally:
        h.close()


@pytest.mark.parametrize(
    "schema",
    [
        {"pattern": "(a+)+$"},
        {"pattern": "a.*a.*a.*a"},
        {"format": "unknown"},
        {"format": []},
        {"pattern": []},
        {"$ref": "https://evil.invalid/schema"},
        {"$ref": "#/$defs/missing"},
        {"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
        {"unevaluatedProperties": False},
    ],
)
def test_schema_admission_has_no_attempt_reservation_or_quota(tmp_path, schema):
    adapter, requests, _ = replay()
    h = Harness(tmp_path / "audit.db", provider=adapter, global_rpm=1, caller_rpm=1)
    attempts = []
    original = adapter.complete

    def complete(*args, **kwargs):
        attempts.append(True)
        return original(*args, **kwargs)

    adapter.complete = complete
    try:
        request = h.call(output_schema=schema)
        response = h.post(request)
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_schema"}
        assert not requests and not attempts
        assert h.journal.get(request["call_id"], "extraction") is None
        assert h.writer.get_system_record(request["call_id"]) is None
        request["output_schema"] = {}
        assert h.post(request).status_code == 200  # Same ID and only quota slot remain usable.
        assert len(requests) == len(attempts) == 1
    finally:
        h.close()


@pytest.mark.parametrize(
    "value",
    [
        "bad",
        "2026-02-30T00:00:00Z",
        "2026-11-07",
        "2026-11-07T00:00:00",
        "2026-11-07T00:00:00+01:99",
    ],
)
def test_datetime_format_is_not_an_annotation(value):
    schema = OutputSchema({"type": "string", "format": "date-time"})
    with pytest.raises(ValueError, match="output_schema_mismatch"):
        schema.validate(value)
    schema.validate("2024-02-29T10:20:30.123Z")


def test_uuid_format_is_checked():
    schema = OutputSchema({"type": "string", "format": "uuid"})
    schema.validate(new_id())
    for value in ("bad", "123456781234123412341234567890ab", new_id() + "\n"):
        with pytest.raises(ValueError, match="output_schema_mismatch"):
            schema.validate(value)


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("replace", [False, True])
def test_live_sidecar_removed_or_replaced_before_reserve(tmp_path, suffix, replace):
    h = Harness(tmp_path / "audit.db")
    sidecar = tmp_path / ("audit.db" + suffix)
    retained = tmp_path / ("retained" + suffix)
    try:
        sidecar.rename(retained)
        if replace:
            sidecar.write_bytes(retained.read_bytes())
            sidecar.chmod(0o600)
        assert h.post().status_code == 503
        assert not h.provider.calls
    finally:
        if sidecar.exists():
            sidecar.unlink()
        retained.rename(sidecar)
        h.close()


@pytest.mark.parametrize("healthy", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
def test_cli_recovers_past_full_quarantine_batch(tmp_path, monkeypatch, healthy, unavailable):
    from ear_llm_gateway import cli, octopus

    h = Harness(tmp_path / "audit.db")
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
    ids = sorted(new_id() for _ in range(100 + healthy))
    before = {}
    for i, cid in enumerate(ids):
        h.journal.reserve(cid, record, global_rpm=200, caller_rpm=200)
        h.journal.pending(cid, record)
        before[cid] = row_for(h.journal, cid, "extraction")
        if i < 100:
            seed(h, "raw", cid)
    original = h.writer.get_system_record

    def read(cid):
        if unavailable and cid == ids[-1]:
            raise RuntimeError("synthetic unavailable")
        return original(cid)

    monkeypatch.setattr(h.writer, "get_system_record", read)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gateway",
            "--storage-url",
            "http://testserver",
            "--journal",
            str(tmp_path / "audit.db"),
            "--aliases",
            '{"default":"synthetic/model"}',
        ],
    )
    monkeypatch.setenv("EAR_GATEWAY_STORAGE_TOKEN", TOKEN)
    monkeypatch.setattr(octopus, "create_provider", lambda: h.provider)
    monkeypatch.setattr(
        cli, "RemoteStorageClient", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(cli, "Journal", lambda *a: h.journal)
    monkeypatch.setattr(cli, "Gateway", lambda **k: h.gateway)
    served = []

    def run(*args, **kwargs):
        assert h.journal.pending_ids() == []
        for cid in ids[:100]:
            assert row_for(h.journal, cid, "extraction") == {**before[cid], "phase": "quarantined"}
        if healthy:
            assert row_for(h.journal, ids[-1], "extraction")["phase"] == "saved"
        served.append(True)

    monkeypatch.setattr(cli.uvicorn, "run", run)
    # Bound pagination even if a future regression returns progress forever.
    reads = []
    original_pending = h.journal.pending_ids

    def pending(**kwargs):
        reads.append(True)
        assert len(reads) <= 5, "recovery did not terminate"
        return original_pending(**kwargs)

    monkeypatch.setattr(h.journal, "pending_ids", pending)
    try:
        if unavailable:
            with pytest.raises(SystemExit) as error:
                cli.main()
            assert error.value.code == 1 and not served
        else:
            cli.main()
            assert served == [True]
        assert not h.provider.calls
    finally:
        h.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("stage", ["before", "inside", "after_commit"])
def test_sidecar_identity_checked_at_transaction_boundaries(tmp_path, suffix, stage):
    h = Harness(tmp_path / "audit.db")
    sidecar = tmp_path / ("audit.db" + suffix)
    retained = tmp_path / ("retained" + suffix)
    original = h.journal._conn
    assert original is not None

    class Connection:
        def __getattr__(self, name):
            return getattr(original, name)

        def commit(self):
            original.commit()
            sidecar.rename(retained)

    try:
        if stage == "before":
            sidecar.rename(retained)
        if stage == "after_commit":
            h.journal._conn = cast(sqlite3.Connection, Connection())
        with (
            pytest.raises(ValueError, match="journal_sidecar_missing"),
            h.journal._transaction() as conn,
        ):
            if stage == "before":
                pytest.fail("transaction entered with missing sidecar")
            conn.execute("INSERT INTO buckets VALUES ('synthetic',0,0)")
            if stage == "inside":
                sidecar.rename(retained)
    finally:
        h.journal._conn = original
        if retained.exists():
            retained.rename(sidecar)
        h.close()


def test_checkpoint_and_clean_reopen_rebind_sidecars(tmp_path):
    from ear_llm_gateway.journal import Journal

    h = Harness(tmp_path / "audit.db")
    try:
        assert h.post().status_code == 200
        assert h.journal._conn is not None
        h.journal._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert h.post().status_code == 200
        h.journal.close()
        h.journal = Journal(str(tmp_path / "audit.db"))
        h.gateway.journal = h.journal
        assert h.post().status_code == 200
        assert len(h.provider.calls) == 3
    finally:
        h.close()


def test_local_adapter_size_rejection_does_not_reserve(tmp_path):
    adapter, requests, _ = replay(max_request_bytes=1)
    h = Harness(tmp_path / "audit.db", provider=adapter)
    try:
        request = h.call()
        response = h.post(request)
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_provider_request"}
        assert not requests
        assert h.journal.get(request["call_id"], "extraction") is None
    finally:
        h.close()


def test_recovery_batch_separates_processed_saved_and_quarantined(tmp_path):
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
        cid = new_id()
        h.journal.reserve(cid, record, global_rpm=2, caller_rpm=2)
        h.journal.pending(cid, record)
        seed(h, "raw", cid)
        batch = h.gateway.recover_batch()
        assert (batch.processed, batch.saved, batch.quarantined) == (1, 0, 1)
        batch = h.gateway.recover_batch()
        assert (batch.processed, batch.saved, batch.quarantined) == (0, 0, 0)
    finally:
        h.close()
