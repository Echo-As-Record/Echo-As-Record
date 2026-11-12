"""Synthetic filesystem attacks; no private inputs or external services."""

import os
import sqlite3
from unittest.mock import patch

import pytest
from ear_contracts import new_id
from ear_dependency.journal import CommitJournal
from ear_storage_client import Context, MarkProcessed


def prepare(journal):
    event = new_id()
    return event, journal.prepare(event, [MarkProcessed("dependency", event)], Context.system())


@pytest.mark.parametrize("shared", [True, False], ids=["0777-parent", "connect-swap"])
def test_connect_directory_swap_never_writes_outside(tmp_path, shared):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    if shared:
        parent.chmod(0o777)
    directory = parent / "journal"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    connect = sqlite3.connect

    def swap(path, *args, **kwargs):
        directory.rename(parent / "detached")
        directory.symlink_to(outside, target_is_directory=True)
        return connect(path, *args, **kwargs)

    with (
        patch("ear_dependency.journal.sqlite3.connect", swap),
        pytest.raises((ValueError, OSError, sqlite3.Error)),
    ):
        journal = CommitJournal(directory, namespace="synthetic")
        try:
            prepare(journal)
        finally:
            journal.close()
    assert not list(outside.iterdir())


@pytest.mark.parametrize("ancestor", [True, False])
def test_directory_symlinks_rejected(tmp_path, ancestor):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        CommitJournal(alias / "journal" if ancestor else alias, namespace="synthetic")
    assert not list(target.iterdir())


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o1777])
def test_untrusted_ancestor_rejected_before_creation(tmp_path, mode):
    parent = tmp_path / "parent"
    parent.mkdir(mode=mode)
    parent.chmod(mode)
    # Only a root-owned sticky ancestor (e.g. system /tmp) is trusted.
    if os.geteuid() == 0 and mode == 0o1777:
        pytest.skip("root-owned sticky directories are intentionally allowed")
    with pytest.raises(ValueError, match="ancestor"):
        CommitJournal(parent / "not-created" / "journal", namespace="synthetic")
    assert not list(parent.iterdir())
    assert parent.stat().st_mode & 0o7777 == mode


FILES = [
    "dependency.lock",
    "dependency.sqlite3",
    "dependency.sqlite3-journal",
    "dependency.sqlite3-wal",
    "dependency.sqlite3-shm",
]


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "fifo", "directory"])
def test_unsafe_files_rejected_without_sqlite_or_repair(tmp_path, name, kind):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not change")
    outside.chmod(0o600)
    path = directory / name
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "public":
        path.write_bytes(b"do not change")
        path.chmod(0o644)
    elif kind == "fifo":
        os.mkfifo(path, mode=0o600)
    else:
        path.mkdir(mode=0o700)
    before = path.lstat()
    with patch("ear_dependency.journal.sqlite3.connect") as connect:
        with pytest.raises((ValueError, OSError)):
            CommitJournal(directory, namespace="synthetic")
        connect.assert_not_called()
    after = path.lstat()
    assert (before.st_ino, before.st_mode, before.st_nlink) == (
        after.st_ino,
        after.st_mode,
        after.st_nlink,
    )
    assert outside.read_bytes() == b"do not change"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o1700])
def test_private_final_directory_required_without_repair(tmp_path, mode):
    path = tmp_path / "journal"
    path.mkdir()
    path.chmod(mode)
    with pytest.raises(ValueError, match="private"):
        CommitJournal(path, namespace="synthetic")
    assert path.stat().st_mode & 0o7777 == mode
    assert not list(path.iterdir())


def test_wrong_owners_rejected():
    from ear_dependency.secure_files import _trusted_directory, private_file

    uid = os.geteuid() + 10001
    for mode, check in (
        (0o100600, private_file),
        (0o40700, lambda info: _trusted_directory(info, final=True)),
        (0o40755, lambda info: _trusted_directory(info, final=False)),
    ):
        with pytest.raises(ValueError):
            check(os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0)))


def test_worker_lock_replacement_cannot_bypass_database_lock(tmp_path):
    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    lock = directory / "dependency.lock"
    saved = directory / "old.lock"
    lock.rename(saved)
    try:
        with pytest.raises(BlockingIOError):
            CommitJournal(directory, namespace="synthetic")
        lock.unlink(missing_ok=True)
        saved.rename(lock)
        prepare(journal)
    finally:
        journal.close()


def test_legacy_worker_lock_still_excludes(tmp_path):
    import fcntl

    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    fd = os.open(directory / "dependency.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            CommitJournal(directory, namespace="synthetic")
        assert not (directory / "dependency.sqlite3").exists()
    finally:
        os.close(fd)


def test_reopen_namespace_private_modes_and_relative_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from pathlib import Path

    directory = Path("private") / "nested"
    journal = CommitJournal(directory, namespace="synthetic")
    event, item = prepare(journal)
    assert journal.db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert journal.db.execute("PRAGMA synchronous").fetchone() == (2,)
    assert directory.stat().st_mode & 0o7777 == 0o700
    for name in ("dependency.lock", "dependency.sqlite3"):
        assert (directory / name).stat().st_mode & 0o7777 == 0o600
    journal.close()
    journal.close()
    with pytest.raises(ValueError, match="namespace"):
        CommitJournal(directory, namespace="different")
    journal = CommitJournal(directory, namespace="synthetic")
    try:
        assert journal.pending(event) == item
        assert journal.pending_ids() == [event]
        assert journal.prepare(event, [], Context.system()) == item
        journal.complete(event)
        assert journal.pending(event) is None
    finally:
        journal.close()
    with pytest.raises(ValueError):
        journal.pending(event)
    with pytest.raises(ValueError):
        CommitJournal(directory / ".." / "other", namespace="synthetic")


@pytest.mark.parametrize("kind", ["directory", "database", "worker", "permissions", "ancestor"])
@pytest.mark.parametrize("method", ["pending", "pending_ids", "prepare", "complete"])
def test_replacement_between_transactions_fails_closed(tmp_path, kind, method):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    directory = parent / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    event, _ = prepare(journal)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    if kind == "directory":
        directory.rename(parent / "detached")
        directory.symlink_to(outside, target_is_directory=True)
    elif kind in {"database", "worker"}:
        name = "dependency.sqlite3" if kind == "database" else "dependency.lock"
        path = directory / name
        path.rename(directory / "old-file")
        path.touch(mode=0o600)
    elif kind == "permissions":
        (directory / "dependency.sqlite3").chmod(0o644)
    else:
        parent.chmod(0o777)
    with pytest.raises((ValueError, OSError)):
        if method == "prepare":
            prepare(journal)
        elif method == "pending_ids":
            journal.pending_ids()
        else:
            getattr(journal, method)(event)
    assert not list(outside.iterdir())
    with pytest.raises(sqlite3.ProgrammingError):
        journal.db.execute("SELECT 1")
    assert journal._files.fds == {}
    assert journal._files.directory is None
    journal.close()


@pytest.mark.parametrize("kind", ["directory", "database", "sidecar", "sidecar-unlink"])
def test_change_during_transaction_blocks_commit(tmp_path, kind):
    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    event, item = prepare(journal)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    with pytest.raises((ValueError, OSError)), journal._transaction():
        journal.db.execute("DELETE FROM pending WHERE event_id=?", (event,))
        journal.validate()  # bind the live rollback sidecar
        if kind == "directory":
            directory.rename(tmp_path / "detached")
            directory.symlink_to(outside, target_is_directory=True)
        elif kind == "database":
            path = directory / "dependency.sqlite3"
            path.rename(directory / "old.sqlite3")
            path.touch(mode=0o600)
        else:
            path = directory / "dependency.sqlite3-journal"
            path.rename(directory / "old-journal")
            if kind == "sidecar":
                path.touch(mode=0o600)
    assert journal._closed
    assert not list(outside.iterdir())
    if kind == "directory":
        directory.unlink()
        (tmp_path / "detached").rename(directory)
        reopened = CommitJournal(directory, namespace="synthetic")
        try:
            assert reopened.pending(event) == item
        finally:
            reopened.close()


@pytest.mark.parametrize("name", FILES[2:])
def test_sidecar_injected_before_transaction_not_followed(tmp_path, name):
    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    (directory / name).symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        prepare(journal)
    assert outside.read_bytes() == b"untouched"
    assert journal._closed


@pytest.mark.parametrize("failure", ["connect", "namespace", "schema", "missing-db"])
def test_initialization_failures_release_fds_connections_and_locks(tmp_path, failure):
    from pathlib import Path

    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    journal.close()
    if failure == "schema":
        with sqlite3.connect(directory / "dependency.sqlite3") as conn:
            conn.execute("DROP TABLE pending")
            conn.execute("CREATE VIEW pending AS SELECT 1")
            conn.execute("DROP TABLE identity")
            conn.execute("CREATE TABLE identity (wrong_column TEXT)")
    before = len(list(Path("/proc/self/fd").iterdir()))
    connections = []
    connect = sqlite3.connect

    def connect_or_fail(path, *args, **kwargs):
        if failure == "connect":
            raise sqlite3.OperationalError("synthetic connect failure")
        if failure == "missing-db":
            (directory / "dependency.sqlite3").rename(directory / "saved.sqlite3")
        conn = connect(path, *args, **kwargs)
        connections.append(conn)
        return conn

    with (
        patch("ear_dependency.journal.sqlite3.connect", connect_or_fail),
        pytest.raises((ValueError, OSError, sqlite3.Error)),
    ):
        CommitJournal(directory, namespace="different" if failure == "namespace" else "synthetic")
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    if failure == "missing-db":
        assert not (directory / "dependency.sqlite3").exists()
        (directory / "saved.sqlite3").rename(directory / "dependency.sqlite3")
    if failure != "schema":
        CommitJournal(directory, namespace="synthetic").close()


@pytest.mark.parametrize("crash", [False, True], ids=["wal-header", "uncheckpointed-wal"])
def test_wal_rejected_without_losing_state(tmp_path, crash):
    import subprocess
    import sys

    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    event, item = prepare(journal)
    journal.close()
    path = directory / "dependency.sqlite3"
    script = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA wal_autocheckpoint=0')
conn.execute('CREATE TABLE wal_evidence (body TEXT)')
conn.execute("INSERT INTO wal_evidence VALUES ('must survive')")
conn.commit()
if sys.argv[2] == 'crash':
    os._exit(0)
conn.close()
"""
    subprocess.run(
        [sys.executable, "-c", script, str(path), "crash" if crash else "close"], check=True
    )
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    if crash:
        assert before["dependency.sqlite3-wal"]
    with patch("ear_dependency.journal.sqlite3.connect") as connect:
        with pytest.raises(ValueError, match="WAL"):
            CommitJournal(directory, namespace="synthetic")
        connect.assert_not_called()
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    # Independent SQLite inspection only AFTER proving rejection preserved all files.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT body FROM wal_evidence").fetchone() == ("must survive",)
        assert (
            conn.execute(
                "SELECT request_id, body FROM pending WHERE event_id=?", (event,)
            ).fetchone()
            == item
        )
    finally:
        conn.close()


def test_hot_rollback_recovery_preserves_pending(tmp_path):
    import subprocess
    import sys

    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    event, item = prepare(journal)
    journal.close()
    script = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA cache_size=1')
conn.execute('BEGIN IMMEDIATE')
conn.execute('DELETE FROM pending')
conn.execute('CREATE TABLE uncommitted (body BLOB)')
for _ in range(100):
    conn.execute('INSERT INTO uncommitted VALUES (zeroblob(4096))')
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(directory / "dependency.sqlite3")], check=True
    )
    assert (directory / "dependency.sqlite3-journal").exists()
    journal = CommitJournal(directory, namespace="synthetic")
    try:
        assert journal.pending(event) == item
        assert journal.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not journal.db.execute(
            "SELECT name FROM sqlite_master WHERE name='uncommitted'"
        ).fetchall()
        prepare(journal)
    finally:
        journal.close()


def test_another_process_and_fork_cannot_use_live_journal(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    directory = tmp_path / "journal"
    journal = CommitJournal(directory, namespace="synthetic")
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from ear_dependency import CommitJournal; "
                "CommitJournal(Path(sys.argv[1]), namespace='synthetic')",
                str(directory),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert child.returncode != 0 and "BlockingIOError" in child.stderr
        result_path = tmp_path / "fork-result.txt"
        pid = os.fork()
        if pid == 0:
            try:
                journal.pending_ids()
            except ValueError:
                Path(result_path).write_text("fork-refused")
                os._exit(0)
            os._exit(2)
        _, status = os.waitpid(pid, 0)
        assert status == 0 and result_path.read_text() == "fork-refused"
        prepare(journal)  # child closing inherited descriptors did not unlock parent
    finally:
        journal.close()
