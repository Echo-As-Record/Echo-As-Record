"""Private operator-seeded model selection on a local Linux filesystem.

The API is the writer; the gateway reads the JSON snapshot at startup. Mount the
private directory, not the file inode. Do not replace the directory while serving.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ear_api.models import load_json_object

_MAX_BYTES = 4096
# The API's bounded JSON decoder admits integers with at most 63 bits.
_MAX_REVISION = (1 << 63) - 1
_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$")
    revision: int = Field(gt=0, le=_MAX_REVISION)

    @field_validator("model")
    @classmethod
    def model_id_not_url(cls, value: str) -> str:
        # Namespace/model and model:variant are valid IDs, not provider addresses.
        if "://" in value:
            raise ValueError("model identifier required")
        return value


class ModelSettingsUnavailable(RuntimeError):
    pass


class ModelSettingsConflict(RuntimeError):
    pass


class ModelSettings:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _locked(self, *, write: bool) -> Iterator[int]:
        try:
            if not self.path.is_absolute() or ".." in self.path.parts or not self.path.name:
                raise ValueError
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | _FLAGS)
            try:
                parent = os.fstat(directory)
                if (
                    not stat.S_ISDIR(parent.st_mode)
                    or stat.S_IMODE(parent.st_mode) != 0o700
                    or parent.st_uid != os.geteuid()
                ):
                    raise ValueError
                # The parent inode is stable across replacements of settings.json.
                # Each request opens its own descriptor, so threads and processes
                # serialize on the same inode; closing it releases the lock.
                operation = fcntl.LOCK_EX if write else fcntl.LOCK_SH
                deadline = time.monotonic() + 1
                while True:
                    try:
                        fcntl.flock(directory, operation | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
                yield directory
            finally:
                os.close(directory)
        except (OSError, ValueError):
            raise ModelSettingsUnavailable from None

    def _read(self, directory: int) -> ModelSelection:
        fd = os.open(self.path.name, os.O_RDONLY | _FLAGS, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_size > _MAX_BYTES
            ):
                raise ValueError
            data = handle.read(_MAX_BYTES + 1)
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError
        return ModelSelection.model_validate(
            load_json_object(data.decode("utf-8"), max_bytes=_MAX_BYTES)
        )

    def read(self) -> ModelSelection:
        with self._locked(write=False) as directory:
            return self._read(directory)

    def update(self, desired: ModelSelection) -> ModelSelection:
        with self._locked(write=True) as directory:
            current = self._read(directory)
            if current.revision != desired.revision:
                raise ModelSettingsConflict
            updated = ModelSelection(model=desired.model, revision=current.revision + 1)
            temporary = ".model-settings-" + secrets.token_hex(16) + ".tmp"
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS, 0o600, dir_fd=directory
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    os.fchmod(fd, 0o600)
                    handle.write(updated.model_dump_json().encode("ascii") + b"\n")
                    handle.flush()
                    os.fsync(fd)
                os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                # If durability is uncertain after rename, return unavailable and
                # require a fresh read; do not report success or roll back in place.
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
            return updated
