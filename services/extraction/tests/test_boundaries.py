"""Ingress, timer and gateway boundary tests (no remote model calls)."""

from datetime import timedelta
from threading import Event

import httpx
import pytest
from ear_extraction.demo import DemoGateway, fixture, submit
from ear_extraction.domain import Call, Command, Interval, Rule
from ear_extraction.model_io import HttpGateway
from ear_extraction.orchestration import ExtractionService
from ear_extraction.worker import Schedule, serve, tick
from ear_storage_client import AppendRaw, Context, Query, iter_query
from ear_storage_client.testing import make_raw, ts


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


def test_retry_is_bounded_and_never_publishes_partial_results():
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
    assert run.payload["status"] == "failed"
    assert run.payload["attempts"] == 2
    assert not list(iter_query(storage.client("extraction"), Query("activity")))


def test_rule_feedback_requires_version():
    with pytest.raises(ValueError, match="target_id_required"):
        Command(kind="rule_update", rule=Rule(scope="x", instruction="y"))


def test_gateway_transport_preserves_attribution_and_schema(monkeypatch):
    seen = []

    def respond(request):
        import json

        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-only"
        return httpx.Response(200, json={"output": {"rule_ids": []}})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    call = Call(
        root_feedback_id="system",
        depth=0,
        run_id="synthetic-run",
        purpose="selection",
        data={"rules": []},
        output_schema={"type": "object"},
    )
    result = HttpGateway("https://module-seven.invalid/invoke", "test-only").complete(call)
    assert result == {"rule_ids": []}
    assert seen[0]["module"] == "extraction"
    assert seen[0]["root_feedback_id"] == "system"
    assert seen[0]["output_schema"] == {"type": "object"}
    assert "system" in seen[0]


def test_gateway_does_not_hide_http_failures(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(403))
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        HttpGateway("https://module-seven.invalid/invoke", "test-only").complete(
            Call(
                root_feedback_id="system",
                depth=0,
                run_id="test",
                purpose="selection",
                data={},
                output_schema={},
            )
        )
