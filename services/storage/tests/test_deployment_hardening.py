"""Additive deployment-only regressions; synthetic tokens and isolated databases."""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import psycopg
import pytest
from ear_storage import deployment as dep
from ear_storage.auth import token_digest
from ear_storage.schema import bootstrap, migrate, writer


def snapshot(dsn):
    with psycopg.connect(dsn) as conn:
        return (
            conn.execute("SELECT * FROM storage.principals ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM storage.type_grants ORDER BY 1,2").fetchall(),
        )


@pytest.fixture
def initialized(pg_dsn, tmp_path):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    directory = tmp_path / "deploy"
    directory.mkdir()
    output = directory / ".env"
    dep.init_identities(pg_dsn, output)
    return pg_dsn, output


@pytest.mark.postgres
def test_initial_exact_grants(initialized):
    dsn, _ = initialized
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT p.module,g.data_type,g.can_subscribe FROM storage.principals p "
            "JOIN storage.type_grants g ON p.id=g.principal_id ORDER BY 1,2"
        ).fetchall()
    assert rows == [
        ("api", dep.FEEDBACK, False),
        ("api", dep.RESULT, False),
        ("storage", dep.FEEDBACK, True),
    ]


@pytest.mark.postgres
@pytest.mark.parametrize("module", ["api", "storage"])
@pytest.mark.parametrize(
    "change", ["extra", "missing", "subscription", "role", "active", "hash", "module"]
)
def test_reentry_rejects_drift_without_mutation(initialized, module, change):
    dsn, output = initialized
    original = output.read_bytes()
    with psycopg.connect(dsn) as conn:
        # Normalize old implementation's overbroad worker grant for independent red tests.
        conn.execute(
            "DELETE FROM storage.type_grants WHERE data_type=%s AND principal_id IN "
            "(SELECT id FROM storage.principals WHERE module='storage')",
            (dep.RESULT,),
        )
        row = conn.execute(
            "SELECT id FROM storage.principals WHERE module=%s", (module,)
        ).fetchone()
        assert row is not None
        pid = row[0]
        if change == "extra":
            conn.execute(
                "INSERT INTO storage.type_grants SELECT %s,name,false FROM storage.registry "
                "WHERE name NOT IN (%s,%s) ORDER BY name LIMIT 1",
                (pid, dep.FEEDBACK, dep.RESULT),
            )
        elif change == "missing":
            conn.execute(
                "DELETE FROM storage.type_grants WHERE principal_id=%s AND data_type=%s",
                (pid, dep.FEEDBACK),
            )
        elif change == "subscription":
            conn.execute(
                "UPDATE storage.type_grants SET can_subscribe=NOT can_subscribe "
                "WHERE principal_id=%s",
                (pid,),
            )
        elif change == "role":
            conn.execute("UPDATE storage.principals SET admin=NOT admin WHERE id=%s", (pid,))
        elif change == "active":
            conn.execute("UPDATE storage.principals SET active=false WHERE id=%s", (pid,))
        elif change == "hash":
            conn.execute(
                "UPDATE storage.principals SET token_hash=%s WHERE id=%s",
                (token_digest("z" * 43), pid),
            )
        else:
            conn.execute(
                "UPDATE storage.principals SET module=%s WHERE id=%s",
                ("storage" if module == "api" else "api", pid),
            )
    before = snapshot(dsn)
    with pytest.raises(ValueError, match=r"configuration.*operator recovery") as error:
        dep.init_identities(dsn, output)
    assert all(
        token not in str(error.value)
        for token in dep.read_identity_tokens(original.decode()).values()
    )
    assert snapshot(dsn) == before
    assert output.read_bytes() == original


@pytest.mark.parametrize("which", ["ancestor", "parent", "file", "dotdot"])
def test_symlink_and_dotdot_rejected_before_database(tmp_path, monkeypatch, which):
    real = tmp_path / "real"
    (real / "deploy").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    if which == "ancestor":
        output = alias / "deploy" / ".env"
    elif which == "parent":
        output = tmp_path / "deploy" / ".env"
        output.parent.symlink_to(real / "deploy", target_is_directory=True)
    elif which == "file":
        output = real / "deploy" / ".env"
        output.symlink_to(tmp_path / "absent")
    else:
        output = real / ".." / "real" / "deploy" / ".env"
    connect = Mock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(dep, "connection", connect)
    with pytest.raises(ValueError):
        dep.init_identities("unused", output)
    connect.assert_not_called()
    assert not (tmp_path / "absent").exists()


@pytest.mark.postgres
def test_parent_replaced_after_lock_never_publishes_into_replacement(pg_dsn, tmp_path, monkeypatch):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    a, b, moved = (tmp_path / name for name in ("A", "B", "original"))
    (a / "deploy").mkdir(parents=True)
    (b / "deploy").mkdir(parents=True)
    lock = fcntl.flock

    def attack(fd, operation):
        lock(fd, operation)
        a.rename(moved)
        a.symlink_to(b, target_is_directory=True)

    monkeypatch.setattr(fcntl, "flock", attack)
    try:
        dep.init_identities(pg_dsn, a / "deploy" / ".env")
    except ValueError as error:
        assert "operator recovery" in str(error)
    assert not (b / "deploy" / ".env").exists()
    assert not list((b / "deploy").iterdir())
    if (moved / "deploy" / ".env").exists():
        assert stat.S_IMODE((moved / "deploy" / ".env").stat().st_mode) == 0o600


@pytest.mark.postgres
def test_relative_concurrent_initializers_only_issue_once(pg_dsn, tmp_path, monkeypatch):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    (tmp_path / "deploy").mkdir()
    monkeypatch.chdir(tmp_path)
    gate = Barrier(2)
    issue = Mock(wraps=secrets.token_urlsafe)
    monkeypatch.setattr(secrets, "token_urlsafe", issue)

    def start():
        gate.wait(timeout=5)
        return dep.init_identities(pg_dsn, Path("deploy/.env"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start) for _ in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == [False, True]
    assert issue.call_count == 2
    output = Path("deploy/.env")
    original = output.read_bytes()
    inode = output.stat().st_ino
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not dep.init_identities(pg_dsn, output)
    assert output.read_bytes() == original and output.stat().st_ino == inode
    assert len(snapshot(pg_dsn)[0]) == 2


@pytest.mark.postgres
def test_post_commit_file_loss_requires_operator_recovery(pg_dsn, tmp_path, monkeypatch):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    (tmp_path / "deploy").mkdir()
    output = tmp_path / "deploy" / ".env"
    real_writer = writer

    @contextmanager
    def lost_after_commit(conn):
        with real_writer(conn):
            yield
        output.unlink()
        raise OSError("synthetic post-commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(dep, "writer", lost_after_commit)
        with pytest.raises(ValueError, match="operator recovery"):
            dep.init_identities(pg_dsn, output)
    before = snapshot(pg_dsn)
    assert len(before[0]) == 2
    with pytest.raises(ValueError, match="operator recovery"):
        dep.init_identities(pg_dsn, output)
    assert snapshot(pg_dsn) == before and not output.exists()


@pytest.mark.postgres
def test_target_appears_during_exclusive_publication(initialized, tmp_path, monkeypatch):
    dsn, output = initialized
    # Use a fresh database state, without changing any real deployment credentials.
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM storage.type_grants")
        conn.execute("DELETE FROM storage.principals")
    output.unlink()
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    link = os.link

    def attack(src, dst, **kwargs):
        output.symlink_to(victim)
        return link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", attack)
    with pytest.raises(ValueError, match="operator recovery"):
        dep.init_identities(dsn, output)
    assert victim.read_text() == "untouched"
    assert output.is_symlink()
    assert snapshot(dsn) == ([], [])


@pytest.mark.postgres
def test_temporary_permissions_without_process_umask(pg_dsn, tmp_path, monkeypatch):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    (tmp_path / "deploy").mkdir()
    output = tmp_path / "deploy" / ".env"
    real_open = os.open

    def restrictive_open(path, flags, mode=0o777, *, dir_fd=None):
        # Simulate an arbitrarily restrictive umask on the temporary file only.
        return real_open(path, flags, 0 if flags & os.O_CREAT else mode, dir_fd=dir_fd)

    def forbidden_umask(_mask):
        raise AssertionError("process-global umask must not be changed")

    monkeypatch.setattr(os, "open", restrictive_open)
    monkeypatch.setattr(os, "umask", forbidden_umask)
    assert dep.init_identities(pg_dsn, output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert set(dep.read_identity_tokens(output.read_text())) == {dep.ADMIN, dep.WORKER}
