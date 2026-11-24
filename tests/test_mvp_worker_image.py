"""Opt-in built-image acceptance, never a build/pull or a production deployment.

EAR_RUN_API_GATEWAY_DEPLOY_TESTS=1 EAR_MVP_WORKER_TEST_IMAGE=<explicit-local-tag>
pytest tests/test_mvp_worker_image.py
Missing worker image skips, including when the existing deployment gate is on.
The small real-storage smoke additionally uses optional EAR_STORAGE_TEST_IMAGE,
local postgres:17-alpine/nats:2.11-alpine and read-only current storage/SDK source.
No model/provider, API, relay daemon, ear-dev, host installs or full business E2E.
EAR_MVP_WORKER_EVIDENCE_DIR optionally retains command/ID/log evidence in /tmp.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "attribution": "attribution",
    "extraction": "extraction.jobs",
    "dependency": "dependency.changes",
}
TOKEN = "z" * 43
ERROR = {
    "module": "mvp_worker",
    "event_id": None,
    "root_feedback_id": "system",
    "depth": 0,
    "code": "mvp_worker_unavailable",
}
HARDENED = [
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges:true",
    "--memory",
    "512m",
    "--cpus",
    "1",
    "--pids-limit",
    "128",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,nodev,size=16m",
]
PRIVATE_STATE = "/state:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=0700"


def synthetic_config(module="attribution"):
    value = {
        "module": module,
        "consumer": MODULES[module],
        "storage_url": "http://storage:8000",
        "storage_token": TOKEN,
        "journal_namespace": "image-synthetic-" + module,
        "state_dir": "/state",
        "poll_seconds": 0.1,
        "timeout": 3.0,
    }
    if module == "attribution":
        value.update(cosine_threshold=0.9, gps_threshold_m=100.0)
    if module == "extraction":
        value.update(gateway_url="http://127.0.0.1:1", model="unused-synthetic")
    return value


def required_worker_image():
    if os.environ.get("EAR_RUN_API_GATEWAY_DEPLOY_TESTS") != "1":
        pytest.skip("worker Docker acceptance requires EAR_RUN_API_GATEWAY_DEPLOY_TESTS=1")
    image = os.environ.get("EAR_MVP_WORKER_TEST_IMAGE")
    if not image:
        pytest.skip("no explicit EAR_MVP_WORKER_TEST_IMAGE; existing CI remains unchanged")
    if not shutil.which("docker"):
        pytest.skip("Docker CLI unavailable; no host installation attempted")
    result = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, timeout=15
    )
    if result.returncode:
        pytest.skip("EAR_MVP_WORKER_TEST_IMAGE unavailable locally; never pull/build in tests")
    return image


class DockerTask:
    def __init__(self, image, directory):
        self.image = image
        self.directory = directory
        self.label = "ear-mvp-worker-image-" + uuid.uuid4().hex
        self.resources = []
        self.commands = []
        self.ids = {}
        self.logs = {}
        self.save_evidence()

    def save_evidence(self, failures=None):
        self.directory.mkdir(parents=True, exist_ok=True)
        report = self.directory / (self.label + ".json")
        report.write_text(
            json.dumps(
                {
                    "label": self.label,
                    "image": self.image,
                    "ids": self.ids,
                    "resources": self.resources,
                    "commands": self.commands,
                    "container_logs": self.logs,
                    "cleanup_failures": failures,
                },
                indent=2,
            )
        )
        report.chmod(0o600)

    def docker(self, *args, payload=None, timeout=30, check=True):
        entry = {"argv": ["docker", *args], "stdin_supplied": payload is not None}
        self.commands.append(entry)
        self.save_evidence()
        try:
            result = subprocess.run(
                entry["argv"], input=payload, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            entry["timeout"] = timeout
            raise
        entry.update(exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
        self.save_evidence()
        if check:
            assert result.returncode == 0, f"isolated Docker {args[0]} failed: {result.stderr}"
        return result

    def image_available(self, image):
        if not image or self.docker("image", "inspect", image, check=False).returncode:
            pytest.skip("optional smoke dependency image unavailable locally: " + str(image))
        return image

    def volume(self):
        name = self.label + "-v" + str(len(self.resources))
        self.resources.append(("volume", name))
        self.docker("volume", "create", "--label", "ear.task=" + self.label, name)
        return name

    def network(self):
        name = self.label + "-network"
        self.resources.append(("network", name))
        self.docker("network", "create", "--internal", "--label", "ear.task=" + self.label, name)
        return name

    def run(
        self,
        *command,
        image=None,
        options=(),
        entrypoint=None,
        network="none",
        state=None,
        detached=False,
        payload=None,
        check=True,
        timeout=30,
    ):
        name = self.label + "-c" + str(len(self.resources))
        self.resources.append(("container", name))
        args = [
            "run",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            "ear.task=" + self.label,
            "--network",
            network,
            *HARDENED,
        ]
        if detached:
            args += ["-d"]
        if payload is not None:
            args += ["-i"]
        if entrypoint:
            args += ["--entrypoint", entrypoint]
        args += ["-v", state + ":/state"] if state else ["--tmpfs", PRIVATE_STATE]
        result = self.docker(
            *args,
            *options,
            image or self.image,
            *command,
            payload=payload,
            check=check,
            timeout=timeout,
        )
        info = self.inspect(name)
        self.ids[name] = info["Id"]
        return name, result

    def inspect(self, name):
        return json.loads(self.docker("inspect", name).stdout)[0]

    def python(self, program, **kwargs):
        return self.run("-c", program, entrypoint="python", **kwargs)

    def secrets(self, files):
        volume = self.volume()
        self.python(
            "import json,os,pathlib,stat,sys; p=pathlib.Path('/state'); s=p.stat(); "
            "assert s.st_uid==10001 and stat.S_IMODE(s.st_mode)==0o700; "
            "[( (p/k).write_text(v), (p/k).chmod(0o600)) "
            "for k,v in json.load(sys.stdin).items()]",
            state=volume,
            payload=json.dumps(files),
        )
        return volume

    def cleanup(self):
        failures = []
        for kind, name in reversed(self.resources):
            result = self.docker(kind, "inspect", name, check=False)
            if result.returncode:
                continue
            info = json.loads(result.stdout)[0]
            labels = info["Config"]["Labels"] if kind == "container" else info["Labels"]
            if labels.get("ear.task") != self.label:
                failures.append("refused unrelated resource: " + name)
                continue
            if kind == "container":
                self.ids[name] = info["Id"]
                logs = self.docker("logs", name, check=False)
                self.logs[name] = logs.stdout + logs.stderr
                args = ("rm", "-f", "-v", name)
            else:
                args = (kind, "rm", name)
            if self.docker(*args, check=False).returncode:
                failures.append("cleanup failed: " + name)
        self.save_evidence(failures)
        assert not failures, failures


@pytest.fixture
def task(tmp_path):
    image = required_worker_image()
    directory = Path(os.environ.get("EAR_MVP_WORKER_EVIDENCE_DIR", str(tmp_path)))
    owner = DockerTask(image, directory)
    try:
        yield owner
    finally:
        owner.cleanup()


def test_missing_worker_image_is_explicit_skip_without_docker(monkeypatch):
    monkeypatch.setenv("EAR_RUN_API_GATEWAY_DEPLOY_TESTS", "1")
    monkeypatch.delenv("EAR_MVP_WORKER_TEST_IMAGE", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("missing image must not invoke Docker")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(pytest.skip.Exception, match="EAR_MVP_WORKER_TEST_IMAGE"):
        required_worker_image()


def test_overlay_opt_in_policy_without_runtime_claim():
    overlay = yaml.safe_load((ROOT / "deploy/compose.mvp-workers.yaml").read_text())
    assert set(overlay["services"]) == {"mvp-" + m for m in MODULES}
    for module in MODULES:
        service = overlay["services"]["mvp-" + module]
        assert service["profiles"] == ["mvp-workers"]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["mem_limit"] == "512m" and service["cpus"] == 1.0
        assert service["pids_limit"] == 128 and service["deploy"]["replicas"] == 1
        assert service["restart"] == "no" and service["stop_grace_period"] == "5m"
        assert service["depends_on"] == {"storage": {"condition": "service_healthy"}}
        assert service["networks"] == ["frontend"]
        assert service["volumes"] == ["mvp_" + module + "_state:/state"]
        assert service["secrets"] == [
            {
                "source": "mvp_" + module + "_config",
                "target": "worker-config.json",
            }
        ]
        assert not {"environment", "ports", "command", "entrypoint"} & service.keys()


def test_compose_real_parser_with_only_synthetic_inputs(task, tmp_path):
    # Reuse an EXISTING CLI image, no socket, network, real .env or host secret mount.
    cli_image = task.image_available("docker:29-cli")
    placeholder = tmp_path / "synthetic-config.json"
    placeholder.write_text("{}")
    placeholder.chmod(0o600)
    options = ["-v", str(placeholder) + ":/synthetic.json:ro"]
    for name in ("compose.yaml", "compose.mvp-workers.yaml"):
        options += ["-v", str(ROOT / "deploy" / name) + ":/repo/deploy/" + name + ":ro"]
    for key in ("EAR_POSTGRES_PASSWORD", "EAR_SERVICE_DB_PASSWORD", "EAR_NATS_TOKEN"):
        options += ["-e", key + "=synthetic-only"]
    for module in MODULES:
        options += ["-e", "EAR_MVP_" + module.upper() + "_CONFIG_FILE=/synthetic.json"]
    options += ["-e", "EAR_MVP_WORKER_IMAGE=" + task.image]

    def compile_config(overlay):
        files = ["-f", "/repo/deploy/compose.yaml"]
        if overlay:
            files += ["-f", "/repo/deploy/compose.mvp-workers.yaml"]
        _, result = task.run(
            "compose",
            "-p",
            task.label,
            "--env-file",
            "/dev/null",
            "--profile",
            "*",
            *files,
            "config",
            "--format",
            "json",
            image=cli_image,
            entrypoint="docker",
            options=options,
        )
        return json.loads(result.stdout)

    base, merged = compile_config(False), compile_config(True)
    for name, value in base["services"].items():
        assert merged["services"][name] == value
    for module in MODULES:
        service = merged["services"]["mvp-" + module]
        assert service["image"] == task.image
        assert service["read_only"] is True
        assert service["secrets"][0]["target"] == "worker-config.json"
    assert all(
        merged["secrets"]["mvp_" + m + "_config"]["file"] == "/synthetic.json" for m in MODULES
    )


def test_image_imports_help_and_effective_runtime_hardening(task):
    program = r"""
import errno, json, os, pathlib, stat
import ear_attribution, ear_extraction, ear_dependency
from ear_attribution.service import AttributionService
from ear_extraction.orchestration import ExtractionService
from ear_dependency.remote import RemoteDependencyService
assert os.geteuid() == os.getegid() == 10001
p = pathlib.Path('/app/mvp_worker.py')
assert p.stat().st_uid == 0 and stat.S_IMODE(p.stat().st_mode) == 0o444
p.read_bytes()
for path in ('/app/forbidden', '/etc/forbidden'):
    try:
        pathlib.Path(path).write_text('forbidden')
    except OSError as exc:
        assert exc.errno in {errno.EROFS, errno.EACCES}
    else:
        raise AssertionError('root filesystem writable')
lines = pathlib.Path('/proc/self/status').read_text().splitlines()
status = dict(line.split(':', 1) for line in lines if ':' in line)
assert int(status['CapEff'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip() == str(512 * 1024**2)
assert pathlib.Path('/sys/fs/cgroup/pids.max').read_text().strip() == '128'
quota, period = pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().split()
assert int(quota) == int(period)
assert {p.name for p in pathlib.Path('/sys/class/net').iterdir()} == {'lo'}
print(json.dumps({'uid': os.geteuid(), 'modules': [m.__file__ for m in
    (ear_attribution, ear_extraction, ear_dependency)], 'caps': 0, 'no_new_privs': True}))
"""
    name, _ = task.python(program)
    info = task.inspect(name)
    host = info["HostConfig"]
    assert info["Config"]["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
    assert host["Memory"] == 512 * 1024**2 and host["NanoCpus"] == 1_000_000_000
    assert host["PidsLimit"] == 128 and not host["PortBindings"]
    assert info["Config"]["Healthcheck"]["Test"] == [
        "CMD",
        "python",
        "/app/mvp_worker.py",
        "--config",
        "/run/secrets/worker-config.json",
        "--healthcheck",
    ]
    _, result = task.run("--help")
    assert "--config" in result.stdout and "--healthcheck" in result.stdout
    assert not result.stderr


@pytest.mark.parametrize("kind", ["absent", "unknown", "missing-token", "public", "symlink"])
def test_container_config_failclosed_before_any_network(task, kind):
    config = synthetic_config()
    if kind == "unknown":
        config["unknown_private_setting"] = "SYNTHETIC_PRIVATE_ERROR"
    if kind == "missing-token":
        config.pop("storage_token")
    volume = task.secrets({"worker-config.json": json.dumps(config)})
    if kind in {"public", "symlink"}:
        script = "from pathlib import Path; p=Path('/state/worker-config.json'); " + (
            "p.chmod(0o644)"
            if kind == "public"
            else "p.rename('/state/target.json'); p.symlink_to('target.json')"
        )
        task.python(script, state=volume)
    options = [] if kind == "absent" else ["-v", volume + ":/run/secrets:ro"]
    _, result = task.run(options=options, check=False)
    assert result.returncode == 1
    assert result.stdout == "" and json.loads(result.stderr) == ERROR
    # Also run the unmodified entrypoint under an audit hook: network=none alone
    # would not prove absence of attempted DNS/connect/socket operations.
    guarded = r"""
import runpy, sys
network = []
def audit(event, args):
    if event.startswith('socket.'):
        network.append(event)
        raise RuntimeError('network forbidden in invalid/unconfigured startup')
sys.addaudithook(audit)
sys.argv = ['/app/mvp_worker.py', '--config', '/run/secrets/worker-config.json']
try:
    runpy.run_path('/app/mvp_worker.py', run_name='__main__')
except SystemExit as exc:
    assert exc.code == 1
else:
    raise AssertionError('startup unexpectedly succeeded')
assert not network, network
"""
    task.python(guarded, options=options)
    assert TOKEN not in result.stderr and "SYNTHETIC_PRIVATE_ERROR" not in result.stderr


def test_local_health_requires_live_lock_and_never_network(task):
    volume = task.secrets({"worker-config.json": json.dumps(synthetic_config())})
    program = r"""
import pathlib, sys
sys.path.insert(0, '/app')
import mvp_worker as worker
network = []
def audit(event, args):
    if event.startswith('socket.'):
        network.append(event)
        raise RuntimeError('local health must never use network')
sys.addaudithook(audit)
config = worker.load_config('/run/secrets/worker-config.json')
state = worker.State(config)
try:
    for status, expected in [('starting', False), ('ready', True), ('busy', True),
                             ('failed', False), ('stopped', False)]:
        state.health(status)
        assert worker.healthy(config) is expected
    state.health('ready')
    assert worker.main(['--config', '/run/secrets/worker-config.json', '--healthcheck']) == 0
    saved = __import__('json').loads(pathlib.Path('/state/health.json').read_text())
    saved['updated_at'] -= 3601
    state.write('health.json', saved)
    assert not worker.healthy(config)
    state.health('ready')
finally:
    state.close()
assert not worker.healthy(config), 'stale ready metadata without live lock must fail'
assert not network
print('local-only health: ready/busy/stale/stopped/unlocked verified')
"""
    task.python(program, options=["-v", volume + ":/run/secrets:ro"])


REAL_STORAGE_BOOT = r"""
import json, os, time
from pathlib import Path
import psycopg
import uvicorn
from ear_storage import migrate
from ear_storage.core_deployment import bootstrap_core, init_core_identities
from ear_storage.http import create_app
from ear_storage.relay import RelayConfig
from ear_storage.service import StorageService
DSN = 'postgresql://postgres:synthetic-only@postgres:5432/ear'
for attempt in range(100):
    try:
        with psycopg.connect(DSN, connect_timeout=1):
            break
    except psycopg.OperationalError:
        time.sleep(.1)
else:
    raise RuntimeError('temporary PostgreSQL readiness deadline')
migrate(DSN)
assert bootstrap_core(DSN)
root = Path('/run/secrets/ear')
output = root / 'core' / 'identities.json'
assert init_core_identities(DSN, output)
credentials = json.loads(output.read_text())['credentials']
for module, consumer in [('attribution', 'attribution'), ('extraction', 'extraction.jobs'),
                         ('dependency', 'dependency.changes')]:
    config = dict(module=module, consumer=consumer, storage_url='http://storage:8000',
                  storage_token=credentials[module]['token'],
                  journal_namespace='isolated-image-' + module, state_dir='/state',
                  poll_seconds=.1, timeout=5.0)
    if module == 'attribution':
        config.update(cosine_threshold=.9, gps_threshold_m=100.0)
    if module == 'extraction':
        config.update(gateway_url='http://127.0.0.1:1', model='unused-synthetic')
    fd = os.open(root / (module + '.json'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream)
relay = RelayConfig(servers=('nats://nats:4222',), token='synthetic-only',
                    stream='EAR_IMAGE_SMOKE', prefix='ear_image_smoke', timeout=2,
                    max_bytes=1024*1024, max_msgs=1000)
app = create_app(StorageService(DSN, relay_config=relay))
print('synthetic empty database/core identities initialized', flush=True)
uvicorn.run(app, host='0.0.0.0', port=8000, workers=1, access_log=False, log_level='warning')
"""

REAL_STORAGE_CHECK = r"""
import asyncio, json, time
from datetime import UTC, datetime
from pathlib import Path
from ear_contracts import NewRawRecord, new_id
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage_client import AppendRaw, Context, Query
from ear_storage_client.remote import RemoteStorageClient
credentials = json.loads(Path('/run/secrets/ear/core/identities.json').read_text())['credentials']
clients = {m: RemoteStorageClient(credentials[m]['token'], 'http://127.0.0.1:8000')
           for m in ('adapter', 'attribution', 'extraction', 'dependency')}
try:
    state = clients['dependency'].scheduler_state()
    assert state['coverage_complete'] is True
    assert all(state['owners'][m]['can_receive'] for m in ('attribution', 'extraction'))
    assert clients['extraction'].count(Query('extraction_run')) == 0
    now = datetime.now(UTC)
    raw = NewRawRecord(id=new_id(), data_type='gps_point', event_start=now, event_end=now,
                       source={'device': 'synthetic-image', 'channel': 'gps', 'user': 'default'},
                       adapter_version='image-smoke', dedup_key='image-smoke',
                       payload={'latitude': 0.0, 'longitude': 0.0})
    event = clients['adapter'].write([AppendRaw(raw)], Context.system()).check().events[0]
    cfg = RelayConfig(servers=('nats://nats:4222',), token='synthetic-only',
                      stream='EAR_IMAGE_SMOKE', prefix='ear_image_smoke', timeout=2,
                      max_bytes=1024*1024, max_msgs=1000)
    for attempt in range(80):
        asyncio.run(run_relay(RelayStore('postgresql://postgres:synthetic-only@postgres:5432/ear',
                                        cfg), once=True))
        attrs = clients['attribution'].query(Query('attribution')).items
        if attrs and clients['dependency'].is_processed('dependency.changes', event.event_id):
            assert len(attrs) == 1 and attrs[0].payload['subject_id'] == raw.id
            assert clients['extraction'].count(Query('extraction_run')) == 0
            print(json.dumps({'gps_id': raw.id, 'attribution_id': attrs[0].id,
                              'coverage_complete': True, 'scheduled_runs': 0}))
            break
        time.sleep(.15)
    else:
        raise AssertionError('real worker failed to consume one synthetic GPS point')
finally:
    for client in clients.values():
        client.close()
"""


def wait_for(task, name, probe, *, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert task.inspect(name)["State"]["Running"], "isolated container exited: " + name
        if probe():
            return
        time.sleep(0.2)
    pytest.fail("isolated container readiness deadline: " + name)


def test_valid_configs_with_temporary_real_storage_and_one_delivery(task, tmp_path):
    storage_image = task.image_available(os.environ.get("EAR_STORAGE_TEST_IMAGE"))
    for image in ("postgres:17-alpine", "nats:2.11-alpine"):
        task.image_available(image)
    network = task.network()
    # Fresh ephemeral DB only. Root performs the official PostgreSQL entrypoint's
    # initialization; worker hardening is verified separately, not claimed for PG.
    task.run(
        image="postgres:17-alpine",
        network=network,
        detached=True,
        options=[
            "--network-alias",
            "postgres",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,nosuid,nodev,size=256m",
            "--tmpfs",
            "/var/run/postgresql:rw,nosuid,nodev,size=8m",
            "-e",
            "PGDATA=/var/lib/postgresql/data/pgdata",
            "-e",
            "POSTGRES_DB=ear",
            "-e",
            "POSTGRES_PASSWORD=synthetic-only",
        ],
    )
    task.run(
        "-js",
        "-sd",
        "/tmp/js",
        "--auth",
        "synthetic-only",
        image="nats:2.11-alpine",
        network=network,
        detached=True,
        options=["--network-alias", "nats"],
    )
    secrets = task.secrets({})
    boot = tmp_path / "storage_boot.py"
    boot.write_text(REAL_STORAGE_BOOT)
    boot.chmod(0o444)
    options = [
        "--network-alias",
        "storage",
        "-v",
        str(boot) + ":/boot.py:ro",
        "-v",
        secrets + ":/run/secrets/ear",
    ]
    # The optional dependency image supplies installed third-party dependencies.
    # Overlay ONLY current read-only source, explicitly not a rebuilt storage image.
    for source, target in (
        ("services/storage/src", "storage"),
        ("libs/ear_storage_client/src", "sdk"),
        ("libs/ear_contracts/src", "contracts"),
    ):
        options += ["-v", str(ROOT / source) + ":/opt/current/" + target + ":ro"]
    options += ["-e", "PYTHONPATH=/opt/current/storage:/opt/current/sdk:/opt/current/contracts"]
    storage, _ = task.run(
        "/boot.py",
        image=storage_image,
        entrypoint="python",
        network=network,
        detached=True,
        options=options,
    )
    wait_for(
        task,
        storage,
        lambda: (
            task.docker(
                "exec",
                storage,
                "python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=1).close()",
                check=False,
            ).returncode
            == 0
        ),
    )
    workers = []
    for module in MODULES:
        secret = task.volume()
        task.python(
            "import pathlib; p=pathlib.Path('/state/worker-config.json'); "
            f"p.write_bytes(pathlib.Path('/seed/{module}.json').read_bytes()); p.chmod(0o600)",
            state=secret,
            options=["-v", secrets + ":/seed:ro"],
        )
        state = task.volume()
        name, _ = task.run(
            network=network,
            detached=True,
            state=state,
            options=[
                "-v",
                secret + ":/run/secrets:ro",
                "--health-interval",
                "1s",
                "--health-start-period",
                "1s",
                "--health-timeout",
                "5s",
                "--stop-timeout",
                "15",
            ],
        )
        workers.append((module, name, secret, state))
        wait_for(
            task,
            name,
            lambda name=name: task.inspect(name)["State"]["Health"]["Status"] == "healthy",
        )
        task.docker(
            "exec",
            name,
            "python",
            "-c",
            "import os,stat,pathlib; p=pathlib.Path('/run/secrets/worker-config.json'); "
            "s=p.stat(); assert s.st_uid==os.geteuid()==10001; "
            "assert stat.S_IMODE(s.st_mode)==0o600 and s.st_nlink==1; "
            "s=pathlib.Path('/state').stat(); "
            "assert s.st_uid==10001 and stat.S_IMODE(s.st_mode)==0o700",
        )
    result = task.docker(
        "exec", "-i", storage, "python", "-", payload=REAL_STORAGE_CHECK, timeout=60
    )
    assert json.loads(result.stdout)["scheduled_runs"] == 0
    # Preserve actual named state across stop/recreate; no replay/backfill/model job.
    module, old, secret, state = workers[0]
    task.docker("stop", "--time", "15", old)
    assert task.inspect(old)["State"]["ExitCode"] == 0
    restarted, _ = task.run(
        network=network,
        detached=True,
        state=state,
        options=[
            "-v",
            secret + ":/run/secrets:ro",
            "--health-interval",
            "1s",
            "--health-start-period",
            "1s",
        ],
    )
    wait_for(
        task, restarted, lambda: task.inspect(restarted)["State"]["Health"]["Status"] == "healthy"
    )
    workers[0] = (module, restarted, secret, state)
    for _, name, _, state in workers:
        task.docker("stop", "--time", "15", name)
        assert task.inspect(name)["State"]["ExitCode"] == 0
        logs = task.docker("logs", name)
        assert not logs.stdout and not logs.stderr
        task.python(
            "import json,pathlib; assert "
            "json.loads(pathlib.Path('/state/health.json').read_text())['status']=='stopped'",
            state=state,
        )
    # Health remains local even with real storage absent. No worker still polling.
    task.docker("stop", "--time", "10", storage)
    _, probe = task.run(
        "--config",
        "/run/secrets/worker-config.json",
        "--healthcheck",
        state=workers[0][3],
        options=["-v", workers[0][2] + ":/run/secrets:ro"],
        check=False,
    )
    assert probe.returncode == 1 and probe.stdout == probe.stderr == ""
