"""No-database validation/security tests for the public boundary and SDK transport."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx
import psycopg
import pytest
from ear_contracts import new_id
from ear_storage.auth import AuthenticationError as ServerAuthError
from ear_storage.http import BoundaryMiddleware, create_app
from ear_storage.schema import ProjectionLimit
from ear_storage.service import StorageService
from ear_storage.snapshots import SnapshotUnavailable as ServerSnapshotError
from ear_storage_client import Context, Create, InvalidRequest, MemoryStorage, Query
from ear_storage_client.errors import PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import (
    AuthenticationError,
    CommitUnknown,
    ProtocolError,
    ResourceLimit,
    ServiceUnavailable,
    SnapshotUnavailable,
)
from ear_storage_client.wire import encode_request
from fastapi.testclient import TestClient

TOKEN = "t" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


class Dummy:
    def __init__(self):
        self.error = None
        self.memory = MemoryStorage().client("extraction")

    def client(self, token):
        if self.error:
            raise self.error
        if token != TOKEN:
            raise ServerAuthError("secret diagnostic " + token)
        return self.memory


class TestTransport(httpx.BaseTransport):
    __test__ = False

    def __init__(self, client: TestClient):
        self.client = client

    def handle_request(self, request):
        response = self.client.request(
            request.method, request.url.path, headers=request.headers, content=request.read()
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )


@pytest.fixture
def dummy():
    return Dummy()


@pytest.fixture
def http(dummy):
    with TestClient(create_app(cast(StorageService, dummy))) as client:
        yield client


def batch():
    value = json.loads(encode_request([Create("activity", new_id(), {})], Context.system()))
    value["request_id"] = new_id()
    return value


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "bearer " + TOKEN,
        "Bearer  " + TOKEN,
        "Bearer " + TOKEN + " ",
        "Basic " + TOKEN,
        "Bearer " + TOKEN + ",Bearer " + TOKEN,
    ],
)
def test_strict_bearer(http, header):
    headers = {} if header is None else {"Authorization": header}
    response = http.get("/v1/identity", headers=headers)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.text


def test_duplicate_authorization_and_metadata(http):
    response = http.get("/v1/identity", headers=[("Authorization", "Bearer " + TOKEN)] * 2)
    assert response.status_code == 401
    assert http.get("/v1/identity", headers=HEADERS).json() == {"codec": 1, "module": "extraction"}
    assert http.get("/v1/registry", headers=HEADERS).status_code == 200
    assert http.get("/docs").status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"null",
        b"[]",
        b'{"codec":NaN}',
        b'{"codec":1,"codec":1}',
        b'{"codec":1e999}',
        b'"text"',
    ],
)
@pytest.mark.parametrize("path", ["/v1/batches", "/v1/read", "/v1/snapshots"])
def test_malformed_json_no_diagnostics(http, payload, path):
    response = http.post(path, content=payload, headers=HEADERS)
    assert response.status_code == 422
    assert response.json()["details"] == {}


@pytest.mark.parametrize("mutation", ["module", "context", "operation", "body", "unknown", "codec"])
def test_batch_closed_fields(http, mutation):
    value = batch()
    if mutation == "module":
        value["module"] = "attribution"
    elif mutation == "context":
        value["context"]["module"] = "attribution"
    elif mutation == "operation":
        value["ops"][0]["optional_deps"] = []
    elif mutation == "body":
        value["ops"][0]["body"]["deps"] = []
    elif mutation == "unknown":
        value["ops"][0]["op"] = "FutureOperation"
    else:
        value["codec"] = True
    assert http.post("/v1/batches", json=value, headers=HEADERS).status_code == 422


@pytest.mark.parametrize(
    "method,args",
    [
        ("__dict__", {}),
        ("get", {"id": new_id(), "module": "adapter"}),
        ("get_version", {"id": new_id(), "version": True}),
        ("get_version", {"id": new_id(), "version": 0}),
        ("get_many", {"ids": [new_id()] * 1001}),
        ("query", {"query": {"data_type": "activity", "dependencies": []}}),
        ("query", {"query": {"data_type": "activity", "limit": True}}),
        (
            "query",
            {
                "query": {
                    "data_type": "activity",
                    "time": {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-02T00:00:00Z"},
                }
            },
        ),
        ("stale_dependencies", {"targets": [], "only_current_dependents": "false"}),
    ],
)
def test_closed_typed_read_arguments(http, method, args):
    response = http.post(
        "/v1/read", json={"codec": 1, "method": method, "args": args}, headers=HEADERS
    )
    assert response.status_code == 422


@pytest.mark.parametrize("where", ["top", "ref"])
def test_dependency_unknown_fields_rejected(http, where):
    value = batch()
    dep = {"ref": {"data_type": "person", "id": new_id(), "version": 1}, "fields": ["x"]}
    (dep if where == "top" else dep["ref"])["unsupported"] = "must not be ignored"
    value["ops"] = [
        {
            "op": "RegisterDependencies",
            "body": {"dependent_type": "activity", "dependent_id": new_id(), "depends_on": [dep]},
        }
    ]
    assert http.post("/v1/batches", json=value, headers=HEADERS).status_code == 422


@pytest.mark.parametrize(
    "error,status",
    [
        (ServerAuthError, 401),
        (PermissionDenied, 403),
        (InvalidRequest, 422),
        (ServerSnapshotError, 410),
        (ProjectionLimit, 413),
        (psycopg.OperationalError, 503),
        (RuntimeError, 500),
    ],
)
def test_sanitized_errors(http, dummy, error, status, caplog):
    dummy.error = error("password=secret DSN SQL " + TOKEN)
    response = http.get("/v1/identity", headers=HEADERS)
    assert response.status_code == status
    assert all(
        secret not in response.text + caplog.text for secret in (TOKEN, "DSN", "SQL", "password")
    )


def test_streaming_limit_and_timeout_before_dispatch():
    async def exercise():
        called = False
        sent = []
        chunks = iter([b"123456", b"789012", b"never read"])

        async def app(scope, receive, send):
            nonlocal called
            called = True

        async def receive():
            return {"type": "http.request", "body": next(chunks), "more_body": True}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "headers": [(b"content-length", b"1")]}
        await BoundaryMiddleware(app, 10, 1)(cast(Any, scope), receive, send)
        assert not called and sent[0]["status"] == 413
        assert next(chunks) == b"never read"
        sent.clear()

        async def slow():
            await asyncio.sleep(0.1)
            return {"type": "http.request", "body": b""}

        await BoundaryMiddleware(app, 10, 0.001)(cast(Any, scope), slow, send)
        assert not called and sent[0]["status"] == 408

    asyncio.run(exercise())


def test_http_body_limit(dummy):
    with TestClient(create_app(cast(StorageService, dummy), max_body_bytes=8)) as client:
        response = client.post("/v1/read", content=b"x" * 9, headers=HEADERS)
        assert response.status_code == 413


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@localhost",
        "http://localhost/?token=secret",
        "http://localhost/#secret",
        "file:///tmp/test",
        "http://localhost/v1",
    ],
)
def test_safe_sdk_endpoint(url):
    with pytest.raises(InvalidRequest) as exc:
        RemoteStorageClient(TOKEN, url)
    assert "secret" not in str(exc.value) and "password" not in str(exc.value)


@pytest.mark.parametrize(
    "status,error",
    [
        (401, AuthenticationError),
        (403, PermissionDenied),
        (422, InvalidRequest),
        (413, ResourceLimit),
        (410, SnapshotUnavailable),
        (503, CommitUnknown),
        (500, CommitUnknown),
        (307, CommitUnknown),
    ],
)
def test_write_known_vs_unknown_no_hidden_retry(status, error):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret diagnostics " + TOKEN)

    with RemoteStorageClient(TOKEN, "http://test", transport=httpx.MockTransport(handler)) as sdk:
        rid = new_id()
        with pytest.raises(error) as exc:
            sdk.write([], Context.system(), request_id=rid)
        assert TOKEN not in repr(sdk) + str(exc.value) + repr(exc.value)
        if isinstance(exc.value, CommitUnknown):
            assert exc.value.request_id == rid
    assert len(calls) == 1


def test_lost_transport_and_unknown_future_conflict():
    def lost(request):
        raise httpx.ReadTimeout("secret " + TOKEN, request=request)

    with RemoteStorageClient(TOKEN, "http://test", transport=httpx.MockTransport(lost)) as sdk:
        with pytest.raises(CommitUnknown) as exc:
            sdk.write([], Context.system())
        assert TOKEN not in str(exc.value)
        with pytest.raises(ServiceUnavailable):
            sdk.count(Query("activity"))

    def future(request):
        return httpx.Response(
            200,
            json={
                "codec": 1,
                "result": {
                    "batch_id": None,
                    "conflict": {"index": 0, "reason": "future", "message": "new conflict"},
                },
            },
        )

    with RemoteStorageClient(TOKEN, "http://test", transport=httpx.MockTransport(future)) as sdk:
        with pytest.raises(CommitUnknown):
            sdk.write([], Context.system())
        with pytest.raises(ProtocolError):
            sdk.subscribe("extraction", lambda _: None)


def test_injected_sync_transport_metadata_and_reads(http):
    with RemoteStorageClient(TOKEN, "http://test", transport=TestTransport(http)) as sdk:
        assert sdk.module == "extraction"
        assert sdk.registry.is_core("activity")
        assert sdk.count(Query("activity")) == 0
        assert sdk.query(Query("activity")).items == ()
        assert sdk.get(new_id()) is None


def test_invalid_metadata_response():
    with (
        RemoteStorageClient(
            TOKEN,
            "http://test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"codec": 2})),
        ) as sdk,
        pytest.raises(ProtocolError),
    ):
        _ = sdk.module


@pytest.mark.parametrize(
    "body",
    [
        "null",
        "[]",
        '"text"',
        '{"codec":true,"result":{}}',
        '{"codec":1,"result":{}}',
        '{"codec":1,"result":{"batch_id":null,"conflict":'
        '{"index":0,"reason":"future","message":"secret"}}}',
    ],
)
def test_malformed_success_receipt_is_commit_unknown(body):
    with (
        RemoteStorageClient(
            TOKEN,
            "http://test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
        ) as sdk,
        pytest.raises(CommitUnknown),
    ):
        sdk.write([], Context.system())


def test_invalid_unicode_rejected_before_db(http):
    response = http.post("/v1/batches", headers=HEADERS, content=b'{"bad":"\\ud800"}')
    assert response.status_code == 422


def test_retry_helper_does_not_replan_unknown_commit():
    from ear_storage_client import write_with_retry

    plans, writes, releases = [], [], []
    sid = new_id()

    def handler(request):
        if request.method == "DELETE":
            releases.append(request)
            return httpx.Response(200, json={"codec": 1, "released": True})
        if request.url.path == "/v1/snapshots":
            return httpx.Response(200, json={"codec": 1, "lease": {"snapshot_id": sid}})
        writes.append(request)
        assert releases, "snapshot must release before write starts"
        raise httpx.ReadTimeout("lost", request=request)

    def plan(reader):
        plans.append(reader)
        return [Create("activity", new_id(), {})]

    with (
        RemoteStorageClient(TOKEN, "http://test", transport=httpx.MockTransport(handler)) as sdk,
        pytest.raises(CommitUnknown),
    ):
        write_with_retry(sdk, plan, Context.system())
    assert len(plans) == len(writes) == len(releases) == 1


def test_snapshot_finally_releases_on_plan_error():
    seen = []
    sid = new_id()

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"codec": 1, "lease": {"snapshot_id": sid}})

    with (
        RemoteStorageClient(TOKEN, "http://test", transport=httpx.MockTransport(handler)) as sdk,
        pytest.raises(RuntimeError, match="plan failed"),
        sdk.snapshot(),
    ):
        raise RuntimeError("plan failed")
    assert seen == [("POST", "/v1/snapshots"), ("DELETE", "/v1/snapshots/" + sid)]


def test_module_expectation_never_authorizes_or_sends_module(http):
    with pytest.raises(PermissionDenied):
        RemoteStorageClient(TOKEN, "http://test", "attribution", transport=TestTransport(http))


def test_cli_serve_explicit_configuration_and_failure_redaction(monkeypatch, capsys):
    import ear_storage.http as http_module
    import uvicorn
    from ear_storage import cli

    seen = []
    app = object()
    monkeypatch.setenv("EAR_STORAGE_DSN", "password=deployment-secret")
    monkeypatch.setattr("sys.argv", ["ear-storage", "serve", "--port", "8101"])
    monkeypatch.setattr(http_module, "create_app_from_dsn", lambda dsn: app)
    monkeypatch.setattr(uvicorn, "run", lambda actual, **kwargs: seen.append((actual, kwargs)))
    cli.main()
    assert seen[0][0] is app
    assert seen[0][1]["port"] == 8101
    assert seen[0][1]["access_log"] is False
    assert seen[0][1]["limit_concurrency"] == 64

    def failed(dsn):
        raise RuntimeError(dsn)

    monkeypatch.setattr(http_module, "create_app_from_dsn", failed)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "deployment-secret" not in capsys.readouterr().err


def test_client_distribution_has_no_service_imports():
    import ast
    from pathlib import Path

    import ear_storage_client

    root = Path(ear_storage_client.__file__).parent
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("ear_storage.")
            elif isinstance(node, ast.Import):
                assert not any(
                    name.name == "ear_storage" or name.name.startswith("ear_storage.")
                    for name in node.names
                )
