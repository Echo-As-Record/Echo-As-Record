"""Operator-only offline backup; no application imports, dotenv discovery or retention.

Only trusted local Compose files/archives and an exclusive Docker administrator are
supported. Stop other operators/schedulers first; Docker has no atomic project lock.
SIGKILL/host loss requires manual recovery using PARTIAL and manifest startState.
Identity archives contain credentials: protect the entire 0700 bundle off-host.
The operator env file is NEVER copied. Fresh PostgreSQL/NATS passwords may change;
restored identity tokens must remain paired with their database hashes.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

VOLUMES = {"postgres_data", "nats_data", "identities"}
APPS = ("storage", "relay", "manage-worker")
SERVICES = {"postgres", "nats", *APPS, "migrate", "bootstrap", "init-identities"}
FILES = ("database.dump", "nats.tar", "identities.tar")
LABEL = "com.docker.compose."
SCHEMA = 14

# This exact stdlib helper runs in a cached Python image, never in an application.
# Only the one validated volume is mounted; no socket, host directory or secrets.
HELPER = r"""
import os, sys, stat, tarfile, shutil
from pathlib import Path, PurePosixPath

def valid(m):
    p = PurePosixPath(m.name)
    if (p.is_absolute() or ".." in p.parts or "\\" in m.name
        or not (m.isdir() or m.isfile()) or m.mode & ~0o777
        or m.uid < 0 or m.gid < 0):
        raise ValueError("unsafe archive")
    return p

root = Path("/volume")
mode = sys.argv[1]
if mode == "empty":
    if any(root.iterdir()):
        raise ValueError("volume not empty")
elif mode == "pack":
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for base, dirs, files in os.walk(root, followlinks=False):
            for path in [Path(base), *(Path(base) / n for n in sorted(files))]:
                m = archive.gettarinfo(str(path), str(path.relative_to(root)))
                m.mode = stat.S_IMODE(m.mode)
                valid(m)
                if m.isfile():
                    with path.open("rb") as f:
                        archive.addfile(m, f)
                else:
                    archive.addfile(m)
            for d in dirs:
                if (Path(base) / d).is_symlink():
                    raise ValueError("source symlink")
elif mode == "unpack":
    if any(root.iterdir()):
        raise ValueError("volume not empty")
    seen, directories = set(), []
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        for m in archive:
            p = valid(m)
            key = str(p)
            if key in seen or (key == "." and not m.isdir()):
                raise ValueError("duplicate archive entry")
            seen.add(key)
            dest = root / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            if m.isdir():
                dest.mkdir(exist_ok=True)
                directories.append((dest, m))
            else:
                with dest.open("xb") as out:
                    source = archive.extractfile(m)
                    if source is None:
                        raise ValueError("missing file")
                    with source:
                        shutil.copyfileobj(source, out)
                    out.flush()
                    os.fchown(out.fileno(), m.uid, m.gid)
                    os.fchmod(out.fileno(), m.mode)
                    os.fsync(out.fileno())
    for dest, m in reversed(directories):
        os.chown(dest, m.uid, m.gid)
        os.chmod(dest, m.mode)
    for base, dirs, files in os.walk(root):
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
else:
    raise ValueError("unknown helper operation")
"""


class Refused(Exception):
    """Messages in this class are constant, credential-free operator diagnostics."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise Refused(message)


def absolute(value: str) -> Path:
    p = Path(value)
    require(p.is_absolute() and ".." not in p.parts, "absolute path without '..' required")
    for part in (p, *p.parents):
        require(not part.is_symlink(), "symlink paths are forbidden")
    return p


def directory_fd(path: Path) -> int:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class Bundle:
    def __init__(self, path: Path, *, create: bool = False):
        if create:
            parent = directory_fd(path.parent)
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        self.fd = directory_fd(path)
        require(stat.S_IMODE(os.fstat(self.fd).st_mode) == 0o700, "bundle must be mode 0700")

    def open(self, name: str, *, write: bool = False) -> BinaryIO:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_WRONLY | os.O_CREAT | os.O_EXCL if write else os.O_RDONLY
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            os.close(fd)
            raise Refused("bundle files must be regular mode 0600")
        return os.fdopen(fd, "wb" if write else "rb")

    def put(self, name: str, data: bytes) -> None:
        with self.open(name, write=True) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.fsync(self.fd)

    def get(self, name: str) -> bytes:
        with self.open(name) as f:
            return f.read()

    def digest(self, name: str) -> str:
        with self.open(name) as f:
            digest = hashlib.sha256()
            while block := f.read(1024 * 1024):
                digest.update(block)
            return digest.hexdigest()

    def remove(self, name: str) -> None:
        os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)

    def close(self) -> None:
        os.close(self.fd)


def validate_tar(stream: BinaryIO, *, identities: bool = False) -> None:
    seen: set[str] = set()
    found_identity = False
    with tarfile.open(fileobj=stream, mode="r:*") as archive:
        for m in archive:
            p = Path(m.name)
            require(
                not p.is_absolute()
                and ".." not in p.parts
                and "\\" not in m.name
                and (m.isdir() or m.isfile())
                and not m.mode & ~0o777
                and m.uid >= 0
                and m.gid >= 0,
                "unsafe archive member",
            )
            key = str(p)
            require(key not in seen and (key != "." or m.isdir()), "duplicate archive member")
            seen.add(key)
            if key == "identities.env":
                require(m.isfile() and m.mode == 0o600 and m.uid == 10001, "identity permissions")
                found_identity = True
    require(not identities or found_identity, "identity archive missing identity file")


class Engine:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.restore_containers: set[str] = set()
        self.restore_volumes: set[str] = set()
        self.stop_timeout = args.stop_timeout
        self.project: str = args.project if args.command == "backup" else args.new_project
        require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", self.project), "invalid project name")
        self.compose_file = absolute(args.compose_file)
        self.env_file = absolute(args.env_file)
        require(self.compose_file.is_file() and self.env_file.is_file(), "explicit files required")
        require(stat.S_IMODE(self.env_file.stat().st_mode) == 0o600, "env file must be 0600")
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR"}
        }
        self.env["COMPOSE_DISABLE_ENV_FILE"] = "1"
        self.compose = [
            str(absolute(args.compose_bin)),
            "--project-name",
            self.project,
            "--project-directory",
            str(self.compose_file.parent),
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.compose_file),
        ]
        self.config = json.loads(
            self.run([*self.compose, "--profile", "init", "config", "--format", "json"])
        )
        self.validate_config()
        self.run(["docker", "image", "inspect", args.helper_image])
        self.operation = uuid.uuid4().hex
        self.restore_containers: set[str] = set()
        self.restore_volumes: set[str] = set()
        self.requested = self.project

    def run(
        self, command: list[str], *, source: BinaryIO | None = None, output: BinaryIO | None = None
    ) -> bytes:
        # Never echo argv, config, Docker diagnostics, SQL errors or archive bytes.
        try:
            result = subprocess.run(
                command,
                stdin=source,
                stdout=output or subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise Refused("operation timed out; operator must inspect project") from None
        if result.returncode:
            category = "compose" if command[0] == self.compose[0] else "docker"
            for name in ("pg_dump", "pg_restore", "psql"):
                if name in command:
                    category = name
            if HELPER in command:
                category = "helper"
            # Emit only fixed labels, never matched stderr text or argv values.
            kinds = [
                name
                for name in ("PermissionError", "ValueError", "FileNotFoundError", "OSError")
                if re.search(rb"(?:^|\n)" + name.encode() + rb":", result.stderr)
            ]
            for needle, label in (
                (b"server version mismatch", "version-mismatch"),
                (b"Permission denied", "permission-denied"),
                (b"unsafe archive", "unsafe-archive"),
                (b"unrecognized option", "invalid-option"),
            ):
                if needle in result.stderr:
                    kinds.append(label)
            raise Refused(
                f"subprocess failed: category={category} exit={result.returncode} "
                + "errors="
                + (",".join(kinds) or "suppressed")
            )
        # pg_dump warnings are not accepted as a silently successful backup.
        if "pg_dump" in command:
            require(not result.stderr.strip(), "pg_dump warning; backup refused")
        return result.stdout or b""

    def docker(self, *args: str) -> bytes:
        return self.run(["docker", *args])

    def objects(self, kind: str, *, project: bool = False) -> list[dict[str, Any]]:
        prefix = ["ps", "-aq"] if kind == "container" else [kind, "ls", "-q"]
        if project:
            prefix += ["--filter", f"label={LABEL}project={self.project}"]
        ids = self.docker(*prefix).decode().split()
        return json.loads(self.docker(kind, "inspect", *ids)) if ids else []

    def validate_config(self) -> None:
        c = self.config
        require(set(c["services"]) == SERVICES, "unsupported service set; unknown writers refused")
        require(set(c["volumes"]) == VOLUMES, "exact three storage volumes required")
        for key, v in c["volumes"].items():
            require(
                v.get("name") == f"{self.project}_{key}"
                and not v.get("external")
                and not v.get("driver_opts")
                and v.get("driver", "local") == "local",
                "external, aliased or nonlocal volumes forbidden",
            )
        expected = {
            "postgres": {"postgres_data": "/var/lib/postgresql/data"},
            "nats": {"nats_data": "/data"},
            "manage-worker": {"identities": "/run/secrets/ear"},
            "init-identities": {"identities": "/run/secrets/ear"},
        }
        for service, conf in c["services"].items():
            require(
                not conf.get("container_name")
                and not conf.get("privileged")
                and not conf.get("network_mode")
                and not conf.get("volumes_from"),
                "unsafe container configuration",
            )
            mounts = {
                v["source"]: v["target"] for v in conf.get("volumes", []) if v["type"] == "volume"
            }
            require(mounts == expected.get(service, {}), "unexpected service volume mapping")
            for mount in conf.get("volumes", []):
                if mount["type"] == "bind":
                    target = {
                        "postgres": "/docker-entrypoint-initdb.d/10-ear.sh",
                        "nats": "/etc/nats/ear.conf",
                    }.get(service)
                    require(mount["target"] == target and mount.get("read_only"), "unexpected bind")
                else:
                    require(mount["type"] == "volume", "unexpected mount type")
            if service in {"postgres", "nats"}:
                require(not conf.get("ports"), "storage infrastructure must not publish ports")
        for net in c.get("networks", {}).values():
            require(
                not net.get("external") and net["name"].startswith(self.project + "_"),
                "external network forbidden",
            )

    def inventory(self) -> dict[str, dict[str, Any]]:
        all_containers = self.objects("container", project=True)
        own_ids = {c["Id"] for c in all_containers}
        for key in VOLUMES:
            attached = set(
                self.docker("ps", "-aq", "--no-trunc", "--filter", "volume=" + self.volume(key))
                .decode()
                .split()
            )
            require(attached <= own_ids, "foreign volume attachment")
        found: dict[str, dict[str, Any]] = {}
        volumes = self.objects("volume", project=True)
        require(
            {v["Name"] for v in volumes} == {self.volume(v) for v in VOLUMES},
            "project must own exactly three volumes",
        )
        for v in volumes:
            require(
                v["Labels"].get(LABEL + "volume") in VOLUMES
                and v["Driver"] == "local"
                and not v.get("Options"),
                "volume labels/driver mismatch",
            )
        for c in all_containers:
            labels = c["Config"].get("Labels") or {}
            if labels.get(LABEL + "project") != self.project:
                require(
                    not any(
                        m.get("Name") in {self.volume(v) for v in VOLUMES}
                        for m in c.get("Mounts", [])
                    ),
                    "foreign volume attachment",
                )
                continue
            service = labels.get(LABEL + "service")
            require(service in SERVICES and service not in found, "unknown or duplicate service")
            require(
                labels.get(LABEL + "project.config_files") == str(self.compose_file),
                "container compose-file label mismatch",
            )
            require(
                labels.get(LABEL + "oneoff", "False").lower() == "false", "oneoff container present"
            )
            require(
                not c["State"].get("Paused") and not c["State"].get("Restarting"),
                "paused/restarting container: operator intervention required; never unpaused",
            )
            require(
                c["State"]["Status"] in {"running", "created", "exited"}, "unstable container state"
            )
            require(
                service in {*APPS, "postgres", "nats"} or not c["State"]["Running"],
                "initialization writer is running",
            )
            configured = {
                self.volume(m["source"]): m["target"]
                for m in self.config["services"][service].get("volumes", [])
                if m["type"] == "volume"
            }
            actual = {
                m["Name"]: m["Destination"] for m in c.get("Mounts", []) if m["Type"] == "volume"
            }
            require(actual == configured, "container volume mapping mismatch")
            found[str(service)] = c
        return found

    def volume(self, key: str) -> str:
        return f"{self.project}_{key}"

    def helper(
        self, key: str, mode: str, *, source: BinaryIO | None = None, output: BinaryIO | None = None
    ) -> None:
        name = f"ear-backup-helper-{self.operation}-{key}"
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--pull=never",
            "--name",
            name,
            "--label",
            f"ear.backup.operation={self.operation}",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "128m",
            "--pids-limit",
            "32",
        ]
        if mode == "unpack":
            command += ["--cap-add", "CHOWN", "--cap-add", "FOWNER"]
        mount = f"type=volume,src={self.volume(key)},dst=/volume,volume-nocopy"
        if mode != "unpack":
            mount += ",readonly"
        command += [
            "--mount",
            mount,
            "--entrypoint",
            "python",
            self.args.helper_image,
            "-c",
            HELPER,
            mode,
        ]
        try:
            self.run(command, source=source, output=output)
        finally:
            # Named helper belongs exclusively to this invocation. Killing it must
            # complete before source services can resume, even on interrupted I/O.
            result = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True,
                env=self.env,
                check=False,
                timeout=30,
            )
            if result.returncode == 0:
                obj = json.loads(result.stdout)[0]
                require(
                    obj["Config"]["Labels"].get("ear.backup.operation") == self.operation,
                    "helper ownership changed",
                )
                self.docker("rm", "-f", obj["Id"])

    def sql(self, container: str, statement: str) -> str:
        return (
            self.docker(
                "exec",
                container,
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                "ear",
                "-Atc",
                statement,
            )
            .decode()
            .strip()
        )

    def meta(self, container: str) -> tuple[int, int]:
        return (
            int(self.sql(container, "SHOW server_version_num")) // 10000,
            int(self.sql(container, "SELECT schema_revision FROM storage.meta")),
        )

    def fresh(self) -> None:
        for kind in ("container", "volume", "network"):
            require(not self.objects(kind, project=True), "target project already exists")
        names = {v["Name"] for v in self.objects("volume")}
        require(not names & {self.volume(v) for v in VOLUMES}, "target volume already exists")
        networks = {n["Name"] for n in self.objects("network")}
        require(
            not networks & {v["name"] for v in self.config["networks"].values()},
            "target network already exists",
        )

    def restore_guard(self) -> None:
        """The tool lock is not a Docker lock: recheck between every mutation."""
        if self.project != self.requested:
            for prefix in (("ps", "-aq"), ("volume", "ls", "-q"), ("network", "ls", "-q")):
                require(
                    not self.docker(*prefix, "--filter", f"label={LABEL}project={self.requested}"),
                    "requested project appeared during restore",
                )
        for c in self.objects("container", project=True):
            require(
                c["Id"] in self.restore_containers
                and c["Config"].get("Labels", {}).get("ear.backup.operation") == self.operation,
                "foreign target container; no cleanup of foreign resources",
            )
        for kind in ("volume", "network"):
            expected = (
                {self.volume(k) for k in VOLUMES}
                if kind == "volume"
                else {n["name"] for n in self.config["networks"].values()}
            )
            for obj in self.objects(kind):
                labels = obj.get("Labels") or {}
                if obj["Name"] in expected or labels.get(LABEL + "project") == self.project:
                    require(
                        obj["Name"] in expected
                        and labels.get("ear.backup.operation") == self.operation
                        and (kind != "volume" or obj["Name"] in self.restore_volumes),
                        "foreign target resource; no cleanup of foreign resources",
                    )
        for key in self.restore_volumes:
            attached = set(
                self.docker("ps", "-aq", "--no-trunc", "--filter", "volume=" + key).decode().split()
            )
            require(attached <= self.restore_containers, "foreign target volume attachment")

    def stop_restore_containers(self) -> None:
        # Never enumerate a project to decide what to stop. Names can be reused.
        for cid in sorted(self.restore_containers):
            with contextlib.suppress(Exception):
                c = json.loads(self.docker("container", "inspect", cid))[0]
                labels = c["Config"].get("Labels") or {}
                if (
                    c["Id"] == cid
                    and labels.get("ear.backup.operation") == self.operation
                    and labels.get(LABEL + "project") == self.project
                    and c["State"]["Running"]
                ):
                    self.docker("stop", "--time", str(self.stop_timeout), cid)

    def isolate(self) -> None:
        # The operator's public project may come online at any instant. Never
        # allocate a volume or run a lifecycle command in that namespace.
        self.requested = self.project
        require(
            not self.docker(
                "volume", "ls", "-q", "--filter", f"label=ear.backup.target={self.requested}"
            ),
            "requested restore already allocated",
        )
        self.project = "ear-restored-" + self.operation
        self.compose[self.compose.index("--project-name") + 1] = self.project
        self.config = json.loads(
            self.run([*self.compose, "--profile", "init", "config", "--format", "json"])
        )
        self.validate_config()
        self.restore_guard()

    def up(self, *services: str) -> None:
        # Only the fixed PG service is translated. Compose itself operates the
        # published project after restore; unsupported shapes fail closed.
        require(services == ("postgres",), "start applications separately after handoff")
        argv = self.postgres_create_command()
        cid = self.run(argv).decode().strip()
        require(re.fullmatch(r"[0-9a-f]{64}", cid), "invalid create result")
        self.restore_containers.add(cid)
        self.restore_guard()
        self.install_postgres_secrets(cid)
        self.restore_guard()
        self.docker("start", cid)
        self.restore_guard()

    def postgres_create_command(self) -> list[str]:
        conf = self.config["services"]["postgres"]
        require(
            set(conf)
            <= {
                "image",
                "environment",
                "healthcheck",
                "networks",
                "restart",
                "volumes",
                "mem_limit",
                "cpus",
                "command",
                "entrypoint",
                "secrets",
            }
            and not conf.get("command")
            and not conf.get("entrypoint")
            and conf["image"] == "postgres:17-alpine"
            and conf["networks"] == {"backend": None}
            and conf["environment"]
            == {
                "POSTGRES_DB": "ear",
                "POSTGRES_USER": "postgres",
                "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_password",
            },
            "unsupported postgres config",
        )
        net = self.config["networks"]["backend"]
        require(net.get("internal") is True, "internal backend required")
        self.restore_guard()
        label = f"ear.backup.operation={self.operation}"
        self.docker(
            "network",
            "create",
            "--internal",
            "--label",
            label,
            "--label",
            f"{LABEL}project={self.project}",
            "--label",
            f"{LABEL}network=backend",
            net["name"],
        )
        self.restore_guard()
        argv = [
            "docker",
            "create",
            "--pull=never",
            "--name",
            self.project + "-postgres-1",
            "--network",
            net["name"],
            "--network-alias",
            "postgres",
            "--label",
            label,
            "--memory",
            str(conf["mem_limit"]),
            "--cpus",
            str(conf["cpus"]),
            "--restart",
            conf["restart"],
        ]
        for key, value in {
            "project": self.project,
            "service": "postgres",
            "container-number": "1",
            "oneoff": "False",
            "project.config_files": str(self.compose_file),
            "project.working_dir": str(self.compose_file.parent),
            "config-hash": self.run([*self.compose, "config", "--hash", "postgres"])
            .decode()
            .split()[-1],
        }.items():
            argv.extend(["--label", f"{LABEL}{key}={value}"])
        for key, value in conf["environment"].items():
            argv.extend(["-e", f"{key}={value}"])
        for m in conf["volumes"]:
            src = self.volume(m["source"]) if m["type"] == "volume" else m["source"]
            argv.extend(
                [
                    "--mount",
                    f"type={m['type']},src={src},dst={m['target']}"
                    + (",readonly" if m.get("read_only") else ""),
                ]
            )
        h = conf["healthcheck"]
        require(h["test"][0] == "CMD-SHELL", "unsupported PG healthcheck")
        argv.extend(
            [
                "--health-cmd",
                h["test"][1],
                "--health-interval",
                h["interval"],
                "--health-timeout",
                h["timeout"],
                "--health-retries",
                str(h["retries"]),
                conf["image"],
            ]
        )
        return argv

    def install_postgres_secrets(self, cid: str) -> None:
        conf = self.config["services"]["postgres"]
        require(
            {m["source"]: m["target"] for m in conf["secrets"]}
            == {k: "/run/secrets/" + k for k in ("postgres_password", "service_db_password")},
            "unsupported PG secrets",
        )
        # Resolve the same explicit dotenv with Compose interpolation; no secret
        # in argv, logs, host temp files, or docker inspect environment.
        expressions = {
            k: "${" + self.config["secrets"][k]["environment"] + ":?required}"
            for k in ("postgres_password", "service_db_password")
        }
        with tempfile.TemporaryFile() as src:
            src.write(json.dumps({"services": {"postgres": {"environment": expressions}}}).encode())
            src.seek(0)
            values = json.loads(
                self.run([*self.compose, "-f", "-", "config", "--format", "json"], source=src)
            )["services"]["postgres"]["environment"]
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            directory = tarfile.TarInfo("secrets")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            tar.addfile(directory)
            for k in expressions:
                data = values[k].encode()
                m = tarfile.TarInfo("secrets/" + k)
                m.size = len(data)
                m.mode = 0o444
                tar.addfile(m, io.BytesIO(data))
        with tempfile.TemporaryFile() as pipe:
            pipe.write(archive.getvalue())
            pipe.seek(0)
            self.run(["docker", "cp", "-", cid + ":/run"], source=pipe)

    def wait_postgres(self) -> str:
        for _ in range(120):
            self.restore_guard()
            containers = self.objects("container", project=True)
            pg = [
                c for c in containers if c["Config"]["Labels"].get(LABEL + "service") == "postgres"
            ]
            if len(pg) == 1 and pg[0]["State"].get("Health", {}).get("Status") == "healthy":
                return pg[0]["Id"]
            time.sleep(0.5)
        raise Refused("fresh PostgreSQL not healthy")


def backup(engine: Engine, path: Path, resume: bool) -> None:
    initial = engine.inventory()
    require("postgres" in initial and "nats" in initial, "postgres and nats containers required")
    require(initial["postgres"]["State"]["Running"], "postgres must already be running")
    bundle = Bundle(path, create=True)
    original = {s: c["Id"] for s, c in initial.items() if c["State"]["Running"] and s != "postgres"}
    success = False
    try:
        bundle.put(
            "PARTIAL", b"Not ready. Keep partial files. Operator must inspect source state.\n"
        )
        start_state = {s: c["State"]["Status"] for s, c in initial.items()}
        bundle.put("start-state.json", json.dumps(start_state, sort_keys=True).encode())
        for s in (*APPS, "nats"):
            if s in original:
                engine.docker(
                    "stop", "--time", str(getattr(engine, "stop_timeout", 45)), original[s]
                )
        stopped = engine.inventory()
        require(
            all(not c["State"]["Running"] for s, c in stopped.items() if s != "postgres"),
            "writer or broker still running",
        )
        pg = initial["postgres"]["Id"]
        require(
            engine.sql(
                pg,
                "SELECT count(*) FROM pg_stat_activity WHERE datname='ear' "
                "AND backend_type='client backend' AND pid<>pg_backend_pid()",
            )
            == "0",
            "unexpected database client; exclusive offline window required",
        )
        major, revision = engine.meta(pg)
        require(major == 17 and revision == SCHEMA, "unsupported database/schema version")
        with bundle.open("database.dump", write=True) as f:
            engine.run(
                [
                    "docker",
                    "exec",
                    pg,
                    "pg_dump",
                    "-U",
                    "postgres",
                    "-d",
                    "ear",
                    "-Fc",
                    "--no-owner",
                    "--no-acl",
                ],
                output=f,
            )
            f.flush()
            os.fsync(f.fileno())
        for key, name in (("nats_data", "nats.tar"), ("identities", "identities.tar")):
            with bundle.open(name, write=True) as f:
                engine.helper(key, "pack", output=f)
                f.flush()
                os.fsync(f.fileno())
            with bundle.open(name) as f:
                validate_tar(f, identities=key == "identities")
        final = engine.inventory()
        require(
            all(not c["State"]["Running"] for s, c in final.items() if s != "postgres"),
            "writer restarted during backup; partial backup refused",
        )
        require(
            {s: c["Id"] for s, c in final.items()} == {s: c["Id"] for s, c in initial.items()},
            "project changed during backup",
        )
        manifest = {
            "formatVersion": 1,
            "schemaVersion": revision,
            "databaseServerMajor": major,
            "natsImage": initial["nats"]["Config"]["Image"],
            "natsImageId": initial["nats"]["Image"],
            "sourceProject": engine.project,
            "startState": start_state,
            "stoppedServices": list(original),
            "files": {n: bundle.digest(n) for n in FILES},
        }
        bundle.put("manifest.json", json.dumps(manifest, sort_keys=True).encode())
        digest = bundle.digest("manifest.json")
        bundle.put("READY", (digest + "\n").encode())
        bundle.remove("PARTIAL")
        success = True
    finally:
        first_failure = sys.exc_info()[0] is not None
        failed_resume = False
        if not success or resume:
            for service in ("nats", *APPS):
                if service in original:
                    try:
                        engine.docker("start", original[service])
                    except BaseException:
                        failed_resume = True
        if not success or failed_resume:
            with contextlib.suppress(Exception):
                bundle.remove("READY")
            with contextlib.suppress(Exception):
                bundle.put("PARTIAL", b"Operator recovery required; not ready.\n")
            print(
                "backup incomplete; needs operator inspection; partial bundle retained",
                file=sys.stderr,
            )
        bundle.close()
        if failed_resume and not first_failure:
            raise Refused(
                "failed to restore original running services; operator intervention required"
            )
    print(
        json.dumps(
            {
                "status": "ready",
                "manifestSha256": digest,
                "sourceResumed": resume,
                "operatorEnvIncluded": False,
            }
        )
    )


def restore(engine: Engine, path: Path, trusted_sha: str, start: bool) -> None:
    require(re.fullmatch(r"[a-f0-9]{64}", trusted_sha), "trusted manifest SHA-256 required")
    bundle = Bundle(path)
    engine.restore_containers = set()
    engine.restore_volumes = set()
    try:
        require("PARTIAL" not in os.listdir(bundle.fd), "partial backup cannot be restored")
        require(bundle.digest("manifest.json") == trusted_sha, "manifest checksum mismatch")
        require(bundle.get("READY").decode().strip() == trusted_sha, "backup not ready")
        manifest = json.loads(bundle.get("manifest.json"))
        require(
            manifest["formatVersion"] == 1
            and manifest["schemaVersion"] == SCHEMA
            and manifest["databaseServerMajor"] == 17,
            "schema/version mismatch; never auto-upgrade",
        )
        require(
            manifest["sourceProject"] != engine.project, "restore requires a different new project"
        )
        require(set(manifest["files"]) == set(FILES), "unexpected manifest payloads")
        for name in FILES:
            require(bundle.digest(name) == manifest["files"][name], "payload checksum mismatch")
        for name in ("nats.tar", "identities.tar"):
            with bundle.open(name) as f:
                validate_tar(f, identities=name == "identities.tar")
        with bundle.open("database.dump") as f:
            require(f.read(5) == b"PGDMP", "not a PostgreSQL custom archive")
        image = engine.config["services"]["nats"]["image"]
        require(image == manifest["natsImage"], "NATS image tag mismatch")
        require(
            json.loads(engine.docker("image", "inspect", image))[0]["Id"]
            == manifest["natsImageId"],
            "NATS cached image differs from backup",
        )
        engine.fresh()
        require(not start, "start NATS/apps explicitly with the returned project after restore")
        engine.isolate()
        for key in sorted(VOLUMES):
            engine.restore_guard()
            engine.docker(
                "volume",
                "create",
                "--label",
                f"{LABEL}project={engine.project}",
                "--label",
                f"{LABEL}volume={key}",
                "--label",
                f"ear.backup.target={engine.requested}",
                "--label",
                f"ear.backup.operation={engine.operation}",
                engine.volume(key),
            )
            volume = json.loads(engine.docker("volume", "inspect", engine.volume(key)))[0]
            require(
                volume["Labels"].get("ear.backup.operation") == engine.operation,
                "volume creation raced; operator intervention required",
            )
            engine.restore_volumes.add(engine.volume(key))
            engine.restore_guard()
            engine.helper(key, "empty")
            engine.restore_guard()
        for key, name in (("nats_data", "nats.tar"), ("identities", "identities.tar")):
            engine.restore_guard()
            with bundle.open(name) as f:
                engine.helper(key, "unpack", source=f)
            engine.restore_guard()
        engine.up("postgres")  # Fresh postgres-init.sh only. NEVER application init profiles.
        pg = engine.wait_postgres()
        engine.inventory()
        require(int(engine.sql(pg, "SHOW server_version_num")) // 10000 == 17, "PG major mismatch")
        require(
            engine.sql(
                pg,
                "SELECT count(*) FROM pg_namespace WHERE nspname NOT IN "
                "('public','information_schema') AND nspname NOT LIKE 'pg_%'",
            )
            == "0",
            "target database is not empty",
        )
        require(
            engine.sql(
                pg,
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON "
                "n.oid=c.relnamespace WHERE n.nspname='public'",
            )
            == "0",
            "target public schema is not empty",
        )
        require(
            engine.sql(
                pg,
                "SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb "
                "AND NOT rolcreaterole FROM pg_roles WHERE rolname='ear_storage'",
            )
            == "t",
            "fresh runtime owner missing/unsafe",
        )
        engine.restore_guard()
        with bundle.open("database.dump") as f:
            engine.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    pg,
                    "pg_restore",
                    "-U",
                    "postgres",
                    "-d",
                    "ear",
                    "--no-owner",
                    "--no-acl",
                    "--role",
                    "ear_storage",
                    "--exit-on-error",
                    "--single-transaction",
                ],
                source=f,
            )
        require(engine.meta(pg) == (17, SCHEMA), "restored schema mismatch; no auto-upgrade")
        require(
            engine.sql(
                pg,
                "SELECT count(*) FROM pg_namespace WHERE nspname NOT IN "
                "('public','information_schema') AND nspname NOT LIKE 'pg_%' "
                "AND nspowner<>(SELECT oid FROM pg_roles WHERE rolname='ear_storage')",
            )
            == "0",
            "restored schema ownership mismatch",
        )
        engine.restore_guard()
        print(
            json.dumps(
                {"status": "restored", "project": engine.project, "applicationsStarted": start}
            )
        )
    except BaseException:
        engine.stop_restore_containers()
        if engine.restore_volumes or engine.restore_containers:
            print(
                "restore incomplete; owned resources retained; foreign resources untouched; "
                "operator inspection required",
                file=sys.stderr,
            )
        raise
    finally:
        bundle.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DANGER: offline storage backup / trusted restore only.",
        epilog="No discovery, downloads, retention or deletion. Protect "
        "0700 bundles: identities contain real secrets. Exclusive operator "
        "window required. SIGKILL/host loss needs manual state recovery.",
    )
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument(
        "--compose-bin", required=True, help="absolute path to trusted Compose v2 binary"
    )
    parser.add_argument(
        "--compose-file", required=True, help="absolute trusted compose file; no discovery"
    )
    parser.add_argument(
        "--env-file", required=True, help="absolute 0600 operator env; never copied/logged"
    )
    parser.add_argument(
        "--bundle", required=True, help="absolute 0700 bundle; backup refuses existing path"
    )
    parser.add_argument(
        "--helper-image",
        default="python:3.12-alpine",
        help="trusted cached Python image; never pulled",
    )
    parser.add_argument(
        "--stop-timeout",
        type=int,
        choices=range(1, 121),
        default=45,
        metavar="SECONDS",
        help="Docker stop grace period (default 45); forced termination may occur",
    )
    parser.add_argument("--project", help="exact existing SOURCE project, backup only")
    parser.add_argument(
        "--allow-downtime",
        action="store_true",
        help="required for backup; stops applications and NATS",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Restart originally running services after durability; failures attempt recovery",
    )
    parser.add_argument(
        "--new-project",
        help="unused isolated TARGET project; no existing containers/volumes/networks",
    )
    parser.add_argument("--confirm-new-project", help="must exactly equal --new-project")
    parser.add_argument(
        "--trust-backup",
        action="store_true",
        help="acknowledge archive can execute SQL; trust its producer, not merely its hash",
    )
    parser.add_argument(
        "--trusted-manifest-sha256",
        help="SHA-256 from trusted separate channel, not auto-read from bundle",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Unsupported during isolated restore; start NATS/apps with the returned project",
    )
    args = parser.parse_args()
    os.umask(0o077)
    lock = None
    try:
        if args.command == "backup":
            require(
                args.project and args.allow_downtime,
                "backup requires --project and --allow-downtime",
            )
            require(not args.new_project and not args.start, "restore options invalid for backup")
        else:
            require(
                args.new_project
                and args.confirm_new_project == args.new_project
                and args.trust_backup
                and args.trusted_manifest_sha256,
                "restore requires exact new-project confirmation, trust and manifest hash",
            )
            require(not args.project and not args.resume, "backup options invalid for restore")
        project = args.project or args.new_project
        require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project), "invalid project name")
        lock = os.open(
            f"/tmp/ear-storage-backup-{project}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = absolute(args.bundle)
        engine = Engine(args)
        if args.command == "backup":
            backup(engine, path, args.resume)
        else:
            restore(engine, path, args.trusted_manifest_sha256, args.start)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        print("storage backup/restore refused: " + message, file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            os.close(lock)


def interrupted(signum: int, frame: Any) -> None:
    raise Refused("interrupted; operator state recovery required")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
