"""HTTP boundary with actual extraction owner, synthetic wire storage and adapter."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from ear_api.app import create_app
from ear_api.execution import ExecutionService
from ear_api.execution_journal import ExecutionJournal
from ear_contracts import DataChanged, new_id
from ear_extraction.domain import Action, Generated, Interval
from ear_extraction.orchestration import ExtractionService
from ear_extraction.suggestions import create_suggestion
from ear_storage_client import Context, Query
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import STORAGE_USER, FakeStorage, headers, settings, tcp_server
from .test_execution import ACTION, NoModel, SyntheticAdapter


@pytest.fixture
def http_world(tmp_path):
    fake = FakeStorage()
    owner = fake.storage.client("extraction")
    owner.write(
        create_suggestion(
            new_id(),
            1,
            Generated(action=Action.model_validate(ACTION)),
            Interval.model_validate({"start": ACTION["start"], "end": ACTION["end"]}),
        ),
        Context.system(),
    ).check()
    sid = owner.query(Query("action_suggestion")).items[0].id
    worker = ExtractionService(owner, NoModel())
    adapter = SyntheticAdapter()
    path = tmp_path / "execution.sqlite3"
    with tcp_server(fake.app) as url:
        config = settings(url, execution_action_v2=True, execution_journal=path)

        @contextmanager
        def serve(**kwargs):
            with TestClient(create_app(config, adapter=adapter, **kwargs)) as http:
                http.headers.update(headers())
                yield http

        def drain():
            for event in list(fake.storage.events):
                if isinstance(event, DataChanged) and event.data_type == "feedback":
                    worker.handle_event(event)
            worker.drain()

        yield SimpleNamespace(
            fake=fake,
            owner=owner,
            worker=worker,
            adapter=adapter,
            config=config,
            sid=sid,
            path=path,
            serve=serve,
            drain=drain,
            base=f"/v1/actions/{sid}",
        )


def state(w, http):
    reply = http.get(w.base + "/execution")
    assert reply.status_code == 200, reply.text
    return reply.json()


def versions(w, http):
    value = state(w, http)
    return {k: value[k] for k in ("expected_version", "expected_revision")}


def advance(w, http):
    reply = http.post(w.base + "/pump", json=versions(w, http))
    assert reply.status_code == 202, reply.text
    return reply.json()


def review(w, http):
    result = http.post(
        "/v1/feedback",
        json={
            "request_id": new_id(),
            "module": "extraction",
            "command": {
                "kind": "review_suggestion",
                "target_id": w.sid,
                "expected_version": 1,
                "decision": "approved",
            },
        },
    )
    assert result.status_code == 202, result.text
    return result.json()


def start(w, http):
    review(w, http)
    w.drain()
    result = http.post(w.base + "/prepare", json={"expected_version": 2})
    assert result.status_code == 202, result.text
    w.drain()
    return advance(w, http)


def settle(w, http):
    w.drain()
    return advance(w, http)


def test_opt_in_two_phase_get_is_read_only_and_never_legacy(http_world, monkeypatch):
    w = http_world

    def forbidden(*args, **kwargs):
        pytest.fail("legacy process must never be connected")

    monkeypatch.setattr(ExecutionService, "process", forbidden)
    with w.serve() as http:
        assert state(w, http)["execution_enabled"] is True
        assert http.post(w.base + "/prepare", json={"expected_version": 1}).status_code == 409
        accepted = review(w, http)
        assert accepted["status"] == "accepted"
        assert http.post(w.base + "/prepare", json={"expected_version": 1}).status_code == 409
        w.drain()
        assert http.post(w.base + "/prepare", json={"expected_version": 1}).status_code == 409
        prepared = http.post(w.base + "/prepare", json={"expected_version": 2})
        assert prepared.status_code == 202
        for _ in range(2):
            assert advance(w, http)["feedback_delivery_state"] == "accepted"
        assert not w.adapter.calls and not w.adapter.lookups
        w.drain()
        before = state(w, http)
        assert before["suggestion_state"] == "executing"
        assert state(w, http) == before
        assert not w.adapter.calls and not w.adapter.lookups
        done = advance(w, http)
        assert done["executor_state"] == "succeeded"
        assert done["suggestion_state"] == "executing"
        assert done["feedback_delivery_state"] == "accepted"
        assert settle(w, http)["suggestion_state"] == "executed"
        assert len(w.adapter.calls) == 1
        assert "synthetic_external_ref" not in str(state(w, http))
        assert state(w, http)["automatic_worker_enabled"] is False
    with w.serve() as http:
        assert advance(w, http)["feedback_delivery_state"] == "applied"
        assert len(w.adapter.calls) == 1


@pytest.mark.parametrize("operation", ["verify", "reconcile", "confirm_executed", "retry"])
def test_unknown_recovery_double_versions_and_ack(http_world, operation):
    w = http_world
    w.adapter.mode = "timeout"
    with w.serve() as http:
        start(w, http)
        done = settle(w, http)
        assert done["suggestion_state"] == "unknown"
        assert done["recovery"]["requires_user_verification"]
        args = {"operation": operation, **versions(w, http)}
        ack = {"confirm_executed": "acknowledge_executed", "retry": "acknowledge_duplicate_risk"}
        if operation in ack:
            assert http.post(w.base + "/recovery", json=args).status_code == 409
            args[ack[operation]] = True
        lookups = len(w.adapter.lookups)
        for key in ("expected_version", "expected_revision"):
            bad = {**args, key: args[key] - 1}
            assert http.post(w.base + "/recovery", json=bad).status_code == 409
        assert len(w.adapter.lookups) == lookups
        if operation in {"verify", "reconcile"}:
            key = w.adapter.calls[0][0]
            # Only adapter evidence, never HTTP-supplied refs/receipts.
            digest = w.owner.get(w.sid).payload["execution"]["action_hash"]
            w.adapter.results[key] = w.adapter.result(key, digest, "succeeded")
        reply = http.post(w.base + "/recovery", json=args)
        assert reply.status_code == 202, reply.text
        assert len(w.adapter.calls) == 1
        if operation == "retry":
            assert advance(w, http)["feedback_delivery_state"] == "accepted"
            assert len(w.adapter.calls) == 1
            w.adapter.mode = "success"
            settle(w, http)
            assert len(w.adapter.calls) == 2
            assert w.adapter.calls[0][0] == w.adapter.calls[1][0]
        final = settle(w, http)
        assert final["suggestion_state"] == "executed"
        if operation == "confirm_executed":
            assert final["recovery"]["confirmation_source"] == "user"
            assert w.adapter.results == {}


@pytest.mark.parametrize("route", ["prepare", "pump", "recovery"])
@pytest.mark.parametrize(
    "key", ["url", "provider", "key", "action", "outcome", "receipt", "external_ref"]
)
def test_no_request_injected_authority_or_external_evidence(http_world, route, key):
    w = http_world
    payload: dict[str, Any] = {"expected_version": 1}
    if route != "prepare":
        payload["expected_revision"] = 1
    if route == "recovery":
        payload["operation"] = "verify"
    with w.serve() as http:
        reply = http.post(w.base + "/" + route, json={**payload, key: "PRIVATE"})
        assert reply.status_code == 422 and reply.json() == {"code": "invalid_input"}
        assert not w.adapter.calls and not w.adapter.lookups


@pytest.mark.parametrize("bad", [None, True, "2", 2.0, 0, -1, 2147483648])
@pytest.mark.parametrize("field", ["expected_version", "expected_revision"])
def test_strict_bounded_versions(http_world, bad, field):
    w = http_world
    with w.serve() as http:
        reply = http.post(
            w.base + "/pump",
            json={
                "expected_version": 2,
                "expected_revision": 3,
                field: bad,
            },
        )
        assert reply.status_code == 422
        assert not w.adapter.calls


@pytest.mark.parametrize("ack", ["true", 1, None, {}, []])
def test_confirmation_ack_is_strict_boolean(http_world, ack):
    w = http_world
    with w.serve() as http:
        for operation, name in [
            ("confirm_executed", "acknowledge_executed"),
            ("retry", "acknowledge_duplicate_risk"),
        ]:
            reply = http.post(
                w.base + "/recovery",
                json={
                    "operation": operation,
                    "expected_version": 1,
                    "expected_revision": 1,
                    name: ack,
                },
            )
            assert reply.status_code == 422


def test_json_limits_queries_methods_and_public_feedback_cannot_bypass(http_world):
    w = http_world
    with w.serve() as http:
        for raw in [
            b'{"expected_version":1,"expected_version":1}',
            b"[]",
            b'{"x":NaN}',
            b'{"x":' + b"[" * 14 + b"0" + b"]" * 14 + b"}",
        ]:
            assert (
                http.post(
                    w.base + "/prepare", content=raw, headers={"content-type": "application/json"}
                ).status_code
                == 422
            )
        assert http.post(w.base + "/prepare", content=b"x" * 65537).status_code == 413
        assert http.post(w.base + "/prepare", content=b"{}").status_code == 415
        assert http.get(w.base + "/execution?expected_version=1").status_code == 422
        for route in ("prepare", "pump", "recovery"):
            assert http.get(w.base + "/" + route).status_code == 405
        assert (
            http.post(
                "/v1/feedback",
                json={
                    "request_id": new_id(),
                    "module": "extraction",
                    "command": {"kind": "action_result", "target_id": w.sid, "outcome": "executed"},
                },
            ).status_code
            == 422
        )
        assert not w.adapter.calls


def test_live_auth_rechecked_on_every_route_and_stale_pump(http_world):
    w = http_world
    with w.serve() as http:
        start(w, http)
        args = versions(w, http)
        for field in args:
            reply = http.post(w.base + "/pump", json={**args, field: args[field] - 1})
            assert reply.status_code == 409
        w.fake.revoked.add(STORAGE_USER)
        assert http.get(w.base + "/execution").status_code == 401
        for route, payload in [
            ("prepare", {"expected_version": 2}),
            ("pump", args),
            ("recovery", {**args, "operation": "verify"}),
        ]:
            assert http.post(w.base + "/" + route, json=payload).status_code == 401
        assert len(w.adapter.calls) == 1
        http.headers.pop("authorization")
        assert http.get(w.base + "/execution").status_code == 401


@pytest.mark.parametrize("opt_in,adapter", [(False, False), (False, True), (True, False)])
def test_default_and_cli_without_adapter_remain_disabled(http_world, opt_in, adapter):
    w = http_world
    config = w.config.model_copy(update={"execution_action_v2": opt_in})
    with TestClient(create_app(config, adapter=w.adapter if adapter else None)) as http:
        http.headers.update(headers())
        assert state(w, http)["execution_enabled"] is False
        assert http.post(w.base + "/prepare", json={"expected_version": 1}).status_code == 501
        assert (
            http.post(
                w.base + "/pump", json={"expected_version": 1, "expected_revision": 1}
            ).status_code
            == 501
        )
        if not adapter:
            payload = {"operation": "verify", "expected_revision": 1}
            if opt_in:
                payload["expected_version"] = 1
            assert http.post(w.base + "/recovery", json=payload).status_code == 501
        assert not w.adapter.calls and not w.adapter.lookups


def test_configuration_defaults_strict_opt_in_and_legacy_injection_rejected(http_world):
    w = http_world
    assert settings("http://localhost").execution_action_v2 is False
    for bad in ("true", 1, None):
        with pytest.raises(ValidationError):
            settings("http://localhost", execution_action_v2=bad)
    with ExecutionJournal(w.path) as journal:
        legacy = ExecutionService(w.fake.client, journal, w.adapter)
        with pytest.raises(ValueError, match="explicit_adapter_and_journal"):
            create_app(w.config, execution=legacy)
    with pytest.raises(ValueError, match="journal_required"):
        create_app(w.config.model_copy(update={"execution_journal": None}), adapter=w.adapter)


def test_parallel_pump_preconditions_share_journal_lock(http_world):
    from concurrent.futures import ThreadPoolExecutor

    w = http_world
    with w.serve() as http:
        review(w, http)
        w.drain()
        assert http.post(w.base + "/prepare", json={"expected_version": 2}).status_code == 202
        w.drain()
        args = versions(w, http)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _: http.post(w.base + "/pump", json=args).status_code, range(4))
            )
        assert sorted(results) == [202, 409, 409, 409]
        assert len(w.adapter.calls) == 1
        assert settle(w, http)["suggestion_state"] == "executed"


@pytest.mark.parametrize("phase", ["start", "result"])
def test_lost_feedback_acceptance_replays_only_feedback_across_http_restart(http_world, phase):
    w = http_world
    with w.serve() as http:
        review(w, http)
        w.drain()
        w.fake.fail_after_write = phase == "start"
        reply = http.post(w.base + "/prepare", json={"expected_version": 2})
        assert reply.status_code == 202
        if phase == "start":
            assert reply.json()["feedback_delivery_state"] == "unknown"
            assert not w.adapter.calls
        else:
            w.drain()
            w.fake.fail_after_write = True
            assert advance(w, http)["feedback_delivery_state"] == "unknown"
            assert len(w.adapter.calls) == 1
        before = w.fake.client.query(Query("feedback")).items
    with w.serve() as http:
        assert advance(w, http)["feedback_delivery_state"] == "accepted"
        assert w.fake.client.query(Query("feedback")).items == before
        if phase == "start":
            settle(w, http)
        assert settle(w, http)["suggestion_state"] == "executed"
        assert len(w.adapter.calls) == 1


def test_cli_serve_never_injects_adapter_even_with_opt_in(http_world, monkeypatch):
    import sys

    import uvicorn
    from ear_api import cli

    w = http_world
    monkeypatch.setattr(cli, "load_settings", lambda _: w.config)
    monkeypatch.setattr(sys, "argv", ["ear-api", "serve", "--config", "synthetic"])

    def run(app, **kwargs):
        assert kwargs["workers"] == 1
        assert app.state.execution_http_enabled is False
        with TestClient(app) as http:
            http.headers.update(headers())
            assert state(w, http)["execution_enabled"] is False
            assert http.post(w.base + "/prepare", json={"expected_version": 1}).status_code == 501
            assert (
                http.post(
                    w.base + "/recovery",
                    json={
                        "operation": "verify",
                        "expected_version": 1,
                        "expected_revision": 1,
                    },
                ).status_code
                == 501
            )

    monkeypatch.setattr(uvicorn, "run", run)
    assert cli.main() == 0
    assert not w.adapter.calls and not w.adapter.lookups


def test_legacy_missing_risk_ack_still_rechecks_upstream_authority(http_world):
    w = http_world
    config = w.config.model_copy(update={"execution_action_v2": False})
    with TestClient(create_app(config)) as http:
        http.headers.update(headers())
        w.fake.revoked.add(STORAGE_USER)
        reply = http.post(
            w.base + "/recovery",
            json={
                "operation": "retry",
                "expected_revision": 1,
            },
        )
        assert reply.status_code == 401
