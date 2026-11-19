"""Exercise deployment setup with protected synthetic mounts and fake upstreams."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import runpy
import sys
from contextlib import nullcontext
from pathlib import Path

import httpx
import pytest
from ear_contracts import new_id
from ear_llm_gateway import cli
from ear_llm_gateway.journal import Journal
from ear_storage_client import MemoryStorage
from fastapi.testclient import TestClient

RUNTIME = Path(__file__).resolve().parents[1] / "deploy/api_gateway_runtime.py"
PROJECTION = "/run/config/model-settings/settings.json"
DIRECT_TOKEN = "/run/secrets/newapi-token"
STORAGE_TOKEN = "/run/secrets/gateway-storage-token"
KEY = "synthetic-file-key"
PRIVATE = "synthetic-private-config"
BOOTSTRAP = {
    "storage_url": "http://synthetic.test",
    "aliases": {"default": "gpt-6-luna"},
    "openai_compatible": True,
}


@pytest.fixture
def runtime(monkeypatch):
    spec = importlib.util.spec_from_file_location("synthetic_gateway_runtime", RUNTIME)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mounts(runtime, tmp_path, monkeypatch):
    def private_file(name, value):
        p = tmp_path / name
        p.write_bytes(value)
        p.chmod(0o600)
        return p

    config = private_file("config.json", json.dumps(BOOTSTRAP).encode())
    projection = private_file("settings.json", b'{"model":"gpt-6-luna","revision":1}')
    key = private_file("key", (KEY + "\n").encode())
    storage = private_file("storage", b"s" * 43)
    paths = {str(config): config, PROJECTION: projection, DIRECT_TOKEN: key, STORAGE_TOKEN: storage}
    reads = []
    actual_read = runtime.protected_file

    def read(path, limit=65536, **kwargs):
        reads.append(path)
        return actual_read(str(paths[path]), limit, **kwargs)

    monkeypatch.setattr(runtime, "protected_file", read)
    monkeypatch.setenv("EAR_GATEWAY_CONFIG", str(config))
    monkeypatch.setenv("EAR_GATEWAY_STORAGE_TOKEN", "not-the-storage-secret")
    monkeypatch.setattr(sys, "argv", [str(RUNTIME)])
    return config, projection, key, reads


@pytest.mark.parametrize(
    "override",
    [
        {"openai_compatible": False},
        {"openai_compatible": 1},
        {"openai_compatible": "true"},
        {"openai_compatible": None},
        {"provider_factory": "ear_llm_gateway.octopus:create_provider"},
        {"provider_factory": "custom:create"},
        {"octopus": None},
        {"aliases": {}},
        {"aliases": {"default": "other"}},
        {"aliases": {"default": "gpt-6-luna", "another": "other"}},
        {"aliases": "gpt-6-luna"},
        {"origin": "https://alternate.invalid"},
        {"newapi_token": PRIVATE},
    ],
)
def test_invalid_opt_in_or_ambiguous_config_does_not_open_mounts(
    override, runtime, mounts, capsys, monkeypatch
):
    config, _, _, reads = mounts
    config.write_text(json.dumps({**BOOTSTRAP, **override}))
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("must not start CLI"))
    assert runtime.main() == 1
    assert reads == [str(config)]
    assert capsys.readouterr().err == '{"code":"gateway_container_unavailable"}\n'


def test_internal_direct_factory_cannot_be_selected_as_custom(runtime, mounts):
    with pytest.raises(ValueError, match="invalid provider factory"):
        runtime.configure_provider({"provider_factory": f"__main__:{runtime.DIRECT_FACTORY_NAME}"})
    assert mounts[-1] == []


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b'{"model":"test"}',
        b'{"revision":1}',
        b'{"model":"test","revision":0}',
        b'{"model":"test","revision":9223372036854775808}',
        b'{"model":"https://elsewhere.invalid","revision":1}',
        b'{"model":"test","revision":-1}',
        b'{"model":"test","revision":true}',
        b'{"model":"test","revision":1.0}',
        b'{"model":"test","revision":"1"}',
        b'{"model":"test","revision":NaN}',
        b'{"model":"test","revision":1,"revision":2}',
        b'{"model":"test","model":"wrong","revision":1}',
        b'{"model":"test","revision":1,"token":"synthetic-private-config"}',
        b'{"model":"","revision":1}',
        b'{"model":17,"revision":1}',
        b'{"model":"-test","revision":1}',
        b'{"model":"test\\n","revision":1}',
        b'{"model":"test?url","revision":1}',
        b'{"model":"test\\u00e9","revision":1}',
        b'{"model":"' + b"x" * 129 + b'","revision":1}',
        b" " * 4097,
    ],
)
def test_invalid_projection_has_no_bootstrap_fallback(raw, runtime, mounts, capsys, monkeypatch):
    config, projection, _, reads = mounts
    projection.write_bytes(raw)
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("must not start CLI"))
    assert runtime.main() == 1
    assert reads == [str(config), PROJECTION]
    assert capsys.readouterr().err == '{"code":"gateway_container_unavailable"}\n'


@pytest.mark.parametrize("mount", [1, 2], ids=["projection", "key"])
@pytest.mark.parametrize("fault", ["missing", "0640", "0400", "symlink", "hardlink", "uid", "fifo"])
def test_direct_mounts_require_exact_0600_owner_and_regular_single_link(
    mount, fault, runtime, mounts, monkeypatch, capsys
):
    path = mounts[mount]
    if fault == "missing":
        path.unlink()
    elif fault == "symlink":
        target = path.with_suffix(".target")
        path.rename(target)
        path.symlink_to(target)
    elif fault == "hardlink":
        os.link(path, path.with_suffix(".another"))
    elif fault == "uid":
        different_uid = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: different_uid)
        reader = runtime.protected_file

        def read(path_arg, *args, **kwargs):
            # Only the selected mount should appear to have the wrong owner.
            with monkeypatch.context() as patch:
                if path_arg != (PROJECTION if mount == 1 else DIRECT_TOKEN):
                    patch.setattr(os, "geteuid", lambda: different_uid - 1)
                return reader(path_arg, *args, **kwargs)

        monkeypatch.setattr(runtime, "protected_file", read)
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.chmod(int(fault, 8))
    if mount == 2:
        monkeypatch.setattr(cli, "RemoteStorageClient", lambda *a, **kw: pytest.fail("no storage"))
    else:
        monkeypatch.setattr(cli, "main", lambda: pytest.fail("must not start CLI"))
    assert runtime.main() == 1
    diagnostic = capsys.readouterr().err
    assert diagnostic.endswith('{"code":"gateway_container_unavailable"}\n')
    assert KEY not in diagnostic and PRIVATE not in diagnostic


@pytest.mark.parametrize("raw", [b"", b"wrong\r\nline\n", b"__ bad__", b"\xff", b"x" * 8197])
def test_bad_key_never_uses_environment_or_calls_storage(raw, runtime, mounts, monkeypatch, capsys):
    _, _, key, reads = mounts
    key.write_bytes(raw)
    monkeypatch.setenv("EAR_NEWAPI_TOKEN", "synthetic-env-must-not-be-used")
    monkeypatch.setattr(cli, "RemoteStorageClient", lambda *a, **kw: pytest.fail("no storage"))
    assert runtime.main() == 1
    assert reads.count(DIRECT_TOKEN) == 1
    diagnostic = capsys.readouterr().err
    assert diagnostic == (
        "gateway startup/runtime failed; inspect deployment health without secrets\n"
        '{"code":"gateway_container_unavailable"}\n'
    )


def test_real_cli_uses_projection_snapshot_until_restart(
    runtime, mounts, monkeypatch, tmp_path, caplog
):
    _, projection, _, reads = mounts
    model_a = "model.provider:one/test@a+-"
    model_b = "B" * 128
    projection.write_text(json.dumps({"model": model_a, "revision": 1}))
    store = MemoryStorage()
    requests = []
    runs = []
    monkeypatch.delenv("EAR_NEWAPI_TOKEN", raising=False)
    for name in ("httpcore", "httpx"):
        caplog.set_level(logging.DEBUG, logger=name)
    monkeypatch.setattr(cli, "Journal", lambda _: Journal(str(tmp_path / "journal.sqlite3")))

    class Writer:
        def __getattr__(self, name):
            return getattr(store.client("llm_gateway"), name)

        def close(self):
            pass

    def storage(token, origin, *, module="extraction"):
        assert origin.rstrip("/") == "http://synthetic.test"
        assert token == {"llm_gateway": "s", "extraction": "c"}[module] * 43
        return Writer() if module == "llm_gateway" else nullcontext(store.client(module))

    def upstream(request):
        assert str(request.url) == "https://newapi.hangjuan.cn/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer " + KEY
        assert KEY not in request.content.decode()
        model = json.loads(request.content)["model"]
        requests.append(model)
        response = {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": '{"accepted":true}',
                    },
                }
            ],
        }
        # These are the log namespaces HTTPX and HTTP Core use. Upstream-controlled
        # header/exception text must not surface even under DEBUG deployment logging.
        logging.getLogger("httpcore.http11").debug("header %s%s", PRIVATE, KEY)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(response).encode()),
        )

    def serve(app, **kwargs):
        assert kwargs["workers"] == 1 and kwargs["access_log"] is False
        assert KEY not in repr(sys.argv) + repr(os.environ)
        assert "EAR_NEWAPI_TOKEN" not in os.environ
        assert len(requests) == (0 if not runs else 2)
        alias = model_b if runs else model_a
        assert json.loads(sys.argv[sys.argv.index("--aliases") + 1]) == {"default": alias}
        payload = {
            "module": "extraction",
            "purpose": "synthetic",
            "root_feedback_id": "system",
            "depth": 0,
            "data": {},
        }
        auth = {"Authorization": "Bearer " + "c" * 43}
        with TestClient(app) as client:
            response = client.post(
                "/v1/complete", json={**payload, "call_id": new_id(), "model": alias}, headers=auth
            )
            assert response.json() == {"code": "unknown_model_alias"}
            for attempt in range(1 if runs else 2):
                if attempt:
                    new_projection = projection.with_suffix(".tmp")
                    new_projection.write_text(json.dumps({"model": model_b, "revision": 2}))
                    new_projection.chmod(0o600)
                    new_projection.replace(projection)
                response = client.post(
                    "/v1/complete", json={**payload, "call_id": new_id()}, headers=auth
                )
                assert response.status_code == 200 and response.json()["output"] == {
                    "accepted": True
                }
                assert requests[-1] == alias
        runs.append(alias)

    monkeypatch.setattr(cli, "RemoteStorageClient", storage)
    monkeypatch.setattr(cli.uvicorn, "run", serve)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(upstream))
    for _ in range(2):
        sys.argv = [str(RUNTIME)]
        assert runtime.main() == 0
    assert reads.count(PROJECTION) == reads.count(DIRECT_TOKEN) == 2
    assert reads.count(STORAGE_TOKEN) == 2
    assert requests == [model_a, model_a, model_b]
    assert KEY not in caplog.text and PRIVATE not in caplog.text


def test_script_main_resolves_internal_factory(monkeypatch, tmp_path, capsys):
    # Test the Docker entrypoint execution mode (__main__) without /run mounts.
    # A fake CLI resolves the exact import path; no provider creation/I/O needed.
    runtime_token = "s" * 43
    path = tmp_path / "config.json"
    path.write_text(json.dumps(BOOTSTRAP))
    path.chmod(0o600)
    monkeypatch.setenv("EAR_GATEWAY_CONFIG", str(path))
    monkeypatch.setenv("EAR_GATEWAY_STORAGE_TOKEN", "unset-by-fixture")
    monkeypatch.setattr(sys, "argv", [str(RUNTIME)])
    descriptor_map = {
        PROJECTION: tmp_path / "projection",
        STORAGE_TOKEN: tmp_path / "storage",
    }
    for fixed, value in (
        (PROJECTION, '{"model":"test","revision":1}'),
        (STORAGE_TOKEN, runtime_token),
    ):
        descriptor_map[fixed].write_text(value)
        descriptor_map[fixed].chmod(0o600)
    actual_open = os.open

    def fake_open(path, flags, *args, **kwargs):
        return actual_open(descriptor_map.get(path, path), flags, *args, **kwargs)

    def launch():
        target = sys.argv[sys.argv.index("--provider-factory") + 1]
        module, name = target.split(":", 1)
        assert module == "__main__" and callable(getattr(sys.modules[module], name))
        assert json.loads(sys.argv[sys.argv.index("--aliases") + 1]) == {"default": "test"}

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(cli, "main", launch)
    with pytest.raises(SystemExit) as err:
        runpy.run_path(str(RUNTIME), run_name="__main__")
    assert err.value.code == 0
    assert not capsys.readouterr().err
