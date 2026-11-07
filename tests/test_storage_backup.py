"""Stdlib tool regressions execute in isolated interpreters, never import business code."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/storage_backup.py"


def isolated(code, *args):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy,sys; globals().update(runpy.run_path(sys.argv[1])); " + code,
            str(SCRIPT),
            *map(str, args),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_help_and_fail_closed_cli(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and "DANGER" in result.stdout
    for command, extra in [
        ("backup", []),
        ("restore", ["--new-project", "new", "--confirm-new-project", "wrong"]),
    ]:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                command,
                "--compose-bin",
                "/no-compose",
                "--compose-file",
                str(tmp_path / "compose"),
                "--env-file",
                str(tmp_path / "env"),
                "--bundle",
                str(tmp_path / "bundle"),
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/no-docker"},
        )
        assert result.returncode == 1
        assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("/absolute", "file"),
        ("../escape", "file"),
        ("a/../../escape", "file"),
        ("a", "symlink"),
        ("a", "hardlink"),
        ("a", "device"),
        ("a", "fifo"),
    ],
)
def test_unsafe_archive(tmp_path, name, kind):
    isolated(
        """
import io
m = tarfile.TarInfo(sys.argv[3])
m.type = {"file": tarfile.REGTYPE, "symlink": tarfile.SYMTYPE,
          "hardlink": tarfile.LNKTYPE, "device": tarfile.CHRTYPE,
          "fifo": tarfile.FIFOTYPE}[sys.argv[4]]
b = io.BytesIO()
with tarfile.open(fileobj=b, mode="w") as t:
    t.addfile(m)
b.seek(0)
try:
    validate_tar(b)
except Refused:
    pass
else:
    raise AssertionError("unsafe archive accepted")
""",
        tmp_path,
        name,
        kind,
    )


def test_private_bundle_refuses_existing_and_symlinks(tmp_path):
    isolated(
        """
p = Path(sys.argv[2]) / "bundle"
b = Bundle(p, create=True)
b.put("private", b"synthetic")
assert stat.S_IMODE(p.stat().st_mode) == 0o700
assert stat.S_IMODE((p / "private").stat().st_mode) == 0o600
assert b.digest("private") == hashlib.sha256(b"synthetic").hexdigest()
try:
    Bundle(p, create=True)
except FileExistsError:
    pass
else:
    raise AssertionError("existing backup accepted")
(p / "link").symlink_to(p / "private")
try:
    b.get("link")
except OSError:
    pass
else:
    raise AssertionError("symlink accepted")
b.close()
alias = p.parent / "alias"
alias.symlink_to(p)
try:
    absolute(str(alias / "new"))
except Refused:
    pass
else:
    raise AssertionError("ancestor symlink accepted")
""",
        tmp_path,
    )


@pytest.mark.parametrize("failure", ["dump", "tar", "fsync", "none", "resume"])
def test_offline_order_failure_partial_and_original_state(tmp_path, failure):
    isolated(
        """
import io
class Fake:
    project = "synthetic-source"
    def __init__(self):
        self.calls = []
        self.running = {"postgres", "storage", "nats"}
    def inventory(self):
        return {s: {"Id": s, "Image": "sha256:synthetic", "Config": {"Image": "nats:test"},
                    "State": {"Running": s in self.running,
                              "Status": "running" if s in self.running else "exited"}}
                for s in ("postgres", "storage", "nats", "relay", "manage-worker")}
    def docker(self, *args):
        self.calls.append(args)
        if args[0] == "stop":
            saved = json.loads((p / "start-state.json").read_text())
            assert saved["postgres"] == "running"
            assert saved["relay"] == saved["manage-worker"] == "exited"
            self.running.remove(args[-1])
        if args[0] == "start":
            assert args[-1] not in {"relay", "manage-worker"}
            if case == "resume":
                raise Refused("resume failed")
            self.running.add(args[-1])
    def sql(self, *args):
        return "0"
    def meta(self, *args):
        return 17, 14
    def run(self, command, output):
        assert self.running == {"postgres"}
        assert "pg_dump" in command and "-Fc" in command
        assert "--no-owner" in command and "--no-acl" in command
        if case == "dump":
            raise Refused("first failure")
        output.write(b"PGDMPsynthetic")
    def helper(self, key, mode, output):
        assert self.running == {"postgres"}
        assert mode == "pack"
        if case == "tar":
            raise Refused("first failure")
        with tarfile.open(fileobj=output, mode="w") as t:
            m = tarfile.TarInfo("identities.env" if key == "identities" else "data")
            m.uid, m.gid, m.mode, m.size = 10001, 10001, 0o600, 1
            t.addfile(m, io.BytesIO(b"x"))
case = sys.argv[3]
p = Path(sys.argv[2]) / "bundle"
f = Fake()
if case == "fsync":
    original_fsync = os.fsync
    def faulty(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode) and f.running == {"postgres"}:
            raise OSError("first failure")
        original_fsync(fd)
    os.fsync = faulty
try:
    backup(f, p, True)
except (Refused, OSError):
    assert case != "none"
    assert not (p / "READY").exists()
    assert (p / "PARTIAL").exists()
else:
    assert case == "none" and (p / "READY").exists()
    assert not (p / "PARTIAL").exists()
assert "postgres" in f.running
if case != "resume":
    assert f.running == {"postgres", "storage", "nats"}
else:
    assert f.running == {"postgres"}
""",
        tmp_path,
        failure,
    )


@pytest.mark.parametrize("case", ["paused", "unknown", "foreign", "extra-volume", "labels"])
def test_inventory_refuses_uncontrolled_writers(case):
    isolated(
        """
e = object.__new__(Engine)
e.project = "isolated"
e.compose_file = Path("/isolated/compose.yaml")
e.config = {"services": {"postgres": {"volumes": [
    {"type": "volume", "source": "postgres_data", "target": "/db"}]}}}
volumes = [{"Name": e.volume(k), "Driver": "local", "Options": {},
            "Labels": {LABEL + "volume": k}} for k in VOLUMES]
c = {"Id": "pg", "Config": {"Labels": {LABEL + "project": e.project,
     LABEL + "service": "postgres", LABEL + "project.config_files": str(e.compose_file)}},
     "State": {"Paused": False, "Restarting": False, "Running": True, "Status": "running"},
     "Mounts": [{"Type": "volume", "Name": e.volume("postgres_data"), "Destination": "/db"}]}
case = sys.argv[2]
if case == "paused": c["State"]["Paused"] = True
if case == "unknown": c["Config"]["Labels"][LABEL + "service"] = "unknown"
if case == "foreign": c["Config"]["Labels"][LABEL + "project"] = "elsewhere"
if case == "extra-volume": volumes.append({"Name": "extra"})
if case == "labels": c["Config"]["Labels"][LABEL + "project.config_files"] = "/other"
e.objects = lambda kind, **kwargs: volumes if kind == "volume" else [c]
e.docker = lambda *args: b""
try:
    e.inventory()
except Refused:
    pass
else:
    raise AssertionError("unsafe inventory accepted")
""",
        case,
    )


def test_environment_does_not_enable_external_integration():
    # Normal test suite must not accidentally inherit production endpoint opts.
    assert "EAR_RUN_BACKUP_TESTS" not in os.environ or os.environ["EAR_RUN_BACKUP_TESTS"] in {
        "0",
        "1",
    }


def test_actual_helper_packs_filesystem_modes_and_rejects_links(tmp_path):
    isolated(
        """
import io
root = Path(sys.argv[2]) / "volume"
root.mkdir(mode=0o755)
root.chmod(0o755)
(root / "data").write_bytes(b"synthetic")
helper = HELPER.replace('Path("/volume")', 'Path(' + repr(str(root)) + ')')
result = subprocess.run([sys.executable, "-c", helper, "pack"], capture_output=True)
assert result.returncode == 0
validate_tar(io.BytesIO(result.stdout))
with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
    assert archive.getmember(".").mode == 0o755
    assert archive.extractfile("data").read() == b"synthetic"
(root / "link").symlink_to(root / "data")
result = subprocess.run([sys.executable, "-c", helper, "pack"], capture_output=True)
assert result.returncode != 0
(root / "link").unlink()
(root / "data").chmod(0o4600)
result = subprocess.run([sys.executable, "-c", helper, "pack"], capture_output=True)
assert result.returncode != 0
""",
        tmp_path,
    )


def test_subprocess_diagnostics_never_echo_secrets():
    isolated(
        """
import secrets
secret = secrets.token_urlsafe(48)
e = object.__new__(Engine)
e.compose = ["/synthetic-compose"]
e.env = {}
original = subprocess.run
subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
    a[0], 7, b"", ("ValueError: unsafe archive " + secret).encode())
try:
    e.run(["docker", "run", HELPER, "pack", secret])
except Refused as exc:
    assert str(exc) == (
        "subprocess failed: category=helper exit=7 errors=ValueError,unsafe-archive")
    assert secret not in str(exc)
else:
    raise AssertionError("failure not reported")
finally:
    subprocess.run = original
"""
    )


@pytest.mark.parametrize(
    "schedule",
    [
        "existing",
        "fresh",
        "volume-first",
        "volume-second",
        "between-resources",
        "adopt",
        "post-create",
        "before-start",
        "cleanup-label-changed",
    ],
)
def test_restore_foreign_race_schedule(tmp_path, schedule):
    isolated(
        r"""
import io
case = sys.argv[3]
p = Path(sys.argv[2]) / "bundle"
b = Bundle(p, create=True)
b.put("database.dump", b"PGDMPsynthetic")
for name in ("nats.tar", "identities.tar"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        m = tarfile.TarInfo("identities.env" if name == "identities.tar" else "data")
        m.uid, m.gid, m.mode, m.size = 10001, 10001, 0o600, 1
        archive.addfile(m, io.BytesIO(b"x"))
    b.put(name, data.getvalue())
manifest = dict(formatVersion=1, schemaVersion=14, databaseServerMajor=17,
    sourceProject="source", natsImage="nats:test", natsImageId="sha256:test",
    files={name: b.digest(name) for name in FILES})
b.put("manifest.json", json.dumps(manifest).encode())
sha = b.digest("manifest.json")
b.put("READY", sha.encode())
b.close()
class Fake(Engine):
    def __init__(self):
        self.project, self.operation, self.stop_timeout = "target", "operation-a", 2
        self.compose_file, self.compose = Path("/synthetic/compose.yaml"), ["compose"]
        self.config = {"services": {"nats": {"image": "nats:test"}}, "networks": {}}
        self.cs, self.vs, self.calls = [], [], []
        self.fired, self.volume_calls = False, 0
        if case == "existing": self.inject()
    def isolate(self):
        self.requested = self.project
    def postgres_create_command(self):
        return ["docker", "create", "--name", "target-postgres-1"]
    def install_postgres_secrets(self, cid):
        assert cid == "a"*64
    def inject(self):
        if self.fired: return
        self.fired = True
        self.cs.append({"Id": "b-exact", "Config": {"Labels": {
            LABEL + "project": self.project, LABEL + "service": "postgres"}},
            "State": {"Running": True}})
    def objects(self, kind, **kwargs):
        return self.cs if kind == "container" else self.vs if kind == "volume" else []
    def fresh(self):
        super().fresh()
        if case == "fresh": self.inject()
    def docker(self, *args):
        self.calls.append(args)
        if args[:2] == ("image", "inspect"): return b'[{"Id":"sha256:test"}]'
        if args[:2] == ("volume", "create"):
            self.volume_calls += 1
            raced = ((case == "volume-first" and self.volume_calls == 1) or
                     (case == "volume-second" and self.volume_calls == 2))
            if raced: self.inject()
            self.vs.append({"Name": args[-1], "Labels": {
                LABEL + "project": self.project,
                "ear.backup.operation": "b" if raced else self.operation}})
            return b""
        if args[:2] == ("volume", "inspect"):
            return json.dumps([v for v in self.vs if v["Name"] == args[-1]]).encode()
        if args[:2] == ("container", "inspect"):
            return json.dumps([c for c in self.cs if c["Id"] == args[-1]]).encode()
        if args[0] in {"start", "stop"}:
            assert args[-1] != "b-exact", "foreign lifecycle mutation"
            c = next(c for c in self.cs if c["Id"] == args[-1])
            assert c["Config"]["Labels"]["ear.backup.operation"] == self.operation
            c["State"]["Running"] = args[0] == "start"
            if case in {"post-create", "cleanup-label-changed"} and args[0] == "start":
                self.inject()
                if case == "cleanup-label-changed":
                    c["Config"]["Labels"]["ear.backup.operation"] = "changed"
            return b""
        if args[0] == "ps": return b""
        raise AssertionError(args)
    def helper(self, key, mode, **kwargs):
        assert not self.fired
        if case == "between-resources": self.inject()
    def run(self, command, **kwargs):
        assert command[:2] == ["docker", "create"] and "--name" in command
        assert "up" not in command
        if case == "adopt":
            self.inject()
            raise Refused("atomic name conflict")
        self.cs.append({"Id": "a"*64, "Config": {"Labels": {
            LABEL + "project": self.project, LABEL + "service": "postgres",
            "ear.backup.operation": self.operation}}, "State": {"Running": False}})
        if case == "before-start": self.inject()
        return b"a"*64
f = Fake()
try:
    restore(f, p, sha, False)
except Refused:
    pass
else:
    raise AssertionError("race accepted")
assert f.fired
assert next(c for c in f.cs if c["Id"] == "b-exact")["State"]["Running"]
assert all(call[-1] != "b-exact" for call in f.calls if call[0] in {"stop", "start"})
if case in {"existing", "fresh"}:
    assert not f.vs and not f.restore_volumes and not f.restore_containers
if case == "volume-first": assert not f.restore_volumes
if case == "volume-second": assert len(f.restore_volumes) == 1
if case == "post-create":
    assert ("stop", "--time", "2", "a"*64) in f.calls
if case == "cleanup-label-changed":
    assert not any(call[0] == "stop" for call in f.calls)
print("deterministic schedule passed:", case)
""",
        tmp_path,
        schedule,
    )
