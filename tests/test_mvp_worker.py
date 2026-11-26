"""Offline/synthetic host tests: no deployment, real identity, model or personal data."""

from __future__ import annotations

import hashlib
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
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import httpx
import pytest
import yaml
from ear_contracts import (
    DEFAULT_REGISTRY,
    DataChanged,
    DataTypeSpec,
    RawRecord,
    Registry,
    SemanticRecord,
    SystemRecord,
    format_utc,
    new_id,
)
from ear_extraction import automatic_timeline, course_blocks, orchestration
from ear_extraction import planning as extraction_planning
from ear_extraction.activity_duration import meets_activity_floor
from ear_extraction.automatic_timeline import prewrite_run_metadata_sha256
from ear_extraction.course_reextract import (
    ReviewedTimelineDraft,
    ReviewedTimelineSpan,
    compile_course_draft,
    course_draft_sha256,
)
from ear_extraction.demo import fixture_registry
from ear_extraction.domain import Activity, Blocked, Command, Interval, Rule
from ear_extraction.reextract_preflight import ExactInterval, PreflightInput
from ear_extraction.reviewed_timeline_handoff import build_reviewed_timeline_draft, project_timeline
from ear_extraction.sessions import pack_session_windows
from ear_extraction.timeline_preview import (
    CompletedReview,
    WindowReview,
    plan_timeline,
)
from ear_llm_gateway.models import GatewayError
from ear_llm_gateway.output_json import OutputSchema
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    MemoryStorage,
    Page,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    Supersede,
    TimeRange,
    Update,
)
from ear_storage_client.remote_errors import ServiceUnavailable
from ear_storage_client.testing import make_raw
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


@pytest.mark.parametrize("value", [0, 661, "330", True, float("inf"), float("nan")])
def test_extraction_rejects_invalid_gateway_timeout(tmp_path, value):
    with pytest.raises(ValueError, match="gateway_timeout"):
        worker.Settings.model_validate(settings(tmp_path, "extraction", gateway_timeout=value))


@pytest.mark.parametrize("module", ["attribution", "dependency"])
def test_gateway_timeout_is_extraction_only(tmp_path, module):
    with pytest.raises(ValueError, match="unexpected_gateway_or_timer"):
        worker.Settings.model_validate(settings(tmp_path, module, gateway_timeout=330))


def test_storage_timeout_ceiling_unchanged_with_gateway_timeout(tmp_path):
    with pytest.raises(ValueError, match=r"(?m)^timeout$"):
        worker.Settings.model_validate(
            settings(tmp_path, "extraction", timeout=121, gateway_timeout=330)
        )


def transcript_only_settings():
    return {
        "transcript_only": True,
        "transcript_summary_rule_id": new_id(),
        "transcript_windows": [{"start": "2026-11-12T00:00:00Z", "end": "2026-11-12T00:15:00Z"}],
    }


def test_transcript_window_list_is_bounded_and_keeps_existing_guards(tmp_path):
    start = datetime.fromisoformat("2026-11-12T00:00:00Z")
    step = timedelta(minutes=15)
    times = [(start + i * step).isoformat().replace("+00:00", "Z") for i in range(26)]
    windows = [{"start": a, "end": b} for a, b in pairwise(times)]
    data = settings(
        tmp_path,
        "extraction",
        **(transcript_only_settings() | {"transcript_windows": windows[:24]}),
    )
    config = worker.Settings.model_validate(data)
    assert len(config.transcript_windows) == 24 and config.schedule is None
    for changes, error in (
        ({"transcript_windows": windows}, "transcript_windows"),
        ({"transcript_windows": [windows[0], windows[0]]}, "transcript_windows_overlap"),
        (
            {"transcript_windows": [{"start": times[0], "end": times[2]}]},
            "transcript_window_too_long",
        ),
        (
            {"schedule": {"start": times[0], "every_seconds": 900}},
            "transcript_only_requires_fixed_rule_and_windows",
        ),
    ):
        with pytest.raises(ValueError, match=error):
            worker.Settings.model_validate(data | changes)


@pytest.mark.parametrize(
    "module,transcript_only,profile",
    [
        ("attribution", False, "attribution"),
        ("dependency", False, None),
        ("extraction", False, None),
        ("extraction", True, "transcript_extraction"),
    ],
)
def test_pump_snapshot_profile_is_selected_by_worker_mode(
    tmp_path, module, transcript_only, profile
):
    config = worker.Settings.model_validate(
        settings(tmp_path, module, **(transcript_only_settings() if transcript_only else {}))
    )
    sid = new_id()
    requests = []

    def request(req):
        requests.append((req.method, req.url.path))
        if req.method == "POST":
            body = {"codec": 1, "ttl_seconds": None}
            if profile is not None:
                body["profile"] = profile
            assert json.loads(req.content) == body
            return httpx.Response(200, json={"codec": 1, "lease": {"snapshot_id": sid}})
        return httpx.Response(200)

    state = worker.State(config)
    try:
        with worker.PumpStorage(
            SECRET,
            config.storage_url,
            stop=threading.Event(),
            state=state,
            transport=httpx.MockTransport(request),
        ) as client:
            if profile is None:
                with (
                    pytest.raises(ValueError, match="requires_transcript_only"),
                    client.snapshot(profile="transcript_extraction"),
                ):
                    pytest.fail("non-transcript worker requested transcript profile")
            with client.snapshot() as view:
                assert view.snapshot_id == sid
    finally:
        state.close()
    assert requests == [("POST", "/v1/snapshots"), ("DELETE", "/v1/snapshots/" + sid)]


@pytest.mark.parametrize(
    "transcript_only,overrides,storage_timeout,gateway_timeout,lease_seconds",
    [
        (False, {}, 35, 35, 900),
        (False, {"timeout": 120}, 120, 120, 900),
        (False, {"gateway_timeout": 330}, 35, 330, 900),
        (False, {"timeout": 120, "gateway_timeout": 660}, 120, 660, 900),
        (True, {}, 35, 35, 1900),
        (True, {"gateway_timeout": 330}, 35, 330, 1900),
        (True, {"gateway_timeout": 330.5, "health_max_age_seconds": 1905}, 35, 330.5, 1905),
        (True, {"gateway_timeout": 630, "health_max_age_seconds": 3500}, 35, 630, 3400),
        (True, {"gateway_timeout": 660, "health_max_age_seconds": 3600}, 35, 660, 3550),
    ],
)
def test_extraction_timeout_and_lease_are_wired_independently_of_storage(
    tmp_path,
    monkeypatch,
    transcript_only,
    overrides,
    storage_timeout,
    gateway_timeout,
    lease_seconds,
):
    data = settings(tmp_path, "extraction")
    del data["timeout"]
    if transcript_only:
        data.update(transcript_only_settings())
    config = worker.load_config(str(write_config(tmp_path, data | overrides)))
    seen = []

    def request(req):
        target = (req.url.host, req.url.path)
        seen.append(target)
        if target == ("synthetic.invalid", "/v1/identity"):
            assert req.extensions["timeout"]["read"] == storage_timeout
            return httpx.Response(200, json={"codec": 1, "module": "extraction"})
        assert target == ("synthetic-gateway.invalid", "/v1/stats")
        assert req.extensions["timeout"]["read"] == gateway_timeout
        return httpx.Response(200, json={"roots": {}})

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(request))

    def serve(service, *_args, **_kwargs):
        assert service.lease_seconds == lease_seconds
        assert service.transcript_only is transcript_only
        assert service.transcript_max_calls == 5
        assert service.gateway.stats() == {"roots": {}}

    monkeypatch.setattr(worker, "serve", serve)
    worker.run(config, threading.Event())
    assert ("synthetic.invalid", "/v1/identity") in seen
    assert seen[-1] == ("synthetic-gateway.invalid", "/v1/stats")


@pytest.mark.parametrize("health", [None, 3399, 3601])
def test_long_transcript_config_requires_sufficient_bounded_health_age(tmp_path, health):
    values = settings(tmp_path, "extraction", gateway_timeout=630, **transcript_only_settings())
    if health is not None:
        values["health_max_age_seconds"] = health
    with pytest.raises(ValueError, match="health_max_age_seconds"):
        worker.Settings.model_validate(values)


def test_long_transcript_job_health_can_cover_one_job_but_not_stay_healthy_forever(
    tmp_path, monkeypatch
):
    cfg = worker.Settings.model_validate(
        settings(
            tmp_path,
            "extraction",
            gateway_timeout=630,
            health_max_age_seconds=3500,
            **transcript_only_settings(),
        )
    )
    fake_time = Mock()
    fake_time.time.return_value = 10_000
    monkeypatch.setattr(worker, "time", fake_time)
    state = worker.State(cfg)
    try:
        state.health("busy")
        fake_time.time.return_value += 3500
        assert worker.healthy(cfg)
        fake_time.time.return_value += 1
        assert not worker.healthy(cfg)
    finally:
        state.close()


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
    from ear_contracts import utcnow

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

    def __init__(self, module, *, authority=True, registry=None):
        specs = dict(DEFAULT_REGISTRY.data_types)
        specs["attribution"] = replace(
            specs["attribution"], requires_dependencies=True, supports_reevaluation=True
        )
        self.registry = registry or Registry(
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


def test_attribution_storage_failure_records_only_safe_first_phase(tmp_path):
    from ear_storage_client.remote_errors import ResourceLimit

    config = worker.Settings.model_validate(settings(tmp_path))
    state = worker.State(config)
    client = worker.PumpStorage(
        SECRET,
        config.storage_url,
        stop=threading.Event(),
        state=state,
        transport=httpx.MockTransport(lambda _: httpx.Response(413, text="private body")),
    )
    try:
        for path in ("/v1/batches", "/v1/consumers/nack"):
            with pytest.raises(ResourceLimit):
                client._request("POST", path, "private payload", request_id=new_id())
        d = json.loads((state.path / "last_storage_failure.json").read_bytes())
        assert set(d) == {"module", "phase", "code", "observed_at"}
        assert (d["module"], d["phase"], d["code"]) == ("attribution", "commit", "resource_limit")
    finally:
        client.close()
        state.close()


class HostedCourseHarness(SyntheticStorage):
    """Exercise the real hosted route with synthetic Storage and Gateway HTTP peers."""

    def __init__(
        self,
        tmp_path,
        monkeypatch,
        *,
        gap=0,
        count=24,
        stride=900,
        active=True,
        origin_offset=0,
        **overrides,
    ):
        super().__init__("extraction", registry=fixture_registry())
        self.now = datetime.fromisoformat("2026-11-12T00:00:00Z")
        origin = self.now - timedelta(days=1, seconds=-origin_offset)
        self.windows = tuple(
            Interval(
                start=origin + timedelta(seconds=stride * i + (gap if i == 23 else 0)),
                end=origin + timedelta(seconds=stride * i + 900),
            )
            for i in range(count)
        )
        self.selected = self.windows[-2:]
        self.envelope = Interval(start=self.selected[0].start, end=self.selected[-1].end)
        self.rule_id = new_id()
        self.config = worker.Settings.model_validate(
            settings(
                tmp_path,
                "extraction",
                **{
                    "timeout": 35,
                    "gateway_timeout": 630,
                    "health_max_age_seconds": 731 if active else 3500,
                    "transcript_only": True,
                    "transcript_course_sessions": active,
                    # Existing synthetic replies are intentionally sequential.
                    "transcript_window_review_concurrency": 1,
                    "transcript_summary_rule_id": self.rule_id,
                    "transcript_windows": [w.model_dump(mode="json") for w in self.windows],
                    **overrides,
                },
            )
        )
        self.client.write(
            [
                Create(
                    "rule",
                    self.rule_id,
                    Rule(scope="课堂", instruction="按内容记笔记").model_dump(),
                )
            ],
            Context.system(),
        ).check()
        self.snapshots, self.queries, self.gateway_calls, self.reconciles = {}, [], [], []
        self.snapshot_opens = 0
        self.transcript_reads = []
        self.replies = []
        self.audit_receipts = {}
        self.last_lease = None
        self.health_after_calls = []
        self.max_batch_bytes = 0
        monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(self.transport))
        monkeypatch.setattr(worker.time, "time", lambda: self.now.timestamp())
        self.monkeypatch = monkeypatch

    def transport(self, req):
        assert req.headers["Authorization"] == "Bearer " + SECRET
        body = json.loads(req.content or b"{}")
        if req.url.host == "synthetic-gateway.invalid":
            assert req.extensions["timeout"]["read"] == 630
            if req.url.path.endswith("/reconcile"):
                self.reconciles.append(req.url.path)
                return self.audit_receipts.get(
                    req.url.path.split("/")[-2],
                    httpx.Response(200, json={"record_saved": True, "status": "unknown"}),
                )
            assert req.url.path == "/v1/complete"
            assert not self.snapshots  # no snapshot lease held during model calls
            run = self.client.get_system_record(body["run_id"])
            assert run is not None and run.payload["status"] == "running"
            [intent] = [c for c in run.payload["gateway_calls"] if c["call_id"] == body["call_id"]]
            assert intent["state"] == "sending"
            self.last_lease = run.payload["lease_until"]
            self.gateway_calls.append(body)
            reply = self.replies.pop(0)
            if callable(reply):
                reply = reply(body)
            self.health_after_calls.append(worker.healthy(self.config))
            if isinstance(reply, BaseException):
                raise reply
            OutputSchema(body["output_schema"]).validate(reply)
            return httpx.Response(
                200, json={"call_id": body["call_id"], "record_saved": True, "output": reply}
            )
        assert req.url.host == "synthetic.invalid"
        if req.url.path == "/v1/snapshots":
            self.snapshot_opens += 1
            assert body == {"codec": 1, "ttl_seconds": None, "profile": "transcript_extraction"}
            sid = new_id()
            context = self.client.snapshot()
            self.snapshots[sid] = context, context.__enter__()
            status, payload = 200, {"codec": 1, "lease": {"snapshot_id": sid}}
        elif req.method == "DELETE" and req.url.path.startswith("/v1/snapshots/"):
            context, _ = self.snapshots.pop(req.url.path.split("/")[-1])
            context.__exit__(None, None, None)
            status, payload = 200, {"codec": 1}
        elif req.url.path == "/v1/read":
            read, values = decode_read(req.content)
            reader = self.snapshots[read.snapshot_id][1] if read.snapshot_id else self.client
            if "query" in values:
                self.queries.append((read.snapshot_id is not None, values["query"]))
                if values["query"].data_type == "transcript_segment":
                    self.transcript_reads.append((read.method, values["query"].time))
            result = getattr(reader, read.method)(**values)
            status, payload = 200, json.loads(encode_read_result(read.method, result))
        else:
            if req.url.path == "/v1/batches":
                self.max_batch_bytes = max(self.max_batch_bytes, len(req.content))
            status, payload = self.request(req.method, req.url.path, body)
        return httpx.Response(status, json=payload)

    def transcript(self, start, text, *, seconds=1):
        row = make_raw(
            "transcript_segment",
            {"text": text, "audio_path": CANARY},
            start=start,
            end=start + timedelta(seconds=seconds),
        )
        self.memory.client("adapter").write([AppendRaw(row)], Context.system()).check()
        return row

    def prior_activity(self, start, *, superseded=False):
        aid = new_id()
        self.client.write(
            [
                Create(
                    "activity",
                    aid,
                    {"title_md": "previous note"},
                    event_start=start,
                    event_end=start + timedelta(seconds=1),
                ),
                RegisterDependencies("activity", aid, []),
            ],
            Context.system(),
        ).check()
        if superseded:
            self.client.write([Supersede("activity", aid, 1)], Context.system()).check()
        return aid

    def semantic(self, record_id: str) -> SemanticRecord:
        record = self.client.get(record_id)
        assert isinstance(record, SemanticRecord)
        return record

    def activity_version(self, aid):
        return self.semantic(aid).version

    def run_record(self, event):
        run = self.client.get_system_record(event.event_id)
        assert run is not None
        return run

    def feedback(self, command=None):
        command = command or Command(kind="extract", interval=self.envelope)
        raw = make_raw(
            "feedback",
            {"module": "extraction", "command": command.model_dump(mode="json")},
            start=self.now,
            channel="api",
        )
        event = (
            self.memory.client("api")
            .write([AppendRaw(raw)], Context.feedback(raw.id))
            .check()
            .events[0]
        )
        assert isinstance(event, DataChanged)
        return event

    def run(self, *events, extra=None):
        def serve(service, _stop, *, schedule, **_):
            assert schedule is None
            service.clock = lambda: self.now
            for event in events:
                service.handle_event(event)
                service.process(event.event_id)
            if extra is not None:
                extra(service)

        self.monkeypatch.setattr(worker, "serve", serve)
        worker.run(self.config, threading.Event())
        return [self.run_record(event) for event in events]

    def batch_ops(self, index):
        return decode_request(self.batches[index][1])[0]


def course_span(first_quote, last_quote=None, *, first=1, last=1, kind="course"):
    return {
        "kind": kind,
        "first": first,
        "last": last,
        "title_md": "课堂",
        "description_md": "示例",
        "first_evidence": {"i": first, "quote": first_quote},
        "last_evidence": {"i": last, "quote": last_quote or first_quote},
    }


def course_product(*ids):
    return {"body_md": "## 要点\n\n例题。", "evidence_ids": list(ids)}


def compressed_activity_floor(activity, raw_by_id, _minimum):
    return meets_activity_floor(activity, raw_by_id, timedelta(microseconds=1))


class CourseHost(HostedCourseHarness):
    """Legacy boundary/receipt fixtures deliberately model seconds of synthetic data.

    Keep their original geometry and replay fingerprints. Floor-specific hosted
    tests use HostedCourseHarness with the real comparator and production settings.
    """

    def __init__(self, tmp_path, monkeypatch, **kwargs):
        for module in (automatic_timeline, course_blocks, extraction_planning):
            monkeypatch.setattr(module, "meets_activity_floor", compressed_activity_floor)
        super().__init__(tmp_path, monkeypatch, **kwargs)


def test_course_host_accepts_exact_original_23_24_and_commits_one_note(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch)
    w23, w24 = host.selected
    prior = host.prior_activity(host.windows[0].start)
    retired = host.prior_activity(w23.start, superseded=True)
    row_a = host.transcript(w23.end - timedelta(seconds=3), "继续刚才的合成例题 A")
    row_b = host.transcript(w24.start + timedelta(seconds=3), "接着上一行合成例题 B")
    unknown = host.transcript(w24.end - timedelta(seconds=3), "不明内容")
    outside = host.transcript(host.windows[0].start, "OUTSIDE_WINDOW")
    start = host.now

    def spends_timeout(output):
        def progress(_call):
            host.now += timedelta(seconds=630)
            return output

        return progress

    host.replies = [
        spends_timeout(data)
        for data in [
            {"spans": [course_span("合成例题 A", "合成例题 B", last=2)]},
            course_product(row_a.id, row_b.id),
        ]
    ]
    event = host.feedback()
    run, same = host.run(event, event)  # duplicate delivery is not a new plan
    assert run is not None and run == same and run.payload["status"] == "succeeded"
    assert run.payload["detail"] == {
        "activities": 1,
        "course_activities": 1,
        "merged_boundaries": 1,
        "uncertain_boundaries": 0,
        "discarded_boundary_hints": 0,
        "unclassified_rows": 1,
    }
    assert run.payload["model_calls"] == [] and run.payload["lease_until"] is None
    assert host.last_lease == format_utc(start + timedelta(seconds=7250))
    assert host.health_after_calls == [True] * 2 and host.now - start > timedelta(seconds=731)
    assert [b["purpose"] for b in host.gateway_calls] == ["course_blocks", "generation"]
    assert all(b["root_feedback_id"] == event.data_id for b in host.gateway_calls)
    block = host.gateway_calls[0]["data"]
    assert [(c["s"], c["e"]) for c in block["coverage"]] == [
        (format_utc(w.start), format_utc(w.end)) for w in host.selected
    ]
    assert all(set(row) == {"i", "s", "e", "text"} for row in block["evidence"])
    for body in host.gateway_calls:
        assert worker.COURSE_BLOCK_SYSTEM in body["system"] and "one to three" not in body["system"]
        assert (
            "Chinese Markdown note per final activity" in body["system"]
            and "sections" in body["system"]
        )
    generation = host.gateway_calls[-1]["data"]
    assert [row["id"] for row in generation["evidence"]] == [row_a.id, row_b.id]
    assert generation["rule"] == {"instruction": "按内容记笔记"}
    for marker in [CANARY, outside.id, "OUTSIDE_WINDOW"]:
        assert marker not in repr(host.gateway_calls)
    for text in [row_a.payload["text"], row_b.payload["text"], unknown.payload["text"]]:
        assert text not in repr(run.payload)
    assert host.activity_version(prior) == 1 and host.activity_version(retired) == 2
    ops = host.batch_ops(-1)
    activity, selection, artifact = [op for op in ops if isinstance(op, Create)]
    assert [op.data_type for op in (activity, selection, artifact)] == [
        "activity",
        "rule_selection",
        "artifact",
    ]
    assert selection.payload["activity_id"] == artifact.payload["activity_id"] == activity.id
    assert [op.dependent_type for op in ops if isinstance(op, RegisterDependencies)] == [
        "activity",
        "rule_selection",
        "artifact",
    ]
    final = ops[-1]
    assert isinstance(final, PutSystemRecord) and final.expected_revision == run.revision - 1
    assert host.client.count(Query("action_suggestion")) == 0
    assert {q.data_type for snap, q in host.queries if snap} == {
        "extraction_run",
        "activity",
        "transcript_segment",
    }
    assert not host.snapshots


@pytest.mark.parametrize(
    "problem", ["partial_start", "partial_end", "gap_start", "old_activity", "reextract"]
)
def test_course_host_refuses_unsupported_requests_or_prior_activity_before_model(
    tmp_path, monkeypatch, problem
):
    host = CourseHost(tmp_path, monkeypatch, gap=60)
    first, last = host.selected
    interval = host.envelope
    old = None
    if problem == "partial_start":
        interval = Interval(start=first.start + timedelta(seconds=1), end=last.end)
    elif problem == "partial_end":
        interval = Interval(start=first.start, end=last.end - timedelta(seconds=1))
    elif problem == "gap_start":
        interval = Interval(start=first.end, end=last.end)
    elif problem == "old_activity":
        old = host.prior_activity(last.end - timedelta(seconds=1))
    command = (
        Command(kind="reextract", interval=interval, preview={})
        if problem == "reextract"
        else Command(kind="extract", interval=interval)
    )
    [run] = host.run(host.feedback(command))
    assert run.payload["status"] == "blocked" and not host.gateway_calls and not host.reconciles
    assert run.payload["gateway_calls"] == []
    assert run.payload["error"] == {
        "old_activity": "session_activity_overlap",
        "reextract": "transcript_course_sessions_manual_extract_only",
    }.get(problem, "transcript_interval_not_allowed")
    if old is not None:
        assert host.activity_version(old) == 1
    else:
        assert not host.queries  # reject before even opening the plan snapshot


def test_course_host_exact_gap_envelope_never_queries_or_sends_gap(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, gap=660)
    first, last = host.selected
    hidden_time = first.end + timedelta(seconds=30)
    hidden = host.transcript(hidden_time, "UNAUTHORIZED_GAP_TEXT")
    old = host.prior_activity(hidden_time)
    a = host.transcript(first.start, "课堂 A")
    b = host.transcript(last.start, "课堂 B")
    host.replies = [
        {"spans": [course_span("课堂 A")]},
        {"spans": [course_span("课堂 B")]},
        course_product(a.id),
        course_product(b.id),
    ]
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "succeeded" and run.payload["detail"]["activities"] == 2
    assert [call["purpose"] for call in host.gateway_calls] == ["course_blocks"] * 2 + [
        "generation"
    ] * 2
    assert hidden.id not in repr(host.gateway_calls) and "UNAUTHORIZED_GAP_TEXT" not in repr(
        host.gateway_calls
    )
    for _, query in host.queries:
        assert query.time is not None and not query.time.overlaps(hidden_time, hidden_time)
    assert host.activity_version(old) == 1


def test_course_host_protects_existing_activity_in_a_short_bridged_gap(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, gap=180)
    first, last = host.selected
    hidden_time = first.end + timedelta(seconds=30)
    hidden = host.transcript(hidden_time, "UNAUTHORIZED_GAP_TEXT")
    old = host.prior_activity(hidden_time)
    a = host.transcript(first.end - timedelta(seconds=4), "课堂前半段")
    b = host.transcript(last.start, "课堂后半段")
    host.replies = [
        {"spans": [course_span("课堂前半段", "课堂后半段", last=2)]},
        course_product(a.id, b.id),
    ]
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "blocked" and run.payload["error"] == "session_activity_overlap"
    assert [call["purpose"] for call in host.gateway_calls] == ["course_blocks", "generation"]
    assert hidden.id not in repr(host.gateway_calls) and "UNAUTHORIZED_GAP_TEXT" not in repr(
        host.gateway_calls
    )
    assert host.activity_version(old) == 1 and host.client.count(Query("artifact")) == 0


@pytest.mark.parametrize("missing", ["all", "part", "none"])
def test_course_host_blocks_missing_or_all_unclassified_without_partial_business(
    tmp_path, monkeypatch, missing
):
    host = CourseHost(tmp_path, monkeypatch)
    if missing != "all":
        host.transcript(host.selected[0].start, "难以判断")
        host.replies.append({"spans": []})
    if missing == "none":
        host.transcript(host.selected[1].start, "同样难以判断")
    if missing == "all":
        host.transcript(host.selected[0].start, "   ")  # blank does not count as unclassified
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "blocked"
    assert run.payload["error"] == (
        "session_all_rows_unclassified"
        if missing == "none"
        else "session_windows_without_transcript"
    )
    assert run.payload["detail"] == {"unclassified_rows": {"all": 0, "part": 1, "none": 2}[missing]}
    assert [call["purpose"] for call in host.gateway_calls] == (
        [] if missing == "all" else ["course_blocks"]
    )
    assert all(
        host.client.count(Query(kind)) == 0 for kind in ("activity", "rule_selection", "artifact")
    )


def test_course_host_live_overlap_prevents_commit_without_replanning(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=1)
    row = host.transcript(host.envelope.start, "已知课堂")
    old = []

    def concurrent_write(_):
        old.append(host.prior_activity(host.envelope.start))
        return course_product(row.id)

    host.replies = [{"spans": [course_span("已知课堂")]}, concurrent_write]
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "blocked" and run.payload["error"] == "session_activity_overlap"
    assert host.client.count(Query("activity")) == 1 and host.activity_version(old[0]) == 1
    assert host.client.count(Query("artifact")) == host.client.count(Query("rule_selection")) == 0


@pytest.mark.parametrize("crash", [False, True])
def test_course_host_timeout_or_lost_worker_never_reinvokes_unknown_call(
    tmp_path, monkeypatch, crash
):
    host = CourseHost(tmp_path, monkeypatch)
    host.transcript(host.selected[0].start, "课堂 A")
    host.transcript(host.selected[1].start, "课堂 B")

    def fails(_):
        host.now += timedelta(seconds=630)
        return KeyboardInterrupt() if crash else httpx.ReadTimeout(CANARY)

    host.replies = [{"spans": [course_span("课堂 A", "课堂 B", last=2)]}, fails]
    event = host.feedback()
    if crash:
        with pytest.raises(KeyboardInterrupt):
            host.run(event)
        saved = host.run_record(event)
        assert saved.payload["status"] == "running"
        host.run(event)  # still leased: neither repair nor another send
        assert not host.reconciles and len(host.gateway_calls) == 2
        host.now = datetime.fromisoformat(saved.payload["lease_until"]) + timedelta(seconds=1)

    def cannot_retry(service):
        with pytest.raises(ValueError, match="gateway_outcome_unknown"):
            service.retry(event.event_id)

    repeat = host.feedback()
    original, duplicate, later = host.run(event, event, repeat, extra=cannot_retry)
    assert original == duplicate
    assert (
        original.payload["status"] == "blocked"
        and original.payload["error"] == "gateway_outcome_unknown"
    )
    assert later.payload["error"] == "overlapping_unresolved_model_call"
    assert len(host.gateway_calls) == 2 and len(host.reconciles) == 1
    intents = original.payload["gateway_calls"]
    assert [item["state"] for item in intents] == ["returned", "sending"]
    assert host.reconciles[0] == f"/v1/calls/{intents[-1]['call_id']}/reconcile"
    assert all(
        set(item) == {"call_id", "purpose", "request_fingerprint", "state"} for item in intents
    )
    assert CANARY not in repr(original.payload) and host.client.count(Query("activity")) == 0


@pytest.mark.parametrize("last_known", [False, True])
def test_course_host_only_allows_known_old_prewrite_budget_failure(
    tmp_path, monkeypatch, last_known
):
    from ear_storage_client import PutSystemRecord

    host = CourseHost(tmp_path, monkeypatch, count=1)
    row = host.transcript(host.envelope.start, "可验证的课堂片段")
    old = host.feedback()

    def save_legacy(service):
        service.handle_event(old)
        run = host.run_record(old)
        calls = [
            {
                "call_id": new_id(),
                "purpose": "segmentation" if i == 0 else "generation",
                "request_fingerprint": "a" * 64,
                "state": "returned" if i < 4 or last_known else "sending",
            }
            for i in range(5)
        ]
        service.storage.write(
            [
                PutSystemRecord(
                    "extraction_run",
                    run.id,
                    {
                        **run.payload,
                        "status": "failed",
                        "attempts": 1,
                        "error": "transcript_max_calls_exceeded",
                        "gateway_calls": calls,
                    },
                    expected_revision=run.revision,
                )
            ],
            Context.feedback(old.data_id),
        ).check()

    host.run(extra=save_legacy)
    original = host.run_record(old)
    host.replies = (
        [{"spans": [course_span("课堂片段")]}, course_product(row.id)] if last_known else []
    )
    [run] = host.run(host.feedback())
    assert host.run_record(old) == original
    assert len(host.gateway_calls) == (2 if last_known else 0)
    assert run.payload["status"] == ("succeeded" if last_known else "blocked")
    if not last_known:
        assert run.payload["error"] == "overlapping_unresolved_model_call"


@pytest.mark.parametrize("fragments_per_window", [4, 5])
def test_course_host_full_allowlist_call_budget_and_batch_limits(
    tmp_path, monkeypatch, fragments_per_window
):
    host = CourseHost(tmp_path, monkeypatch, stride=1560)  # 24 independent capture blocks
    origin = host.now
    rows = [
        host.transcript(w.start + timedelta(seconds=10 * i), "合成记录")
        for w in host.windows
        for i in range(fragments_per_window)
    ]
    block_reply = {
        "spans": [
            course_span("合成记录", first=i, last=i, kind="other")
            for i in range(1, fragments_per_window + 1)
        ]
    }
    host.replies = [block_reply] * 24 + [course_product(r.id) for r in rows]
    interval = Interval(start=host.windows[0].start, end=host.windows[-1].end)
    [run] = host.run(host.feedback(Command(kind="extract", interval=interval)))
    assert len(run.payload["gateway_calls"]) == len(host.gateway_calls) == 120
    assert host.last_lease == format_utc(origin + timedelta(seconds=84250))
    assert host.max_batch_bytes < 8 * 1024 * 1024
    if fragments_per_window == 4:
        assert run.payload["status"] == "succeeded"
        assert host.client.count(Query("artifact")) == 96 and len(host.batch_ops(-1)) < 2000
    else:
        assert (
            run.payload["status"] == "failed"
            and run.payload["error"] == "transcript_max_calls_exceeded"
        )
        assert host.client.count(Query("activity")) == host.client.count(Query("artifact")) == 0
        assert host.replies  # no side effect for the 121st logical call


def test_course_host_splits_only_generation_that_exceeds_local_limit(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch)
    a = host.transcript(host.selected[0].end - timedelta(seconds=3), "续前 " + "甲" * 60_000)
    b = host.transcript(host.selected[1].start, "续后 " + "乙" * 60_000)
    host.replies = [
        {"spans": [course_span("续前", "续后", last=2)]},
        course_product(a.id),
        course_product(b.id),
        course_product(a.id, b.id),
    ]
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "succeeded"
    assert [call["purpose"] for call in host.gateway_calls] == [
        "course_blocks",
        "generation",
        "generation",
        "consolidation",
    ]
    assert all(len(call["data"]["evidence"]) == 1 for call in host.gateway_calls[1:3])
    assert len(run.payload["gateway_calls"]) == 4 and host.client.count(Query("artifact")) == 1


def test_course_host_rejects_unsplittable_generation_before_its_intent(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=1)
    host.transcript(host.envelope.start, "甲" * 90_000)
    host.replies = [{"spans": [course_span("甲甲")]}]
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "failed" and run.payload["error"] == "InputBudgetExceeded"
    assert [call["purpose"] for call in host.gateway_calls] == ["course_blocks"]
    assert [call["state"] for call in run.payload["gateway_calls"]] == ["returned"]
    assert host.client.count(Query("activity")) == host.client.count(Query("artifact")) == 0


def test_course_flag_validation_and_explicit_false_preserve_host_contract(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, active=False)
    values = host.config.model_dump()
    assert not worker.Settings.model_validate(values).transcript_course_sessions
    invalid = [
        ({"transcript_course_sessions": "true"}, "transcript_course_sessions"),
        ({"transcript_only": False}, "requires_transcript_only"),
        ({"transcript_summary_rule_id": None}, "requires_fixed_rule"),
        ({"transcript_windows": tuple(reversed(host.windows))}, "requires_ordered_windows"),
        (
            {"schedule": {"start": format_utc(host.now), "every_seconds": 900}},
            "requires_fixed_rule",
        ),
        ({"health_max_age_seconds": 729}, "health_max_age_seconds_below_course_call_budget"),
        ({"health_max_age_seconds": 3601}, "health_max_age_seconds"),
    ]
    for changes, error in invalid:
        with pytest.raises(ValueError, match=error):
            worker.Settings.model_validate(values | {"transcript_course_sessions": True} | changes)
    [run] = host.run(host.feedback())
    assert run.payload["error"] == "transcript_interval_not_allowed" and not host.gateway_calls
    single = host.selected[0]
    row = host.transcript(single.start, "一窗旧方式")
    host.replies = [
        {
            "activities": [
                {**single.model_dump(mode="json"), "title_md": "旧方式", "evidence_ids": [row.id]}
            ]
        },
        course_product(row.id),
    ]
    [run] = host.run(host.feedback(Command(kind="extract", interval=single)))
    assert run.payload["status"] == "succeeded" and run.payload["detail"] == {"activities": 1}
    assert [b["purpose"] for b in host.gateway_calls] == ["segmentation", "generation"]
    assert (
        "one to three" in host.gateway_calls[0]["system"]
        and worker.COURSE_BLOCK_SYSTEM not in host.gateway_calls[0]["system"]
    )


def test_course_host_stale_run_revision_fences_return_and_business_batch(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=1)
    row = host.transcript(host.envelope.start, "课堂")
    event = host.feedback()

    def fence_return(_):
        current = host.run_record(event)
        host.client.write(
            [
                PutSystemRecord(
                    "extraction_run",
                    current.id,
                    {**current.payload, "fence_probe": True},
                    expected_revision=current.revision,
                )
            ],
            Context.feedback(event.data_id),
        ).check()
        return course_product(row.id)

    host.replies = [{"spans": [course_span("课堂")]}, fence_return]
    [run] = host.run(event)
    assert run.payload["status"] == "running" and run.payload["fence_probe"]
    assert run.payload["gateway_calls"][-1]["state"] == "sending"
    assert host.client.count(Query("activity")) == host.client.count(Query("artifact")) == 0
    host.now = datetime.fromisoformat(run.payload["lease_until"]) + timedelta(seconds=1)
    [blocked] = host.run(event)
    assert (
        blocked.payload["status"] == "blocked"
        and blocked.payload["error"] == "gateway_outcome_unknown"
    )
    assert len(host.gateway_calls) == 2 and len(host.reconciles) == 1


def replacement_setup(host, *, bridge=True, tail=0, artifact_only_citation=False):
    first, last = host.windows
    a = host.transcript(first.end - timedelta(seconds=3), "旧范围第一行")
    b = host.transcript(last.start, "旧范围第二行")
    groups = [(a, b)] if bridge else [(a,), (b,)]
    draft, targets, preview = [], [], {}
    for group in groups:
        ids = [r.id for r in group]
        start, end = group[0].event_start, group[-1].event_end + timedelta(seconds=tail)
        activity = Activity(
            start=start,
            end=end,
            title_md="synthetic old",
            evidence_ids=ids[:1] if artifact_only_citation else ids,
        )
        draft.append(activity)
        aid, note_id, selection_id = (new_id() for _ in range(3))
        targets.extend((aid, note_id, selection_id))
        preview.update({aid: 1, note_id: 1})
        host.client.write(
            [
                Create(
                    "activity",
                    aid,
                    activity.model_dump(mode="json"),
                    event_start=start,
                    event_end=end,
                ),
                Create(
                    "artifact",
                    note_id,
                    {"activity_id": aid, "body_md": "old markdown", "evidence_ids": ids},
                    event_start=start,
                    event_end=end,
                ),
                Create(
                    "rule_selection",
                    selection_id,
                    {"activity_id": aid},
                    event_start=start,
                    event_end=end,
                ),
                *(
                    RegisterDependencies(kind, id, [])
                    for kind, id in zip(
                        ("activity", "artifact", "rule_selection"),
                        (aid, note_id, selection_id),
                        strict=True,
                    )
                ),
            ],
            Context.system(),
        ).check()
    digest = course_draft_sha256(draft)
    host.replies = (
        [{"spans": [course_span("第一行", "第二行", last=2)]}, course_product(a.id, b.id)]
        if bridge
        else [
            {"spans": [course_span("第一行")]},
            {"spans": [course_span("第二行")]},
            course_product(a.id),
            course_product(b.id),
        ]
    )
    host.config = worker.Settings.model_validate(
        host.config.model_dump()
        | {
            "transcript_course_reextract": True,
            "transcript_course_reextract_draft_sha256": digest,
        }
    )
    return Command(kind="reextract", interval=host.envelope, preview=preview), targets, (a, b)


@pytest.mark.parametrize("bridge", [True, False])
def test_course_reextract_exact_gapped_windows_preserves_history_and_does_not_repeat(
    tmp_path, monkeypatch, bridge
):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080 if bridge else 1560)
    command, targets, _ = replacement_setup(host, bridge=bridge)
    hidden = host.transcript(host.windows[0].end + timedelta(seconds=5), "PRIVATE GAP TRANSCRIPT")
    old = {id: host.semantic(id) for id in targets}
    action_id = new_id()
    host.client.write(
        [Create("action_suggestion", action_id, {"artifact_id": targets[1], "state": "executed"})],
        Context.system(),
    ).check()
    action = host.client.get(action_id)
    event = host.feedback(command)

    def verify_preview(service):
        assert service.preview_reextract(host.envelope)["versions"] == command.preview
        with pytest.raises(Blocked, match="manual_extract_only"):
            service.schedule(host.envelope)

    host.run(extra=verify_preview)
    [finished] = host.run(event)
    assert finished.payload["status"] == "succeeded"
    assert finished.payload["detail"]["covered_rows"] == 2
    assert finished.payload["detail"]["activities"] == (1 if bridge else 2)
    assert finished.payload["detail"]["course_replacement"] is True
    assert finished.payload["model_calls"] == []
    assert "old markdown" not in repr(host.gateway_calls)
    assert hidden.id not in repr(host.gateway_calls) and "PRIVATE GAP TRANSCRIPT" not in repr(
        host.gateway_calls
    )
    assert host.snapshot_opens == 0
    assert host.transcript_reads == [("query", TimeRange(w.start, w.end)) for w in host.windows]
    ops = host.batch_ops(-1)
    assert {op.id: op.expected_version for op in ops if isinstance(op, Supersede)} == dict.fromkeys(
        targets, 1
    )
    assert isinstance(ops[-1], PutSystemRecord) and ops[-1].payload["status"] == "succeeded"
    assert all(
        host.client.get_version(id, 1) == old[id] and host.semantic(id).superseded for id in targets
    )
    assert (
        host.client.get(action_id) == action and host.client.count(Query("action_suggestion")) == 1
    )
    versions = {}
    for kind in ("activity", "artifact"):
        for record in host.client.query(Query(kind)).items:
            assert isinstance(record, SemanticRecord)
            versions[record.id] = record.version
    count = len(host.gateway_calls)
    [repeat] = host.run(
        host.feedback(Command(kind="reextract", interval=host.envelope, preview=versions))
    )
    assert repeat.payload["error"] == "course_reextract_already_completed"
    assert len(host.gateway_calls) == count


@pytest.mark.parametrize(
    ("problem", "error", "sends"),
    [
        ("subsequence", "course_reextract_requires_full_window_list", 0),
        ("gap_activity", "course_reextract_old_activity_out_of_scope", 0),
        ("stale_preview", "reextract_preview_changed", 0),
        ("external_evidence", "course_reextract_old_evidence_out_of_scope", 0),
        ("manual", "course_reextract_protected_manual_or_edited", 0),
        ("edited", "course_reextract_protected_manual_or_edited", 0),
        ("pending", "course_reextract_protected_action", 0),
        ("approved", "course_reextract_protected_action", 0),
        ("blank", "course_reextract_blank_transcript_rows", 0),
        ("unclassified", "course_reextract_incomplete_coverage", 1),
        ("uncertain", "course_reextract_incomplete_coverage", 1),
        ("changed_draft", "course_reextract_draft_changed", 1),
        ("bad_generation", "ValueError", 2),
        ("selection_version", "reextract_preview_changed", 2),
        ("new_action", "course_reextract_protected_action", 2),
        ("new_target", "course_reextract_protected_manual_or_edited", 2),
    ],
)
def test_course_reextract_never_retires_old_objects_on_failed_guards(
    tmp_path, monkeypatch, problem, error, sends
):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080)
    command, targets, (a, b) = replacement_setup(host)
    activity, artifact, selection = [host.semantic(id) for id in targets]
    assert command.preview is not None
    if problem == "subsequence":
        command = Command(kind="reextract", interval=host.windows[0], preview=command.preview)
    elif problem == "stale_preview":
        command = Command(kind="reextract", interval=host.envelope, preview={})
    elif problem == "gap_activity":
        host.prior_activity(host.windows[0].end + timedelta(seconds=30))
    elif problem == "external_evidence":
        id = new_id()
        host.client.write(
            [
                Create(
                    "artifact",
                    id,
                    {"activity_id": activity.id, "evidence_ids": [new_id()]},
                    event_start=activity.event_start,
                    event_end=activity.event_end,
                ),
                RegisterDependencies("artifact", id, []),
            ],
            Context.system(),
        ).check()
        command.preview[id] = 1
    elif problem in {"manual", "new_target"}:

        def add_manual(_call=None):
            id = new_id()
            host.client.write(
                [
                    Create(
                        "artifact",
                        id,
                        {"activity_id": activity.id, "source": "manual"},
                        event_start=activity.event_start,
                        event_end=activity.event_end,
                    ),
                    RegisterDependencies("artifact", id, []),
                ],
                Context.system(),
            ).check()
            return course_product(a.id, b.id)

        if problem == "manual":
            add_manual()
        else:
            host.replies[-1] = add_manual
    elif problem == "edited":
        host.client.write(
            [
                Update("activity", activity.id, 1, activity.payload | {"title_md": "edit"}),
                RegisterDependencies("activity", activity.id, []),
            ],
            Context.system(),
        ).check()
        command.preview[activity.id] = 2
    elif problem in {"pending", "approved", "new_action"}:

        def add_action(_call=None):
            host.client.write(
                [
                    Create(
                        "action_suggestion",
                        new_id(),
                        {
                            "artifact_id": artifact.id,
                            "state": "approved" if problem == "new_action" else problem,
                        },
                    )
                ],
                Context.system(),
            ).check()
            return course_product(a.id, b.id)

        if problem == "new_action":
            host.replies[-1] = add_action
        else:
            add_action()
    elif problem == "blank":
        host.transcript(host.windows[0].start, "   ")
    elif problem == "unclassified":
        host.transcript(host.windows[0].start, "无法分类")
        host.replies[0] = {"spans": [course_span("第一行", "第二行", first=2, last=3)]}
    elif problem == "uncertain":
        host.replies[0] = {"spans": [course_span("第一行"), course_span("第二行", first=2, last=2)]}
    elif problem == "changed_draft":
        host.config = worker.Settings.model_validate(
            host.config.model_dump() | {"transcript_course_reextract_draft_sha256": "0" * 64}
        )
    elif problem == "bad_generation":
        host.replies[-1] = course_product(new_id())  # Valid wire shape; fails local evidence check.
    elif problem == "selection_version":

        def change_selection(_call):
            host.client.write(
                [
                    Update("rule_selection", selection.id, 1, selection.payload | {"new": True}),
                    RegisterDependencies("rule_selection", selection.id, []),
                ],
                Context.system(),
            ).check()
            return course_product(a.id, b.id)

        host.replies[-1] = change_selection
    before = {id: host.semantic(id) for id in targets}
    [run] = host.run(host.feedback(command))
    assert run.payload["error"] == error and run.payload["status"] in {"failed", "blocked"}
    assert len(host.gateway_calls) == sends
    for id in targets:
        record = host.semantic(id)
        assert not record.superseded
        assert record.version == before[id].version + int(
            problem == "selection_version" and id == selection.id
        )
        assert host.client.get_version(id, before[id].version) == before[id]


@pytest.mark.parametrize(
    ("problem", "expected", "audit_reads"),
    [
        ("returned", "succeeded", 0),
        ("confirmed", "succeeded", 1),
        ("definite_rejection", "succeeded", 1),
        ("not_listed", "overlapping_unresolved_model_call", 0),
        ("terminal_unknown", "overlapping_unresolved_model_call", 1),
        ("missing_audit", "overlapping_unresolved_model_call", 1),
        ("wrong_call_id", "overlapping_unresolved_model_call", 1),
        ("untracked", "overlapping_unresolved_model_call", 0),
        ("running", "overlapping_unresolved_model_call", 0),
        ("unverified_id", "course_reextract_unverified_predecessor_id", 0),
        ("partial_overlap", "overlapping_unresolved_model_call", 0),
        ("write_unknown", "overlapping_unresolved_model_call", 0),
    ],
)
def test_course_reextract_does_not_retry_or_skip_unknown_predecessor(
    tmp_path, monkeypatch, problem, expected, audit_reads
):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080)
    command, targets, _ = replacement_setup(host)
    interval = host.envelope
    if problem == "partial_overlap":
        interval = Interval(start=interval.start - timedelta(seconds=1), end=interval.end)
    old = host.feedback(Command(kind="extract", interval=interval))
    call_id = new_id()
    returned = problem in {"returned", "write_unknown"}
    status, error = (
        ("failed", "ValidationError")
        if problem == "returned"
        else ("blocked", "gateway_outcome_unknown")
    )
    if problem == "write_unknown":
        error = "gateway_result_not_committed"
    if problem == "running":
        status = "running"

    def admit(service):
        service.handle_event(old)
        run = host.run_record(old)
        p = {
            **run.payload,
            "status": status,
            "error": error,
            "attempts": 1,
            "gateway_calls": [
                {
                    "call_id": call_id,
                    "request_fingerprint": "a" * 64,
                    "purpose": "course_blocks",
                    "state": "returned" if returned else "sending",
                }
            ],
        }
        if problem == "untracked":
            p.pop("gateway_calls")
        service.storage.write(
            [PutSystemRecord("extraction_run", run.id, p, expected_revision=run.revision)],
            Context.feedback(old.data_id),
        ).check()

    before = None
    if problem != "unverified_id":
        host.run(extra=admit)
        before = host.run_record(old)
    host.config = worker.Settings.model_validate(
        host.config.model_dump()
        | {
            "transcript_course_reextract_reconciled_run_ids": []
            if problem == "not_listed"
            else [old.event_id],
        }
    )
    codes = {
        "terminal_unknown": "provider_outcome_unknown",
        "definite_rejection": "provider_rejected",
    }
    code = codes.get(problem)
    host.audit_receipts[call_id] = (
        httpx.Response(
            200,
            json={
                "call_id": new_id() if problem == "wrong_call_id" else call_id,
                "record_saved": True,
                "status": "failed" if code else "success",
                "error_code": code,
                "response_recoverable": False,
            },
        )
        if problem != "missing_audit"
        else httpx.Response(404)
    )

    [run] = host.run(host.feedback(command))
    assert len(host.reconciles) == audit_reads
    if audit_reads:
        assert host.reconciles == [f"/v1/calls/{call_id}/reconcile"]
    if before is not None:
        assert host.run_record(old) == before
    if expected == "succeeded":
        assert run.payload["status"] == "succeeded"
        assert len(host.gateway_calls) == 2 and all(
            call["call_id"] != call_id for call in host.gateway_calls
        )
    else:
        assert run.payload["status"] == "blocked" and run.payload["error"] == expected
        assert not host.gateway_calls and all(not host.semantic(id).superseded for id in targets)


def test_course_reextract_missing_selected_window_blocks_before_model(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080)
    command, targets, _ = replacement_setup(host)
    extra = Interval(start=host.windows[-1].end, end=host.windows[-1].end + timedelta(minutes=15))
    host.config = worker.Settings.model_validate(
        host.config.model_dump() | {"transcript_windows": (*host.windows, extra)}
    )
    interval = Interval(start=host.envelope.start, end=extra.end)
    [run] = host.run(
        host.feedback(Command(kind="reextract", interval=interval, preview=command.preview))
    )
    assert run.payload["error"] == "session_windows_without_transcript"
    assert not host.gateway_calls and all(not host.semantic(id).superseded for id in targets)


def test_course_reextract_rejects_bad_private_config(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    default = host.config.model_dump()
    assert not host.config.transcript_course_reextract
    assert host.config.transcript_course_reextract_reconciled_run_ids == ()
    enabled = default | {
        "transcript_course_reextract": True,
        "transcript_course_reextract_draft_sha256": "a" * 64,
    }
    id = new_id()
    for config, error in [
        (default | {"transcript_course_reextract": "true"}, "transcript_course_reextract"),
        (enabled | {"transcript_course_sessions": False}, "requires_course_blocks"),
        (
            enabled | {"transcript_course_reextract_draft_sha256": "not-a-digest"},
            "requires_draft_sha256",
        ),
        (
            enabled | {"transcript_course_reextract_reconciled_run_ids": [id, id]},
            "invalid_course_reextract_reconciled_run_ids",
        ),
        (
            default | {"transcript_course_reextract_reconciled_run_ids": [id]},
            "unexpected_course_reextract_settings",
        ),
    ]:
        with pytest.raises(ValueError, match=error):
            worker.Settings.model_validate(config)


@pytest.mark.parametrize("inside", [False, True])
def test_course_reextract_checks_for_new_rows_without_rereading_raw_bodies(
    tmp_path, monkeypatch, inside
):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080)
    command, targets, (a, b) = replacement_setup(host)
    first = host.windows[0]

    def append(_call):
        host.transcript(first.start if inside else first.end, "new raw row")
        return course_product(a.id, b.id)

    host.replies[-1] = append
    [run] = host.run(host.feedback(command))
    assert run.payload["error"] == ("course_reextract_transcript_changed" if inside else None)
    assert all(host.semantic(id).superseded is not inside for id in targets)
    allowed = [TimeRange(w.start, w.end) for w in host.windows]
    assert host.snapshot_opens == 0
    assert [t for meth, t in host.transcript_reads if meth == "query"] == allowed
    assert [t for meth, t in host.transcript_reads if meth == "count"] == (
        allowed[:1] if inside else allowed
    )


def test_course_reextract_refuses_to_retire_an_activity_in_a_long_gap(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1560)
    command, targets, _ = replacement_setup(host, bridge=False)
    hidden = host.prior_activity(host.windows[0].end + timedelta(seconds=30))
    assert command.preview is not None
    command.preview[hidden] = 1  # The unchanged envelope preview includes the unrelated activity.
    [run] = host.run(host.feedback(command))
    assert run.payload["error"] == "course_reextract_old_activity_out_of_scope"
    assert not host.gateway_calls and not host.transcript_reads
    assert all(not host.semantic(id).superseded for id in [*targets, hidden])


_DRAFT_SPANS = "transcript_course_reextract_draft_spans"
_SCHEMA_DISCARDS = "transcript_course_reextract_schema_discard_attestations"
_RECONCILED_RUNS = "transcript_course_reextract_reconciled_run_ids"


def reviewed_span(first, last, a, b):
    return {
        "first": first,
        "last": last,
        "start": format_utc(a.event_start),
        "end": format_utc(b.event_end),
        "title_md": "LOCAL REVIEWED TITLE",
        "description_md": "LOCAL REVIEWED DESCRIPTION",
    }


def prior_course_run(
    host, command, *, purposes=("segmentation",), error="gateway_outcome_unknown", sending=True
):
    old = host.feedback(command)
    intents = [
        {
            "call_id": new_id(),
            "request_fingerprint": "a" * 64,
            "purpose": purpose,
            "state": "sending" if sending and i == len(purposes) - 1 else "returned",
        }
        for i, purpose in enumerate(purposes)
    ]

    def admit(service):
        service.handle_event(old)
        run = host.run_record(old)
        p = run.payload | {
            "status": "blocked" if sending else "failed",
            "error": error,
            "attempts": 1,
            "gateway_calls": intents,
        }
        service.storage.write(
            [PutSystemRecord("extraction_run", run.id, p, expected_revision=run.revision)],
            Context.feedback(old.data_id),
        ).check()

    host.run(extra=admit)
    intent = intents[-1]
    receipt = {
        "call_id": intent["call_id"],
        "record_saved": True,
        "status": "failed" if sending else "success",
        "error_code": "provider_outcome_unknown" if sending else None,
        "response_recoverable": False,
    }
    host.audit_receipts[intent["call_id"]] = httpx.Response(200, json=receipt)
    return (
        old,
        {
            "run_id": old.event_id,
            "call_id": intent["call_id"],
            "request_fingerprint": intent["request_fingerprint"],
            "proof": "schema_response_received",
            "stage": "extraction_schema",
        },
        receipt,
    )


def test_course_reextract_reuses_reviewed_plan_and_consumes_exactly_two_schema_discards(
    tmp_path, monkeypatch
):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080)
    command, targets, (a, b) = replacement_setup(host)
    old_runs = []
    attestations = []
    for window in host.windows:
        event, attestation, _ = prior_course_run(host, Command(kind="extract", interval=window))
        old_runs.append(event)
        attestations.append(attestation)
    for i in range(5):
        budget = i == 4
        event, _, _ = prior_course_run(
            host,
            Command(kind="extract", interval=host.windows[i % 2]),
            purposes=("segmentation", "generation") + (("generation",) * 3 if budget else ()),
            error="transcript_max_calls_exceeded" if budget else "ValueError",
            sending=False,
        )
        old_runs.append(event)
    before = [host.run_record(event) for event in old_runs]
    span = reviewed_span(1, 2, a, b)
    host.config = worker.Settings.model_validate(
        host.config.model_dump()
        | {
            _DRAFT_SPANS: [span],
            _SCHEMA_DISCARDS: attestations,
            _RECONCILED_RUNS: [event.event_id for event in old_runs],
        }
    )
    host.replies = [course_product(a.id, b.id)]
    [run] = host.run(host.feedback(command))
    assert run.payload["status"] == "succeeded"
    assert run.payload["detail"]["covered_rows"] == 2
    assert [host.run_record(event) for event in old_runs] == before
    assert sorted(host.reconciles) == sorted(
        f"/v1/calls/{attestation['call_id']}/reconcile" for attestation in attestations
    )
    [send] = host.gateway_calls
    assert send["purpose"] == "generation" and send["run_id"] == run.id
    assert send["call_id"] not in {c["call_id"] for r in before for c in r.payload["gateway_calls"]}
    assert send["data"]["activity"] == {key: span[key] for key in ("start", "end")}
    for key in ("title_md", "description_md"):
        assert span[key] not in repr(send) and span[key] not in repr(run.payload)
    for attestation in attestations:
        assert attestation["call_id"] not in repr(send)
        assert attestation["request_fingerprint"] not in repr(send)
    [new_activity] = host.client.query(Query("activity")).items
    assert new_activity.payload == {
        key: span[key] for key in ("start", "end", "title_md", "description_md")
    } | {"evidence_ids": [a.id, b.id]}
    assert all(host.semantic(id).superseded for id in targets)


@pytest.mark.parametrize(
    "problem",
    [
        "no_attestation",
        "call_id",
        "fingerprint",
        "run_id",
        "purpose",
        "state",
        "multi_call",
        "inner_range",
        "batch_range",
        "reextract",
        "newer",
        "audit_unknown",
        "audit_unsaved",
        "audit_recoverable",
        "audit_missing",
        "audit_wrong_id",
        "audit_resolved",
    ],
)
def test_course_reextract_schema_attestation_fails_closed(tmp_path, monkeypatch, problem):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080)
    command, targets, (a, b) = replacement_setup(host)
    event = host.feedback(command)
    if problem == "newer":
        host.run(extra=lambda service: service.handle_event(event))
    window = host.windows[0]
    if problem == "inner_range":
        window = Interval(start=window.start + timedelta(seconds=1), end=window.end)
    elif problem == "batch_range":
        window = host.envelope
    old, attestation, receipt = prior_course_run(
        host,
        Command(
            kind="reextract" if problem == "reextract" else "extract",
            interval=window,
            preview={} if problem == "reextract" else None,
        ),
        purposes={"purpose": ("generation",), "multi_call": ("segmentation", "segmentation")}.get(
            problem, ("segmentation",)
        ),
        sending=problem != "state",
        error="ValueError" if problem == "state" else "gateway_outcome_unknown",
    )
    predecessors = [old]
    call_id = attestation["call_id"]
    if problem == "run_id":
        other, _, _ = prior_course_run(
            host, Command(kind="extract", interval=window), sending=False, error="ValueError"
        )
        predecessors.append(other)
        attestation["run_id"] = other.event_id
    elif problem == "call_id":
        attestation["call_id"] = new_id()
    elif problem == "fingerprint":
        attestation["request_fingerprint"] = "b" * 64
    elif problem == "audit_unknown":
        receipt["status"] = "unknown"
    elif problem == "audit_unsaved":
        receipt["record_saved"] = False
    elif problem == "audit_recoverable":
        receipt["response_recoverable"] = True
    elif problem == "audit_wrong_id":
        receipt["call_id"] = new_id()
    elif problem == "audit_resolved":
        receipt["error_code"] = "provider_rejected"
    host.audit_receipts[call_id] = (
        httpx.Response(404) if problem == "audit_missing" else httpx.Response(200, json=receipt)
    )
    before = [host.run_record(e) for e in predecessors]
    host.config = worker.Settings.model_validate(
        host.config.model_dump()
        | {
            _DRAFT_SPANS: [reviewed_span(1, 2, a, b)],
            _SCHEMA_DISCARDS: [] if problem == "no_attestation" else [attestation],
            _RECONCILED_RUNS: [e.event_id for e in predecessors],
        }
    )
    [run] = host.run(event)
    assert run.payload["status"] == "blocked"
    assert run.payload["error"] == (
        "course_reextract_unused_schema_discard_attestation"
        if problem in {"state", "audit_resolved"}
        else "overlapping_unresolved_model_call"
    )
    assert not host.gateway_calls and not host.transcript_reads and not host.snapshot_opens
    assert [host.run_record(e) for e in predecessors] == before
    assert all(not host.semantic(id).superseded for id in targets)


@pytest.mark.parametrize(
    ("problem", "error"),
    [
        ("missing_row", "course_reextract_draft_not_complete_partition"),
        ("overlap", "course_reextract_draft_not_complete_partition"),
        ("long_gap", "course_reextract_draft_crosses_capture_block"),
        ("wrong_time", "course_reextract_draft_anchor_time_mismatch"),
        ("different_digest", "course_reextract_draft_changed"),
        ("blank", "course_reextract_blank_transcript_rows"),
    ],
)
def test_course_reextract_invalid_reviewed_plan_never_falls_back_to_segmentation(
    tmp_path, monkeypatch, problem, error
):
    host = CourseHost(
        tmp_path, monkeypatch, count=2, stride=1560 if problem == "long_gap" else 1080
    )
    command, targets, (a, b) = replacement_setup(host, bridge=problem != "long_gap")
    spans = [reviewed_span(1, 2, a, b)]
    changes = {_DRAFT_SPANS: spans}
    if problem == "missing_row":
        spans[:] = [reviewed_span(1, 1, a, a)]
    elif problem == "overlap":
        spans.append(reviewed_span(2, 2, b, b))
    elif problem == "wrong_time":
        spans[0]["end"] = format_utc(b.event_end + timedelta(seconds=1))
    elif problem == "different_digest":
        # Both parts anchor exactly and cover every row, but the reviewed draft was one span.
        spans[:] = [reviewed_span(1, 1, a, a), reviewed_span(2, 2, b, b)]
    elif problem == "blank":
        host.transcript(host.windows[0].start, "   ")
    host.config = worker.Settings.model_validate(host.config.model_dump() | changes)
    [run] = host.run(host.feedback(command))
    assert run.payload["error"] == error and run.payload["status"] == "blocked"
    assert not host.gateway_calls and all(not host.semantic(id).superseded for id in targets)


def test_course_reextract_private_plan_fits_config_with_2090_ordered_rows(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch)
    raw = []
    for i in range(2090):
        start = host.windows[i // 90].start + timedelta(seconds=(i % 90) // 2 * 3)
        row = make_raw(
            "transcript_segment", {"text": "合成"}, start=start, end=start + timedelta(seconds=1)
        )
        raw.append(RawRecord.model_validate(row.model_dump() | {"ingested_at": host.now}))
    parts = [raw[:1000], raw[1000:]]
    spans = [
        reviewed_span(1, 1000, parts[0][0], parts[0][-1]),
        reviewed_span(1001, 2090, parts[1][0], parts[1][-1]),
    ]
    approved = [
        Activity(
            start=part[0].event_start,
            end=part[-1].event_end,
            title_md="draft",
            evidence_ids=[r.id for r in part],
        )
        for part in parts
    ]
    digest = course_draft_sha256(approved)
    assert len(json.dumps([r.id for r in raw]).encode()) > worker.MAX_CONFIG
    config = host.config.model_dump(mode="json") | {
        "storage_token": SECRET,
        "transcript_course_reextract": True,
        "transcript_course_reextract_draft_sha256": digest,
        _DRAFT_SPANS: spans,
    }
    path = write_config(tmp_path, config)
    assert path.stat().st_size < 5000
    loaded = worker.load_config(str(path))
    assert loaded.transcript_course_reextract_draft_spans is not None
    result = compile_course_draft(
        loaded.transcript_course_reextract_draft_spans, host.windows, [*reversed(raw), raw[0]]
    )
    assert course_draft_sha256(result.segmentation.activities) == digest
    assert [a.evidence_ids for a in result.segmentation.activities] == [
        a.evidence_ids for a in approved
    ]


def test_course_reextract_schema_attestations_are_private_bounded_and_closed(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2, stride=1080)
    default = host.config.model_dump()
    _, _, (a, b) = replacement_setup(host)
    values = host.config.model_dump() | {_DRAFT_SPANS: [reviewed_span(1, 2, a, b)]}
    attestations = [
        {
            "run_id": new_id(),
            "call_id": new_id(),
            "request_fingerprint": "a" * 64,
            "proof": "schema_response_received",
            "stage": "extraction_schema",
        }
        for _ in range(5)
    ]
    [first, second, *_] = attestations
    values[_RECONCILED_RUNS] = [a["run_id"] for a in attestations]
    for config, reason in [
        (values | {_SCHEMA_DISCARDS: attestations}, "at most 4"),
        (values | {_SCHEMA_DISCARDS: [first, first]}, "invalid_course_reextract"),
        (
            values | {_SCHEMA_DISCARDS: [first, second | {"call_id": first["call_id"]}]},
            "invalid_course_reextract",
        ),
        (values | {_SCHEMA_DISCARDS: [first], _RECONCILED_RUNS: []}, "invalid_course_reextract"),
        (values | {_SCHEMA_DISCARDS: [first], _DRAFT_SPANS: None}, "requires_draft_spans"),
        (values | {_SCHEMA_DISCARDS: [first | {"stage": "transport"}]}, "extraction_schema"),
        (values | {_SCHEMA_DISCARDS: [first | {"proof": True}]}, "schema_response_received"),
        (values | {_SCHEMA_DISCARDS: [first | {"log_body": CANARY}]}, "Extra inputs"),
        (values | {_SCHEMA_DISCARDS: [first | {"request_fingerprint": False}]}, "string"),
        (default | {_SCHEMA_DISCARDS: [first]}, "unexpected_course_reextract_settings"),
        (values | {_DRAFT_SPANS: [reviewed_span("1", 2, a, b)]}, "valid integer"),
    ]:
        with pytest.raises(ValueError, match=reason) as failure:
            worker.Settings.model_validate(config)
        assert CANARY not in str(failure.value)


_TIMELINE = "transcript_course_reextract_timeline"
_LEGACY_DIGEST = "transcript_course_reextract_draft_sha256"


def timeline_replacement_setup(host, *, one_candidate=False, paired_family=True):
    command, targets, (a, b) = replacement_setup(host, bridge=paired_family)
    omitted = host.transcript(b.event_end + timedelta(seconds=2), "OMITTED_PRIVATE_TRANSCRIPT")
    raw = []
    for item in (omitted, b, a, a):  # Unsorted with a repeat, like a touching seam query.
        record = host.client.get(item.id)
        assert isinstance(record, RawRecord)
        raw.append(record)
    projections = []
    for id in targets:
        record = host.semantic(id)
        projections.append(
            {
                "start": record.event_start,
                "end": record.event_end,
                "id": id,
                "data_type": record.data_type,
                "version": record.version,
                "activity_id": record.payload.get("activity_id"),
                "protection": "automatic",
                "action_safety": "clear" if record.data_type == "activity" else None,
            }
        )
    preflight = PreflightInput.model_validate(
        {
            "scope": host.envelope.model_dump(),
            "captures": [w.model_dump() | {"id": new_id()} for w in host.windows],
            "holds": [],
            "targets": sorted(projections, key=lambda r: (r["start"], r["end"], r["id"])),
        }
    )
    timeline = project_timeline(host.windows, raw, preflight)
    [view] = plan_timeline(timeline)["windows"]
    left, right = {"i": 1, "quote": "第一行"}, {"i": 2, "quote": "第二行"}
    # A verified join spans a physical capture seam; the third row is reviewed
    # but deliberately unclassified, not a third activity or a note input.
    reviewed = WindowReview.model_validate(
        {
            "digest": view["digest"],
            "spans": [
                {
                    "first": 1,
                    "last": 1 if one_candidate else 2,
                    "engagement": "participation",
                    "first_evidence": left,
                    "last_evidence": left if one_candidate else right,
                    "engagement_evidence": left,
                    "continuity": [] if one_candidate else [{"left": left, "right": right}],
                }
            ],
        }
    )
    completed = CompletedReview(timeline=timeline, reviews=(reviewed,))
    span = ReviewedTimelineSpan.model_validate(
        reviewed_span(1, 1 if one_candidate else 2, a, a if one_candidate else b)
    )
    draft = build_reviewed_timeline_draft(host.windows, raw, preflight, completed, (span,))
    host.config = worker.Settings.model_validate(
        host.config.model_dump() | {_LEGACY_DIGEST: None, _TIMELINE: draft.model_dump(mode="json")}
    )
    host.replies = [course_product(a.id, *(() if one_candidate else (b.id,)))]
    return command, targets, raw, preflight, completed, draft


def test_reviewed_timeline_uses_assembly_and_fixed_rule_without_sending_omissions(
    tmp_path, monkeypatch
):
    host = HostedCourseHarness(tmp_path, monkeypatch, count=2)
    command, targets, raw, _, _, draft = timeline_replacement_setup(host)
    omitted, b, a, _ = raw
    config_file = tmp_path / "synthetic-reviewed.json"
    config_file.write_text(
        json.dumps(host.config.model_dump(mode="json") | {"storage_token": SECRET})
    )
    config_file.chmod(0o600)
    host.config = worker.load_config(str(config_file))
    [run] = host.run(host.feedback(command))
    assert run.payload["status"] == "succeeded"
    assert run.payload["detail"]["reviewed_timeline"] is True
    assert run.payload["detail"]["unclassified_rows"] == 1
    assert run.payload["detail"]["covered_rows"] == 2
    assert [call["purpose"] for call in host.gateway_calls] == ["generation"]
    assert all(
        secret not in repr(host.gateway_calls)
        for secret in (
            omitted.id,
            omitted.payload["text"],
            draft.spans[0].title_md,
            draft.spans[0].description_md,
        )
    )
    assert omitted.payload["text"] not in repr(run.payload) and run.payload["model_calls"] == []
    [activity] = host.client.query(Query("activity")).items
    assert isinstance(activity, SemanticRecord)
    assert activity.payload["title_md"] == draft.spans[0].title_md
    assert activity.payload["evidence_ids"] == [a.id, b.id]
    [artifact] = host.client.query(Query("artifact")).items
    assert isinstance(artifact, SemanticRecord)
    assert artifact.payload["evidence_ids"] == [a.id, b.id]
    assert host.client.get(omitted.id) == omitted
    assert host.client.count(Query("action_suggestion")) == 0
    assert host.snapshot_opens == 0
    ops = host.batch_ops(-1)
    assert {op.id for op in ops if isinstance(op, Supersede)} == set(targets)
    assert isinstance(ops[-1], PutSystemRecord) and ops[-1].payload["status"] == "succeeded"
    assert all(host.semantic(id).superseded for id in targets)


@pytest.mark.parametrize(
    ("problem", "error", "calls"),
    [
        ("public", "reextract_preview_changed", 0),
        ("private", "course_reextract_timeline_old_pins_changed", 0),
        ("input", "course_reextract_timeline_input_changed", 0),
        ("missing_family", "course_reextract_timeline_old_family_without_successor", 0),
        ("late_private", "course_reextract_timeline_old_pins_changed", 1),
        ("late_input", "course_reextract_transcript_changed", 1),
        ("late_rule", "gateway_result_not_committed", 1),
        ("held_sending", "overlapping_unresolved_model_call", 0),
    ],
)
def test_reviewed_timeline_guards_leave_old_family_intact(
    tmp_path, monkeypatch, problem, error, calls
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    command, targets, raw, _, _, _ = timeline_replacement_setup(
        host, one_candidate=problem == "missing_family", paired_family=problem != "missing_family"
    )
    omitted, b, a, _ = raw

    def bump_selection():
        selection = host.semantic(targets[2])
        host.client.write(
            [
                Update(
                    "rule_selection",
                    selection.id,
                    selection.version,
                    selection.payload | {"rule_ids": [host.rule_id]},
                ),
                RegisterDependencies("rule_selection", selection.id, []),
            ],
            Context.system(),
        ).check()

    if problem == "public":
        command = command.model_copy(update={"preview": {targets[0]: 1, targets[1]: 2}})
    elif problem == "private":
        bump_selection()
    elif problem == "input":
        host.transcript(omitted.event_end + timedelta(seconds=2), "NEW UNCLASSIFIED ROW")
    elif problem == "held_sending":
        predecessor, _, _ = prior_course_run(
            host, Command(kind="extract", interval=host.windows[0])
        )
        host.config = worker.Settings.model_validate(
            host.config.model_dump() | {_RECONCILED_RUNS: [predecessor.event_id]}
        )
    elif problem.startswith("late"):

        def during_generation(_call):
            if problem == "late_private":
                bump_selection()
            elif problem == "late_input":
                host.transcript(omitted.event_end + timedelta(seconds=2), "LATE UNCLASSIFIED ROW")
            else:
                rule = host.semantic(host.rule_id)
                host.client.write(
                    [
                        Update(
                            "rule", rule.id, rule.version, rule.payload | {"instruction": "changed"}
                        )
                    ],
                    Context.system(),
                ).check()
            return course_product(a.id, b.id)

        host.replies = [during_generation]
    [run] = host.run(host.feedback(command))
    assert run.payload["status"] == "blocked" and run.payload["error"] == error
    assert len(host.gateway_calls) == calls and host.reconciles == []
    assert all(not host.semantic(id).superseded for id in targets)
    assert host.client.count(Query("activity")) == len(targets) // 3
    assert host.client.count(Query("artifact")) == len(targets) // 3
    assert host.client.count(Query("rule_selection")) == len(targets) // 3


def test_reviewed_timeline_handoff_and_host_config_fail_closed(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    _, _, raw, preflight, completed, draft = timeline_replacement_setup(host)
    with pytest.raises(ValueError, match="exact_complete_review"):
        build_reviewed_timeline_draft(
            host.windows,
            raw,
            preflight,
            completed.model_copy(update={"allow_partial": True}),
            draft.spans,
        )
    with pytest.raises(ValueError, match="timeline_reply_set_mismatch"):
        build_reviewed_timeline_draft(
            host.windows, raw, preflight, completed.model_copy(update={"reviews": ()}), draft.spans
        )
    with pytest.raises(ValueError, match="preflight_not_clear"):
        project_timeline(
            host.windows, raw, preflight.model_copy(update={"holds": (preflight.scope,)})
        )
    without_bridge = completed.reviews[0].spans[0].model_copy(update={"continuity": ()})
    broken = completed.model_copy(
        update={"reviews": (completed.reviews[0].model_copy(update={"spans": (without_bridge,)}),)}
    )
    _, _, a, _ = raw
    with pytest.raises(ValueError, match="candidate_not_safe"):
        build_reviewed_timeline_draft(
            host.windows,
            raw,
            preflight,
            broken,
            (ReviewedTimelineSpan.model_validate(reviewed_span(1, 1, a, a)),),
        )
    blank_ref = host.transcript(raw[0].event_end + timedelta(seconds=1), "   ")
    blank = host.client.get(blank_ref.id)
    assert isinstance(blank, RawRecord)
    with pytest.raises(Blocked, match="blank_transcript_rows"):
        draft.compile(host.windows, [*raw, blank])
    changed_only_omission = raw[0].model_copy(update={"payload": {"text": "ALTERED OMITTED TEXT"}})
    with pytest.raises(Blocked, match="timeline_input_changed"):
        draft.compile(host.windows, [changed_only_omission, *raw[1:]])
    invalid_drafts = [
        ({"review_complete": False}, "requires_complete_review"),
        ({"review_complete": 1}, "valid boolean"),
        ({"spans": []}, "at least 1 item"),
        ({"extra": "unknown"}, "Extra inputs"),
        ({"draft_sha256": "0" * 64}, "timeline_draft_changed"),
        ({"old_pins": [draft.old_pins[0], draft.old_pins[0]]}, "invalid_family_pins"),
    ]
    for changes, error in invalid_drafts:
        with pytest.raises(ValueError, match=error):
            ReviewedTimelineDraft.model_validate(draft.model_dump() | changes)
    config = host.config.model_dump()
    predecessor = new_id()
    attestation = {
        "run_id": predecessor,
        "call_id": new_id(),
        "request_fingerprint": "a" * 64,
        "proof": "schema_response_received",
        "stage": "extraction_schema",
    }
    for changes, error in [
        ({"transcript_course_reextract": False}, "invalid_course_reextract_timeline_settings"),
        ({"transcript_course_sessions": False}, "requires_course_blocks"),
        ({_LEGACY_DIGEST: "a" * 64}, "invalid_course_reextract_timeline_settings"),
        (
            {_DRAFT_SPANS: [draft.spans[0].model_dump()]},
            "invalid_course_reextract_timeline_settings",
        ),
        (
            {_SCHEMA_DISCARDS: [attestation], _RECONCILED_RUNS: [predecessor]},
            "invalid_course_reextract_timeline_settings",
        ),
        (
            {
                "transcript_windows": [
                    host.windows[0].model_dump(),
                    host.windows[1].model_dump()
                    | {"start": host.windows[1].start + timedelta(seconds=1)},
                ]
            },
            "requires_contiguous_windows",
        ),
    ]:
        with pytest.raises(ValueError, match=error):
            worker.Settings.model_validate(config | changes)


_AUTO_TIMELINE = "transcript_automatic_timeline"
_AUTO_PURPOSE = "timeline_window_review_v2"
_AUTO_PREWRITE = "transcript_automatic_timeline_prewrite_pins"


def configure_automatic_timeline(host, targets=(), **changes):
    old = []
    for id in targets:
        record = host.semantic(id)
        old.append(
            {
                "start": record.event_start,
                "end": record.event_end,
                "id": id,
                "data_type": record.data_type,
                "version": record.version,
                "activity_id": record.payload.get("activity_id"),
                "protection": "automatic",
                "action_safety": "clear" if record.data_type == "activity" else None,
            }
        )
    data = {
        "scope": host.envelope.model_dump(),
        "captures": [w.model_dump() | {"id": new_id()} for w in host.windows],
        "holds": [],
        "targets": sorted(old, key=lambda r: (r["start"], r["end"], r["id"])),
        **changes,
    }
    host.config = worker.Settings.model_validate(
        host.config.model_dump()
        | {"transcript_course_reextract": False, _LEGACY_DIGEST: None, _AUTO_TIMELINE: data}
    )
    return data


def compute_seam_rows(host):
    seam = host.envelope.start + timedelta(minutes=20)
    return [seam + timedelta(milliseconds=3200 * (i - 96)) for i in range(100)]


def timeline_window_reply(call, specs):
    assert call["purpose"] == _AUTO_PURPOSE
    assert set(call["data"]) == {"instructions", "window"}
    view = call["data"]["window"]
    assert "digest" not in view
    displayed = {r["i"]: r["text"] for r in view["rows"]}

    def anchor(i):
        return {"i": i, "quote": displayed[i][:64]}

    spans = []
    for first, last, engagement, bridged in specs:
        spans.append(
            {
                "first": first,
                "last": last,
                "engagement": engagement,
                "engagement_evidence": None if engagement == "unknown" else anchor(first),
                "continuity": [
                    {"left": anchor(i), "right": anchor(i + 1)}
                    for i in view["review_edges"]
                    if bridged and first <= i < last
                ],
            }
        )
    return {"spans": spans}


@pytest.mark.parametrize("value", [0, 9, True, 3.0, "3", None])
def test_window_review_concurrency_is_strict_bounded_worker_config(tmp_path, value):
    with pytest.raises(ValueError, match="transcript_window_review_concurrency"):
        worker.Settings.model_validate(
            settings(tmp_path, "extraction", transcript_window_review_concurrency=value)
        )


@pytest.mark.parametrize("mode", ["default", "serial", "unknown", "degraded"])
def test_automatic_timeline_wave_orders_receipts_and_never_replays(tmp_path, monkeypatch, mode):
    host = CourseHost(tmp_path, monkeypatch, count=4)
    host.envelope = Interval(start=host.windows[0].start, end=host.windows[-1].end)
    values = host.config.model_dump()
    if mode == "serial":
        values["transcript_window_review_concurrency"] = 1
    else:
        values.pop("transcript_window_review_concurrency")
    host.config = worker.Settings.model_validate(values)
    assert host.config.transcript_window_review_concurrency == (1 if mode == "serial" else 3)
    configure_automatic_timeline(host)
    rows = [
        host.transcript(window.start + timedelta(seconds=30), f"SYNTHETIC_WAVE_{i}")
        for i, window in enumerate(host.windows, 1)
    ]
    # An unclassified row after each activity also keeps it independent when
    # the physical capture coverage is continuous across the fixed views.
    for window in host.windows:
        host.transcript(window.start + timedelta(seconds=35), "unclassified synthetic row")
    owner = threading.get_ident()
    entered = threading.Barrier(3)
    third = threading.Event()
    order_lock = threading.Lock()
    writes, sends, finishes, generations, reconciles = [], {}, [], [], []
    durable = set()
    complete_path = "/v1/complete"

    def transport(req):
        if req.url.host != "synthetic-gateway.invalid":
            assert threading.get_ident() == owner  # Includes Storage query, writes and ACKs.
            response = host.transport(req)
            if req.url.path == "/v1/batches" and response.status_code == 200:
                for op in host.batch_ops(-1):
                    if isinstance(op, PutSystemRecord) and op.data_type == "extraction_run":
                        states = [
                            (c["call_id"], c["state"]) for c in op.payload.get("gateway_calls", [])
                        ]
                        writes.append(states)
                        with order_lock:
                            durable.update(cid for cid, state in states if state == "sending")
            return response
        if req.url.path != complete_path:
            assert threading.get_ident() == owner
            reconciles.append(req.url.path.split("/")[-2])
            return httpx.Response(
                200, json={"record_saved": True, "status": "success", "output": "not recoverable"}
            )
        call = json.loads(req.content)
        cid = call["call_id"]
        with order_lock:
            assert cid in durable
        if call["purpose"] == _AUTO_PURPOSE:
            view = call["data"]["window"]
            first, last = view["core"]
            assert last == first + 1
            index = (first + 1) // 2
            assert "core_time" in view
            with order_lock:
                assert index not in sends
                sends[index] = cid
            if mode != "serial":
                assert threading.get_ident() != owner or index == 4
                if index <= 3:
                    entered.wait(timeout=5)
                    if index != 3:
                        assert third.wait(timeout=5)
            else:
                assert threading.get_ident() == owner
            with order_lock:
                finishes.append(index)
            if index == 3:
                third.set()
            if index == 1 and mode == "unknown":
                raise httpx.ReadTimeout("SYNTHETIC_WAVE_UNKNOWN")
            reply = timeline_window_reply(call, [(first, first, "participation", False)])
            if mode == "degraded" and index == 2:
                reply["spans"][0]["engagement_evidence"]["quote"] = "SYNTHETIC_INVENTED_PROOF"
        else:
            assert threading.get_ident() == owner and call["purpose"] == "generation"
            assert len(finishes) == 4  # Assembly and all receipts before generation.
            [evidence] = call["data"]["evidence"]
            generations.append(evidence["id"])
            reply = course_product(evidence["id"])
        OutputSchema(call["output_schema"]).validate(reply)
        return httpx.Response(200, json={"call_id": cid, "record_saved": True, "output": reply})

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(transport))
    event = host.feedback()
    run, redelivery, overlap = host.run(event, event, host.feedback())
    assert run == redelivery
    if mode == "unknown":
        assert finishes[0] == 3
        assert set(sends) == {1, 2, 3} and not generations
        assert (run.payload["status"], run.payload["error"]) == (
            "blocked",
            "gateway_outcome_unknown",
        )
        assert overlap.payload["error"] == "overlapping_unresolved_model_call"
        assert [i["state"] for i in run.payload["gateway_calls"]] == [
            "sending",
            "returned",
            "returned",
        ]
        assert reconciles == [sends[1]]
        assert all(
            host.client.count(Query(kind)) == 0
            for kind in ("activity", "artifact", "rule_selection")
        )

        def retry(service):
            service.process(run.id)
            with pytest.raises(ValueError, match="gateway_outcome_unknown"):
                service.retry(run.id)
            with pytest.raises(ValueError, match="call_not_in_run"):
                service.reconcile_run(run.id, call_id=new_id())
            receipt = service.reconcile_run(run.id)
            assert receipt["call_id"] == sends[1] and "output" not in receipt

        host.run(extra=retry)  # A fresh worker and GatewayClient, same storage.
        assert reconciles == [sends[1], sends[1]] and len(sends) == 3
    else:
        assert finishes[0] == (1 if mode == "serial" else 3)
        assert run.payload["status"] == "succeeded", run.payload["error"]
        assert overlap.payload["error"] == "automatic_timeline_overlapping_successful_run"
        assert generations == [
            row.id for i, row in enumerate(rows, 1) if i != 2 or mode != "degraded"
        ]
        if mode == "degraded":
            assert run.payload["detail"]["review_proof_degradation"]["downgraded_engagements"] == 1
            assert "SYNTHETIC_INVENTED_PROOF" not in repr(run.payload)
        assert not reconciles
    intents = run.payload["gateway_calls"]
    assert [c["call_id"] for c in intents if c["purpose"] == _AUTO_PURPOSE] == [
        sends[i] for i in sorted(sends)
    ]
    returns = []
    seen = set()
    for states in writes:
        added = [cid for cid, state in states if state == "returned" and cid not in seen]
        assert len(added) <= 1  # A separate, coordinator-owned CAS per returned intent.
        returns.extend(added)
        seen.update(added)
    assert returns[: len(sends) - (mode == "unknown")] == [
        sends[i] for i in sorted(sends) if i != 1 or mode != "unknown"
    ]
    assert run.payload["model_calls"] == [] and "SYNTHETIC_WAVE_" not in repr(run.payload)


def test_automatic_timeline_exact_manual_extract_sparse_and_nonrepeating(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    scope = configure_automatic_timeline(host)
    start, seam = host.windows[0].start, host.windows[1].start
    a = host.transcript(seam - timedelta(seconds=3), "我正在回答你的问题")
    b = host.transcript(seam, "继续回答刚才的问题")
    host.transcript(seam + timedelta(seconds=2), "片段未分类")
    c = host.transcript(seam + timedelta(seconds=4), "我正在听回放")
    host.transcript(seam + timedelta(seconds=6), "片段未分类")
    host.transcript(seam + timedelta(seconds=8), "广播作为背景")
    host.transcript(seam + timedelta(seconds=10), "片段未分类")
    host.transcript(seam + timedelta(seconds=12), "内容与用户关系未知")
    host.replies = [
        lambda call: timeline_window_reply(
            call,
            [
                (1, 2, "participation", True),
                (4, 4, "watching_listening", False),
                (6, 6, "background_playback", False),
                (8, 8, "unknown", False),
            ],
        ),
        course_product(a.id, b.id),
        course_product(c.id),
    ]
    # Load exactly as the hosted worker would, without any configuration discovery.
    host.config = worker.load_config(
        str(write_config(tmp_path, host.config.model_dump(mode="json") | {"storage_token": SECRET}))
    )
    wrong = host.feedback(Command(kind="extract", interval=host.windows[0]))
    event, repeated = host.feedback(), host.feedback()
    bad, run, duplicate, retry = host.run(wrong, event, event, repeated)
    assert duplicate == run
    assert bad.payload["error"] == "automatic_timeline_requires_exact_command_scope"
    assert run.payload["status"] == "succeeded"
    assert retry.payload["error"] == "automatic_timeline_overlapping_successful_run"
    assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE, "generation", "generation"]
    assert run.payload["detail"] == {
        "automatic_timeline": True,
        "review_windows": 1,
        "unavailable_coverage_intervals": 0,
        "candidate_engagement": {
            "participation": 1,
            "watching_listening": 1,
            "background_playback": 1,
            "unknown": 1,
        },
        "materialized_engagement": {"participation": 1, "watching_listening": 1},
        "withheld_candidate_count": 2,
        "activities": 2,
        "course_activities": 0,
        "merged_boundaries": 1,
        "uncertain_boundaries": 6,
        "discarded_boundary_hints": 0,
        "unclassified_rows": 5,
        "covered_rows": 3,
    }
    activities = host.client.query(Query("activity")).items
    assert {r.payload["title_md"] for r in activities} == {"参与活动", "观看／收听"}
    assert {tuple(r.payload["evidence_ids"]) for r in activities} == {(a.id, b.id), (c.id,)}
    assert host.client.count(Query("artifact")) == host.client.count(Query("rule_selection")) == 2
    assert host.client.count(Query("action_suggestion")) == 0
    assert all(
        isinstance(r, SemanticRecord) and r.event_start is not None and r.event_start >= start
        for r in activities
    )
    assert host.snapshot_opens == 0
    assert host.transcript_reads == [("query", TimeRange(w.start, w.end)) for w in host.windows]
    review = repr(host.gateway_calls[0]["data"])
    assert all(v not in review for v in (a.id, b.id, c.id, CANARY, scope["captures"][0]["id"]))
    assert all(
        r["text"] not in repr(run.payload) for r in host.gateway_calls[0]["data"]["window"]["rows"]
    )
    assert all(
        word not in repr(host.gateway_calls[1:]) for word in ("未分类", "广播", "用户关系未知")
    )
    assert run.payload["model_calls"] == []


@pytest.mark.parametrize(
    "problem",
    [
        "quote",
        "quote_row",
        "span_overlap",
        "span_out_of_view",
        "span_bad_order",
        "timeout",
        "empty",
        "seam",
        "proof",
        "edge",
        "gateway_id",
        "gateway_unsaved",
    ],
)
def test_automatic_timeline_bad_window_or_unresolved_review_never_writes_or_replays(
    tmp_path, monkeypatch, problem
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    configure_automatic_timeline(host)
    # Align the legacy row seam with a fixed UTC seam for the standalone test.
    for i, at in enumerate(compute_seam_rows(host)):
        host.transcript(at, f"合成陈述{i}")

    def answer(call):
        view = call["data"]["window"]
        i = view["rows"][0]["i"]
        reply = timeline_window_reply(call, [(i, i, "participation", False)])
        span = reply["spans"][0]
        if problem == "quote":
            span["engagement_evidence"]["quote"] = "secret-not-present"
        elif problem == "quote_row":
            span["engagement_evidence"]["i"] += 1
        elif problem == "proof":
            span["engagement_evidence"] = None
        elif problem == "edge":
            span["continuity"] = [
                {
                    "after_row": i,
                    "left": span["engagement_evidence"],
                    "right": span["engagement_evidence"],
                }
            ]
        elif problem == "span_overlap":
            reply["spans"] *= 2
        elif problem == "span_out_of_view":
            span["last"] = view["rows"][-1]["i"] + 1
        elif problem == "span_bad_order":
            reply = timeline_window_reply(
                call, [(i + 1, i + 1, "participation", False), (i, i, "participation", False)]
            )
        elif problem == "timeout":
            return httpx.ReadTimeout("not safe to resend")
        elif problem == "empty":
            reply["spans"] = []
        elif problem == "seam":
            # Same span near the compute edge without an affirmative bridge.
            return timeline_window_reply(call, [(95, 98, "participation", False)])
        return reply

    host.replies = [answer, answer]
    bad_envelope = problem in {"gateway_id", "gateway_unsaved"}
    if bad_envelope:
        host.replies = [{"spans": [{"first": 1, "last": 1, "engagement": "participation"}]}]
        transport = host.transport

        def invalid_envelope(req):
            response = transport(req)
            if req.url.host != "synthetic-gateway.invalid":
                return response
            invalid = {"call_id": new_id()} if problem == "gateway_id" else {"record_saved": False}
            return httpx.Response(200, json=response.json() | invalid)

        monkeypatch.setattr(
            httpx, "HTTPTransport", lambda **_: httpx.MockTransport(invalid_envelope)
        )
    event, repeat = host.feedback(), host.feedback()
    run, repeated = host.run(event, repeat)
    assert run.payload["status"] == repeated.payload["status"] == "blocked"
    expected = {
        "empty": "automatic_timeline_no_supported_activity",
        "seam": "automatic_timeline_no_supported_activity",
        **dict.fromkeys(
            ("quote", "quote_row", "proof", "edge"), "automatic_timeline_no_supported_activity"
        ),
        "timeout": "gateway_outcome_unknown",
        "gateway_id": "gateway_outcome_unknown",
        "gateway_unsaved": "gateway_outcome_unknown",
    }.get(problem, "automatic_timeline_invalid_window_review")
    assert run.payload["error"] == expected
    reason = {
        **dict.fromkeys(
            ("span_overlap", "span_out_of_view", "span_bad_order"), "span_bounds_or_order"
        ),
    }.get(problem)
    if reason:
        assert run.payload["detail"] == {
            "automatic_timeline_review_failure": {
                "purpose": _AUTO_PURPOSE,
                "window_number": 1,
                "reason": reason,
            }
        }
        assert "secret-not-present" not in repr(run.payload)
        assert run.payload["gateway_calls"][0]["state"] == "returned"
    elif bad_envelope:
        assert run.payload["gateway_calls"][0]["state"] == "sending"
        assert "detail" not in run.payload
    elif problem in {"quote", "quote_row", "proof"}:
        assert run.payload["detail"]["review_proof_degradation"]["downgraded_engagements"] == 2
        assert all(c["state"] == "returned" for c in run.payload["gateway_calls"])
        assert "secret-not-present" not in repr(run.payload)
    assert repeated.payload["error"] == "overlapping_unresolved_model_call"
    planned_two = {"empty", "seam", "quote", "quote_row", "proof", "edge"}
    assert len(host.gateway_calls) == (2 if problem in planned_two else 1)
    assert host.client.count(Query("activity")) == host.client.count(Query("artifact")) == 0
    assert all(call["purpose"] == _AUTO_PURPOSE for call in host.gateway_calls)


@pytest.mark.parametrize(
    "role", ["participation", "watching_listening", "background_playback", "unknown"]
)
def test_automatic_timeline_degrades_one_saved_v2_reply_without_extra_sends(
    tmp_path, monkeypatch, role
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    configure_automatic_timeline(host)
    seam = host.windows[1].start
    texts = ["unproved role", "same proposed context", "unused", "isolated supported role"]
    rows = [
        host.transcript(seam + timedelta(seconds=i * 3 - 3), text) for i, text in enumerate(texts)
    ]

    def review(call):
        reply = timeline_window_reply(call, [(1, 2, role, True), (4, 4, "participation", False)])
        first, last = reply["spans"]
        first["engagement_evidence"] = {"i": 1, "quote": "invented synthetic role proof"}
        first["continuity"][0]["right"]["quote"] = "invented synthetic continuity"
        last["transition_before"] = {
            "left": {"i": 2, "quote": texts[1]},
            "right": {"i": 4, "quote": texts[3]},
        }
        return reply

    host.replies = [review, course_product(rows[-1].id)]
    event = host.feedback()
    run, redelivery, repeat = host.run(event, event, host.feedback())
    assert run == redelivery and run.payload["status"] == "succeeded"
    assert repeat.payload["error"] == "automatic_timeline_overlapping_successful_run"
    assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE, "generation"]
    assert [c["state"] for c in run.payload["gateway_calls"]] == ["returned", "returned"]
    assert run.payload["detail"]["review_proof_degradation"] == {
        "downgraded_engagements": 1,
        "discarded_transitions": 1,
        "discarded_continuity": 1,
    }
    assert run.payload["detail"]["discarded_boundary_hints"] == 2
    assert run.payload["detail"]["merged_boundaries"] == 0
    assert run.payload["detail"]["candidate_engagement"]["unknown"] == 2
    [activity] = host.client.query(Query("activity")).items
    assert activity.payload["evidence_ids"] == [rows[-1].id]
    assert "invented synthetic" not in repr(run.payload)
    assert all(text not in repr(host.gateway_calls[1]) for text in texts[:-1])
    assert not host.reconciles


def test_automatic_timeline_reports_second_window_schema_failure_without_body(
    tmp_path, monkeypatch
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    configure_automatic_timeline(host)
    for i, at in enumerate(compute_seam_rows(host)):
        host.transcript(at, f"合成证据{i}")
    host.replies = [
        lambda call: timeline_window_reply(call, [(1, 1, "participation", False)]),
        {"spans": []},
    ]
    transport = host.transport

    def unexpected_shape(req):
        response = transport(req)
        if req.url.host != "synthetic-gateway.invalid" or len(host.gateway_calls) != 2:
            return response
        # Exercise the extraction-side validator independently of the normal Gateway schema.
        envelope = response.json()
        envelope["output"]["digest"] = "synthetic-never-retain"
        return httpx.Response(200, json=envelope)

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **_: httpx.MockTransport(unexpected_shape))
    [run] = host.run(host.feedback())
    assert run.payload["error"] == "automatic_timeline_invalid_window_review"
    assert run.payload["detail"] == {
        "automatic_timeline_review_failure": {
            "purpose": _AUTO_PURPOSE,
            "window_number": 2,
            "reason": "reply_schema",
        }
    }
    assert "synthetic-never-retain" not in repr(run.payload)
    assert run.payload["model_calls"] == []
    assert len(host.gateway_calls) == 2
    assert all(c["purpose"] == _AUTO_PURPOSE for c in host.gateway_calls)
    assert len({c["call_id"] for c in host.gateway_calls}) == 2
    assert host.client.count(Query("activity")) == host.client.count(Query("rule_selection")) == 0


@pytest.mark.parametrize(
    "problem",
    ["none", "private", "public", "successor", "uncertain", "late_raw", "late_action", "hold"],
)
def test_automatic_timeline_reextract_checks_whole_family_and_never_partially_retires(
    tmp_path, monkeypatch, problem
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    command, targets, (a, b) = replacement_setup(host, bridge=problem != "successor")
    configure_automatic_timeline(host, targets)
    if problem == "uncertain":
        host.transcript(b.event_start - timedelta(seconds=1), "unassigned between candidates")
    if problem == "hold":
        prior_course_run(host, Command(kind="extract", interval=host.windows[0]))
    if problem == "public":
        command = command.model_copy(update={"preview": {targets[0]: 1}})
    if problem == "private":
        selection = host.semantic(targets[2])
        host.client.write(
            [
                Update("rule_selection", selection.id, 1, {**selection.payload, "rule_ids": []}),
                RegisterDependencies("rule_selection", selection.id, []),
            ],
            Context.system(),
        ).check()

    def review(call):
        if problem == "successor":
            spans = [(1, 1, "participation", False)]
        elif problem == "uncertain":
            spans = [(1, 1, "participation", False), (3, 3, "unknown", False)]
        else:
            spans = [(1, 2, "participation", True)]
        return timeline_window_reply(call, spans)

    def generate(_call):
        if problem == "late_raw":
            host.transcript(b.event_end + timedelta(seconds=2), "LATE SYNTHETIC TRANSCRIPT")
        if problem == "late_action":
            host.client.write(
                [
                    Create(
                        "action_suggestion",
                        new_id(),
                        {"artifact_id": targets[1], "state": "pending"},
                    )
                ],
                Context.system(),
            ).check()
        return course_product(a.id, b.id)

    host.replies = [review, generate]
    [run] = host.run(host.feedback(command))
    if problem == "none":
        assert run.payload["status"] == "succeeded" and run.payload["detail"]["course_replacement"]
        assert all(host.semantic(id).superseded for id in targets)
        assert {op.id for op in host.batch_ops(-1) if isinstance(op, Supersede)} == set(targets)
        assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE, "generation"]
        return
    expected, calls = {
        "private": ("automatic_timeline_old_pins_changed", 0),
        "public": ("reextract_preview_changed", 0),
        "successor": ("course_reextract_timeline_old_family_without_successor", 1),
        "uncertain": ("automatic_timeline_old_family_uncertain", 1),
        "late_raw": ("course_reextract_transcript_changed", 2),
        "late_action": ("course_reextract_protected_action", 2),
        "hold": ("overlapping_unresolved_model_call", 0),
    }[problem]
    assert run.payload["status"] == "blocked" and run.payload["error"] == expected
    assert all(not host.semantic(id).superseded for id in targets)
    assert host.client.count(Query("activity")) == len(targets) // 3
    assert len(host.gateway_calls) == calls and not host.reconciles


@pytest.mark.parametrize("problem", ["old_time", "artifact_only_citation", "outside_old_family"])
def test_automatic_timeline_degradation_cannot_newly_retire_old_family(
    tmp_path, monkeypatch, problem
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    touches_time = problem == "old_time"
    loses_citation = problem == "artifact_only_citation"
    command, targets, (a, b) = replacement_setup(
        host, tail=8 if touches_time else 0, artifact_only_citation=loses_citation
    )
    configure_automatic_timeline(host, targets)
    c = host.transcript(
        b.event_start + timedelta(seconds=3), "background row outside original citations"
    )
    host.transcript(c.event_start + timedelta(seconds=3), "second background row")
    host.transcript(c.event_start + timedelta(seconds=6), "unassigned")
    host.transcript(c.event_start + timedelta(seconds=9), "unproved unrelated role")
    before = {id: host.semantic(id) for id in targets}

    def review(call):
        specs = (
            [
                (1, 2, "participation", True),
                (3, 3, "background_playback", False),
                (4, 4, "background_playback", False),
            ]
            if not loses_citation
            else [
                (1, 1, "participation", False),
                (2, 2, "background_playback", False),
                (3, 4, "background_playback", False),
            ]
        )
        reply = timeline_window_reply(call, [*specs, (6, 6, "watching_listening", False)])
        view_rows = {r["i"]: r["text"] for r in call["data"]["window"]["rows"]}
        for prior, current in zip(reply["spans"][:2], reply["spans"][1:3], strict=True):
            left, right = prior["last"], current["first"]
            current["transition_before"] = {
                "left": {"i": left, "quote": view_rows[left]},
                "right": {"i": right, "quote": view_rows[right]},
            }
        if touches_time:
            # Rows 1 and 2 still cover every old citation. The unresolved 3/4
            # edge falls in old covered time and must still veto retirement.
            reply["spans"][2]["transition_before"]["right"]["quote"] = "invalid old-time cut"
        reply["spans"][-1]["engagement_evidence"] = None
        return reply

    host.replies = [review, course_product(a.id, b.id)]
    event = host.feedback(command)
    run, redelivery, repeat = host.run(event, event, host.feedback(command))
    assert run == redelivery
    if problem == "outside_old_family":
        assert run.payload["status"] == "succeeded"
        assert repeat.payload["error"] == "automatic_timeline_overlapping_successful_run"
        assert [call["purpose"] for call in host.gateway_calls] == [_AUTO_PURPOSE, "generation"]
        assert all(host.semantic(id).superseded for id in targets)
        assert all(host.client.get_version(id, 1) == before[id] for id in targets)
        assert not host.reconciles
        return
    assert (run.payload["status"], run.payload["error"]) == (
        "blocked",
        "automatic_timeline_old_family_uncertain",
    )
    assert repeat.payload["error"] == "overlapping_unresolved_model_call"
    assert [call["purpose"] for call in host.gateway_calls] == [_AUTO_PURPOSE]
    assert run.payload["gateway_calls"][0]["state"] == "returned"
    assert {id: host.semantic(id) for id in targets} == before
    assert all(
        host.client.count(Query(kind)) == 1 for kind in ("activity", "artifact", "rule_selection")
    )
    assert not host.reconciles


def test_automatic_timeline_exact_component_config_and_stale_admission(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    preflight = configure_automatic_timeline(host)
    config = host.config.model_dump()
    first, last = preflight["captures"]
    for settings_change, error in [
        ({"transcript_course_sessions": False}, "requires_exclusive_course_mode"),
        ({"transcript_course_reextract": True, _LEGACY_DIGEST: "a" * 64}, "exclusive_course_mode"),
        ({_AUTO_TIMELINE: preflight | {"holds": [host.windows[0].model_dump()]}}, "not_clear"),
        (
            {
                _AUTO_TIMELINE: preflight
                | {"captures": [first | {"end": first["end"] - timedelta(seconds=1)}, last]}
            },
            "not_clear",
        ),
    ]:
        with pytest.raises(ValueError, match=error):
            worker.Settings.model_validate(config | settings_change)
    event = host.feedback()
    host.run(extra=lambda service: service.handle_event(event))
    # Even a different (synthetic) capture projection of the same interval is
    # a new private authorization, not something queued old feedback can adopt.
    configure_automatic_timeline(host)
    [run] = host.run(event)
    assert run.payload["error"] == "automatic_timeline_scope_changed"
    assert host.transcript_reads == host.gateway_calls == []


@pytest.mark.parametrize("existing", ["activity", "artifact", "rule_selection"])
def test_automatic_timeline_rejects_nonempty_extract_before_raw(tmp_path, monkeypatch, existing):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    configure_automatic_timeline(host)
    start = host.envelope.start
    id = new_id()
    host.client.write(
        [
            Create(
                existing,
                id,
                {"activity_id": new_id()},
                event_start=start,
                event_end=start + timedelta(seconds=1),
            ),
            RegisterDependencies(existing, id, []),
        ],
        Context.system(),
    ).check()
    [run] = host.run(host.feedback())
    assert run.payload["error"] in {
        "automatic_timeline_old_pins_changed",
        "automatic_timeline_old_family_out_of_scope",
    }
    assert host.transcript_reads == host.gateway_calls == []


@pytest.mark.parametrize("budget", [True, False])
def test_automatic_timeline_bounded_and_crosses_compute_seam(tmp_path, monkeypatch, budget):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    configure_automatic_timeline(host)
    if budget:
        # Two views and no call left for generation, without an oversized view.
        monkeypatch.setattr(orchestration, "COURSE_MAX_CALLS_PER_WINDOW", 1)
    refs = [host.transcript(at, f"活动证据{i}") for i, at in enumerate(compute_seam_rows(host))]

    def reply(call):
        return timeline_window_reply(call, [(95, 98, "participation", True)])

    ids = [r.id for r in refs[94:98]]
    host.replies = [reply, reply, course_product(*ids)]
    [run] = host.run(host.feedback())
    if budget:
        assert run.payload["error"] == "automatic_timeline_review_budget_exceeded"
        assert not host.gateway_calls
    else:
        assert run.payload["status"] == "succeeded"
        assert run.payload["detail"]["merged_boundaries"] == 1
        assert [c["purpose"] for c in host.gateway_calls] == [
            _AUTO_PURPOSE,
            _AUTO_PURPOSE,
            "generation",
        ]
        [activity] = host.client.query(Query("activity")).items
        assert activity.payload["evidence_ids"] == ids


@pytest.mark.parametrize(
    ("problem", "error"),
    [
        ("empty", "session_windows_without_transcript"),
        ("blank", "course_reextract_blank_transcript_rows"),
        ("outside", "automatic_timeline_invalid_input"),
        ("action_rule", "transcript_summary_rule_ineligible"),
    ],
)
def test_automatic_timeline_does_not_send_without_usable_scope_and_no_action_rule(
    tmp_path, monkeypatch, problem, error
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    configure_automatic_timeline(host)
    first, last = host.windows
    host.transcript(first.start + timedelta(seconds=2), "synthetic")
    if problem != "empty":
        host.transcript(last.start + timedelta(seconds=2), "synthetic")
    if problem == "blank":
        host.transcript(last.start + timedelta(seconds=3), "   ")
    if problem == "outside":
        host.transcript(last.end - timedelta(seconds=1), "straddling", seconds=2)
    if problem == "action_rule":
        rule = host.semantic(host.rule_id)
        host.client.write(
            [Update("rule", rule.id, rule.version, rule.payload | {"action": "calendar"})],
            Context.system(),
        ).check()
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "blocked" and run.payload["error"] == error
    assert not host.gateway_calls
    assert (
        host.client.count(Query("activity")) == host.client.count(Query("action_suggestion")) == 0
    )


def set_prewrite_pins(host, *pins):
    host.config = worker.Settings.model_validate(host.config.model_dump() | {_AUTO_PREWRITE: pins})


def prewrite_pin(host, run):
    return {
        "run_id": run.id,
        "scope": host.envelope.model_dump(mode="json"),
        "metadata_sha256": prewrite_run_metadata_sha256(run),
        "proof": "all_calls_saved_success_no_business_write",
        "calls": [
            {
                "call_id": call["call_id"],
                "record_saved": True,
                "status": "success",
                "error_code": None,
                "response_recoverable": False,
            }
            for call in run.payload["gateway_calls"]
        ],
    }


def revise_prewrite_run(host, run, **changes):
    host.client.write(
        [
            PutSystemRecord(
                "extraction_run", run.id, run.payload | changes, expected_revision=run.revision
            )
        ],
        Context.feedback(run.payload["root_feedback_id"]),
    ).check()
    changed = host.client.get_system_record(run.id)
    assert changed is not None
    return changed


_LEGACY_REVIEW_PROOF = "legacy_window_review_all_saved_success_no_business_write"
_LEGACY_REVIEW_PURPOSE = "timeline_window_review"


def legacy_review_run(host, command, preflight, *, pins=(), error=None, purposes=None):
    # Rebuild the exact v1 canonical hash independently, with the old prefix.
    parts = [
        PreflightInput.model_validate(preflight).model_dump(mode="json"),
        [w.model_dump(mode="json") for w in host.windows],
        host.rule_id,
    ]
    if pins:
        parts.append(sorted(pins, key=lambda p: p["run_id"]))
    sha = hashlib.sha256(
        b"ear.automatic-timeline-scope/1\0"
        + json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    event, _, _ = prior_course_run(
        host,
        command,
        purposes=purposes or (_LEGACY_REVIEW_PURPOSE, _LEGACY_REVIEW_PURPOSE),
        error=error or "automatic_timeline_invalid_window_review",
        sending=False,
    )
    return revise_prewrite_run(
        host, host.run_record(event), status="blocked", automatic_timeline_scope_sha256=sha
    )


@pytest.mark.parametrize(
    "mode",
    ["empty", "original_pins", "replace", "invalid_v2", "degraded_v2", "unknown_v2", "protected"],
)
def test_automatic_timeline_legacy_review_exact_pinned_one_trial(tmp_path, monkeypatch, mode):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    replacement = mode in {"replace", "protected"}
    if replacement:
        command, targets, (a, b) = replacement_setup(host)
    else:
        seam = host.windows[1].start
        a = host.transcript(seam - timedelta(seconds=3), "合成参与")
        b = host.transcript(seam, "合成参与持续")
        command, targets = Command(kind="extract", interval=host.envelope), ()
    prior_pins = []
    if mode == "original_pins":
        prior, _, _ = prior_course_run(
            host,
            Command(kind="extract", interval=host.windows[0]),
            error="ValueError",
            sending=False,
        )
        prior_pins.append(prewrite_pin(host, host.run_record(prior)))
    preflight = configure_automatic_timeline(host, targets)
    old = legacy_review_run(host, command, preflight, pins=prior_pins)
    [default] = host.run(host.feedback(command))
    assert default.payload["error"] == "overlapping_unresolved_model_call"
    pin = prewrite_pin(host, old) | {"proof": _LEGACY_REVIEW_PROOF}
    if mode == "original_pins":
        set_prewrite_pins(host, pin)
        [incomplete] = host.run(host.feedback(command))
        assert incomplete.payload["error"] == "overlapping_unresolved_model_call"
        assert not host.gateway_calls
    set_prewrite_pins(host, pin, *prior_pins)
    host.config = worker.load_config(
        str(write_config(tmp_path, host.config.model_dump(mode="json") | {"storage_token": SECRET}))
    )
    if mode == "protected":
        assert len(targets) >= 2
        host.client.write(
            [
                Create(
                    "action_suggestion", new_id(), {"artifact_id": targets[1], "state": "pending"}
                )
            ],
            Context.system(),
        ).check()

    def review(call):
        intent = host.client.get_system_record(call["run_id"])
        assert intent is not None
        assert intent.payload["automatic_timeline_v1_predecessor_run_id"] == old.id
        if mode == "unknown_v2":
            return httpx.ReadTimeout("synthetic unknown")
        reply = timeline_window_reply(call, [(1, 2, "participation", True)])
        if mode == "invalid_v2":
            reply["spans"][0]["last"] = 3  # Strict bounds still block the trial.
        elif mode == "degraded_v2":
            reply["spans"][0]["engagement_evidence"]["quote"] = "not in evidence"
        return reply

    host.replies = [review, course_product(a.id, b.id)]
    event = host.feedback(command)
    run, duplicate, second = host.run(event, event, host.feedback(command))
    assert run == duplicate
    assert host.client.get_system_record(old.id) == old
    assert host.reconciles == (
        [f"/v1/calls/{host.gateway_calls[0]['call_id']}/reconcile"] if mode == "unknown_v2" else []
    )
    assert {c["call_id"] for c in old.payload["gateway_calls"]}.isdisjoint(
        c["call_id"] for c in host.gateway_calls
    )
    if mode == "protected":
        assert run.payload["error"] == "course_reextract_protected_action"
        assert not host.gateway_calls and all(not host.semantic(id).superseded for id in targets)
    else:
        assert second.payload["error"] == "automatic_timeline_legacy_trial_already_used"
        assert run.payload["automatic_timeline_v1_predecessor_run_id"] == old.id
        if mode in {"empty", "original_pins", "replace"}:
            assert run.payload["status"] == "succeeded"
            assert run.payload["model_calls"] == []
            if replacement:
                assert run.payload["detail"]["course_replacement"] is True
            assert all(host.semantic(id).superseded for id in targets)
            assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE, "generation"]
            assert host.client.count(Query("activity")) == 1
        else:
            assert (
                run.payload["error"]
                == {
                    "unknown_v2": "gateway_outcome_unknown",
                    "invalid_v2": "automatic_timeline_invalid_window_review",
                    "degraded_v2": "automatic_timeline_no_supported_activity",
                }[mode]
            )
            assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE]
            assert host.client.count(Query("activity")) == 0
            # Pin removal/restoration can't reset the durable trial marker.
            set_prewrite_pins(host)
            [unpinned] = host.run(host.feedback(command))
            assert unpinned.payload["error"] == "overlapping_unresolved_model_call"
            set_prewrite_pins(host, pin)
            [repinned] = host.run(host.feedback(command))
            assert repinned.payload["error"] == "automatic_timeline_legacy_trial_already_used"
            assert len(host.gateway_calls) == 1


@pytest.mark.parametrize(
    "problem",
    [
        "scope",
        "wider",
        "preview",
        "generic_pin",
        "sending",
        "generation",
        "v2",
        "unknown",
        "missing_audit",
        "drift",
        "duplicate",
        "marker",
    ],
)
def test_automatic_timeline_legacy_review_pin_rejects_unproven_run(tmp_path, monkeypatch, problem):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    targets = ()
    command = Command(kind="extract", interval=host.envelope)
    previous = command
    if problem == "wider":
        previous = command.model_copy(
            update={
                "interval": Interval(
                    start=host.envelope.start - timedelta(seconds=1), end=host.envelope.end
                )
            }
        )
    if problem == "preview":
        command, targets, _ = replacement_setup(host)
        previous = command.model_copy(update={"preview": {targets[0]: 1}})
    preflight = configure_automatic_timeline(host, targets)
    purpose = {"generation": "generation", "v2": _AUTO_PURPOSE}.get(problem, _LEGACY_REVIEW_PURPOSE)
    old = legacy_review_run(
        host,
        previous,
        preflight,
        purposes=(_LEGACY_REVIEW_PURPOSE, purpose),
        error="gateway_outcome_unknown" if problem == "unknown" else None,
    )
    if problem == "sending":
        calls = old.payload["gateway_calls"]
        old = revise_prewrite_run(
            host, old, gateway_calls=[calls[0], calls[1] | {"state": "sending"}]
        )
    if problem == "duplicate":
        call = old.payload["gateway_calls"][0]
        old = revise_prewrite_run(host, old, gateway_calls=[call, call])
    if problem == "marker":
        old = revise_prewrite_run(host, old, automatic_timeline_v1_predecessor_run_id=new_id())
    pin = prewrite_pin(host, old)
    if problem != "generic_pin":
        pin["proof"] = _LEGACY_REVIEW_PROOF
    if problem == "scope":
        # Same wall-clock extent but different capture identity and hence different scope.
        configure_automatic_timeline(host)
    if problem == "drift":
        old = revise_prewrite_run(host, old, detail={})
    if problem == "missing_audit":
        pin["calls"].pop()
    set_prewrite_pins(host, pin)
    [new] = host.run(host.feedback(command))
    assert new.payload["error"] == {
        "missing_audit": "automatic_timeline_prewrite_audit_mismatch",
        "drift": "automatic_timeline_prewrite_pin_changed",
    }.get(problem, "overlapping_unresolved_model_call")
    assert host.gateway_calls == host.reconciles == host.transcript_reads == []
    assert host.client.get_system_record(old.id) == old


@pytest.mark.parametrize("predecessor", ["all_unclassified", "larger_failure"])
def test_automatic_timeline_prewrite_starts_only_fresh_calls(tmp_path, monkeypatch, predecessor):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    row = host.transcript(host.windows[1].start - timedelta(seconds=3), "合成参与")
    host.transcript(host.windows[1].start, "合成旁话")
    if predecessor == "all_unclassified":
        host.replies = [{"spans": []}]
        [old] = host.run(host.feedback())
        assert old.payload["error"] == "session_all_rows_unclassified"
    else:
        wider = Interval(start=host.envelope.start - timedelta(days=1), end=host.envelope.end)
        event, _, _ = prior_course_run(
            host,
            Command(kind="extract", interval=wider),
            purposes=("course_blocks", "course_blocks", "generation"),
            error="ValidationError",
            sending=False,
        )
        old = host.run_record(event)
    configure_automatic_timeline(host)
    [unpinned] = host.run(host.feedback())
    assert unpinned.payload["error"] == "overlapping_unresolved_model_call"
    set_prewrite_pins(host, prewrite_pin(host, old))
    host.config = worker.load_config(
        str(write_config(tmp_path, host.config.model_dump(mode="json") | {"storage_token": SECRET}))
    )
    host.gateway_calls.clear()
    host.replies = [
        lambda call: timeline_window_reply(call, [(1, 1, "participation", False)]),
        course_product(row.id),
    ]
    new = host.feedback()
    run, repeated = host.run(new, new)
    assert run == repeated and run.payload["status"] == "succeeded"
    assert host.client.get_system_record(old.id) == old
    assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE, "generation"]
    assert all(c["run_id"] == new.event_id for c in host.gateway_calls)
    assert {c["call_id"] for c in old.payload["gateway_calls"]}.isdisjoint(
        c["call_id"] for c in host.gateway_calls
    )
    assert host.reconciles == []
    assert host.client.count(Query("activity")) == 1
    assert host.client.count(Query("action_suggestion")) == 0
    [replay] = host.run(host.feedback())
    assert replay.payload["error"] == "automatic_timeline_overlapping_successful_run"
    assert len(host.gateway_calls) == 2


@pytest.mark.parametrize(
    "problem,expected",
    [
        ("sending", "overlapping_unresolved_model_call"),
        ("unknown", "overlapping_unresolved_model_call"),
        ("pending", "overlapping_unresolved_model_call"),
        ("unknown_commit", "overlapping_unresolved_model_call"),
        ("valid_uncommitted", "overlapping_unresolved_model_call"),
        ("unclassified_generation", "overlapping_unresolved_model_call"),
        ("other_unpinned", "overlapping_unresolved_model_call"),
        ("other_unknown_commit_without_intents", "overlapping_unresolved_model_call"),
        ("legacy_succeeded", "automatic_timeline_overlapping_successful_run"),
        ("different_uuid", "overlapping_unresolved_model_call"),
        ("not_in_scope", "automatic_timeline_unused_prewrite_pin"),
        ("audit_missing", "automatic_timeline_prewrite_audit_mismatch"),
        ("audit_reordered", "automatic_timeline_prewrite_audit_mismatch"),
        ("changed", "automatic_timeline_prewrite_pin_changed"),
        ("retired", "automatic_timeline_prewrite_scope_has_business_objects"),
    ],
)
def test_automatic_timeline_prewrite_is_fail_closed(tmp_path, monkeypatch, problem, expected):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    window = host.windows[0]
    if problem == "not_in_scope":
        window = Interval(start=window.start - timedelta(minutes=15), end=window.start)
    event, _, _ = prior_course_run(
        host,
        Command(kind="extract", interval=window),
        purposes=("course_blocks", "generation"),
        error="ValueError",
        sending=problem == "sending",
    )
    old = host.run_record(event)
    if problem in {
        "unknown",
        "pending",
        "unknown_commit",
        "valid_uncommitted",
        "unclassified_generation",
    }:
        status, error = {
            "unknown": ("blocked", "gateway_outcome_unknown"),
            "pending": ("queued", None),
            "unknown_commit": ("blocked", "CommitUnknown"),
            "valid_uncommitted": ("blocked", "gateway_result_not_committed"),
            "unclassified_generation": ("blocked", "session_all_rows_unclassified"),
        }[problem]
        old = revise_prewrite_run(host, old, status=status, error=error)
    if problem in {"other_unpinned", "other_unknown_commit_without_intents", "legacy_succeeded"}:
        other, _, _ = prior_course_run(
            host, Command(kind="extract", interval=window), error="ValueError", sending=False
        )
        if problem == "other_unknown_commit_without_intents":
            revise_prewrite_run(
                host,
                host.run_record(other),
                status="blocked",
                error="CommitUnknown",
                gateway_calls=[],
            )
        if problem == "legacy_succeeded":
            revise_prewrite_run(
                host,
                host.run_record(other),
                status="succeeded",
                error=None,
                detail={"automatic_timeline": False},
            )
    if problem == "retired":
        host.prior_activity(window.start, superseded=True)
    pin = prewrite_pin(host, old)
    if problem == "different_uuid":
        pin["run_id"] = new_id()
    if problem == "audit_missing":
        pin["calls"].pop()
    if problem == "audit_reordered":
        pin["calls"].reverse()
    if problem == "changed":
        old = revise_prewrite_run(host, old, error="ValidationError")
    configure_automatic_timeline(host)
    set_prewrite_pins(host, pin)
    [new] = host.run(host.feedback())
    assert new.payload["error"] == expected
    assert host.client.get_system_record(old.id) == old
    assert host.gateway_calls == host.reconciles == host.transcript_reads == []
    assert host.client.count(Query("artifact")) == 0


def test_automatic_timeline_prewrite_pins_are_private_and_scope_bound(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    event, _, _ = prior_course_run(
        host,
        Command(kind="extract", interval=host.windows[0]),
        error="ValueError",
        sending=False,
    )
    pin = prewrite_pin(host, host.run_record(event))
    configure_automatic_timeline(host)
    config = host.config.model_dump()
    for change in (
        {_AUTO_TIMELINE: None, _AUTO_PREWRITE: [pin]},
        {_AUTO_PREWRITE: [pin, pin]},
        {_AUTO_PREWRITE: [pin | {"scope": host.windows[0].model_dump(mode="json")}]},
        {_AUTO_PREWRITE: [pin | {"proof": "unverified"}]},
        {_AUTO_PREWRITE: [pin | {"calls": [pin["calls"][0] | {"record_saved": False}]}]},
        {_AUTO_PREWRITE: [pin | {"calls": [pin["calls"][0] | {"status": "pending"}]}]},
        {_AUTO_PREWRITE: [pin | {"calls": [pin["calls"][0] | {"status": "failed"}]}]},
        {_AUTO_PREWRITE: [pin | {"calls": [pin["calls"][0] | {"response_recoverable": True}]}]},
    ):
        with pytest.raises(ValueError):
            worker.Settings.model_validate(config | change)
    stale = host.feedback()
    host.run(extra=lambda service: service.handle_event(stale))
    set_prewrite_pins(host, pin)
    [new] = host.run(stale)
    assert new.payload["error"] == "automatic_timeline_scope_changed"
    assert host.gateway_calls == host.reconciles == []


@pytest.mark.parametrize("stage", ["review", "generation"])
def test_automatic_timeline_prewrite_rechecks_after_fresh_call(tmp_path, monkeypatch, stage):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    row = host.transcript(host.windows[1].start - timedelta(seconds=3), "合成参与")
    host.transcript(host.windows[1].start, "合成旁话")
    event, _, _ = prior_course_run(
        host,
        Command(kind="extract", interval=host.windows[0]),
        error="ValueError",
        sending=False,
    )
    old = host.run_record(event)
    configure_automatic_timeline(host)
    set_prewrite_pins(host, prewrite_pin(host, old))
    changes = []

    def change():
        changes.append(revise_prewrite_run(host, old, error="ValidationError"))

    def review(call):
        if stage == "review":
            change()
        return timeline_window_reply(call, [(1, 1, "participation", False)])

    def generate(_call):
        change()
        return course_product(row.id)

    host.replies = [review, generate]
    [new] = host.run(host.feedback())
    assert new.payload["error"] == "automatic_timeline_prewrite_pin_changed"
    assert len(host.gateway_calls) == (1 if stage == "review" else 2)
    assert host.reconciles == []
    assert host.client.count(Query("activity")) == 0
    assert host.client.get_system_record(old.id) == changes[0]


_EMPTY_SUCCESS_PROOF = "all_calls_saved_success_outputs_only_outside_scope"


def legacy_success_outside_component(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=3, origin_offset=120)
    old_scope = Interval(start=host.windows[0].start, end=host.windows[-1].end)
    outside = host.transcript(host.windows[0].end - timedelta(seconds=1), "合成旧课堂")
    first = host.transcript(host.windows[2].start - timedelta(seconds=2), "合成新参与")
    host.transcript(host.windows[2].start, "合成新旁话")
    host.replies = [{"spans": [course_span("合成旧课堂")]}, course_product(outside.id)]
    [old] = host.run(host.feedback(Command(kind="extract", interval=old_scope)))
    assert old.payload["status"] == "succeeded"
    assert old.payload["detail"]["activities"] == 1
    [activity] = host.client.query(Query("activity")).items
    assert activity.event_end == host.envelope.start  # Half-open boundary.
    host.windows = host.selected
    host.config = worker.Settings.model_validate(
        host.config.model_dump() | {"transcript_windows": host.windows}
    )
    configure_automatic_timeline(host)
    host.gateway_calls.clear()
    host.transcript_reads.clear()
    return host, old, activity, first


def empty_success_pin(host, old, activity):
    return prewrite_pin(host, old) | {
        "proof": _EMPTY_SUCCESS_PROOF,
        "outside_activity_ids": [activity.id],
        "scope_history": {
            kind: {
                "revision": host.client.type_revision(kind),
                "versions": sum(
                    item.version
                    for item in host.client.query(Query(kind, include_superseded=True)).items
                ),
            }
            for kind in ("activity", "artifact", "rule_selection")
        },
    }


def test_automatic_timeline_empty_success_is_explicit_and_never_replays(tmp_path, monkeypatch):
    host, old, outside, first = legacy_success_outside_component(tmp_path, monkeypatch)
    old_family = {
        kind: host.client.query(Query(kind)).items
        for kind in ("activity", "artifact", "rule_selection")
    }
    [unpinned] = host.run(host.feedback())
    assert unpinned.payload["error"] == "automatic_timeline_overlapping_successful_run"
    set_prewrite_pins(host, prewrite_pin(host, old))
    [wrong_proof] = host.run(host.feedback())
    assert wrong_proof.payload["error"] == "overlapping_unresolved_model_call"
    assert host.gateway_calls == host.transcript_reads == []
    set_prewrite_pins(host, empty_success_pin(host, old, outside))
    host.config = worker.load_config(
        str(write_config(tmp_path, host.config.model_dump(mode="json") | {"storage_token": SECRET}))
    )
    wrong = host.feedback(Command(kind="extract", interval=host.windows[0]))
    replacing = host.feedback(Command(kind="reextract", interval=host.envelope, preview={}))
    bad, reextract = host.run(wrong, replacing)
    assert (
        bad.payload["error"]
        == reextract.payload["error"]
        == ("automatic_timeline_requires_exact_command_scope")
    )
    assert host.gateway_calls == host.transcript_reads == []
    host.replies = [
        lambda call: timeline_window_reply(call, [(1, 1, "participation", False)]),
        course_product(first.id),
    ]
    event = host.feedback()
    run, same_delivery, fresh_replay = host.run(event, event, host.feedback())
    assert run == same_delivery and run.payload["status"] == "succeeded", run.payload["error"]
    assert fresh_replay.payload["error"] == "automatic_timeline_overlapping_successful_run"
    assert [call["purpose"] for call in host.gateway_calls] == [_AUTO_PURPOSE, "generation"]
    assert {call["run_id"] for call in host.gateway_calls} == {run.id}
    assert {call["call_id"] for call in old.payload["gateway_calls"]}.isdisjoint(
        call["call_id"] for call in host.gateway_calls
    )
    assert host.reconciles == [] and host.client.get_system_record(old.id) == old
    for kind, (record,) in old_family.items():
        assert host.semantic(record.id) == record
        assert host.client.count(Query(kind)) == 2
    assert host.client.count(Query("action_suggestion")) == 0


@pytest.mark.parametrize(
    "problem,expected",
    [
        ("same_scope", "overlapping_unresolved_model_call"),
        ("partial_scope", "overlapping_unresolved_model_call"),
        ("auto", "overlapping_unresolved_model_call"),
        ("scheduled", "overlapping_unresolved_model_call"),
        ("replacement", "overlapping_unresolved_model_call"),
        ("pending", "overlapping_unresolved_model_call"),
        ("sending", "overlapping_unresolved_model_call"),
        ("unknown", "overlapping_unresolved_model_call"),
        ("zero", "overlapping_unresolved_model_call"),
        ("missing_witness", "automatic_timeline_empty_success_outputs_unverified"),
        ("unrelated_witness", "automatic_timeline_empty_success_outputs_unverified"),
        ("missing_audit", "automatic_timeline_prewrite_audit_mismatch"),
        ("wrong_audit", "automatic_timeline_prewrite_audit_mismatch"),
        ("other_pending", "overlapping_unresolved_model_call"),
        ("stale_history", "automatic_timeline_empty_success_history_changed"),
        ("missing_history", "automatic_timeline_empty_success_history_unverified"),
    ],
)
def test_automatic_timeline_empty_success_fails_closed(tmp_path, monkeypatch, problem, expected):
    host, old, outside, _ = legacy_success_outside_component(tmp_path, monkeypatch)
    assert outside.event_start is not None
    if problem in {"same_scope", "partial_scope"}:
        scope = (
            host.envelope
            if problem == "same_scope"
            else Interval(start=outside.event_start, end=host.windows[0].end)
        )
        p = old.payload | {
            "command": Command(kind="extract", interval=scope).model_dump(mode="json")
        }
        host.client.write(
            [
                PutSystemRecord(
                    "extraction_run",
                    old.id,
                    p,
                    expected_revision=old.revision,
                    event_start=scope.start,
                    event_end=scope.end,
                )
            ],
            Context.feedback(old.payload["root_feedback_id"]),
        ).check()
        revised = host.client.get_system_record(old.id)
        assert revised is not None
        old = revised
    elif problem in {"auto", "scheduled", "replacement", "pending", "sending", "unknown", "zero"}:
        changes = {
            "auto": {"automatic_timeline_scope_sha256": "a" * 64},
            "scheduled": {"scheduled": True},
            "replacement": {
                "command": old.payload["command"] | {"kind": "reextract", "preview": {}}
            },
            "pending": {"status": "running"},
            "sending": {
                "gateway_calls": [
                    old.payload["gateway_calls"][0],
                    old.payload["gateway_calls"][1] | {"state": "sending"},
                ]
            },
            "unknown": {"error": "gateway_outcome_unknown"},
            "zero": {"detail": {"activities": 0}},
        }[problem]
        old = revise_prewrite_run(host, old, **changes)
    pin = empty_success_pin(host, old, outside)
    if problem == "missing_witness":
        pin["outside_activity_ids"] = [new_id()]
    elif problem == "unrelated_witness":
        pin["outside_activity_ids"] = [
            host.prior_activity(host.windows[0].start - timedelta(seconds=1))
        ]
    elif problem == "missing_audit":
        pin["calls"].pop()
    elif problem == "wrong_audit":
        pin["calls"][0]["call_id"] = new_id()
    elif problem == "other_pending":
        other = host.feedback()
        host.run(extra=lambda service: service.handle_event(other))
    elif problem == "stale_history":
        host.prior_activity(outside.event_start)
    elif problem == "missing_history":
        # Model a head obscured by Storage's causal visibility filter.
        original_query = host.client.query

        def hide_artifacts(query):
            if query.data_type == "artifact" and query.time is None:
                return Page((), None)
            return original_query(query)

        monkeypatch.setattr(host.client, "query", hide_artifacts)
    set_prewrite_pins(host, pin)
    [new] = host.run(host.feedback())
    assert new.payload["error"] == expected
    assert host.client.get_system_record(old.id) == old
    assert host.gateway_calls == host.reconciles == host.transcript_reads == []


@pytest.mark.parametrize("kind", ["activity", "artifact", "rule_selection"])
@pytest.mark.parametrize("state", ["active", "superseded", "moved_out"])
def test_automatic_timeline_empty_success_checks_every_business_version(
    tmp_path, monkeypatch, kind, state
):
    host, old, outside, _ = legacy_success_outside_component(tmp_path, monkeypatch)
    id = new_id()
    payload = {"activity_id": new_id()}
    start = host.envelope.start
    host.client.write(
        [
            Create(kind, id, payload, event_start=start, event_end=start + timedelta(seconds=1)),
            RegisterDependencies(kind, id, []),
        ],
        Context.system(),
    ).check()
    if state == "superseded":
        host.client.write([Supersede(kind, id, 1)], Context.system()).check()
    elif state == "moved_out":
        host.client.write(
            [
                Update(
                    kind,
                    id,
                    1,
                    payload,
                    event_start=outside.event_start,
                    event_end=outside.event_end,
                ),
                RegisterDependencies(kind, id, []),
            ],
            Context.system(),
        ).check()
        assert not host.client.query(
            Query(kind, time=TimeRange(start, host.envelope.end), include_superseded=True)
        ).items
    set_prewrite_pins(host, empty_success_pin(host, old, outside))
    [new] = host.run(host.feedback())
    assert new.payload["error"] == "automatic_timeline_prewrite_scope_has_business_objects"
    assert host.gateway_calls == host.reconciles == host.transcript_reads == []


@pytest.mark.parametrize("stage", ["review", "generation"])
def test_automatic_timeline_empty_success_rechecks_history_between_sends_and_before_commit(
    tmp_path, monkeypatch, stage
):
    host, old, outside, first = legacy_success_outside_component(tmp_path, monkeypatch)
    set_prewrite_pins(host, empty_success_pin(host, old, outside))

    def appear_and_move():
        start = host.envelope.start
        id = host.prior_activity(start)
        host.client.write(
            [
                Update(
                    "activity",
                    id,
                    1,
                    {"title_md": "moved"},
                    event_start=outside.event_start,
                    event_end=outside.event_end,
                ),
                RegisterDependencies("activity", id, []),
            ],
            Context.system(),
        ).check()

    def review(call):
        if stage == "review":
            appear_and_move()
        return timeline_window_reply(call, [(1, 1, "participation", False)])

    def generate(_call):
        appear_and_move()
        return course_product(first.id)

    host.replies = [review, generate]
    [new] = host.run(host.feedback())
    assert new.payload["error"] == "automatic_timeline_empty_success_history_changed"
    assert len(host.gateway_calls) == (1 if stage == "review" else 2)
    assert host.reconciles == [] and host.client.get_system_record(old.id) == old
    assert host.client.count(Query("activity")) == 2  # No activity from the new run.
    assert host.client.count(Query("artifact")) == host.client.count(Query("rule_selection")) == 1


def test_automatic_timeline_empty_success_proof_has_closed_fields(tmp_path, monkeypatch):
    host, old, outside, _ = legacy_success_outside_component(tmp_path, monkeypatch)
    pin = empty_success_pin(host, old, outside)
    for invalid in (
        prewrite_pin(host, old) | {"outside_activity_ids": [outside.id]},
        prewrite_pin(host, old) | {"proof": _EMPTY_SUCCESS_PROOF},
        pin | {"outside_activity_ids": [outside.id, outside.id]},
        pin | {"calls": [pin["calls"][0] | {"status": "pending"}]},
    ):
        with pytest.raises(ValueError):
            set_prewrite_pins(host, invalid)


def test_automatic_timeline_empty_success_cannot_authorize_already_admitted_feedback(
    tmp_path, monkeypatch
):
    host, old, outside, _ = legacy_success_outside_component(tmp_path, monkeypatch)
    event = host.feedback()
    host.run(extra=lambda service: service.handle_event(event))
    set_prewrite_pins(host, empty_success_pin(host, old, outside))
    [new] = host.run(event)
    assert new.payload["error"] == "automatic_timeline_scope_changed"
    assert host.gateway_calls == host.transcript_reads == []


_GAPPED = "transcript_automatic_timeline_gaps"


_ASR_GAPS = "transcript_automatic_timeline_asr_gaps"


def asr_gap_setup(host):
    origin = host.envelope.start
    gaps = (
        ExactInterval(start=origin + timedelta(minutes=8), end=origin + timedelta(minutes=9)),
        ExactInterval(start=origin + timedelta(minutes=13), end=host.envelope.end),
    )
    preflight = configure_automatic_timeline(host)
    host.config = worker.Settings.model_validate(host.config.model_dump() | {_ASR_GAPS: gaps})
    return preflight, gaps


def asr_row(host, start, end, capture_id, text="synthetic observed evidence"):
    row = make_raw(
        "transcript_segment",
        {"text": text, "audio_path": CANARY}
        | ({"audio_ref_id": capture_id} if capture_id is not None else {}),
        start=start,
        end=end,
    )
    host.memory.client("adapter").write([AppendRaw(row)], Context.system()).check()
    return row


def test_automatic_timeline_asr_gaps_split_same_core_without_clipping_captures(
    tmp_path, monkeypatch
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    preflight, (gap, tail) = asr_gap_setup(host)
    capture_id = preflight["captures"][0]["id"]
    left = asr_row(host, gap.start - timedelta(seconds=2), gap.start, capture_id, "left synthetic")
    right = asr_row(host, gap.end, gap.end + timedelta(seconds=2), capture_id, "right synthetic")

    def review(start, end, row):
        def reply(call):
            view = call["data"]["window"]
            assert view["block_coverage"] == {"start": format_utc(start), "end": format_utc(end)}
            assert view["core_time"]["start"] == format_utc(start)
            assert view["core_time"]["end"] == format_utc(
                min(end, host.envelope.start + timedelta(minutes=10))
            )
            [item] = view["rows"]
            assert item["start"] == format_utc(row.event_start)
            assert item["end"] == format_utc(row.event_end)
            assert view["review_edges"] == []
            return timeline_window_reply(call, [(item["i"], item["i"], "participation", True)])

        return reply

    def generate(row):
        def reply(call):
            assert "Do not treat its edges" in call["data"]["supplemental_instructions"]
            assert [item["id"] for item in call["data"]["evidence"]] == [row.id]
            return course_product(row.id)

        return reply

    host.replies = [
        review(host.envelope.start, gap.start, left),
        review(gap.end, tail.start, right),
        generate(left),
        generate(right),
    ]
    host.config = worker.load_config(
        str(write_config(tmp_path, host.config.model_dump(mode="json") | {"storage_token": SECRET}))
    )
    old_command = Command(kind="reextract", interval=host.envelope, preview={})
    [wrong, run, repeat] = host.run(host.feedback(old_command), host.feedback(), host.feedback())
    assert wrong.payload["error"] == "automatic_timeline_requires_exact_command_scope"
    assert repeat.payload["error"] == "automatic_timeline_overlapping_successful_run"
    assert run.payload["status"] == "succeeded"
    detail = run.payload["detail"]
    assert (
        detail["declared_asr_gap_intervals"]
        == detail["covered_rows"]
        == detail["review_windows"]
        == 2
    )
    assert detail["merged_boundaries"] == detail["discarded_boundary_hints"] == 0
    assert [t for method, t in host.transcript_reads if method == "query"] == [
        TimeRange(w.start, w.end) for w in host.windows
    ]
    assert all(q.data_type != "audio_ref" for _, q in host.queries)
    assert host.snapshot_opens == 0
    assert all(
        capture_id not in json.dumps(call) and CANARY not in json.dumps(call)
        for call in host.gateway_calls
    )
    assert set(
        (a.event_start, a.event_end, tuple(a.payload["evidence_ids"]))
        for a in host.client.query(Query("activity")).items
    ) == {(r.event_start, r.event_end, (r.id,)) for r in (left, right)}
    assert host.client.count(Query("artifact")) == host.client.count(Query("rule_selection")) == 2


@pytest.mark.parametrize(
    ("defect", "error"),
    [
        ("inside", "automatic_timeline_transcript_overlaps_asr_gap"),
        ("crosses", "automatic_timeline_transcript_overlaps_asr_gap"),
        ("missing_id", "automatic_timeline_transcript_outside_admitted_capture"),
        ("unknown_id", "automatic_timeline_transcript_outside_admitted_capture"),
        ("wrong_capture", "automatic_timeline_transcript_outside_admitted_capture"),
        ("outside_capture", "automatic_timeline_transcript_outside_admitted_capture"),
        ("no_rows", "automatic_timeline_no_supported_activity"),
    ],
)
def test_automatic_timeline_asr_gaps_block_unsafe_rows_before_any_send(
    tmp_path, monkeypatch, defect, error
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    preflight, (gap, _) = asr_gap_setup(host)
    a, b = preflight["captures"]
    delta = timedelta(seconds=1)
    if defect != "no_rows":
        asr_row(host, gap.start - 3 * delta, gap.start - 2 * delta, a["id"])
        start, end, id = {
            "inside": (gap.start, gap.end, a["id"]),
            "crosses": (gap.start - delta, gap.end + delta, a["id"]),
            "missing_id": (gap.end, gap.end + delta, None),
            "unknown_id": (gap.end, gap.end + delta, new_id()),
            "wrong_capture": (gap.end, gap.end + delta, b["id"]),
            "outside_capture": (b["start"] - delta, b["start"] + delta, a["id"]),
        }[defect]
        asr_row(host, start, end, id)
    [run] = host.run(host.feedback())
    assert run.payload["status"] == "blocked"
    assert run.payload["error"] == error
    assert host.gateway_calls == host.reconciles == []
    assert host.client.count(Query("activity")) == 0


def test_automatic_timeline_asr_gaps_validate_exact_private_empty_scope(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    preflight, (gap, tail) = asr_gap_setup(host)
    config = host.config.model_dump()
    aid = new_id()
    pin = {
        "proof": "all_calls_saved_success_no_business_write",
        "run_id": new_id(),
        "metadata_sha256": "a" * 64,
        "scope": preflight["scope"],
        "calls": [
            {
                "call_id": new_id(),
                "record_saved": True,
                "status": "success",
                "error_code": None,
                "response_recoverable": False,
            }
        ],
    }
    target = gap.model_dump() | {
        "id": aid,
        "version": 1,
        "data_type": "activity",
        "protection": "automatic",
        "action_safety": "clear",
    }
    for changes, error in [
        ({_AUTO_TIMELINE: None}, "asr_gaps_require_scope"),
        ({_AUTO_PREWRITE: [pin]}, "asr_gaps_require_unpinned_empty_scope"),
        ({_GAPPED: [gap]}, "asr_gaps_require_unpinned_empty_scope"),
        (
            {_AUTO_TIMELINE: preflight | {"targets": [target]}},
            "asr_gaps_require_unpinned_empty_scope",
        ),
        ({_AUTO_TIMELINE: preflight | {"holds": [gap]}}, "preflight_not_clear"),
        (
            {_AUTO_TIMELINE: preflight | {"captures": [preflight["captures"][0]]}},
            "preflight_not_clear",
        ),
        (
            {"schedule": {"start": format_utc(host.envelope.start), "every_seconds": 600}},
            "requires_fixed_rule_and_windows",
        ),
        ({_ASR_GAPS: [tail, gap]}, "asr_gaps_invalid"),
        ({_ASR_GAPS: [gap, gap]}, "asr_gaps_invalid"),
        ({_ASR_GAPS: [{"start": gap.start, "end": gap.start}]}, "empty_or_reversed_interval"),
        ({_ASR_GAPS: [{"start": gap.start.isoformat(), "end": gap.end}]}, "expected_utc_timestamp"),
        (
            {_ASR_GAPS: [{"start": "2026-01-01T00:00:00.0000001Z", "end": gap.end}]},
            "expected_utc_timestamp",
        ),
        (
            {_ASR_GAPS: [{"start": gap.start, "end": tail.end + timedelta(microseconds=1)}]},
            "asr_gaps_invalid",
        ),
        ({_ASR_GAPS: [preflight["scope"]]}, "asr_gaps_leave_no_coverage"),
    ]:
        with pytest.raises(ValueError, match=error):
            worker.Settings.model_validate(config | changes)


@pytest.mark.parametrize("change", ["remove", "shift"])
def test_automatic_timeline_asr_gaps_bound_to_private_scope_sha(tmp_path, monkeypatch, change):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    _, (gap, tail) = asr_gap_setup(host)
    event = host.feedback()
    host.run(extra=lambda service: service.handle_event(event))
    assert host.run_record(event).payload["status"] == "queued"
    changed = (
        []
        if change == "remove"
        else [{"start": gap.start, "end": gap.end + timedelta(microseconds=1)}, tail]
    )
    host.config = worker.Settings.model_validate(host.config.model_dump() | {_ASR_GAPS: changed})
    [run] = host.run(event)
    assert run.payload["error"] == "automatic_timeline_scope_changed"
    assert host.transcript_reads == host.gateway_calls == []


@pytest.mark.parametrize("predecessor", ["unknown", "returned", "superseded_target"])
def test_automatic_timeline_asr_gaps_do_not_waive_old_fences(tmp_path, monkeypatch, predecessor):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    _, (gap, _) = asr_gap_setup(host)
    if predecessor == "superseded_target":
        host.prior_activity(gap.start, superseded=True)
        old = None
        before = None
    else:
        old, _, _ = prior_course_run(
            host,
            Command(kind="extract", interval=gap),
            error="gateway_outcome_unknown" if predecessor == "unknown" else "ValidationError",
            sending=predecessor == "unknown",
        )
        before = host.run_record(old)
    [run] = host.run(host.feedback())
    assert run.payload["error"] == (
        "overlapping_unresolved_model_call"
        if old
        else "automatic_timeline_asr_scope_has_business_objects"
    )
    if old:
        assert host.run_record(old) == before
    assert host.transcript_reads == host.gateway_calls == host.reconciles == []


def test_automatic_timeline_asr_gaps_fence_late_transcript_before_next_send(tmp_path, monkeypatch):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    preflight, (gap, _) = asr_gap_setup(host)
    capture_id = preflight["captures"][0]["id"]
    asr_row(host, gap.start - timedelta(seconds=1), gap.start, capture_id)
    asr_row(host, gap.end, gap.end + timedelta(seconds=1), capture_id)

    def late_row(call):
        asr_row(host, gap.start, gap.end, capture_id, "late synthetic gap evidence")
        return timeline_window_reply(call, [(1, 1, "participation", False)])

    host.replies = [late_row]
    [run] = host.run(host.feedback())
    assert run.payload["error"] == "course_reextract_transcript_changed"
    assert [call["purpose"] for call in host.gateway_calls] == [_AUTO_PURPOSE]
    assert host.client.count(Query("activity")) == 0


def gapped_timeline_setup(host, *, malformed_evidence=False, activity_only_citation=False):
    start = host.envelope.start
    clip, gap = timedelta(seconds=80), timedelta(minutes=12)
    components = (
        Interval(start=start, end=start + 13 * clip),
        Interval(
            start=start + 13 * clip + gap,
            end=start + 25 * clip + gap,
        ),
    )
    host.windows = pack_session_windows(components)
    host.envelope = Interval(start=components[0].start, end=components[1].end)
    declared = Interval(start=components[0].end, end=components[1].start)
    captures, rows = [], []
    for component, count in zip(components, (13, 12), strict=True):
        for i in range(count):
            a = component.start + i * clip
            capture_id = new_id()
            captures.append({"start": a, "end": a + clip, "id": capture_id})
            row = make_raw(
                "transcript_segment",
                {"text": f"synthetic line {len(rows) + 1}", "audio_ref_id": capture_id},
                start=a + timedelta(seconds=17),
                end=a + timedelta(seconds=23),
            )
            rows.append(row)
    host.memory.client("adapter").write([AppendRaw(row) for row in rows], Context.system()).check()
    a, b = rows[0].event_start, rows[-1].event_end
    models = {
        "activity": {
            "title_md": "synthetic old",
            "evidence_ids": [rows[0].id, rows[-1].id]
            + ([rows[1].id] if activity_only_citation else []),
        },
        "artifact": {
            "body_md": "synthetic old artifact",
            "evidence_ids": (
                [r.id for i, r in enumerate(rows) if not activity_only_citation or i != 1]
                if not malformed_evidence
                else [new_id()]
            ),
        },
        "rule_selection": {},
    }
    targets, preview, projections, ops = [], {}, [], []
    aid = new_id()
    for kind, payload in models.items():
        id = aid if kind == "activity" else new_id()
        parent = None if kind == "activity" else aid
        targets.append(id)
        if kind != "rule_selection":
            preview[id] = 1
        projections.append(
            {
                "data_type": kind,
                "id": id,
                "version": 1,
                "activity_id": parent,
                "start": a,
                "end": b,
                "protection": "automatic",
                "action_safety": "clear" if kind == "activity" else None,
            }
        )
        if parent is not None:
            payload["activity_id"] = parent
        else:
            payload.update(start=format_utc(a), end=format_utc(b))
        ops.extend(
            [
                Create(kind, id, payload, event_start=a, event_end=b),
                RegisterDependencies(kind, id, []),
            ]
        )
    host.client.write(ops, Context.system()).check()
    host.config = worker.Settings.model_validate(
        host.config.model_dump()
        | {
            "transcript_windows": host.windows,
            _GAPPED: [declared.model_dump(mode="json")],
            _AUTO_TIMELINE: {
                "scope": host.envelope.model_dump(),
                "captures": captures,
                "holds": [],
                "targets": sorted(projections, key=lambda t: t["id"]),
            },
        }
    )
    command = Command(kind="reextract", interval=host.envelope, preview=preview)
    return command, targets, rows, components, declared


def wider_prewrite_predecessor(
    host, *, kind: Literal["extract", "reextract"] = "extract", sending=False, purposes=None
):
    # The new scope cuts a strict subset from the old job, so even the saved
    # call outside the future scope must be represented by the proof.
    wider = Interval(
        start=host.envelope.start - timedelta(hours=1),
        end=host.envelope.end + timedelta(hours=1),
    )
    event, _, _ = prior_course_run(
        host,
        Command(kind=kind, interval=wider, preview={} if kind == "reextract" else None),
        purposes=purposes or ("course_blocks", "course_blocks", "generation"),
        error="ValidationError",
        sending=sending,
    )
    return host.run_record(event)


@pytest.mark.parametrize("predecessor", ["none", "wider_failure", "all_unclassified"])
def test_automatic_timeline_gapped_25_captures_atomic_fragment_replacement(
    tmp_path, monkeypatch, predecessor
):
    host = CourseHost(tmp_path, monkeypatch, count=2, transcript_window_review_concurrency=3)
    failures = []
    if predecessor == "wider_failure":
        failures = [wider_prewrite_predecessor(host), wider_prewrite_predecessor(host)]
    elif predecessor == "all_unclassified":
        old_run = wider_prewrite_predecessor(host, purposes=("course_blocks", "course_boundary"))
        failures = [
            revise_prewrite_run(
                host, old_run, status="blocked", error="session_all_rows_unclassified"
            )
        ]
    command, targets, rows, components, gap = gapped_timeline_setup(host)
    if failures:
        [blocked] = host.run(host.feedback(command))
        assert blocked.payload["error"] == "overlapping_unresolved_model_call"
        assert not host.transcript_reads and not host.gateway_calls
        set_prewrite_pins(host, *(prewrite_pin(host, old_run) for old_run in failures))
        host.config = worker.load_config(
            str(
                write_config(
                    tmp_path, host.config.model_dump(mode="json") | {"storage_token": SECRET}
                )
            )
        )
    old = {id: host.semantic(id) for id in targets}
    # The omitted interval is not read, included in a view, or used to mark a cut.
    gap_row = host.transcript(gap.start + timedelta(seconds=1), "GAP_CANARY")
    denoted = [rows[:13], rows[13:]]

    reviews = 5

    def review(call):
        view = call["data"]["window"]
        start, end = (datetime.fromisoformat(view["block_coverage"][k]) for k in ("start", "end"))
        lo, hi = view["rows"][0]["i"], view["rows"][-1]["i"]
        component = components[lo >= 14]
        assert component.start <= start < end <= component.end
        assert (hi <= 13) or (lo >= 14)
        assert [r["i"] for r in view["rows"]] == list(range(lo, hi + 1))
        assert view["review_edges"] == list(range(lo, hi))  # Real clip seams, not packed seams.
        assert len(host.config.transcript_windows) == 4
        assert host.config.transcript_windows[0].end > rows[11].event_start
        assert host.config.transcript_windows[0].end < rows[11].event_end
        assert not any(
            r["id"] in json.dumps(view)
            for r in host.config.transcript_automatic_timeline.model_dump()["captures"]
        )
        return timeline_window_reply(call, [(lo, hi, "participation", True)])

    def note(part, call):
        assert "Do not treat its edges" in call["data"]["supplemental_instructions"]
        assert [r["id"] for r in call["data"]["evidence"]] == [r.id for r in part]
        assert gap_row.id not in json.dumps(call) and "GAP_CANARY" not in json.dumps(call)
        return course_product(part[0].id)

    host.replies = [
        *[review] * reviews,
        lambda c: note(denoted[0], c),
        lambda c: note(denoted[1], c),
    ]
    event = host.feedback(command)
    [run] = host.run(event)
    assert run.payload["status"] == "succeeded", run.payload["error"]
    assert run.payload["detail"]["declared_gap_intervals"] == 1
    final = host.batch_ops(-1)
    assert {op.id for op in final if isinstance(op, Supersede)} == set(targets)
    activities = [op for op in final if isinstance(op, Create) and op.data_type == "activity"]
    assert len(activities) == 2
    for activity, part in zip(activities, denoted, strict=True):
        assert (activity.event_start, activity.event_end) == (
            part[0].event_start,
            part[-1].event_end,
        )
        assert activity.payload["evidence_ids"] == [r.id for r in part]
        assert "不代表活动开始、结束或跨空档延续" in activity.payload["description_md"]
    assert not any(
        getattr(op, "data_type", None) == "action_suggestion" and isinstance(op, Create | Supersede)
        for op in final
    )
    assert all(host.semantic(id).superseded for id in targets)
    assert all(host.client.get_version(id, 1) == old[id] for id in targets)
    assert [time for method, time in host.transcript_reads if method == "query"] == [
        TimeRange(w.start, w.end) for w in host.windows
    ]
    assert host.snapshot_opens == 0 and not host.reconciles
    [redelivery] = host.run(event)
    assert redelivery.revision == run.revision and len(host.gateway_calls) == reviews + 2
    assert all(c["run_id"] == run.id for c in host.gateway_calls)
    for failure in failures:
        assert failure.event_start is not None and failure.event_end is not None
        assert failure.event_start < host.envelope.start < host.envelope.end < failure.event_end
        assert host.client.get_system_record(failure.id) == failure
        assert {c["call_id"] for c in failure.payload["gateway_calls"]}.isdisjoint(
            c["call_id"] for c in host.gateway_calls
        )
    [repeat] = host.run(host.feedback(command))
    assert repeat.payload["error"] == "automatic_timeline_overlapping_successful_run"
    assert len(host.gateway_calls) == reviews + 2


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize(
    "problem, expected, calls",
    [
        ("artifact_unclassified", "automatic_timeline_old_family_uncertain", 2),
        ("artifact_background", "automatic_timeline_old_family_uncertain", 2),
        ("activity_background", "automatic_timeline_old_family_uncertain", 2),
        ("old_evidence", "course_reextract_old_evidence_out_of_scope", 0),
        ("wrong_source", "automatic_timeline_transcript_outside_admitted_capture", 0),
        ("held_gap", "overlapping_unresolved_model_call", 0),
        ("held_envelope", "overlapping_unresolved_model_call", 0),
        ("late_held_gap", "overlapping_unresolved_model_call", 1),
        ("stale_selection", "automatic_timeline_old_pins_changed", 0),
        ("partial_preview", "reextract_preview_changed", 0),
        ("gap_occupant", "course_reextract_old_activity_out_of_scope", 0),
        ("action_commit_race", "gateway_result_not_committed", 4),
        ("selection_commit_race", "gateway_result_not_committed", 4),
    ],
)
def test_automatic_timeline_gapped_guarded_family_replacement(
    tmp_path, monkeypatch, problem, expected, calls, pinned
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    old_run = wider_prewrite_predecessor(host) if pinned else None
    command, targets, rows, _, gap = gapped_timeline_setup(
        host,
        malformed_evidence=problem == "old_evidence",
        activity_only_citation=problem == "activity_background",
    )
    if old_run is not None:
        set_prewrite_pins(host, prewrite_pin(host, old_run))
    if problem == "wrong_source":
        host.transcript(rows[0].event_start + timedelta(seconds=1), "unadmitted capture")
    elif problem in {"held_gap", "held_envelope"}:
        prior_course_run(
            host, Command(kind="extract", interval=gap if problem == "held_gap" else host.envelope)
        )
    elif problem == "stale_selection":
        host.client.write(
            [
                Update(
                    "rule_selection", targets[-1], 1, {"activity_id": targets[0], "rule_ids": []}
                ),
                RegisterDependencies("rule_selection", targets[-1], []),
            ],
            Context.system(),
        ).check()
    elif problem == "partial_preview":
        command = command.model_copy(update={"preview": {targets[0]: 1}})
    elif problem == "gap_occupant":
        host.prior_activity(gap.start + timedelta(seconds=1))

    reviews = 5

    def review(call):
        if problem == "late_held_gap" and len(host.gateway_calls) == 1:
            host.client.write(
                [
                    PutSystemRecord(
                        "extraction_run",
                        new_id(),
                        {
                            "status": "blocked",
                            "error": "gateway_outcome_unknown",
                            "attempts": 1,
                            "command": {"kind": "extract"},
                            "gateway_calls": [{"state": "sending"}],
                        },
                        event_start=gap.start,
                        event_end=gap.end,
                    )
                ],
                Context.system(),
            ).check()
        rows_in_view = call["data"]["window"]["rows"]
        lo, hi = rows_in_view[0]["i"], rows_in_view[-1]["i"]
        if lo >= 14:
            spans = [(lo, hi, "participation", True)]
        elif problem == "artifact_unclassified":
            spans = [(1, 1, "participation", False), (3, 13, "participation", True)]
        elif problem in {"artifact_background", "activity_background"}:
            spans = [
                (1, 1, "participation", False),
                (2, 2, "background_playback", False),
                (3, 13, "participation", True),
            ]
        else:
            spans = [(1, 13, "participation", True)]
        return timeline_window_reply(
            call,
            [
                (max(lo, a), min(hi, b), role, bridge)
                for a, b, role, bridge in spans
                if max(lo, a) <= min(hi, b)
            ],
        )

    if problem in {"action_commit_race", "selection_commit_race"}:
        before = host.request

        def inject(method, path, body):
            request = {k: v for k, v in body.items() if k != "request_id"}
            if path == "/v1/batches" and any(
                isinstance(op, Supersede) for op in decode_request(json.dumps(request))[0]
            ):
                ops = (
                    [
                        Create(
                            "action_suggestion",
                            new_id(),
                            {"artifact_id": targets[1], "state": "pending"},
                        )
                    ]
                    if problem == "action_commit_race"
                    else [
                        Update(
                            "rule_selection",
                            targets[2],
                            1,
                            {"activity_id": targets[0], "rule_ids": []},
                        ),
                        RegisterDependencies("rule_selection", targets[2], []),
                    ]
                )
                host.client.write(ops, Context.system()).check()
            return before(method, path, body)

        monkeypatch.setattr(host, "request", inject)
    host.replies = [
        *[review] * reviews,
        course_product(rows[0].id),
        course_product(rows[-1].id),
    ]
    [run] = host.run(host.feedback(command))
    assert (run.payload["status"], run.payload["error"]) == ("blocked", expected)
    if calls >= 2:
        calls += reviews - 2
    assert len(host.gateway_calls) == calls and not host.reconciles
    assert all(not host.semantic(id).superseded for id in targets)
    assert host.client.count(Query("activity")) == 1 + (problem == "gap_occupant")
    assert host.client.count(Query("artifact")) == host.client.count(Query("rule_selection")) == 1
    if old_run is not None:
        assert host.client.get_system_record(old_run.id) == old_run
    if problem == "selection_commit_race":
        assert host.semantic(targets[2]).version == 2


def test_automatic_timeline_gapped_requires_exact_declared_complement_and_no_hold(
    tmp_path, monkeypatch
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    _, _, _, components, gap = gapped_timeline_setup(host)
    config = host.config.model_dump()
    preflight = config[_AUTO_TIMELINE]
    for changes in (
        {_GAPPED: []},
        {_GAPPED: [{"start": gap.start + timedelta(microseconds=1), "end": gap.end}]},
        {_GAPPED: [gap.model_dump(), gap.model_dump()]},
        {_AUTO_TIMELINE: preflight | {"holds": [gap.model_dump()]}},
        {_AUTO_TIMELINE: preflight | {"holds": [host.windows[0].model_dump()]}},
        {_AUTO_TIMELINE: preflight | {"targets": []}},
        {_AUTO_TIMELINE: None},
        {"transcript_windows": host.windows[:1] + host.windows[2:]},
        {"transcript_windows": (host.envelope,)},
    ):
        with pytest.raises(ValueError):
            worker.Settings.model_validate(config | changes)
    assert not host.transcript_reads and not host.gateway_calls
    assert len(pack_session_windows((components[0],))) == 2
    with pytest.raises(ValueError, match="window_count_out_of_range"):
        pack_session_windows((Interval(start=gap.start, end=gap.start + timedelta(minutes=361)),))
    assert ExactInterval.model_validate(config[_GAPPED][0]).end == gap.end


@pytest.mark.parametrize(
    "problem, expected",
    [
        ("sending", "overlapping_unresolved_model_call"),
        ("other_unpinned", "overlapping_unresolved_model_call"),
        ("unknown_write", "overlapping_unresolved_model_call"),
        ("unclassified_generation", "overlapping_unresolved_model_call"),
        ("failed_reextract", "overlapping_unresolved_model_call"),
        ("automatic_old_extract", "overlapping_unresolved_model_call"),
        ("prior_successful_reextract", "automatic_timeline_overlapping_successful_run"),
        ("prior_legacy_successful_reextract", "automatic_timeline_overlapping_successful_run"),
        ("incomplete_wider_audit", "automatic_timeline_prewrite_audit_mismatch"),
    ],
)
def test_automatic_timeline_gapped_prewrite_only_clears_proven_old_extract(
    tmp_path, monkeypatch, problem, expected
):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    old_run = wider_prewrite_predecessor(
        host,
        kind="reextract" if problem == "failed_reextract" else "extract",
        sending=problem == "sending",
    )
    if problem == "other_unpinned":
        wider_prewrite_predecessor(host)
    elif problem == "unknown_write":
        revise_prewrite_run(host, wider_prewrite_predecessor(host), error="CommitUnknown")
    elif problem == "unclassified_generation":
        old_run = revise_prewrite_run(
            host, old_run, status="blocked", error="session_all_rows_unclassified"
        )
    elif problem == "automatic_old_extract":
        old_run = revise_prewrite_run(host, old_run, automatic_timeline_scope_sha256="a" * 64)
    elif problem in {"prior_successful_reextract", "prior_legacy_successful_reextract"}:
        revise_prewrite_run(
            host,
            wider_prewrite_predecessor(host, kind="reextract"),
            status="succeeded",
            error=None,
            detail={"course_replacement": True} if problem == "prior_successful_reextract" else {},
        )
    command, targets, _, _, _ = gapped_timeline_setup(host)
    pin = prewrite_pin(host, old_run)
    if problem == "incomplete_wider_audit":
        pin["calls"].pop(0)
    set_prewrite_pins(host, pin)
    [run] = host.run(host.feedback(command))
    assert (run.payload["status"], run.payload["error"]) == ("blocked", expected)
    assert not host.gateway_calls and not host.reconciles and not host.transcript_reads
    assert all(
        host.semantic(id).version == 1 and not host.semantic(id).superseded for id in targets
    )
    assert host.client.get_system_record(old_run.id) == old_run


@pytest.mark.parametrize("omit_citation", [False, True])
def test_automatic_timeline_prewrite_exact_contiguous_family(tmp_path, monkeypatch, omit_citation):
    host = CourseHost(tmp_path, monkeypatch, count=2)
    old_run = wider_prewrite_predecessor(host)
    command, targets, (a, b) = replacement_setup(host, bridge=True)
    configure_automatic_timeline(host, targets)
    set_prewrite_pins(host, prewrite_pin(host, old_run))
    host.replies = [
        lambda c: timeline_window_reply(
            c,
            [(1, 1, "participation", False), (2, 2, "background_playback", False)]
            if omit_citation
            else [(1, 2, "participation", True)],
        ),
        course_product(a.id, b.id),
    ]
    [run] = host.run(host.feedback(command))
    assert host.client.get_system_record(old_run.id) == old_run
    if omit_citation:
        assert run.payload["error"] == "automatic_timeline_old_family_uncertain"
        assert len(host.gateway_calls) == 1
        assert all(not host.semantic(id).superseded for id in targets)
    else:
        assert run.payload["status"] == "succeeded"
        assert len(host.gateway_calls) == 2
        assert all(host.semantic(id).superseded for id in targets)
    assert not host.reconciles


@pytest.mark.parametrize("value", [0, -1, 1441, True, False, 10.0, "10", None])
def test_activity_floor_requires_bounded_json_integer(tmp_path, value):
    values = settings(tmp_path, "extraction", activity_min_duration_minutes=value)
    with pytest.raises(ValueError, match="activity_min_duration_minutes"):
        worker.load_config(str(write_config(tmp_path, values)))


@pytest.mark.parametrize("override, survives", [(None, False), (5, True)])
def test_automatic_timeline_activity_floor_inclusive_at_configured_boundary(
    tmp_path, monkeypatch, override, survives
):
    values = {} if override is None else {"activity_min_duration_minutes": override}
    host = HostedCourseHarness(tmp_path, monkeypatch, count=1, **values)
    assert host.config.activity_min_duration_minutes == (override or 10)
    configure_automatic_timeline(host)
    start = host.envelope.start + timedelta(minutes=1)
    row = host.transcript(start, "正在听这段合成录音", seconds=300)
    host.replies = [
        lambda call: timeline_window_reply(call, [(1, 1, "watching_listening", False)]),
        course_product(row.id),
    ]
    [run] = host.run(host.feedback())
    assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE] + (
        ["generation"] if survives else []
    )
    for kind in ("activity", "rule_selection", "artifact"):
        assert host.client.count(Query(kind)) == int(survives)
    assert host.client.get(row.id) is not None
    if survives:
        [activity] = host.client.query(Query("activity")).items
        assert (activity.event_start, activity.event_end) == (row.event_start, row.event_end)
    else:
        assert run.payload["error"] == "automatic_timeline_no_supported_activity"
        assert run.payload["detail"]["below_min_duration_candidates"] == 1
        assert run.payload["detail"]["unclassified_rows"] == 1
        assert run.payload["detail"]["materialized_engagement"]["watching_listening"] == 0


@pytest.mark.parametrize("proof", [False, True])
def test_automatic_timeline_activity_floor_waits_for_positive_cross_window_assembly(
    tmp_path, monkeypatch, proof
):
    host = HostedCourseHarness(tmp_path, monkeypatch, count=2)
    configure_automatic_timeline(host)
    start = host.envelope.start
    host.transcript(start + timedelta(seconds=895), "未分类的上下文")
    rows = [
        host.transcript(start + timedelta(seconds=seconds), text, seconds=length)
        for seconds, length, text in [
            (900, 1, "我来继续解这道题"),
            (1199, 1, "解到七号问题"),
            (1200, 1, "接着回答七号问题"),
            (1499, 1, "我回答完七号问题"),
            (1500, 1, "未分类的间隔"),
            (1501, 1, "我来看另一段回放"),
        ]
    ]

    def review(call):
        view = call["data"]["window"]
        visible = [r["i"] for r in view["rows"]]
        main = [i for i in visible if 2 <= i <= 5]
        specs = [(min(main), max(main), "participation", proof)]
        if 7 in visible:
            specs.append((7, 7, "watching_listening", False))
        return timeline_window_reply(call, specs)

    host.replies = [review, review, course_product(*(r.id for r in rows[:4]))]
    [run] = host.run(host.feedback())
    assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE] * 2 + (
        ["generation"] if proof else []
    ), run.payload
    if not proof:
        assert run.payload["error"] == "automatic_timeline_no_supported_activity"
        assert host.client.count(Query("activity")) == 0
        return
    assert run.payload["status"] == "succeeded"
    assert run.payload["detail"]["merged_boundaries"] == 1
    assert run.payload["detail"]["below_min_duration_candidates"] == 1
    assert run.payload["detail"]["below_min_duration_rows"] == 1
    assert run.payload["detail"]["withheld_candidate_count"] == 1
    [activity] = host.client.query(Query("activity")).items
    assert activity.event_start == rows[0].event_start
    assert activity.event_end == rows[3].event_end
    assert activity.event_start is not None and activity.event_end is not None
    assert activity.event_end - activity.event_start == timedelta(minutes=10)
    assert set(activity.payload["evidence_ids"]) == {r.id for r in rows[:4]}
    assert rows[-1].id not in repr(host.gateway_calls[-1]["data"])


def test_automatic_timeline_activity_floor_replacement_preserves_every_old_version(
    tmp_path, monkeypatch
):
    host = HostedCourseHarness(tmp_path, monkeypatch, count=2)
    command, targets, _ = replacement_setup(host)
    configure_automatic_timeline(host, targets)
    before = {id: host.semantic(id) for id in targets}
    host.replies = [lambda call: timeline_window_reply(call, [(1, 2, "participation", True)])]
    event = host.feedback(command)
    run, repeat = host.run(event, event)
    assert run == repeat and run.payload["status"] == "blocked"
    assert run.payload["error"] == "automatic_timeline_old_family_uncertain"
    assert run.payload["detail"]["below_min_duration_rows"] == 2
    assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE]
    assert {id: host.semantic(id) for id in targets} == before
    assert all(
        host.client.count(Query(kind)) == 1 for kind in ("activity", "rule_selection", "artifact")
    )


def test_automatic_timeline_activity_floor_is_bound_to_admission(tmp_path, monkeypatch):
    host = HostedCourseHarness(tmp_path, monkeypatch, count=1)
    configure_automatic_timeline(host)
    event = host.feedback()
    host.run(extra=lambda service: service.handle_event(event))
    assert host.run_record(event).payload["status"] == "queued"
    host.config = worker.Settings.model_validate(
        host.config.model_dump() | {"activity_min_duration_minutes": 1}
    )
    [run] = host.run(event)
    assert run.payload["error"] == "automatic_timeline_scope_changed"
    assert host.gateway_calls == host.transcript_reads == []


def test_course_blocks_activity_floor_fresh_only(tmp_path, monkeypatch):
    host = HostedCourseHarness(tmp_path, monkeypatch, count=2, activity_min_duration_minutes=1440)
    command, targets, (a, b) = replacement_setup(host)
    [run] = host.run(host.feedback(command))
    assert run.payload["status"] == "succeeded"
    assert [c["purpose"] for c in host.gateway_calls] == ["course_blocks", "generation"]
    assert all(host.semantic(id).superseded for id in targets)
    [activity] = host.client.query(Query("activity")).items
    assert (activity.event_start, activity.event_end) == (a.event_start, b.event_end)


def test_course_blocks_activity_floor_withholds_fresh_short(tmp_path, monkeypatch):
    host = HostedCourseHarness(tmp_path, monkeypatch, count=1)
    host.transcript(host.envelope.start, "合成短课堂")
    host.replies = [{"spans": [course_span("合成短课堂")]}]
    [run] = host.run(host.feedback())
    assert run.payload["error"] == "no_supported_activity"
    assert run.payload["detail"]["below_min_duration_candidates"] == 1
    assert [c["purpose"] for c in host.gateway_calls] == ["course_blocks"]
    assert host.client.count(Query("activity")) == 0


def test_automatic_timeline_activity_floor_preserves_old_artifact_only_citation(
    tmp_path, monkeypatch
):
    host = HostedCourseHarness(tmp_path, monkeypatch, count=2)
    command, targets, (a, b) = replacement_setup(host, artifact_only_citation=True)
    earlier = host.transcript(a.event_start - timedelta(seconds=599), "同一任务先从七号问题开始")
    seam = host.envelope.start + timedelta(minutes=10)
    host.transcript(seam - timedelta(seconds=1), "正在继续七号问题")
    host.transcript(seam, "继续回答刚才的七号问题")
    configure_automatic_timeline(host, targets)

    # One candidate is exactly ten minutes and shares the old activity's citation.
    # The explicitly distinct short candidate is the artifact's other citation.
    # Omission of the earlier row from a short window view is not a contradiction.
    def review(call):
        view = call["data"]["window"]
        visible = {row["i"]: row["text"] for row in view["rows"]}
        main = [i for i in visible if i < 5]
        spans = [(min(main), max(main), "participation", True)] if main else []
        if 5 in visible:
            spans.append((5, 5, "watching_listening", False))
        output = timeline_window_reply(call, spans)
        if 4 in visible and 5 in visible:
            output["spans"][-1]["transition_before"] = {
                "left": {"i": 4, "quote": visible[4]},
                "right": {"i": 5, "quote": visible[5]},
            }
        return output

    host.replies = [review, review, course_product(earlier.id, a.id)]
    [run] = host.run(host.feedback(command))
    assert run.payload["error"] == "automatic_timeline_old_family_uncertain", run.payload
    assert run.payload["detail"]["materialized_engagement"]["participation"] == 1
    assert run.payload["detail"]["below_min_duration_candidates"] == 1
    assert [c["purpose"] for c in host.gateway_calls] == [_AUTO_PURPOSE] * 2
    assert all(host.semantic(id).version == 1 for id in targets)
    assert b.id in host.semantic(targets[1]).payload["evidence_ids"]


def test_chat_evidence_default_off_and_only_supported_in_general_extraction(tmp_path):
    general = settings(tmp_path, "extraction")
    assert worker.Settings.model_validate(general).chat_evidence is False
    assert worker.Settings.model_validate(general | {"chat_evidence": True}).chat_evidence is True
    with pytest.raises(ValueError, match="chat_evidence_requires_general_extraction"):
        worker.Settings.model_validate(
            general | transcript_only_settings() | {"chat_evidence": True}
        )
    with pytest.raises(ValueError, match="chat_evidence_requires_general_extraction"):
        worker.Settings.model_validate(settings(tmp_path, "attribution", chat_evidence=True))
