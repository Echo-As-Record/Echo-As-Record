"""HTTP wire-v1 boundary: closed commands, fresh authentication, sanitized failures.

No direct admin CRUD, DSN discovery, implicit migration, request-header/body logging or
cached principals. Sync endpoints run in FastAPI's worker pool, not the event loop.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import psycopg
from ear_contracts import is_id
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.consumer_wire import (
    ConsumerRequest,
    DeliveryCommand,
    RegisterConsumer,
    ReplayCommand,
)
from ear_storage_client.engine import canonical_json
from ear_storage_client.remote_errors import DeliveryExpired
from ear_storage_client.wire import (
    SnapshotRequest,
    check_keys,
    decode_read,
    decode_request,
    encode_read_result,
    encode_result,
    load_json,
)
from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from ear_storage.auth import AuthenticationError
from ear_storage.consumer_broker import ConsumerUnavailable
from ear_storage.consumers import ConsumerPolicy
from ear_storage.relay import RelayConfig
from ear_storage.schema import ProjectionLimit, SchemaMismatch
from ear_storage.service import StorageService
from ear_storage.snapshots import SnapshotUnavailable


def json_response(value: Any, status: int = 200) -> Response:
    return Response(
        canonical_json(value),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def failure(status: int, code: str, message: str) -> Response:
    response = json_response({"code": code, "message": message, "details": {}}, status)
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


class BoundaryMiddleware:
    """Bound body memory BEFORE JSON parsing, including absent/lying Content-Length.

    Reading and buffering are capped incrementally; slow uploads have a total deadline.
    Exceptions never reach Uvicorn's traceback logger (SQL/DSN could be in diagnostics).
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int, body_timeout: float) -> None:
        self.app, self.limit, self.timeout = app, max_body_bytes, body_timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            chunks = bytearray()
            async with asyncio.timeout(self.timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(chunks) + len(chunk) > self.limit:
                        raise ProjectionLimit
                    chunks.extend(chunk)
                    if not message.get("more_body", False):
                        break
            scope["ear.body"] = bytes(chunks)
            await self.app(scope, receive, safe_send)
            return
        except DeliveryExpired:
            error = failure(409, "delivery_expired", "delivery expired")
        except SnapshotUnavailable:
            error = failure(410, "snapshot_unavailable", "snapshot unavailable")
        except AuthenticationError:
            error = failure(401, "unauthenticated", "invalid credentials")
        except PermissionDenied:
            error = failure(403, "forbidden", "access denied")
        except ProjectionLimit:
            error = failure(413, "resource_limit", "resource limit exceeded")
        except (InvalidRequest, ValidationError):
            error = failure(422, "invalid_input", "invalid request")
        except TimeoutError:
            error = failure(408, "request_timeout", "request body timed out")
        except (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            SchemaMismatch,
            ConsumerUnavailable,
        ):
            error = failure(503, "unavailable", "storage unavailable")
        except Exception:
            error = failure(500, "internal_error", "storage request failed")
        if not started:
            await error(scope, receive, safe_send)


def create_app(
    service: StorageService, *, max_body_bytes: int = 8 * 1024 * 1024, body_timeout: float = 15.0
) -> FastAPI:
    if type(max_body_bytes) is not int or max_body_bytes < 1 or not 0 < body_timeout <= 120:
        raise ValueError("invalid HTTP resource limits")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BoundaryMiddleware, max_body_bytes=max_body_bytes, body_timeout=body_timeout)

    def authenticated(request: Request):
        headers = request.headers.getlist("authorization")
        if len(headers) != 1 or re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", headers[0]) is None:
            raise AuthenticationError("invalid credentials")
        token = headers[0][7:]
        client = service.client(token)
        # Pre-authentication also keeps revoked snapshot credentials at HTTP 401;
        # the service's generic 410 still hides missing/foreign/expired lease IDs.
        module = client.module
        return token, client, module

    @app.get("/v1/identity")
    def identity(request: Request):
        _token, _client, module = authenticated(request)
        return json_response({"codec": 1, "module": module})

    @app.get("/v1/registry")
    def registry(request: Request):
        _token, client, _module = authenticated(request)
        value = client.registry
        return json_response(
            {"codec": 1, "registry": value.to_json(), "core_types": sorted(value.core_types)}
        )

    @app.post("/v1/management/requests")
    def management_request(request: Request):
        from ear_storage.management import submit
        from ear_storage.management_input import ManagementInput

        _token, client, _module = authenticated(request)
        body = ManagementInput.model_validate(load_json(request.scope["ear.body"]))
        return json_response(submit(client, body), 202)

    @app.post("/v1/management/credentials/{feedback_id}/claim")
    def credential_claim(feedback_id: str, request: Request):
        from ear_storage.credentials import claim_credentials

        _token, client, _module = authenticated(request)
        if request.scope["ear.body"] not in (b"", b"{}"):
            raise InvalidRequest("claim takes no parameters")
        issued = claim_credentials(client, feedback_id)
        return json_response({"principal_id": issued.principal_id, "token": issued.token})

    @app.post("/v1/batches")
    def batches(request: Request):
        _token, client, _module = authenticated(request)
        data = load_json(request.scope["ear.body"])
        check_keys(data, {"codec", "request_id", "context", "ops"})
        request_id = data.get("request_id")
        if not is_id(request_id):
            raise InvalidRequest("invalid request id")
        data.pop("request_id")
        ops, context = decode_request(canonical_json(data))
        result = client.write(ops, context, request_id=request_id)
        return Response(encode_result(result), media_type="application/json")

    @app.post("/v1/read")
    def read(request: Request):
        token, client, _module = authenticated(request)
        parsed, args = decode_read(request.scope["ear.body"])
        reader = (
            client
            if parsed.snapshot_id is None
            else service.read_snapshot(token, parsed.snapshot_id)
        )
        result = getattr(reader, parsed.method.value)(**args)
        return Response(encode_read_result(parsed.method, result), media_type="application/json")

    @app.post("/v1/snapshots")
    def snapshot(request: Request):
        token, _client, _module = authenticated(request)
        data = load_json(request.scope["ear.body"])
        if not isinstance(data, dict) or type(data.get("codec")) is not int:
            raise InvalidRequest("invalid snapshot request")
        parsed = SnapshotRequest.model_validate_json(canonical_json(data))
        lease = service.create_snapshot(token, ttl_seconds=parsed.ttl_seconds)
        return json_response({"codec": 1, "lease": lease.to_json()})

    @app.delete("/v1/snapshots/{snapshot_id}")
    def release(snapshot_id: str, request: Request):
        token, _client, _module = authenticated(request)
        service.release_snapshot(token, snapshot_id)
        return json_response({"codec": 1, "released": True})

    @app.post("/v1/consumers/{operation}")
    def consumer_command(operation: str, request: Request):
        token, _client, _module = authenticated(request)
        models = {
            "register": RegisterConsumer,
            "fetch": ConsumerRequest,
            "ack": DeliveryCommand,
            "nack": DeliveryCommand,
            "deadletters": ConsumerRequest,
            "replay": ReplayCommand,
        }
        model = models.get(operation)
        if model is None:
            raise InvalidRequest("unknown consumer command")
        data = load_json(request.scope["ear.body"])
        if not isinstance(data, dict) or type(data.get("codec")) is not int:
            raise InvalidRequest("invalid consumer command")
        parsed = model.model_validate(data)
        consumers = getattr(service, "consumers", None)
        if consumers is None:
            raise ConsumerUnavailable("consumer broker not configured")
        if operation in {"ack", "nack"}:
            assert isinstance(parsed, DeliveryCommand)
            result = consumers.finish(token, parsed, nack=operation == "nack")
        else:
            result = getattr(consumers, operation)(token, parsed)
        return json_response(result)

    return app


def create_app_from_dsn(
    dsn: str,
    *,
    relay_config: RelayConfig | None = None,
    consumer_policy: ConsumerPolicy | None = None,
    **kwargs: Any,
) -> FastAPI:
    """Deployment-only composition; never reads .env or exposes configuration."""
    return create_app(
        StorageService(dsn, relay_config=relay_config, consumer_policy=consumer_policy), **kwargs
    )
