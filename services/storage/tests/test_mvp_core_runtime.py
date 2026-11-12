"""D1 core loop over real authenticated storage/API/gateway HTTP + isolated PG/NATS.

Only Evidence/embeddings/audio locator and the model provider are synthetic.
Actual business owners, GatewayClient, scheduler, relay, grants, journals and
Finish authority are exercised. No external model/calendar, ear-dev, default
registry mutation, production data, OS crash or full-day acceptance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import socket
import threading
import time
from contextlib import ExitStack, contextmanager, suppress
from typing import Any

import httpx
import pytest
import uvicorn
from ear_adapter import AdapterConfig, AlreadyIngested, Ingested, Runner
from ear_adapter.ear_api import Embedding
from ear_adapter.testing import FakeLocator, FakeSource, embedding_json, evidence_json
from ear_api.app import create_app as api_app
from ear_api.config import Settings
from ear_attribution import AttributionService, MatchConfig
from ear_attribution import CommitJournal as AttributionJournal
from ear_contracts import (
    Cause,
    LLMCallRecord,
    ReevaluationRequested,
    SemanticRecord,
    TracedCause,
    new_id,
)
from ear_dependency import CommitJournal as DependencyJournal
from ear_dependency import RemoteDependencyService
from ear_dependency.domain import Step
from ear_extraction.domain import Generated, Segmentation, Selection
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import COMPLETION_CONSUMER, CONSUMER, ExtractionService
from ear_llm_gateway import Gateway, GatewayClient, ProviderResult
from ear_llm_gateway.http import create_app as gateway_app
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.models import CallRequest
from ear_llm_gateway.output_json import OutputSchema
from ear_storage import PostgresStorage, migrate
from ear_storage.core_deployment import bootstrap_core, core_mvp_registry, init_core_identities
from ear_storage.http import create_app as storage_app
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.service import StorageService
from ear_storage_client import Context, PublishReevaluation, Query
from ear_storage_client.remote import RemoteStorageClient
from nats.js.errors import NotFoundError

from .test_api_gateway_integration import Harness
from .test_relay import reader

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
RAW_TYPES = {
    "audio_ref",
    "transcript_segment",
    "local_speaker",
    "voiceprint",
    "ambient_sound",
    "gps_point",
    "phone_info",
}
INTERVAL = {"start": "2026-10-26T01:55:38Z", "end": "2026-10-26T02:25:38Z"}
# Fixture lifecycle, NOT a request timeout or worker completion deadline.
# SDK/gateway/HTTPX pools expire idle connections after 5s; keep the server
# alive beyond that window. Short idle-close behavior is tested explicitly below.
LOOPBACK_KEEPALIVE_SECONDS = 30


@contextmanager
def loopback(app, *, timeout_keep_alive: int = LOOPBACK_KEEPALIVE_SECONDS):
    """Core runtime listener with the same TCP semantics as a normal TCP server.

    The shared helper's prebound proto=0 socket bypasses asyncio's NODELAY
    setup. h11 sends headers and body separately; Nagle then adds a delayed-ACK
    interval to virtually every reused connection. Do not compensate with a
    larger business deadline, weaker coverage checks, or hidden HTTP retries.

    Normal scenarios exercise pooling with client-first idle expiration. This
    finite server lifetime does not guarantee against arbitrary disconnects; the
    separate fault-injection test still requires visible errors and zero retries.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    runner = uvicorn.Server(
        uvicorn.Config(
            app, log_level="critical", access_log=False, timeout_keep_alive=timeout_keep_alive
        )
    )
    thread = threading.Thread(target=runner.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not runner.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runner.started, "core loopback startup failed"
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        runner.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "core loopback cleanup failed"


class SelectedSource(FakeSource):
    def list_captures(self):
        raise AssertionError("explicit synthetic capture must not enumerate source")

    def read_embedding(self, resolution_id: str) -> Embedding:
        self.embedding_reads += 1
        data = embedding_json(resolution_id)
        data["values"] = [1.0, 0.0, 0.0] if resolution_id.endswith("-1") else [0.0, 1.0, 0.0]
        return Embedding.model_validate(data)


class SyntheticProvider:
    """Typed fixture at the real gateway's provider boundary, NOT a fake gateway."""

    def __init__(self):
        self.calls: list[CallRequest] = []
        self.subject: str | None = None
        self.client: RemoteStorageClient | None = None

    def complete(self, call: CallRequest, *, model: str, timeout: float) -> ProviderResult:
        assert model == "synthetic/core-d1" and timeout > 0
        assert call.module == "extraction" and call.system == SYSTEM
        assert self.client is not None and call.run_id is not None
        run = self.client.get_system_record(call.run_id)
        assert run is not None
        intent = run.payload["gateway_calls"][-1]
        fingerprint = hashlib.sha256(
            json.dumps(
                call.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        assert intent == {
            "call_id": call.call_id,
            "purpose": call.purpose,
            "state": "sending",
            "request_fingerprint": fingerprint,
        }
        self.calls.append(call)
        data = call.data
        if call.purpose == "segmentation":
            assert not data.get("attributions") and not data.get("entities")
            result = Segmentation.model_validate(
                {
                    "activities": [
                        {
                            **data["interval"],
                            "title_md": "Synthetic meeting",
                            "evidence_ids": [data["evidence"][0]["id"]],
                        }
                    ]
                }
            ).model_dump(mode="json")
        elif call.purpose == "selection":
            result = Selection(rule_ids=[r["id"] for r in data["rules"]]).model_dump(mode="json")
        else:
            assert call.purpose in {"generation", "reevaluation"}
            selected = next(
                a for a in data["attributions"] if a["payload"]["subject_id"] == self.subject
            )
            person = next(
                e for e in data["entities"] if e["id"] == selected["payload"]["target_id"]
            )
            assert all(set(e["payload"]) <= {"name"} for e in data["entities"])
            fixed = data["rule"]["payload"]["instruction"] == "fixed"
            body = (
                "Synthetic unchanged note."
                if fixed
                else "Synthetic owner: " + person["payload"]["name"]
            )
            output: dict[str, Any] = {"body_md": body, "evidence_ids": [data["evidence"][0]["id"]]}
            if call.purpose == "reevaluation" and body == data["old_artifact"]["body_md"]:
                output = {"unchanged": True}
            elif not fixed:
                output["action"] = {
                    "kind": "reminder",
                    "title": body,
                    "due_at": "2026-11-06T09:00:00Z",
                }
            result = Generated.model_validate(output).model_dump(mode="json")
        # The actual gateway validates again; no schema/purpose/context override.
        OutputSchema(call.output_schema).validate(result)
        return ProviderResult(result)


@pytest.fixture
def core_http_runtime(pg_dsn, nats_server, tmp_path):
    """Public initialization on a NEW database; no in-process business consumers.

    Management is not needed here. If added, init-identities must run BEFORE
    init_core_identities; never retrofit this profile onto a legacy database.
    """
    registry = core_mvp_registry()
    migrate(pg_dsn)
    assert bootstrap_core(pg_dsn) is True
    output = tmp_path / "core-identities" / "identities.json"
    assert init_core_identities(pg_dsn, output) is True
    credentials = json.loads(output.read_text())["credentials"]
    principals = {
        ("api" if role == "api_user" else role): Harness(**entry)
        for role, entry in credentials.items()
    }
    assert set(principals) == {
        "adapter",
        "attribution",
        "extraction",
        "dependency",
        "api",
        "llm_gateway",
    }
    assert bootstrap_core(pg_dsn) is False
    assert init_core_identities(pg_dsn, output) is False
    assert registry.get("rule_selection").supports_reevaluation
    suffix = new_id().replace("-", "")
    cfg = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="EAR_CORE_" + suffix,
        prefix="ear_core_" + suffix,
        timeout=2,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    try:
        with ExitStack() as stack:
            url = stack.enter_context(
                loopback(storage_app(StorageService(pg_dsn, relay_config=cfg)))
            )
            clients = {
                m: stack.enter_context(RemoteStorageClient(p.token, url))
                for m, p in principals.items()
            }
            provider = SyntheticProvider()
            provider.client = clients["extraction"]
            gateway_journal = Journal(str(tmp_path / "gateway.sqlite3"))
            stack.callback(gateway_journal.close)
            gateway = Gateway(
                callers=lambda token: RemoteStorageClient(token, url),
                writer=clients["llm_gateway"],
                provider=provider,
                journal=gateway_journal,
                aliases={"default": "synthetic/core-d1"},
            )
            assert gateway.verifier is None
            gateway_url = stack.enter_context(loopback(gateway_app(gateway)))
            model = stack.enter_context(
                GatewayClient(gateway_url, principals["extraction"].token, system=SYSTEM)
            )
            api_token = secrets.token_urlsafe(32)
            settings = Settings.model_validate(
                {
                    "storage_url": url,
                    "bindings": [
                        {
                            "subject": "synthetic_d1",
                            "role": "user",
                            "token": api_token,
                            "storage_token": principals["api"].token,
                        }
                    ],
                }
            )
            api_url = stack.enter_context(loopback(api_app(settings)))
            http = stack.enter_context(
                httpx.Client(
                    base_url=api_url,
                    trust_env=False,
                    timeout=15,
                    headers={"Authorization": "Bearer " + api_token},
                )
            )
            rt = Harness(
                dsn=pg_dsn,
                cfg=cfg,
                store=PostgresStorage(pg_dsn),
                clients=clients,
                principals=principals,
                url=url,
                gateway_url=gateway_url,
                identity_output=output,
                stack=stack,
                model=model,
                provider=provider,
                http=http,
                directory=tmp_path,
                scheduler=None,
                attr_journal=None,
                dep_journal=None,
            )
            yield rt
    finally:

        async def cleanup():
            async with reader(cfg) as js:
                with suppress(NotFoundError):
                    await js.delete_stream(cfg.stream)

        asyncio.run(cleanup())


@pytest.fixture
def core_runtime(core_http_runtime):
    rt = core_http_runtime
    start_attribution(rt)
    rt.extraction = ExtractionService(rt.clients["extraction"], rt.model)
    rt.extraction.subscribe()
    return rt


def start_attribution(rt):
    journal = AttributionJournal(
        rt.directory / "attribution", namespace="synthetic-core-attribution"
    )
    rt.stack.callback(journal.close)
    rt.attr_journal = journal
    rt.attribution = AttributionService.business(
        rt.clients["attribution"],
        config=MatchConfig(cosine_threshold=0.9, gps_threshold_m=100),
        journal=journal,
    )
    rt.attribution.subscribe()


def start_scheduler(rt):
    journal = DependencyJournal(rt.directory / "dependency", namespace="synthetic-core-dependency")
    rt.stack.callback(journal.close)
    rt.dep_journal = journal
    rt.scheduler = RemoteDependencyService(rt.clients["dependency"], journal=journal)
    rt.scheduler.subscribe()
    state = rt.clients["dependency"].scheduler_state()
    assert state["coverage_complete"] is True
    for module in ("attribution", "extraction"):
        assert state["owners"][module]["enabled"] is True
        assert state["owners"][module]["can_receive"] is True
        assert state["owners"][module]["online"] is None


def relay(rt):
    return asyncio.run(run_relay(RelayStore(rt.dsn, rt.cfg), once=True))


def pump(rt, rounds=2):
    for _ in range(rounds):
        relay(rt)
        rt.clients["attribution"].run_until_idle()
        rt.clients["extraction"].run_until_idle()
        rt.extraction.drain()
        if rt.scheduler is not None:
            rt.clients["dependency"].run_until_idle()
            rt.scheduler.tick()


def rows(rt, kind):
    values = rt.clients["extraction"].query(Query(kind)).items
    assert all(isinstance(r, SemanticRecord) for r in values)
    return list(values)


def api_status(rt, fid):
    response = rt.http.get(f"/v1/feedback/{fid}/status")
    assert response.status_code == 200, response.text
    return response.json()


def submit(rt, module, command):
    body = {"request_id": new_id(), "module": module, "command": command}
    response = rt.http.post("/v1/feedback", json=body)
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["status"] == "accepted"
    fid = accepted["feedback_id"]
    pending = api_status(rt, fid)
    assert pending["status"] == "pending" and pending["completion_source"] is None
    assert rt.http.post("/v1/feedback", json=body).json() == accepted
    return fid


def command(rt, module, value):
    fid = submit(rt, module, value)
    pump(rt)
    status = api_status(rt, fid)
    assert status["status"] == "succeeded", (status, [r.payload for r in rt.extraction.runs()])
    assert status["completion_source"] == (
        "attribution_feedback_result" if module == "attribution" else "extraction_run"
    )
    return fid


def events(rt):
    return [event for _, batch in rt.store.outbox() for event in batch]


def requests_for(rt, root):
    return [
        e for e in events(rt) if isinstance(e, ReevaluationRequested) and e.root_feedback_id == root
    ]


def verify_round(rt, root, expected, *, requests=None):
    if requests is None:
        requests = requests_for(rt, root)
    assert {e.target_id: e.target_type for e in requests} == expected
    assert len(requests) == len(expected)  # one request per target, not legacy fixed 2
    outcomes = {}
    for request in requests:
        finish = rt.clients["dependency"].get_reevaluation_result(request.event_id)
        assert finish is not None, [s for s in rt.scheduler.records() if isinstance(s, Step)]
        outcomes[request.target_id] = finish.outcome
        assert finish.root_feedback_id == root and finish.depth == request.depth
        assert finish.request_event_id == request.event_id
        assert rt.clients["extraction"].is_processed(COMPLETION_CONSUMER, request.event_id)
    steps = [
        s
        for s in rt.scheduler.records()
        if isinstance(s, Step) and s.request_event_id in {r.event_id for r in requests}
    ]
    assert len(steps) == len(expected) and all(s.state == "done" for s in steps)
    calls = [c for c in rt.provider.calls if c.root_feedback_id == root]
    assert {c.request_event_id for c in calls} == {e.event_id for e in requests}
    assert len(calls) == len(expected) and len({c.call_id for c in calls}) == len(calls)
    call_by_request = {c.request_event_id: c for c in calls}
    for request in requests:
        call = call_by_request[request.event_id]
        assert call.purpose == (
            "selection" if request.target_type == "rule_selection" else "reevaluation"
        )
        assert call.depth == request.depth
        record = rt.clients["llm_gateway"].get_system_record(call.call_id)
        assert (
            record is not None and record.data_type == "llm_call" and record.writer == "llm_gateway"
        )
        audit = LLMCallRecord.model_validate(record.payload)
        assert audit.status == "success" and audit.request_event_id == request.event_id
        assert audit.root_feedback_id == root and audit.depth == request.depth
        traces = list(call.additional_causes)
        primary = TracedCause(cause=request.cause, root_feedback_id=root, depth=request.depth)
        if primary not in traces:
            traces.append(primary)
        assert audit.additional_causes == tuple(traces)
        assert all(trace in audit.additional_causes for trace in request.additional_causes)
        assert audit.usage is None
        assert "request_text" not in record.payload and "response_text" not in record.payload
    for target in expected:
        deps = rt.clients["dependency"].dependencies_of(target)
        assert deps
        stale = rt.clients["dependency"].stale_dependencies([d.depends_on.id for d in deps])
        assert not any(d.dependent.id == target for d in stale)
    return outcomes, requests


@pytest.mark.parametrize("restart", [False, True], ids=["main", "restart-and-duplicate"])
def test_d1_core_loop_real_http_gateway_and_finish(core_runtime, restart):
    rt = core_runtime
    evidence = evidence_json()
    for item in evidence["evidence"]:
        if item["ref"]["type"] == "speech_segment":
            item["payload"]["text"] = "Synthetic meeting task due 2026-11-06 09:00 UTC."
    source = SelectedSource({"cap-1": evidence})
    importer = Runner(
        source=source, storage=rt.clients["adapter"], locator=FakeLocator(), config=AdapterConfig()
    )
    assert isinstance(importer.run_once(capture_ids=["cap-1"]).outcomes[0], Ingested)
    pump(rt)
    raw_counts = {name: rt.clients["adapter"].count(Query(name)) for name in RAW_TYPES}
    assert raw_counts == {
        "audio_ref": 1,
        "transcript_segment": 3,
        "local_speaker": 3,
        "voiceprint": 2,
        "ambient_sound": 1,
        "gps_point": 1,
        "phone_info": 2,
    }
    assert len(rows(rt, "person")) == 2 and len(rows(rt, "place")) == 1
    attrs = rows(rt, "attribution")
    assert len(attrs) == 3
    count = len(events(rt))
    replay_importer = Runner(
        source=source, storage=rt.clients["adapter"], locator=FakeLocator(), config=AdapterConfig()
    )
    assert isinstance(replay_importer.run_once(capture_ids=["cap-1"]).outcomes[0], AlreadyIngested)
    assert len(events(rt)) == count
    persons = [a for a in attrs if a.payload["target_type"] == "person"]
    original, peer = persons
    rt.provider.subject = original.payload["subject_id"]
    for attr, name in ((original, "Synthetic A"), (peer, "Synthetic B")):
        command(
            rt,
            "attribution",
            {"kind": "rename", "target_id": attr.payload["target_id"], "name": name},
        )
    for instruction in ("owner", "fixed"):
        command(
            rt,
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
    command(rt, "extraction", {"kind": "extract", "interval": INTERVAL})
    [activity] = rows(rt, "activity")
    [selection] = rows(rt, "rule_selection")
    before = {a.id: a for a in rows(rt, "artifact")}
    assert len(before) == 2
    owner = next(
        a for a in before.values() if a.payload["body_md"] == "Synthetic owner: Synthetic A"
    )
    fixed = next(a for a in before.values() if a.payload["body_md"] == "Synthetic unchanged note.")
    assert set(selection.payload["rule_ids"]) == {a.payload["rule_id"] for a in before.values()}
    [old_suggestion] = rows(rt, "action_suggestion")
    for path, ids in (
        ("/v1/timeline", {activity.id}),
        ("/v1/records/artifact", set(before)),
        ("/v1/records/action_suggestion", {old_suggestion.id}),
    ):
        response = rt.http.get(path)
        assert response.status_code == 200, response.text
        assert {r["id"] for r in response.json()["items"]} == ids
    for artifact in before.values():
        deps = rt.clients["extraction"].dependencies_of(artifact.id)
        assert {d.depends_on.id for d in deps if d.depends_on.data_type == "attribution"} == {
            a.id for a in attrs
        }
        assert all(d.depends_on.data_type != "rule_selection" for d in deps)
        assert all(
            d.fields == ("name",) for d in deps if d.depends_on.data_type in {"person", "place"}
        )
    assert rt.clients["extraction"].dependencies_of(activity.id) == []
    start_scheduler(rt)
    root = submit(
        rt,
        "attribution",
        {
            "kind": "correct_attribution",
            "subject_id": original.payload["subject_id"],
            "previous_target_id": original.payload["target_id"],
            "new_target_id": peer.payload["target_id"],
            "expected_version": original.version,
        },
    )
    # Pause with real requests in the persistent outbox, before extraction consumes them.
    relay(rt)
    rt.clients["attribution"].run_until_idle()
    status = api_status(rt, root)
    assert (
        status["status"] == "succeeded"
        and status["completion_source"] == "attribution_feedback_result"
    )
    relay(rt)
    rt.clients["dependency"].run_until_idle()
    rt.scheduler.tick()
    expected = {**{ident: "artifact" for ident in before}, selection.id: "rule_selection"}
    assert {e.target_id for e in requests_for(rt, root)} == set(expected)
    assert not any(c.root_feedback_id == root for c in rt.provider.calls)
    if restart:
        rt.attr_journal.close()
        rt.dep_journal.close()
        for module in ("attribution", "dependency", "extraction"):
            rt.clients[module].close()
            rt.clients[module] = rt.stack.enter_context(
                RemoteStorageClient(rt.principals[module].token, rt.url)
            )
        rt.provider.client = rt.clients["extraction"]
        start_attribution(rt)
        start_scheduler(rt)
        rt.extraction = ExtractionService(rt.clients["extraction"], rt.model)
        rt.extraction.subscribe()
    pump(rt, rounds=4)
    outcomes, requests = verify_round(rt, root, expected)
    assert outcomes == {owner.id: "changed", fixed.id: "unchanged", selection.id: "unchanged"}
    after = {a.id: a for a in rows(rt, "artifact")}
    assert after[fixed.id] == fixed
    assert after[owner.id].version == owner.version + 1
    assert after[owner.id].payload["body_md"] == "Synthetic owner: Synthetic B"
    assert rt.clients["extraction"].get_version(owner.id, 1) == owner
    assert rows(rt, "activity") == [activity] and rows(rt, "rule_selection") == [selection]
    retired = rt.clients["extraction"].get(old_suggestion.id)
    assert isinstance(retired, SemanticRecord) and retired.superseded
    [replacement] = rows(rt, "action_suggestion")
    assert replacement.id != old_suggestion.id and replacement.payload["state"] == "pending"
    assert replacement.payload["artifact_version"] == after[owner.id].version
    corrected = rt.clients["attribution"].get(original.id)
    assert corrected is not None and corrected.payload["user_protected"] is True
    assert corrected.payload["target_id"] == peer.payload["target_id"]

    # Reviewed suggestions survive a later changed artifact; no external action runs.
    command(
        rt,
        "extraction",
        {
            "kind": "review_suggestion",
            "target_id": replacement.id,
            "expected_version": replacement.version,
            "decision": "approved",
        },
    )
    approved = rt.clients["extraction"].get(replacement.id)
    assert approved is not None and approved.payload["state"] == "approved"
    renamed = command(
        rt,
        "attribution",
        {"kind": "rename", "target_id": peer.payload["target_id"], "name": "Synthetic B renamed"},
    )
    pump(rt)
    rename_requests = requests_for(rt, renamed)
    unexpected_rename = [e for e in rename_requests if e.target_id not in expected]
    # Keep the strict name-only gate below. Defer it only so this real run also
    # verifies reviewed/protected state, journal reopening and duplicate safety.
    next_outcomes, next_requests = verify_round(
        rt,
        renamed,
        expected,
        requests=[e for e in rename_requests if e.target_id in expected],
    )
    assert next_outcomes == outcomes
    assert rt.clients["extraction"].get(replacement.id) == approved
    assert rt.clients["extraction"].get(original.id) == corrected
    assert rows(rt, "activity") == [activity]

    # Remove the matching reference: an unprotected rematch would now choose a
    # different/new person. Explicit user protection must still win.
    removed = command(
        rt,
        "attribution",
        {
            "kind": "remove_reference",
            "subject_id": original.payload["subject_id"],
            "target_id": peer.payload["target_id"],
            "rejudge_history": False,
        },
    )
    person = rt.clients["attribution"].get(peer.payload["target_id"])
    assert person is not None and original.payload["subject_id"] not in person.payload["references"]
    assert requests_for(rt, removed) == []  # reference edit's history gate remains in force
    # Authorized explicit recheck through real broker, never fabricated Finish.
    protected_request = (
        rt.clients["dependency"]
        .write(
            [
                PublishReevaluation(
                    "attribution",
                    original.id,
                    Cause(
                        data_type="person",
                        data_id=person.id,
                        old_version=None,
                        new_version=person.version,
                    ),
                    "Synthetic protected attribution recheck",
                )
            ],
            Context.feedback(removed).deeper(),
        )
        .check()
        .events[0]
    )
    assert isinstance(protected_request, ReevaluationRequested)
    pump(rt)
    finish = rt.clients["dependency"].get_reevaluation_result(protected_request.event_id)
    assert finish is not None and finish.outcome == "unchanged"
    assert finish.root_feedback_id == removed
    assert len(rows(rt, "person")) == 2
    assert rt.clients["attribution"].get(original.id) == corrected
    assert rt.clients["attribution"].is_processed("attribution", protected_request.event_id)
    calls_before, events_before = len(rt.provider.calls), len(events(rt))
    for request in [*requests, *next_requests]:
        rt.extraction.handle_event(
            request
        )  # actual duplicate committed event, not fabricated request
    rt.attribution.handle(protected_request)
    rt.extraction.drain()
    rt.scheduler.tick()
    pump(rt)
    assert len(rt.provider.calls) == calls_before and len(events(rt)) == events_before
    assert all(r.payload["status"] == "succeeded" for r in rt.extraction.runs())
    assert not rt.attr_journal.pending_ids() and not rt.dep_journal.pending_ids()
    all_audit = rt.clients["llm_gateway"].query(Query("llm_call")).items
    assert {r.id for r in all_audit} == {c.call_id for c in rt.provider.calls}
    assert sum(c.purpose == "segmentation" for c in rt.provider.calls) == 1
    for module, consumer in (
        ("attribution", "attribution"),
        ("extraction", CONSUMER),
        ("dependency", "dependency.changes"),
    ):
        assert rt.clients[module].deadletters(consumer) == []
    assert all(
        s.state in {"done", "unaffected"} for s in rt.scheduler.records() if isinstance(s, Step)
    )

    print(
        "CORE_OBSERVATION "
        + json.dumps(
            {
                "restart": restart,
                "raw_counts": raw_counts,
                "gateway_http": True,
                "synthetic_provider_calls": len(rt.provider.calls),
                "authoritative_llm_calls": len(all_audit),
                "first_round_outcomes": outcomes,
                "second_round_outcomes": next_outcomes,
                "pending_replaced": True,
                "approved_preserved": True,
                "protected_attribution_after_reference_removal": finish.outcome,
                "duplicates_no_extra_calls_or_events": True,
                "unexpected_name_only_requests": [
                    e.model_dump(mode="json") for e in unexpected_rename
                ],
            },
            sort_keys=True,
        )
    )
    # Accepted v1.2: a name-only change MUST NOT resurrect a references-only
    # historical rejudge which an earlier skip_history reference edit suppressed.
    assert unexpected_rename == [], "name-only event retriggered gated reference dependencies"


def test_loopback_accepted_socket_disables_nagle(monkeypatch):
    """No timing threshold: verify actual accepted socket, not a client option.

    A prebound socket with proto=0 bypasses asyncio's TCP_NODELAY setup. Uvicorn
    writes headers/body separately, so every reused connection pays delayed ACK.
    """
    import socket

    from fastapi import FastAPI
    from uvicorn.protocols.http.h11_impl import H11Protocol

    options = []
    original = H11Protocol.connection_made

    def connected(self, transport):
        sock = transport.get_extra_info("socket")
        options.append(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
        return original(self, transport)

    monkeypatch.setattr(H11Protocol, "connection_made", connected)
    app = FastAPI()

    @app.get("/synthetic")
    def metadata():
        return {"synthetic": True}

    with loopback(app) as url, httpx.Client(base_url=url, trust_env=False) as client:
        for _ in range(3):
            assert client.get("/synthetic").json() == {"synthetic": True}
    assert options and set(options) == {1}, options


def test_loopback_keepalive_outlives_sdk_pool(monkeypatch):
    """Check lifecycle policy and pool replacement without sleep/timing thresholds.

    This is a transport-only synthetic endpoint, not storage/auth acceptance.
    Inspect the actual SDK pool and accepted server protocol, then explicitly
    expire the idle pool entry to verify it is replaced rather than reused.
    """
    from fastapi import FastAPI
    from uvicorn.protocols.http.h11_impl import H11Protocol

    protocols = []
    responded = threading.Event()
    original = H11Protocol.on_response_complete

    def completed(self):
        original(self)
        protocols.append(self)
        responded.set()

    monkeypatch.setattr(H11Protocol, "on_response_complete", completed)
    app = FastAPI()

    @app.get("/synthetic")
    def metadata():
        return {"synthetic": True}

    with loopback(app) as url, RemoteStorageClient("s" * 43, url) as client:
        first = client._request("GET", "/synthetic")
        assert first.json() == {"synthetic": True}
        assert responded.wait(5), "server response lifecycle callback missing"
        transport: Any = client._http._transport
        [pooled] = transport._pool.connections
        connection = pooled._connection
        assert connection.is_idle()
        expiry = connection._keepalive_expiry
        assert expiry is not None and expiry > 0
        assert protocols[0].timeout_keep_alive > expiry, (
            "fixture server must outlive the SDK idle pool",
            protocols[0].timeout_keep_alive,
            expiry,
        )
        assert protocols[0].timeout_keep_alive_task is not None
        responded.clear()
        second = client._request("GET", "/synthetic")
        assert second.json() == first.json()
        assert responded.wait(5), "reused response lifecycle callback missing"
        assert second.extensions["network_stream"] is first.extensions["network_stream"]
        assert protocols == [protocols[0], protocols[0]]

        # Deterministic expiration; do not wait five seconds or change a global clock.
        connection._expire_at = float("-inf")
        responded.clear()
        third = client._request("GET", "/synthetic")
        assert third.json() == first.json()
        assert responded.wait(5), "replacement response lifecycle callback missing"
        assert connection.is_closed()
        assert third.extensions["network_stream"] is not first.extensions["network_stream"]
        assert len(protocols) == 3 and protocols[2] is not protocols[0]


@pytest.mark.parametrize("write", [False, True], ids=["read-unavailable", "commit-unknown"])
def test_loopback_close_after_pool_checkout_is_not_retried(monkeypatch, write):
    """Force the close/reuse interleaving on real TCP, not by racing wall clocks.

    Invoke the real server keep-alive handler AFTER httpcore checks out an idle
    connection and BEFORE request headers are sent. This proves error propagation,
    not that the historical eight-step failure had this cause. The deliberately
    short server policy is confined to this fault-injection test.
    """
    from ear_storage_client.remote_errors import CommitUnknown, ServiceUnavailable
    from fastapi import FastAPI
    from httpcore._sync.http11 import HTTP11Connection
    from uvicorn.protocols.http.h11_impl import H11Protocol

    protocols = []
    attempts = []
    received = []
    responded = threading.Event()
    original_complete = H11Protocol.on_response_complete
    original_send = HTTP11Connection._send_request_headers

    def completed(self):
        original_complete(self)
        protocols.append(self)
        # Suspend the timer; fire the same handler at the exact checkout barrier.
        # No sleep, random retry or injected SDK exception is used.
        self.timeout_keep_alive_task.cancel()
        responded.set()

    def send(self, request):
        if request.url.target in {b"/v1/read", b"/v1/batches"}:
            attempts.append(request.url.target)
            [protocol] = protocols
            assert self._request_count == 2, "fault must target the reused connection"
            assert self._network_stream.get_extra_info("client_addr") == protocol.client
            assert protocol.timeout_keep_alive == 1
            closed = threading.Event()
            closing = []

            def close_idle():
                try:
                    protocol.timeout_keep_alive_handler()
                    closing.append(protocol.transport.is_closing())
                finally:
                    closed.set()

            protocol.loop.call_soon_threadsafe(close_idle)
            assert closed.wait(5), "server close callback did not complete"
            assert closing == [True]
        return original_send(self, request)

    monkeypatch.setattr(H11Protocol, "on_response_complete", completed)
    monkeypatch.setattr(HTTP11Connection, "_send_request_headers", send)
    app = FastAPI()

    @app.get("/synthetic")
    def metadata():
        received.append("warmup")
        return {"synthetic": True}

    @app.post("/v1/read")
    @app.post("/v1/batches")
    def unexpected():
        received.append("unexpected")
        return {"unexpected": True}

    with (
        loopback(app, timeout_keep_alive=1) as url,
        RemoteStorageClient("s" * 43, url) as client,
    ):
        assert client._request("GET", "/synthetic").json() == {"synthetic": True}
        assert responded.wait(5), "warmup response lifecycle callback missing"
        request_id = new_id() if write else None
        path = "/v1/batches" if write else "/v1/read"
        expected = CommitUnknown if write else ServiceUnavailable
        with pytest.raises(expected) as failure:
            client._request("POST", path, "{}", request_id=request_id)
        assert isinstance(failure.value.__context__, httpx.TransportError)
        if write:
            assert isinstance(failure.value, CommitUnknown)
            assert failure.value.request_id == request_id
        assert attempts == [path.encode()], "transport must not silently retry"
        assert received == ["warmup"]
        assert len(protocols) == 1, "must not reconnect and conceal the injected close"
        chain = []
        error: BaseException | None = failure.value
        while error is not None:
            chain.append(type(error).__name__)
            error = error.__cause__ or error.__context__
        print(
            "LOOPBACK_CLOSE_OBSERVATION "
            + json.dumps({"path": path, "error_chain": chain, "attempts": len(attempts)})
        )
