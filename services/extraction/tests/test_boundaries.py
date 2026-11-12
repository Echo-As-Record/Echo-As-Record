"""Ingress, timer and gateway boundary tests (no remote model calls)."""

import json
from datetime import timedelta
from threading import Event

import httpx
import pytest
from ear_contracts import is_id, new_id
from ear_extraction.demo import DemoGateway, fixture, submit
from ear_extraction.domain import Call, Command, Interval, Rule
from ear_extraction.model_io import SYSTEM, ModelGateway
from ear_extraction.orchestration import ExtractionService, schedule_id
from ear_extraction.worker import Schedule, serve, tick
from ear_llm_gateway.client import GatewayClient
from ear_llm_gateway.models import CallRequest, GatewayError
from ear_storage_client import AppendRaw, Context, Query, iter_query
from ear_storage_client.testing import make_raw, ts
from pydantic import ValidationError


def test_malformed_feedback_is_recorded_not_retried():
    storage, _ = fixture()
    service = ExtractionService(storage.client("extraction"), DemoGateway())
    service.subscribe()
    raw = make_raw(
        "feedback", {"module": "extraction", "command": []}, start="2026-11-05T00:00:00Z"
    )
    storage.client("api").write([AppendRaw(raw)], Context.feedback(raw.id)).check()
    storage.run_until_idle()
    assert not storage.dead_letters
    assert service.runs()[0].payload["error"] == "invalid_command"
    assert service.runs()[0].payload["status"] == "failed"


def test_other_modules_feedback_is_ignored():
    storage, _ = fixture()
    service = ExtractionService(storage.client("extraction"), DemoGateway())
    service.subscribe()
    raw = make_raw(
        "feedback", {"module": "attribution", "command": {}}, start="2026-11-05T00:00:00Z"
    )
    storage.client("api").write([AppendRaw(raw)], Context.feedback(raw.id)).check()
    storage.run_until_idle()
    assert service.runs() == []


def test_timer_only_processes_completed_periods():
    storage, _ = fixture()
    now = ts("2026-11-05T10:30:00Z")
    service = ExtractionService(storage.client("extraction"), DemoGateway(), clock=lambda: now)
    schedule = Schedule(ts("2026-11-05T09:00:00Z"), timedelta(hours=1))
    tick(service, schedule)
    assert len(service.runs()) == 1
    assert service.runs()[0].payload["command"]["interval"]["end"] == "2026-11-05T10:00:00Z"
    tick(service, schedule)
    assert len(service.runs()) == 1


def test_worker_can_stop_without_processing():
    storage, _ = fixture()
    service = ExtractionService(storage.client("extraction"), DemoGateway())
    stop = Event()
    stop.set()
    serve(service, stop)
    assert service.runs() == []


def test_unknown_port_outcome_stops_after_one_attempt_without_partial_results():
    class Offline:
        def complete(self, call):
            raise TimeoutError()

    storage, _ = fixture()
    now = ts("2026-11-05T10:00:00Z")
    service = ExtractionService(
        storage.client("extraction"), Offline(), clock=lambda: now, max_attempts=2
    )
    service.subscribe()
    id = submit(
        storage,
        Command(kind="extract", interval=Interval(start=ts("2026-11-05T09:00:00Z"), end=now)),
    )
    service.drain()
    now += timedelta(seconds=31)
    service.drain()
    run = storage.client("extraction").get_system_record(id)
    assert run is not None
    assert run.payload["status"] == "blocked"
    assert run.payload["error"] == "gateway_outcome_unknown"
    assert run.payload["attempts"] == 1
    assert not list(iter_query(storage.client("extraction"), Query("activity")))


def test_rule_feedback_requires_version():
    with pytest.raises(ValueError, match="target_id_required"):
        Command(kind="rule_update", rule=Rule(scope="x", instruction="y"))


@pytest.fixture
def gateway_call() -> Call:
    return Call(
        root_feedback_id="system",
        depth=0,
        run_id=new_id(),
        purpose="selection",
        data={"rules": []},
        output_schema={"type": "object"},
    )


@pytest.mark.parametrize("scheduled", [False, True])
def test_gateway_transport_preserves_attribution_and_schema(gateway_call: Call, scheduled):
    if scheduled:
        gateway_call = gateway_call.model_copy(
            update={
                "run_id": schedule_id(
                    Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))
                )
            }
        )
    else:
        gateway_call = gateway_call.model_copy(update={"root_feedback_id": new_id()})
    seen: list[CallRequest] = []

    def respond(request: httpx.Request):
        assert request.method == "POST"
        assert str(request.url) == "https://module-seven.invalid/v1/complete"
        assert request.headers["Authorization"] == "Bearer " + "t" * 43
        body = json.loads(request.content)
        seen.append(CallRequest.model_validate(body))
        for field, value in gateway_call.model_dump(mode="json").items():
            assert body[field] == value
        assert body["system"] == SYSTEM
        assert body["model"] == "default"
        assert is_id(body["call_id"])
        return httpx.Response(200, json={"record_saved": True, "output": {"rule_ids": []}})

    with GatewayClient(
        "https://module-seven.invalid",
        "t" * 43,
        system=SYSTEM,
        transport=httpx.MockTransport(respond),
    ) as client:
        gateway: ModelGateway = client  # pyright checks structural compatibility.
        assert gateway.complete(gateway_call) == {"rule_ids": []}
        assert gateway.complete(gateway_call) == {"rule_ids": []}
    assert len(seen) == 2
    assert seen[0].call_id != seen[1].call_id


@pytest.mark.parametrize("status", [403, 307])
def test_gateway_does_not_hide_http_failures_or_follow_redirects(gateway_call, status):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, json={}, headers={"Location": "https://other.invalid"})

    with (
        GatewayClient(
            "https://module-seven.invalid",
            "t" * 43,
            system=SYSTEM,
            transport=httpx.MockTransport(respond),
        ) as gateway,
        pytest.raises(GatewayError, match="gateway_request_failed") as error,
    ):
        gateway.complete(gateway_call)
    assert error.value.status == status
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"output": {}},
        {"record_saved": False, "output": {}},
        {"record_saved": True, "output": []},
        {"record_saved": True, "output": "text"},
        {"record_saved": True, "output": None},
        {"record_saved": True},
    ],
)
def test_gateway_rejects_invalid_response(gateway_call, body):
    with (
        GatewayClient(
            "https://module-seven.invalid",
            "t" * 43,
            system=SYSTEM,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        ) as gateway,
        pytest.raises(GatewayError, match="invalid_gateway_response") as error,
    ):
        gateway.complete(gateway_call)
    assert error.value.status == 502


def test_gateway_transport_failure_is_unknown_and_not_retried(gateway_call):
    seen = []

    def respond(request):
        seen.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with (
        GatewayClient(
            "https://module-seven.invalid",
            "t" * 43,
            system=SYSTEM,
            transport=httpx.MockTransport(respond),
        ) as gateway,
        pytest.raises(GatewayError, match="gateway_outcome_unknown"),
    ):
        gateway.complete(gateway_call)
    assert len(seen) == 1


def test_gateway_rejects_invalid_run_id_before_transport(gateway_call):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"record_saved": True, "output": {}})

    with (
        GatewayClient(
            "https://module-seven.invalid",
            "t" * 43,
            system=SYSTEM,
            transport=httpx.MockTransport(respond),
        ) as gateway,
        pytest.raises(ValidationError, match="run_id"),
    ):
        gateway.complete(gateway_call.model_copy(update={"run_id": "synthetic-run"}))
    assert not seen


def test_tick_pumps_only_jobs_once_before_drain(monkeypatch):
    storage, _ = fixture()
    service = ExtractionService(storage.client("extraction"), DemoGateway())
    order = []
    monkeypatch.setattr(
        service.storage, "poll_once", lambda consumer: order.append(consumer), raising=False
    )
    monkeypatch.setattr(service, "drain", lambda: order.append("drain"))
    tick(service)
    assert order == ["extraction.jobs", "drain"]
