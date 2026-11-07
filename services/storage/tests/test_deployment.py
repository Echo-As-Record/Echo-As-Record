"""Storage slice policy and explicit operator commands; all credentials are synthetic."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest
import yaml
from ear_storage import cli
from ear_storage.deployment import ADMIN, WORKER, init_identities, read_identity_tokens
from ear_storage.health import HealthProbe
from ear_storage.http import create_app_from_dsn
from ear_storage.schema import bootstrap, migrate
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]


def test_compose_policy():
    config = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text())
    services = config["services"]
    assert {k for k, v in services.items() if not v.get("profiles")} == {
        "postgres",
        "nats",
        "storage",
        "relay",
        "manage-worker",
    }
    assert "ports" not in services["postgres"] and "ports" not in services["nats"]
    assert services["storage"]["ports"] == ["127.0.0.1:${EAR_STORAGE_PORT:-8000}:8000"]
    assert config["networks"]["backend"]["internal"] is True
    for name in ("storage", "relay", "manage-worker"):
        service = services[name]
        assert service["user"] == "10001:10001"
        assert service["tmpfs"] == ["/tmp:size=16m,noexec,nosuid"]
        assert not {"migrate", "bootstrap", "init-identities"} & service["depends_on"].keys()
        assert "EAR_STORAGE_DSN" not in service["environment"]
        assert "EAR_NATS_TOKEN" not in service["environment"]
    for name in ("migrate", "bootstrap", "init-identities"):
        assert services[name]["profiles"] == ["init"]
        assert services[name]["restart"] == "no"
    assert services["bootstrap"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert "postgres_data" in config["volumes"] and "nats_data" in config["volumes"]
    assert "authorization { token: $EAR_NATS_TOKEN }" in (ROOT / "deploy/nats.conf").read_text()


def test_build_policy():
    dockerfile = (ROOT / "deploy/Dockerfile").read_text()
    assert "uv sync --locked --package ear-storage --no-dev --no-editable" in dockerfile
    assert "COPY . " not in dockerfile
    for module in ("adapters", "extraction"):
        assert f"services/{module}/pyproject.toml" in dockerfile
    ignores = (ROOT / ".dockerignore").read_text().splitlines()
    assert {"**/.env", "**/.env.*", "**/.git", "**/.venv", "fixtures/", "recordings/"} <= set(
        ignores
    )


def test_liveness_and_generic_readiness(monkeypatch):
    app = create_app_from_dsn("host=unreachable password=DO_NOT_LEAK")
    probe = Mock(return_value=False)
    monkeypatch.setattr(HealthProbe, "ready", probe)
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        probe.assert_not_called()
        response = client.get("/health/ready", headers={"Authorization": "invalid"})
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert response.headers["cache-control"] == "no-store"
        probe.return_value = True
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/v1/identity").status_code == 401


def test_identity_file_policy(tmp_path):
    with pytest.raises(ValueError):
        init_identities("not used", tmp_path / "tokens.txt")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    output = deploy / ".env"
    output.write_text("USER_FIELD=keep\n")
    output.chmod(0o644)
    with pytest.raises(ValueError):
        init_identities("not used", output)
    output.unlink()
    output.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        init_identities("not used", output)
    with pytest.raises(ValueError):
        read_identity_tokens(f"{ADMIN}={'x' * 43}\n")


def test_cli_snapshot_sweep_is_bounded(monkeypatch, capsys):
    import ear_storage.snapshots as snapshots

    sweep = Mock(return_value=3)
    monkeypatch.setattr(snapshots, "sweep_expired", sweep)
    monkeypatch.setenv("EAR_STORAGE_DSN", "synthetic")
    monkeypatch.setattr(sys, "argv", ["ear-storage", "snapshot-sweep", "--batch-size", "7"])
    cli.main()
    sweep.assert_called_once_with("synthetic", batch_size=7)
    assert json.loads(capsys.readouterr().out) == {"deleted": 3}


@pytest.mark.postgres
def test_identities_atomic_idempotent_private(pg_dsn, tmp_path, monkeypatch, capsys):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    output = deploy / ".env"
    output.write_text("USER_FIELD=preserved\n")
    output.chmod(0o600)
    monkeypatch.setenv("EAR_STORAGE_DSN", pg_dsn)
    monkeypatch.setattr(sys, "argv", ["ear-storage", "init-identities", "--output", str(output)])
    cli.main()
    assert capsys.readouterr().out == '{"status": "created"}\n'
    contents = output.read_text()
    tokens = read_identity_tokens(contents)
    assert set(tokens) == {ADMIN, WORKER}
    assert contents.startswith("USER_FIELD=preserved\n")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    inode = output.stat().st_ino
    cli.main()
    assert capsys.readouterr().out == '{"status": "unchanged"}\n'
    assert output.read_text() == contents and output.stat().st_ino == inode
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM storage.principals").fetchone() == (2,)
    output.unlink()
    with pytest.raises(ValueError, match="refusing automatic reissue"):
        init_identities(pg_dsn, output)
    assert not output.exists()


@pytest.mark.postgres
def test_health_bootstrap_revision_and_db_down(pg_dsn):
    probe = HealthProbe(pg_dsn)
    assert not probe.ready()
    migrate(pg_dsn)
    assert not probe.ready()
    bootstrap(pg_dsn)
    assert probe.ready()
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("UPDATE storage.meta SET schema_revision=schema_revision-1")
    assert not probe.ready()
    # The probe neither migrates nor bootstraps an old/missing database.
    with psycopg.connect(pg_dsn) as conn:
        from ear_storage.schema import SCHEMA_VERSION

        assert conn.execute("SELECT schema_revision FROM storage.meta").fetchone() == (
            SCHEMA_VERSION - 1,
        )
    assert not HealthProbe("host=127.0.0.1 port=1 dbname=absent connect_timeout=1").ready()


@pytest.mark.postgres
@pytest.mark.nats
def test_health_requires_configured_broker(pg_dsn, nats_server):
    from ear_storage.relay import RelayConfig

    migrate(pg_dsn)
    bootstrap(pg_dsn)
    relay = RelayConfig(servers=(nats_server.url,), token=nats_server.token)
    # A TCP listener is not enough: an absent stream is not ready.
    assert not HealthProbe(pg_dsn, relay).ready()
    assert HealthProbe(pg_dsn).ready()  # HTTP-only readiness without NATS config
    wrong = RelayConfig(servers=(nats_server.url,), token="wrong-test-token")
    assert not HealthProbe(pg_dsn, wrong).ready()


def test_worker_health_is_recent_success_not_process_online(monkeypatch):
    import time
    import urllib.error
    import urllib.request

    from ear_storage.health import WorkerHealth

    with WorkerHealth() as health:
        url = "http://127.0.0.1:" + str(health.server.server_port)
        with urllib.request.urlopen(url + "/health/live", timeout=2) as response:
            assert response.status == 200
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url + "/health/ready", timeout=2)
        assert error.value.code == 503
        health.record_poll(True)
        with urllib.request.urlopen(url + "/health/ready", timeout=2) as response:
            assert response.status == 200
        health.last_success = time.monotonic() - 6
        assert not health.ready()
        health.record_poll(True)
        health.record_poll(False)
        assert not health.ready()
    assert not health.ready()


def test_cli_failure_frames_never_include_exception_message(monkeypatch, capsys):
    def fail(_dsn):
        raise ValueError("password=DO_NOT_LEAK")

    monkeypatch.setattr(cli, "bootstrap", fail)
    monkeypatch.setenv("EAR_STORAGE_DSN", "password=DO_NOT_LEAK")
    monkeypatch.setattr(sys, "argv", ["ear-storage", "bootstrap"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "DO_NOT_LEAK" not in output.out + output.err
    payload = json.loads(output.out)
    assert payload["error"] == "ValueError"
    assert all(set(frame) == {"file", "line"} for frame in payload["frames"])
