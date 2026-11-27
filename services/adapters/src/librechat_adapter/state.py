"""增量水位与单实例锁（放在 Ear 私有文件卷里，不新增任何数据库或服务）。

水位只是性能优化：丢了或损坏就退回全量导出，而全量导出对 ① 是幂等的（确定性 id、修订探测）。
文件原子替换；独占 flock 防止两个适配器同时读同一个来源、同时推进水位。
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from librechat_adapter.volume import SUBDIR, VolumeError, _check_dir

STATE_DIR = "state"


class AlreadyRunning(Exception):
    """另一个适配器进程正在处理同一个 LibreChat 实例。"""


@dataclass(frozen=True, slots=True)
class State:
    watermark: datetime | None = None
    last_full_at: datetime | None = None


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _format(value: datetime | None) -> str | None:
    return (
        None if value is None else value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class RunState:
    def __init__(self, root: Path, device: str, *, dir_mode: int = 0o750) -> None:
        self.directory = root / SUBDIR / STATE_DIR
        _check_dir(root / SUBDIR, dir_mode)
        _check_dir(self.directory, 0o700)
        self.path = self.directory / f"{device}.json"
        self.lock_path = self.directory / f"{device}.lock"

    @contextmanager
    def lock(self) -> Iterator[None]:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AlreadyRunning from None
            yield
        finally:
            os.close(fd)

    def load(self) -> State:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return State()
        if not isinstance(data, dict) or data.get("version") != 1:
            return State()
        return State(_parse(data.get("watermark")), _parse(data.get("last_full_at")))

    def save(self, state: State) -> None:
        body = json.dumps(
            {
                "version": 1,
                "watermark": _format(state.watermark),
                "last_full_at": _format(state.last_full_at),
            }
        ).encode("utf-8")
        temp = self.path.with_suffix(".tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise VolumeError("无法写入水位文件") from None
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.path)
