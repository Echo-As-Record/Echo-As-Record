"""Real ③/④/⑤ CLI children + loopback HTTP and disposable PG/NATS, fake model.

No in-process owner handlers, fake Finish, external provider/calendar, ear-dev,
image build or production data. HTTP/gateway/provider and explicit relay ticks
stay in pytest; only workers are independent OS processes. No crash claim.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from ear_adapter import AdapterConfig, Ingested, Runner
from ear_adapter.testing import FakeLocator, evidence_json
from ear_contracts import LLMCallRecord, SemanticRecord, TracedCause
from ear_dependency.domain import SCHEMA, Step
from ear_extraction.orchestration import COMPLETION_CONSUMER
from ear_storage.core_deployment import init_core_identities
from ear_storage_client import Query

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "mvp_worker.py"
pytestmark = [pytest.mark.postgres, pytest.mark.nats]


def shared_fixtures():
    # pytest's importlib mode does not put the repository root on sys.path.
    # Reuse the existing isolated fixtures without a root/conftest modification;
    # restore the import path immediately, including on import failure.
    sys.path.insert(0, str(ROOT))
    try:
        from services.storage.tests import conftest, test_mvp_core_runtime

        return conftest, test_mvp_core_runtime
    finally:
        sys.path.pop(0)


fixtures, core = shared_fixtures()
postgres_server = fixtures.postgres_server
pg_dsn = fixtures.pg_dsn
nats_server = fixtures.nats_server
core_http_runtime = core.core_http_runtime
Harness = core.Harness
INTERVAL, RAW_TYPES = core.INTERVAL, core.RAW_TYPES
SelectedSource = core.SelectedSource
api_status, events, relay = core.api_status, core.events, core.relay
requests_for, rows, submit = core.requests_for, core.rows, core.submit
CONSUMERS = {
    "attribution": "attribution",
    "extraction": "extraction.jobs",
    "dependency": "dependency.changes",
}


def child_environment(directory):
    # Allowlist: no inherited deployment/model credentials, external endpoints or
    # Python injection. Poison loopback proxies also exercise trust_env=False.
    return {
        "PATH": os.defpath,
        "HOME": str(directory),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "ALL_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "",
    }


def assert_alive(workers):
    for worker in workers:
        assert worker.process.poll() is None, (
            worker.module,
            "worker exited before business completion",
            worker.process.returncode,
        )


def wait_for(rt, workers, condition, label, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert_alive(workers)
        # Real outbox -> JetStream only. NEVER pump owners or construct them here.
        relay(rt)
        if condition():
            return
        time.sleep(0.15)
    # Capture synthetic metadata BEFORE fixture teardown removes the database.
    # A deadline/blocked graph remains a failure, never a skip or fake completion.
    propagation = rt.clients["dependency"].query(Query("propagation_record", limit=100))
    runs = rt.clients["extraction"].query(Query("extraction_run", limit=100))
    print(
        "WORKER_PROCESS_BLOCKER "
        + json.dumps(
            {
                "phase": label,
                "workers": {
                    w.module: {"pid": w.process.pid, "exit": w.process.poll()} for w in workers
                },
                "propagation": [r.payload for r in propagation.items],
                "propagation_has_more": propagation.next_cursor is not None,
                "runs": [
                    {
                        "id": r.id,
                        "kind": r.payload["command"]["kind"],
                        "status": r.payload["status"],
                        "error": r.payload.get("error"),
                    }
                    for r in runs.items
                ],
                "runs_has_more": runs.next_cursor is not None,
                "requests": [
                    e.model_dump(mode="json")
                    for e in events(rt)
                    if e.event_type == "reevaluation_requested"
                ],
                "provider_calls": [
                    {
                        "call_id": c.call_id,
                        "purpose": c.purpose,
                        "root_feedback_id": c.root_feedback_id,
                        "request_event_id": c.request_event_id,
                    }
                    for c in rt.provider.calls
                ],
            },
            sort_keys=True,
        )
    )
    raise AssertionError(f"real worker deadline: {label}")


def ready(worker):
    path = worker.state / "health.json"
    if not path.exists():
        return False
    value = json.loads(path.read_text())
    assert value["pid"] == worker.process.pid != os.getpid()
    assert value["module"] == worker.module
    assert value["scope"] == "worker_pump_only"
    return value["status"] in {"ready", "busy"}


@contextmanager
def worker_processes(rt):
    directory = rt.directory / "workers"
    directory.mkdir(mode=0o700)
    workers = []
    failures = []
    try:
        # Owners register first; dependency then proves actual scheduler authority.
        for module, consumer in CONSUMERS.items():
            state = directory / module
            state.mkdir(mode=0o700)
            config = {
                "module": module,
                "storage_url": rt.url,
                "storage_token": rt.principals[module].token,
                "consumer": consumer,
                "journal_namespace": "synthetic-core-process-" + module,
                "state_dir": str(state),
                "poll_seconds": 0.1,
                "timeout": 35.0,
            }
            if module == "attribution":
                config.update(cosine_threshold=0.9, gps_threshold_m=100.0)
            if module == "extraction":
                # Callers select a configured alias, not a provider model identifier.
                config.update(gateway_url=rt.gateway_url, model="default")
            path = directory / (module + ".json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(config, stream)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(state.stat().st_mode) == 0o700
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "--config", str(path)],
                cwd=ROOT,
                env=child_environment(directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            worker = Harness(module=module, process=process, state=state, config=path)
            workers.append(worker)
            wait_for(
                rt, workers, lambda worker=worker: ready(worker), module + " readiness", timeout=30
            )
            probe = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(path), "--healthcheck"],
                cwd=ROOT,
                env=child_environment(directory),
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert probe.returncode == 0 and probe.stdout == probe.stderr == b""
        assert len({w.process.pid for w in workers}) == 3
        yield workers
    finally:
        # Send SIGTERM to every child before joining; never touch unrelated PIDs.
        for worker in workers:
            if worker.process.poll() is None:
                worker.process.terminate()
        for worker in workers:
            try:
                out, err = worker.process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                worker.process.kill()
                out, err = worker.process.communicate(timeout=10)
                failures.append((worker.module, "forced cleanup required"))
            if worker.process.returncode != 0 or out or err:
                failures.append((worker.module, "unclean exit", worker.process.returncode))
            health = worker.state / "health.json"
            if not health.exists() or json.loads(health.read_text())["status"] != "stopped":
                failures.append((worker.module, "missing stopped health"))
        assert not failures, failures


def command(rt, workers, module, value):
    fid = submit(rt, module, value)

    def succeeded():
        status = api_status(rt, fid)
        assert status["status"] not in {"failed", "blocked", "rejected"}, status
        return status["status"] == "succeeded"

    wait_for(rt, workers, succeeded, module + " feedback")
    status = api_status(rt, fid)
    assert status["completion_source"] == (
        "attribution_feedback_result" if module == "attribution" else "extraction_run"
    )
    return fid


def steps(rt):
    page = rt.clients["dependency"].query(
        Query("propagation_record", where={"schema_tag": SCHEMA, "record_kind": "step"})
    )
    assert page.next_cursor is None, "smoke must not silently truncate scheduler evidence"
    return [Step.model_validate(item.payload) for item in page.items]


def test_public_initialization_and_three_real_worker_processes(core_http_runtime):
    rt = core_http_runtime
    assert rt.scheduler is None and not hasattr(rt, "attribution")
    assert not hasattr(rt, "extraction")
    with worker_processes(rt) as workers:
        state = rt.clients["dependency"].scheduler_state()
        assert state["coverage_complete"] is True
        for module in ("attribution", "extraction"):
            assert state["owners"][module]["enabled"] is True
            assert state["owners"][module]["can_receive"] is True
            assert state["owners"][module]["online"] is None
        # Reverify the same bundle against the actual registered consumers.
        assert init_core_identities(rt.dsn, rt.identity_output) is False
        source = SelectedSource({"cap-1": evidence_json()})
        importer = Runner(
            source=source,
            storage=rt.clients["adapter"],
            locator=FakeLocator(),
            config=AdapterConfig(),
        )
        assert isinstance(importer.run_once(capture_ids=["cap-1"]).outcomes[0], Ingested)
        wait_for(
            rt, workers, lambda: len(rows(rt, "attribution")) == 3, "synthetic raw attribution"
        )
        assert {kind: rt.clients["adapter"].count(Query(kind)) for kind in RAW_TYPES} == {
            "audio_ref": 1,
            "transcript_segment": 3,
            "local_speaker": 3,
            "voiceprint": 2,
            "ambient_sound": 1,
            "gps_point": 1,
            "phone_info": 2,
        }
        attrs = rows(rt, "attribution")
        original, peer = [a for a in attrs if a.payload["target_type"] == "person"]
        assert len(rows(rt, "person")) == 2 and len(rows(rt, "place")) == 1
        rt.provider.subject = original.payload["subject_id"]
        for attr, name in ((original, "Synthetic A"), (peer, "Synthetic B")):
            command(
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
            command(
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
        extract_root = command(rt, workers, "extraction", {"kind": "extract", "interval": INTERVAL})
        [activity] = rows(rt, "activity")
        [selection] = rows(rt, "rule_selection")
        artifacts = rows(rt, "artifact")
        assert len(artifacts) == 2
        owner = next(a for a in artifacts if a.payload["body_md"] == "Synthetic owner: Synthetic A")
        fixed = next(a for a in artifacts if a.payload["body_md"] == "Synthetic unchanged note.")
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
        response = rt.http.get("/v1/records/artifact")
        assert response.status_code == 200
        assert {r["payload"]["body_md"] for r in response.json()["items"]} == {
            "Synthetic owner: Synthetic A",
            "Synthetic unchanged note.",
        }
        [suggestion] = rows(rt, "action_suggestion")
        root = command(
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
        expected = {owner.id: "artifact", fixed.id: "artifact", selection.id: "rule_selection"}

        def completed():
            requests = requests_for(rt, root)
            assert len(requests) <= 3
            if len(requests) != 3:
                return False
            assert {r.target_id: r.target_type for r in requests} == expected
            if any(
                rt.clients["dependency"].get_reevaluation_result(r.event_id) is None
                for r in requests
            ):
                return False
            finished = [
                s for s in steps(rt) if s.request_event_id in {r.event_id for r in requests}
            ]
            assert not any(s.state == "blocked" for s in finished), finished
            return len(finished) == 3 and all(s.state == "done" for s in finished)

        wait_for(rt, workers, completed, "dependency requests and authoritative extraction Finish")
        requests = requests_for(rt, root)
        outcomes = {}
        for request in requests:
            finish = rt.clients["dependency"].get_reevaluation_result(request.event_id)
            assert finish is not None
            assert finish.root_feedback_id == root and finish.depth == request.depth
            assert finish.request_event_id == request.event_id
            assert rt.clients["extraction"].is_processed(COMPLETION_CONSUMER, request.event_id)
            outcomes[request.target_id] = finish.outcome
        assert outcomes == {owner.id: "changed", fixed.id: "unchanged", selection.id: "unchanged"}
        current = rt.clients["extraction"].get(owner.id)
        assert isinstance(current, SemanticRecord) and current.version == owner.version + 1
        assert current.payload["body_md"] == "Synthetic owner: Synthetic B"
        assert rt.clients["extraction"].get_version(owner.id, owner.version) == owner
        assert rt.clients["extraction"].get(fixed.id) == fixed
        assert rows(rt, "activity") == [activity] and rows(rt, "rule_selection") == [selection]
        corrected = rt.clients["attribution"].get(original.id)
        assert corrected is not None and corrected.payload["user_protected"] is True
        assert corrected.payload["target_id"] == peer.payload["target_id"]
        retired = rt.clients["extraction"].get(suggestion.id)
        assert isinstance(retired, SemanticRecord) and retired.superseded
        [replacement] = rows(rt, "action_suggestion")
        assert replacement.id != suggestion.id and replacement.payload["state"] == "pending"
        assert replacement.payload["artifact_version"] == current.version

        for target in expected:
            deps = rt.clients["dependency"].dependencies_of(target)
            assert deps
            stale = rt.clients["dependency"].stale_dependencies([d.depends_on.id for d in deps])
            assert not any(d.dependent.id == target for d in stale)
        timeline = rt.http.get("/v1/timeline")
        assert timeline.status_code == 200
        assert [r["id"] for r in timeline.json()["items"]] == [activity.id]
        visible = rt.http.get("/v1/records/artifact")
        assert visible.status_code == 200
        assert {r["payload"]["body_md"] for r in visible.json()["items"]} == {
            "Synthetic owner: Synthetic B",
            "Synthetic unchanged note.",
        }
        calls = [c for c in rt.provider.calls if c.root_feedback_id == root]
        assert len(calls) == 3
        assert {c.request_event_id for c in calls} == {r.event_id for r in requests}
        by_request = {r.event_id: r for r in requests}
        audits = rt.clients["llm_gateway"].query(Query("llm_call"))
        assert audits.next_cursor is None
        assert {r.id for r in audits.items} == {c.call_id for c in rt.provider.calls}
        assert len(audits.items) == len(rt.provider.calls) == 7
        assert sum(c.purpose == "segmentation" for c in rt.provider.calls) == 1
        for call in rt.provider.calls:
            record = next(r for r in audits.items if r.id == call.call_id)
            audit = LLMCallRecord.model_validate(record.payload)
            assert record.writer == "llm_gateway" and audit.status == "success"
            assert audit.root_feedback_id == call.root_feedback_id
            assert audit.depth == call.depth
            assert audit.caller_module == "extraction" and audit.purpose == call.purpose
            assert audit.model == "synthetic/core-d1"
            assert call.run_id is not None
            run = rt.clients["extraction"].get_system_record(call.run_id)
            assert run is not None and run.payload["status"] == "succeeded"
            intents = [i for i in run.payload["gateway_calls"] if i["call_id"] == call.call_id]
            assert len(intents) == 1 and intents[0]["state"] == "returned"
            assert intents[0]["purpose"] == audit.purpose
            assert audit.request_event_id == call.request_event_id
            assert audit.usage is None
            assert "request_text" not in record.payload and "response_text" not in record.payload
            if call.root_feedback_id == root:
                request = by_request[call.request_event_id]
                assert call.purpose == (
                    "selection" if request.target_type == "rule_selection" else "reevaluation"
                )
                traces = list(call.additional_causes)
                primary = TracedCause(
                    cause=request.cause, root_feedback_id=root, depth=request.depth
                )
                if primary not in traces:
                    traces.append(primary)
                assert audit.additional_causes == tuple(traces)
                assert all(t in audit.additional_causes for t in request.additional_causes)
            else:
                assert call.root_feedback_id == extract_root and call.request_event_id is None
        assert api_status(rt, extract_root)["status"] == "succeeded"
        assert api_status(rt, root)["status"] == "succeeded"
        for module, consumer in CONSUMERS.items():
            assert rt.clients[module].deadletters(consumer) == []
        assert_alive(workers)
        assert not hasattr(rt, "extraction") and rt.scheduler is None
    assert all(w.process.returncode == 0 for w in workers)
    print(
        "WORKER_PROCESS_OBSERVATION "
        + json.dumps(
            {
                "pids": {w.module: w.process.pid for w in workers},
                "exit_codes": {w.module: w.process.returncode for w in workers},
                "workers_independent_processes": True,
                "http_in_pytest_process": True,
                "postgres_nats_temporary_containers": True,
                "model": "synthetic_provider_only",
                "authoritative_llm_calls": len(audits.items),
                "completed_requests": len(requests),
                "outcomes": outcomes,
                "external_actions_executed": False,
                "outbox_events": len(events(rt)),
            },
            sort_keys=True,
        )
    )
