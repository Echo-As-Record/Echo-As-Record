"""Synthetic source-derived HTTP replays, not recordings from a production model."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager, nullcontext

import httpx
import pytest
import uvicorn
from ear_contracts import new_id
from ear_llm_gateway import CallRequest, Gateway, ProviderFailure
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.models import GatewayError
from ear_llm_gateway.octopus import CHAT_PATH, SOURCE_COMMIT, OctopusProvider, create_provider
from ear_llm_gateway.output_json import OutputSchema, encode, strict_json
from ear_storage_client import MemoryStorage
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

SECRET = "synthetic-manager-token"
MODEL = "synthetic/model"
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def call(**changes):
    return CallRequest.model_validate(
        {
            "call_id": new_id(),
            "module": "extraction",
            "purpose": "synthetic",
            "root_feedback_id": "system",
            "depth": 0,
            "system": "SENSITIVE_PROMPT",
            "data": {"input": "synthetic"},
            "output_schema": SCHEMA,
            **changes,
        }
    )


def envelope(content='{"answer":7}'):
    # Mirrors router_models._build_openai_response; usage is deliberately synthetic.
    return {
        "id": "chatcmpl-synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "logprobs": None,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": None,
                    "tool_calls": None,
                },
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3},
        "system_fingerprint": None,
        "service_tier": None,
    }


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay: float = 0, failure=None):
        self.chunks, self.delay, self.failure = chunks, delay, failure
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True


def replay(*, raw=None, status=200, headers=None, stream=None, **config):
    requests = []
    stream = stream or Bytes([raw if raw is not None else encode(envelope())])

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, headers={"content-type": "application/json", **(headers or {})}, stream=stream
        )

    adapter = OctopusProvider(
        "https://manager.invalid",
        SECRET,
        models={MODEL: "litellm"},
        service="synthetic/ear",
        transport=httpx.MockTransport(handler),
        **config,
    )
    return adapter, requests, stream


def test_source_pin_request_mapping_schema_and_unknown_usage(caplog):
    adapter, requests, stream = replay()
    result = adapter.complete(
        call(data={"api_key": "UNTRUSTED", "base_url": "https://evil.invalid"}),
        model=MODEL,
        timeout=1,
    )
    assert SOURCE_COMMIT == "b3c7cd5dd950a79e94533b114619082a542eac66"
    assert result.output == {"answer": 7} and result.usage is None
    assert len(requests) == 1 and stream.closed
    request = requests[0]
    assert str(request.url) == "https://manager.invalid" + CHAT_PATH
    assert request.headers["authorization"] == "Bearer " + SECRET
    assert request.headers["accept-encoding"] == "identity"
    payload = json.loads(request.content)
    assert set(payload) == {
        "messages",
        "model",
        "provider",
        "service",
        "stream",
        "timeout_secs",
        "max_tokens",
    }
    assert payload["provider"] == "litellm" and payload["model"] == MODEL
    assert payload["service"] == "synthetic/ear" and payload["stream"] is False
    assert payload["timeout_secs"] == 1 and payload["max_tokens"] == 4096
    assert json.loads(payload["messages"][-1]["content"])["data"]["api_key"] == "UNTRUSTED"
    assert SECRET not in repr(adapter) + caplog.text
    assert "SENSITIVE_PROMPT" not in caplog.text


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 422, 429, 500, 502, 503])
def test_status_never_authorizes_retry_or_redirect(status):
    adapter, requests, stream = replay(
        status=status,
        headers={"location": "https://evil.invalid"},
        raw=b"SENSITIVE_PROMPT " + SECRET.encode(),
    )
    with pytest.raises(ProviderFailure) as error:
        adapter.complete(call(), model=MODEL, timeout=1)
    assert not error.value.safe_to_retry and len(requests) == 1 and stream.closed
    assert str(error.value) == "provider_outcome_unknown"


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>secret</html>",
        b"[]",
        b'{"object":"chat.completion","object":"duplicate"}',
        b'{"value":NaN}',
        b'{"value":1e999}',
        b"\xff",
        b"[" * 49 + b"]" * 49,
        b'{"value":"\\ud800"}',
        b"{} trailing",
    ],
)
def test_strict_json_envelope_failures(raw):
    adapter, requests, _ = replay(raw=raw)
    with pytest.raises(ProviderFailure):
        adapter.complete(call(), model=MODEL, timeout=1)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "null",
        '```json\n{"answer":7}\n```',
        '{"answer":true}',
        '{"answer":"7"}',
        '{"answer":7,"extra":1}',
        "{}",
        '{"answer":NaN}',
        '{"answer":7,"answer":8}',
    ],
)
def test_output_object_and_schema_rejection(content):
    adapter, _, _ = replay(raw=encode(envelope(content)))
    with pytest.raises(ProviderFailure):
        adapter.complete(call(), model=MODEL, timeout=1)


@pytest.mark.parametrize(
    "mutation", ["choices", "role", "finish", "tool", "usage", "unknown", "index"]
)
def test_envelope_shape_drift(mutation):
    body = envelope()
    if mutation == "choices":
        body["choices"] *= 2
    elif mutation == "role":
        body["choices"][0]["message"]["role"] = "user"
    elif mutation == "finish":
        body["choices"][0]["finish_reason"] = "length"
    elif mutation == "tool":
        body["choices"][0]["message"]["tool_calls"] = []
    elif mutation == "usage":
        body["usage"]["prompt_tokens"] = 4
    elif mutation == "index":
        body["choices"][0]["index"] = False
    else:
        body["unknown"] = True
    adapter, _, _ = replay(raw=encode(body))
    with pytest.raises(ProviderFailure):
        adapter.complete(call(), model=MODEL, timeout=1)


@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": "4097"},
        {"content-length": "-1"},
        {"content-length": "4, 4"},
        {"content-length": "1"},
        {"content-encoding": "gzip"},
        {"content-type": "text/event-stream"},
    ],
)
def test_response_headers_fail_closed(headers):
    adapter, _, stream = replay(headers=headers, max_response_bytes=4096)
    with pytest.raises(ProviderFailure):
        adapter.complete(call(), model=MODEL, timeout=1)
    assert stream.closed


def test_chunk_limit_disconnect_and_total_deadline():
    for stream in (
        Bytes([b"x" * 33, b"x" * 33]),
        Bytes([b"{"], failure=httpx.ReadError("SENSITIVE_PROMPT")),
        Bytes([b" " for _ in range(100)], delay=0.01),
    ):
        adapter, requests, _ = replay(stream=stream, max_response_bytes=64)
        started = time.monotonic()
        with pytest.raises(ProviderFailure):
            adapter.complete(call(), model=MODEL, timeout=0.08)
        assert time.monotonic() - started < 1
        assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "unapproved"},
        {"output_schema": {"$ref": "https://evil.invalid/schema"}},
        {"output_schema": {"pattern": "(a+)+$"}},
        {"output_schema": {"format": "unsupported-format"}},
        {"output_schema": {"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}},
    ],
)
def test_unsupported_schema_and_model_rejected_before_http(changes):
    adapter, requests, _ = replay()
    model = changes.get("model", MODEL)
    with pytest.raises(ProviderFailure):
        adapter.complete(call(**changes), model=model, timeout=1)
    assert not requests


def test_request_limit_and_invalid_timeout_before_http():
    adapter, requests, _ = replay(max_request_bytes=1)
    for timeout in [1, float("nan"), float("inf"), 0, 121]:
        with pytest.raises(ProviderFailure):
            adapter.complete(call(), model=MODEL, timeout=timeout)
    assert not requests


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@manager.invalid",
        "https://manager.invalid/path",
        "https://manager.invalid?token=x",
        "https://manager.invalid#frag",
        "ftp://manager.invalid",
        "http://manager.invalid",
    ],
)
def test_unsafe_origins_rejected(origin):
    with pytest.raises(ValueError, match="invalid_octopus_configuration"):
        OctopusProvider(origin, SECRET, models={MODEL: "litellm"}, service="synthetic/ear")


def test_factory_configuration_and_defensive_model_copy(monkeypatch):
    models = {MODEL: "litellm"}
    provider = OctopusProvider(
        "https://manager.invalid", SECRET, models=models, service="synthetic/ear"
    )
    models[MODEL] = "openai"
    assert provider._models[MODEL] == "litellm"
    for key, value in {
        "ORIGIN": "https://manager.invalid",
        "TOKEN": SECRET,
        "MODELS": json.dumps(models),
        "SERVICE": "synthetic/ear",
    }.items():
        monkeypatch.setenv("EAR_OCTOPUS_" + key, value)
    assert isinstance(create_provider(), OctopusProvider)
    monkeypatch.setenv("EAR_OCTOPUS_MODELS", '{"model":"provider-instance-name"}')
    with pytest.raises(ValueError, match="invalid_octopus_configuration"):
        create_provider()
    monkeypatch.setenv("EAR_OCTOPUS_TOKEN", "header\r\ninjection")
    with pytest.raises(ValueError, match="invalid_octopus_configuration"):
        create_provider()


def test_schema_local_refs_nullable_constraints_and_json_equality():
    schema = OutputSchema(
        {
            "type": "object",
            "additionalProperties": False,
            "$defs": {"Item": {"type": "integer", "minimum": 1, "maximum": 5}},
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {"$ref": "#/$defs/Item"},
                },
                "label": {"anyOf": [{"type": "null"}, {"type": "string", "maxLength": 3}]},
            },
            "required": ["items", "label"],
        }
    )
    schema.validate({"items": [1, 2], "label": None})
    for bad in (
        {"items": [0], "label": None},
        {"items": [], "label": None},
        {"items": [1], "label": "long"},
        {"items": [True], "label": None},
    ):
        with pytest.raises(ValueError):
            schema.validate(bad)
    with pytest.raises(ValueError):
        OutputSchema({"const": {"n": True}}).validate({"n": 1})
    assert strict_json('{"text":"[escaped \\" quote]"}')


@contextmanager
def server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    runner = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=lambda: runner.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not runner.started and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert runner.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        runner.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_real_loopback_http_proxy_isolation_audit_and_no_unknown_retry(
    tmp_path, monkeypatch, caplog
):
    app, requests = FastAPI(), []
    outcome = ["success"]

    @app.post(CHAT_PATH)
    async def completion(request: Request):
        requests.append((dict(request.headers), await request.json()))
        if outcome[0] == "redirect":
            return Response(status_code=307, headers={"Location": "/leaked"})
        if outcome[0] == "slow":

            async def slow():
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    yield b" "

            return StreamingResponse(slow(), media_type="application/json")
        return Response(encode(envelope()), media_type="application/json")

    @app.post("/leaked")
    async def leaked():
        pytest.fail("redirect followed")

    # Any accidental ambient proxy use fails, instead of accessing the network.
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(variable, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    store = MemoryStorage()
    writer = store.client("llm_gateway")
    journal = Journal(str(tmp_path / "octopus.sqlite3"))
    try:
        with server(app) as origin:
            provider = OctopusProvider(
                origin, SECRET, models={MODEL: "litellm"}, service="synthetic/ear"
            )
            gateway = Gateway(
                callers=lambda _: nullcontext(store.client("extraction")),
                writer=writer,
                provider=provider,
                journal=journal,
                aliases={"default": MODEL},
                attempts=3,
                timeout=1,
            )
            request = call()
            result = gateway.complete("synthetic-caller-token", request)
            assert result["output"] == {"answer": 7} and result["usage"] is None
            record = writer.get_system_record(request.call_id)
            assert record is not None
            assert record.payload["usage"] is None
            assert "request_text" not in record.payload and "response_text" not in record.payload
            assert (
                gateway.stats("synthetic-caller-token")["roots"]["system"]["usage"]["total_tokens"][
                    "unknown_calls"
                ]
                == 1
            )
            with pytest.raises(GatewayError, match="duplicate_call_id"):
                gateway.complete("synthetic-caller-token", request)
            for failure in ("redirect", "slow"):
                outcome[0] = failure
                gateway.timeout = 0.15
                failed_call = call()
                with pytest.raises(GatewayError, match="provider_outcome_unknown"):
                    gateway.complete("synthetic-caller-token", failed_call)
                saved = writer.get_system_record(failed_call.call_id)
                assert saved is not None and saved.payload["status"] == "failed"
            assert len(requests) == 3  # attempts=3 must not repeat either failure.
            assert all(h["authorization"] == "Bearer " + SECRET for h, _ in requests)
            assert "SENSITIVE_PROMPT" not in str(record.payload) + caplog.text
            assert SECRET not in str(record.payload) + caplog.text
            assert not store._log
    finally:
        journal.close()


def test_cli_uses_builtin_factory_by_default(tmp_path, monkeypatch):
    import sys

    from ear_llm_gateway import cli, octopus

    adapter, requests, _ = replay()
    store = MemoryStorage()
    closed = []

    class Writer:
        def __getattr__(self, name):
            return getattr(store.client("llm_gateway"), name)

        def close(self):
            closed.append(True)

    def remote(token, url, **kwargs):
        assert url.rstrip("/") == "http://127.0.0.1:9999"
        return (
            Writer()
            if kwargs.get("module") == "llm_gateway"
            else nullcontext(store.client("extraction"))
        )

    def serve(app, **kwargs):
        from fastapi.testclient import TestClient

        assert kwargs["workers"] == 1 and kwargs["access_log"] is False
        with TestClient(app) as client:
            result = client.post(
                "/v1/complete",
                json=call().model_dump(mode="json"),
                headers={"Authorization": "Bearer " + "a" * 43},
            )
            assert result.status_code == 200, result.text
            assert result.json()["usage"] is None

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ear-llm-gateway",
            "--storage-url",
            "http://127.0.0.1:9999",
            "--journal",
            str(tmp_path / "default-cli.sqlite3"),
            "--aliases",
            json.dumps({"default": MODEL}),
        ],
    )
    monkeypatch.setenv("EAR_GATEWAY_STORAGE_TOKEN", "b" * 43)
    monkeypatch.setattr(octopus, "create_provider", lambda: adapter)
    monkeypatch.setattr(cli, "RemoteStorageClient", remote)
    monkeypatch.setattr(cli.uvicorn, "run", serve)
    cli.main()
    assert closed == [True] and len(requests) == 1


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["string", "null"]},
        {"required": [1]},
        {"properties": []},
        {"items": True},
        {"anyOf": []},
        {"minimum": True},
        {"maxLength": -1},
        {"$ref": "#/$defs/missing"},
        {"enum": []},
        {"additionalProperties": "yes"},
    ],
)
def test_invalid_dialect_rejected_without_http(schema):
    adapter, requests, _ = replay()
    with pytest.raises(ProviderFailure):
        adapter.complete(call(output_schema=schema), model=MODEL, timeout=1)
    assert not requests


def test_metadata_journal_hides_response_and_upstream_credentials(tmp_path):
    adapter, requests, _ = replay(raw=encode(envelope('{"answer":7,"secret":"SENSITIVE_REPLY"}')))
    store = MemoryStorage()
    journal = Journal(str(tmp_path / "metadata.sqlite3"))
    try:
        gateway = Gateway(
            callers=lambda _: nullcontext(store.client("extraction")),
            writer=store.client("llm_gateway"),
            provider=adapter,
            journal=journal,
            aliases={"default": MODEL},
        )
        request = call(output_schema={})
        assert gateway.complete("not-forwarded", request)["output"]["secret"] == "SENSITIVE_REPLY"
        row = journal.get(request.call_id, request.module)
        assert row is not None
        assert all(
            value not in row["record"] for value in (SECRET, "SENSITIVE_REPLY", "SENSITIVE_PROMPT")
        )
        assert len(requests) == 1
    finally:
        journal.close()
