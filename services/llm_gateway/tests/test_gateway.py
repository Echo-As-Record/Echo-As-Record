"""Synthetic providers only. No credentials, paid APIs or production network access."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
import uvicorn
from ear_contracts import Cause, LLMCallRecord, LLMUsage, TracedCause, new_id
from ear_llm_gateway import CallRequest, Gateway, GatewayClient, ProviderFailure, ProviderResult
from ear_llm_gateway.http import create_app
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.models import GatewayError
from ear_storage_client import MemoryStorage, ModelBodyPolicy, Query
from ear_storage_client.protocol import StorageClient
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError
from ear_storage_client.wire import decode_read, decode_request, encode_read_result, encode_result
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

TOKEN = "a" * 43
OTHER = "b" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


class SyntheticProvider:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [ProviderResult({"answer": "synthetic-reply"})])

    def complete(self, call, *, model, timeout):
        self.calls.append((call.call_id, model, timeout))
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Harness:
    def __init__(self, path, *, provider=None, body_policy=None, **options):
        self.store = MemoryStorage(model_body_policy=body_policy or ModelBodyPolicy())
        self.identities = {TOKEN: "extraction", OTHER: "attribution"}
        self.journal = Journal(str(path))
        self.provider = provider or SyntheticProvider()
        self.writer = self.store.client("llm_gateway")
        self.gateway = Gateway(
            callers=self.caller,
            writer=self.writer,
            provider=self.provider,
            journal=self.journal,
            aliases={"default": "synthetic/model"},
            body_policy=body_policy or ModelBodyPolicy(),
            **options,
        )
        self.client = TestClient(create_app(self.gateway))

    def caller(self, token):
        harness = self
        client = self.store.client(self.identities.get(token, "extraction"))

        class LiveIdentity:
            @property
            def module(self):
                if token not in harness.identities:
                    raise AuthenticationError("SECRET_AUTH_ERROR")
                return harness.identities[token]

            def __getattr__(self, name):
                if token not in harness.identities:
                    raise AuthenticationError("SECRET_AUTH_ERROR")
                return getattr(client, name)

        return nullcontext(cast(StorageClient, LiveIdentity()))

    @staticmethod
    def call(**changes):
        return {
            "call_id": new_id(),
            "module": "extraction",
            "purpose": "synthetic_test",
            "root_feedback_id": "system",
            "depth": 0,
            "data": {"prompt": "SENSITIVE_PROMPT"},
            **changes,
        }

    def post(self, call=None, headers=None):
        return self.client.post(
            "/v1/complete", json=call or self.call(), headers=headers or HEADERS
        )

    def close(self):
        self.client.close()
        self.journal.close()


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path / "journal.sqlite3")
    yield h
    h.close()


def test_success_metadata_usage_unknown_and_no_events(harness, caplog):
    call = harness.call()
    before = len(harness.store._log)
    response = harness.post(call)
    assert response.status_code == 200, response.text
    assert response.json()["record_saved"] is True
    stored = harness.writer.get_system_record(call["call_id"])
    assert stored.writer == "llm_gateway"
    record = LLMCallRecord.model_validate(stored.payload)
    assert record.usage is None
    assert record.model == "synthetic/model"
    assert record.request_text is None and record.response_text is None
    assert "SENSITIVE_PROMPT" not in harness.journal.get(call["call_id"], "extraction")["record"]
    assert "synthetic-reply" not in harness.journal.get(call["call_id"], "extraction")["record"]
    assert len(harness.store._log) == before
    assert "SENSITIVE_PROMPT" not in caplog.text
    stats = harness.client.get("/v1/stats", headers=HEADERS)
    assert stats.status_code == 200, stats.text
    usage = stats.json()["roots"]["system"]["usage"]["total_tokens"]
    assert usage == {"known_tokens": 0, "unknown_calls": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"module": None},
        {"module": "attribution"},
        {"depth": 1},
        {"root_feedback_id": new_id()},
        {"include_body": True},
        {"model": "https://attacker.invalid/model"},
        {"model": "nonexistent"},
        {"call_id": "SENSITIVE_PROMPT"},
        {"data": "SENSITIVE_PROMPT"},
        {
            "additional_causes": [
                {
                    "cause": {
                        "data_type": "artifact",
                        "data_id": new_id(),
                        "old_version": None,
                        "new_version": 1,
                    },
                    "root_feedback_id": "system",
                    "depth": 1,
                    "token": "SENSITIVE_PROMPT",
                }
            ]
        },
    ],
)
def test_reject_before_provider_and_redact(harness, change):
    call = harness.call(**change)
    response = harness.post(call)
    assert response.status_code in {401, 403, 422}
    assert "SENSITIVE_PROMPT" not in response.text
    assert not harness.provider.calls
    assert harness.writer.count(Query("llm_call")) == 0


def test_missing_module_auth_and_duplicate_headers(harness):
    call = harness.call()
    del call["module"]
    assert harness.post(call).status_code == 422
    assert harness.client.post("/v1/complete", json=harness.call()).status_code == 401
    assert harness.post(headers={"Authorization": "Bearer " + "z" * 43}).status_code == 401
    response = harness.client.post(
        "/v1/complete",
        json=harness.call(),
        headers=[
            ("Authorization", "Bearer " + TOKEN),
            ("Authorization", "Bearer " + OTHER),
        ],
    )
    assert response.status_code == 401
    assert not harness.provider.calls


def test_revocation_live_identity_no_cache(harness):
    assert harness.post().status_code == 200
    del harness.identities[TOKEN]
    assert harness.post().status_code == 401
    assert len(harness.provider.calls) == 1
    assert harness.client.get("/v1/stats", headers=HEADERS).status_code == 401


def test_no_privileged_caller_impersonation(harness):
    assert harness.post().status_code == 200
    assert harness.post(harness.call(module="llm_gateway")).status_code == 403
    response = harness.client.get("/v1/stats", headers={"Authorization": "Bearer " + OTHER})
    assert response.json() == {"module": "attribution", "roots": {}}
    assert len(harness.provider.calls) == 1


def test_duplicate_never_executes_or_returns_cached_output(harness):
    call = harness.call()
    assert harness.post(call).status_code == 200
    assert harness.post(call).status_code == 409
    assert harness.post({**call, "data": {"changed": True}}).status_code == 409
    assert len(harness.provider.calls) == 1
    status = harness.client.post(f"/v1/calls/{call['call_id']}/reconcile", headers=HEADERS)
    assert status.json()["response_recoverable"] is False
    assert "output" not in status.json()
    hidden = harness.client.post(
        f"/v1/calls/{call['call_id']}/reconcile",
        headers={
            "Authorization": "Bearer " + OTHER,
        },
    )
    assert hidden.status_code == 404


@pytest.mark.parametrize(
    "failure,count,expected",
    [
        (ProviderFailure(safe_to_retry=True), 2, "provider_rejected"),
        (ProviderFailure(), 1, "provider_outcome_unknown"),
        (httpx.ReadTimeout("SENSITIVE_PROMPT"), 1, "provider_outcome_unknown"),
        (httpx.ConnectError("SENSITIVE_PROMPT"), 1, "provider_outcome_unknown"),
        (ValueError("SENSITIVE_PROMPT"), 1, "provider_outcome_unknown"),
    ],
)
def test_bounded_retry_unknown_stops(tmp_path, failure, count, expected):
    h = Harness(tmp_path / "retry.sqlite3", provider=SyntheticProvider([failure]), attempts=2)
    try:
        call = h.call()
        response = h.post(call)
        assert response.status_code == 502, response.text
        assert len(h.provider.calls) == count
        record = h.writer.get_system_record(call["call_id"])
        assert record is not None
        assert record.payload["error_code"] == expected
        assert record.payload["usage"] is None
        assert "SENSITIVE_PROMPT" not in str(record.payload) + response.text
    finally:
        h.close()


def test_safe_retry_then_success(tmp_path):
    p = SyntheticProvider(
        [
            ProviderFailure(safe_to_retry=True),
            ProviderResult({"ok": True}, LLMUsage(input_tokens=0, output_tokens=3)),
        ]
    )
    h = Harness(tmp_path / "safe.sqlite3", provider=p)
    try:
        response = h.post()
        assert response.status_code == 200, response.text
        assert len(p.calls) == 2
        usage = h.client.get("/v1/stats", headers=HEADERS).json()["roots"]["system"]["usage"]
        assert usage["input_tokens"] == {"known_tokens": 0, "unknown_calls": 0}
        assert usage["output_tokens"]["known_tokens"] == 3
        assert usage["total_tokens"]["unknown_calls"] == 1
    finally:
        h.close()


def test_storage_failure_visible_and_reconcile_no_recall(harness):
    class BrokenWriter:
        def __getattr__(self, name):
            return getattr(harness.writer, name)

        def write(self, *args, **kwargs):
            raise RuntimeError("SENSITIVE_PROMPT api_key")

    harness.gateway.writer = BrokenWriter()
    call = harness.call()
    response = harness.post(call)
    assert response.status_code == 503 and response.json()["code"] == "record_pending"
    assert "output" not in response.json()
    assert harness.journal.get(call["call_id"], "extraction")["phase"] == "pending"
    assert harness.post(call).status_code == 409
    harness.gateway.writer = harness.writer
    response = harness.client.post(f"/v1/calls/{call['call_id']}/reconcile", headers=HEADERS)
    assert response.status_code == 200
    assert len(harness.provider.calls) == 1
    assert harness.writer.get_system_record(call["call_id"]).payload["status"] == "success"


def test_crash_recovery_active_becomes_unknown(tmp_path):
    path = str(tmp_path / "crash.sqlite3")
    now = datetime.now(UTC)
    record = LLMCallRecord(
        caller_module="extraction",
        purpose="crash",
        root_feedback_id="system",
        depth=0,
        started_at=now,
        ended_at=now,
        status="failed",
        error_code="provider_outcome_unknown",
    )
    call_id = new_id()
    journal = Journal(path)
    journal.reserve(call_id, record, global_rpm=5, caller_rpm=5)
    with pytest.raises(ValueError, match="journal_already_owned"):
        Journal(path)
    journal.close()
    h = Harness(path)
    try:
        pending = h.journal.get(call_id, "extraction")
        assert pending is not None and pending["phase"] == "pending"
        response = h.client.post(f"/v1/calls/{call_id}/reconcile", headers=HEADERS)
        assert response.status_code == 200
        saved = h.writer.get_system_record(call_id)
        assert saved is not None and saved.payload["error_code"] == "provider_outcome_unknown"
        assert h.post(h.call(call_id=call_id)).status_code == 409
        assert not h.provider.calls
    finally:
        h.close()


def test_rate_limit_persists_restart_and_caller_scoping(tmp_path):
    path = str(tmp_path / "limits.sqlite3")
    h = Harness(path, global_rpm=2, caller_rpm=1)
    assert h.post().status_code == 200
    assert h.post().status_code == 429
    response = h.post(h.call(module="attribution"), headers={"Authorization": "Bearer " + OTHER})
    assert response.status_code == 200
    h.close()
    h = Harness(path, global_rpm=2, caller_rpm=1)
    try:
        assert h.post().status_code == 429
        assert not h.provider.calls
    finally:
        h.close()


def test_body_limits_length_chunked_encoding_and_timeout(harness):
    client = TestClient(create_app(harness.gateway, max_body_bytes=64))
    assert client.post("/v1/complete", content=b"x" * 65).status_code == 413
    assert (
        client.post(
            "/v1/complete",
            content=b"{}",
            headers={
                "Content-Encoding": "gzip",
            },
        ).status_code
        == 415
    )

    async def check():
        async def chunks():
            yield b"x" * 33
            yield b"x" * 33

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=create_app(
                    harness.gateway,
                    max_body_bytes=64,
                    body_timeout=0.01,
                )
            ),
            base_url="http://test",
        ) as ac:
            assert (await ac.post("/v1/complete", content=chunks())).status_code == 413

            async def slow():
                await asyncio.sleep(0.03)
                yield b"{}"

            assert (await ac.post("/v1/complete", content=slow())).status_code == 408

    asyncio.run(check())
    assert not harness.provider.calls


def test_explicit_body_policy_bounded_scope(tmp_path):
    policy = ModelBodyPolicy(enabled=True, max_bytes=24, allowed_callers=("extraction",))
    h = Harness(tmp_path / "bodies.sqlite3", body_policy=policy)
    try:
        call = h.call(data={"unicode": "合成测试" * 50})
        assert h.post(call).status_code == 200
        saved = h.writer.get_system_record(call["call_id"])
        assert saved is not None
        record = saved.payload
        assert sum(len(record.get(k, "").encode()) for k in ("request_text", "response_text")) <= 24
        assert "request_text" in record
        other = h.call(module="attribution")
        assert h.post(other, headers={"Authorization": "Bearer " + OTHER}).status_code == 200
        saved = h.writer.get_system_record(other["call_id"])
        assert saved is not None
        assert "request_text" not in saved.payload
    finally:
        h.close()


def test_request_preflight_missing_and_unreadable_cause_fail_closed(harness):
    response = harness.post(harness.call(request_event_id=new_id(), depth=1))
    assert response.status_code == 403
    assert response.json()["code"] == "invalid_call_context"
    trace = TracedCause(
        cause=Cause(data_type="artifact", data_id=new_id(), old_version=None, new_version=1),
        root_feedback_id="system",
        depth=1,
    )
    assert (
        harness.post(harness.call(additional_causes=[trace.model_dump(mode="json")])).status_code
        == 403
    )
    assert not harness.provider.calls


@contextmanager
def tcp_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(2)
        sock.close()
        raise RuntimeError("synthetic_server_failed")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_real_tcp_gateway_client_and_synthetic_fixed_provider(harness):
    upstream = FastAPI()
    upstream_calls = []

    @upstream.post("/synthetic")
    async def synthetic(request: Request):
        upstream_calls.append(await request.json())
        return {"output": {"transport": "real_tcp"}, "usage": {"output_tokens": 2}}

    with tcp_server(upstream) as provider_url:

        class FixedSyntheticTCPProvider:
            # Test protocol ONLY; not named or presented as Octopus/OpenAI.
            def complete(self, call, *, model, timeout):
                with httpx.Client(
                    timeout=timeout, trust_env=False, follow_redirects=False
                ) as client:
                    response = client.post(provider_url + "/synthetic", json={"model": model})
                    response.raise_for_status()
                    data = response.json()
                    return ProviderResult(data["output"], LLMUsage.model_validate(data["usage"]))

        harness.gateway.provider = FixedSyntheticTCPProvider()
        with (
            tcp_server(create_app(harness.gateway)) as gateway_url,
            GatewayClient(gateway_url, TOKEN) as client,
        ):
            call = CallRequest.model_validate(harness.call())
            assert client.complete(call) == {"transport": "real_tcp"}
            with pytest.raises(GatewayError):
                client.complete(call)
            assert client.reconcile(call.call_id)["record_saved"]
            assert "credentials redacted" in repr(client)
    assert len(upstream_calls) == 1


def test_real_tcp_storage_identity_revocation_and_commit_recovery(tmp_path):
    store = MemoryStorage()
    writer = store.client("llm_gateway")
    app = FastAPI()
    enabled = {TOKEN, OTHER}
    writes, receipts = [], {}
    lost_reply = [True]

    def authenticated(request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        return token if token in enabled else None

    @app.get("/v1/identity")
    def identity(request: Request):
        token = authenticated(request)
        if token is None:
            return JSONResponse({"code": "unauthenticated"}, status_code=401)
        return {"codec": 1, "module": "llm_gateway" if token == OTHER else "extraction"}

    @app.post("/v1/read")
    async def read(request: Request):
        if not authenticated(request):
            return JSONResponse({"code": "unauthenticated"}, status_code=401)
        parsed, values = decode_read(await request.body())
        return Response(
            encode_read_result(parsed.method, getattr(writer, parsed.method)(**values)),
            media_type="application/json",
        )

    @app.post("/v1/llm/call-id-check")
    async def call_id_check(request: Request):
        if authenticated(request) != OTHER:
            return JSONResponse({"code": "access_denied"}, status_code=403)
        body = await request.json()
        return {"occupied": writer.call_id_occupied(body["call_id"])}

    @app.post("/v1/batches")
    async def batches(request: Request):
        if authenticated(request) != OTHER:
            return JSONResponse({"code": "access_denied"}, status_code=403)
        body = await request.json()
        request_id = body.pop("request_id")
        writes.append(request_id)
        if request_id not in receipts:
            ops, context = decode_request(json.dumps(body))
            receipts[request_id] = encode_result(writer.write(ops, context))
        if lost_reply[0]:
            lost_reply[0] = False
            return JSONResponse({"code": "unavailable"}, status_code=503)
        return Response(receipts[request_id], media_type="application/json")

    with (
        tcp_server(app) as url,
        RemoteStorageClient(OTHER, url, module="llm_gateway") as remote_writer,
    ):
        journal = Journal(str(tmp_path / "remote.sqlite3"))
        provider = SyntheticProvider()
        gateway = Gateway(
            callers=lambda token: RemoteStorageClient(token, url),
            writer=remote_writer,
            provider=provider,
            journal=journal,
            aliases={"default": "synthetic/model"},
        )
        with TestClient(create_app(gateway)) as client:
            call = Harness.call()
            response = client.post("/v1/complete", json=call, headers=HEADERS)
            assert response.status_code == 503
            assert response.json()["code"] == "record_pending"
            # The external call succeeded and ① committed, but its reply was lost.
            assert writer.get_system_record(call["call_id"]) is not None
            response = client.post(f"/v1/calls/{call['call_id']}/reconcile", headers=HEADERS)
            assert response.status_code == 200
            assert len(provider.calls) == 1 and len(writes) == 1
            enabled.remove(TOKEN)
            response = client.post("/v1/complete", json=Harness.call(), headers=HEADERS)
            assert response.status_code == 401
            assert len(provider.calls) == 1
        journal.close()


def test_concurrent_duplicate_and_concurrency_limit(harness):
    entered, release = threading.Event(), threading.Event()

    class BlockingProvider:
        calls = 0

        def complete(self, call, *, model, timeout):
            self.calls += 1
            entered.set()
            assert release.wait(3)
            return ProviderResult({"ok": True})

    provider = BlockingProvider()
    harness.gateway.provider = provider
    call = CallRequest.model_validate(harness.call())
    results = []
    thread = threading.Thread(target=lambda: results.append(harness.gateway.complete(TOKEN, call)))
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(GatewayError) as error:
            harness.gateway.complete(TOKEN, call)
        assert error.value.code == "duplicate_call_id"
        # The active journal marker is durable before a provider is entered.
        assert harness.journal.get(call.call_id, "extraction")["phase"] == "active"
    finally:
        release.set()
        thread.join(3)
    assert provider.calls == 1 and len(results) == 1


def test_committed_request_all_origins_and_forgery_rejected(harness):
    from ear_storage_client import Context, Create, PublishReevaluation

    target, source, second = new_id(), new_id(), new_id()
    harness.store.client("extraction").write(
        [
            Create("artifact", target, {}),
            Create("rule", source, {}),
            Create("rule", second, {}),
        ],
        Context.system(),
    ).check()
    extra = TracedCause(
        cause=Cause(data_type="rule", data_id=second, old_version=None, new_version=1),
        root_feedback_id="system",
        depth=2,
    )
    event_id = new_id()
    harness.store.client("dependency").write(
        [
            PublishReevaluation(
                "artifact",
                target,
                Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                "synthetic change",
                event_id=event_id,
                additional_causes=(extra,),
            ),
        ],
        Context("system", 1),
    ).check()

    # Default path now uses the public, side-effect-free SDK preflight, not a
    # deployment override reading MemoryStorage's private request map.
    assert harness.gateway.verifier is None
    for changes in ({"request_event_id": new_id()}, {"depth": 3}, {"additional_causes": []}):
        call = harness.call(
            request_event_id=event_id, depth=1, additional_causes=[extra.model_dump(mode="json")]
        )
        call.update(changes)
        assert harness.post(call).status_code == 403
    assert not harness.provider.calls
    call = harness.call(
        request_event_id=event_id, depth=1, additional_causes=[extra.model_dump(mode="json")]
    )
    response = harness.post(call)
    assert response.status_code == 200, response.text
    saved = harness.writer.get_system_record(call["call_id"])
    assert saved is not None
    record = LLMCallRecord.model_validate(saved.payload)
    assert record.request_event_id == event_id
    assert extra in record.additional_causes
    assert {t.cause.data_id for t in record.additional_causes} == {source, second}


def test_revocation_during_provider_blocks_response_and_preserves_audit(harness):
    class RevokingProvider:
        def complete(self, call, *, model, timeout):
            del harness.identities[TOKEN]
            return ProviderResult({"sensitive": "synthetic secret reply"})

    harness.gateway.provider = RevokingProvider()
    call = harness.call()
    response = harness.post(call)
    assert response.status_code == 401
    assert "synthetic secret reply" not in response.text
    saved = harness.writer.get_system_record(call["call_id"])
    assert saved is not None and saved.payload["status"] == "success"


def test_record_replay_same_storage_request_id_after_precommit_failure(tmp_path):
    # Post-commit lost replies are covered by the real TCP test; precommit
    # failure must replay the original immutable payload/request ID as well.
    requests = []
    store = MemoryStorage()
    memory = store.client("llm_gateway")

    def wire(request):
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json={"codec": 1, "module": "llm_gateway"})
        if request.url.path == "/v1/llm/call-id-check":
            cid = json.loads(request.content)["call_id"]
            return httpx.Response(200, json={"occupied": memory.call_id_occupied(cid)})
        if request.url.path == "/v1/read":
            parsed, values = decode_read(request.content)
            return httpx.Response(
                200,
                text=encode_read_result(parsed.method, getattr(memory, parsed.method)(**values)),
            )
        body = json.loads(request.content)
        requests.append(body.copy())
        if len(requests) == 1:
            return httpx.Response(503, json={"code": "unavailable"})
        body.pop("request_id")
        ops, context = decode_request(json.dumps(body))
        return httpx.Response(200, text=encode_result(memory.write(ops, context)))

    h = Harness(tmp_path / "replay.sqlite3")
    with RemoteStorageClient(
        OTHER, "http://fixed.test", transport=httpx.MockTransport(wire)
    ) as writer:
        h.gateway.writer = writer
        try:
            call = h.call()
            assert h.post(call).status_code == 503
            response = h.client.post(f"/v1/calls/{call['call_id']}/reconcile", headers=HEADERS)
            assert response.status_code == 200, response.text
            assert requests[0] == requests[1]
            assert len(h.provider.calls) == 1
        finally:
            h.close()


def test_deployment_recovery_does_not_require_revoked_caller(harness):
    class BrokenWriter:
        def __getattr__(self, name):
            return getattr(harness.writer, name)

        def write(self, *args, **kwargs):
            raise RuntimeError("synthetic storage unavailable")

    harness.gateway.writer = BrokenWriter()
    call = harness.call()
    assert harness.post(call).status_code == 503
    del harness.identities[TOKEN]
    harness.gateway.writer = harness.writer
    assert harness.gateway.recover_pending() == 1
    assert harness.gateway.recover_pending() == 0
    assert len(harness.provider.calls) == 1
    assert harness.writer.get_system_record(call["call_id"]) is not None


def test_default_preflight_missing_capability_fails_closed(harness):
    class LegacyClient:
        module = "extraction"

    harness.gateway.callers = lambda token: nullcontext(LegacyClient())
    call = harness.call(request_event_id=new_id(), depth=1)
    response = harness.post(call)
    assert response.status_code == 503
    assert response.json()["code"] == "request_preflight_unavailable"
    assert not harness.provider.calls
    assert harness.journal.get(call["call_id"], "extraction") is None
    assert harness.writer.count(Query("llm_call")) == 0


@pytest.mark.parametrize("failure", ["authentication", "permission", "unavailable"])
def test_retry_rechecks_access_and_classifies_preflight_failure(harness, failure):
    from ear_storage_client import PermissionDenied

    original = harness.gateway._check
    attempts = []

    def check(*args, **kwargs):
        # Admission and first attempt pass; recheck before retry and release fails.
        if attempts:
            if failure == "authentication":
                raise AuthenticationError("SECRET_AUTH_ERROR")
            if failure == "permission":
                raise PermissionDenied("SECRET_AUTH_ERROR")
            raise GatewayError(503, "request_preflight_unavailable")
        return original(*args, **kwargs)

    class RejectingProvider:
        def complete(self, call, *, model, timeout):
            attempts.append(call.call_id)
            raise ProviderFailure(safe_to_retry=True)

    harness.gateway._check = check
    harness.gateway.provider = RejectingProvider()
    call = harness.call()
    response = harness.post(call)
    assert (
        response.status_code
        == {"authentication": 401, "permission": 403, "unavailable": 503}[failure]
    )
    assert len(attempts) == 1
    record = harness.writer.get_system_record(call["call_id"])
    assert record is not None and record.payload["status"] == "failed"
    expected = (
        "caller_preflight_unavailable" if failure == "unavailable" else "caller_access_revoked"
    )
    assert record.payload["error_code"] == expected
    assert "SECRET_AUTH_ERROR" not in response.text + str(record.payload)


def test_cli_default_remote_preflight_connection(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    from ear_llm_gateway import cli
    from ear_storage_client import (
        Context,
        Create,
        InvalidRequest,
        LLMCallPreflight,
        PublishReevaluation,
    )

    store = MemoryStorage()
    target, source = new_id(), new_id()
    store.client("extraction").write(
        [Create("artifact", target, {}), Create("rule", source, {})], Context.system()
    ).check()
    event = (
        store.client("dependency")
        .write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                    "synthetic",
                )
            ],
            Context("system", 1),
        )
        .check()
        .events[0]
    )
    provider = SyntheticProvider()
    provider_module = ModuleType("ear_synthetic_cli_provider")
    monkeypatch.setattr(provider_module, "create_provider", lambda: provider, raising=False)
    monkeypatch.setitem(sys.modules, provider_module.__name__, provider_module)
    monkeypatch.setenv("EAR_GATEWAY_STORAGE_TOKEN", OTHER)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ear-llm-gateway",
            "--storage-url",
            "http://synthetic.test",
            "--journal",
            str(tmp_path / "cli.sqlite3"),
            "--provider-factory",
            "ear_synthetic_cli_provider:create_provider",
            "--aliases",
            '{"default":"synthetic/model"}',
        ],
    )
    preflights, clients, calls = [], [], []

    def wire(request):
        token = request.headers["authorization"].removeprefix("Bearer ")
        assert token in {TOKEN, OTHER}
        module = "extraction" if token == TOKEN else "llm_gateway"
        client = store.client(module)
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json={"codec": 1, "module": module})
        if request.url.path == "/v1/llm/call-id-check":
            assert token == OTHER
            cid = json.loads(request.content)["call_id"]
            return httpx.Response(200, json={"occupied": client.call_id_occupied(cid)})
        if request.url.path == "/v1/llm/preflight":
            assert token == TOKEN  # Never use the privileged audit writer's token.
            preflights.append(json.loads(request.content))
            try:
                result = client.preflight_llm_call(
                    LLMCallPreflight.model_validate_json(request.content)
                )
            except InvalidRequest:
                return httpx.Response(422, json={"code": "invalid_input"})
            return httpx.Response(200, json=result.model_dump(mode="json"))
        if request.url.path == "/v1/read":
            parsed, values = decode_read(request.content)
            return httpx.Response(
                200,
                text=encode_read_result(parsed.method, getattr(client, parsed.method)(**values)),
            )
        assert request.url.path == "/v1/batches" and token == OTHER
        body = json.loads(request.content)
        body.pop("request_id")
        ops, context = decode_request(json.dumps(body))
        return httpx.Response(200, text=encode_result(client.write(ops, context)))

    def remote(token, url, **kwargs):
        assert url.rstrip("/") == "http://synthetic.test"
        client = RemoteStorageClient(token, url, transport=httpx.MockTransport(wire), **kwargs)
        clients.append(client)
        return client

    def run(app, **kwargs):
        assert kwargs["workers"] == 1 and kwargs["access_log"] is False
        with TestClient(app) as client:
            invalid = Harness.call(request_event_id=new_id(), depth=1)
            response = client.post("/v1/complete", json=invalid, headers=HEADERS)
            assert response.status_code == 403
            assert not provider.calls
            call = Harness.call(request_event_id=event.event_id, depth=1)
            calls.append(call)
            response = client.post("/v1/complete", json=call, headers=HEADERS)
            assert response.status_code == 200, response.text

    monkeypatch.setattr(cli, "RemoteStorageClient", remote)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    cli.main()
    assert len(provider.calls) == 1 and len(preflights) == 4
    assert all(client._http.is_closed for client in clients)
    assert all("data" not in preflight and "system" not in preflight for preflight in preflights)
    saved = store.client("llm_gateway").get_system_record(calls[0]["call_id"])
    assert saved is not None and saved.payload["request_event_id"] == event.event_id
    assert saved.payload["additional_causes"][0]["cause"]["data_id"] == source
