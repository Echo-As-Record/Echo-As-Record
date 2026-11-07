"""Opt-in real roundtrip. New synthetic projects only, no existing env/service access."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/storage_backup.py"

CONNECT = """
import asyncio, hashlib, json
from pathlib import Path
import psycopg, nats
from psycopg.conninfo import make_conninfo
from ear_storage import PostgresStorage
from ear_storage.auth import authenticate, token_digest
from ear_storage.deployment import read_identity_tokens
from ear_storage.schema import connection
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_contracts import new_id, VersionRef, Cause
from ear_storage_client import (
    Context, Create, Update, RegisterDependencies, MarkProcessed, PublishReevaluation,
)
password = Path("/run/secrets/service_db_password").read_text().strip()
dsn = make_conninfo(host="postgres", user="ear_storage", dbname="ear", password=password)
token = Path("/run/secrets/nats_token").read_text().strip()
cfg = RelayConfig(servers=("nats://nats:4222",), token=token)
"""
SEED = (
    CONNECT
    + """
store = PostgresStorage(dsn)
c = store.client("extraction")
a, b, event = new_id(), new_id(), new_id()
c.write([Create("activity", a, {"synthetic": 1}), Create("activity", b, {}),
         RegisterDependencies("activity", b, [VersionRef(data_type="activity", id=a, version=1)]),
         MarkProcessed("extraction.synthetic", event)], Context.system()).check()
c.write([Update("activity", a, 1, {"synthetic": 2})], Context.system()).check()
store.client("dependency").write([PublishReevaluation(
    "activity", b, Cause(data_type="activity", data_id=a, old_version=1, new_version=2),
    "synthetic restore proof")], Context(root_feedback_id="system", depth=1)).check()
assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == 4
async def seed_consumer():
    from nats.js.api import ConsumerConfig, AckPolicy
    nc = await nats.connect(servers=list(cfg.servers), token=token)
    try:
        js = nc.jetstream()
        await js.add_consumer(cfg.stream, ConsumerConfig(durable_name="backup_synthetic",
            ack_policy=AckPolicy.EXPLICIT, filter_subject="ear.events.>"))
        sub = await js.pull_subscribe("ear.events.>", durable="backup_synthetic", stream=cfg.stream)
        messages = await sub.fetch(1, timeout=3)
        await messages[0].ack_sync()
    finally:
        await nc.close()
asyncio.run(seed_consumer())
c.write([Create("activity", new_id(), {"pending": True})], Context.system()).check()
RelayStore(dsn, cfg).pause(True)
print("synthetic-seeded")
"""
)
SNAPSHOT = (
    CONNECT
    + """
result = {}
with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn:
    tables = conn.execute("SELECT table_schema,table_name FROM information_schema.tables "
        "WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema') "
        "ORDER BY 1,2").fetchall()
    for schema, table in tables:
        # Skip liveness bookkeeping changed by relay polls, not durable business content.
        if table == "relay_state":
            continue
        rows = conn.execute(psycopg.sql.SQL("SELECT row_to_json(t)::text FROM {}.{} t ORDER BY 1")
            .format(psycopg.sql.Identifier(schema), psycopg.sql.Identifier(table))).fetchall()
        result[schema + "." + table] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    tokens = read_identity_tokens(Path("/run/secrets/ear/identities.env").read_text())
    result["identitiesValid"] = all(authenticate(conn, token_digest(v)) for v in tokens.values())
    identity_bytes = Path("/run/secrets/ear/identities.env").read_bytes()
    result["identitiesSha"] = hashlib.sha256(identity_bytes).hexdigest()
    result["pending"] = conn.execute(
        "SELECT count(*) FROM storage.outbox WHERE delivered_at IS NULL").fetchone()[0]
    result["published"] = conn.execute("SELECT count(*) FROM storage.published").fetchone()[0]
    result["dependencies"] = conn.execute(
        "SELECT count(*) FROM storage.dependency_edges").fetchone()[0]
    result["processed"] = conn.execute("SELECT count(*) FROM storage.processed").fetchone()[0]
    result["schemaVersion"] = conn.execute("SELECT schema_revision FROM storage.meta").fetchone()[0]
    result["relayPaused"] = conn.execute(
        "SELECT paused FROM storage.relay_state WHERE singleton").fetchone()[0]
    result["role"] = conn.execute("SELECT current_user").fetchone()[0]
    result["safeRole"] = conn.execute(
        "SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole "
        "FROM pg_roles WHERE rolname=current_user").fetchone()[0]
async def snapshot():
    nc = await nats.connect(servers=list(cfg.servers), token=token)
    try:
        js = nc.jetstream()
        info = await js.stream_info(cfg.stream)
        consumer = await js.consumer_info(cfg.stream, "backup_synthetic")
        result["stream"] = [info.state.messages, info.state.first_seq, info.state.last_seq]
        result["consumer"] = [consumer.delivered.stream_seq, consumer.ack_floor.stream_seq,
                              consumer.num_pending, consumer.num_ack_pending]
    finally:
        await nc.close()
asyncio.run(snapshot())
print(json.dumps(result, sort_keys=True))
"""
)
OWNER = (
    CONNECT
    + """
with psycopg.connect(dsn) as conn:
    conn.execute("CREATE SCHEMA synthetic_restored_owner")
    conn.execute("CREATE TABLE synthetic_restored_owner.probe (id integer)")
    conn.execute("INSERT INTO synthetic_restored_owner.probe VALUES (1)")
    assert conn.execute("SELECT current_user").fetchone()[0] == "ear_storage"
    conn.rollback()
print("runtime-owner-ddl-and-insert-ok-rolled-back")
"""
)


def checked(command, *, data=None):
    # Compose shell variables take precedence over --env-file. Never import host
    # deploy credentials, port/image overrides or Compose project settings.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EAR_", "COMPOSE_"))}
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    result = subprocess.run(
        command, input=data, env=env, capture_output=True, check=False, timeout=600
    )
    if result.returncode:
        # No raw Docker diagnostics or command dump: they can contain credentials.
        raise AssertionError("isolated operation failed: " + command[0])
    return result.stdout


@pytest.mark.skipif(
    os.environ.get("EAR_RUN_BACKUP_TESTS") != "1",
    reason="explicit isolated backup roundtrip opt-in",
)
def test_real_offline_roundtrip():
    with tempfile.TemporaryDirectory(prefix="ear-backup-roundtrip-") as directory:
        run_roundtrip(Path(directory).resolve())


def run_roundtrip(work):
    override = os.environ.get("EAR_TEST_BACKUP_COMPOSE_BIN")
    if override is None:
        shim = work / "docker-compose"
        shim.write_text('#!/bin/sh\nexec docker compose "$@"\n')
        shim.chmod(0o700)
        compose_bin = str(shim)
    else:
        binary = Path(override)
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            pytest.fail("EAR_TEST_BACKUP_COMPOSE_BIN must name an absolute executable")
        compose_bin = str(binary.resolve())
    image = os.environ.get("EAR_TEST_BACKUP_IMAGE", "ear-storage:local")
    checked([compose_bin, "version"])
    checked(["docker", "image", "inspect", "--", image])
    source = "ear-backup-src-" + uuid.uuid4().hex[:12]
    target = "ear-backup-dst-" + uuid.uuid4().hex[:12]
    compose = work / "compose.yaml"
    text = (ROOT / "deploy/compose.yaml").read_text()
    text = text.replace("context: ..", "context: " + str(ROOT))
    text = text.replace("./postgres-init.sh:", str(ROOT / "deploy/postgres-init.sh") + ":")
    text = text.replace("./nats.conf:", str(ROOT / "deploy/nats.conf") + ":")
    compose.write_text(text)
    envs = {}
    for project in (source, target):
        env = work / (project + ".env")
        env.write_text(
            "\n".join(
                [
                    "EAR_POSTGRES_PASSWORD=" + secrets.token_urlsafe(32),
                    "EAR_SERVICE_DB_PASSWORD=" + secrets.token_urlsafe(32),
                    "EAR_NATS_TOKEN=a" + secrets.token_urlsafe(32),
                    f"EAR_STORAGE_IMAGE={image}",
                    "EAR_STORAGE_PORT=0",
                    "",
                ]
            )
        )
        env.chmod(0o600)
        envs[project] = env

    def dc(project, *args):
        return [
            compose_bin,
            "--project-name",
            project,
            "--project-directory",
            str(work),
            "--env-file",
            str(envs[project]),
            "-f",
            str(compose),
            *args,
        ]

    def code(project, program):
        return checked(
            dc(
                project,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "python",
                "manage-worker",
                "-c",
                program,
            )
        )

    def tool(command, project, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                command,
                "--compose-bin",
                compose_bin,
                "--compose-file",
                str(compose),
                "--env-file",
                str(envs[project]),
                "--bundle",
                str(work / "bundle"),
                "--stop-timeout",
                "2",
                *extra,
            ],
            capture_output=True,
            check=False,
            timeout=600,
        )

    requested_target = target
    state = {
        "phase": "initializing",
        "source": source,
        "target": target,
        "work": str(work),
        "production_touched": False,
    }
    (work / "state.json").write_text(json.dumps(state))

    def phase(name):
        state["phase"] = name
        (work / "state.json").write_text(json.dumps(state))
        print("roundtrip phase: " + name, flush=True)

    try:
        checked(dc(source, "--profile", "init", "run", "--rm", "-T", "init-identities"))
        checked(dc(source, "up", "-d", "--no-build", "--pull", "never", "nats", "storage"))
        phase("seed")
        code(source, SEED)
        # Runtime relay is logically paused; backup must not change that DB state.
        checked(dc(source, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "relay"))
        phase("snapshot-source")
        before = json.loads(code(source, SNAPSHOT))
        assert before["pending"] == 1 and before["published"] == 1
        assert before["stream"] == [4, 1, 4] and before["consumer"] == [1, 1, 3, 0]
        phase("backup")

        def service_states():
            ids = checked(dc(source, "ps", "-aq")).decode().split()
            objects = json.loads(checked(["docker", "inspect", *ids]))
            return {
                c["Config"]["Labels"]["com.docker.compose.service"]: c["State"]["Status"]
                for c in objects
            }

        original_states = service_states()
        result = tool("backup", source, "--project", source, "--allow-downtime", "--resume")
        (work / "backup.log").write_bytes(result.stdout + result.stderr)
        assert result.returncode == 0, result.stderr.decode()
        assert service_states() == original_states
        assert before["relayPaused"] is True
        manifest_sha = json.loads(result.stdout)["manifestSha256"]
        assert json.loads(code(source, SNAPSHOT)) == before
        manifest = json.loads((work / "bundle/manifest.json").read_bytes())
        assert manifest["startState"] == original_states
        assert manifest["startState"]["relay"] == "running"
        assert not (work / "bundle").joinpath(envs[source].name).exists()
        for p in (work / "bundle").iterdir():
            assert p.stat().st_mode & 0o777 == 0o600
        restore_args = (
            "--new-project",
            target,
            "--confirm-new-project",
            target,
            "--trust-backup",
            "--trusted-manifest-sha256",
            manifest_sha,
        )
        # All race participants are newly allocated test-owned projects. Injection
        # is Python monkeypatch only; the production CLI has no unsafe pause flag.
        race_proofs = []

        def exercise_race(schedule):
            race = "ear-backup-race-" + uuid.uuid4().hex[:12]
            race_env = work / (race + ".env")
            race_env.write_bytes(envs[target].read_bytes())
            race_env.chmod(0o600)
            envs[race] = race_env
            phase("race-" + schedule)
            import runpy
            import time
            from argparse import Namespace
            from unittest.mock import patch

            module = runpy.run_path(str(SCRIPT))
            engine_type = module["Engine"]
            args = Namespace(
                command="restore",
                new_project=race,
                stop_timeout=2,
                compose_bin=compose_bin,
                compose_file=str(compose),
                env_file=str(race_env),
                helper_image="python:3.12-alpine",
            )
            engine = engine_type(args)
            envs["ear-restored-" + engine.operation] = race_env
            raced = {}

            def database_bytes(cid):
                dump = checked(
                    [
                        "docker",
                        "exec",
                        cid,
                        "pg_dump",
                        "-U",
                        "postgres",
                        "-d",
                        "ear",
                        "--no-owner",
                        "--no-acl",
                    ]
                )
                return b"\n".join(
                    line
                    for line in dump.splitlines()
                    if not line.startswith((b"\\restrict ", b"\\unrestrict "))
                )

            def launch_b():
                checked(
                    dc(race, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "postgres")
                )
                cid = checked(dc(race, "ps", "-q", "postgres")).decode().strip()
                for _ in range(120):
                    obj = json.loads(checked(["docker", "inspect", cid]))[0]
                    if obj["State"].get("Health", {}).get("Status") == "healthy":
                        break
                    time.sleep(0.5)
                else:
                    raise AssertionError("B PostgreSQL did not become healthy")
                checked(
                    [
                        "docker",
                        "exec",
                        cid,
                        "psql",
                        "-X",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        "postgres",
                        "-d",
                        "ear",
                        "-c",
                        "CREATE TABLE public.race_sentinel (v text); "
                        "INSERT INTO public.race_sentinel VALUES ('B must survive');",
                    ]
                )
                raced.update(cid=cid, before=database_bytes(cid), started=obj["State"]["StartedAt"])

            original_fresh, original_docker, original_run = (
                engine.fresh,
                engine.docker,
                engine.run,
            )

            def paused_fresh():
                original_fresh()
                if schedule == "fresh":
                    launch_b()

            def paused_docker(*args):
                if schedule == "volume-create" and args[:2] == ("volume", "create") and not raced:
                    launch_b()
                return original_docker(*args)

            def paused_run(command, **kwargs):
                if (
                    (
                        schedule in {"compose-create", "stopped"}
                        and command[:2] == ["docker", "create"]
                    )
                    or (schedule == "db-import" and "pg_restore" in command)
                ) and not raced:
                    launch_b()
                    if schedule == "stopped":
                        checked(["docker", "stop", raced["cid"]])
                return original_run(command, **kwargs)

            with (
                patch.object(engine, "fresh", paused_fresh),
                patch.object(engine, "docker", paused_docker),
                patch.object(engine, "run", paused_run),
                pytest.raises(module["Refused"]),
            ):
                module["restore"](engine, work / "bundle", manifest_sha, False)
            assert raced, "injection did not run"
            obj = json.loads(checked(["docker", "inspect", raced["cid"]]))[0]
            assert obj["State"]["Running"] == (schedule != "stopped")
            assert not {m.get("Name") for m in obj["Mounts"]} & engine.restore_volumes
            assert obj["State"]["StartedAt"] == raced["started"]
            if schedule == "stopped":
                checked(["docker", "start", raced["cid"]])
                for _ in range(120):
                    if (
                        json.loads(checked(["docker", "inspect", raced["cid"]]))[0]["State"][
                            "Health"
                        ]["Status"]
                        == "healthy"
                    ):
                        break
                    time.sleep(0.5)
            assert database_bytes(raced["cid"]) == raced["before"]
            assert "ear.backup.operation" not in obj["Config"]["Labels"]
            race_proofs.append(
                dict(
                    schedule=schedule,
                    project=race,
                    containerId=raced["cid"],
                    runningAfterRefusal=obj["State"]["Running"],
                    databaseUnchanged=True,
                    notRestarted=True,
                    cleanupOwner="test",
                )
            )
            print("real race passed: " + schedule, flush=True)

        for schedule in ("fresh", "volume-create", "compose-create", "db-import", "stopped"):
            exercise_race(schedule)
        state["races"] = race_proofs
        phase("reject-corrupt-hash")
        dump = work / "bundle/database.dump"
        original_dump = dump.read_bytes()
        dump.write_bytes(original_dump + b"corruption")
        rejected = tool("restore", target, *restore_args)
        assert rejected.returncode == 1
        assert b"payload checksum mismatch" in rejected.stderr
        dump.write_bytes(original_dump)
        phase("restore")
        result = tool("restore", target, *restore_args)
        (work / "restore.log").write_bytes(result.stdout + result.stderr)
        assert result.returncode == 0, result.stderr.decode()
        requested = target
        target = json.loads(result.stdout)["project"]
        assert target.startswith("ear-restored-") and target != requested
        envs[target] = envs[requested]
        containers = json.loads(checked(dc(target, "ps", "-a", "--format", "json")))
        if isinstance(containers, dict):
            containers = [containers]
        assert {c["Service"] for c in containers} == {"postgres"}
        # Only NATS is started for read-only verification; applications stay absent.
        checked(dc(target, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "nats"))
        phase("snapshot-target")
        after = json.loads(code(target, SNAPSHOT))
        assert after == before
        assert after["safeRole"] is True
        code(target, OWNER)
        again = tool("restore", target, *restore_args)
        assert again.returncode == 1  # existing target is never dropped/overwritten
        assert json.loads(code(target, SNAPSHOT)) == before
        state.update(
            phase="passed",
            manifestSha256=manifest_sha,
            proof={
                "allTableHashesEqual": True,
                "identityTokensValid": True,
                "pendingOutbox": after["pending"],
                "published": after["published"],
                "dependencies": after["dependencies"],
                "processed": after["processed"],
                "stream": after["stream"],
                "consumer": after["consumer"],
                "runtimeOwner": after["role"],
                "changedPasswords": True,
                "defaultNoApplications": True,
                "existingTargetRejected": True,
                "corruptHashRejected": True,
                "nonSuperuserOwner": after["safeRole"],
                "relayPausePreserved": after["relayPaused"],
                "sourceServiceStatesPreserved": True,
            },
        )
    finally:
        failures = []
        # A failed CLI restore can allocate its private project before returning its name.
        # Only consider volumes labelled for this invocation's unique requested target.
        owned = (
            checked(
                [
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=ear.backup.target={requested_target}",
                ]
            )
            .decode()
            .split()
        )
        if owned:
            for volume in json.loads(checked(["docker", "volume", "inspect", *owned])):
                labels = volume["Labels"]
                op = labels.get("ear.backup.operation", "")
                if (
                    re.fullmatch(r"[0-9a-f]{32}", op)
                    and labels.get("com.docker.compose.project") == "ear-restored-" + op
                ):
                    envs["ear-restored-" + op] = envs[requested_target]
        for project in reversed(envs):
            try:
                checked(
                    dc(
                        project,
                        "--profile",
                        "init",
                        "down",
                        "--timeout",
                        "1",
                        "--volumes",
                        "--remove-orphans",
                    )
                )
            except Exception:
                failures.append(project)
        for project in (source, requested_target):
            Path(f"/tmp/ear-storage-backup-{project}.lock").unlink(missing_ok=True)
        for project in envs:
            for prefix in (["ps", "-aq"], ["volume", "ls", "-q"], ["network", "ls", "-q"]):
                if checked(
                    ["docker", *prefix, "--filter", "label=com.docker.compose.project=" + project]
                ).strip():
                    failures.append(project)
        state["cleanupFailures"] = failures
        (work / "state.json").write_text(json.dumps(state, indent=2))
        print("isolated roundtrip evidence: " + json.dumps(state), flush=True)
        assert not failures
