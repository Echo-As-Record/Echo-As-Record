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
