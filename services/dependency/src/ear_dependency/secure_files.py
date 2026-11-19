"""Private Linux journal: trusted ancestry, pinned directory and inode locks.

Adapted from the reviewed hardened file primitives.

Not a sandbox against root or arbitrary races by malicious processes of our UID.
Never chmod existing files, change umask, or delete recovery evidence. WAL is
explicitly unsupported: reject before SQLite can checkpoint or open sidecars.
"""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import suppress
from pathlib import Path

DB_NAME = "dependency.sqlite3"
LOCK_NAME = "dependency.lock"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("journal file must be private, regular, owned and singly linked (0600)")


def _trusted_directory(info: os.stat_result, *, final: bool) -> None:
    if final:
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("journal directory must be private and owned (0700)")
    elif info.st_uid not in {0, os.geteuid()} or (
        info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
    ):
        raise ValueError("untrusted journal ancestor")


def _directory(path: Path, *, create: bool) -> tuple[int, tuple[tuple[int, int], ...]]:
    fd = os.open("/", _DIRECTORY_FLAGS)
    chain = []
    try:
        _trusted_directory(os.fstat(fd), final=path == Path("/"))
        chain.append(identity(os.fstat(fd)))
        for index, part in enumerate(path.parts[1:], start=1):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            _trusted_directory(info, final=index == len(path.parts) - 1)
            chain.append(identity(info))
        return fd, tuple(chain)
    except BaseException:
        os.close(fd)
        raise


class JournalFiles:
    def __init__(self, path: Path) -> None:
        if ".." in path.parts:
            raise ValueError("unsafe journal path")
        self.path = path if path.is_absolute() else Path.cwd() / path
        self.directory: int | None = None
        self.fds: dict[str, int] = {}
        self._sidecars: dict[str, tuple[int, int]] = {}
        self._pid = os.getpid()
        try:
            self.directory, self._chain = _directory(self.path, create=True)
            fcntl.flock(self.directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Retain the worker lock for exclusion with old workers, but also lock
            # the actual DB. Replacing/aliasing worker.lock cannot bypass DB flock.
            for name in (LOCK_NAME, DB_NAME):
                fd = os.open(
                    name, os.O_CREAT | os.O_RDWR | _FILE_FLAGS, 0o600, dir_fd=self.directory
                )
                self.fds[name] = fd
                private_file(os.fstat(fd))
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.validate()
        except BaseException:
            self.close()
            raise

    @property
    def uri(self) -> str:
        # rw (not rwc): SQLite must not create a replacement if the DB vanishes.
        return f"file:/proc/self/fd/{self.directory}/{DB_NAME}?mode=rw"

    def validate(self, *, journal_removed: bool = False) -> None:
        if self.directory is None or self._pid != os.getpid():
            raise ValueError("journal closed or inherited across fork")
        current, chain = _directory(self.path, create=False)
        try:
            _trusted_directory(os.fstat(self.directory), final=True)
            if chain != self._chain or identity(os.fstat(current)) != self._chain[-1]:
                raise ValueError("journal directory identity changed")
        finally:
            os.close(current)
        for name, fd in self.fds.items():
            opened = os.fstat(fd)
            private_file(opened)
            named = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
            private_file(named)
            if identity(opened) != identity(named):
                raise ValueError("journal file identity changed")
        for suffix in ("-journal", "-wal", "-shm"):
            name = DB_NAME + suffix
            try:
                fd = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=self.directory)
            except FileNotFoundError:
                if suffix in self._sidecars and not journal_removed:
                    raise ValueError("journal sidecar missing") from None
                self._sidecars.pop(suffix, None)
                continue
            try:
                info = os.fstat(fd)
                private_file(info)
                named = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                private_file(named)
                if identity(info) != identity(named) or self._sidecars.get(
                    suffix, identity(info)
                ) != identity(info):
                    raise ValueError("journal sidecar identity changed")
                if suffix != "-journal":
                    raise ValueError(
                        "WAL journal unsupported; preserve files for explicit recovery"
                    )
                self._sidecars[suffix] = identity(info)
            finally:
                os.close(fd)
        header = os.pread(self.fds[DB_NAME], 20, 0)
        if header[:16] == b"SQLite format 3\x00" and 2 in header[18:20]:
            raise ValueError("WAL journal unsupported; preserve files for explicit recovery")

    def close(self) -> None:
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None
