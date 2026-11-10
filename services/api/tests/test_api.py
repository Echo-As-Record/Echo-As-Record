from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from ear_api.app import create_app
from ear_api.business import BUSINESS_TYPES
from ear_api.cli import load_settings, main
from ear_api.config import Settings
from ear_contracts import DEFAULT_REGISTRY, new_id
from ear_storage_client import Context, Create, PutSystemRecord, Query, Supersede, Update
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import (
    ADMIN,
    STORAGE_ADMIN,
    STORAGE_USER,
    USER,
    FakeStorage,
    headers,
    settings,
    tcp_server,
)


def test_business_types_match_original_allowlist():
    expected = {
        "activity",
        "artifact",
        "person",
        "place",
        "rule",
        "action_suggestion",
        "attribution",
        "voiceprint",
        "gps_point",
        "transcript_segment",
        "phone_info",
        "audio_ref",
        "ambient_sound",
        "local_speaker",
        "feedback",
    }
    assert expected == BUSINESS_TYPES
    assert "plugin_management_feedback" not in BUSINESS_TYPES
    assert not BUSINESS_TYPES.intersection(
        name for name, spec in DEFAULT_REGISTRY.data_types.items() if spec.kind == "system_record"
    )


@pytest.mark.parametrize(
    "data_type",
    [*sorted(set(DEFAULT_REGISTRY.data_types) - BUSINESS_TYPES), "unknown_type", "demo_result"],
)
@pytest.mark.parametrize("suffix", ["", "/{id}", "/{id}?version=1", "/{id}/history"])
@pytest.mark.parametrize("admin", [False, True])
def test_non_business_types_are_not_found(data_type, suffix, admin):
    # Rejected types must be handled locally, before contacting storage.
    with TestClient(create_app(settings("http://127.0.0.1:1"))) as client:
        result = client.get(
            "/v1/records/" + data_type + suffix.format(id=new_id()), headers=headers(admin)
        )
    assert result.status_code == 404
    assert result.json() == {"code": "not_found"}


def command(**changes):
    return {
        "request_id": new_id(),
        "module": "extraction",
        "command": {"kind": "rule_create", "rule": {"scope": "meeting", "instruction": "notes"}},
        **changes,
    }


def test_auth_is_header_only_and_no_admin_fallback(gateway):
    client, fake, _ = gateway
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", cookies={"token": ADMIN}).status_code == 401
    assert client.get("/v1/me?token=" + ADMIN, headers=headers()).status_code == 422
    assert (
        client.get(
            "/v1/me",
            headers=[("authorization", "Bearer " + USER), ("authorization", "Bearer " + ADMIN)],
        ).status_code
        == 401
    )
    assert client.get("/v1/me", headers=headers()).json() == {"subject": "owner", "role": "user"}
    assert fake.requests[-1][2] == STORAGE_USER
    before = len(fake.requests)
    assert client.get("/v1/plugins", headers=headers()).status_code == 403
    assert (
        client.post("/v1/management/requests", headers=headers(), json={"admin": True}).status_code
        == 403
    )
    assert len(fake.requests) == before
    fake.revoked.add(STORAGE_USER)
    assert client.get("/v1/me", headers=headers()).status_code == 401
    assert client.get("/v1/me", headers=headers(True)).status_code == 200
    assert fake.requests[-1][2] == STORAGE_ADMIN


def test_configuration_rejects_shared_upstream_identity():
    values = settings("http://localhost").model_dump(mode="json")
    # SecretStr dumps redact; deliberately supply the synthetic credentials below.
    values["bindings"] = [
        {"subject": "user", "role": "user", "token": USER, "storage_token": STORAGE_ADMIN},
        {"subject": "admin", "role": "admin", "token": ADMIN, "storage_token": STORAGE_ADMIN},
    ]
    with pytest.raises(ValidationError):
        Settings.model_validate(values)
    assert STORAGE_ADMIN not in repr(settings("http://localhost"))


def test_feedback_is_stable_after_unknown_and_restart(gateway):
    client, fake, config = gateway
    data = command()
    fake.fail_after_write = True
    result = client.post("/v1/feedback", headers=headers(), json=data)
    assert result.status_code == 503
    assert result.json()["code"] == "acceptance_unknown_replay_identical_request"
    assert "PRIVATE" not in result.text
    # Reconstruct the whole API: no in-process idempotency cache is required.
    with TestClient(create_app(config)) as fresh:
        accepted = fresh.post("/v1/feedback", headers=headers(), json=data)
        replay = fresh.post("/v1/feedback", headers=headers(), json=data)
        assert accepted.status_code == 202
        assert replay.json() == accepted.json()
        altered = {
            **data,
            "command": {
                "kind": "rule_create",
                "rule": {"scope": "changed", "instruction": "notes"},
            },
        }
        assert fresh.post("/v1/feedback", headers=headers(), json=altered).status_code == 422
    fid = accepted.json()["feedback_id"]
    assert accepted.json()["root_feedback_id"] == fid
    assert accepted.json()["business_status"] == "pending"
    records = fake.client.query(Query("feedback"))
    assert len(records.items) == 1
    assert records.items[0].id == fid
    assert records.items[0].payload["command"]["kind"] == "rule_create"
    assert fake.storage.events[-1].root_feedback_id == fid
    assert fake.storage.events[-1].depth == 0


def test_feedback_same_request_different_subjects(gateway):
    client, fake, _ = gateway
    data = command()
    a = client.post("/v1/feedback", headers=headers(), json=data)
    b = client.post("/v1/feedback", headers=headers(True), json=data)
    assert a.status_code == b.status_code == 202
    assert a.json()["feedback_id"] != b.json()["feedback_id"]
    assert len(fake.client.query(Query("feedback")).items) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"admin": True},
        {"token": "do not echo"},
        {"module": "storage"},
        {
            "command": {
                "kind": "action_result",
                "target_id": new_id(),
                "expected_version": 1,
                "outcome": "executed",
                "receipt": "untrusted",
            }
        },
        {"command": {"kind": "reevaluate", "target_id": new_id()}},
        {"command": {"kind": "rule_delete", "target_id": new_id(), "expected_version": True}},
        {"command": {"kind": "rule_create", "rule": {"scope": "x" * 8193, "instruction": "x"}}},
    ],
)
def test_closed_inputs(gateway, change):
    client, fake, _ = gateway
    result = client.post("/v1/feedback", headers=headers(), json={**command(), **change})
    assert result.status_code == 422
    assert result.json() == {"code": "invalid_input"}
    assert not fake.receipts


def test_bounded_body_json_origin_and_security_headers(gateway):
    client, _, _ = gateway
    assert client.post("/v1/feedback", content=b"x" * 65537, headers=headers()).status_code == 413
    assert (
        client.post(
            "/v1/feedback",
            content='{"a":1,"a":2}',
            headers={**headers(), "Content-Type": "application/json"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/feedback",
            content='{"x":NaN}',
            headers={**headers(), "Content-Type": "application/json"},
        ).status_code
        == 422
    )
    assert client.post("/v1/feedback", content=b"{}", headers=headers()).status_code == 415
    assert (
        client.get("/v1/me", headers={**headers(), "Origin": "https://evil.invalid"}).status_code
        == 403
    )
    assert (
        client.get("/v1/me", headers={**headers(), "Origin": "http://testserver"}).status_code
        == 200
    )
    for path in ("/health/live", "/missing", "/v1/me"):
        r = client.get(path)
        assert r.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "access-control-allow-origin" not in r.headers


def test_current_history_timeline_and_permission_passthrough(gateway):
    client, fake, _ = gateway
    ids = [new_id(), new_id()]
    worker = fake.storage.client("extraction")
    for aid in ids:
        worker.write(
            [
                Create(
                    "activity",
                    aid,
                    {"title_md": "v1"},
                    event_start=datetime.fromisoformat("2026-01-01T00:00:00Z"),
                    event_end=datetime.fromisoformat("2026-01-01T01:00:00Z"),
                )
            ],
            Context.system(),
        ).check()
    worker.write([Update("activity", ids[0], 1, {"title_md": "v2"})], Context.system()).check()
    worker.write([Supersede("activity", ids[0], 2)], Context.system()).check()
    first = client.get("/v1/timeline?limit=1&include_superseded=true", headers=headers()).json()
    assert len(first["items"]) == 1 and first["next_cursor"] is not None
    second = client.get(
        "/v1/timeline",
        params={"limit": 1, "include_superseded": "true", "cursor": first["next_cursor"]},
        headers=headers(),
    ).json()
    assert len(second["items"]) == 1
    current = client.get("/v1/timeline", headers=headers()).json()
    assert len(current["items"]) == 1 and current["items"][0]["id"] == ids[1]
    path = "/v1/records/activity/" + ids[0]
    assert client.get(path + "?version=1", headers=headers()).json()["payload"] == {
        "title_md": "v1"
    }
    h = client.get(path + "/history?limit=2", headers=headers()).json()
    assert [r["version"] for r in h["items"]] == [1, 2]
    assert h["next_after_version"] == 2
    assert (
        client.get(path + "/history?after_version=2", headers=headers()).json()["items"][0][
            "status"
        ]
        == "superseded"
    )
    assert client.get("/v1/records/person", headers=headers()).status_code == 403
    assert (
        client.get("/v1/records/plugin_management_result", headers=headers(True)).status_code == 404
    )
    assert client.get("/v1/timeline?limit=101", headers=headers()).status_code == 422
    assert client.get("/v1/timeline?limit=1&limit=2", headers=headers()).status_code == 422
    assert client.get("/v1/timeline?start=2026-01-01", headers=headers()).status_code == 422


def test_pending_contract_and_real_business_status(gateway):
    client, fake, _ = gateway
    pending = command(
        module="dependency",
        command={
            "kind": "reevaluate_related",
            "previous_target_id": new_id(),
            "new_target_id": new_id(),
        },
    )
    result = client.post("/v1/feedback", headers=headers(), json=pending)
    assert result.status_code == 202
    assert result.json()["integration"] == "pending_contract"
    feedback = client.post("/v1/feedback", headers=headers(), json=command()).json()
    fid = feedback["feedback_id"]
    url = "/v1/feedback/" + fid + "/status"
    assert client.get(url, headers=headers()).json()["status"] == "pending"
    rid = new_id()
    payload = {"root_feedback_id": fid, "status": "queued", "model_calls": ["PRIVATE_BODY"]}
    worker = fake.storage.client("extraction")
    worker.write([PutSystemRecord("extraction_run", rid, payload)], Context.system()).check()
    assert client.get(url, headers=headers()).json()["status"] == "queued"
    worker.write(
        [
            PutSystemRecord(
                "extraction_run", rid, {**payload, "status": "succeeded"}, expected_revision=1
            )
        ],
        Context.system(),
    ).check()
    status = client.get(url, headers=headers())
    assert status.json()["status"] == "succeeded"
    assert "PRIVATE_BODY" not in status.text
    assert status.json()["completion_source"] == "extraction_run"


@pytest.mark.parametrize(
    "action",
    [
        "register_plugin",
        "set_permissions",
        "enable",
        "disable",
        "rotate_credentials",
        "revoke_credentials",
        "retry_task",
        "status",
        "history_backfill",
    ],
)
def test_all_management_actions_use_existing_feedback_http(gateway, action):
    client, fake, _ = gateway
    data = {"request_id": new_id(), "action": action, "module": "demo"}
    if action == "register_plugin":
        data["types"] = [{"name": "demo_result", "kind": "semantic"}]
    if action == "set_permissions":
        data.update(read_types=["activity"], subscribe_types=["activity"])
    if action == "retry_task":
        data["target_feedback_id"] = new_id()
    if action == "history_backfill":
        data.update(
            rangeStart="2026-01-01T00:00:00Z",
            rangeEnd="2026-01-02T00:00:00Z",
            input_types=["activity"],
        )
    result = client.post("/v1/management/requests", headers=headers(True), json=data)
    assert result.status_code == 202, result.text
    assert result.json()["status"] == "accepted"
    replay = client.post("/v1/management/requests", headers=headers(True), json=data)
    assert replay.json() == result.json()
    assert len(fake.managed) == 1
    assert fake.requests[-1][1:] == ("v1/management/requests", STORAGE_ADMIN)
    assert not fake.receipts


@pytest.mark.parametrize("choice", ["catch_up", "skip"])
def test_recovery_revision_and_upstream_revocation(gateway, choice):
    client, fake, _ = gateway
    data = {"request_id": new_id(), "action": "enable", "module": "demo", "resume_choice": choice}
    assert (
        client.post("/v1/management/requests", headers=headers(True), json=data).status_code == 422
    )
    data["expected_revision"] = 2
    assert (
        client.post("/v1/management/requests", headers=headers(True), json=data).status_code == 202
    )
    fake.upstream_admin = False
    data["request_id"] = new_id()
    assert (
        client.post("/v1/management/requests", headers=headers(True), json=data).status_code == 403
    )


def test_registry_unknown_online_results_and_history_job(gateway):
    client, fake, _ = gateway
    plugins = client.get("/v1/plugins?limit=1", headers=headers(True))
    assert plugins.status_code == 200, plugins.text
    assert plugins.json()["items"][0]["online"] is None
    assert plugins.json()["items"][0]["enabled"] is None
    assert plugins.json()["next_after"] == "demo"
    rid, fid = new_id(), new_id()
    fake.storage.client("storage").write(
        [
            PutSystemRecord(
                "plugin_management_result",
                rid,
                {
                    "feedback_id": fid,
                    "status": "pending",
                    "stage": "resources",
                    "module": "demo",
                    "error_code": "private error with body",
                    "token": "SECRET",
                    "online": True,
                },
            )
        ],
        Context.system(),
    ).check()
    result = client.get("/v1/management/results/" + rid, headers=headers(True))
    assert result.status_code == 200
    assert "SECRET" not in result.text and "private error" not in result.text
    assert result.json()["payload"]["error_code"] == "management_failed"
    assert result.json()["payload"]["online"] is None
    assert (
        client.get("/v1/management/results?module=demo", headers=headers(True)).json()["items"][0][
            "id"
        ]
        == rid
    )
    assert (
        client.get("/v1/management/history/jobs/" + new_id(), headers=headers(True)).json()[
            "status"
        ]
        == "planned"
    )


def test_single_use_claim_not_cached_or_retried(gateway, caplog):
    client, fake, _ = gateway
    fid = new_id()
    path = "/v1/management/credentials/" + fid + "/claim"
    caplog.set_level(logging.INFO)
    result = client.post(path, headers=headers(True), json={})
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["token"] == "c" * 43
    assert "c" * 43 not in caplog.text
    assert client.post(path, headers=headers(True), json={}).status_code == 403
    fake.fail_claim = True
    result = client.post(
        "/v1/management/credentials/" + new_id() + "/claim", headers=headers(True), json={}
    )
    assert result.status_code == 503
    assert result.json()["code"] == "credential_delivery_unknown"
    assert len(fake.claimed) == 2


def test_reextract_preview_matches_extraction_and_no_business_writes(gateway):
    from ear_extraction.domain import Interval
    from ear_extraction.planning import preview_reextract

    client, fake, _ = gateway
    worker = fake.storage.client("extraction")
    aid, artifact_id, suggestion_id = new_id(), new_id(), new_id()
    bounds = {
        "event_start": datetime.fromisoformat("2026-01-01T00:00:00Z"),
        "event_end": datetime.fromisoformat("2026-01-01T01:00:00Z"),
    }
    worker.write(
        [
            Create(
                "activity",
                aid,
                {"title_md": "meeting"},
                event_start=bounds["event_start"],
                event_end=bounds["event_end"],
            ),
            Create(
                "artifact",
                artifact_id,
                {"activity_id": aid, "manual": True},
                event_start=bounds["event_start"],
                event_end=bounds["event_end"],
            ),
            Create(
                "action_suggestion",
                suggestion_id,
                {"artifact_id": artifact_id, "state": "pending"},
                event_start=bounds["event_start"],
                event_end=bounds["event_end"],
            ),
        ],
        Context.system(),
    ).check()
    interval = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T02:00:00Z"}
    expected = preview_reextract(fake.client, Interval.model_validate(interval))
    before = len(fake.storage.events)
    result = client.post("/v1/previews/reextract", json=interval, headers=headers())
    assert result.status_code == 200, result.text
    assert result.json() == expected
    assert len(fake.storage.events) == before
    partial = {**interval, "end": "2026-01-01T00:30:00Z"}
    assert client.post("/v1/previews/reextract", json=partial, headers=headers()).status_code == 409


def test_real_tcp_api_to_real_tcp_simulated_storage():
    fake = FakeStorage()
    with (
        tcp_server(fake.app) as storage_url,
        tcp_server(create_app(settings(storage_url))) as api_url,
        httpx.Client(base_url=api_url, trust_env=False) as client,
    ):
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert client.get("/v1/me", headers=headers()).json()["role"] == "user"
        accepted = client.post("/v1/feedback", headers=headers(), json=command())
        assert accepted.status_code == 202
        fid = accepted.json()["feedback_id"]
        assert (
            client.get("/v1/feedback/" + fid + "/status", headers=headers()).json()["status"]
            == "pending"
        )
        assert len(fake.client.query(Query("feedback")).items) == 1


def test_static_csp_cli_protected_config_and_health(tmp_path, monkeypatch, capsys):
    (tmp_path / "index.html").write_text('<!doctype html><script src="/plugins/app.js"></script>')
    (tmp_path / "app.js").write_text('"use strict";')
    config = settings("http://127.0.0.1:1", static_dir=tmp_path)
    with TestClient(create_app(config)) as client:
        r = client.get("/plugins/index.html")
        assert r.status_code == 200
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert "unsafe-inline" not in r.headers["content-security-policy"]
    file = tmp_path / "config.json"
    content = {
        "storage_url": "http://127.0.0.1:1",
        "bindings": [
            {"subject": "owner", "role": "user", "token": USER, "storage_token": STORAGE_USER}
        ],
    }
    file.write_text(json.dumps(content))
    os.chmod(file, 0o600)
    assert load_settings(str(file)).bindings[0].subject == "owner"
    monkeypatch.setattr("sys.argv", ["ear-api", "check", "--config", str(file)])
    assert main() == 0
    os.chmod(file, 0o644)
    assert main() == 1
    assert USER not in capsys.readouterr().out


def test_no_cross_module_writes_or_direct_admin_code():
    root = Path(__file__).resolve().parents[1] / "src" / "ear_api"
    source = "\n".join(p.read_text() for p in root.glob("*.py"))
    for forbidden in (
        "import psycopg",
        "AuthOperator",
        "PostgresStorage",
        "MarkProcessed(",
        "PutSystemRecord(",
        "Supersede(",
        "Update(",
        "Create(",
    ):
        assert forbidden not in source
    assert "ear_storage.management_input import ManagementInput" in source
