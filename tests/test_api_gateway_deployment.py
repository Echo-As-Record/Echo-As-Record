"""Opt-in, synthetic-only image smoke; default tests never build/start containers.

Run after root lock/sync and explicit independent image builds:
EAR_RUN_API_GATEWAY_DEPLOY_TESTS=1 EAR_API_TEST_IMAGE=ear-api:<task-tag>
EAR_GATEWAY_TEST_IMAGE=ear-gateway:<task-tag> EAR_STORAGE_TEST_IMAGE=ear-storage:<task-tag>
pytest tests/test_api_gateway_deployment.py
EAR_COMPOSE_BIN can identify a standalone Compose executable.
No production endpoints, paid models, calendars, migrations or automatic grants.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml
from ear_api.app import create_app
from ear_api.config import Settings
from ear_api.upstream import Upstream
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 43
WRITER = "b" * 43
API_TOKEN = "c" * 43
API_STORAGE = "d" * 43


def settings(**changes):
    return Settings.model_validate(
        {
            "storage_url": "http://synthetic.test",
            "bindings": [
                {
                    "subject": "synthetic",
                    "role": "admin",
                    "token": API_TOKEN,
                    "storage_token": API_STORAGE,
                }
            ],
            "timeout": 0.2,
            **changes,
        }
    )


def test_bundled_static_default_and_explicit_disable():
    for enabled in (True, False):
        config = settings(**({} if enabled else {"static_dir": None}))
        upstream = Mock()
        with TestClient(create_app(config, upstream=upstream)) as client:
            page = client.get("/plugins/")
            assert page.status_code == (200 if enabled else 404)
            if enabled:
                assert "<html" in page.text.lower()
                assert client.get("/plugins/app.js").status_code == 200
                assert client.get("/plugins/../../config.py").status_code == 404
        upstream.ready.assert_not_called()


def test_health_is_generic_and_does_not_authorize_or_initialize():
    seen = []

    def unavailable(request):
        seen.append(request.url.path)
        raise httpx.ConnectError("SYNTHETIC_PRIVATE_ERROR", request=request)

    config = settings()
    with TestClient(
        create_app(config, upstream=Upstream(config, httpx.MockTransport(unavailable)))
    ) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert seen == []
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable", "scope": "api_storage_connectivity"}
        assert response.headers["cache-control"] == "no-store"
        assert seen == ["/v1/identity"]
        assert "SYNTHETIC_PRIVATE_ERROR" not in response.text


def runtime_module():
    spec = importlib.util.spec_from_file_location(
        "ear_deploy_gateway", ROOT / "deploy/api_gateway_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("kind", ["public", "symlink", "hardlink", "oversize", "fifo"])
def test_gateway_secret_loader_rejects_unsafe_files(tmp_path, kind):
    module = runtime_module()
    path = tmp_path / "config"
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b"x" * (65537 if kind == "oversize" else 1))
        path.chmod(0o644 if kind == "public" else 0o600)
        if kind == "symlink":
            alias = tmp_path / "alias"
            alias.symlink_to(path)
            path = alias
        elif kind == "hardlink":
            os.link(path, tmp_path / "other")
    with pytest.raises((OSError, ValueError)):
        module.protected_file(str(path))


def test_gateway_secret_loader_and_startup_errors_are_redacted(tmp_path, monkeypatch, capsys):
    module = runtime_module()
    path = tmp_path / "config"
    path.write_text('{"secret":"SYNTHETIC_PRIVATE_ERROR"}')
    path.chmod(0o600)
    assert module.protected_file(str(path)) == path.read_bytes()
    monkeypatch.setenv("EAR_GATEWAY_CONFIG", str(path))
    monkeypatch.setattr(sys, "argv", ["gateway"])
    assert module.main() == 1
    assert capsys.readouterr().err == '{"code":"gateway_container_unavailable"}\n'
    with pytest.raises(ValueError):
        json.loads('{"aliases":{},"aliases":{}}', object_pairs_hook=module.unique_object)


def test_overlay_policy_keeps_original_default_services():
    base = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())
    overlay = yaml.safe_load((ROOT / "deploy/compose.api-gateway.yaml").read_text())
    assert set(base["services"]) & set(overlay["services"]) == set()
    assert {name for name, svc in base["services"].items() if not svc.get("profiles")} == {
        "postgres",
        "nats",
        "storage",
        "relay",
        "manage-worker",
    }
    for name, service in overlay["services"].items():
        assert service["profiles"] == ["api-gateway"]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["depends_on"] == {"storage": {"condition": "service_healthy"}}
        assert "environment" not in service and "command" not in service
        assert any("/state" in str(v) for v in service["volumes"])
        assert "ports" not in service or name == "api"
    assert overlay["services"]["gateway"]["deploy"]["replicas"] == 1
    assert all("file" in secret for secret in overlay["secrets"].values())


def compose_command():
    binary = os.environ.get("EAR_COMPOSE_BIN") or shutil.which("docker-compose")
    if binary:
        return [binary]
    docker = shutil.which("docker")
    if docker:
        result = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=10)
        if result.returncode == 0:
            return [docker, "compose"]
    pytest.skip("Compose CLI unavailable; set EAR_COMPOSE_BIN to test real merge")


def test_compose_cli_merge_does_not_modify_storage_services(tmp_path):
    command = compose_command()
    placeholder = tmp_path / "synthetic-config.json"
    placeholder.write_text("{}")
    environment = {
        **os.environ,
        "EAR_POSTGRES_PASSWORD": "synthetic-only",
        "EAR_SERVICE_DB_PASSWORD": "synthetic-only",
        "EAR_NATS_TOKEN": "synthetic-only",
        "EAR_API_CONFIG_FILE": str(placeholder),
        "EAR_GATEWAY_CONFIG_FILE": str(placeholder),
        "EAR_GATEWAY_STORAGE_TOKEN_FILE": str(placeholder),
        "EAR_OCTOPUS_TOKEN_FILE": str(placeholder),
    }

    def compile_config(files):
        result = subprocess.run(
            [
                *command,
                "--env-file",
                "/dev/null",
                "--profile",
                "*",
                *[value for p in files for value in ("-f", str(ROOT / p))],
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        assert result.returncode == 0, "Compose rejected isolated synthetic configuration"
        return json.loads(result.stdout)

    base = compile_config(["deploy/compose.yaml"])
    merged = compile_config(["deploy/compose.yaml", "deploy/compose.api-gateway.yaml"])
    for name, service in base["services"].items():
        assert merged["services"][name] == service
    for name in ("api", "gateway"):
        assert merged["services"][name]["profiles"] == ["api-gateway"]
    assert all(v["type"] == "volume" for v in merged["services"]["gateway"]["volumes"])
    builtin = compile_config(
        [
            "deploy/compose.yaml",
            "deploy/compose.api-gateway.yaml",
            "deploy/compose.gateway-octopus.yaml",
        ]
    )
    assert any(v["target"] == "octopus-token" for v in builtin["services"]["gateway"]["secrets"])
    environment["EAR_GATEWAY_PROVIDER_DIR"] = str(tmp_path)
    custom = compile_config(
        [
            "deploy/compose.yaml",
            "deploy/compose.api-gateway.yaml",
            "deploy/compose.gateway-provider.yaml",
        ]
    )
    assert any(v["target"] == "/opt/ear/provider" for v in custom["services"]["gateway"]["volumes"])
    for config in (builtin, custom):
        for name, service in base["services"].items():
            assert config["services"][name] == service


SYNTHETIC_STORAGE = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ear_contracts import is_id
from ear_storage_client import MemoryStorage
from ear_storage_client.wire import decode_read, decode_request, encode_read_result, encode_result
store = MemoryStorage()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def reply(self, status, body):
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == '/health/ready': return self.reply(200, {'status':'ready'})
        if self.path != '/v1/identity': return self.reply(404, {})
        module = {'a'*43:'extraction','b'*43:'llm_gateway','d'*43:'api'}.get(
            self.headers.get('Authorization','').removeprefix('Bearer '))
        if module is None: return self.reply(401, {})
        return self.reply(200, {'codec':1,'module':module})
    def do_POST(self):
        module = {'a'*43:'extraction','b'*43:'llm_gateway','d'*43:'api'}.get(
            self.headers.get('Authorization','').removeprefix('Bearer '))
        if module is None: return self.reply(401, {})
        body = self.rfile.read(int(self.headers['Content-Length']))
        client = store.client(module)
        try:
            if self.path == '/v1/llm/call-id-check':
                if module != 'llm_gateway': return self.reply(403, {'code':'access_denied'})
                value = json.loads(body)
                if (not isinstance(value, dict) or set(value) != {'call_id'}
                        or not is_id(value['call_id'])):
                    return self.reply(400, {'code':'invalid_call_id'})
                return self.reply(200, {'occupied':client.call_id_occupied(value['call_id'])})
            if self.path == '/v1/read':
                parsed, values = decode_read(body)
                result = getattr(client, parsed.method)(**values)
                return self.reply(200, encode_read_result(parsed.method, result))
            if self.path == '/v1/batches':
                data = json.loads(body)
                data.pop('request_id')
                ops, context = decode_request(json.dumps(data))
                return self.reply(200, encode_result(client.write(ops, context)))
            return self.reply(404, {})
        except Exception:
            return self.reply(503, {'code':'synthetic_storage_failed'})
ThreadingHTTPServer(('0.0.0.0',8000), Handler).serve_forever()
"""

SYNTHETIC_PROVIDER = """from ear_llm_gateway.models import ProviderResult
class Provider:
    def complete(self, call, *, model, timeout):
        return ProviderResult({"synthetic": True})
def create_provider():
    return Provider()
"""


SYNTHETIC_OCTOPUS = r"""
import json, runpy, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/api/v1/llm/chat/v1/chat/completions'
        assert self.headers.get('Authorization') == 'Bearer synthetic-octopus-only'
        assert request['model'] == 'synthetic/model'
        assert request['provider'] == 'openai'
        reply = {'id':'chatcmpl-synthetic', 'object':'chat.completion', 'created':1,
                 'model':'synthetic/model', 'choices':[{'index':0, 'finish_reason':'stop',
                 'message':{'role':'assistant', 'content':'{"synthetic":true}'}}],
                 'usage':{'prompt_tokens':0,'completion_tokens':1,'total_tokens':1}}
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server = ThreadingHTTPServer(('127.0.0.1',18080),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
sys.argv = ['/app/api_gateway_runtime.py']
runpy.run_path('/app/api_gateway_runtime.py',run_name='__main__')
"""


@pytest.mark.skipif(
    os.environ.get("EAR_RUN_API_GATEWAY_DEPLOY_TESTS") != "1",
    reason="explicit opt-in required for isolated synthetic container smoke",
)
@pytest.mark.parametrize("builtin", [False, True], ids=["custom", "builtin-octopus"])
def test_built_images_nonroot_secrets_static_health_and_persistent_recovery(tmp_path, builtin):
    from ear_contracts import new_id

    api_image = os.environ.get("EAR_API_TEST_IMAGE")
    gateway_image = os.environ.get("EAR_GATEWAY_TEST_IMAGE")
    storage_image = os.environ.get("EAR_STORAGE_TEST_IMAGE")
    missing = [
        name
        for name, value in (
            ("EAR_API_TEST_IMAGE", api_image),
            ("EAR_GATEWAY_TEST_IMAGE", gateway_image),
            ("EAR_STORAGE_TEST_IMAGE", storage_image),
        )
        if not value
    ]
    assert not missing, "explicit task-tag images required: " + ", ".join(missing)
    prefix = "ear-api-gateway-test-" + uuid.uuid4().hex[:12]
    containers, volumes = [], []
    hardened = [
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "256m",
        "--pids-limit",
        "128",
    ]

    def docker(*args, payload=None, timeout=25, check=True):
        result = subprocess.run(
            ["docker", *args], input=payload, capture_output=True, text=True, timeout=timeout
        )
        if check:
            assert result.returncode == 0, "isolated Docker operation failed: " + args[0]
        return result

    def volume(suffix):
        name = prefix + "-" + suffix
        docker("volume", "create", "--label", "ear.task=" + prefix, name)
        volumes.append(name)
        return name

    def run(name, image, *args, entrypoint=None, command=()):
        containers.append(name)
        options = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "ear.task=" + prefix,
            "--network",
            prefix,
            "--read-only",
            *hardened,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
        ]
        if entrypoint:
            options += ["--entrypoint", entrypoint]
        docker(*options, *args, image, *command)

    def secret_volume(suffix, image, files):
        name = volume(suffix)
        docker(
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            *hardened,
            "-v",
            name + ":/state",
            "--entrypoint",
            "python",
            image,
            "-c",
            "import json,sys,pathlib,os,stat; s=os.stat('/state'); "
            "assert s.st_uid == 10001 and stat.S_IMODE(s.st_mode) == 0o700; "
            "files=json.load(sys.stdin); "
            "[(pathlib.Path('/state',k).write_text(v), "
            "pathlib.Path('/state',k).chmod(0o600)) for k,v in files.items()]",
            payload=json.dumps(files),
        )
        return name

    def request(name, port, path, body=None, token=TOKEN):
        source = f"""import json, urllib.request, urllib.error
r = urllib.request.Request(
    'http://127.0.0.1:{port}{path}',
    data={json.dumps(body).encode()!r} if {body is not None!r} else None,
    headers={{'Authorization': 'Bearer ' + {token!r}, 'Content-Type': 'application/json'}},
)
try:
    with urllib.request.urlopen(r, timeout=2) as response:
        print(json.dumps([response.status, response.read().decode()]))
except urllib.error.HTTPError as error:
    print(json.dumps([error.code, error.read().decode()]))
"""
        return json.loads(docker("exec", name, "python", "-c", source, timeout=8).stdout)

    def wait_live(name, port):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                assert request(name, port, "/health/live")[0] == 200
                return
            except (AssertionError, ValueError):
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                if not state["Running"]:
                    break
                time.sleep(0.2)
        # Only these isolated synthetic containers; never change production logging.
        logs = docker("logs", name, check=False)
        diagnostic = tmp_path / (name + ".log")
        descriptor = os.open(diagnostic, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(logs.stdout + logs.stderr)
        pytest.fail(f"isolated service not live; private synthetic log: {diagnostic}")

    try:
        for image in (api_image, gateway_image):
            config = json.loads(docker("image", "inspect", image).stdout)[0]["Config"]
            assert config["User"] == "10001:10001"
            assert not any(TOKEN in value or WRITER in value for value in config["Env"])
            assert config["Healthcheck"]["Timeout"] == 4_000_000_000
        # COPY must not inherit a private host umask for non-secret executable code.
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            *hardened,
            "--entrypoint",
            "python",
            gateway_image,
            "-c",
            "import os,stat,pathlib; p=pathlib.Path('/app/api_gateway_runtime.py'); "
            "s=p.stat(); assert os.geteuid()==10001 and s.st_uid==0; "
            "assert stat.S_IMODE(s.st_mode)==0o444; p.read_text()",
        )
        docker("network", "create", "--internal", "--label", "ear.task=" + prefix, prefix)
        script = tmp_path / "synthetic_storage.py"
        script.write_text(SYNTHETIC_STORAGE)
        script.chmod(0o644)
        provider = tmp_path / "synthetic_provider.py"
        provider.write_text(SYNTHETIC_PROVIDER)
        provider.chmod(0o644)
        storage = prefix + "-storage"
        containers.append(storage)
        docker(
            "run",
            "-d",
            "--name",
            storage,
            "--label",
            "ear.task=" + prefix,
            "--network",
            prefix,
            "--network-alias",
            "storage",
            *hardened,
            "--read-only",
            "--entrypoint",
            "python",
            "-v",
            str(script) + ":/synthetic.py:ro",
            storage_image,
            "/synthetic.py",
        )
        launcher = tmp_path / "synthetic_octopus.py"
        launcher.write_text(SYNTHETIC_OCTOPUS)
        launcher.chmod(0o644)
        provider_config = (
            {
                "octopus": {
                    "origin": "http://127.0.0.1:18080",
                    "models": {"synthetic/model": "openai"},
                    "service": "synthetic/test",
                }
            }
            if builtin
            else {"provider_factory": "synthetic_provider:create_provider"}
        )
        gateway_secrets = secret_volume(
            "gateway-secrets",
            gateway_image,
            {
                "gateway-config.json": json.dumps(
                    {
                        "storage_url": "http://storage:8000",
                        **provider_config,
                        "aliases": {"default": "synthetic/model"},
                    }
                ),
                "gateway-storage-token": WRITER,
                **({"octopus-token": "synthetic-octopus-only"} if builtin else {}),
            },
        )
        raw = {
            "storage_url": "http://storage:8000",
            "bindings": [
                {
                    "subject": "synthetic",
                    "role": "admin",
                    "token": API_TOKEN,
                    "storage_token": API_STORAGE,
                }
            ],
            "timeout": 0.2,
            "execution_journal": "/state/execution.sqlite3",
        }
        api_secrets = secret_volume("api-secrets", api_image, {"api-config.json": json.dumps(raw)})
        journal = volume("gateway-journal")
        execution = volume("api-journal")
        call_id = new_id()
        seed = f"""import os,stat
s=os.stat('/state')
assert s.st_uid == 10001 and stat.S_IMODE(s.st_mode) == 0o700
from datetime import UTC,datetime
from ear_contracts import LLMCallRecord
from ear_llm_gateway.journal import Journal
j=Journal('/state/gateway.sqlite3')
n=datetime.now(UTC)
r=LLMCallRecord(caller_module='extraction',purpose='synthetic_test',model='synthetic/model',root_feedback_id='system',depth=0,started_at=n,ended_at=n,status='failed',error_code='provider_outcome_unknown')
j.reserve({call_id!r},r,global_rpm=60,caller_rpm=20)
j.close()
"""
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            *hardened,
            "-v",
            journal + ":/state",
            "--entrypoint",
            "python",
            gateway_image,
            "-c",
            seed,
        )
        gateway = prefix + "-gateway"
        gateway_mounts = [
            "-v",
            gateway_secrets + ":/run/secrets:ro",
            "-v",
            journal + ":/state",
        ]
        if builtin:
            gateway_mounts += ["-v", str(launcher) + ":/synthetic_octopus.py:ro"]
            gateway_options = {"entrypoint": "python", "command": ("/synthetic_octopus.py",)}
        else:
            gateway_mounts += ["-v", str(provider) + ":/opt/ear/provider/synthetic_provider.py:ro"]
            gateway_options = {}
        run(gateway, gateway_image, *gateway_mounts, **gateway_options)
        wait_live(gateway, 8077)
        result = request(gateway, 8077, f"/v1/calls/{call_id}/reconcile", {})
        assert result[0] == 200
        assert json.loads(result[1])["error_code"] == "provider_outcome_unknown"
        occupancy = "/v1/llm/call-id-check"
        assert request(storage, 8000, occupancy, {"call_id": call_id})[0] == 403
        assert request(storage, 8000, occupancy, {"call_id": "invalid"}, WRITER)[0] == 400
        status, body = request(storage, 8000, occupancy, {"call_id": call_id}, WRITER)
        assert status == 200 and json.loads(body) == {"occupied": True}
        fresh_id = new_id()
        status, body = request(storage, 8000, occupancy, {"call_id": fresh_id}, WRITER)
        assert status == 200 and json.loads(body) == {"occupied": False}
        call = {
            "call_id": fresh_id,
            "module": "extraction",
            "purpose": "synthetic_test",
            "root_feedback_id": "system",
            "depth": 0,
            "data": {"prompt": "SYNTHETIC_BODY"},
        }
        status, body = request(gateway, 8077, "/v1/complete", call)
        assert status == 200 and json.loads(body)["output"] == {"synthetic": True}
        competitor = docker(
            "run",
            "--rm",
            "--network",
            prefix,
            *hardened,
            *gateway_mounts,
            *(["--entrypoint", "python"] if builtin else []),
            gateway_image,
            *(["/synthetic_octopus.py"] if builtin else []),
            check=False,
        )
        assert competitor.returncode != 0
        assert WRITER not in competitor.stdout + competitor.stderr
        assert "gateway_container_unavailable" in competitor.stderr
        docker("stop", "--time", "3", gateway)
        docker("rm", gateway)
        containers.remove(gateway)
        run(gateway, gateway_image, *gateway_mounts, **gateway_options)
        wait_live(gateway, 8077)
        assert request(gateway, 8077, "/v1/complete", call)[0] == 409
        api = prefix + "-api"
        run(api, api_image, "-v", api_secrets + ":/run/secrets:ro", "-v", execution + ":/state")
        wait_live(api, 8086)
        assert request(api, 8086, "/plugins/")[0] == 200
        assert request(api, 8086, "/health/ready")[0] == 200
        for name in (api, gateway):
            inspected = json.loads(docker("inspect", name).stdout)[0]
            assert inspected["HostConfig"]["CapDrop"] == ["ALL"]
            assert "no-new-privileges:true" in inspected["HostConfig"]["SecurityOpt"]
            assert inspected["HostConfig"]["ReadonlyRootfs"] is True
            assert not inspected["HostConfig"]["PortBindings"]
            assert "synthetic-octopus-only" not in json.dumps(inspected["Config"])
            state = docker(
                "exec",
                name,
                "python",
                "-c",
                "import os,stat; s=os.stat('/state'); "
                "assert s.st_uid == 10001 and stat.S_IMODE(s.st_mode) == 0o700",
            )
            assert state.returncode == 0
            logs = docker("logs", name)
            assert not any(
                value in logs.stdout + logs.stderr
                for value in (
                    TOKEN,
                    WRITER,
                    API_TOKEN,
                    API_STORAGE,
                    "SYNTHETIC_BODY",
                    "synthetic-octopus-only",
                )
            )
        docker("stop", "--time", "3", storage)
        started = time.monotonic()
        assert request(api, 8086, "/health/ready")[0] == 503
        assert time.monotonic() - started < 5
        # Gateway health is expressly liveness, not storage/provider readiness.
        assert request(gateway, 8077, "/health/live")[0] == 200
    finally:
        for name in reversed(containers):
            docker("rm", "-f", name, check=False)
        for name in reversed(volumes):
            docker("volume", "rm", name, check=False)
        docker("network", "rm", prefix, check=False)


def test_api_cli_serves_one_worker_without_implicit_initialization(tmp_path, monkeypatch):
    from ear_api import cli

    raw = {
        "storage_url": "http://synthetic.test",
        "bindings": [
            {
                "subject": "synthetic",
                "role": "admin",
                "token": API_TOKEN,
                "storage_token": API_STORAGE,
            }
        ],
    }
    config = tmp_path / "api.json"
    config.write_text(json.dumps(raw))
    config.chmod(0o600)
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    monkeypatch.setattr(sys, "argv", ["ear-api", "serve", "--config", str(config)])
    assert cli.main() == 0
    assert run.call_args.kwargs["workers"] == 1
    assert run.call_args.kwargs["access_log"] is False
    app = run.call_args.args[0]
    assert any(getattr(route, "path", None) == "/plugins" for route in app.routes)
    assert app.state.execution_http_enabled is False


def test_journal_crash_is_recovered_as_audit_without_provider_execution(tmp_path):
    from datetime import UTC, datetime

    from ear_contracts import LLMCallRecord, new_id
    from ear_llm_gateway.journal import Journal
    from ear_llm_gateway.service import Gateway
    from ear_storage_client import MemoryStorage

    path = str(tmp_path / "gateway.sqlite3")
    call_id = new_id()
    now = datetime.now(UTC)
    record = LLMCallRecord(
        caller_module="extraction",
        purpose="synthetic_test",
        model="synthetic/model",
        root_feedback_id="system",
        depth=0,
        started_at=now,
        ended_at=now,
        status="failed",
        error_code="provider_outcome_unknown",
    )
    journal = Journal(path)
    try:
        journal.reserve(call_id, record, global_rpm=60, caller_rpm=20)
        row = journal.get(call_id, "extraction")
        assert row is not None
        request_id = row["request_id"]
        with pytest.raises(ValueError, match="journal_already_owned"):
            Journal(path)
    finally:
        journal.close()
    store = MemoryStorage()
    provider = Mock()
    journal = Journal(path)
    try:
        row = journal.get(call_id, "extraction")
        assert row is not None and row["phase"] == "pending"
        gateway = Gateway(
            callers=Mock(),
            writer=store.client("llm_gateway"),
            provider=provider,
            journal=journal,
            aliases={"default": "synthetic/model"},
        )
        assert gateway.recover_pending(limit=1) == 1
        assert gateway.recover_pending(limit=1) == 0
        row = journal.get(call_id, "extraction")
        assert row is not None
        assert row["phase"] == "saved" and row["request_id"] == request_id
        saved = store.client("llm_gateway").get_system_record(call_id)
        assert saved is not None and saved.payload["error_code"] == "provider_outcome_unknown"
        provider.complete.assert_not_called()
    finally:
        journal.close()


def test_api_readiness_real_tcp_timeout_is_bounded():
    import socket
    import threading

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        stop = threading.Event()

        def stall():
            connection, _ = listener.accept()
            with connection:
                stop.wait(3)

        thread = threading.Thread(target=stall, daemon=True)
        thread.start()
        config = settings(storage_url=f"http://127.0.0.1:{listener.getsockname()[1]}")
        try:
            with TestClient(create_app(config)) as client:
                started = time.monotonic()
                response = client.get("/health/ready")
                assert response.status_code == 503
                assert time.monotonic() - started < 2
                assert client.get("/health/live").status_code == 200
        finally:
            stop.set()
            thread.join(timeout=4)
        assert not thread.is_alive()


def test_builtin_factory_reads_protected_token_without_custom_directory(monkeypatch):
    module = runtime_module()
    calls = []

    def read(path, limit):
        calls.append((path, limit))
        return b"synthetic-octopus-only"

    monkeypatch.setattr(module, "protected_file", read)
    for key in ("TOKEN", "ORIGIN", "MODELS", "SERVICE"):
        monkeypatch.delenv("EAR_OCTOPUS_" + key, raising=False)
    factory = module.configure_provider(
        {
            "octopus": {
                "origin": "http://127.0.0.1:18080",
                "models": {"synthetic/model": "openai"},
                "service": "synthetic/test",
            }
        }
    )
    assert factory == "ear_llm_gateway.octopus:create_provider"
    assert calls == [("/run/secrets/octopus-token", 8194)]
    from ear_llm_gateway.octopus import create_provider

    provider = create_provider()
    assert "synthetic-octopus-only" not in repr(provider)
    assert module.configure_provider({"provider_factory": "synthetic:create"}) == "synthetic:create"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "options",
    [
        None,
        {},
        {"token": "SYNTHETIC_PRIVATE_ERROR"},
        {"origin": 1, "models": {}, "service": "synthetic/test"},
    ],
)
def test_builtin_bad_config_fails_closed_redacted(tmp_path, monkeypatch, capsys, options):
    module = runtime_module()
    path = tmp_path / "gateway.json"
    path.write_text(
        json.dumps({"storage_url": "http://synthetic.test", "aliases": {}, "octopus": options})
    )
    path.chmod(0o600)
    monkeypatch.setenv("EAR_GATEWAY_CONFIG", str(path))
    monkeypatch.setattr(sys, "argv", ["gateway"])
    assert module.main() == 1
    assert capsys.readouterr().err == '{"code":"gateway_container_unavailable"}\n'


@pytest.mark.parametrize(
    "missing", ["EAR_API_TEST_IMAGE", "EAR_GATEWAY_TEST_IMAGE", "EAR_STORAGE_TEST_IMAGE"]
)
def test_image_smoke_missing_image_fails_before_docker(tmp_path, monkeypatch, missing):
    for key in ("EAR_API_TEST_IMAGE", "EAR_GATEWAY_TEST_IMAGE", "EAR_STORAGE_TEST_IMAGE"):
        monkeypatch.setenv(key, "synthetic:task-tag")
    monkeypatch.delenv(missing)
    docker = Mock(side_effect=AssertionError("Docker must not run"))
    monkeypatch.setattr(subprocess, "run", docker)
    with pytest.raises(AssertionError, match=missing):
        test_built_images_nonroot_secrets_static_health_and_persistent_recovery(tmp_path, False)
    docker.assert_not_called()


@pytest.mark.skipif(
    os.environ.get("EAR_RUN_API_GATEWAY_DEPLOY_TESTS") != "1",
    reason="explicit opt-in required for isolated image permission probe",
)
@pytest.mark.parametrize(
    ("variable", "runtime_path"),
    [
        ("EAR_STORAGE_TEST_IMAGE", "/app/runtime.py"),
        ("EAR_GATEWAY_TEST_IMAGE", "/app/api_gateway_runtime.py"),
        ("EAR_API_TEST_IMAGE", "/app/.venv/bin/ear-api"),
    ],
)
def test_built_image_entrypoint_readable_as_nonroot(variable, runtime_path):
    image = os.environ.get(variable)
    assert image, "explicit task-tag image required: " + variable
    program = (
        "import os,pathlib,stat; "
        f"p=pathlib.Path({runtime_path!r}); s=p.stat(); "
        "assert os.geteuid()==10001 and s.st_uid==0; "
        "assert not (stat.S_IMODE(s.st_mode) & 0o022); "
        "p.read_bytes(); "
        + (
            "assert os.access(p,os.X_OK)"
            if runtime_path.endswith("ear-api")
            else "assert stat.S_IMODE(s.st_mode)==0o444"
        )
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--label",
            "ear.task=ear-image-permissions-" + uuid.uuid4().hex,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--entrypoint",
            "python",
            image,
            "-c",
            program,
        ],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, "nonroot entrypoint permission probe failed: " + variable
