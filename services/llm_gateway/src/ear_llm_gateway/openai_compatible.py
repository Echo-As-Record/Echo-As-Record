"""Direct New API chat adapter, tested only against synthetic responses."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, cast

import httpx
from ear_contracts import LLMUsage

from ear_llm_gateway.models import (
    MAX_PROVIDER_TIMEOUT_SECONDS,
    CallRequest,
    GatewayError,
    ProviderFailure,
    ProviderResult,
)
from ear_llm_gateway.output_json import OutputSchema, encode, strict_json

logger = logging.getLogger(__name__)

CHAT_URL = "https://newapi.hangjuan.cn/v1/chat/completions"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_COMPLETION_TOKENS = 4096
_WIRE_FAILURES = frozenset(
    {
        "direct_http_status",
        "direct_media_type",
        "direct_length",
        "direct_response_limit",
        "direct_envelope",
        "direct_choices",
        "direct_choice",
        "direct_message",
        "direct_output",
        "direct_usage",
        "direct_usage_details",
        "direct_credential_echo",
    }
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}")


def valid_model_id(value: object) -> bool:
    return isinstance(value, str) and "://" not in value and _MODEL.fullmatch(value) is not None


class OpenAICompatibleProvider:
    """One request; no caller-supplied endpoint, headers or wire options."""

    def __init__(self, credential: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(credential, str) or not re.fullmatch(
            r"[A-Za-z0-9._~+/-]{1,8192}={0,2}", credential
        ):
            raise ValueError("invalid_direct_configuration")
        self._credential = credential
        self._transport = transport  # Trusted injection for offline tests only.

    def __repr__(self) -> str:
        return "<OpenAICompatibleProvider configuration redacted>"

    def admit(self, call: CallRequest, *, model: str, timeout: float) -> None:
        # Gateway has already checked the schema, before reserving the call.
        try:
            self._prepare(call, model, timeout)
            return
        except Exception:
            pass
        raise GatewayError(422, "invalid_provider_request")

    @staticmethod
    def _prepare(call: CallRequest, model: str, timeout: float) -> tuple[OutputSchema, bytes]:
        if not valid_model_id(model) or not (
            type(timeout) in (float, int)
            and math.isfinite(timeout)
            and 0 < timeout <= MAX_PROVIDER_TIMEOUT_SECONDS
        ):
            raise ValueError("invalid_direct_request")
        schema = OutputSchema(call.output_schema)
        body = encode(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": call.system},
                    {"role": "system", "content": "Return a JSON object matching output_schema."},
                    {
                        "role": "user",
                        # Some New API channels convert system messages to Responses
                        # instructions. JSON mode also requires a directive in input.
                        "content": encode(
                            {
                                "instruction": "Return a JSON object matching output_schema.",
                                "data": call.data,
                                "output_schema": schema.root,
                            }
                        ).decode("utf-8"),
                    },
                ],
                "stream": False,
                "response_format": {"type": "json_object"},
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
            }
        )
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("direct_request_limit")
        return schema, body

    def complete(self, call: CallRequest, *, model: str, timeout: float) -> ProviderResult:
        phase = "prepare"
        try:
            schema, body = self._prepare(call, model, timeout)
            phase = "transport"
            # asyncio.run waits for DNS's default executor even after cancellation.
            # Close this private loop without joining it; late DNS cannot resume I/O.
            loop = asyncio.new_event_loop()
            try:
                raw = loop.run_until_complete(self._request(body, timeout))
            finally:
                loop.close()
            phase = "decode"
            value = strict_json(raw)
            phase = "chat_response"
            output, usage = _reply(value)
            phase = "extraction_schema"
            schema.validate(output)
            phase = "output_boundary"
            if self._credential.encode("ascii") in encode(output):
                raise ValueError("direct_credential_echo")
            return ProviderResult(output, usage)
        except Exception as exc:
            # Record only closed local stage names; never log raw upstream values.
            if phase == "transport" and isinstance(exc, TimeoutError):
                phase = "transport_deadline"
            elif phase == "transport" and isinstance(exc, httpx.ConnectTimeout):
                phase = "transport_connect_timeout"
            elif phase == "transport" and isinstance(exc, httpx.ReadTimeout):
                phase = "transport_read_timeout"
            elif type(exc) is ValueError and exc.args and isinstance(exc.args[0], str):
                code = exc.args[0]
                if code in _WIRE_FAILURES:
                    phase = code
            logger.warning("newapi_call_failed stage=%s", phase)
        # No remote status can attest non-execution. Do not retain exception context.
        raise ProviderFailure()

    async def _request(self, body: bytes, timeout: float) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with asyncio.timeout_at(deadline):
            async with (
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=timeout,
                    transport=self._transport
                    if self._transport is not None
                    else httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                    headers={
                        "Authorization": "Bearer " + self._credential,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as client,
                client.stream("POST", CHAT_URL, content=body) as response,
            ):
                if response.status_code != 200:
                    raise ValueError("direct_http_status")
                content_types = response.headers.get_list("content-type")
                if (
                    len(content_types) != 1
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ValueError("direct_media_type")
                lengths = response.headers.get_list("content-length")
                transfer = response.headers.get_list("transfer-encoding")
                if transfer and (lengths or [item.lower() for item in transfer] != ["chunked"]):
                    raise ValueError("direct_length")
                expected = MAX_RESPONSE_BYTES
                if lengths:
                    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                        raise ValueError("direct_length")
                    expected = int(lengths[0])
                    if expected > MAX_RESPONSE_BYTES:
                        raise ValueError("direct_response_limit")
                raw = bytearray()
                # HTTPX implements aiter_raw as an async generator, though its
                # annotation is AsyncIterator. Close it before the private loop.
                async with aclosing(
                    cast(AsyncGenerator[bytes, None], response.aiter_raw())
                ) as chunks:
                    async for chunk in chunks:
                        if len(raw) + len(chunk) > expected:
                            raise ValueError("direct_response_limit")
                        raw.extend(chunk)
                if lengths and len(raw) != expected:
                    raise ValueError("direct_length")
        if loop.time() >= deadline:
            raise TimeoutError
        return bytes(raw)


def _reply(reply: Any) -> tuple[dict[str, Any], LLMUsage | None]:
    if (
        not isinstance(reply, dict)
        or reply.keys()
        - {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
            "service_tier",
            "system_fingerprint",
        }
        or reply.get("object") != "chat.completion"
        # The upstream may echo a canonical model name. It is not used for routing.
        or not valid_model_id(reply.get("model"))
        or not isinstance(reply.get("id"), str)
        or not 1 <= len(reply["id"]) <= 256
        or not _count(reply.get("created"))
        or any(
            reply.get(key) is not None and not isinstance(reply[key], str)
            for key in ("system_fingerprint", "service_tier")
        )
    ):
        raise ValueError("direct_envelope")
    choices = reply.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("direct_choices")
    choice = choices[0]
    if (
        not isinstance(choice, dict)
        or choice.keys() - {"index", "finish_reason", "message", "logprobs"}
        or type(choice.get("index")) is not int
        or choice["index"] != 0
        or choice.get("finish_reason") != "stop"
        or choice.get("logprobs") is not None
    ):
        raise ValueError("direct_choice")
    message = choice.get("message")
    if (
        not isinstance(message, dict)
        or message.keys()
        - {
            "role",
            "content",
            "refusal",
            "tool_calls",
            "function_call",
            "audio",
            "annotations",
            "reasoning_content",
            "reasoning",
            "name",
            "tool_call_id",
        }
        or message.get("role") != "assistant"
        or not isinstance(message.get("content"), str)
        or any(
            message.get(key) is not None and not isinstance(message[key], str)
            for key in ("reasoning_content", "reasoning", "name")
        )
        or message.get("tool_call_id") not in (None, "")
        or any(message.get(key) is not None for key in ("refusal", "function_call", "audio"))
        or any(message.get(key) not in (None, []) for key in ("tool_calls", "annotations"))
    ):
        raise ValueError("direct_message")
    # New API can send optional reasoning text. Never return or retain it.
    output = strict_json(message["content"])
    if not isinstance(output, dict):
        raise ValueError("direct_output")
    return output, _usage(reply.get("usage"))


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _usage(raw: Any) -> LLMUsage | None:
    if raw is None:
        return None  # Missing usage is unknown; never estimate or assume zero.
    counts = {"prompt_tokens", "completion_tokens", "total_tokens"}
    details = {
        "prompt_tokens_details": {"cached_tokens", "audio_tokens", "text_tokens", "image_tokens"},
        "completion_tokens_details": {
            "reasoning_tokens",
            "text_tokens",
            "audio_tokens",
            "image_tokens",
            "accepted_prediction_tokens",
            "rejected_prediction_tokens",
        },
    }
    # New API may add provider-specific accounting (e.g. input_tokens,
    # output_tokens and cache creation counts). We only use the standard
    # Chat Completions totals; other fields are not forwarded or persisted.
    if (
        not isinstance(raw, dict)
        or any(not _count(raw.get(key)) for key in counts)
        or raw["total_tokens"] != raw["prompt_tokens"] + raw["completion_tokens"]
    ):
        raise ValueError("direct_usage")
    for key, fields in details.items():
        value = raw.get(key)
        if value is not None and (
            not isinstance(value, dict) or any(not _count(value[k]) for k in fields & value.keys())
        ):
            raise ValueError("direct_usage_details")
    return LLMUsage(
        input_tokens=raw["prompt_tokens"],
        output_tokens=raw["completion_tokens"],
        total_tokens=raw["total_tokens"],
    )
