"""ASGI size/deadline limits and secret-free errors, including framework errors."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.errors import ConflictError
from ear_storage_client.remote_errors import (
    AuthenticationError,
    CommitUnknown,
    ProtocolError,
    ResourceLimit,
    ServiceUnavailable,
    SnapshotUnavailable,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from ear_api.config import Settings
from ear_api.execution_journal import JournalUnavailable
from ear_api.models import load_json_object
from ear_api.upstream import GatewayError

HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'",
}


def response(value: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers=HEADERS)


def failure(status: int, code: str) -> JSONResponse:
    result = response({"code": code}, status)
    if status == 401:
        result.headers["WWW-Authenticate"] = "Bearer"
    return result


class Boundary:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                protected = {k.lower().encode() for k in HEADERS}
                message["headers"] = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in protected and not k.lower().startswith(b"access-control-")
                ] + [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()]
            await send(message)

        try:
            if not await self._prepare(scope, receive):
                return
            await self.app(scope, receive, safe_send)
            return
        except GatewayError as exc:
            error = failure(exc.status, exc.code)
        except JournalUnavailable:
            error = failure(503, "execution_journal_unavailable")
        except AuthenticationError:
            error = failure(401, "unauthenticated")
        except SnapshotUnavailable:
            error = failure(410, "snapshot_unavailable")
        except PermissionDenied:
            error = failure(403, "forbidden")
        except CommitUnknown:
            error = failure(503, "acceptance_unknown_replay_identical_request")
        except ConflictError:
            error = failure(409, "feedback_conflict")
        except ResourceLimit:
            error = failure(413, "resource_limit")
        except (InvalidRequest, ValidationError, ValueError, json.JSONDecodeError):
            error = failure(422, "invalid_input")
        except TimeoutError:
            error = failure(408, "request_timeout")
        except (ServiceUnavailable, ProtocolError):
            error = failure(503, "storage_unavailable")
        except Exception:
            # Do not let exceptions containing bodies/credentials reach access/error loggers.
            error = failure(500, "internal_error")
        if not started:
            await error(scope, receive, safe_send)

    async def _prepare(self, scope: Scope, receive: Receive) -> bool:
        """Bound/validate ingress independently of response and exception handling."""
        headers = scope.get("headers", [])
        if len(headers) > 64 or sum(len(k) + len(v) for k, v in headers) > 16384:
            raise GatewayError(431, "headers_too_large")
        if len(scope.get("query_string", b"")) > 4096 or len(scope["path"]) > 2048:
            raise GatewayError(414, "request_target_too_large")
        for name in (b"content-type", b"content-length", b"content-encoding", b"host"):
            if sum(k.lower() == name for k, _v in headers) > 1:
                raise GatewayError(422, "invalid_input")
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        if lengths and (not lengths[0].isdigit() or len(lengths[0]) > 10):
            raise GatewayError(422, "invalid_input")
        if lengths and int(lengths[0]) > self.settings.max_body_bytes:
            raise GatewayError(413, "body_too_large")
        if lengths and any(k.lower() == b"transfer-encoding" for k, _v in headers):
            raise GatewayError(422, "invalid_input")
        origins = [v.decode("latin1") for k, v in headers if k.lower() == b"origin"]
        if origins and (
            len(origins) != 1
            or self.settings.public_origin is None
            or origins[0] != self.settings.public_origin.rstrip("/")
        ):
            raise GatewayError(403, "cross_origin_denied")
        if any(k.lower() == b"content-encoding" and v != b"identity" for k, v in headers):
            raise GatewayError(415, "encoding_not_supported")
        chunks = bytearray()
        async with asyncio.timeout(self.settings.body_timeout):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return False
                if message["type"] != "http.request":
                    raise ValueError("invalid request message")
                chunk = message.get("body", b"")
                if len(chunks) + len(chunk) > self.settings.max_body_bytes:
                    raise GatewayError(413, "body_too_large")
                chunks.extend(chunk)
                if not message.get("more_body", False):
                    break
        if lengths and int(lengths[0]) != len(chunks):
            raise GatewayError(422, "invalid_input")
        content_types = [v for k, v in headers if k.lower() == b"content-type"]
        if chunks and content_types and content_types[0].split(b";", 1)[0] == b"application/json":
            # Preflight catches decoder recursion and numeric overflow, before app.py
            # performs its existing parsing. It also protects preview/admin bodies.
            load_json_object(chunks, max_bytes=self.settings.max_body_bytes, authority=False)
        scope["ear_api.body"] = bytes(chunks)
        return True
