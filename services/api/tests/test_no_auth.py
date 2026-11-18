"""Opt-in loopback test access never changes the internal authorization domain."""

from __future__ import annotations

import pytest
from ear_api.app import create_app
from ear_api.business import BUSINESS_TYPES, feedback_identity
from ear_api.config import Settings
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, new_id
from ear_storage_client import MemoryStorage
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


@pytest.fixture
def local_admin_api():
    fake = FakeStorage()
    # The shipped baseline registry lacks the statically admitted MVP addition.
    fake.storage = MemoryStorage(
        registry=DEFAULT_REGISTRY.extend_core(
            DataTypeSpec("rule_selection", "semantic", "extraction")
        )
    )
    fake.client = fake.storage.client("api")
    with tcp_server(fake.app) as url:
        config = settings(url, public_origin=ORIGIN, test_no_auth=True, test_no_auth_role="admin")
        with TestClient(create_app(config), base_url=ORIGIN) as client:
            yield client, fake, config


def test_admin_no_auth_uses_explicit_endpoint_bindings(local_admin_api):
    client, fake, config = local_admin_api
    for headers in (
        {},
        {"Authorization": "Basic bad"},
        {"Authorization": "Bearer " + USER},
        {"Authorization": "Bearer " + ADMIN},
        {"Authorization": "Bearer bad"},
        {"X-Role": "user", "X-Subject": "owner"},
        [("Authorization", "first"), ("Authorization", "second")],
    ):
        assert client.get("/v1/me", headers=headers).json() == {"subject": "admin", "role": "admin"}
        assert client.get("/v1/plugins", headers=headers).status_code == 200
        assert client.get("/v1/management/results", headers=headers).status_code == 200
        assert all(token == STORAGE_ADMIN for _, _, token in fake.requests)
        fake.requests.clear()
        assert client.get("/v1/records/activity", headers=headers).status_code == 200
        # Ordinary user lacks person grants even though admin has them in this fixture.
        assert client.get("/v1/records/person", headers=headers).status_code == 403
        assert all(token == STORAGE_USER for _, _, token in fake.requests)
        fake.requests.clear()
    assert config.authenticate([]) == config.bindings[0]
    assert config.authenticate([], session=True) == config.bindings[1]
    assert config.authenticate([], admin=True) == config.bindings[1]
    assert client.get("/v1/session").json() == {"test_no_auth": True}
    for path in ("/v1/session", "/v1/me", "/workspace/", "/plugins/", "/v1/plugins"):
        output = client.get(path)
        assert all(s not in output.text for s in (USER, ADMIN, STORAGE_USER, STORAGE_ADMIN))


def test_admin_no_auth_management_still_rechecks_storage_authority(local_admin_api):
    client, fake, _ = local_admin_api
    # Synthetic status request only; no real deployment management writes in acceptance.
    payload = {"request_id": new_id(), "action": "status", "module": "demo"}
    assert client.post("/v1/management/requests", json=payload).status_code == 403
    assert not fake.requests
    assert (
        client.post("/v1/management/requests", json=payload, headers={"Origin": ORIGIN}).status_code
        == 202
    )
    fake.upstream_admin = False
    assert client.get("/v1/plugins").status_code == 403
    assert (
        client.post("/v1/management/requests", json=payload, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    fake.revoked.add(STORAGE_ADMIN)
    for path in ("/v1/me", "/v1/plugins", "/v1/management/results"):
        assert client.get(path).status_code == 401
    assert client.get("/v1/session").json() == {"test_no_auth": True}


def test_admin_no_auth_does_not_relabel_an_ordinary_storage_identity(local_admin_api):
    _, fake, config = local_admin_api
    raw = config.model_dump()
    # Swap roles, not secrets: management still cannot use an ordinary credential.
    raw["bindings"] = [
        {**raw["bindings"][0], "role": "admin"},
        {**raw["bindings"][1], "role": "user"},
    ]
    with TestClient(create_app(Settings.model_validate(raw)), base_url=ORIGIN) as client:
        # wire-v1 identity lacks role; the actual management boundary must still refuse.
        assert client.get("/v1/plugins").status_code == 403
        assert all(token == STORAGE_USER for _, _, token in fake.requests)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.invalid:18186"},
        {"Host": "127.0.0.1:8086"},
        {"Origin": "http://evil.invalid"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_admin_no_auth_preserves_ingress_guards(local_admin_api, headers):
    client, fake, _ = local_admin_api
    assert client.get("/v1/plugins", headers=headers).status_code == 403
    assert not fake.requests


@pytest.mark.parametrize("role", [None, True, 1, "root", "ADMIN", ""])
def test_no_auth_role_is_closed_enum(role):
    with pytest.raises(ValidationError):
        settings(
            "http://127.0.0.1:1", public_origin=ORIGIN, test_no_auth=True, test_no_auth_role=role
        )


def test_admin_role_requires_explicit_no_auth_and_unique_admin_binding():
    with pytest.raises(ValidationError):
        settings("http://127.0.0.1:1", public_origin=ORIGIN, test_no_auth_role="admin")
    config = settings("http://127.0.0.1:1", public_origin=ORIGIN)
    assert config.test_no_auth_role == "user"
    raw = dict(config.model_dump(), test_no_auth=True, test_no_auth_role="admin")
    raw["bindings"] = [config.bindings[0].model_dump()]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)
    raw["bindings"] = [
        config.bindings[1].model_dump(),
        {
            "subject": "other",
            "role": "admin",
            "token": "x" * 43,
            "storage_token": "y" * 43,
        },
    ]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


@pytest.mark.parametrize(
    "roles", [[], ["admin"], ["user"], ["user", "admin", "user"], ["user", "admin", "admin"]]
)
def test_admin_no_auth_requires_exactly_one_of_each_role(roles):
    raw = settings("http://127.0.0.1:1", public_origin=ORIGIN).model_dump()
    raw.update(test_no_auth=True, test_no_auth_role="admin")
    raw["bindings"] = [
        {
            "subject": f"binding_{i}",
            "role": role,
            "token": chr(65 + 2 * i) * 43,
            "storage_token": chr(66 + 2 * i) * 43,
        }
        for i, role in enumerate(roles)
    ]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


@pytest.mark.parametrize("data_type", sorted(BUSINESS_TYPES))
def test_admin_no_auth_all_business_types_use_only_user(local_admin_api, data_type):
    client, fake, _ = local_admin_api
    response = client.get("/v1/records/" + data_type, headers={"Authorization": "Bearer " + ADMIN})
    assert response.status_code == (403 if data_type == "person" else 200)
    assert fake.requests and all(token == STORAGE_USER for _, _, token in fake.requests)


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/v1/timeline", None),
        (
            "GET",
            "/v1/evidence/search?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z&q=synthetic",
            None,
        ),
        ("GET", "/v1/records/activity/{id}", None),
        ("GET", "/v1/records/activity/{id}?version=1", None),
        ("GET", "/v1/records/activity/{id}/history", None),
        ("GET", "/v1/feedback/{id}/status", None),
        ("GET", "/v1/previews/person/{id}", None),
        (
            "POST",
            "/v1/previews/reextract",
            {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"},
        ),
        ("GET", "/v1/actions/{id}/execution", None),
        ("POST", "/v1/actions/{id}/prepare", {"expected_version": 1}),
        ("POST", "/v1/actions/{id}/pump", {"expected_version": 1, "expected_revision": 1}),
        ("POST", "/v1/actions/{id}/recovery", {"operation": "verify", "expected_revision": 1}),
    ],
)
def test_admin_no_auth_business_route_matrix_never_uses_admin(
    local_admin_api, method, path, payload
):
    client, fake, _ = local_admin_api
    result = client.request(
        method,
        path.replace("{id}", new_id()),
        json=payload,
        headers={"Origin": ORIGIN, "Authorization": "Bearer " + ADMIN},
    )
    assert result.status_code in {200, 404}
    assert fake.requests and all(token == STORAGE_USER for _, _, token in fake.requests)


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/v1/plugins", None),
        ("GET", "/v1/management/results", None),
        ("GET", "/v1/management/results/{id}", None),
        ("GET", "/v1/management/history/jobs/{id}", None),
        ("POST", "/v1/management/requests", {"action": "status", "module": "demo"}),
        ("POST", "/v1/management/credentials/{id}/claim", {}),
    ],
)
def test_admin_no_auth_management_route_matrix_never_uses_user(
    local_admin_api, method, path, payload
):
    client, fake, _ = local_admin_api
    if path == "/v1/management/requests":
        payload = dict(payload, request_id=new_id())
    result = client.request(
        method,
        path.replace("{id}", new_id()),
        json=payload,
        headers={"Origin": ORIGIN, "Authorization": "Bearer " + USER},
    )
    assert result.status_code in {200, 202, 404}
    assert fake.requests and all(token == STORAGE_ADMIN for _, _, token in fake.requests)


def test_admin_no_auth_revoked_user_never_falls_back_to_live_admin(local_admin_api):
    client, fake, _ = local_admin_api
    fake.revoked.add(STORAGE_USER)
    for bearer in (USER, ADMIN):
        fake.requests.clear()
        assert (
            client.get(
                "/v1/records/activity", headers={"Authorization": "Bearer " + bearer}
            ).status_code
            == 401
        )
        assert fake.requests == [("GET", "v1/identity", None)]
    assert client.get("/v1/me").status_code == 200
    assert client.get("/v1/plugins").status_code == 200


def test_admin_no_auth_revoked_admin_never_falls_back_to_live_user(local_admin_api):
    client, fake, _ = local_admin_api
    fake.revoked.add(STORAGE_ADMIN)
    for path in ("/v1/me", "/v1/plugins", "/v1/management/results"):
        fake.requests.clear()
        assert client.get(path, headers={"Authorization": "Bearer " + USER}).status_code == 401
        assert fake.requests == [("GET", "v1/identity", None)]
    fake.requests.clear()
    assert client.get("/v1/records/activity").status_code == 200
    assert all(token == STORAGE_USER for _, _, token in fake.requests)


def test_admin_no_auth_feedback_audits_actual_business_principal(local_admin_api):
    client, fake, config = local_admin_api
    request_id = new_id()
    body = {
        "request_id": request_id,
        "module": "extraction",
        "command": {"kind": "rule_create", "rule": {"scope": "synthetic", "instruction": "notes"}},
    }
    response = client.post(
        "/v1/feedback", json=body, headers={"Origin": ORIGIN, "Authorization": "Bearer " + ADMIN}
    )
    assert response.status_code == 202
    fid, _ = feedback_identity(config.bindings[0].subject, request_id)
    assert response.json()["feedback_id"] == fid
    record = fake.client.get(fid)
    assert record is not None
    assert record.source.device == "api_owner"
    assert record.dedup_key == "owner:" + request_id
    assert all(token == STORAGE_USER for _, _, token in fake.requests)
    assert (
        client.post("/v1/feedback", json=body, headers={"Origin": ORIGIN}).json() == response.json()
    )


def test_authenticated_mode_flags_never_switch_bearer_binding(gateway):
    client, _, config = gateway
    for admin in (False, True):
        for session in (False, True):
            assert (
                config.authenticate(["Bearer " + USER], admin=admin, session=session)
                == config.bindings[0]
            )
            assert (
                config.authenticate(["Bearer " + ADMIN], admin=admin, session=session)
                == config.bindings[1]
            )
    assert client.get("/v1/plugins", headers={"Authorization": "Bearer " + USER}).status_code == 403
    assert (
        client.get("/v1/records/person", headers={"Authorization": "Bearer " + ADMIN}).status_code
        == 200
    )
