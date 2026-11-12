"""Offline/synthetic host tests: no deployment, real identity, model or personal data."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, Registry, SystemRecord, new_id
from ear_llm_gateway.models import GatewayError
from ear_storage_client import Context, MemoryStorage, Page, PutSystemRecord
from ear_storage_client.remote_errors import ServiceUnavailable
from ear_storage_client.wire import (
    decode_read,
    decode_request,
    encode_read_result,
    encode_result,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/mvp_worker.py"
SECRET = "a" * 43
CANARY = "SYNTHETIC_PRIVATE_ERROR"
spec = importlib.util.spec_from_file_location("ear_test_mvp_worker", SCRIPT)
assert spec and spec.loader
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)


def settings(tmp_path, module="attribution", **changes):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    value = {
        "module": module,
        "storage_url": "http://synthetic.invalid",
        "storage_token": SECRET,
        "consumer": worker.CONSUMERS[module],
        "journal_namespace": "synthetic-deployment",
        "state_dir": str(state),
        "poll_seconds": 0.05,
        "timeout": 1,
    }
    if module == "attribution":
        value.update(cosine_threshold=0.9, gps_threshold_m=100)
    if module == "extraction":
        value.update(gateway_url="http://synthetic-gateway.invalid", model="synthetic")
    return {**value, **changes}


def write_config(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("mode", [0o000, 0o400, 0o640, 0o644, 0o660, 0o700, 0o4600])
def test_config_exact_0600(tmp_path, mode):
    path = write_config(tmp_path, settings(tmp_path))
    path.chmod(mode)
    with pytest.raises((ValueError, OSError)):
        worker.load_config(str(path))


@pytest.mark.parametrize("kind", ["symlink", "ancestor_link", "hardlink", "fifo", "directory"])
def test_config_rejects_unsafe_paths(tmp_path, kind):
    path = write_config(tmp_path, settings(tmp_path))
    if kind == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path)
        path = alias
    elif kind == "ancestor_link":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        path = alias / path.name
    elif kind == "hardlink":
        os.link(path, tmp_path / "alias")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path = tmp_path
    with pytest.raises((ValueError, OSError)):
        worker.load_config(str(path))


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_config_untrusted_ancestor(tmp_path, mode):
    ancestor = tmp_path / "unsafe"
    ancestor.mkdir(mode=mode)
    ancestor.chmod(mode)
    path = write_config(ancestor, settings(tmp_path))
    with pytest.raises(ValueError):
        worker.load_config(str(path))


def test_config_wrong_owner_and_ancestor_owner(tmp_path, monkeypatch):
    path = write_config(tmp_path, settings(tmp_path))
    original = os.fstat
    target = path.stat().st_ino

    def foreign(fd):
        info = original(fd)
        if info.st_ino == target:
            values = list(info)
            values[4] = os.geteuid() + 9999
            return os.stat_result(values)
        return info

    monkeypatch.setattr(worker.os, "fstat", foreign)
    with pytest.raises(ValueError):
        worker.load_config(str(path))
    target = tmp_path.stat().st_ino
    with pytest.raises(ValueError):
        worker.load_config(str(path))


@pytest.mark.parametrize("content", ["{" + CANARY, "[]", "x" * 65537, '{"module":1,"module":2}'])
def test_config_failures_redacted(tmp_path, content, capsys):
    path = write_config(tmp_path, {})
    path.write_text(content)
    assert worker.main(["--config", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (worker.ERROR_METADATA + "\n")


@pytest.mark.parametrize("args", [["--token", SECRET], ["--module", CANARY], ["--con", CANARY]])
def test_cli_rejects_secret_arguments_without_echo(args, capsys):
    assert worker.main(args) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.err and CANARY not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "changes",
    [
        {"module": "import.this"},
        {"consumer": "attribution.other"},
        {"full_visibility": True},
        {"from_beginning": True},
        {"provider_factory": "arbitrary:code"},
        {"cosine_threshold": None},
        {"gps_threshold_m": None},
        {"gps_threshold_m": -1},
        {"cosine_threshold": 2},
        {"cosine_threshold": True},
        {"poll_seconds": 0},
        {"timeout": float("nan")},
        {"max_records": True},
        {"max_events_per_tick": True},
        {"max_events_per_tick": 0},
        {"max_events_per_tick": 101},
        {"state_dir": "relative"},
        {"state_dir": "/tmp/../state"},
        {"storage_url": "http://user:secret@localhost"},
        {"storage_url": "http://localhost?secret"},
        {"storage_url": "http://localhost/#secret"},
        {"storage_url": "file:///tmp/test"},
        {"storage_url": "http://localhost/path"},
        {"storage_url": " http://localhost"},
        {"schedule": {"start": "2026-11-12T00:00:00Z", "every_seconds": 10}},
    ],
)
def test_config_fail_closed(tmp_path, changes):
    with pytest.raises(ValueError):
        worker.Settings.model_validate({**settings(tmp_path), **changes})


@pytest.mark.parametrize("module", ["attribution", "extraction", "dependency"])
def test_valid_configs_no_default_timer_or_history(tmp_path, module):
    cfg = worker.load_config(str(write_config(tmp_path, settings(tmp_path, module))))
    assert cfg.schedule is None and "from_beginning" not in cfg.model_fields_set
    assert SECRET not in repr(cfg)


@pytest.mark.parametrize(
    "start", ["2020-01-01", "2020-01-01T00:00:00", "2020-01-01T01:00:00+01:00"]
)
def test_timer_requires_explicit_utc(tmp_path, start):
    value = settings(tmp_path, "extraction", schedule={"start": start, "every_seconds": 60})
    with pytest.raises(ValueError):
        worker.Settings.model_validate(value)


def test_timer_explicit_start_preserved(tmp_path):
    value = settings(
        tmp_path, "extraction", schedule={"start": "2026-11-12T00:00:00Z", "every_seconds": 60}
    )
    cfg = worker.Settings.model_validate(value)
    assert cfg.schedule.schedule().start.isoformat() == "2026-11-12T00:00:00+00:00"


def test_private_state_lock_namespace_and_metadata(tmp_path):
    cfg = worker.Settings.model_validate(settings(tmp_path))
    state = worker.State(cfg)
    try:
        state.health("ready")
        assert worker.healthy(cfg)
        with pytest.raises(BlockingIOError):
            worker.State(cfg)
        body = (state.path / "health.json").read_text()
        for private in (SECRET, cfg.storage_url, cfg.journal_namespace, cfg.state_dir):
            assert private not in body
        assert stat.S_IMODE((state.path / "health.json").stat().st_mode) == 0o600
        state.health("failed")
        assert not worker.healthy(cfg)
    finally:
        state.close()
    other = cfg.model_copy(update={"journal_namespace": "other"})
    with pytest.raises(ValueError):
        worker.State(other)
    # Failed startup does not erase identity evidence or repair permissions.
    assert (Path(cfg.state_dir) / "identity.json").exists()


@pytest.mark.parametrize("mode", [0o750, 0o770, 0o755])
def test_state_exact_0700_not_repaired(tmp_path, mode):
    cfg = worker.Settings.model_validate(settings(tmp_path))
    Path(cfg.state_dir).chmod(mode)
    with pytest.raises(ValueError):
        worker.State(cfg)
    assert stat.S_IMODE(Path(cfg.state_dir).stat().st_mode) == mode


def test_state_replacement_detected(tmp_path):
    cfg = worker.Settings.model_validate(settings(tmp_path))
    state = worker.State(cfg)
    try:
        Path(cfg.state_dir).rename(tmp_path / "old-state")
        Path(cfg.state_dir).mkdir(mode=0o700)
        with pytest.raises(ValueError):
            state.health("ready")
        assert not (Path(cfg.state_dir) / "health.json").exists()
    finally:
        state.close()


def test_startup_and_runtime_exceptions_are_generic(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, settings(tmp_path))
    monkeypatch.setattr(worker, "run", Mock(side_effect=RuntimeError(CANARY + SECRET)))
    assert worker.main(["--config", str(path)]) == 1
    assert capsys.readouterr().err == (worker.ERROR_METADATA + "\n")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_sets_stop_and_restores_handler(signum):
    stop = threading.Event()
    before = signal.getsignal(signum)
    with worker.stopping(stop):
        os.kill(os.getpid(), signum)
        assert stop.is_set()
    assert signal.getsignal(signum) == before


def test_clients_reject_environment_proxy_and_redirect(tmp_path, monkeypatch):
    from ear_llm_gateway.client import GatewayClient

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent-synthetic-certificate")
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://secret-recipient.invalid"})

    cfg = worker.Settings.model_validate(settings(tmp_path))
    state = worker.State(cfg)
    client = worker.PumpStorage(
        SECRET,
        cfg.storage_url,
        transport=httpx.MockTransport(redirect),
        stop=threading.Event(),
        state=state,
    )
    gateway = GatewayClient(cfg.storage_url, SECRET, transport=httpx.MockTransport(redirect))
    try:
        assert client._http.trust_env is False and client._http.follow_redirects is False
        assert gateway._http.trust_env is False and gateway._http.follow_redirects is False
        with pytest.raises(ServiceUnavailable):
            _ = client.module
        with pytest.raises(GatewayError):
            gateway.stats()
        assert len(calls) == 2 and all("secret-recipient" not in url for url in calls)
    finally:
        gateway.close()
        client.close()
        state.close()


@pytest.mark.parametrize("failure", ["handler", "processed_read"])
def test_pump_failure_surfaces_after_nack_and_stop_skips_poll(tmp_path, failure):
    cfg = worker.Settings.model_validate(settings(tmp_path))
    state = worker.State(cfg)
    stop = threading.Event()
    client = worker.PumpStorage(SECRET, cfg.storage_url, stop=stop, state=state)
    calls = []
    from ear_contracts import DataChanged, utcnow

    event = DataChanged(
        event_id=new_id(),
        occurred_at=utcnow(),
        skip_history=False,
        data_type="feedback",
        data_id=new_id(),
        operation="created",
        old_version=None,
        new_version=1,
        root_feedback_id="system",
        depth=0,
    )

    def consumer(operation, name, **values):
        calls.append((operation, name, values))
        if operation == "fetch":
            return {"delivery": {"event": event.model_dump(mode="json"), "delivery_token": SECRET}}
        return {"status": "ready"}

    client._consumer = consumer
    if failure == "handler":
        client.is_processed = lambda *_: False
    else:
        client._read = Mock(side_effect=ServiceUnavailable(CANARY))
    try:
        with pytest.raises(ValueError):
            client.subscribe(cfg.consumer, Mock(), from_beginning=True)
        client.subscribe(cfg.consumer, Mock(side_effect=RuntimeError(CANARY)))
        with pytest.raises(RuntimeError, match="worker_handler_failed"):
            client.poll_once(cfg.consumer)
        assert calls[-1][0] == "nack"
        assert calls[0][2]["from_beginning"] is False
        stop.set()
        count = len(calls)
        assert client.poll_once(cfg.consumer) == 0 and len(calls) == count
    finally:
        client.close()
        state.close()


def record(status="queued", **payload):
    return SystemRecord(
        id=new_id(),
        data_type="extraction_run",
        revision=1,
        writer="extraction",
        event_start=None,
        event_end=None,
        created_at=datetime.fromisoformat("2026-11-12T00:00:00Z"),
        updated_at=datetime.fromisoformat("2026-11-12T00:00:00Z"),
        payload={"status": status, **payload},
    )


def test_extraction_drain_bound_stop_and_full_record_limit(tmp_path):
    cfg = worker.Settings.model_validate(settings(tmp_path, "extraction", max_jobs_per_tick=2))
    client = Mock(module="extraction")
    jobs = [record("succeeded"), record(), record(), record()]
    client.query.return_value = Page(tuple(jobs), None)
    state, stop = Mock(), threading.Event()
    service = worker.BoundedExtraction(client, Mock(), config=cfg, stop=stop, state=state)
    service.process = Mock()
    service.drain()
    assert service.process.call_count == 2
    stop.set()
    service.drain()
    assert service.process.call_count == 2
    stop.clear()
    service.config = cfg.model_copy(update={"max_records": 1})
    with pytest.raises(RuntimeError, match="worker_record_limit"):
        service.drain()
    assert service.process.call_count == 2  # no partial view treated as complete


def test_extraction_gateway_system_and_timer_opt_in(tmp_path, monkeypatch):
    cfg = worker.Settings.model_validate(settings(tmp_path, "extraction"))
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent-synthetic-certificate")
    storage = Mock(module="extraction")
    monkeypatch.setattr(worker, "PumpStorage", Mock(return_value=storage))
    received = []

    def fake_serve(service, stop, **kwargs):
        received.append(kwargs)
        assert service.gateway.system == worker.SYSTEM
        assert service.gateway.model == "synthetic"
        assert service.gateway._http.trust_env is False
        assert service.gateway._http.follow_redirects is False
        stop.set()

    monkeypatch.setattr(worker, "serve", fake_serve)
    worker.run(cfg, threading.Event())
    assert received[0]["schedule"] is None
    assert storage.close.call_count == 1
    timer = worker.Timer(start="2026-11-12T00:00:00Z", every_seconds=60)
    worker.run(cfg.model_copy(update={"schedule": timer}), threading.Event())
    assert received[1]["schedule"].every.total_seconds() == 60


class SyntheticStorage:
    """In-process fake HTTP peer; actual worker stays RemoteStorageClient-only.

    No auth/storage reliability claims: this fixture tests process wiring/lifecycle,
    actual journal replay and strict authority refusal, not PostgreSQL/JetStream.
    """

    def __init__(self, module, *, authority=True):
        specs = dict(DEFAULT_REGISTRY.data_types)
        specs["attribution"] = replace(
            specs["attribution"], requires_dependencies=True, supports_reevaluation=True
        )
        self.registry = Registry(
            specs, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
        ).extend_core(
            DataTypeSpec("attribution_state", "system_record", "attribution"),
            DataTypeSpec("attribution_feedback_result", "system_record", "attribution"),
        )
        self.memory = MemoryStorage(registry=self.registry)
        self.client = self.memory.client(module)
        self.module, self.authority = module, authority
        self.calls, self.receipts, self.batches = [], {}, []
        self.fail_after_commit = False

    def request(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/v1/identity":
            return 200, {"codec": 1, "module": self.module}
        if path == "/v1/registry":
            return 200, {
                "codec": 1,
                "registry": self.registry.to_json(),
                "core_types": sorted(self.registry.core_types),
            }
        if path == "/v1/scheduler/state":
            if not self.authority:
                return 403, {"code": CANARY}
            return 200, {
                "codec": 1,
                "coverage_complete": True,
                "coverage_revision": "synthetic-grants",
                "owners": {},
            }
        if path == "/v1/consumers/register":
            assert body["from_beginning"] is False
            return 200, {"codec": 1, "status": "ready"}
        if path == "/v1/consumers/fetch":
            return 200, {"codec": 1, "status": "idle", "delivery": None}
        if path == "/v1/read":
            request, values = decode_read(json.dumps(body))
            value = getattr(self.client, request.method)(**values)
            return 200, json.loads(encode_read_result(request.method, value))
        if path == "/v1/snapshots":
            return 200, {"codec": 1, "lease": {"snapshot_id": new_id()}}
        if path.startswith("/v1/snapshots/") and method == "DELETE":
            return 200, {"codec": 1}
        if path == "/v1/batches":
            request_id = body.pop("request_id")
            encoded = json.dumps(body, sort_keys=True)
            self.batches.append((request_id, encoded))
            if request_id in self.receipts:
                old, result = self.receipts[request_id]
                assert old == encoded
            else:
                ops, context = decode_request(json.dumps(body))
                result = self.client.write(ops, context)
                self.receipts[request_id] = encoded, result
            if self.fail_after_commit:
                self.fail_after_commit = False
                return 503, {"code": CANARY}
            return 200, json.loads(encode_result(result))
        raise AssertionError("unexpected synthetic request")

    @contextmanager
    def listening(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def handle_request(self):
                assert self.headers.get("Authorization") == "Bearer " + SECRET
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
                )
                status, value = fixture.request(self.command, self.path, body)
                encoded = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = do_POST = do_DELETE = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def launch(path):
    env = dict(os.environ)
    # No deployment/model/test opt-in environment is consumed by the CLI.
    env.update(PYTHONDONTWRITEBYTECODE="1", HTTP_PROXY="http://127.0.0.1:1")
    return subprocess.Popen(
        [sys.executable, str(SCRIPT), "--config", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def wait_ready(process, state):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        assert process.poll() is None, process.communicate(timeout=1)
        try:
            if json.loads((state / "health.json").read_bytes())["status"] == "ready":
                return
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    raise AssertionError("synthetic worker readiness deadline")


def stop_process(process):
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=8)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


@pytest.mark.parametrize("module", ["attribution", "extraction", "dependency"])
def test_real_cli_fake_http_pump_signal_health_no_history(tmp_path, module):
    fixture = SyntheticStorage(module)
    with fixture.listening() as url:
        value = settings(tmp_path, module, storage_url=url)
        path = write_config(tmp_path, value)
        process = launch(path)
        try:
            wait_ready(process, Path(value["state_dir"]))
            check = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(path), "--healthcheck"],
                capture_output=True,
                timeout=5,
            )
            assert check.returncode == 0 and check.stdout == check.stderr == b""
        finally:
            out, err = stop_process(process)
    assert process.returncode == 0 and out == err == b""
    calls = [path for _, path, _ in fixture.calls]
    assert "/v1/consumers/fetch" in calls
    assert not any("history" in path or "complete" in path for path in calls)
    if module == "extraction":
        assert fixture.batches == []  # no implicit timer admitted on startup
    assert json.loads((Path(value["state_dir"]) / "health.json").read_text())["status"] == "stopped"


def test_dependency_remote_authority_denied_no_memory_fallback(tmp_path):
    fixture = SyntheticStorage("dependency", authority=False)
    with fixture.listening() as url:
        path = write_config(tmp_path, settings(tmp_path, "dependency", storage_url=url))
        process = launch(path)
        try:
            out, err = process.communicate(timeout=8)
        finally:
            stop_process(process)
    assert process.returncode == 1 and out == b""
    assert err == (worker.ERROR_METADATA + "\n").encode()
    assert not any(path == "/v1/consumers/register" for _, path, _ in fixture.calls)
    assert not any("PublishReevaluation" in body for _, body in fixture.batches)
    source = SCRIPT.read_text()
    assert "MemoryStorage" not in source and "MemoryClient" not in source


def test_attribution_subprocess_replay_before_subscribe_after_lost_receipt(tmp_path):
    from ear_attribution.transactions import CommitJournal

    fixture = SyntheticStorage("attribution")
    with fixture.listening() as url:
        value = settings(tmp_path, storage_url=url)
        path = write_config(tmp_path, value)
        namespace = json.dumps(
            [value["journal_namespace"], value["module"], value["consumer"], value["storage_url"]]
        )
        journal_path = Path(value["state_dir"]) / "journal"
        journal = CommitJournal(journal_path, namespace=namespace)
        event_id = new_id()
        journal.prepare(
            event_id,
            [PutSystemRecord("attribution_state", new_id(), {"synthetic": True})],
            Context.system(),
        )
        journal.close()
        fixture.fail_after_commit = True
        first = launch(path)
        try:
            _, err = first.communicate(timeout=8)
        finally:
            stop_process(first)
        assert first.returncode == 1 and CANARY.encode() not in err
        assert not any(p == "/v1/consumers/register" for _, p, _ in fixture.calls)
        second = launch(path)
        try:
            wait_ready(second, Path(value["state_dir"]))
        finally:
            out, err = stop_process(second)
        assert second.returncode == 0 and out == err == b""
        assert len(fixture.batches) == 2 and fixture.batches[0] == fixture.batches[1]
        journal = CommitJournal(journal_path, namespace=namespace)
        try:
            assert journal.pending_ids() == []
        finally:
            journal.close()


def test_worker_image_and_opt_in_overlay_static():
    dockerfile = (ROOT / "deploy/Dockerfile.worker").read_text()
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]
    for member in workspace["members"]:
        manifest = f"{member}/pyproject.toml"
        assert f"COPY {manifest} {manifest}" in dockerfile.split("RUN uv sync")[0]
    assert "--locked" in dockerfile and "--no-dev" in dockerfile
    assert "USER 10001:10001" in dockerfile and "COPY --chmod=0444" in dockerfile
    overlay = yaml.safe_load((ROOT / "deploy/compose.mvp-workers.yaml").read_text())
    assert set(overlay["services"]) == {"mvp-attribution", "mvp-extraction", "mvp-dependency"}
    for service in overlay["services"].values():
        assert service["profiles"] == ["mvp-workers"]
        assert service["user"] == "10001:10001" and service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["deploy"]["replicas"] == 1 and service["restart"] == "no"
        assert service["volumes"][0].endswith(":/state")
        assert "environment" not in service and "ports" not in service
    assert len(overlay["volumes"]) == 3


def test_health_is_readonly_rejects_stale_namespace_and_unlocked_state(tmp_path):
    cfg = worker.Settings.model_validate(settings(tmp_path))
    state = worker.State(cfg)
    try:
        state.health("ready")
        assert worker.healthy(cfg)
        assert not worker.healthy(cfg.model_copy(update={"journal_namespace": "wrong"}))
        path = state.path / "health.json"
        data = json.loads(path.read_text())
        data["updated_at"] = time.time() - 10000
        state.write("health.json", data)
        assert not worker.healthy(cfg)
        state.health("ready")
        before = path.read_bytes()
    finally:
        state.close()
    assert not worker.healthy(cfg)  # same PID alive, but no worker lock
    assert path.read_bytes() == before


def test_healthcheck_does_not_connect_or_acquire_worker_state(tmp_path, monkeypatch):
    cfg = worker.Settings.model_validate(settings(tmp_path))
    path = write_config(
        tmp_path, cfg.model_dump(exclude={"storage_token"}) | {"storage_token": SECRET}
    )
    monkeypatch.setattr(worker, "PumpStorage", Mock(side_effect=AssertionError("network")))
    state = worker.State(cfg)
    try:
        state.health("ready")
        assert worker.main(["--config", str(path), "--healthcheck"]) == 0
        worker.PumpStorage.assert_not_called()
    finally:
        state.close()


def test_pre_stopped_worker_does_not_connect_or_recover(tmp_path, monkeypatch):
    cfg = worker.Settings.model_validate(settings(tmp_path))
    storage = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(worker, "PumpStorage", storage)
    stop = threading.Event()
    stop.set()
    worker.run(cfg, stop)
    storage.assert_not_called()
    assert list(Path(cfg.state_dir).iterdir()) == []


@pytest.mark.parametrize("deliveries,expected", [([1] * 10, 4), ([1, 1, 0, 1], 2)])
def test_dependency_poll_batch_is_finite_and_stops_on_idle(
    tmp_path, monkeypatch, deliveries, expected
):
    cfg = worker.Settings.model_validate(settings(tmp_path, "dependency"))
    state = worker.State(cfg)
    stop = threading.Event()
    client = worker.PumpStorage(SECRET, cfg.storage_url, stop=stop, state=state)
    calls = []
    values = iter(deliveries)

    def poll(consumer):
        calls.append(consumer)
        return next(values)

    monkeypatch.setattr(client, "poll_once", poll)
    try:
        assert client.poll_batch(cfg.consumer, max_deliveries=4) == expected
        assert calls == [cfg.consumer] * (4 if expected == 4 else 3)
        stop.set()
        before = len(calls)
        assert client.poll_batch(cfg.consumer, max_deliveries=4) == 0
        assert len(calls) == before
    finally:
        client.close()
        state.close()


def test_dependency_poll_batch_does_not_hide_nack_or_admit_after_stop(tmp_path, monkeypatch):
    cfg = worker.Settings.model_validate(settings(tmp_path, "dependency"))
    state = worker.State(cfg)
    stop = threading.Event()
    client = worker.PumpStorage(SECRET, cfg.storage_url, stop=stop, state=state)
    try:
        poll = Mock(side_effect=RuntimeError("worker_handler_failed"))
        monkeypatch.setattr(client, "poll_once", poll)
        with pytest.raises(RuntimeError, match="worker_handler_failed"):
            client.poll_batch(cfg.consumer, max_deliveries=4)
        assert poll.call_count == 1

        def stop_during_handler(consumer):
            stop.set()
            return 1

        poll = Mock(side_effect=stop_during_handler)
        monkeypatch.setattr(client, "poll_once", poll)
        assert client.poll_batch(cfg.consumer, max_deliveries=4) == 1
        assert poll.call_count == 1
    finally:
        client.close()
        state.close()


def test_dependency_host_batches_admission_before_each_tick(tmp_path, monkeypatch):
    import ear_dependency.remote

    cfg = worker.Settings.model_validate(settings(tmp_path, "dependency", max_events_per_tick=4))
    stop = threading.Event()
    order = []
    client = Mock()
    client.poll_batch.side_effect = lambda *a, **kw: order.append("admit") or 4
    scheduler = Mock()
    scheduler.subscribe.side_effect = lambda: order.append("subscribe")

    def tick():
        order.append("tick")
        stop.set()

    scheduler.tick.side_effect = tick
    monkeypatch.setattr(worker, "PumpStorage", Mock(return_value=client))
    monkeypatch.setattr(
        ear_dependency.remote, "RemoteDependencyService", Mock(return_value=scheduler)
    )
    worker.run(cfg, stop)
    assert order == ["subscribe", "admit", "tick"]
    client.poll_batch.assert_called_once_with(cfg.consumer, max_deliveries=4)
    client.poll_once.assert_not_called()


def test_dependency_default_yields_to_scheduler_after_each_delivery(tmp_path, monkeypatch):
    """Backlogged old roots must get a decision before admitting the next root.

    An explicit batch remains supported, but the default CLI must not collect
    32 deliveries while starving decisions/Finish polling between them.
    """
    import ear_dependency.remote

    cfg = worker.Settings.model_validate(settings(tmp_path, "dependency"))
    stop = threading.Event()
    order = []
    client = Mock()

    def batch(consumer, *, max_deliveries):
        assert consumer == cfg.consumer
        assert max_deliveries == 1
        order.append("delivery")
        return 1

    client.poll_batch.side_effect = batch
    scheduler = Mock()

    def tick():
        order.append("tick")
        if len(order) == 6:
            stop.set()

    scheduler.tick.side_effect = tick
    monkeypatch.setattr(worker, "PumpStorage", Mock(return_value=client))
    monkeypatch.setattr(
        ear_dependency.remote, "RemoteDependencyService", Mock(return_value=scheduler)
    )
    worker.run(cfg, stop)
    assert order == ["delivery", "tick"] * 3
    assert client.poll_batch.call_count == scheduler.tick.call_count == 3
