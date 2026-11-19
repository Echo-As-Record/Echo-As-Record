"""All upstream traffic uses HTTPX MockTransport; no production wire assertions."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext

import httpx
import pytest
from ear_contracts import LLMUsage, new_id
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.models import CallRequest, GatewayError, ProviderFailure
from ear_llm_gateway.openai_compatible import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    OpenAICompatibleProvider,
)
from ear_llm_gateway.output_json import encode
from ear_llm_gateway.service import Gateway
from ear_storage_client import MemoryStorage

MODEL = "gpt-6-luna"
TOKEN = "synthetic-direct-token"
PRIVATE = "synthetic-private-message"
URL = "https://newapi.hangjuan.cn/v1/chat/completions"


def call(**changes):
    return CallRequest.model_validate(
        {
            "call_id": new_id(),
            "module": "extraction",
            "purpose": "synthetic",
            "root_feedback_id": "system",
            "depth": 0,
            "system": PRIVATE,
            "data": {},
            "output_schema": {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            **changes,
        }
    )


def reply(content='{"answer":42}'):
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
                "message": {"role": "assistant", "content": content, "refusal": None},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


class Body(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, delay: float = 0, fail: bool = False):
        self.chunks, self.delay, self.fail = chunks, delay, fail
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.read += 1
            yield chunk
        if self.fail:
            raise httpx.ReadError(PRIVATE + TOKEN)

    async def aclose(self):
        self.closed = True


def offline(*, raw=None, headers=None, status=200, stream=None):
    requests = []
    stream = stream or Body(raw if raw is not None else encode(reply()))

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers=headers if headers is not None else {"content-type": "application/json"},
            stream=stream,
        )

    return OpenAICompatibleProvider(TOKEN, transport=httpx.MockTransport(handler)), requests, stream


def test_documented_newapi_extras_are_not_retained():
    envelope = reply()
    envelope["choices"][0]["message"]["reasoning_content"] = PRIVATE
    envelope["usage"]["prompt_tokens_details"] = {"text_tokens": 5, "image_tokens": 0}
    envelope["usage"]["completion_tokens_details"] = {"text_tokens": 3, "image_tokens": 0}
    envelope["usage"].update(input_tokens=0, output_tokens=0, input_tokens_details=None)
    envelope["usage"]["claude_cache_creation_5_m_tokens"] = 0
    envelope["usage"]["claude_cache_creation_1_h_tokens"] = 0
    envelope["usage"]["prompt_tokens_details"]["cached_creation_tokens"] = 0
    provider, _, _ = offline(raw=encode(envelope))
    result = provider.complete(call(), model=MODEL, timeout=1)
    assert result.output == {"answer": 42}
    assert result.usage == LLMUsage(input_tokens=5, output_tokens=3, total_tokens=8)
    assert PRIVATE not in repr(result)


def test_fixed_request_usage_redaction_and_transport_options(monkeypatch, caplog):
    monkeypatch.setenv("HTTPS_PROXY", "https://synthetic-proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/synthetic-invalid-certificate-path")
    actual_client = httpx.AsyncClient

    def client(**kwargs):
        for key in ("trust_env", "follow_redirects"):
            assert kwargs[key] is False
        assert kwargs["timeout"] == 1
        return actual_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    envelope = reply()
    envelope["model"] = "canonical/gpt-6-luna-snapshot"
    envelope["usage"]["completion_tokens_details"] = {"reasoning_tokens": 1}
    provider, requests, stream = offline(raw=encode(envelope))
    data = {"base_url": "https://alternate.invalid", "api_key": "untrusted", "stream": True}
    request = call(data=data)
    provider.admit(request, model=MODEL, timeout=1)
    assert not requests
    result = provider.complete(request, model=MODEL, timeout=1)
    assert result.output == {"answer": 42}
    assert result.usage == LLMUsage(input_tokens=5, output_tokens=3, total_tokens=8)
    assert len(requests) == 1 and stream.closed
    wire = requests[0]
    assert str(wire.url) == URL and wire.method == "POST"
    assert wire.headers["authorization"] == f"Bearer {TOKEN}"
    assert wire.headers["accept-encoding"] == "identity"
    assert json.loads(wire.content) == {
        "model": MODEL,
        "stream": False,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 4096,
        "messages": [
            {"role": "system", "content": PRIVATE},
            {"role": "system", "content": "Return a JSON object matching output_schema."},
            {
                "role": "user",
                "content": encode(
                    {
                        "instruction": "Return a JSON object matching output_schema.",
                        "data": data,
                        "output_schema": request.output_schema,
                    }
                ).decode(),
            },
        ],
    }
    assert TOKEN not in wire.content.decode() + repr(provider) + caplog.text
    assert PRIVATE not in caplog.text


def test_default_transport_has_no_retries_or_env(monkeypatch):
    def transport(**kwargs):
        assert kwargs == {"retries": 0, "trust_env": False}
        return httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=Body(encode(reply()))
            )
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    assert OpenAICompatibleProvider(TOKEN).complete(call(), model=MODEL, timeout=1).output == {
        "answer": 42
    }


@pytest.mark.parametrize("status", [201, 301, 302, 303, 307, 308, 401, 429, 500, 503])
def test_non_200_neither_reads_body_nor_redirects_nor_retries(status, caplog):
    target = "https://alternative.invalid/private"
    provider, requests, stream = offline(
        status=status,
        headers={"location": target},
        raw=(PRIVATE + TOKEN).encode(),
    )
    with pytest.raises(ProviderFailure, match=r"^provider_outcome_unknown$") as err:
        provider.complete(call(), model=MODEL, timeout=1)
    assert not err.value.safe_to_retry and err.value.__context__ is None
    assert len(requests) == 1 and str(requests[0].url) == URL
    assert stream.read == 0 and stream.closed
    assert all(s not in caplog.text for s in (PRIVATE, TOKEN, target))


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "null",
        '```json\n{"answer":42}\n```',
        '{"answer":true}',
        '{"answer":"42"}',
        '{"answer":42,"extra":1}',
        "{}",
        '{"answer":1e999}',
        '{"answer":4,"answer":2}',
    ],
)
def test_invalid_content_is_unknown(content):
    provider, requests, _ = offline(raw=encode(reply(content)))
    with pytest.raises(ProviderFailure, match=r"^provider_outcome_unknown$"):
        provider.complete(call(), model=MODEL, timeout=1)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"{} trailing",
        b'{"key":1,"key":2}',
        b'{"key":NaN}',
        b'"\\ud800"',
        b"\xff",
        b"[" * 49 + b"]" * 49,
    ],
)
def test_invalid_envelope_json_is_unknown(raw):
    provider, requests, _ = offline(raw=raw)
    with pytest.raises(ProviderFailure, match=r"^provider_outcome_unknown$"):
        provider.complete(call(), model=MODEL, timeout=1)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("error",), {"message": PRIVATE}),
        (("model",), "not a model identifier"),
        (("created",), True),
        (("object",), "chat.completion.chunk"),
        (("choices",), []),
        (("choices",), [{}, {}]),
        (("choices", 0, "index"), False),
        (("choices", 0, "finish_reason"), "length"),
        (("choices", 0, "finish_reason"), "content_filter"),
        (("choices", 0, "finish_reason"), "tool_calls"),
        (("choices", 0, "message", "role"), "user"),
        (("choices", 0, "message", "refusal"), PRIVATE),
        (("choices", 0, "message", "tool_calls"), [{}]),
        (("choices", 0, "message", "function_call"), {}),
        (("choices", 0, "message", "reasoning_content"), {"invalid": PRIVATE}),
        (("usage",), {}),
        (("usage",), PRIVATE),
        (("usage", "prompt_tokens"), True),
        (("usage", "completion_tokens"), -1),
        (("usage", "total_tokens"), 9),
        (("usage", "total_tokens"), 8.0),
        (("usage", "completion_tokens_details"), {"reasoning_tokens": PRIVATE}),
    ],
)
def test_invalid_envelope_fields_are_unknown(path, value):
    body = reply()
    nested = body
    for key in path[:-1]:
        nested = nested[key]
    nested[path[-1]] = value
    provider, _, _ = offline(raw=encode(body))
    with pytest.raises(ProviderFailure, match=r"^provider_outcome_unknown$") as err:
        provider.complete(call(), model=MODEL, timeout=1)
    assert not err.value.safe_to_retry


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_null_usage_is_unknown(missing):
    envelope = reply()
    envelope["usage"] = None
    if missing:
        del envelope["usage"]
    provider, _, _ = offline(raw=encode(envelope))
    assert provider.complete(call(), model=MODEL, timeout=1).usage is None


def test_output_that_echoes_key_is_not_released():
    provider, _, _ = offline(raw=encode(reply('{"answer":"' + TOKEN + '"}')))
    with pytest.raises(ProviderFailure, match=r"^provider_outcome_unknown$"):
        provider.complete(call(output_schema={}), model=MODEL, timeout=1)


@pytest.mark.parametrize(
    "headers",
    [
        [("content-type", "text/event-stream")],
        [("content-type", "application/json"), ("content-type", "text/plain")],
        [("content-encoding", "gzip")],
        [("content-length", str(MAX_RESPONSE_BYTES + 1))],
        [("content-length", "1"), ("content-length", "1")],
        [("content-length", "1"), ("transfer-encoding", "chunked")],
        [("content-length", "-1")],
    ],
)
def test_bad_headers_fail_before_read(headers):
    if not any(key == "content-type" for key, _ in headers):
        headers = [("content-type", "application/json"), *headers]
    provider, _, stream = offline(headers=headers)
    with pytest.raises(ProviderFailure):
        provider.complete(call(), model=MODEL, timeout=1)
    assert stream.read == 0 and stream.closed


@pytest.mark.parametrize("length_delta", [-1, 1])
def test_declared_size_must_equal_actual(length_delta):
    raw = encode(reply())
    provider, _, stream = offline(
        raw=raw,
        headers={
            "content-type": "application/json",
            "content-length": str(len(raw) + length_delta),
        },
    )
    with pytest.raises(ProviderFailure):
        provider.complete(call(), model=MODEL, timeout=1)
    assert stream.closed


def test_undeclared_stream_stops_at_byte_limit():
    stream = Body(b" " * MAX_RESPONSE_BYTES, b"!", b"never read")
    provider, requests, _ = offline(stream=stream)
    with pytest.raises(ProviderFailure):
        provider.complete(call(), model=MODEL, timeout=1)
    assert len(requests) == 1 and stream.closed and stream.read == 2


@pytest.mark.parametrize(
    ("data", "model", "timeout"),
    [
        ({"text": "x" * MAX_REQUEST_BYTES}, MODEL, 1),
        ({"v": float("nan")}, MODEL, 1),
        ({}, "https://alternate.invalid/?caller_override", 1),
        ({}, MODEL, 0),
        ({}, MODEL, float("inf")),
        ({}, MODEL, 301),
    ],
)
def test_admission_limits_before_any_request(data, model, timeout):
    provider, requests, _ = offline()
    with pytest.raises(GatewayError, match=r"^invalid_provider_request$") as err:
        provider.admit(call(data=data), model=model, timeout=timeout)
    assert err.value.status == 422 and not requests


def test_admission_rejects_unsupported_schema_without_io():
    provider, requests, _ = offline()
    with pytest.raises(GatewayError, match=r"^invalid_provider_request$"):
        provider.admit(
            call(output_schema={"$ref": "https://external.invalid"}), model=MODEL, timeout=1
        )
    assert not requests


@pytest.mark.parametrize(
    ("error", "stage"),
    [
        (TimeoutError, "deadline"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.ConnectTimeout, "connect_timeout"),
    ],
)
def test_timeout_category_no_sensitive_log_or_retry(error, stage, caplog):
    sent = []

    async def handler(request):
        sent.append(request)
        raise error(PRIVATE + TOKEN)

    provider = OpenAICompatibleProvider(TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderFailure, match="provider_outcome_unknown") as err:
        provider.complete(call(), model=MODEL, timeout=1)
    assert len(sent) == 1 and not err.value.safe_to_retry
    assert "stage=transport_" + stage in caplog.text
    assert PRIVATE not in caplog.text and TOKEN not in caplog.text


def test_deadline_covers_headers_and_slow_drip_body():
    stream = Body(*(b" " for _ in range(20)), delay=0.02)
    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(0.04)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    provider = OpenAICompatibleProvider(TOKEN, transport=httpx.MockTransport(handler))
    started = time.monotonic()
    with pytest.raises(ProviderFailure, match=r"^provider_outcome_unknown$"):
        provider.complete(call(), model=MODEL, timeout=0.15)
    assert time.monotonic() - started < 0.40
    assert stream.closed and 1 <= stream.read < len(stream.chunks) and len(requests) == 1


def test_deadline_does_not_join_stalled_resolver():
    import threading

    started, stop = threading.Event(), threading.Event()
    requests = []

    def resolve():
        started.set()
        stop.wait(2)

    async def handler(request):
        requests.append(request)
        # Simulate DNS via the same default executor used by the asyncio resolver.
        await asyncio.get_running_loop().run_in_executor(None, resolve)
        pytest.fail("must not get past resolution")

    provider = OpenAICompatibleProvider(TOKEN, transport=httpx.MockTransport(handler))
    began = time.monotonic()
    try:
        with pytest.raises(ProviderFailure, match=r"^provider_outcome_unknown$"):
            provider.complete(call(), model=MODEL, timeout=0.10)
        assert time.monotonic() - began < 1
        assert started.is_set() and len(requests) == 1
    finally:
        stop.set()


def test_gateway_does_not_retry_or_persist_response_on_disconnect(tmp_path, caplog):
    provider, requests, stream = offline(stream=Body((TOKEN + PRIVATE).encode(), fail=True))
    storage = MemoryStorage()
    writer = storage.client("llm_gateway")
    request = call()
    journal = Journal(str(tmp_path / "gateway.sqlite3"))
    try:
        gateway = Gateway(
            callers=lambda _: nullcontext(storage.client("extraction")),
            writer=writer,
            provider=provider,
            journal=journal,
            aliases={"default": MODEL},
            attempts=3,
        )
        with pytest.raises(GatewayError, match=r"^provider_outcome_unknown$") as err:
            gateway.complete("synthetic-caller", request)
        assert err.value.status == 502 and len(requests) == 1 and stream.closed
        record = writer.get_system_record(request.call_id)
        assert record is not None
        assert record.payload["status"] == "failed" and record.payload["usage"] is None
        assert PRIVATE not in repr(record) + caplog.text
        assert TOKEN not in repr(record) + caplog.text
        with pytest.raises(GatewayError, match=r"^duplicate_call_id$"):
            gateway.complete("synthetic-caller", request)
        assert len(requests) == 1
    finally:
        journal.close()
