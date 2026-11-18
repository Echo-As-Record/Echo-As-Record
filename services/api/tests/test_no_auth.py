"""Opt-in loopback test access never changes the internal authorization domain."""

from __future__ import annotations

import pytest
from ear_api.app import create_app
from ear_api.config import Settings
from ear_contracts import new_id
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import ADMIN, STORAGE_ADMIN, STORAGE_USER, USER, FakeStorage, settings, tcp_server

ORIGIN = "http://127.0.0.1:18186"


@pytest.fixture
def local_test_api():
    fake = FakeStorage()
    with tcp_server(fake.app) as url:
        config = settings(url, public_origin=ORIGIN, test_no_auth=True)
        with TestClient(create_app(config), base_url=ORIGIN) as client:
            yield client, fake, config


def test_opt_in_ignores_all_bearers_and_preserves_fixed_identity(local_test_api):
    client, fake, config = local_test_api
    for headers in (
        {},
        {"Authorization": "Basic bad"},
        {"Authorization": "Bearer bad"},
        {"Authorization": "Bearer " + ADMIN},
        [("Authorization", "first"), ("Authorization", "second")],
    ):
        response = client.get("/v1/me", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"subject": "owner", "role": "user"}
        assert client.get("/v1/records/activity", headers=headers).status_code == 200
    assert all(token == STORAGE_USER for _, _, token in fake.requests)
    assert config.authenticate(["Bearer " + ADMIN]).role == "user"


def test_default_still_requires_token_and_discovery_is_minimal(gateway):
    client, _, config = gateway
    assert not config.test_no_auth
    for headers in ({}, {"Authorization": "Bearer malformed"}):
        assert client.get("/v1/me", headers=headers).status_code == 401
        assert client.get("/v1/records/activity", headers=headers).status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer " + USER}).status_code == 200
    response = client.get("/v1/session")
    assert response.json() == {"test_no_auth": False}
    assert response.headers["cache-control"] == "no-store"


def test_no_auth_does_not_bypass_live_storage_authority(local_test_api):
    client, fake, _ = local_test_api
    # Fake storage denies the fixed ordinary user this type, including with an admin bearer.
    assert (
        client.get("/v1/records/person", headers={"Authorization": "Bearer " + ADMIN}).status_code
        == 403
    )
    fake.revoked.add(STORAGE_USER)
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/records/activity").status_code == 401
    assert client.get("/v1/session").json() == {"test_no_auth": True}


def test_no_auth_admin_endpoints_remain_forbidden(local_test_api):
    client, fake, _ = local_test_api
    for path in (
        "/v1/plugins",
        "/v1/management/results",
        "/v1/management/results/" + new_id(),
        "/v1/management/history/jobs/" + new_id(),
    ):
        assert client.get(path, headers={"Authorization": "Bearer " + ADMIN}).status_code == 403
    for path in ("/v1/management/requests", "/v1/management/credentials/" + new_id() + "/claim"):
        assert client.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert not fake.requests


def test_host_origin_and_browser_site_guards(local_test_api):
    client, fake, _ = local_test_api
    for host in (
        "evil.invalid:18186",
        "localhost:18186",
        "127.0.0.1:9999",
        "127.0.0.1.evil.invalid",
    ):
        response = client.get("/v1/session", headers={"Host": host})
        assert response.status_code == 403
        assert response.json() == {"code": "host_denied"}
    assert client.get("/v1/me", headers={"Origin": "http://evil.invalid"}).status_code == 403
    assert client.get("/v1/me", headers={"Origin": "null"}).status_code == 403
    assert client.get("/v1/me", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/v1/me", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.post("/v1/feedback", json={}).json() == {"code": "origin_required"}
    assert not fake.requests
    response = client.get(
        "/v1/session", headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    )
    assert response.json() == {"test_no_auth": True}
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert "access-control-allow-origin" not in response.headers
    assert client.get("/health/live", headers={"Host": "127.0.0.1:8086"}).status_code == 200
    assert client.get("/v1/me", headers={"Host": "127.0.0.1:8086"}).status_code == 403


def test_feedback_uses_fixed_user_and_does_not_leak_tokens(local_test_api):
    client, fake, _ = local_test_api
    response = client.post(
        "/v1/feedback",
        headers={"Origin": ORIGIN, "Authorization": "broken"},
        json={
            "request_id": new_id(),
            "module": "extraction",
            "command": {
                "kind": "rule_create",
                "rule": {"scope": "synthetic", "instruction": "notes"},
            },
        },
    )
    assert response.status_code == 202
    assert all(token == STORAGE_USER for _, _, token in fake.requests)
    for path in ("/v1/session", "/v1/me", "/workspace/", "/plugins/"):
        output = client.get(path)
        assert output.status_code == 200
        assert all(
            secret not in output.text for secret in (USER, ADMIN, STORAGE_USER, STORAGE_ADMIN)
        )


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://0.0.0.0:8086",
        "https://example.com",
        "http://192.168.1.2",
        "http://127.0.0.1.evil.invalid",
    ],
)
def test_no_auth_rejects_nonloopback_origin(origin):
    with pytest.raises(ValidationError):
        settings("http://127.0.0.1:1", test_no_auth=True, public_origin=origin)


@pytest.mark.parametrize("origin", [ORIGIN, "http://localhost:8086", "http://[::1]:8086"])
def test_no_auth_allows_explicit_loopback_origin(origin):
    assert settings("http://127.0.0.1:1", test_no_auth=True, public_origin=origin).test_no_auth


@pytest.mark.parametrize("value", ["true", 1, None])
def test_no_auth_requires_strict_boolean(value):
    with pytest.raises(ValidationError):
        settings("http://127.0.0.1:1", test_no_auth=value, public_origin=ORIGIN)


def test_no_auth_requires_one_unambiguous_ordinary_binding():
    config = settings("http://127.0.0.1:1", public_origin=ORIGIN)
    raw = config.model_dump()
    raw["test_no_auth"] = True
    raw["bindings"] = [config.bindings[1].model_dump()]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)
    raw["bindings"] = [
        config.bindings[0].model_dump(),
        {
            "subject": "other",
            "role": "user",
            "token": "x" * 43,
            "storage_token": "y" * 43,
        },
    ]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)
