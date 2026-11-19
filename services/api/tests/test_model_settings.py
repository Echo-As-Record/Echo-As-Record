from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from ear_api.app import create_app
from ear_api.config import Settings
from ear_api.model_settings import ModelSelection, ModelSettings, ModelSettingsUnavailable
from ear_api.upstream import Upstream
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import ADMIN, STORAGE_ADMIN, headers, settings

ROUTE = "/v1/settings/model"
ORIGIN = "http://testserver"
INITIAL = {"model": "gpt-6-luna", "revision": 1}
UPDATED = {"model": "new/model:variant@v2+latest-1.2", "revision": 2}


@pytest.fixture
def selection_path(tmp_path: Path) -> Path:
    parent = tmp_path / "settings"
    parent.mkdir(mode=0o700)
    target = parent / "settings.json"
    target.write_text(json.dumps(INITIAL), encoding="ascii")
    target.chmod(0o600)
    return target


class AdminWire:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.admin = True
        self.revoked = False
        self.bad_response = False

    def reply(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + STORAGE_ADMIN
        if self.revoked:
            return httpx.Response(401)
        if request.url.path == "/v1/identity":
            # Actual deployed wire-v1 identity does NOT identify an admin.
            return httpx.Response(200, json={"codec": 1, "module": "api"})
        assert request.url.path == "/v1/management/plugins"
        assert request.url.params["limit"] == "1"
        if not self.admin:
            return httpx.Response(403, json={"code": "PRIVATE upstream denial"})
        if self.bad_response:
            return httpx.Response(200, json={"codec": 1, "secret": "PRIVATE upstream error"})
        return httpx.Response(200, json={"codec": 1, "items": [], "next_after": None})


@pytest.fixture
def api(selection_path: Path) -> Iterator[tuple[TestClient, AdminWire]]:
    wire = AdminWire()
    config = settings("http://upstream.invalid", model_settings_file=selection_path)
    with TestClient(
        create_app(config, upstream=Upstream(config, httpx.MockTransport(wire.reply)))
    ) as client:
        yield client, wire


def test_updates_only_exact_projection_with_cas_and_live_authority(api, selection_path):
    client, wire = api
    auth = {**headers(True), "origin": ORIGIN}
    assert client.get(ROUTE, headers=auth).json() == INITIAL
    old_inode = selection_path.stat().st_ino
    saved = client.put(ROUTE, headers=auth, json={"model": UPDATED["model"], "revision": 1})
    assert saved.status_code == 200
    assert saved.json() == UPDATED
    assert saved.headers["cache-control"] == "no-store"
    assert json.loads(selection_path.read_bytes()) == UPDATED
    assert stat.S_IMODE(selection_path.stat().st_mode) == 0o600
    assert selection_path.stat().st_ino != old_inode
    assert list(selection_path.parent.iterdir()) == [selection_path]
    assert ModelSettings(selection_path).read().model_dump() == UPDATED
    conflict = client.put(ROUTE, headers=auth, json=INITIAL)
    assert conflict.status_code == 409
    assert conflict.json() == {"code": "model_settings_conflict"}
    assert client.get(ROUTE, headers=auth).json() == UPDATED
    assert wire.calls == ["/v1/identity", "/v1/management/plugins"] * 4
    assert json.loads(selection_path.read_bytes()) == UPDATED


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_denied_bearer_or_current_role_never_touches_file(api, selection_path, monkeypatch, method):
    client, wire = api
    kwargs = {"json": INITIAL} if method == "PUT" else {}

    def unexpected_fs(*_args, **_kwargs):
        pytest.fail("denied request must not access model settings")

    monkeypatch.setattr(ModelSettings, "_locked", unexpected_fs)
    for auth, expected in [({}, 401), (headers(), 403), ({"Cookie": "token=" + ADMIN}, 401)]:
        reply = client.request(method, ROUTE, headers=auth, **kwargs)
        assert reply.status_code == expected
    reply = client.request(
        method, ROUTE, headers=[("authorization", "Bearer " + ADMIN)] * 2, **kwargs
    )
    assert reply.status_code == 401
    assert wire.calls == []
    assert client.get("/v1/me", headers=headers(True)).json()["role"] == "admin"
    wire.admin = False
    reply = client.request(method, ROUTE, headers=headers(True), **kwargs)
    assert reply.status_code == 403
    assert reply.json() == {"code": "forbidden"}
    assert wire.calls[-2:] == ["/v1/identity", "/v1/management/plugins"]
    wire.admin = True
    wire.bad_response = True
    reply = client.request(method, ROUTE, headers=headers(True), **kwargs)
    assert reply.status_code == 503
    assert reply.json() == {"code": "storage_unavailable"}
    wire.revoked = True
    reply = client.request(method, ROUTE, headers=headers(True), **kwargs)
    assert reply.status_code == 401
    assert json.loads(selection_path.read_bytes()) == INITIAL


@pytest.mark.parametrize("mode", [None, "user", "admin"])
def test_disabled_without_configuration_or_under_either_noauth_role(selection_path, mode):
    wire = AdminWire()
    config = settings(
        "http://upstream.invalid",
        public_origin="http://127.0.0.1",
        model_settings_file=selection_path if mode else None,
        test_no_auth=mode is not None,
        test_no_auth_role=mode or "user",
    )
    with TestClient(
        create_app(config, upstream=Upstream(config, httpx.MockTransport(wire.reply))),
        base_url="http://127.0.0.1",
    ) as client:
        for auth in ({}, headers(True)):
            for method in ("GET", "PUT"):
                reply = client.request(
                    method,
                    ROUTE,
                    headers={**auth, "origin": "http://127.0.0.1"},
                    **({"json": INITIAL} if method == "PUT" else {}),
                )
                assert reply.status_code == 404
                assert reply.json() == {"code": "not_found"}
        assert client.get("/v1/session").json() == {"test_no_auth": mode is not None}
    assert wire.calls == []
    assert json.loads(selection_path.read_bytes()) == INITIAL


def test_loopback_admin_opt_in_without_api_tokens(selection_path):
    origin = "http://127.0.0.1:18186"
    raw = settings("http://upstream.invalid", public_origin=origin).model_dump()
    for binding in raw["bindings"]:
        del binding["token"]
    with pytest.raises(ValidationError, match="require an API token"):
        Settings.model_validate(raw)
    raw.update(
        test_no_auth=True,
        test_no_auth_role="admin",
        test_no_auth_model_settings=True,
        model_settings_file=selection_path,
    )
    for invalid in (
        {"test_no_auth": False, "test_no_auth_role": "user"},
        {"test_no_auth_role": "user"},
        {"model_settings_file": None},
        {"public_origin": "http://192.0.2.1:18186"},
        {"test_no_auth_model_settings": "true"},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**raw, **invalid})
    config = Settings.model_validate(raw)
    assert all(b.token is None for b in config.bindings)
    wire = AdminWire()
    with TestClient(
        create_app(config, upstream=Upstream(config, httpx.MockTransport(wire.reply))),
        base_url=origin,
    ) as client:
        assert client.get("/v1/session").json() == {
            "test_no_auth": True,
            "test_no_auth_model_settings": True,
        }
        assert client.put(ROUTE, json=INITIAL).json() == {"code": "origin_required"}
        for bad in (
            {"Origin": "https://elsewhere.invalid"},
            {"Origin": origin, "Host": "bad.invalid:18186"},
            {"Origin": origin, "Sec-Fetch-Site": "cross-site"},
        ):
            assert client.put(ROUTE, headers=bad, json=INITIAL).status_code == 403
        assert not wire.calls
        assert client.get(ROUTE).json() == INITIAL
        saved = client.put(
            ROUTE,
            headers={"Origin": origin},
            json={
                "model": UPDATED["model"],
                "revision": 1,
            },
        )
        assert saved.status_code == 200 and saved.json() == UPDATED
        assert client.put(ROUTE, headers={"Origin": origin}, json=INITIAL).status_code == 409
        wire.revoked = True
        assert client.get(ROUTE).status_code == 401
        assert client.put(ROUTE, headers={"Origin": origin}, json=UPDATED).status_code == 401
        assert json.loads(selection_path.read_bytes()) == UPDATED


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "PRIVATE"},
        {"url": "https://invalid.example"},
        {"base_url": "PRIVATE"},
        {"key": "PRIVATE"},
        {"api_key": "PRIVATE"},
        {"role": "admin"},
        {"subject": "owner"},
        {"expected_revision": 1},
        {"revision": True},
        {"revision": "1"},
        {"revision": 1.0},
        {"revision": 0},
        {"revision": -1},
        {"revision": None},
        {"revision": 1 << 63},
        {"model": ""},
        {"model": 1},
        {"model": "x" * 129},
        {"model": "/other"},
        {"model": "gpt-6-luna\n"},
        {"model": "模型"},
        {"model": "x y"},
        {"model": "<script>"},
        {"model": "https://invalid.example"},
    ],
)
def test_closed_strict_input(api, payload):
    client, wire = api
    reply = client.put(ROUTE, headers=headers(True), json={**INITIAL, **payload})
    assert reply.status_code == 422
    assert reply.json() == {"code": "invalid_input"}
    assert wire.calls == []


def test_query_ingress_and_static_navigation(api):
    client, wire = api
    auth = headers(True)
    for query in ("?anything", "?revision=1&revision=1", "?%", "?&"):
        assert client.get(ROUTE + query, headers=auth).status_code == 422
        assert client.put(ROUTE + query, headers=auth, json=INITIAL).status_code == 422
    assert client.request("GET", ROUTE, headers=auth, json={}).status_code == 422
    for raw, expected in [
        ("{}", 422),
        ('{"model":"x","model":"y","revision":1}', 422),
        (" " * 65537, 413),
    ]:
        reply = client.put(ROUTE, headers={**auth, "content-type": "application/json"}, content=raw)
        assert reply.status_code == expected
    assert client.put(ROUTE, headers=auth, content="{}").status_code == 415
    for method in ("GET", "PUT"):
        reply = client.request(
            method,
            ROUTE,
            headers={**auth, "origin": "https://elsewhere.invalid"},
            **({"json": INITIAL} if method == "PUT" else {}),
        )
        assert reply.status_code == 403
        assert reply.json() == {"code": "cross_origin_denied"}
        assert not any(key.startswith("access-control-") for key in reply.headers)
    assert 'href="/model-settings/"' in client.get("/plugins/").text
    for name, mime in [
        ("", "text/html"),
        ("model-settings.js", "text/javascript"),
        ("model-settings.css", "text/css"),
    ]:
        asset = client.get("/model-settings/" + name)
        assert asset.status_code == 200
        assert asset.headers["content-type"].split(";")[0] == mime
        assert asset.headers["x-content-type-options"] == "nosniff"
        assert asset.headers["cache-control"] == "no-store"
        csp = asset.headers["content-security-policy"]
        for directive in (
            "script-src 'self'",
            "style-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
        ):
            assert directive in csp
        assert "unsafe-" not in csp
    assert client.get("/model-settings/workspace.html").status_code == 404
    assert client.get("/model-settings").status_code == 404
    assert 'href="/plugins/"' in client.get("/model-settings/").text
    assert wire.calls == []


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "symlink",
        "hardlink",
        "fifo",
        "directory",
        "mode",
        "parent_mode",
        "parent_symlink",
        "parent_file",
    ],
)
def test_wrong_path_or_permissions_fail_closed(api, selection_path, tmp_path, defect):
    client, _wire = api
    other = tmp_path / "other"
    parent = selection_path.parent
    match defect:
        case "missing":
            selection_path.unlink()
        case "symlink":
            selection_path.rename(other)
            selection_path.symlink_to(other)
        case "hardlink":
            os.link(selection_path, other)
        case "fifo":
            selection_path.unlink()
            os.mkfifo(selection_path, 0o600)
        case "directory":
            selection_path.unlink()
            selection_path.mkdir(mode=0o700)
        case "mode":
            selection_path.chmod(0o640)
        case "parent_mode":
            parent.chmod(0o750)
        case "parent_symlink":
            parent.rename(other)
            parent.symlink_to(other, target_is_directory=True)
        case "parent_file":
            parent.rename(other)
            parent.touch(mode=0o600)
    for reply in [
        client.get(ROUTE, headers=headers(True)),
        client.put(ROUTE, headers=headers(True), json=INITIAL),
    ]:
        assert reply.status_code == 503
        assert reply.json() == {"code": "model_settings_unavailable"}
    if defect in {"missing", "parent_file"}:
        assert not selection_path.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not-json",
        b'{"model":"x","model":"y","revision":1}',
        b'{"model":"x","revision":true}',
        b'{"model":"x","revision":0}',
        json.dumps(INITIAL).encode("utf-16"),
        b'{"model":"x","revision":1,"api_key":"PRIVATE"}',
        b" " * 4097,
    ],
)
def test_invalid_file_never_reset_or_echoed(api, selection_path, raw):
    client, _wire = api
    selection_path.write_bytes(raw)
    assert client.get(ROUTE, headers=headers(True)).json() == {"code": "model_settings_unavailable"}
    assert client.put(ROUTE, headers=headers(True), json=INITIAL).json() == {
        "code": "model_settings_unavailable"
    }
    assert selection_path.read_bytes() == raw


def test_atomic_replace_failure_cleans_up_and_restart_can_retry(api, selection_path, monkeypatch):
    client, _wire = api

    def failed_replace(*_args, **_kwargs):
        raise OSError("PRIVATE")

    desired = {"model": "alternate", "revision": 1}
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failed_replace)
        reply = client.put(ROUTE, headers=headers(True), json=desired)
        assert reply.status_code == 503
        assert reply.json() == {"code": "model_settings_unavailable"}
    assert json.loads(selection_path.read_bytes()) == INITIAL
    assert list(selection_path.parent.iterdir()) == [selection_path]
    assert (
        ModelSettings(selection_path).update(ModelSelection.model_validate(desired)).revision == 2
    )


def test_post_replace_sync_failure_requires_fresh_read(api, selection_path, monkeypatch):
    client, _wire = api
    sync = os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("PRIVATE")
        sync(fd)

    desired = {"model": "alternate", "revision": 1}
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory_sync)
        reply = client.put(ROUTE, headers=headers(True), json=desired)
        assert reply.status_code == 503
        assert reply.json() == {"code": "model_settings_unavailable"}
    assert client.get(ROUTE, headers=headers(True)).json() == {"model": "alternate", "revision": 2}
    assert client.put(ROUTE, headers=headers(True), json=desired).status_code == 409


def test_uses_stable_directory_lock_with_bounded_wait(selection_path):
    directory = os.open(selection_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        with pytest.raises(ModelSettingsUnavailable):
            ModelSettings(selection_path).read()
    finally:
        os.close(directory)
    assert ModelSettings(selection_path).read().revision == 1


def test_compare_and_swap_across_processes(selection_path):
    script = """
import sys
from pathlib import Path
from ear_api.model_settings import ModelSelection, ModelSettings, ModelSettingsConflict
sys.stdin.readline()
try:
    desired = ModelSelection(model=sys.argv[2], revision=1)
    print(ModelSettings(Path(sys.argv[1])).update(desired).model)
except ModelSettingsConflict:
    print('conflict')
"""
    with (
        subprocess.Popen(
            [sys.executable, "-c", script, str(selection_path), "first"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as first,
        subprocess.Popen(
            [sys.executable, "-c", script, str(selection_path), "second"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as second,
    ):
        for child in (first, second):
            assert child.stdin is not None
            child.stdin.write("go\n")
            child.stdin.flush()
        outcomes = {child.communicate(timeout=10)[0].strip() for child in (first, second)}
        assert first.returncode == second.returncode == 0
    persisted = ModelSettings(selection_path).read()
    assert persisted.revision == 2
    assert persisted.model in {"first", "second"}
    assert outcomes == {"conflict", persisted.model}
