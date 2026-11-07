"""④-compatible complete(call)->dict port, with no vendor fallback or automatic retries.

An old ④ Call has no call_id: each complete invocation creates a NEW logical call.
For recoverable workflows supply a CallRequest with a persisted call_id. A transport
error may mean executed; reconcile that ID, NEVER automatically invoke another ID.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
from ear_contracts import new_id
from pydantic import BaseModel

from ear_llm_gateway.models import CallRequest, GatewayError


def endpoint(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError
        return url
    except (ValueError, httpx.InvalidURL):
        raise ValueError("invalid_gateway_endpoint") from None


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        model: str = "default",
        system: str = "",
        timeout: float = 90,
        max_response_bytes: int = 2 * 1024 * 1024,
        transport: httpx.BaseTransport | None = None,
    ):
        import re

        if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            raise ValueError("invalid_credentials")
        if not math.isfinite(timeout) or not 0 < timeout <= 120 or max_response_bytes < 1:
            raise ValueError("invalid_client_limits")
        self.model, self.system, self.max_response_bytes = model, system, max_response_bytes
        self._http = httpx.Client(
            base_url=endpoint(base_url),
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=transport or httpx.HTTPTransport(retries=0),
            headers={"Authorization": "Bearer " + token},
        )

    def __repr__(self):
        return "<GatewayClient credentials redacted>"

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _request(self, method: str, path: str, body=None):
        try:
            with self._http.stream(method, path, json=body) as response:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise GatewayError(502, "gateway_response_limit")
                    chunks.append(chunk)
                import json

                data = json.loads(b"".join(chunks))
                if response.status_code != 200:
                    # Do not reflect remote error strings/HTML or arbitrary code values.
                    raise GatewayError(response.status_code, "gateway_request_failed")
                if not isinstance(data, dict):
                    raise ValueError
                return data
        except GatewayError:
            raise
        except Exception:
            raise GatewayError(503, "gateway_outcome_unknown") from None

    def complete(self, call: BaseModel | dict[str, Any]) -> dict[str, Any]:
        values = call.model_dump(mode="json") if isinstance(call, BaseModel) else dict(call)
        values.setdefault("call_id", new_id())
        values.setdefault("model", self.model)
        values.setdefault("system", self.system)
        checked = CallRequest.model_validate(values)
        data = self._request("POST", "/v1/complete", checked.model_dump(mode="json"))
        if not data.get("record_saved") or not isinstance(data.get("output"), dict):
            raise GatewayError(502, "invalid_gateway_response")
        return data["output"]

    def reconcile(self, call_id: str):
        from ear_contracts import is_id

        if not is_id(call_id):
            raise ValueError("invalid_call_id")
        return self._request("POST", f"/v1/calls/{call_id}/reconcile")

    def stats(self):
        return self._request("GET", "/v1/stats")
