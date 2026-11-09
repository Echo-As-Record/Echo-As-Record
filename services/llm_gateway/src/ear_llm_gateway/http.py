"""Fail-closed FastAPI boundary. No request/error body or credentials in diagnostics."""

from __future__ import annotations

import asyncio
import re

from ear_contracts.base import Id, RootFeedbackId
from ear_storage_client import PermissionDenied
from ear_storage_client.remote_errors import AuthenticationError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ear_llm_gateway.models import CallRequest, GatewayError
from ear_llm_gateway.service import Gateway


def failure(status: int, code: str):
    return JSONResponse({"code": code}, status_code=status, headers={"Cache-Control": "no-store"})


class BodyLimit:
    def __init__(self, app: ASGIApp, max_bytes: int, timeout: float):
        self.app, self.max_bytes, self.timeout = app, max_bytes, timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
        try:
            if len(lengths) > 1 or (lengths and (int(lengths[0]) < 0)):
                raise ValueError
            if lengths and int(lengths[0]) > self.max_bytes:
                return await failure(413, "body_limit")(scope, receive, send)
        except ValueError:
            return await failure(400, "invalid_request")(scope, receive, send)
        if any(
            k.lower() == b"content-encoding" and v.lower() != b"identity"
            for k, v in scope["headers"]
        ):
            return await failure(415, "unsupported_encoding")(scope, receive, send)
        chunks, size = [], 0
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            while True:
                message = await asyncio.wait_for(
                    receive(),
                    max(
                        0.001,
                        deadline - asyncio.get_running_loop().time(),
                    ),
                )
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_bytes:
                    return await failure(413, "body_limit")(scope, receive, send)
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
        except TimeoutError:
            return await failure(408, "body_timeout")(scope, receive, send)
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


def create_app(gateway: Gateway, *, max_body_bytes: int = 256 * 1024, body_timeout: float = 5):
    if max_body_bytes < 1 or not 0 < body_timeout <= 30:
        raise ValueError("invalid_body_limits")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, debug=False)
    app.add_middleware(BodyLimit, max_bytes=max_body_bytes, timeout=body_timeout)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        # Pydantic errors include input values (including prompts and credentials).
        return failure(422, "invalid_request")

    @app.exception_handler(GatewayError)
    async def gateway_error(_request, error):
        return failure(error.status, error.code)

    def token(request: Request):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1 or re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", headers[0]) is None:
            raise GatewayError(401, "unauthenticated")
        return headers[0][7:]

    def run(operation, *args):
        try:
            return JSONResponse(operation(*args), headers={"Cache-Control": "no-store"})
        except GatewayError:
            raise
        except AuthenticationError:
            raise GatewayError(401, "unauthenticated") from None
        except PermissionDenied:
            raise GatewayError(403, "access_denied") from None
        except Exception:
            # Do not attach exceptions: adapter/storage errors may contain secret bodies.
            raise GatewayError(503, "gateway_unavailable") from None

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.post("/v1/complete")
    def complete(call: CallRequest, request: Request):
        return run(gateway.complete, token(request), call)

    @app.post("/v1/calls/{call_id}/reconcile")
    def reconcile(call_id: Id, request: Request):
        return run(gateway.reconcile, token(request), call_id)

    @app.get("/v1/stats")
    def stats(request: Request, root_feedback_id: RootFeedbackId | None = None):
        return run(gateway.stats, token(request), root_feedback_id)

    return app
