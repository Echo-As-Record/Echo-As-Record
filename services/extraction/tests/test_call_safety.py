"""Crash/unknown-response fault injection; no real model or deployment."""

from datetime import timedelta
from threading import Barrier, Lock, Thread

import httpx
import pytest
from ear_contracts import new_id
from ear_extraction.call_safety import DurableCalls
from ear_extraction.demo import DemoGateway, fixture, submit
from ear_extraction.domain import Blocked, Call, Command, Interval
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import ExtractionService
from ear_extraction.persistence import records
from ear_llm_gateway import GatewayClient
from ear_storage_client import AppendRaw, Context, PutSystemRecord
from ear_storage_client.remote_errors import CommitUnknown
from ear_storage_client.testing import make_raw, ts

RANGE = Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))


class Clock:
    now = ts("2026-11-05T10:00:00Z")

    def __call__(self):
        return self.now

    def advance(self):
        self.now += timedelta(hours=1)


class Crash(BaseException):
    """A process exit, deliberately not caught by a task's Exception handler."""


def queued(gateway):
    store, _ = fixture()
    clock = Clock()
    service = ExtractionService(store.client("extraction"), gateway, clock=clock)
    service.subscribe()
    id = submit(store, Command(kind="extract", interval=RANGE))
    return store, clock, service, id


def get_run(service, id):
    result = service.storage.get_system_record(id)
    assert result is not None
    return result


@pytest.mark.parametrize("failure", ["timeout", "disconnect", 409, 429, 502, 503, "invalid"])
def test_gateway_unknown_never_resends_after_restart_retry_or_reconcile(failure):
    seen = []
    reconciled = []

    def respond(request):
        if request.url.path.endswith("/reconcile"):
            reconciled.append(request.url.path.split("/")[-2])
            return httpx.Response(
                200,
                json={
                    "record_saved": True,
                    "status": "success",
                    "response_recoverable": False,
                    # Even a malicious/incorrect receipt must not supply business output.
                    "output": {"activities": []},
                },
            )
        import json

        seen.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("SECRET_TIMEOUT", request=request)
        if failure == "disconnect":
            raise httpx.ReadError("SECRET_DISCONNECT", request=request)
        if failure == "invalid":
            return httpx.Response(200, json={"record_saved": False})
        return httpx.Response(failure, json={"error": "not a safe retry attestation"})

    with GatewayClient(
        "https://synthetic.invalid",
        "t" * 43,
        system=SYSTEM,
        transport=httpx.MockTransport(respond),
    ) as http:
        store, clock, service, id = queued(http)
        service.drain()
        state = get_run(service, id)
        assert state.payload["status"] == "blocked"
        assert state.payload["error"] == "gateway_outcome_unknown"
        intent = state.payload["gateway_calls"][0]
        assert intent["call_id"] == seen[0]["call_id"]
        assert len(intent["request_fingerprint"]) == 64
        clock.advance()
        recovered = ExtractionService(service.storage, http, clock=clock)
        recovered.drain()
        recovered.process(id)
        with pytest.raises(ValueError, match="gateway_outcome_unknown"):
            recovered.retry(id)
        receipt = recovered.reconcile_run(id)
        assert "output" not in receipt and receipt["response_recoverable"] is False
        assert receipt["status"] == "success"
        assert get_run(recovered, id).payload["status"] == "blocked"
        with pytest.raises(ValueError, match="gateway_outcome_unknown"):
            recovered.retry(id)
        assert reconciled and set(reconciled) == {intent["call_id"]}
        assert len(seen) == 1
        assert not records(service.storage, "activity")
        assert not records(service.storage, "artifact")
        assert not store.dead_letters


@pytest.mark.parametrize("receipt_status", [404, 503])
def test_reconcile_error_cannot_release_blocked_run(receipt_status):
    sent = []

    def respond(request):
        sent.append(request.url.path)
        if request.url.path.endswith("/reconcile"):
            return httpx.Response(receipt_status, json={})
        raise httpx.ReadTimeout("synthetic", request=request)

    with GatewayClient(
        "https://synthetic.invalid", "t" * 43, transport=httpx.MockTransport(respond)
    ) as http:
        _, clock, service, id = queued(http)
        service.drain()
        clock.advance()
        ExtractionService(service.storage, http, clock=clock).drain()
        with pytest.raises(ValueError, match="gateway_outcome_unknown"):
            service.retry(id)
        assert sent.count("/v1/complete") == 1
        assert get_run(service, id).payload["status"] == "blocked"


@pytest.mark.parametrize("phase", ["before_send", "after_send", "after_return"])
def test_crash_windows_recover_only_to_blocked(monkeypatch, phase):
    class Port(DemoGateway):
        def __init__(self):
            self.calls = []
            self.receipts = []

        def complete(self, call):
            self.calls.append(call)
            if phase == "after_send":
                raise Crash()
            return super().complete(call)

        def reconcile(self, call_id):
            self.receipts.append(call_id)
            return {"status": "success", "record_saved": True, "response_recoverable": False}

    port = Port()
    _, clock, service, id = queued(port)
    save = DurableCalls.save

    def crashing_save(tracker, payload):
        save(tracker, payload)
        calls = payload.get("gateway_calls")
        if calls and (
            (phase == "before_send" and calls[-1]["state"] == "sending")
            or (phase == "after_return" and calls[-1]["state"] == "returned")
        ):
            raise Crash()

    monkeypatch.setattr(DurableCalls, "save", crashing_save)
    with pytest.raises(Crash):
        service.drain()
    intent = get_run(service, id).payload["gateway_calls"][0]
    assert intent["state"] == ("returned" if phase == "after_return" else "sending")
    count = len(port.calls)
    assert count == (0 if phase == "before_send" else 1)
    monkeypatch.setattr(DurableCalls, "save", save)
    clock.advance()
    recovered = ExtractionService(service.storage, port, clock=clock)
    recovered.drain()
    assert get_run(recovered, id).payload["status"] == "blocked"
    with pytest.raises(ValueError, match=r"gateway_(outcome_unknown|result_not_committed)"):
        recovered.retry(id)
    assert len(port.calls) == count
    if intent["state"] == "sending":
        assert port.receipts == [intent["call_id"]]


@pytest.mark.parametrize("committed", [False, True])
def test_intent_commit_unknown_never_sends_without_ack(monkeypatch, committed):
    class Port(DemoGateway):
        count = 0

        def complete(self, call):
            self.count += 1
            return super().complete(call)

    port = Port()
    _, clock, service, id = queued(port)
    write = service.storage.write
    fired = False

    def uncertain(ops, context):
        nonlocal fired
        intent = next((op for op in ops if isinstance(op, PutSystemRecord)), None)
        if intent and intent.payload.get("gateway_calls") and not fired:
            fired = True
            if committed:
                write(ops, context).check()
            raise CommitUnknown(new_id())
        return write(ops, context)

    monkeypatch.setattr(service.storage, "write", uncertain)
    service.drain()
    assert fired and port.count == 0
    clock.advance()
    recovered = ExtractionService(service.storage, port, clock=clock)
    recovered.drain()
    assert get_run(service, id).payload["status"] == "blocked"
    with pytest.raises(ValueError, match="gateway_outcome_unknown"):
        recovered.retry(id)
    assert port.count == 0


def test_cas_intent_conflict_fences_losing_worker_before_send():
    class Port:
        def __init__(self):
            self.ids = []
            self.lock = Lock()

        def complete(self, call):
            with self.lock:
                self.ids.append(call.call_id)
            return {"activities": []}

    port = Port()
    store, _, service, id = queued(port)
    run = get_run(service, id)
    start = Barrier(2)
    outcomes = []

    def worker():
        # Two independent clients with exactly the same read revision.
        tracker = DurableCalls(
            store.client("extraction"),
            port,
            id,
            run.payload,
            run.revision,
            Context.feedback(run.payload["root_feedback_id"]),
        )
        start.wait(timeout=5)
        try:
            tracker.complete(
                Call(
                    root_feedback_id=run.payload["root_feedback_id"],
                    depth=0,
                    run_id=id,
                    purpose="segmentation",
                    data={},
                    output_schema={"type": "object"},
                )
            )
            outcomes.append("sent")
        except Blocked as exc:
            outcomes.append(str(exc))

    workers = [Thread(target=worker) for _ in range(2)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["run_fence_lost", "sent"]
    assert len(port.ids) == 1
    assert get_run(service, id).payload["gateway_calls"][0]["call_id"] == port.ids[0]


def test_legacy_never_attempted_job_gets_tracking_before_wait():
    store, clock, service, id = queued(DemoGateway())
    run = get_run(service, id)
    payload = dict(run.payload)
    payload.pop("gateway_calls")
    service.storage.write(
        [PutSystemRecord("extraction_run", id, payload, expected_revision=run.revision)],
        service._context(run),
    ).check()
    raw = make_raw("gps_point", {"latitude": 0}, start="2026-11-05T09:30:00Z")
    store.client("adapter").write([AppendRaw(raw)], Context.system()).check()
    service.drain()
    assert get_run(service, id).payload["status"] == "waiting_attribution"
    assert get_run(service, id).payload["gateway_calls"] == []
    clock.advance()
    service.drain()
    assert get_run(service, id).payload["error"] == "attributions_pending"


@pytest.mark.parametrize("already_requeued", [False, True])
def test_legacy_attempted_job_without_intent_is_blocked(already_requeued):
    _, clock, service, id = queued(DemoGateway())
    run = get_run(service, id)
    payload = {
        **run.payload,
        "status": "queued" if already_requeued else "retry_wait",
        "attempts": 0 if already_requeued else 1,
        "retry_at": None,
    }
    payload.pop("gateway_calls")
    service.storage.write(
        [PutSystemRecord("extraction_run", id, payload, expected_revision=run.revision)],
        service._context(run),
    ).check()
    clock.advance()
    service.drain()
    assert get_run(service, id).payload["error"] == "legacy_gateway_outcome_untracked"
    with pytest.raises(ValueError, match="legacy_gateway_outcome_untracked"):
        service.retry(id)


def test_timer_cannot_bypass_unknown_run_via_larger_interval():
    class Unknown:
        count = 0

        def complete(self, call):
            self.count += 1
            raise TimeoutError()

    port = Unknown()
    store, _ = fixture()
    clock = Clock()
    service = ExtractionService(store.client("extraction"), port, clock=clock)
    id = service.schedule(RANGE)[0]
    service.process(id)
    assert get_run(service, id).payload["status"] == "blocked"
    later = Interval(start=RANGE.start, end=RANGE.end + timedelta(hours=1))
    clock.advance()
    recovered = ExtractionService(store.client("extraction"), port, clock=clock)
    new_ids = recovered.schedule(later)
    assert new_ids
    new_run = get_run(recovered, new_ids[0])
    assert Interval.model_validate(new_run.payload["command"]["interval"]).start == RANGE.end
    assert port.count == 1
    assert get_run(service, id).payload["status"] == "blocked"


@pytest.mark.parametrize("committed", [False, True])
def test_business_commit_unknown_is_fenced_not_replanned(monkeypatch, committed):
    class Port(DemoGateway):
        count = 0

        def complete(self, call):
            self.count += 1
            return super().complete(call)

    port = Port()
    _, clock, service, id = queued(port)
    write = service.storage.write
    fired = False

    def uncertain(ops, context):
        nonlocal fired
        if not fired and any(
            isinstance(op, PutSystemRecord) and op.payload.get("status") == "succeeded"
            for op in ops
        ):
            fired = True
            if committed:
                write(ops, context).check()
            raise CommitUnknown(new_id())
        return write(ops, context)

    monkeypatch.setattr(service.storage, "write", uncertain)
    service.drain()
    assert fired and port.count == 1
    clock.advance()
    recovered = ExtractionService(service.storage, port, clock=clock)
    recovered.drain()
    final = get_run(recovered, id)
    assert final.payload["status"] == ("succeeded" if committed else "blocked")
    assert len(records(service.storage, "activity")) == int(committed)
    assert port.count == 1
    if not committed:
        with pytest.raises(ValueError, match="gateway_result_not_committed"):
            recovered.retry(id)


@pytest.mark.parametrize("committed", [False, True])
def test_returned_intent_ack_loss_does_not_start_next_model_step(monkeypatch, committed):
    class Port(DemoGateway):
        count = 0

        def complete(self, call):
            self.count += 1
            return super().complete(call)

    port = Port()
    _, clock, service, id = queued(port)
    write = service.storage.write
    fired = False

    def uncertain(ops, context):
        nonlocal fired
        for op in ops:
            if isinstance(op, PutSystemRecord) and not fired:
                calls = op.payload.get("gateway_calls", [])
                if calls and calls[-1]["state"] == "returned":
                    fired = True
                    if committed:
                        write(ops, context).check()
                    raise CommitUnknown(new_id())
        return write(ops, context)

    monkeypatch.setattr(service.storage, "write", uncertain)
    service.drain()
    assert fired and port.count == 1
    clock.advance()
    ExtractionService(service.storage, port, clock=clock).drain()
    assert get_run(service, id).payload["status"] == "blocked"
    with pytest.raises(ValueError, match=r"gateway_(outcome_unknown|result_not_committed)"):
        service.retry(id)
    assert not records(service.storage, "activity")
    assert port.count == 1


def test_expired_lease_recovery_fences_worker_still_inside_provider():
    class Port(DemoGateway):
        count = 0

        def complete(self, call):
            self.count += 1
            clock.advance()
            recovered = ExtractionService(service.storage, self, clock=clock)
            recovered.process(id)
            assert get_run(service, id).payload["error"] == "gateway_outcome_unknown"
            return super().complete(call)

    port = Port()
    _, clock, service, id = queued(port)
    service.drain()
    final = get_run(service, id)
    assert port.count == 1
    assert final.payload["status"] == "blocked"
    assert final.payload["gateway_calls"][0]["state"] == "sending"
    assert not records(service.storage, "activity")
    with pytest.raises(ValueError, match="gateway_outcome_unknown"):
        service.retry(id)
