"""Accepted §12.2 eight-step SYNTHETIC loop, including the actual CLI timer.

Real ③/④/⑤ child processes, loopback ①/⑥/⑦ HTTP, disposable PostgreSQL/NATS,
real request/Finish/llm_call and action-v2 HTTP. Only Evidence/embeddings/locator,
provider and explicitly trusted idempotent external adapter are fake. No owner
handler calls, fabricated Finish, real audio/model/calendar, ear-dev or deployment.
Two bounded cases: success, and lost external reply + lookup recovery after API
reopen. Graceful child restarts are NOT OS crash/power-loss/exactly-once claims.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from ear_adapter import AdapterConfig, AlreadyIngested, Ingested, Runner
from ear_adapter.testing import FakeLocator, evidence_json
from ear_api.app import create_app
from ear_api.config import Settings
from ear_api.execution import ExternalResult
from ear_contracts import LLMCallRecord, SemanticRecord, TracedCause, new_id
from ear_extraction.domain import Action
from ear_extraction.orchestration import COMPLETION_CONSUMER
from ear_storage_client import Query

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.postgres, pytest.mark.nats]


def shared_helpers():
    sys.path.insert(0, str(ROOT))
    try:
        from tests import test_mvp_worker_runtime

        return test_mvp_worker_runtime
    finally:
        sys.path.pop(0)


w = shared_helpers()
core = w.core
postgres_server, pg_dsn, nats_server = w.postgres_server, w.pg_dsn, w.nats_server
core_http_runtime = w.core_http_runtime


class IdempotentExternal:
    """Trusted fake external service; its effects survive API lifespan reopening."""

    name = "synthetic_eight_steps"
    reliable_idempotency = True

    def __init__(self, rt, path):
        self.rt, self.path = rt, path
        self.calls, self.lookups, self.effects = [], [], {}
        self.lose_reply = False

    def lookup(self, action_key: str, action_hash: str) -> ExternalResult:
        self.lookups.append(action_key)
        saved = self.effects.get(action_key)
        if saved is not None:
            assert saved.action_hash == action_hash
            return saved
        return ExternalResult(
            action_key=action_key, action_hash=action_hash, outcome="not_executed"
        )

    def execute(self, action_key: str, action_hash: str, action: Action) -> ExternalResult:
        # Observe real ④ acknowledgement AND durable intent at the effect boundary.
        with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as db:
            [row] = db.execute("SELECT body FROM execution").fetchall()
        persisted = json.loads(row[0])
        current = self.rt.clients["extraction"].get(persisted["suggestion_id"])
        assert isinstance(current, SemanticRecord)
        assert current.writer == "extraction" and current.payload["state"] == "executing"
        assert persisted["state"] == "executing" and persisted["delivery"] == "applied"
        assert persisted["owner_version"] == current.version
        assert persisted["owner_receipt"] == current.payload["receipt"]
        assert persisted["owner_execution"] == current.payload["execution"]
        assert persisted["action_key"] == action_key and persisted["action_hash"] == action_hash
        assert action == Action.model_validate(current.payload["action"])
        assert action.title == "Synthetic owner: Synthetic B"
        self.calls.append(action_key)
        result = self.effects.setdefault(
            action_key,
            ExternalResult(
                action_key=action_key,
                action_hash=action_hash,
                outcome="succeeded",
                external_ref="synthetic-private-effect",
            ),
        )
        assert result.action_hash == action_hash
        if self.lose_reply:
            raise TimeoutError("synthetic reply lost AFTER idempotent effect")
        return result


@pytest.fixture
def eight_runtime(core_http_runtime):
    rt = core_http_runtime
    rt.path = rt.directory / "action-v2.sqlite3"
    rt.external = IdempotentExternal(rt, rt.path)
    rt.exits = []
    rt.api_reopens = 0
    token = secrets.token_urlsafe(32)
    settings = Settings.model_validate(
        {
            "storage_url": rt.url,
            "execution_action_v2": True,
            "execution_journal": rt.path,
            "bindings": [
                {
                    "subject": "synthetic_d1",
                    "role": "user",
                    "token": token,
                    "storage_token": rt.principals["api"].token,
                }
            ],
        }
    )
    # The shared fixture's default API stays unused; this app is the v2 host.
    # No global patching of fixture factories, permissions or owner implementations.
    with ExitStack() as api_stack:

        def reopen():
            api_stack.close()  # Real lifespan releases the journal/OS lock.
            url = api_stack.enter_context(core.loopback(create_app(settings, adapter=rt.external)))
            rt.http = api_stack.enter_context(
                httpx.Client(
                    base_url=url,
                    trust_env=False,
                    timeout=15,
                    headers={"Authorization": "Bearer " + token},
                )
            )
            rt.api_reopens += 1

        rt.reopen_api = reopen
        reopen()
        assert stat.S_IMODE(rt.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(rt.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(rt.identity_output.stat().st_mode) == 0o600
        yield rt


def restart_worker(rt, workers, module, *, schedule=None):
    """Graceful restart of ONE owned child, same identity/consumer/private journal."""
    worker = next(item for item in workers if item.module == module)
    old = worker.process
    old.terminate()
    try:
        out, err = old.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        old.kill()
        old.communicate(timeout=10)
        pytest.fail(f"{module} required forced restart cleanup")
    rt.exits.append({"module": module, "pid": old.pid, "exit": old.returncode})
    assert old.returncode == 0 and out == err == b""
    assert json.loads((worker.state / "health.json").read_text())["status"] == "stopped"
    if schedule is not None:
        config = json.loads(worker.config.read_text())
        config["schedule"] = schedule
        worker.config.write_text(json.dumps(config))  # Existing test-private 0600 file.
    assert stat.S_IMODE(worker.config.stat().st_mode) == 0o600
    worker.process = subprocess.Popen(
        [sys.executable, str(w.SCRIPT), "--config", str(worker.config)],
        cwd=ROOT,
        env=w.child_environment(worker.config.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def ready():
        value = json.loads((worker.state / "health.json").read_text())
        return value["pid"] == worker.process.pid and w.ready(worker)

    w.wait_for(rt, workers, ready, module + " restart readiness", timeout=30)
    assert worker.process.pid != old.pid


def recent_evidence():
    """Move only synthetic fixture timestamps into ONE completed 30-minute period.

    No clock monkeypatch: the actual CLI timer schedules this period; the next
    one is ~25 minutes away, beyond this bounded test. No historical catch-up.
    """
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=35)
    delta = start - datetime.fromisoformat(core.INTERVAL["start"])

    def shift(value):
        if isinstance(value, dict):
            return {key: shift(item) for key, item in value.items()}
        if isinstance(value, list):
            return [shift(item) for item in value]
        if isinstance(value, str) and value.startswith("2026-10-26T"):
            return (datetime.fromisoformat(value) + delta).isoformat().replace("+00:00", "Z")
        return value

    return shift(evidence_json()), start


def feedback(rt, workers, module, value):
    body = {"request_id": new_id(), "module": module, "command": value}
    reply = rt.http.post("/v1/feedback", json=body)
    assert reply.status_code == 202, reply.text
    accepted = reply.json()
    fid = accepted["feedback_id"]
    # No relay tick since submission: accepted is not owner execution.
    assert core.api_status(rt, fid)["status"] == "pending"
    assert rt.http.post("/v1/feedback", json=body).json() == accepted

    def done():
        status = core.api_status(rt, fid)
        assert status["status"] not in {"failed", "blocked", "rejected"}, status
        return status["status"] == "succeeded"

    w.wait_for(rt, workers, done, module + " real feedback completion")
    assert core.api_status(rt, fid)["completion_source"] == (
        "attribution_feedback_result" if module == "attribution" else "extraction_run"
    )
    return fid, body, accepted


def action_state(rt):
    reply = rt.http.get(rt.action_base + "/execution")
    assert reply.status_code == 200, reply.text
    assert "synthetic-private-effect" not in reply.text
    return reply.json()


def action_post(rt, route, **extra):
    current = action_state(rt)
    body = {key: current[key] for key in ("expected_version", "expected_revision")}
    reply = rt.http.post(rt.action_base + route, json={**body, **extra})
    assert reply.status_code == 202, reply.text
    return reply.json(), body


def all_rows(rt, kind):
    page = rt.clients["extraction"].query(Query(kind, limit=100, include_superseded=True))
    assert page.next_cursor is None, "never silently truncate acceptance evidence"
    return list(page.items)


def verify_trace(rt, root, expected):
    requests = core.requests_for(rt, root)
    assert {r.target_id for r in requests} == set(expected) and len(requests) == 3
    calls = [c for c in rt.provider.calls if c.root_feedback_id == root]
    assert len(calls) == 3 and {c.request_event_id for c in calls} == {r.event_id for r in requests}
    finishes = []
    for request in requests:
        finish = rt.clients["dependency"].get_reevaluation_result(request.event_id)
        assert finish is not None
        assert finish.outcome == expected[request.target_id]
        assert finish.root_feedback_id == root and finish.depth == request.depth
        assert finish.request_event_id == request.event_id
        assert rt.clients["extraction"].is_processed(COMPLETION_CONSUMER, request.event_id)
        call = next(c for c in calls if c.request_event_id == request.event_id)
        record = rt.clients["llm_gateway"].get_system_record(call.call_id)
        assert record is not None and record.writer == "llm_gateway"
        audit = LLMCallRecord.model_validate(record.payload)
        assert audit.status == "success" and audit.request_event_id == request.event_id
        assert audit.caller_module == "extraction" and audit.root_feedback_id == root
        assert audit.depth == request.depth and audit.model == "synthetic/core-d1"
        primary = TracedCause(cause=request.cause, root_feedback_id=root, depth=request.depth)
        assert primary in audit.additional_causes
        assert all(c in audit.additional_causes for c in request.additional_causes)
        assert audit.usage is None
        assert "request_text" not in record.payload and "response_text" not in record.payload
        run = rt.clients["extraction"].get_system_record(call.run_id)
        assert run is not None and run.payload["status"] == "succeeded"
        assert any(
            i["call_id"] == call.call_id and i["state"] == "returned"
            for i in run.payload["gateway_calls"]
        )
        deps = rt.clients["dependency"].dependencies_of(request.target_id)
        assert deps
        assert not any(
            d.dependent.id == request.target_id
            for d in rt.clients["dependency"].stale_dependencies([d.depends_on.id for d in deps])
        )
        finishes.append(finish.model_dump(mode="json"))
    steps = [s for s in w.steps(rt) if s.request_event_id in {r.event_id for r in requests}]
    assert len(steps) == 3 and all(s.state == "done" for s in steps)
    return requests, finishes


@pytest.mark.parametrize("lost_reply", [False, True], ids=["normal", "lookup-recovery"])
def test_eight_steps_real_workers_and_http_actions(eight_runtime, lost_reply):
    rt = eight_runtime
    rt.external.lose_reply = lost_reply
    assert rt.scheduler is None and not hasattr(rt, "extraction") and not hasattr(rt, "attribution")
    with w.worker_processes(rt) as workers:
        # 1–2: real adapter writes synthetic evidence; actual ③ CLI consumes it.
        evidence, timer_start = recent_evidence()
        source = core.SelectedSource({"cap-1": evidence})
        importer = Runner(
            source=source,
            storage=rt.clients["adapter"],
            locator=FakeLocator(),
            config=AdapterConfig(),
        )
        assert isinstance(importer.run_once(capture_ids=["cap-1"]).outcomes[0], Ingested)
        w.wait_for(rt, workers, lambda: len(core.rows(rt, "attribution")) == 3, "raw attribution")
        raw_counts = {kind: rt.clients["adapter"].count(Query(kind)) for kind in core.RAW_TYPES}
        assert raw_counts == {
            "audio_ref": 1,
            "transcript_segment": 3,
            "local_speaker": 3,
            "voiceprint": 2,
            "ambient_sound": 1,
            "gps_point": 1,
            "phone_info": 2,
        }
        attrs = core.rows(rt, "attribution")
        original, peer = [a for a in attrs if a.payload["target_type"] == "person"]
        assert len(core.rows(rt, "person")) == 2 and len(core.rows(rt, "place")) == 1
        rt.provider.subject = original.payload["subject_id"]
        for attr, name in ((original, "Synthetic A"), (peer, "Synthetic B")):
            feedback(
                rt,
                workers,
                "attribution",
                {
                    "kind": "rename",
                    "target_id": attr.payload["target_id"],
                    "name": name,
                },
            )
        for instruction in ("owner", "fixed"):
            feedback(
                rt,
                workers,
                "extraction",
                {
                    "kind": "rule_create",
                    "rule": {
                        "scope": "synthetic meeting",
                        "instruction": instruction,
                        "action": "reminder" if instruction == "owner" else None,
                    },
                },
            )
        # 3: opt in ONLY now, after rules/attribution are ready. No direct schedule call.
        restart_worker(
            rt,
            workers,
            "extraction",
            schedule={
                "start": timer_start.isoformat().replace("+00:00", "Z"),
                "every_seconds": 1800,
            },
        )
        w.wait_for(rt, workers, lambda: len(core.rows(rt, "artifact")) == 2, "CLI timer extraction")
        [scheduled] = [r for r in all_rows(rt, "extraction_run") if r.payload["scheduled"]]
        assert scheduled.payload["status"] == "succeeded"
        assert scheduled.payload["root_feedback_id"] == "system"
        [activity], [selection] = core.rows(rt, "activity"), core.rows(rt, "rule_selection")
        artifacts = core.rows(rt, "artifact")
        owner = next(a for a in artifacts if a.payload["body_md"] == "Synthetic owner: Synthetic A")
        fixed = next(a for a in artifacts if a.payload["body_md"] == "Synthetic unchanged note.")
        [old_suggestion] = core.rows(rt, "action_suggestion")
        assert set(selection.payload["rule_ids"]) == {a.payload["rule_id"] for a in artifacts}
        assert rt.clients["extraction"].dependencies_of(activity.id) == []
        for artifact in artifacts:
            deps = rt.clients["extraction"].dependencies_of(artifact.id)
            assert {d.depends_on.id for d in deps if d.depends_on.data_type == "attribution"} == {
                a.id for a in attrs
            }
            assert not any(d.depends_on.data_type == "rule_selection" for d in deps)
            assert all(
                d.fields == ("name",) for d in deps if d.depends_on.data_type in {"person", "place"}
            )
        # 4: actual ⑥ HTTP reads; not SDK-only assertions.
        for path, ids in (
            ("/v1/timeline", {activity.id}),
            ("/v1/records/artifact", {owner.id, fixed.id}),
            ("/v1/records/action_suggestion", {old_suggestion.id}),
        ):
            reply = rt.http.get(path)
            assert reply.status_code == 200
            assert {r["id"] for r in reply.json()["items"]} == ids
        # Establish the normal-flow precondition, rather than measuring the raw
        # import/rename backlog as correction latency. Read-only barrier; no owner
        # pumping, history skipping, scheduler writes or larger timeout. This is
        # deliberately NOT a sustained-load/backlog performance acceptance test.
        initial_events = {e.event_id for e in core.events(rt)}

        def baseline_settled():
            page = rt.clients["dependency"].query(
                Query("propagation_record", where={"schema_tag": w.SCHEMA}, limit=100)
            )
            assert page.next_cursor is None
            handled = {
                r.payload["event"]["event_id"]
                for r in page.items
                if r.payload["record_kind"] == "trigger"
                and r.payload["status"] in {"closed", "ignored"}
            }
            return initial_events <= handled and all(
                r.payload["state"] in {"done", "unaffected"}
                for r in page.items
                if r.payload["record_kind"] == "step"
            )

        w.wait_for(rt, workers, baseline_settled, "initial events settled before correction")
        # 5–6: correction via API -> real ③ -> real ⑤ requests -> real ④ Finish.
        correction_started = time.monotonic()
        root, correction_body, correction_reply = feedback(
            rt,
            workers,
            "attribution",
            {
                "kind": "correct_attribution",
                "subject_id": original.payload["subject_id"],
                "previous_target_id": original.payload["target_id"],
                "new_target_id": peer.payload["target_id"],
                "expected_version": original.version,
            },
        )
        expected = {owner.id: "changed", fixed.id: "unchanged", selection.id: "unchanged"}

        def completed():
            requests = core.requests_for(rt, root)
            assert len(requests) <= 3
            if len(requests) != 3:
                return False
            ids = {r.event_id for r in requests}
            steps = [s for s in w.steps(rt) if s.request_event_id in ids]
            assert not any(s.state == "blocked" for s in steps), steps
            return len(steps) == 3 and all(s.state == "done" for s in steps)

        w.wait_for(rt, workers, completed, "correction requests and authoritative Finish")
        correction_seconds = time.monotonic() - correction_started
        assert correction_seconds < 90, "correction submission through Finish exceeded 90s"
        requests, finishes = verify_trace(rt, root, expected)
        current = rt.clients["extraction"].get(owner.id)
        assert isinstance(current, SemanticRecord) and current.version == owner.version + 1
        assert current.payload["body_md"] == "Synthetic owner: Synthetic B"
        assert rt.clients["extraction"].get_version(owner.id, owner.version) == owner
        assert rt.clients["extraction"].get(fixed.id) == fixed
        assert core.rows(rt, "activity") == [activity] and core.rows(rt, "rule_selection") == [
            selection
        ]
        corrected = rt.clients["attribution"].get(original.id)
        assert corrected.payload["user_protected"] is True
        assert corrected.payload["target_id"] == peer.payload["target_id"]
        retired = rt.clients["extraction"].get(old_suggestion.id)
        assert isinstance(retired, SemanticRecord) and retired.superseded
        [suggestion] = core.rows(rt, "action_suggestion")
        assert suggestion.id != old_suggestion.id and suggestion.payload["state"] == "pending"
        assert suggestion.payload["artifact_version"] == current.version
        rt.action_base = f"/v1/actions/{suggestion.id}"
        # 7: review -> owner approved -> HTTP prepare -> owner executing -> one effect.
        review_root, review_body, review_reply = feedback(
            rt,
            workers,
            "extraction",
            {
                "kind": "review_suggestion",
                "target_id": suggestion.id,
                "expected_version": suggestion.version,
                "decision": "approved",
            },
        )
        assert action_state(rt)["suggestion_state"] == "approved"
        approved_version = action_state(rt)["expected_version"]
        reply = rt.http.post(
            rt.action_base + "/prepare", json={"expected_version": approved_version}
        )
        assert reply.status_code == 202 and reply.json()["feedback_delivery_state"] == "accepted"
        before_ack, _ = action_post(rt, "/pump")  # No relay since prepare.
        assert before_ack["suggestion_state"] == "approved"
        assert not rt.external.calls and not rt.external.lookups
        w.wait_for(
            rt, workers, lambda: action_state(rt)["suggestion_state"] == "executing", "④ start ack"
        )
        result, stale_pump = action_post(rt, "/pump")
        assert result["executor_state"] == ("unknown" if lost_reply else "succeeded")
        assert result["feedback_delivery_state"] == "accepted"
        assert len(rt.external.calls) == len(rt.external.effects) == 1
        terminal = "unknown" if lost_reply else "executed"
        w.wait_for(
            rt, workers, lambda: action_state(rt)["suggestion_state"] == terminal, "④ result ack"
        )
        settled, _ = action_post(rt, "/pump")
        assert settled["feedback_delivery_state"] == "applied"
        assert rt.http.post(rt.action_base + "/pump", json=stale_pump).status_code == 409
        rt.reopen_api()
        if lost_reply:
            assert action_state(rt)["recovery"]["requires_user_verification"] is True
            for _ in range(2):
                assert action_post(rt, "/pump")[0]["executor_state"] == "unknown"
            assert len(rt.external.calls) == 1
            lookups = len(rt.external.lookups)
            recovered, _ = action_post(rt, "/recovery", operation="verify")
            assert recovered["executor_state"] == "succeeded"
            assert len(rt.external.lookups) == lookups + 1 and len(rt.external.calls) == 1
            w.wait_for(
                rt,
                workers,
                lambda: action_state(rt)["suggestion_state"] == "executed",
                "④ recovery ack",
            )
        final, _ = action_post(rt, "/pump")
        assert (
            final["suggestion_state"] == "executed"
            and final["feedback_delivery_state"] == "applied"
        )
        assert (
            final["automatic_worker_enabled"] is False and final["automatic_retry_allowed"] is False
        )
        history = rt.clients["extraction"].history(suggestion.id)
        states = [r.payload["state"] for r in history]
        assert states == [
            "pending",
            "approved",
            "executing",
            *(["unknown"] if lost_reply else []),
            "executed",
        ]
        assert all(r.writer == "extraction" for r in history)
        for record in history[1:]:
            status = core.api_status(rt, record.root_feedback_id)
            assert (
                status["status"] == "succeeded" and status["completion_source"] == "extraction_run"
            )
        reply = rt.http.get(f"/v1/records/action_suggestion/{suggestion.id}")
        assert reply.status_code == 200 and reply.json()["payload"]["state"] == "executed"
        assert core.api_status(rt, review_root)["status"] == "succeeded"
        # 8: complete provenance via authoritative ① SDK, not unsupported ⑥ system-record routes.
        verify_trace(rt, root, expected)
        audits = rt.clients["llm_gateway"].query(Query("llm_call", limit=100))
        assert audits.next_cursor is None
        assert len(audits.items) == len(rt.provider.calls) == 7  # Actions never invoke the model.
        assert {r.id for r in audits.items} == {c.call_id for c in rt.provider.calls}
        assert sum(c.purpose == "segmentation" for c in rt.provider.calls) == 1
        assert all(
            c.root_feedback_id == "system" for c in rt.provider.calls if c.request_event_id is None
        )
        # All three real processes + API reopen; durable requests/versions/effects must stay fixed.
        kinds = (
            "attribution",
            "person",
            "place",
            "activity",
            "rule",
            "rule_selection",
            "artifact",
            "action_suggestion",
            "extraction_run",
        )
        before = {kind: all_rows(rt, kind) for kind in kinds}
        event_ids = [e.event_id for e in core.events(rt)]
        for module in w.CONSUMERS:
            restart_worker(rt, workers, module)
        rt.reopen_api()
        assert rt.http.post("/v1/feedback", json=correction_body).json() == correction_reply
        assert rt.http.post("/v1/feedback", json=review_body).json() == review_reply
        replay = Runner(
            source=source,
            storage=rt.clients["adapter"],
            locator=FakeLocator(),
            config=AdapterConfig(),
        )
        assert isinstance(replay.run_once(capture_ids=["cap-1"]).outcomes[0], AlreadyIngested)
        assert (
            rt.http.post(
                rt.action_base + "/prepare", json={"expected_version": approved_version}
            ).status_code
            == 409
        )
        for _ in range(2):
            assert action_post(rt, "/pump")[0]["feedback_delivery_state"] == "applied"
        # A fresh rename of the already-current name provides a post-restart ③
        # consumer barrier without changing a business version. ④ also resumes
        # its real timer, whose already-covered period must not regenerate output.
        final_version = action_state(rt)["expected_version"]
        feedback(
            rt,
            workers,
            "attribution",
            {
                "kind": "rename",
                "target_id": peer.payload["target_id"],
                "name": "Synthetic B",
            },
        )
        # Explicit tick observations need not claim broker redelivery or OS crash recovery.
        assert action_state(rt)["expected_version"] == final_version
        assert {kind: all_rows(rt, kind) for kind in kinds} == before
        new_events = core.events(rt)
        assert [e.event_id for e in new_events[: len(event_ids)]] == event_ids
        assert len(new_events) == len(event_ids) + 1  # Only the fresh no-op feedback raw event.
        assert len(rt.external.calls) == len(rt.external.effects) == 1
        assert len(rt.provider.calls) == 7
        assert verify_trace(rt, root, expected)[1] == finishes
        for module, consumer in w.CONSUMERS.items():
            assert rt.clients[module].deadletters(consumer) == []
        assert datetime.now(UTC) < timer_start + timedelta(hours=1), (
            "test exceeded its single timer period"
        )
        w.assert_alive(workers)
    assert all(worker.process.returncode == 0 for worker in workers)
    print(
        "EIGHT_STEPS_OBSERVATION "
        + json.dumps(
            {
                "case": "lookup-recovery" if lost_reply else "normal",
                "steps": list(range(1, 9)),
                "timer": "actual_cli_one_completed_period",
                "pids": {worker.module: worker.process.pid for worker in workers},
                "exit_codes": {worker.module: worker.process.returncode for worker in workers},
                "restarted_children": rt.exits,
                "api_lifespans": rt.api_reopens,
                "raw_counts": raw_counts,
                "correction_root": root,
                "correction_submission_to_finish_seconds": round(correction_seconds, 3),
                "initial_backlog_settled_before_correction": True,
                "deadline_seconds": 90,
                "request_ids": [r.event_id for r in requests],
                "finish_outcomes": expected,
                "authoritative_llm_calls": len(audits.items),
                "suggestion_history": states,
                "external_execute_calls": len(rt.external.calls),
                "external_effects": len(rt.external.effects),
                "external_lookup_calls": len(rt.external.lookups),
                "external_adapter": "trusted_idempotent_fake",
                "real_model": False,
                "real_calendar": False,
                "http_in_pytest_process": True,
                "duplicate_api_and_input_no_business_effect": True,
                "three_cli_workers_restarted_gracefully": True,
                "os_crash_tested": False,
                "trace_read_channel": "authenticated_storage_http_sdk",
            },
            sort_keys=True,
        )
    )
