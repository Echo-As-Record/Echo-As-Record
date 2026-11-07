"""Synthetic opt-in bodies; no models, secrets, network headers or environment capture."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import LLMCallRecord, new_id, utcnow
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.cli import model_body_policy_from_env
from ear_storage.service import StorageService
from ear_storage_client import (
    InvalidRequest,
    MemoryStorage,
    ModelBodyPolicy,
    PermissionDenied,
    PutSystemRecord,
    Query,
)
from ear_storage_client.engine import State, Transaction
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.wire import encode_result
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .test_completion import capable_registry
from .test_http_postgres import live_server
from .test_llm_calls import SYS, payload


@pytest.fixture(
    params=[
        "memory",
        pytest.param("postgres", marks=pytest.mark.postgres),
        pytest.param("http", marks=pytest.mark.postgres),
    ]
)
def bodies(request):
    policy = ModelBodyPolicy(enabled=True, max_bytes=8, allowed_callers=("extraction",))
    dsn = operator = issued = None
    if request.param == "memory":
        store = MemoryStorage(capable_registry(), model_body_policy=policy)
        client = store.client("llm_gateway")
        server = nullcontext(None)
    else:
        dsn = request.getfixturevalue("pg_dsn")
        migrate(dsn)
        bootstrap(dsn, capable_registry())
        store = PostgresStorage(dsn, model_body_policy=policy)
        client = store.client("llm_gateway")
        operator = AuthOperator(dsn)
        issued = operator.issue_principal(
            "llm_gateway", read_types=["llm_call"], subscribe_types=[]
        )
        server = (
            live_server(StorageService(dsn, model_body_policy=policy))
            if request.param == "http"
            else nullcontext(None)
        )
    with (
        server as url,
        RemoteStorageClient(issued.token, url) if url and issued else nullcontext(client) as c,
    ):
        yield store, c, dsn, operator, issued


@pytest.mark.parametrize("request_text,response_text", [("", ""), ("中文", "é"), (None, "🦉🦉")])
def test_allowed_roundtrip_and_result_has_no_body(bodies, request_text, response_text):
    store, client, *_ = bodies
    op = PutSystemRecord(
        "llm_call", new_id(), payload(request_text=request_text, response_text=response_text)
    )
    result = client.write([op], SYS).check()
    assert result.events == ()
    encoded = encode_result(result)
    assert "request_text" not in encoded and "response_text" not in encoded
    assert all(not hasattr(item, "payload") for item in result.results)
    saved = client.get_system_record(op.id)
    assert saved.payload == op.payload
    assert client.query(Query("llm_call")).items == (saved,)
    with (
        client.snapshot()
        if not isinstance(store, PostgresStorage) or isinstance(client, RemoteStorageClient)
        else nullcontext(client) as reader
    ):
        assert reader.get_system_record(op.id) == saved
    with pytest.raises(InvalidRequest):
        client.write(
            [PutSystemRecord("llm_call", op.id, payload(request_text="new"), expected_revision=1)],
            SYS,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"request_text": "中文", "response_text": "abc"},
        {"request_text": "", "caller_module": "dependency"},
        {"response_text": "x", "include_body": True},
        {"request_text": {"text": "x"}},
        {"response_text": 12},
        {"request_text": "x", "headers": {"Authorization": "synthetic"}},
        {"request_text": "x", "usage": {"input_tokens": 0, "request_text": "x"}},
        {"request_text": "x", "model_body_policy": {"enabled": True}},
    ],
)
def test_scope_size_and_closed_fields(bodies, changes):
    _, client, *_ = bodies
    with pytest.raises(InvalidRequest):
        client.write([PutSystemRecord("llm_call", new_id(), {**payload(), **changes})], SYS)
    assert client.count(Query("llm_call")) == 0


def test_only_gateway_can_write_even_when_enabled(bodies):
    store, _, *_ = bodies
    with pytest.raises(PermissionDenied):
        store.client("extraction").write(
            [PutSystemRecord("llm_call", new_id(), payload(request_text="x"))], SYS
        )


def test_disable_preserves_read_and_receipt(bodies):
    store, client, dsn, _, issued = bodies
    op = PutSystemRecord("llm_call", new_id(), payload(request_text="saved"))
    receipt = new_id()
    first = (
        client.write([op], SYS)
        if isinstance(store, MemoryStorage)
        else client.write([op], SYS, request_id=receipt)
    ).check()
    old = client.get_system_record(op.id)
    if isinstance(store, MemoryStorage):
        store.model_body_policy = ModelBodyPolicy()
        disabled = nullcontext(client)
        server = nullcontext(None)
    elif isinstance(client, RemoteStorageClient):
        server = live_server(StorageService(dsn))
        disabled = nullcontext(client)
    else:
        server = nullcontext(None)
        disabled = nullcontext(PostgresStorage(dsn).client("llm_gateway"))
    with server as url, RemoteStorageClient(issued.token, url) if url else disabled as off:
        assert off.get_system_record(op.id) == old
        assert off.query(Query("llm_call")).items == (old,)
        if not isinstance(store, MemoryStorage):
            replay = off.write([op], SYS, request_id=receipt)
            assert replay == first
            assert "request_text" not in encode_result(replay)
            assert "saved" not in encode_result(replay)
        for body in ("", "new"):
            with pytest.raises(InvalidRequest):
                off.write([PutSystemRecord("llm_call", new_id(), payload(request_text=body))], SYS)
        with pytest.raises(InvalidRequest):
            off.write([PutSystemRecord("llm_call", op.id, op.payload, expected_revision=1)], SYS)
        off.write([PutSystemRecord("llm_call", new_id(), payload())], SYS).check()


@pytest.mark.parametrize("field", ["request_text", "response_text"])
@pytest.mark.parametrize("value", [True, 12, 1.1, float("nan"), float("inf"), {}, [], b"text"])
def test_strict_body_types(field, value):
    with pytest.raises(ValidationError):
        LLMCallRecord.model_validate({**payload(), field: value})


def test_policy_none_empty_scope_and_configuration():
    call = LLMCallRecord.model_validate(payload(request_text=""))
    ModelBodyPolicy(enabled=True).validate(call)
    for policy in (ModelBodyPolicy(), ModelBodyPolicy(enabled=True, allowed_callers=())):
        with pytest.raises(InvalidRequest):
            policy.validate(call)
    invalid_configs: tuple[dict[str, Any], ...] = (
        {"enabled": "false"},
        {"max_bytes": True},
        {"max_bytes": 0},
        {"max_bytes": float("inf")},
        {"allowed_callers": ["extraction"]},
        {"allowed_callers": ("*",)},
    )
    for kw in invalid_configs:
        with pytest.raises(ValueError):
            ModelBodyPolicy(**kw)


def test_env_strict_default_false_and_scope():
    assert model_body_policy_from_env({}) == ModelBodyPolicy()
    assert not model_body_policy_from_env({"EAR_MODEL_BODY_ENABLED": "false"}).enabled
    assert model_body_policy_from_env(
        {
            "EAR_MODEL_BODY_ENABLED": "true",
            "EAR_MODEL_BODY_MAX_BYTES": "8",
            "EAR_MODEL_BODY_ALLOWED_CALLERS": "extraction,dependency",
        }
    ) == ModelBodyPolicy(True, 8, ("extraction", "dependency"))
    assert model_body_policy_from_env({"EAR_MODEL_BODY_ALLOWED_CALLERS": ""}).allowed_callers == ()


@pytest.mark.parametrize(
    "name,values",
    [
        ("EAR_MODEL_BODY_ENABLED", ["False", "TRUE", "0", "1", "yes", "", "false "]),
        ("EAR_MODEL_BODY_MAX_BYTES", ["0", "-1", "1.0", "nan", "inf", "true", ""]),
        ("EAR_MODEL_BODY_ALLOWED_CALLERS", ["*", "extraction,", " extraction", "a.b"]),
    ],
)
def test_bad_env_fails_closed(name, values):
    for value in values:
        with pytest.raises(ValueError):
            model_body_policy_from_env({name: value})


def test_lowest_engine_entry_defaults_off_and_metadata_unchanged():
    registry = capable_registry()
    txn = Transaction(State(), registry, "llm_gateway", SYS, utcnow(), new_id())
    for body in ("", "synthetic"):
        with pytest.raises(InvalidRequest):
            txn._put_system(PutSystemRecord("llm_call", new_id(), payload(request_text=body)))
    assert txn.s.system == {}
    op = PutSystemRecord(
        "llm_call", new_id(), {**payload(), "request_text": None, "response_text": None}
    )
    txn._put_system(op)
    assert "request_text" not in txn.s.system[op.id].payload
    assert "response_text" not in txn.s.system[op.id].payload


def test_schema_accepts_body_shape_but_not_wrong_types():
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/records/llm-call-record.schema.json"
        ).read_text()
    )
    validator = Draft202012Validator(schema)
    for value in (None, "", "synthetic"):
        validator.validate({**payload(), "request_text": value, "response_text": value})
    for value in (False, 12, {}, []):
        assert list(validator.iter_errors({**payload(), "request_text": value}))


@pytest.mark.postgres
def test_http_factory_injects_into_real_backend(pg_dsn):
    from ear_storage import http
    from ear_storage_client.wire import encode_request
    from fastapi.testclient import TestClient

    migrate(pg_dsn)
    bootstrap(pg_dsn)
    policy = ModelBodyPolicy(True)
    issued = AuthOperator(pg_dsn).issue_principal(
        "llm_gateway", read_types=["llm_call"], subscribe_types=[]
    )
    headers = {"Authorization": "Bearer " + issued.token}
    op = PutSystemRecord("llm_call", new_id(), payload(request_text="synthetic"))
    batch = {**json.loads(encode_request([op], SYS)), "request_id": new_id()}
    with TestClient(http.create_app_from_dsn(pg_dsn, model_body_policy=policy)) as client:
        response = client.post("/v1/batches", json=batch, headers=headers)
        assert response.status_code == 200
        assert "synthetic" not in response.text and "request_text" not in response.text
    with TestClient(http.create_app_from_dsn(pg_dsn)) as client:
        assert client.post("/v1/batches", json=batch, headers=headers).json() == response.json()
        batch["request_id"] = new_id()
        assert client.post("/v1/batches", json=batch, headers=headers).status_code == 422
        batch["model_body_policy"] = {"enabled": True}
        assert client.post("/v1/batches", json=batch, headers=headers).status_code == 422
    saved = PostgresStorage(pg_dsn).get_system_record(op.id)
    assert saved is not None and saved.payload["request_text"] == "synthetic"
    with pytest.raises(InvalidRequest):
        PostgresStorage(pg_dsn).client("llm_gateway").write(
            [PutSystemRecord("llm_call", new_id(), op.payload)], SYS
        )


@pytest.mark.parametrize("enabled", ["false", "true", "invalid"])
def test_cli_forwards_deployment_policy_and_rejects_bad_env(monkeypatch, capsys, enabled):
    import uvicorn
    from ear_storage import cli, http

    captured = []
    monkeypatch.setenv("EAR_STORAGE_DSN", "synthetic")
    monkeypatch.delenv("EAR_NATS_SERVERS", raising=False)
    monkeypatch.setenv("EAR_MODEL_BODY_ENABLED", enabled)
    monkeypatch.setenv("EAR_MODEL_BODY_MAX_BYTES", "8")
    monkeypatch.setenv("EAR_MODEL_BODY_ALLOWED_CALLERS", "extraction")
    monkeypatch.setattr("sys.argv", ["ear-storage", "serve"])
    monkeypatch.setattr(http, "create_app_from_dsn", lambda dsn, **kw: captured.append(kw))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kw: None)
    if enabled == "invalid":
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 1
        assert captured == []
        assert "invalid" not in capsys.readouterr().err
    else:
        cli.main()
        assert captured[0]["model_body_policy"] == ModelBodyPolicy(
            enabled == "true", 8, ("extraction",)
        )
