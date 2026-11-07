"""Deterministic synthetic plugin; real isolated PostgreSQL/JetStream, no model."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from ear_contracts import VersionRef, new_id
from ear_storage.auth import AuthenticationError
from ear_storage.history import job_status
from ear_storage.history_execution import claim, commit
from ear_storage.http import create_app
from ear_storage.schema import connection, one
from ear_storage.service import StorageService
from ear_storage_client import (
    Context,
    Create,
    InvalidRequest,
    PermissionDenied,
    RegisterDependencies,
    Update,
)
from ear_storage_client.consumer_wire import ConsumerRequest
from ear_storage_client.wire import encode_request
from fastapi.testclient import TestClient

from .test_history import recover, relay
from .test_management import command
from .test_management_integration import enable, execute
from .test_management_integration import setup as setup
from .test_reconfiguration import drain
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()
DAY = datetime(2026, 1, 1, tzinfo=UTC)


def arrange(setup, count=1, catchup=False):
    _, backend, _, service, _, _, _ = setup
    plugin = enable(setup, dependencies=True)
    ids = sorted(new_id() for _ in range(count))
    backend.client("extraction").write(
        [
            Create("activity", rid, {"n": i + 1}, event_start=DAY, event_end=DAY)
            for i, rid in enumerate(ids)
        ],
        SYS,
    ).check()
    if catchup:
        execute(setup, command("disable"))
        accepted, result = recover(setup, "catch_up")
        assert result["enabled"] and result["need_recovery_decision"]
    else:
        accepted, result = execute(
            setup,
            command(
                "history_backfill",
                input_types=("activity",),
                rangeStart=DAY,
                rangeEnd=DAY + timedelta(days=1),
            ),
        )
    assert result["status"] == "succeeded"
    return (
        plugin,
        service.client(plugin.token),
        ids,
        result["history_job_id"],
        accepted["feedback_id"],
    )


def chunk(client, lease, feedback, *, outcome="completed", error=None):
    item = lease["item"]
    snapshot = client.read_snapshot(lease["snapshot"]["snapshot_id"])
    current = snapshot.get(item["id"])
    assert current is not None
    output = new_id()
    ops = [
        Create(
            "sample_item",
            output,
            {"sum": current.payload["n"]},
            dedup_key="synthetic:" + current.id,
        ),
        RegisterDependencies(
            "sample_item",
            output,
            [VersionRef(data_type=current.data_type, id=current.id, version=current.version)],
        ),
    ]
    return output, {
        "claim_token": lease["claim_token"],
        "fence": lease["fence"],
        "request_id": new_id(),
        "batch": json.loads(
            encode_request(ops if outcome == "completed" else [], Context.feedback(feedback))
        ),
        "outcome": outcome,
        "error_code": error,
    }


def expire(dsn, job):
    with connection(dsn) as conn:
        conn.execute(
            "UPDATE storage.history_items SET lease_until='-infinity' WHERE job_id=%s", (job,)
        )


def test_current_two_items_restart_and_atomic_idempotent_receipt(setup):
    dsn, backend, _, service, _, admin, _ = setup
    plugin, client, ids, job, feedback = arrange(setup, 2)
    backend.client("extraction").write([Update("activity", ids[0], 1, {"n": 40})], SYS).check()
    first = claim(client, job)
    assert first["item"]["version"] == 2
    assert first["snapshot"]["commit_seq"] > job_status(client, job)["recovery_boundary"]
    out, body = chunk(client, first, feedback)
    with connection(dsn) as conn:
        digest = one(
            conn, "SELECT claim_hash FROM storage.history_items WHERE object_id=%s", (ids[0],)
        )[0]
        assert digest == hashlib.sha256(first["claim_token"].encode()).hexdigest()
        assert first["claim_token"] not in digest
    response = commit(client, job, body)
    assert response["job_state"] == "running"
    assert backend.get(out).payload == {"sum": 40}
    rebuilt = StorageService(dsn).client(plugin.token)
    assert commit(rebuilt, job, body) == response
    assert len(backend.history(out)) == 1
    altered = dict(body, outcome="failed", error_code="business_failed")
    altered["batch"] = json.loads(encode_request([], Context.feedback(feedback)))
    with pytest.raises(InvalidRequest):
        commit(rebuilt, job, altered)
    second = claim(rebuilt, job)
    out2, body2 = chunk(rebuilt, second, feedback)
    assert commit(rebuilt, job, body2)["job_state"] == "completed"
    assert backend.get(out2).payload == {"sum": 2}
    assert claim(client, job)["status"] == "completed"
    with pytest.raises(PermissionDenied):
        claim(service.client(admin.token), job)
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.history_receipts")[0] == 2
        assert one(conn, "SELECT count(*) FROM storage.snapshot_leases")[0] == 0


def test_stale_atomic_no_progress_expiry_fence_and_workers(setup):
    dsn, backend, _, _, _, _, _ = setup
    _, client, ids, job, feedback = arrange(setup)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: claim(client, job), range(2)))
    assert sorted(c["status"] for c in claims) == ["busy", "claimed"]
    first = next(c for c in claims if c["status"] == "claimed")
    out, body = chunk(client, first, feedback)
    backend.client("extraction").write([Update("activity", ids[0], 1, {"n": 9})], SYS).check()
    assert commit(client, job, body) == {
        "status": "conflict",
        "error_code": "stale_inputs",
        "advanced": False,
    }
    assert backend.get(out) is None
    assert job_status(client, job)["items"] == {"claimed": 1}
    expire(dsn, job)
    with pytest.raises(PermissionDenied):
        commit(client, job, body)
    newer = claim(client, job)
    assert newer["fence"] == first["fence"] + 1
    with pytest.raises(PermissionDenied):
        commit(client, job, body)
    out, body = chunk(client, newer, feedback)
    assert commit(client, job, body)["job_state"] == "completed"
    assert backend.get(out).payload == {"sum": 9}


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("change", ["revoke", "disable", "epoch", "grant"])
def test_current_authority_denies_receipt_replay(setup, change, replay):
    _, _, _, service, operator, _, _ = setup
    plugin, client, _, job, feedback = arrange(setup)
    lease = claim(client, job)
    _, body = chunk(client, lease, feedback)
    if replay:
        commit(client, job, body)
    if change == "revoke":
        operator.revoke(plugin.principal_id)
    elif change == "disable":
        execute(setup, command("disable"))
    elif change == "epoch":
        rotated = operator.rotate(plugin.principal_id)
        client = service.client(rotated.token)
    else:
        operator.set_grants(plugin.principal_id, read_types=["sample_item"])
    with pytest.raises((AuthenticationError, PermissionDenied)):
        commit(client, job, body)


def test_catchup_complete_releases_only_covered_events(setup):
    _, backend, _, service, _, _, _ = setup
    plugin, client, _, job, feedback = arrange(setup, catchup=True)
    assert service.consumers is not None
    with pytest.raises(PermissionDenied):
        service.consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
    # Claim after a newer event: read CURRENT; only finite planned candidates are processed.
    future = new_id()
    backend.client("extraction").write([Create("activity", future, {"n": 99})], SYS).check()
    lease = claim(client, job)
    _, body = chunk(client, lease, feedback)
    assert commit(client, job, body)["job_state"] == "completed"
    relay(setup)
    assert drain(service.consumers, plugin.token, "sample.managed") == [future]
    assert drain(service.consumers, plugin.token, "sample.managed") == []
    assert not execute(setup, command("status"))[1]["need_recovery_decision"]


@pytest.mark.parametrize(
    "outcome,error", [("failed", "business_failed"), ("blocked", "needs_operator")]
)
def test_worker_terminal_receipts_preserve_results(setup, outcome, error):
    _, backend, _, _, _, _, _ = setup
    _, client, _, job, feedback = arrange(setup)
    lease = claim(client, job)
    out, body = chunk(client, lease, feedback, outcome=outcome, error=error)
    result = commit(client, job, body)
    assert result["job_state"] == outcome and result["items"] == {outcome: 1}
    assert commit(client, job, body) == result
    assert backend.get(out) is None


def test_http_claim_commit_boundary(setup):
    _, _, _, service, _, admin, _ = setup
    plugin, client, _, job, feedback = arrange(setup)
    with TestClient(create_app(service)) as http:
        path = "/v1/history/jobs/" + job
        headers = {"Authorization": "Bearer " + plugin.token}
        assert http.post(path + "/claim").status_code == 401
        assert (
            http.post(
                path + "/claim", headers={"Authorization": "Bearer " + admin.token}
            ).status_code
            == 403
        )
        response = http.post(path + "/claim", headers=headers)
        assert response.status_code == 200, response.text
        lease = response.json()
        _, body = chunk(client, lease, feedback)
        result = http.post(path + "/commit", headers=headers, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["job_state"] == "completed"
        assert http.post(path + "/commit", headers=headers, json=body).json() == result.json()


def test_no_output_receipt_and_finite_crash_retries(setup, monkeypatch):
    dsn, _, _, _, _, _, _ = setup
    _, client, _, job, feedback = arrange(setup, 2)
    first = claim(client, job)
    _, body = chunk(client, first, feedback)
    body["batch"] = json.loads(encode_request([], Context.feedback(feedback)))
    result = commit(client, job, body)
    assert result["items"] == {"completed": 1, "candidate": 1}
    assert commit(client, job, body) == result
    monkeypatch.setattr("ear_storage.history_execution.MAX_ATTEMPTS", 1)
    claim(client, job)
    expire(dsn, job)
    assert claim(client, job)["error_code"] == "needs_operator"
    assert job_status(client, job)["state"] == "blocked"


def test_new_collection_input_conflict_and_wrong_dependency_type(setup):
    _, backend, _, _, _, _, _ = setup
    _, client, _, job, feedback = arrange(setup)
    lease = claim(client, job)
    out, body = chunk(client, lease, feedback)
    # A newly arriving input is not an existing dependency, but still invalidates the computation.
    backend.client("extraction").write([Create("activity", new_id(), {"n": 80})], SYS).check()
    conflict = commit(client, job, body)
    assert conflict["status"] == "conflict" and not conflict["advanced"]
    assert commit(client, job, body) == conflict
    assert backend.get(out) is None
    assert job_status(client, job)["items"] == {"claimed": 1}


def test_remote_convenience_with_real_http_backend(setup):
    import httpx
    from ear_storage_client.remote import RemoteStorageClient

    _, backend, _, service, _, _, _ = setup
    plugin, _, _, job, feedback = arrange(setup)
    with TestClient(create_app(service)) as http:

        def forward(request):
            response = http.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, content=response.content)

        with RemoteStorageClient(
            plugin.token, "http://testserver", transport=httpx.MockTransport(forward)
        ) as remote:
            assert remote.history_job(job)["state"] == "planned"
            lease = remote.claim_history_job(job)
            snapshot = remote.read_snapshot(lease["snapshot"]["snapshot_id"])
            record = snapshot.get(lease["item"]["id"])
            assert record is not None
            output = new_id()
            ops = [
                Create("sample_item", output, {"sum": record.payload["n"]}),
                RegisterDependencies(
                    "sample_item",
                    output,
                    [VersionRef(data_type=record.data_type, id=record.id, version=record.version)],
                ),
            ]
            kwargs = dict(request_id=new_id())
            args = (job, lease["claim_token"], lease["fence"], ops, Context.feedback(feedback))
            result = remote.commit_history_job(*args, **kwargs)
            assert result["job_state"] == "completed"
            assert remote.commit_history_job(*args, **kwargs) == result
            assert backend.get(output).payload == {"sum": 1}


@pytest.mark.parametrize("failure", ["receipt", "expired_during_write"])
def test_chunk_failure_rolls_back_business_and_progress(setup, monkeypatch, failure):
    from ear_storage import history_execution
    from ear_storage.backend import PostgresClient

    dsn, backend, _, _, _, _, _ = setup
    _, client, _, job, feedback = arrange(setup)
    lease = claim(client, job)
    output, body = chunk(client, lease, feedback)
    if failure == "receipt":

        def abort(*args):
            raise RuntimeError("synthetic interruption")

        monkeypatch.setattr(history_execution, "_finish_progress", abort)
        expected = RuntimeError
    else:
        original = PostgresClient._write_locked

        def late(self, conn, *args, **kwargs):
            result = original(self, conn, *args, **kwargs)
            conn.execute(
                "UPDATE storage.history_items SET lease_until='-infinity' WHERE job_id=%s", (job,)
            )
            return result

        monkeypatch.setattr(PostgresClient, "_write_locked", late)
        expected = PermissionDenied
    with pytest.raises(expected):
        commit(client, job, body)
    assert backend.get(output) is None
    assert job_status(client, job)["items"] == {"claimed": 1}
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.history_receipts")[0] == 0
        assert (
            one(
                conn, "SELECT count(*) FROM storage.idempotency WHERE scope=%s", ("history:" + job,)
            )[0]
            == 0
        )


def test_wrong_type_dependency_and_no_publish_or_fake_finish(setup):
    from ear_contracts import Cause
    from ear_storage_client import PublishReevaluation

    _, backend, _, _, _, _, _ = setup
    _, client, ids, job, feedback = arrange(setup)
    lease = claim(client, job)
    output, body = chunk(client, lease, feedback)
    ops = [
        Create("sample_item", output, {}),
        RegisterDependencies(
            "sample_item", output, [VersionRef(data_type="sample_item", id=ids[0], version=1)]
        ),
    ]
    body["batch"] = json.loads(encode_request(ops, Context.feedback(feedback)))
    with pytest.raises(InvalidRequest):
        commit(client, job, body)
    assert backend.get(output) is None
    body["batch"] = json.loads(
        encode_request(
            [
                PublishReevaluation(
                    "sample_item",
                    output,
                    Cause(data_type="activity", data_id=ids[0], old_version=None, new_version=1),
                    "synthetic",
                )
            ],
            Context.feedback(feedback).deeper(),
        )
    )
    with pytest.raises(InvalidRequest, match="operation_not_allowed"):
        commit(client, job, body)
    body["batch"] = json.loads(encode_request([], Context("system", 1, request_event_id=job)))
    with pytest.raises(InvalidRequest, match="scheduler_integration"):
        commit(client, job, body)


def test_catchup_existing_dependent_requires_scheduler_without_forged_request(setup):
    dsn, backend, _, service, _, _, _ = setup
    plugin = enable(setup, dependencies=True)
    client = service.client(plugin.token)
    source, target = new_id(), new_id()
    backend.client("extraction").write([Create("activity", source, {"n": 1})], SYS).check()
    client.write(
        [
            Create("sample_item", target, {"old": True}),
            RegisterDependencies(
                "sample_item", target, [VersionRef(data_type="activity", id=source, version=1)]
            ),
        ],
        SYS,
    ).check()
    execute(setup, command("disable"))
    backend.client("extraction").write([Update("activity", source, 1, {"n": 2})], SYS).check()
    accepted, result = recover(setup, "catch_up")
    job = result["history_job_id"]
    assert result["status"] == "succeeded" and result["enabled"]
    assert job_status(client, job)["requires_scheduler"]
    first = claim(client, job)
    assert first["status"] == "claimed"
    _, body = chunk(client, first, accepted["feedback_id"])
    body["batch"] = json.loads(encode_request([], Context.feedback(accepted["feedback_id"])))
    assert commit(client, job, body)["job_state"] == "running"
    assert claim(client, job)["error_code"] == "scheduler_unavailable"
    assert backend.get(target).payload == {"old": True}
    assert execute(setup, command("status"))[1]["need_recovery_decision"]
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.request_completions")[0] == 0
        assert one(conn, "SELECT count(*) FROM storage.published")[0] == 0
