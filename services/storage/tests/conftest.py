"""Opt-in isolated PostgreSQL 17; never reads .env or touches unrelated containers."""

from __future__ import annotations

import os
import secrets
import subprocess
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


class TestDsn(str):
    __test__ = False

    def __repr__(self) -> str:
        return "<isolated PostgreSQL test DSN redacted>"


def docker(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], check=False, capture_output=True, text=True, env=env, timeout=60
    )
    if result.returncode:
        # Never include command/environment/driver diagnostics containing a password.
        raise RuntimeError("isolated PostgreSQL docker operation failed")
    return result.stdout.strip()


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[TestDsn]:
    if os.environ.get("EAR_RUN_POSTGRES_TESTS") != "1":
        pytest.skip("real PostgreSQL disabled; set EAR_RUN_POSTGRES_TESTS=1 (Docker required)")
    if os.environ.get("EAR_POSTGRES_TEST_DSN"):
        raise RuntimeError("external databases are not allowed: use the isolated Docker fixture")
    token = uuid.uuid4().hex
    name = "ear-storage-it-" + token
    password = secrets.token_urlsafe(32)
    env = {**os.environ, "POSTGRES_PASSWORD": password}
    try:
        docker(
            "run",
            "--detach",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--label",
            "ear-storage-it=" + token,
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,noexec,nosuid,size=384m",
            "-e",
            "PGDATA=/var/lib/postgresql/data/pgdata",
            "-e",
            "POSTGRES_PASSWORD",
            "-p",
            "127.0.0.1::5432",
            "postgres:17-alpine",
            env=env,
        )
        port = docker("port", name, "5432/tcp").rsplit(":", 1)[1]
        dsn = TestDsn(
            make_conninfo(
                host="127.0.0.1",
                port=port,
                user="postgres",
                password=password,
                dbname="postgres",
                connect_timeout=2,
            )
        )
        for _ in range(100):
            try:
                with psycopg.connect(dsn, autocommit=True):
                    break
            except psycopg.OperationalError:
                time.sleep(0.1)
        else:
            raise RuntimeError("isolated PostgreSQL did not become ready")
        yield dsn
    finally:
        # Also handle a run command that timed out after Docker created the container.
        # Never clean by broad label selection or touch an unrelated container.
        probe = subprocess.run(
            ["docker", "inspect", "--format", '{{index .Config.Labels "ear-storage-it"}}', name],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if probe.returncode == 0:
            if probe.stdout.strip() != token:
                raise RuntimeError("refusing to remove a container with a different task label")
            docker("rm", "--force", name)


@pytest.fixture
def pg_dsn(postgres_server: TestDsn) -> Iterator[TestDsn]:
    name = "ear_storage_it_" + uuid.uuid4().hex
    with psycopg.connect(postgres_server, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield TestDsn(make_conninfo(postgres_server, dbname=name))
    finally:
        with psycopg.connect(postgres_server, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class NatsServer:
    def __init__(self, name: str, url: str, token: str) -> None:
        self.name, self.url, self.token = name, url, token

    def __repr__(self) -> str:
        return "<isolated NATS server redacted>"


@pytest.fixture(scope="session")
def nats_server() -> Iterator[NatsServer]:
    if os.environ.get("EAR_RUN_NATS_TESTS") != "1":
        pytest.skip("real NATS disabled; set EAR_RUN_NATS_TESTS=1")
    if os.environ.get("EAR_RUN_POSTGRES_TESTS") != "1":
        pytest.fail("NATS tests require EAR_RUN_POSTGRES_TESTS=1; cannot silently skip P2")
    if os.environ.get("EAR_NATS_TEST_DSN"):
        pytest.fail("external NATS is forbidden; use the isolated fixture")
    import socket

    label = uuid.uuid4().hex
    name = "ear-nats-it-" + label
    token = secrets.token_urlsafe(32)
    try:
        docker(
            "run",
            "--detach",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--label",
            "ear-nats-it=" + label,
            "--memory",
            "128m",
            "--cpus",
            "1",
            "--tmpfs",
            "/data:rw,noexec,nosuid,size=64m",
            "-p",
            "127.0.0.1::4222",
            "--entrypoint",
            "sh",
            "nats:2.11-alpine",
            "-c",
            "while [ ! -f /data/server.conf ]; do sleep 0.1; done; "
            "exec nats-server -c /data/server.conf",
        )
        # Test-only credential travels through stdin, never argv, environment or diagnostics.
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                name,
                "sh",
                "-c",
                "cat > /data/server.tmp && mv /data/server.tmp /data/server.conf",
            ],
            input=f'port: 4222\nauthorization {{ token: "{token}" }}\n'
            'jetstream { store_dir: "/data/js", max_file_store: 48MB, max_memory_store: 8MB }\n',
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError("isolated NATS configuration failed")
        port = int(docker("port", name, "4222/tcp").rsplit(":", 1)[1])
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("isolated NATS readiness failed")
        yield NatsServer(name, f"nats://127.0.0.1:{port}", token)
    finally:
        probe = subprocess.run(
            ["docker", "inspect", "--format", '{{index .Config.Labels "ear-nats-it"}}', name],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if probe.returncode == 0:
            if probe.stdout.strip() != label:
                raise RuntimeError("refusing unrelated NATS container cleanup")
            docker("rm", "--force", name)
