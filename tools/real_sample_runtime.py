"""Private, fixed-project operator helper; no upgrades, deletion, or provider start.

Use plan first. prepare writes ONLY a new outside-repo 0700 directory and 0600
files. initialize requires --integration-ready after the main agent freezes and
builds images. An interrupted initialization is NOT automatically resumed.
up never migrates, builds, pulls, rotates or recreates containers. Preserve all
state/volumes on failure. No generic compose passthrough is exposed.

container-config/container-export are internal one-shot commands in the NEW
isolated storage image, not source credential discovery. The latter's stdout is
captured directly into a private client file, never sent to an operator terminal.

There is deliberately NO import command: an explicit capture selection is not
source authorization. The coordinator must first prove a dedicated source token
has Evidence AND embedding read scopes and a trusted read-only raw-audio view.
Use HttpEarSource + InboxAudioLocator + Runner only after that gate, after target
consumers are healthy, with an explicit bounded capture_ids list and private
logging (Runner logs capture identifiers). Never use a source DB/artifact cache.
This profile supports deterministic evidence viewing/attribution, NOT LLM
activity extraction. The latest extraction GPS geocode gate remains unchanged.
No geocoder, provider, external actions, model calls or source discovery here.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_ROOTLESS = ContextVar("rootless_docker", default=False)
PROJECT = "ear-real-sample-20261118"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path.home() / ".local/state" / PROJECT
SERVICES = (
    "postgres",
    "nats",
    "storage",
    "relay",
    "manage-worker",
    "api",
    "mvp-attribution",
    "mvp-dependency",
)
INIT = ("migrate", "bootstrap", "init-identities", "init-core-identities", "real-sample-config")
VOLUMES = (
    "postgres_data",
    "nats_data",
    "identities",
    "real_api_config",
    "real_attribution_config",
    "real_dependency_config",
    "api_execution",
    "mvp_attribution_state",
    "mvp_dependency_state",
)
IMAGES = {
    "EAR_STORAGE_IMAGE": "ear-storage",
    "EAR_API_IMAGE": "ear-api",
    "EAR_MVP_WORKER_IMAGE": "ear-mvp-worker",
}
FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
BASE_IMAGES = {"POSTGRES_IMAGE": "postgres:17-alpine", "NATS_IMAGE": "nats:2.11-alpine"}
ERROR = '{"code":"real_sample_runtime_refused","preserve_state":true}'


class Refused(ValueError):
    """Fixed diagnostics only; never attach subprocess output or secrets."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("duplicate_key")
        result[key] = value
    return result


@contextmanager
def directory(path: Path, *, private: bool = False, owner: int | None = None):
    owner = os.geteuid() if owner is None else owner
    if not path.is_absolute() or ".." in path.parts:
        raise Refused("absolute_no_links_path_required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in {0, owner, os.geteuid()} or (
                info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                raise Refused("unsafe_ancestor")
        info = os.fstat(fd)
        if private and (info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700):
            raise Refused("private_directory_required")
        yield fd
    finally:
        os.close(fd)


def read_private(path: Path, *, owner: int | None = None, limit: int = 65536) -> bytes:
    owner = os.geteuid() if owner is None else owner
    with directory(path.parent, owner=owner) as parent:
        fd = os.open(path.name, os.O_RDONLY | FLAGS, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != owner
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size > limit
            ):
                raise Refused("private_file_required")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
            after = os.fstat(fd)
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            # Ignore atime, but reject changed content, permissions, links or identity.
            if (
                before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
                or before.st_size != after.st_size
                or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
                or len(data) != before.st_size
                or len(data) > limit
            ):
                raise Refused("private_file_changed")
            return data
        finally:
            os.close(fd)


def load_private(path: Path, *, owner: int | None = None) -> dict[str, Any]:
    value = json.loads(read_private(path, owner=owner), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise Refused("object_required")
    return value


def publish(path: Path, value: bytes, *, owner: int | None = None) -> None:
    """Exclusive publication; partial files retained, never repaired or overwritten."""
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FLAGS, 0o600, dir_fd=parent)
        try:
            os.fchmod(fd, 0o600)
            if owner is not None:
                os.fchown(fd, owner, owner)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(value)
                stream.flush()
                os.fsync(fd)
            os.fsync(parent)
        finally:
            os.close(fd)


def publish_json(path: Path, value: Any, *, owner: int | None = None) -> None:
    publish(path, (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode(), owner=owner)


def rootless_environment() -> dict[str, str]:
    uid = os.geteuid()
    if uid == 0:
        raise Refused("rootless_user_required")
    runtime = Path("/run/user") / str(uid)
    with directory(runtime, private=True) as parent:
        sock = os.stat("docker.sock", dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != uid:
            raise Refused("private_rootless_socket_required")
    return {"DOCKER_HOST": f"unix://{runtime}/docker.sock", "XDG_RUNTIME_DIR": str(runtime)}


def clean_environment() -> dict[str, str]:
    # No inherited EAR_*, COMPOSE_*, DOCKER_*, proxies, or dotenv discovery.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR") if k in os.environ}
    env.update(COMPOSE_DISABLE_ENV_FILE="1", DOCKER_HOST="unix:///var/run/docker.sock")
    if _ROOTLESS.get():
        env.update(rootless_environment())
    return env


def require_rootless_daemon() -> None:
    value = json.loads(
        command(["docker", "info", "--format", "{{json .SecurityOptions}}"], timeout=10)
    )
    if not isinstance(value, list) or "name=rootless" not in value:
        raise Refused("rootless_daemon_required")


def command(argv: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(
        argv, capture_output=True, cwd=ROOT, env=clean_environment(), timeout=timeout, check=False
    )
    if result.returncode:
        raise Refused("operator_command_failed")
    return result.stdout.decode()


def dc(state: Path, compose: str) -> list[str]:
    if compose:
        binary = Path(compose)
        if not binary.is_absolute() or ".." in binary.parts:
            raise Refused("absolute_compose_binary_required")
        with directory(binary.parent):
            info = binary.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o022
                or info.st_uid not in {0, os.geteuid()}
                or not os.access(binary, os.X_OK)
            ):
                raise Refused("unsafe_compose_binary")
    prefix = [compose] if compose else ["docker", "compose"]
    return [
        *prefix,
        "--project-name",
        PROJECT,
        "--env-file",
        str(state / "runtime.env"),
        "-f",
        str(ROOT / "deploy/compose.yaml"),
        "-f",
        str(ROOT / "deploy/compose.real-sample.yaml"),
    ]


def inventory() -> dict[str, list[str]]:
    result = {}
    for kind, args in {
        "containers": ["ps", "-a"],
        "volumes": ["volume", "ls"],
        "networks": ["network", "ls"],
    }.items():
        field = "{{.Names}}" if kind == "containers" else "{{.Name}}"
        all_names = command(
            ["docker", *args, "--filter", f"name={PROJECT}", "--format", field]
        ).splitlines()
        selected = {
            n
            for n in all_names
            if n == PROJECT or n.startswith(PROJECT + "-") or n.startswith(PROJECT + "_")
        }
        selected.update(
            command(
                [
                    "docker",
                    *args,
                    "--filter",
                    f"label=com.docker.compose.project={PROJECT}",
                    "--format",
                    field,
                ]
            ).splitlines()
        )
        result[kind] = sorted(selected)
    return result


def require_fresh() -> None:
    if any(inventory().values()):
        raise Refused("project_already_exists")


def check_ports() -> None:
    # Docker daemon published ports AND local namespace listeners; check again at up.
    ports = (18100, 18186)
    published = "\n".join(
        command(["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Ports}}"])
        for port in ports
    )
    if any(re.search(rf":{port}->", published) for port in ports):
        raise Refused("port_busy")
    for port in ports:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))


def state_path(value: str) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path == Path("/")
        or path.is_relative_to(ROOT)
    ):
        raise Refused("outside_repository_state_required")
    return path


def validate_thresholds(cosine: Any, gps: Any) -> None:
    if (
        type(cosine) not in {float, int}
        or type(gps) not in {float, int}
        or not math.isfinite(cosine)
        or not -1 <= cosine <= 1
        or not math.isfinite(gps)
        or gps < 0
    ):
        raise Refused("explicit_thresholds_required")


def validate_options(options: dict) -> None:
    if (
        set(options)
        != {"project", "cosine_threshold", "gps_threshold_m", "storage_port", "api_port"}
        or options["project"] != PROJECT
        or type(options["storage_port"]) is not int
        or type(options["api_port"]) is not int
        or options["storage_port"] != 18100
        or options["api_port"] != 18186
    ):
        raise Refused("state_mismatch")
    validate_thresholds(options["cosine_threshold"], options["gps_threshold_m"])


def check_budget() -> None:
    # Read-only Linux preflight; no model load, build or cleanup to make room.
    info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    if int(info["MemAvailable"].split()[0]) < 4 * 1024 * 1024:
        raise Refused("insufficient_memory_headroom")


def prepare(state: Path, *, cosine: float, gps: float) -> None:
    validate_thresholds(cosine, gps)
    require_fresh()
    check_ports()
    with directory(state.parent) as parent:
        os.mkdir(state.name, 0o700, dir_fd=parent)
    options = {
        "project": PROJECT,
        "cosine_threshold": cosine,
        "gps_threshold_m": gps,
        "storage_port": 18100,
        "api_port": 18186,
    }
    publish_json(state / "options.json", options)
    values = {
        "EAR_POSTGRES_PASSWORD": secrets.token_hex(32),
        "EAR_SERVICE_DB_PASSWORD": secrets.token_hex(32),
        "EAR_NATS_TOKEN": "n" + secrets.token_hex(32),
        "EAR_STORAGE_PORT": "18100",
        "EAR_API_PORT": "18186",
        "EAR_REAL_OPTIONS_FILE": str(state / "options.json"),
        "EAR_REAL_RUNTIME_SCRIPT": str(ROOT / "tools/real_sample_runtime.py"),
        **{key: f"{image}:{PROJECT}" for key, image in IMAGES.items()},
    }
    if any(not re.fullmatch(r"[A-Za-z0-9_./:-]+", v) for v in values.values()):
        raise Refused("unsafe_env_value")
    publish(state / "runtime.env", "".join(f"{k}={v}\n" for k, v in values.items()).encode())


def validate_state(state: Path) -> None:
    with directory(state, private=True):
        pass
    options = load_private(state / "options.json")
    validate_options(options)
    values = unique_object(
        line.split("=", 1) for line in read_private(state / "runtime.env").decode().splitlines()
    )
    expected = {
        "EAR_STORAGE_PORT": "18100",
        "EAR_API_PORT": "18186",
        "EAR_REAL_OPTIONS_FILE": str(state / "options.json"),
        "EAR_REAL_RUNTIME_SCRIPT": str(ROOT / "tools/real_sample_runtime.py"),
        **{key: f"{image}:{PROJECT}" for key, image in IMAGES.items()},
    }
    secret_keys = {"EAR_POSTGRES_PASSWORD", "EAR_SERVICE_DB_PASSWORD", "EAR_NATS_TOKEN"}
    if set(values) != set(expected) | secret_keys or any(
        values[k] != v for k, v in expected.items()
    ):
        raise Refused("environment_mismatch")
    for key in secret_keys:
        pattern = r"n[0-9a-f]{64}" if key == "EAR_NATS_TOKEN" else r"[0-9a-f]{64}"
        if not re.fullmatch(pattern, values[key]):
            raise Refused("invalid_private_environment")


def source_digest() -> str:
    if command(["git", "-C", str(ROOT), "diff", "--name-only", "--diff-filter=U"]).strip():
        raise Refused("integration_conflicts")
    names = command(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard"]).splitlines()
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        if name.endswith(".md") or name.startswith(("tests/", ".github/")) or "/tests/" in name:
            continue
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def image_ids() -> dict[str, str]:
    ids = {
        key: command(
            ["docker", "image", "inspect", f"{image}:{PROJECT}", "--format", "{{.Id}}"]
        ).strip()
        for key, image in IMAGES.items()
    }
    ids.update(
        {
            key: command(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).strip()
            for key, image in BASE_IMAGES.items()
        }
    )
    if any(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None for value in ids.values()):
        raise Refused("invalid_image_identity")
    protected = command(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=ear-dev"]
    ).splitlines()
    if protected:
        used = command(["docker", "inspect", "--format", "{{.Image}}", *protected]).splitlines()
        if {ids[key] for key in IMAGES} & set(used):
            raise Refused("protected_image_reuse")
    return ids


def validate_compose(state: Path, compose: str) -> None:
    # Never print resolved config (which may contain credentials).
    config = json.loads(
        command([*dc(state, compose), "--profile", "init", "config", "--format", "json"])
    )
    if config["name"] != PROJECT or set(config["services"]) != set(SERVICES + INIT):
        raise Refused("unexpected_services")
    image_for = {name: f"{IMAGES['EAR_STORAGE_IMAGE']}:{PROJECT}" for name in SERVICES + INIT}
    image_for.update(
        postgres=BASE_IMAGES["POSTGRES_IMAGE"],
        nats=BASE_IMAGES["NATS_IMAGE"],
        api=f"{IMAGES['EAR_API_IMAGE']}:{PROJECT}",
    )
    image_for.update(
        {
            name: f"{IMAGES['EAR_MVP_WORKER_IMAGE']}:{PROJECT}"
            for name in ("mvp-attribution", "mvp-dependency")
        }
    )
    allowed_binds = {
        "postgres": {
            (str(ROOT / "deploy/postgres-init.sh"), "/docker-entrypoint-initdb.d/10-ear.sh")
        },
        "nats": {(str(ROOT / "deploy/nats.conf"), "/etc/nats/ear.conf")},
        "real-sample-config": {
            (str(ROOT / "tools/real_sample_runtime.py"), "/opt/real_sample_runtime.py")
        },
    }
    ports_expected = {"storage": (18100, 8000), "api": (18186, 8086)}
    for name, service in config["services"].items():
        if (
            any(
                service.get(k)
                for k in (
                    "container_name",
                    "network_mode",
                    "privileged",
                    "pid",
                    "ipc",
                    "devices",
                    "volumes_from",
                    "extra_hosts",
                )
            )
            or service["image"] != image_for[name]
        ):
            raise Refused("unsafe_compose")
        ports = service.get("ports", [])
        if len(ports) != (1 if name in ports_expected else 0):
            raise Refused("unsafe_port")
        for port in ports:
            published, target = ports_expected[name]
            if (
                port.get("host_ip") != "127.0.0.1"
                or str(port["published"]) != str(published)
                or port["target"] != target
                or port.get("protocol", "tcp") != "tcp"
            ):
                raise Refused("unsafe_port")
        if set(service.get("networks", {})) - {"backend", "frontend"}:
            raise Refused("unsafe_network")
        if (
            not 0 < int(service.get("mem_limit", 0)) <= 512 * 1024 * 1024
            or not 0 < float(service.get("cpus", 0)) <= 1
            or not 0 < int(service.get("pids_limit", 0)) <= 128
        ):
            raise Refused("unbounded_resources")
        if name in SERVICES and service.get("logging", {}).get("driver") != "none":
            raise Refused("private_logging_required")
        for volume in service.get("volumes", []):
            if volume["type"] == "bind":
                if (volume["source"], volume["target"]) not in allowed_binds.get(
                    name, set()
                ) or volume.get("read_only") is not True:
                    raise Refused("unexpected_bind_mount")
            elif volume["type"] != "volume" or volume["source"] not in VOLUMES:
                raise Refused("unexpected_volume")
        if name not in {"postgres", "nats", "real-sample-config"} and (
            service.get("user") != "10001:10001"
            or service.get("cap_add")
            or "ALL" not in service.get("cap_drop", [])
            or not any(
                x in service.get("security_opt", [])
                for x in ("no-new-privileges:true", "no-new-privileges")
            )
        ):
            raise Refused("unsafe_runtime_identity")
        if name in {"api", "mvp-attribution", "mvp-dependency"} and not service.get("read_only"):
            raise Refused("readonly_runtime_required")
    secrets_spec = config.get("secrets", {})
    if set(secrets_spec) != {
        "postgres_password",
        "service_db_password",
        "nats_token",
        "real_options",
    }:
        raise Refused("unexpected_secret")
    for key, spec in secrets_spec.items():
        if spec.get("external"):
            raise Refused("external_secret_refused")
        if key == "real_options":
            if spec.get("file") != str(state / "options.json"):
                raise Refused("unexpected_secret_file")
        elif spec.get("environment") != "EAR_" + key.upper():
            raise Refused("unexpected_secret_environment")
    for kind, expected in (("volumes", set(VOLUMES)), ("networks", {"backend", "frontend"})):
        if set(config[kind]) != expected:
            raise Refused("unexpected_resource")
        for key, resource in config[kind].items():
            if (
                resource.get("external")
                or resource.get("driver_opts")
                or resource.get("driver") not in (None, "local" if kind == "volumes" else "bridge")
                or resource.get("name") != f"{PROJECT}_{key}"
            ):
                raise Refused("external_resource_refused")
    if not all(n.get("internal") for n in config["networks"].values()):
        raise Refused("egress_network_refused")


def resources() -> dict[str, dict[str, str]]:
    found = inventory()
    result = {}
    for kind, names in found.items():
        result[kind] = {}
        allowed = (
            {f"{PROJECT}-{service}-1" for service in SERVICES}
            if kind == "containers"
            else {
                f"{PROJECT}_{key}"
                for key in (VOLUMES if kind == "volumes" else ("backend", "frontend"))
            }
        )
        if set(names) - allowed:
            raise Refused("unexpected_project_resource")
        for name in names:
            prefix = (
                ["docker", "inspect"] if kind == "containers" else ["docker", kind[:-1], "inspect"]
            )
            item = json.loads(command([*prefix, name]))[0]
            labels = (
                item.get("Config", {}).get("Labels", {})
                if kind == "containers"
                else item.get("Labels", {})
            )
            if labels.get("com.docker.compose.project") != PROJECT:
                raise Refused("resource_owner_mismatch")
            if kind == "volumes":
                # Docker has no volume UUID. Name alone misses deletion/recreation;
                # retain daemon creation metadata (not contents or credentials).
                if not item.get("CreatedAt") or item.get("Driver") != "local":
                    raise Refused("volume_identity_unavailable")
                metadata = {
                    key: item.get(key)
                    for key in ("Name", "CreatedAt", "Driver", "Scope", "Options", "Labels")
                }
                result[kind][name] = hashlib.sha256(
                    json.dumps(metadata, sort_keys=True).encode()
                ).hexdigest()
            else:
                ident = item.get("Id")
                if not isinstance(ident, str) or not ident:
                    raise Refused("resource_identity_unavailable")
                result[kind][name] = ident
    return result


def validate_clients(value: Any) -> None:
    fixed = {
        "project": PROJECT,
        "storage_url": "http://127.0.0.1:18100",
        "api_url": "http://127.0.0.1:18186",
    }
    keys = {"adapter_storage_token", "api_user_token"}
    if (
        not isinstance(value, dict)
        or set(value) != set(fixed) | keys
        or any(value[k] != v for k, v in fixed.items())
        or any(
            not isinstance(value[k], str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value[k]) is None
            for k in keys
        )
        or value["adapter_storage_token"] == value["api_user_token"]
    ):
        raise Refused("invalid_client_export")


def initialize(state: Path, compose: str, *, integration_ready: bool) -> None:
    if not integration_ready:
        raise Refused("main_agent_readiness_required")
    validate_state(state)
    check_budget()
    with directory(state, private=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_fresh()
        check_ports()
        snapshot = {"source_sha256": source_digest(), "images": image_ids()}
        validate_compose(state, compose)
        # Intent is retained even if the first Docker call fails. Never auto-upgrade/reissue.
        publish_json(state / "initialization-intent.json", snapshot)
        base = dc(state, compose)
        command(
            [*base, "up", "-d", "--no-build", "--pull", "never", "--wait", "postgres", "nats"],
            timeout=180,
        )
        for service in INIT:
            command(
                [
                    *base,
                    "--profile",
                    "init",
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--pull",
                    "never",
                    service,
                ],
                timeout=180,
            )
        exported = command(
            [
                *base,
                "--profile",
                "init",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--pull",
                "never",
                "real-sample-config",
                "container-export",
            ],
            timeout=60,
        )
        clients = json.loads(exported, object_pairs_hook=unique_object)
        validate_clients(clients)
        publish_json(state / "clients.json", clients)
        if snapshot != {"source_sha256": source_digest(), "images": image_ids()}:
            raise Refused("integration_changed")
        publish_json(state / "initialized.json", {**snapshot, "resources": resources()})


def up(state: Path, compose: str, *, integration_ready: bool) -> None:
    if not integration_ready:
        raise Refused("main_agent_readiness_required")
    validate_state(state)
    check_budget()
    with directory(state, private=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = load_private(state / "initialized.json")
        intent = load_private(state / "initialization-intent.json")
        if {k: saved[k] for k in ("source_sha256", "images")} != intent:
            raise Refused("initialization_receipt_mismatch")
        if saved["source_sha256"] != source_digest() or saved["images"] != image_ids():
            raise Refused("frozen_source_or_images_changed")
        current = resources()
        for kind, entries in saved["resources"].items():
            if any(current[kind].get(name) != ident for name, ident in entries.items()):
                raise Refused("initialized_resource_missing_or_replaced")
        # On first up, ports must be free; on repeat Docker owns its published bindings.
        if not current["containers"].get(f"{PROJECT}-storage-1"):
            check_ports()
        validate_compose(state, compose)
        command(
            [
                *dc(state, compose),
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--no-recreate",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                "180",
                *SERVICES,
            ],
            timeout=240,
        )


def make_configs(bundle: dict, options: dict) -> dict[str, dict]:
    """Pure config construction: real issued identities, never fixture credentials."""
    validate_options(options)
    credentials = bundle["credentials"]
    tokens = [
        credentials[role]["token"] for role in ("adapter", "api_user", "attribution", "dependency")
    ]
    if any(
        not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", t) for t in tokens
    ) or len(set(tokens)) != len(tokens):
        raise Refused("invalid_issued_credentials")
    configs = {}
    for module, consumer in (("attribution", "attribution"), ("dependency", "dependency.changes")):
        configs[module] = {
            "module": module,
            "storage_url": "http://storage:8000",
            "storage_token": credentials[module]["token"],
            "consumer": consumer,
            "journal_namespace": PROJECT,
            "state_dir": "/state",
        }
    configs["attribution"].update(
        cosine_threshold=options["cosine_threshold"], gps_threshold_m=options["gps_threshold_m"]
    )
    configs["api"] = {
        "storage_url": "http://storage:8000",
        "public_origin": "http://127.0.0.1:18186",
        "execution_action_v2": False,
        "bindings": [
            {
                "subject": "real_sample_user",
                "role": "user",
                "token": secrets.token_urlsafe(32),
                "storage_token": credentials["api_user"]["token"],
            }
        ],
    }
    return configs


def container_config() -> None:
    bundle = load_private(Path("/run/secrets/ear/core/identities.json"), owner=10001)
    options_path = Path("/run/secrets/real-options.json")
    options = load_private(options_path, owner=options_path.stat().st_uid)
    if options["project"] != PROJECT:
        raise Refused("wrong_project")
    configs = make_configs(bundle, options)
    # Each named volume is new and empty; fail instead of repairing old permissions.
    for name in configs:
        with directory(Path("/config") / name) as parent_fd:
            if os.listdir(parent_fd):
                raise Refused("config_volume_not_empty")
    for name, value in configs.items():
        parent = Path("/config") / name
        leaf = parent / "runtime"
        leaf.mkdir(mode=0o700)
        publish_json(leaf / "config.json", value, owner=10001)
        os.chown(leaf, 10001, 10001)


def container_export() -> dict:
    if os.geteuid() != 0 or not Path("/.dockerenv").exists():
        raise Refused("container_only")
    bundle = load_private(Path("/run/secrets/ear/core/identities.json"), owner=10001)
    api = load_private(Path("/config/api/runtime/config.json"), owner=10001)
    return {
        "project": PROJECT,
        "storage_url": "http://127.0.0.1:18100",
        "adapter_storage_token": bundle["credentials"]["adapter"]["token"],
        "api_url": "http://127.0.0.1:18186",
        "api_user_token": next(b["token"] for b in api["bindings"] if b["role"] == "user"),
    }


def plan(state: Path, compose: str) -> dict:
    import shlex

    script = [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/real_sample_runtime.py")]
    common = ["--state", str(state)] + (["--compose-bin", compose] if compose else [])
    if _ROOTLESS.get():
        common += ["--rootless"]
    return {
        "project": PROJECT,
        "state": str(state),
        "started": False,
        "enabled": list(SERVICES),
        "disabled": ["extraction", "gateway", "geocoder", "external_actions"],
        "ports": {"storage": "127.0.0.1:18100", "api": "127.0.0.1:18186"},
        "initialization_order": list(INIT),
        "real_input": {
            "authorization_proved": False,
            "import_command_available": False,
            "required_source_scopes": ["Evidence read", "speaker embedding read"],
            "transport": "HttpEarSource and InboxAudioLocator, never DB/artifact bypass",
            "selection": "explicit bounded capture_ids; never list/scan all captures",
            "logging": "private only; Runner includes capture identifiers",
            "consumer_gate": "attribution and dependency healthy before any import",
            "gps_gate": "unchanged; GPS extraction waits for gps_geocode",
            "result_kind": "deterministic evidence view, not LLM activities/artifacts",
        },
        "resource_gate": "at least 4 GiB MemAvailable before initialization/start; serial only",
        "commands": {
            "prepare": shlex.join(
                [
                    *script,
                    "prepare",
                    *common,
                    "--cosine-threshold",
                    "0.9",
                    "--gps-threshold-m",
                    "100",
                ]
            ),
            "validate": shlex.join([*script, "validate", *common]),
            "initialize_after_main_agent_ready": shlex.join(
                [*script, "initialize", *common, "--integration-ready"]
            ),
            "up_after_initialize": shlex.join([*script, "up", *common, "--integration-ready"]),
            "status": shlex.join([*script, "status", *common]),
        },
        "thresholds": "example explicit values, NOT calibrated; operator selects before prepare",
        "build_after_integration": [
            shlex.join(
                [
                    "docker",
                    "build",
                    "--pull=false",
                    "-f",
                    str(ROOT / "deploy" / dockerfile),
                    "-t",
                    f"{image}:{PROJECT}",
                    str(ROOT),
                ]
            )
            for image, dockerfile in (
                ("ear-storage", "Dockerfile"),
                ("ear-api", "Dockerfile.api"),
                ("ear-mvp-worker", "Dockerfile.worker"),
            )
        ],
    }


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    selection = _ROOTLESS.set(False)
    try:
        parser = Parser(description=__doc__, allow_abbrev=False)
        parser.add_argument(
            "action",
            choices=[
                "plan",
                "prepare",
                "validate",
                "initialize",
                "up",
                "status",
                "container-config",
                "container-export",
            ],
        )
        parser.add_argument("--state", default=str(DEFAULT_STATE))
        parser.add_argument("--compose-bin", default="")
        parser.add_argument("--rootless", action="store_true", help="use only own runtime socket")
        parser.add_argument("--integration-ready", action="store_true")
        parser.add_argument("--cosine-threshold", type=float)
        parser.add_argument("--gps-threshold-m", type=float)
        args = parser.parse_args(argv)
        _ROOTLESS.set(args.rootless)
        if args.rootless and args.action != "plan":
            if args.action in {"container-config", "container-export"}:
                raise Refused("rootless_operator_flag_only")
            require_rootless_daemon()
        if args.action == "container-config":
            if os.geteuid() != 0 or not Path("/.dockerenv").exists():
                raise Refused("container_only")
            container_config()
        elif args.action == "container-export":
            print(json.dumps(container_export()))
            return 0
        else:
            state = state_path(args.state)
            if args.action == "plan":
                print(json.dumps(plan(state, args.compose_bin), indent=2))
                return 0
            if args.action == "prepare":
                if args.cosine_threshold is None or args.gps_threshold_m is None:
                    raise Refused("explicit_thresholds_required")
                prepare(state, cosine=args.cosine_threshold, gps=args.gps_threshold_m)
            elif args.action == "validate":
                validate_state(state)
                validate_compose(state, args.compose_bin)
            elif args.action == "initialize":
                initialize(state, args.compose_bin, integration_ready=args.integration_ready)
            elif args.action == "up":
                up(state, args.compose_bin, integration_ready=args.integration_ready)
            else:
                # Only selected metadata, never Docker inspect Env/logs or app bodies.
                names = inventory()["containers"]
                statuses = []
                for name in names:
                    statuses.append(
                        json.loads(
                            command(["docker", "inspect", "--format", "{{json .State}}", name])
                        )
                    )
                print(
                    json.dumps(
                        {
                            "project": PROJECT,
                            "containers": len(names),
                            "running": sum(s["Running"] for s in statuses),
                            "healthy": sum(
                                s.get("Health", {}).get("Status") == "healthy" for s in statuses
                            ),
                        }
                    )
                )
                return 0
        print(json.dumps({"project": PROJECT, "operation": args.action, "status": "completed"}))
        return 0
    except Exception:
        print(ERROR, file=sys.stderr)
        return 1
    finally:
        _ROOTLESS.reset(selection)


if __name__ == "__main__":
    raise SystemExit(main())
