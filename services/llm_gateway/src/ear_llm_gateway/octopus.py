"""Octopus Manager chat adapter pinned to source, NOT production-wire verified.

Source b3c7cd5dd950a79e94533b114619082a542eac66 in /home/aytony/octopus:
  octopus/main.py; octopus/llm/{router,router_models,chat_service,llm}.py
  octopus/router/remote_access/{middleware,auth_helpers}.py
  octopus/llm/provider/{openai_provider,litellm_provider}.py

Manager synthesizes usage (zero prompt + estimated completion); discard it, never
report it as measured usage. Manager strips upstream finish/refusal information.
Our JSON/schema checks do not restore those signals. Manager provider SDK retries
and egress/proxy configuration are outside this adapter's control.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from types import MappingProxyType

import httpx

from ear_llm_gateway.client import endpoint
from ear_llm_gateway.models import CallRequest, GatewayError, ProviderFailure, ProviderResult
from ear_llm_gateway.output_json import OutputSchema, encode, strict_json

SOURCE_COMMIT = "b3c7cd5dd950a79e94533b114619082a542eac66"
CHAT_PATH = "/api/v1/llm/chat/v1/chat/completions"


class OctopusProvider:
    """Deployment-only immutable routing; no caller headers/URLs/keys forwarded.

    `models` maps gateway-resolved model IDs to Octopus's provider TYPE enum, not
    arbitrary llm_provider names (the wire protocol cannot select those by name).
    Bearer is the Manager owner access token, NOT an upstream model API key.
    HTTP is allowed only on loopback; remote deployments require HTTPS.
    Async transport injection is a trusted composition/test seam, not a wire field.
    """

    def __init__(
        self,
        origin: str,
        credential: str,
        *,
        models: dict[str, str],
        service: str,
        max_response_bytes: int = 1024 * 1024,
        max_request_bytes: int = 256 * 1024,
        max_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        try:
            url = endpoint(origin)
            if url.scheme != "https" and url.host not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError
            if not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,8192}={0,2}", credential):
                raise ValueError
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", service):
                raise ValueError
            if (
                not models
                or len(models) > 128
                or any(
                    not isinstance(key, str)
                    or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@+-]{0,255}", key)
                    or value not in {"litellm", "openai", "ollama"}
                    for key, value in models.items()
                )
            ):
                raise ValueError
            for value, maximum in (
                (max_response_bytes, 4 * 1024 * 1024),
                (max_request_bytes, 1024 * 1024),
                (max_tokens, 32768),
            ):
                if type(value) is not int or not 1 <= value <= maximum:
                    raise ValueError
        except Exception:
            raise ValueError("invalid_octopus_configuration") from None
        self._url = url.copy_with(path=CHAT_PATH)
        self._credential = credential
        self._models = MappingProxyType(dict(models))
        self._service = service
        self._max_response = max_response_bytes
        self._max_request = max_request_bytes
        self._max_tokens = max_tokens
        self._transport = transport

    def __repr__(self):
        return "<OctopusProvider deployment configuration redacted>"

    def admit(self, call: CallRequest, *, model: str, timeout: float) -> None:
        """Pure pre-reservation admission; never opens a transport or retries."""
        try:
            OutputSchema(call.output_schema)
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise GatewayError(422, "invalid_schema") from None
        try:
            self._prepare(call, model=model, timeout=timeout)
        except Exception:
            raise GatewayError(422, "invalid_provider_request") from None

    def _prepare(self, call: CallRequest, *, model: str, timeout: float):
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError
        provider = self._models[model]
        schema = OutputSchema(call.output_schema)
        messages = [
            {"role": "system", "content": call.system},
            {"role": "system", "content": "Return only a JSON object matching output_schema."},
            {
                "role": "user",
                "content": encode(
                    {
                        "data": call.data,
                        "output_schema": call.output_schema,
                    }
                ).decode(),
            },
        ]
        # Never merge caller data with wire kwargs: api_key/base_url/service,
        # extra_body/tools/provider and headers are not caller capabilities.
        body = encode(
            {
                "messages": messages,
                "model": model,
                "provider": provider,
                "service": self._service,
                "stream": False,
                "timeout_secs": timeout,
                "max_tokens": self._max_tokens,
            }
        )
        if len(body) > self._max_request:
            raise ValueError
        return schema, body

    def complete(self, call: CallRequest, *, model: str, timeout: float) -> ProviderResult:
        try:
            schema, body = self._prepare(call, model=model, timeout=timeout)
            raw = asyncio.run(self._request(body, timeout))
            reply = strict_json(raw)
            output = self._output(reply)
            schema.validate(output)
            return ProviderResult(output=output, usage=None)
        except Exception:
            # Direct adapter callers also receive conservative, non-retryable errors.
            # Gateway admission runs separately before any durable reservation.
            # Never trust a remote status/text to attest non-execution.
            raise ProviderFailure() from None

    async def _request(self, body: bytes, timeout: float) -> bytes:
        # Total I/O deadline, not merely an inactivity timeout (slow-drip safe).
        async with asyncio.timeout(timeout):
            async with (
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=timeout,
                    transport=self._transport
                    or httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                    headers={
                        "Authorization": "Bearer " + self._credential,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as client,
                client.stream("POST", self._url, content=body) as response,
            ):
                if response.status_code != 200:
                    raise ValueError("octopus_http_failure")
                if (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    != "application/json"
                ):
                    raise ValueError("octopus_media_type")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("octopus_encoding")
                lengths = response.headers.get_list("content-length")
                expected = None
                if lengths:
                    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                        raise ValueError("octopus_length")
                    expected = int(lengths[0])
                    if expected > self._max_response:
                        raise ValueError("octopus_response_limit")
                chunks, size = [], 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > self._max_response:
                        raise ValueError("octopus_response_limit")
                    chunks.append(chunk)
                if expected is not None and size != expected:
                    raise ValueError("octopus_length")
                return b"".join(chunks)

    @staticmethod
    def _output(reply):
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
        ):
            raise ValueError("octopus_envelope")
        if not isinstance(reply.get("id"), str) or not reply["id"].startswith("chatcmpl-"):
            raise ValueError("octopus_id")
        if type(reply.get("created")) is not int or reply["created"] < 0:
            raise ValueError("octopus_created")
        if not isinstance(reply.get("model"), str) or not 1 <= len(reply["model"]) <= 256:
            raise ValueError("octopus_model")
        choices = reply.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("octopus_choices")
        choice = choices[0]
        if (
            not isinstance(choice, dict)
            or type(choice.get("index")) is not int
            or choice["index"] != 0
            or choice.get("finish_reason") != "stop"
        ):
            raise ValueError("octopus_finish")
        message = choice.get("message")
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError("octopus_message")
        if message.keys() - {
            "role",
            "content",
            "refusal",
            "tool_calls",
            "function_call",
            "annotations",
            "audio",
        } or any(
            message.get(key) is not None
            for key in ("refusal", "tool_calls", "function_call", "audio")
        ):
            raise ValueError("octopus_nontext")
        usage = reply.get("usage")
        if not isinstance(usage, dict) or any(
            type(usage.get(key)) is not int or usage[key] < 0
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        ):
            raise ValueError("octopus_usage")
        if usage["prompt_tokens"] != 0 or usage["total_tokens"] != usage["completion_tokens"]:
            raise ValueError("octopus_usage_protocol_drift")
        output = strict_json(message["content"])
        if not isinstance(output, dict):
            raise ValueError("octopus_output_not_object")
        return output


def create_provider() -> OctopusProvider:
    """Read only deployment-owned values; never discover production configuration.

    EAR_OCTOPUS_ORIGIN: HTTPS origin (HTTP loopback is for local replay/tunnel).
    EAR_OCTOPUS_TOKEN: Manager owner bearer access token.
    EAR_OCTOPUS_MODELS: JSON map of approved model ID -> provider type.
    EAR_OCTOPUS_SERVICE: fixed owner/service locator for attribution, not auth.
    """
    try:
        models = strict_json(os.environ["EAR_OCTOPUS_MODELS"])
        if not isinstance(models, dict):
            raise ValueError
        return OctopusProvider(
            os.environ["EAR_OCTOPUS_ORIGIN"],
            os.environ["EAR_OCTOPUS_TOKEN"],
            models=models,
            service=os.environ["EAR_OCTOPUS_SERVICE"],
        )
    except Exception:
        raise ValueError("invalid_octopus_configuration") from None
