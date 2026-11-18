"""Synthetic, serial safety tests. Never deploy, read real input or contact a source.

The HTTP smoke uses a loopback fixture + ordinary Evidence/embedding APIs,
InboxAudioLocator and the adapter's normal storage interface (MemoryStorage).
This is NOT proof of real source authorization, deployment or LLM extraction.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "real_runtime_under_test", ROOT / "tools/real_sample_runtime.py"
)
assert spec and spec.loader
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
CANARY = "SYNTHETIC_SECRET_MUST_NOT_APPEAR"


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail("No real Docker, source SSH or subprocess calls allowed")

    monkeypatch.setattr(runtime, "command", refused)
    monkeypatch.setattr(runtime, "check_ports", lambda: None)
    monkeypatch.setattr(runtime, "check_budget", lambda: None)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "require_fresh", lambda: None)
    path = tmp_path / "private"
    runtime.prepare(path, cosine=0.9, gps=100.0)
    return path


def options():
    return {
        "project": runtime.PROJECT,
        "cosine_threshold": 0.9,
        "gps_threshold_m": 100.0,
        "storage_port": 18100,
        "api_port": 18186,
    }


def bundle():
    return {
        "credentials": {
            role: {"token": char * 43}
            for role, char in zip(
                ("adapter", "api_user", "attribution", "dependency"), "abcd", strict=True
            )
        }
    }


def rewrite(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def test_plan_has_no_effects_or_import_escape(tmp_path, capsys):
    assert runtime.main(["plan", "--state", str(tmp_path / "not-created")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert not (tmp_path / "not-created").exists()
    assert not result["started"]
    assert not result["real_input"]["authorization_proved"]
    assert not result["real_input"]["import_command_available"]
    assert set(result["disabled"]) == {"extraction", "gateway", "geocoder", "external_actions"}
    assert "not LLM" in result["real_input"]["result_kind"]
    assert "unchanged" in result["real_input"]["gps_gate"]
    assert (
        "consumer" in result["real_input"]["consumer_gate"]
        or "healthy" in result["real_input"]["consumer_gate"]
    )


@pytest.mark.parametrize("path", ["relative", "/", "/tmp/../private", str(ROOT / "private")])
def test_bad_state_paths(path):
    with pytest.raises(runtime.Refused):
        runtime.state_path(path)


@pytest.mark.parametrize(
    "cosine,gps",
    [
        (True, 10),
        (0.9, False),
        (float("nan"), 1),
        (0.9, float("inf")),
        (-1.1, 10),
        (1.1, 10),
        (0, -1),
    ],
)
def test_thresholds_fail_before_any_io(tmp_path, cosine, gps):
    with pytest.raises(runtime.Refused):
        runtime.prepare(tmp_path / "new", cosine=cosine, gps=gps)
    assert not (tmp_path / "new").exists()


def test_prepare_private_non_overwriting_and_valid(state):
    assert state.stat().st_mode & 0o777 == 0o700
    for name in ("runtime.env", "options.json"):
        assert (state / name).stat().st_mode & 0o777 == 0o600
    runtime.validate_state(state)
    original = (state / "runtime.env").read_bytes()
    with pytest.raises(FileExistsError):
        runtime.prepare(state, cosine=0.8, gps=90)
    assert (state / "runtime.env").read_bytes() == original
    with pytest.raises(FileExistsError):
        runtime.publish(state / "runtime.env", b"overwrite")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o660])
def test_private_file_mode(state, mode):
    path = state / "runtime.env"
    path.chmod(mode)
    with pytest.raises(runtime.Refused):
        runtime.read_private(path)


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "ancestor_link", "fifo", "directory", "oversize"]
)
def test_private_file_safety(state, tmp_path, kind):
    path = state / "unsafe"
    if kind == "symlink":
        path.symlink_to(state / "runtime.env")
    elif kind == "hardlink":
        os.link(state / "runtime.env", path)
    elif kind == "ancestor_link":
        alias = tmp_path / "alias"
        alias.symlink_to(state, target_is_directory=True)
        path = alias / "runtime.env"
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    else:
        runtime.publish(path, b"x" * 65537)
    with pytest.raises((runtime.Refused, OSError)):
        runtime.read_private(path)


def test_duplicate_json_and_env_rejected(state):
    runtime.publish(state / "duplicates.json", b'{"a":1,"a":2}')
    with pytest.raises(runtime.Refused):
        runtime.load_private(state / "duplicates.json")
    path = state / "runtime.env"
    with path.open("a") as f:
        f.write("EAR_API_PORT=18186\n")
    with pytest.raises(runtime.Refused):
        runtime.validate_state(state)


@pytest.mark.parametrize(
    "key,value",
    [
        ("cosine_threshold", True),
        ("gps_threshold_m", -1),
        ("project", "ear-dev"),
        ("api_port", 8086),
    ],
)
def test_state_tampering_rejected(state, key, value):
    data = runtime.load_private(state / "options.json")
    data[key] = value
    rewrite(state / "options.json", data)
    with pytest.raises(runtime.Refused):
        runtime.validate_state(state)


def test_clean_environment_ignores_runtime_injection(monkeypatch):
    for key in (
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "COMPOSE_FILE",
        "COMPOSE_PROFILES",
        "EAR_API_PORT",
        "HTTP_PROXY",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, CANARY)
    env = runtime.clean_environment()
    assert CANARY not in str(env)
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert env["COMPOSE_DISABLE_ENV_FILE"] == "1"


def test_command_captures_failures_and_uses_explicit_cwd(monkeypatch, capsys):
    # Access original function through the module reloaded without the autouse stub.
    assert spec and spec.loader
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=1, stdout=CANARY.encode(), stderr=CANARY.encode())

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(original.Refused) as exc:
        original.command(["unused"])
    assert CANARY not in str(exc.value)
    assert seen["capture_output"] and seen["cwd"] == ROOT
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("action", ["initialize", "up", "import", "down", "--unknown"])
def test_cli_fail_closed_sanitized_no_side_effects(tmp_path, capsys, action):
    assert runtime.main([action, "--state", str(tmp_path / CANARY)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == runtime.ERROR
    assert CANARY not in captured.err


def test_frozen_source_refuses_unmerged_paths(monkeypatch):
    monkeypatch.setattr(runtime, "command", lambda *a, **kw: "services/api/tests/test_api.py\n")
    with pytest.raises(runtime.Refused, match="integration_conflicts"):
        runtime.source_digest()


def test_source_digest_tracks_code_not_docs_or_tests(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("first")
    (tmp_path / "README.md").write_text("notes")
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(
        runtime, "command", lambda args: "" if "diff" in args else "app.py\nREADME.md\n"
    )
    before = runtime.source_digest()
    (tmp_path / "README.md").write_text("changed notes")
    assert runtime.source_digest() == before
    (tmp_path / "app.py").write_text("second")
    assert runtime.source_digest() != before


def normalized_compose(state):
    """Contract fixture, NOT a substitute for a real Compose config acceptance."""
    config = {
        "name": runtime.PROJECT,
        "services": {},
        "volumes": {},
        "networks": {},
        "secrets": {
            key: {"environment": "EAR_" + key.upper()}
            for key in ("postgres_password", "service_db_password", "nats_token")
        },
    }
    config["secrets"]["real_options"] = {"file": str(state / "options.json")}
    for kind, names in (("volumes", runtime.VOLUMES), ("networks", ("backend", "frontend"))):
        config[kind] = {
            name: {
                "name": f"{runtime.PROJECT}_{name}",
                **({"internal": True} if kind == "networks" else {}),
            }
            for name in names
        }
    for name in runtime.SERVICES + runtime.INIT:
        image = "ear-storage:" + runtime.PROJECT
        if name == "postgres":
            image = runtime.BASE_IMAGES["POSTGRES_IMAGE"]
        elif name == "nats":
            image = runtime.BASE_IMAGES["NATS_IMAGE"]
        elif name == "api":
            image = "ear-api:" + runtime.PROJECT
        elif name.startswith("mvp-"):
            image = "ear-mvp-worker:" + runtime.PROJECT
        service = {
            "image": image,
            "user": "10001:10001",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "read_only": True,
            "mem_limit": 256 * 1024 * 1024,
            "cpus": 1.0,
            "pids_limit": 128,
            "logging": {"driver": "none"},
            "networks": {"backend": None},
        }
        if name in {"storage", "api"}:
            service["ports"] = [
                {
                    "host_ip": "127.0.0.1",
                    "published": "18100" if name == "storage" else "18186",
                    "target": 8000 if name == "storage" else 8086,
                }
            ]
        config["services"][name] = service
    return config


def test_resolved_compose_contract(state, monkeypatch):
    config = normalized_compose(state)
    monkeypatch.setattr(runtime, "command", lambda *a, **kw: json.dumps(config))
    runtime.validate_compose(state, "")


@pytest.mark.parametrize(
    "mutation",
    [
        "egress",
        "external_volume",
        "extra_service",
        "public_port",
        "target_port",
        "privileged",
        "host_network",
        "unbounded_memory",
        "unbounded_pids",
        "source_bind",
        "unknown_volume",
        "secret_file",
        "logging",
        "old_image",
        "runtime_root",
        "writable_root",
        "caps",
    ],
)
def test_resolved_compose_rejects_dangerous_drift(state, monkeypatch, mutation):
    c = normalized_compose(state)
    api = c["services"]["api"]
    if mutation == "egress":
        c["networks"]["frontend"]["internal"] = False
    elif mutation == "external_volume":
        c["volumes"]["identities"]["external"] = True
    elif mutation == "extra_service":
        c["services"]["gateway"] = copy.deepcopy(api)
    elif mutation == "public_port":
        api["ports"][0]["host_ip"] = "0.0.0.0"
    elif mutation == "target_port":
        api["ports"][0]["target"] = 22
    elif mutation == "privileged":
        api["privileged"] = True
    elif mutation == "host_network":
        api["network_mode"] = "host"
    elif mutation == "unbounded_memory":
        api["mem_limit"] = 0
    elif mutation == "unbounded_pids":
        api["pids_limit"] = -1
    elif mutation == "source_bind":
        api["volumes"] = [
            {
                "type": "bind",
                "source": "/var/run/docker.sock",
                "target": "/socket",
                "read_only": True,
            }
        ]
    elif mutation == "unknown_volume":
        api["volumes"] = [{"type": "volume", "source": "ear-dev_data", "target": "/data"}]
    elif mutation == "secret_file":
        c["secrets"]["real_options"]["file"] = "/etc/private"
    elif mutation == "logging":
        api["logging"]["driver"] = "json-file"
    elif mutation == "old_image":
        api["image"] = "ear-api:ear-dev"
    elif mutation == "runtime_root":
        api["user"] = "0:0"
    elif mutation == "writable_root":
        api["read_only"] = False
    else:
        api["cap_add"] = ["SYS_ADMIN"]
    monkeypatch.setattr(runtime, "command", lambda *a, **kw: json.dumps(c))
    with pytest.raises(runtime.Refused):
        runtime.validate_compose(state, "")


def test_overlay_static_no_source_provider_and_bounded_runtime():
    config = yaml.safe_load((ROOT / "deploy/compose.real-sample.yaml").read_text())
    assert not set(config["services"]) & {
        "gateway",
        "geocoder",
        "extraction",
        "adapter",
        "mvp-extraction",
    }
    assert config["networks"]["frontend"]["internal"] is True
    for name in runtime.SERVICES:
        assert config["services"][name]["logging"]["driver"] == "none"
    for name in ("mvp-attribution", "mvp-dependency", "api"):
        svc = config["services"][name]
        assert svc["read_only"] and svc["pids_limit"] == 128 and svc["cpus"] == 1
        assert svc["user"] == "10001:10001"
    assert config["services"]["bootstrap"]["command"] == ["bootstrap-core"]


@pytest.mark.parametrize("service", ["init-core-identities", "real-sample-config"])
def test_init_extends_names_base_file_explicitly(service):
    # Compose resolves same-file extends before merging -f overlays. The parent
    # exists only in compose.yaml, so omitting file fails with the real CLI.
    overlay = yaml.safe_load((ROOT / "deploy/compose.real-sample.yaml").read_text())
    assert overlay["services"][service]["extends"] == {
        "file": "compose.yaml",
        "service": "init-identities",
    }


def test_real_compose_resolves_cross_file_extends(state, monkeypatch):
    """Opt-in local config parsing only: no daemon, deployment, pulls or real secrets."""
    binary = os.environ.get("EAR_REAL_SAMPLE_TEST_COMPOSE_BIN")
    if not binary:
        pytest.skip("explicit local Compose binary required for config-only regression")
    calls = []

    def config_only(argv, **kwargs):
        assert argv[-5:] == ["--profile", "init", "config", "--format", "json"]
        assert argv[0] == binary
        calls.append(argv)
        result = subprocess.run(
            argv,
            capture_output=True,
            cwd=ROOT,
            env=runtime.clean_environment(),
            timeout=30,
            check=False,
        )
        # Never let captured config or diagnostics (which can contain secrets)
        # reach pytest's assertion rendering.
        if result.returncode:
            pytest.fail("local Compose config parsing failed", pytrace=False)
        return result.stdout.decode()

    monkeypatch.setattr(runtime, "command", config_only)
    runtime.validate_compose(state, binary)
    assert len(calls) == 1


def test_configs_only_new_normal_user_and_worker_identities():
    configs = runtime.make_configs(bundle(), options())
    assert set(configs) == {"api", "attribution", "dependency"}
    assert len(configs["api"]["bindings"]) == 1
    binding = configs["api"]["bindings"][0]
    assert binding["role"] == "user"
    assert binding["token"] != binding["storage_token"]
    assert configs["api"]["execution_action_v2"] is False
    for role in ("attribution", "dependency"):
        assert configs[role]["storage_token"] == bundle()["credentials"][role]["token"]
        assert not set(configs[role]) & {"gateway_url", "model", "schedule"}


def test_configs_reject_duplicated_credentials():
    b = bundle()
    b["credentials"]["api_user"]["token"] = b["credentials"]["adapter"]["token"]
    with pytest.raises(runtime.Refused):
        runtime.make_configs(b, options())


def initialize_fakes(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "source_digest", lambda: "source-hash")
    monkeypatch.setattr(runtime, "image_ids", lambda: {"image": "image-hash"})
    monkeypatch.setattr(runtime, "validate_compose", lambda *a: None)
    monkeypatch.setattr(
        runtime, "resources", lambda: {"containers": {}, "volumes": {}, "networks": {}}
    )

    def command(args, **kwargs):
        calls.append(args)
        if "container-export" in args:
            return json.dumps(
                {
                    "project": runtime.PROJECT,
                    "adapter_storage_token": "a" * 43,
                    "api_user_token": "e" * 43,
                    "api_url": "http://127.0.0.1:18186",
                    "storage_url": "http://127.0.0.1:18100",
                }
            )
        return ""

    monkeypatch.setattr(runtime, "command", command)
    return calls


def test_initialization_order_restart_and_no_automatic_resume(state, monkeypatch):
    calls = initialize_fakes(monkeypatch)
    runtime.initialize(state, "", integration_ready=True)
    runs = [args for args in calls if "run" in args]
    assert [args[-1] for args in runs] == [*runtime.INIT, "container-export"]
    assert all(
        "--no-deps" in args and "--pull" in args and "never" in args and "-T" in args
        for args in runs
    )
    assert all("build" not in args and "down" not in args for args in calls)
    assert runtime.load_private(state / "initialized.json")["source_sha256"] == "source-hash"
    assert (state / "clients.json").stat().st_mode & 0o777 == 0o600
    before = len(calls)
    with pytest.raises(FileExistsError):
        runtime.initialize(state, "", integration_ready=True)
    assert len(calls) == before
    runtime.up(state, "", integration_ready=True)
    assert "--no-recreate" in calls[-1] and "--no-deps" in calls[-1]
    assert calls[-1][-len(runtime.SERVICES) :] == list(runtime.SERVICES)


def test_failure_retains_intent_and_never_retries(state, monkeypatch):
    initialize_fakes(monkeypatch)
    calls = []

    def fail(args, **kwargs):
        calls.append(args)
        raise runtime.Refused("failure")

    monkeypatch.setattr(runtime, "command", fail)
    with pytest.raises(runtime.Refused):
        runtime.initialize(state, "", integration_ready=True)
    assert (state / "initialization-intent.json").exists()
    assert not (state / "initialized.json").exists()
    with pytest.raises(FileExistsError):
        runtime.initialize(state, "", integration_ready=True)
    assert len(calls) == 1


def test_start_refuses_source_drift_and_receipt_mismatch(state, monkeypatch):
    calls = initialize_fakes(monkeypatch)
    runtime.initialize(state, "", integration_ready=True)
    before = len(calls)
    monkeypatch.setattr(runtime, "source_digest", lambda: "changed")
    with pytest.raises(runtime.Refused):
        runtime.up(state, "", integration_ready=True)
    assert len(calls) == before
    rewrite(state / "initialization-intent.json", {"source_sha256": "bad", "images": {}})
    with pytest.raises(runtime.Refused, match="initialization_receipt_mismatch"):
        runtime.up(state, "", integration_ready=True)


def test_inventory_only_selects_fixed_project(monkeypatch):
    names = f"ear-dev-storage-1\nunrelated\n{runtime.PROJECT}-storage-1\n"
    monkeypatch.setattr(
        runtime, "command", lambda args: names if f"name={runtime.PROJECT}" in args else ""
    )
    result = runtime.inventory()
    assert all(values == [f"{runtime.PROJECT}-storage-1"] for values in result.values())


def test_unknown_project_resource_refused(monkeypatch):
    monkeypatch.setattr(
        runtime, "inventory", lambda: {"containers": [f"{runtime.PROJECT}-gateway-1"]}
    )
    with pytest.raises(runtime.Refused, match="unexpected_project_resource"):
        runtime.resources()


@contextmanager
def synthetic_source(*, deny_embeddings=False):
    from ear_adapter.testing import embedding_json, evidence_json

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.headers.get("Authorization") != "Bearer synthetic-source-only":
                code, body = 401, {}
            elif self.path == "/api/captures/cap-1/evidence":
                code, body = 200, evidence_json()
            elif self.path.startswith("/api/evidence/speaker_resolution/") and self.path.endswith(
                "/embedding"
            ):
                code, body = (
                    (403, {})
                    if deny_embeddings
                    else (200, embedding_json(self.path.split("/")[-2]))
                )
            else:
                code, body = 404, {}
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def synthetic_runner(tmp_path, url):
    from ear_adapter import AdapterConfig, HttpEarSource, InboxAudioLocator, Runner
    from ear_adapter.testing import AUDIO_BYTES
    from ear_storage_client import MemoryStorage

    audio = tmp_path / "inbox/brain/ios-test/2026-10-26/collection/105538__GROUP-1/sample.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(AUDIO_BYTES)
    storage = MemoryStorage().client("adapter")
    return Runner(
        source=HttpEarSource(url, "synthetic-source-only", timeout=2),
        storage=storage,
        locator=InboxAudioLocator(tmp_path),
        config=AdapterConfig(data_root=tmp_path),
    ), storage


def test_explicit_bounded_evidence_embedding_locator_path_no_new_model(tmp_path):
    from ear_adapter import AlreadyIngested, Ingested, Runner
    from ear_storage_client import Query

    with synthetic_source() as (url, calls):
        runner, storage = synthetic_runner(tmp_path, url)
        report = runner.run_once(capture_ids=["cap-1"])
        assert len(report.outcomes) == 1 and isinstance(report.outcomes[0], Ingested)
        counts = {
            kind: storage.count(Query(kind))
            for kind in ("audio_ref", "voiceprint", "transcript_segment")
        }
        assert all(counts.values())
        restarted = Runner(
            source=runner.source, storage=storage, locator=runner.locator, config=runner.config
        )
        again = restarted.run_once(capture_ids=["cap-1"])
        assert isinstance(again.outcomes[0], AlreadyIngested)
        assert counts == {kind: storage.count(Query(kind)) for kind in counts}
        assert "/api/captures" not in calls
        assert any(path.endswith("/embedding") for path in calls)
        assert all(
            path == "/api/captures/cap-1/evidence"
            or path.startswith("/api/evidence/speaker_resolution/")
            for path in calls
        )


def test_embedding_denial_never_falls_back_to_db_or_artifacts(tmp_path):
    import urllib.error

    from ear_storage_client import Query

    with synthetic_source(deny_embeddings=True) as (url, calls):
        runner, storage = synthetic_runner(tmp_path, url)
        with pytest.raises(urllib.error.HTTPError) as exc:
            runner.run_once(capture_ids=["cap-1"])
        assert exc.value.code == 403
        assert storage.count(Query("audio_ref")) == 0
        assert "/api/captures" not in calls


def test_freshness_checks_even_stopped_resources(monkeypatch):
    # Use the unpatched implementation; no Docker command is executed.
    assert spec and spec.loader
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    monkeypatch.setattr(
        original, "inventory", lambda: {"containers": [], "volumes": ["existing"], "networks": []}
    )
    with pytest.raises(original.Refused, match="project_already_exists"):
        original.require_fresh()


def test_volume_recreation_metadata_changes_identity(monkeypatch):
    name = runtime.PROJECT + "_postgres_data"
    monkeypatch.setattr(runtime, "inventory", lambda: {"volumes": [name]})
    item = {
        "Name": name,
        "CreatedAt": "2026-11-18T00:00:00Z",
        "Driver": "local",
        "Labels": {"com.docker.compose.project": runtime.PROJECT},
    }
    monkeypatch.setattr(runtime, "command", lambda *a: json.dumps([item]))
    first = runtime.resources()
    item["CreatedAt"] = "2026-11-18T01:00:00Z"
    assert runtime.resources() != first
    item["Labels"]["com.docker.compose.project"] = "ear-dev"
    with pytest.raises(runtime.Refused, match="resource_owner_mismatch"):
        runtime.resources()


def test_image_identity_checks_include_base_images_and_protect_ear_dev(monkeypatch):
    calls = []

    def command(args):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return "sha256:" + ("a" if args[3].startswith("ear-") else "b") * 64
        if "ps" in args:
            return "protected-container"
        return "sha256:" + "a" * 64

    monkeypatch.setattr(runtime, "command", command)
    with pytest.raises(runtime.Refused, match="protected_image_reuse"):
        runtime.image_ids()
    assert all(any(image in args for args in calls) for image in runtime.BASE_IMAGES.values())


def test_bad_client_export_keeps_intent_but_not_clients(state, monkeypatch):
    calls = initialize_fakes(monkeypatch)
    original = runtime.command

    def command(args, **kwargs):
        if "container-export" in args:
            return json.dumps({"admin_token": CANARY})
        return original(args, **kwargs)

    monkeypatch.setattr(runtime, "command", command)
    with pytest.raises(runtime.Refused, match="invalid_client_export"):
        runtime.initialize(state, "", integration_ready=True)
    assert calls and (state / "initialization-intent.json").exists()
    assert not (state / "clients.json").exists()
    assert not (state / "initialized.json").exists()


def test_compose_binary_must_be_explicit_safe_file(state, tmp_path):
    for value in ("relative-compose", "/tmp/../compose"):
        with pytest.raises(runtime.Refused):
            runtime.dc(state, value)
    binary = tmp_path / "compose"
    binary.write_text("synthetic, never executed")
    binary.chmod(0o755)
    assert runtime.dc(state, str(binary))[0] == str(binary)
    binary.chmod(0o777)
    with pytest.raises(runtime.Refused):
        runtime.dc(state, str(binary))


def test_directory_lock_blocks_concurrent_initialization(state, monkeypatch):
    import fcntl

    calls = initialize_fakes(monkeypatch)
    with runtime.directory(state, private=True) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            runtime.initialize(state, "", integration_ready=True)
    assert calls == []
    assert not (state / "initialization-intent.json").exists()


def test_latest_gps_extraction_gate_remains_in_source():
    # This runtime must not weaken or fake geocoding to make extraction pass.
    text = (ROOT / "services/extraction/src/ear_extraction/segmentation.py").read_text()
    assert 'unreferenced_by=("gps_geocode", "gps_point_id")' in text
    assert 'Waiting("geocode_pending")' in text
    assert "mvp-extraction" not in runtime.SERVICES
