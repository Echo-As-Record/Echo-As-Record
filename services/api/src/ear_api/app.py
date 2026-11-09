"""Standalone FastAPI surface. Storage owns authorization and every resource change."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any, Literal

from ear_contracts import SemanticRecord, SystemRecord, is_id
from ear_contracts.base import UtcDatetime
from ear_extraction.domain import Interval
from ear_storage.management_input import ManagementInput
from ear_storage_client import Query, TimeRange
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from ear_api.boundary import Boundary, failure, response
from ear_api.business import (
    BUSINESS_TYPES,
    feedback_status,
    management_result,
    preview_reextract,
    submit_feedback,
)
from ear_api.config import Binding, Settings
from ear_api.execution import (
    PROTOCOL_BLOCKER,
    ActionAdapter,
    ExecutionService,
    journal_status,
)
from ear_api.execution_journal import ExecutionJournal, JournalUnavailable
from ear_api.models import FeedbackInput, load_json_object
from ear_api.upstream import GatewayError, Upstream


def valid_id(value: str) -> str:
    if not is_id(value):
        raise GatewayError(422, "invalid_input")
    return value


def body(request: Request, *, empty: bool = False) -> dict[str, Any]:
    raw = request.scope["ear_api.body"]
    if empty and raw in (b"", b"{}"):
        return {}
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise GatewayError(415, "json_required")

    data = load_json_object(raw)
    if empty and data:
        raise ValueError("empty body required")
    return data


def params(request: Request, allowed: set[str]) -> dict[str, str]:
    items = list(request.query_params.multi_items())
    result = dict(items)
    if len(result) != len(items) or not set(result) <= allowed:
        raise GatewayError(422, "invalid_input")
    return result


def integer(value: str, minimum: int = 1, maximum: int = 100) -> int:
    if not re.fullmatch(r"[0-9]{1,10}", value) or not minimum <= int(value) <= maximum:
        raise GatewayError(422, "invalid_input")
    return int(value)


def pagination(values: dict[str, str]) -> tuple[int, str | None]:
    cursor = values.get("cursor")
    if cursor is not None and (not cursor or len(cursor) > 512):
        raise GatewayError(422, "invalid_input")
    return integer(values.get("limit", "50")), cursor


class RecoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["verify", "confirm_executed", "retry"]
    expected_revision: int = Field(strict=True, ge=1, le=2147483647)
    acknowledge_duplicate_risk: bool = Field(default=False, strict=True)


def create_app(
    settings: Settings,
    *,
    upstream: Upstream | None = None,
    execution: ExecutionService | None = None,
    adapter: ActionAdapter | None = None,
) -> FastAPI:
    remote = upstream or Upstream(settings)
    if execution is not None and (settings.execution_journal is not None or adapter is not None):
        raise ValueError("ambiguous_execution_configuration")
    if adapter is not None and settings.execution_journal is None:
        raise ValueError("execution_journal_required")
    journal = execution.journal if execution is not None else None
    trusted_adapter = execution.adapter if execution is not None else adapter

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal journal
        if settings.execution_journal is None:
            # Injected execution services are borrowed; their owner closes the journal.
            yield
        else:
            with ExecutionJournal(settings.execution_journal) as owned:
                journal = owned
                try:
                    yield
                finally:
                    journal = None

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.execution_http_enabled = False

    def current_journal() -> ExecutionJournal | None:
        if settings.execution_journal is not None and journal is None:
            raise JournalUnavailable()
        return journal

    app.add_middleware(Boundary, settings=settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return failure(422, "invalid_input")

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return failure(
            exc.status_code, "not_found" if exc.status_code == 404 else "request_rejected"
        )

    def authenticated(request: Request, *, admin: bool = False) -> Binding:
        binding = settings.authenticate(request.headers.getlist("authorization"))
        if binding is None:
            raise GatewayError(401, "unauthenticated")
        if admin and binding.role != "admin":
            raise GatewayError(403, "admin_required")
        return binding

    def business_type(value: str) -> str:
        if value not in BUSINESS_TYPES:
            raise GatewayError(404, "not_found")
        return value

    @app.get("/health/live")
    def live(request: Request):
        params(request, set())
        return response({"status": "alive"})

    @app.get("/health/ready")
    def ready(request: Request):
        params(request, set())
        healthy = remote.ready()
        return response(
            {"status": "ready" if healthy else "unavailable", "scope": "api_storage_connectivity"},
            200 if healthy else 503,
        )

    @app.get("/v1/me")
    def me(request: Request):
        params(request, set())
        binding = authenticated(request)
        with remote.client(binding):
            pass
        return response({"subject": binding.subject, "role": binding.role})

    def list_records(data_type: str, request: Request):
        binding = authenticated(request)
        business_type(data_type)
        values = params(request, {"limit", "cursor", "start", "end", "include_superseded"})
        limit, cursor = pagination(values)
        include = values.get("include_superseded", "false")
        if include not in {"true", "false"}:
            raise ValueError("boolean required")
        time = None
        if "start" in values or "end" in values:
            adapter = TypeAdapter(UtcDatetime)
            time = TimeRange(
                adapter.validate_python(values.get("start")),
                adapter.validate_python(values.get("end")),
            )
        with remote.client(binding) as client:
            page = client.query(
                Query(
                    data_type,
                    time=time,
                    limit=limit,
                    cursor=cursor,
                    include_superseded=include == "true",
                )
            )
        return response(
            {
                "items": [r.model_dump(mode="json") for r in page.items],
                "next_cursor": page.next_cursor,
                "consistency": "live",
            }
        )

    app.add_api_route("/v1/records/{data_type}", list_records, methods=["GET"])

    @app.get("/v1/timeline")
    def timeline(request: Request):
        return list_records("activity", request)

    @app.get("/v1/records/{data_type}/{record_id}")
    def get_record(data_type: str, record_id: str, request: Request):
        binding = authenticated(request)
        business_type(data_type)
        valid_id(record_id)
        values = params(request, {"version"})
        with remote.client(binding) as client:
            record = (
                client.get_version(record_id, integer(values["version"], maximum=2147483647))
                if "version" in values
                else client.get(record_id)
            )
        if record is None or record.data_type != data_type:
            raise GatewayError(404, "not_found")
        return response(record.model_dump(mode="json"))

    @app.get("/v1/records/{data_type}/{record_id}/history")
    def history(data_type: str, record_id: str, request: Request):
        binding = authenticated(request)
        business_type(data_type)
        valid_id(record_id)
        values = params(request, {"after_version", "limit"})
        limit = integer(values.get("limit", "50"))
        after = integer(values.get("after_version", "0"), minimum=0, maximum=2147483647)
        # Freeze current version and pages without reading full history.
        with remote.client(binding) as client, client.snapshot() as reader:
            current = reader.get(record_id)
            if current is None or current.data_type != data_type:
                raise GatewayError(404, "not_found")
            last = min(current.version, after + limit)
            items = []
            for version in range(after + 1, last + 1):
                item = reader.get_version(record_id, version)
                if item is None:
                    raise GatewayError(503, "history_incomplete")
                items.append(item.model_dump(mode="json"))
        return response(
            {"items": items, "next_after_version": last if last < current.version else None}
        )

    @app.post("/v1/feedback")
    def feedback(request: Request):
        binding = authenticated(request)
        params(request, set())
        parsed = FeedbackInput.model_validate(body(request))
        # Validate before upstream I/O; public callers cannot assert execution success.
        parsed.normalized_command()
        with remote.client(binding) as client:
            result = submit_feedback(client, binding, parsed)
        return response(result, 202)

    @app.get("/v1/feedback/{feedback_id}/status")
    def feedback_state(feedback_id: str, request: Request):
        binding = authenticated(request)
        params(request, set())
        valid_id(feedback_id)
        with remote.client(binding) as client:
            result = feedback_status(client, feedback_id)
        return response(result)

    @app.post("/v1/previews/reextract")
    def reextract(request: Request):
        binding = authenticated(request)
        params(request, set())
        interval = Interval.model_validate(body(request))
        with remote.client(binding) as client, client.snapshot() as reader:
            result = preview_reextract(reader, interval)
        return response(result)

    @app.get("/v1/previews/person/{person_id}")
    def person_preview(person_id: str, request: Request):
        binding = authenticated(request)
        params(request, set())
        valid_id(person_id)
        with remote.client(binding) as client, client.snapshot() as reader:
            person = reader.get(person_id)
            if person is None or person.data_type != "person":
                raise GatewayError(404, "not_found")
            count = reader.count(Query("attribution", where={"target_id": person_id}))
        return response(
            {"person_id": person_id, "attributed_records": count, "candidate_identity": None}
        )

    @app.post("/v1/management/requests")
    def manage(request: Request):
        binding = authenticated(request, admin=True)
        params(request, set())
        parsed = ManagementInput.model_validate(body(request))
        result = remote.management(binding, "/v1/management/requests", parsed.payload())
        return response(result, 202)

    @app.post("/v1/management/credentials/{feedback_id}/claim")
    def claim(feedback_id: str, request: Request):
        binding = authenticated(request, admin=True)
        params(request, set())
        valid_id(feedback_id)
        body(request, empty=True)
        result = remote.management(
            binding,
            "/v1/management/credentials/" + feedback_id + "/claim",
            {},
            claim=True,
        )
        return response(result)

    @app.get("/v1/management/results")
    def results(request: Request):
        binding = authenticated(request, admin=True)
        values = params(request, {"limit", "cursor", "module"})
        limit, cursor = pagination(values)
        module = values.get("module")
        if module is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,54}", module) is None:
            raise ValueError("invalid module")
        with remote.client(binding) as client:
            page = client.query(
                Query(
                    "plugin_management_result",
                    limit=limit,
                    cursor=cursor,
                    where={"module": module} if module else {},
                )
            )
        return response(
            {
                "items": [management_result(r) for r in page.items if isinstance(r, SystemRecord)],
                "next_cursor": page.next_cursor,
            }
        )

    @app.get("/v1/management/results/{result_id}")
    def result(result_id: str, request: Request):
        binding = authenticated(request, admin=True)
        params(request, set())
        valid_id(result_id)
        with remote.client(binding) as client:
            record = client.get_system_record(result_id)
        if record is None or record.data_type != "plugin_management_result":
            raise GatewayError(404, "not_found")
        return response(management_result(record))

    @app.get("/v1/management/history/jobs/{job_id}")
    def history_job(job_id: str, request: Request):
        binding = authenticated(request, admin=True)
        params(request, set())
        valid_id(job_id)
        with remote.client(binding) as client:
            result = client.history_job(job_id)
        return response(result)

    @app.get("/v1/actions/{suggestion_id}/execution")
    def execution_status(suggestion_id: str, request: Request):
        binding = authenticated(request)
        params(request, set())
        valid_id(suggestion_id)
        # Never disclose a local journal entry without CURRENT upstream authorization.
        with remote.client(binding) as client:
            record = client.get(suggestion_id)
        if not isinstance(record, SemanticRecord) or record.data_type != "action_suggestion":
            raise GatewayError(404, "not_found")
        active = current_journal()
        local = (
            journal_status(active, suggestion_id)
            if active is not None
            else {"executor_state": "disabled", "journal_revision": None}
        )
        return response(
            {
                "suggestion_id": suggestion_id,
                "suggestion_version": record.version,
                "suggestion_state": record.payload.get("state"),
                "suggestion_status": record.status,
                **local,
                "execution_enabled": False,
                "verification_enabled": active is not None and trusted_adapter is not None,
                "recovery_protocol": "pending_extraction_collaboration",
                "blocker": PROTOCOL_BLOCKER,
            }
        )

    @app.post("/v1/actions/{suggestion_id}/recovery")
    def execution_recovery(suggestion_id: str, request: Request):
        binding = authenticated(request)
        params(request, set())
        valid_id(suggestion_id)
        parsed = RecoveryInput.model_validate(body(request))
        if parsed.operation == "retry" and not parsed.acknowledge_duplicate_risk:
            raise GatewayError(409, "duplicate_risk_confirmation_required")
        with remote.client(binding) as client:
            record = client.get(suggestion_id)
        if not isinstance(record, SemanticRecord) or record.data_type != "action_suggestion":
            raise GatewayError(404, "not_found")
        if parsed.operation == "verify":
            active = current_journal()
            if active is not None and trusted_adapter is not None:
                # Use THIS request's reauthenticated storage client, not the injected
                # service's long-lived client. No HTTP operation calls execute().
                with remote.client(binding) as client:
                    service = ExecutionService(client, active, trusted_adapter)
                    service.verify(suggestion_id, parsed.expected_revision)
                    return response(
                        {"suggestion_id": suggestion_id, **service.status(suggestion_id)}
                    )
        # Even acknowledged risk is not permission to invent ④'s recovery protocol.
        # No HTTP self-report can assert success; verification only calls lookup().
        raise GatewayError(501, PROTOCOL_BLOCKER)

    @app.get("/v1/plugins")
    def plugins(request: Request):
        binding = authenticated(request, admin=True)
        values = params(request, {"limit", "after"})
        limit = integer(values.get("limit", "50"))
        after = values.get("after", "")
        if after and re.fullmatch(r"[a-z][a-z0-9_]{0,54}", after) is None:
            raise ValueError("invalid module")
        return response(remote.plugin_directory(binding, limit=limit, after=after))

    if settings.static_dir is not None:
        app.mount("/plugins", StaticFiles(directory=settings.static_dir, html=True), name="plugins")
    return app
