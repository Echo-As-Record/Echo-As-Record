"""导出流的来源：本地文件（已导出的 NDJSON）或受信的导出命令。

命令由操作员在私有配置里给出 argv（不经 shell），请求 JSON 经 stdin 传入，所以截止时间等参数
不会拼进命令行；子进程只继承最小环境（不含存储令牌）。典型用法是 `ssh <host> <导出脚本>`，
见 tools/librechat_export_docker.sh。进程超时、非零退出和 stderr 都不会把数据写进日志。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Literal, Protocol

from librechat_adapter.config import REQUEST_FORMAT
from librechat_adapter.wire import ProtocolError, StreamError

_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "XDG_RUNTIME_DIR",
)
_STDERR_KEEP = 2048


class SourceUnavailable(StreamError):
    """导出命令启动失败、超时或非零退出；下轮重试。消息固定。"""


@dataclass(frozen=True, slots=True)
class ExportRequest:
    mode: Literal["full", "incremental"]
    since: datetime | None = None

    def __post_init__(self) -> None:
        if (self.mode == "incremental") != (self.since is not None):
            raise ValueError("增量导出必须给出 since，全量导出不得给出")

    def to_json(self) -> str:
        since = None
        if self.since is not None:
            since = self.since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return json.dumps({"format": REQUEST_FORMAT, "mode": self.mode, "since": since})


class ExportSource(Protocol):
    def open(self, request: ExportRequest) -> AbstractContextManager[Iterator[str]]: ...


def read_lines(stream: IO[bytes], limit: int) -> Iterator[str]:
    """逐行读取，单行上限 `limit` 字节；超长或非 UTF-8 都是协议错误。"""
    while True:
        raw = stream.readline(limit + 1)
        if not raw:
            return
        if len(raw) > limit:
            raise ProtocolError("导出流的单行超过上限")
        try:
            yield raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("导出流不是合法的 UTF-8") from None


class FileSource:
    """读取一个已导出的 NDJSON 文件；忽略 request（文件内容就是那一次导出的结果）。"""

    def __init__(self, path: Path, *, max_line_bytes: int) -> None:
        self.path = path
        self.max_line_bytes = max_line_bytes

    @contextmanager
    def open(self, request: ExportRequest) -> Iterator[Iterator[str]]:
        try:
            handle = open(self.path, "rb")  # noqa: SIM115 - closed in finally below
        except OSError:
            raise SourceUnavailable("无法读取导出文件") from None
        try:
            yield read_lines(handle, self.max_line_bytes)
        finally:
            handle.close()


class CommandSource:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        max_line_bytes: int,
        timeout: float = 3600.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError("导出命令必须是非空字符串数组")
        self.argv = tuple(argv)
        self.max_line_bytes = max_line_bytes
        self.timeout = timeout
        source = os.environ if env is None else env
        self.env = {k: source[k] for k in _ENV_ALLOWLIST if k in source}
        self.stderr_tail = b""
        self.returncode: int | None = None

    @contextmanager
    def open(self, request: ExportRequest) -> Iterator[Iterator[str]]:
        self.stderr_tail = b""
        self.returncode = None
        try:
            proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                close_fds=True,
                shell=False,
            )
        except OSError:
            raise SourceUnavailable("无法启动导出命令") from None
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        timer = threading.Timer(self.timeout, proc.kill)
        timer.daemon = True
        timer.start()
        drain = threading.Thread(target=self._drain, args=(proc.stderr,), daemon=True)
        drain.start()
        try:
            try:
                proc.stdin.write(request.to_json().encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass  # 进程已退出；退出码在下面核对
            yield read_lines(proc.stdout, self.max_line_bytes)
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            drain.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                stream.close()
            self.returncode = proc.returncode
        if self.returncode != 0:
            raise SourceUnavailable("导出命令以非零状态退出")

    def _drain(self, stream: IO[bytes]) -> None:
        kept = bytearray()
        for chunk in iter(lambda: stream.read(1024), b""):
            if len(kept) < _STDERR_KEEP:
                kept += chunk[: _STDERR_KEEP - len(kept)]
        self.stderr_tail = bytes(kept)
