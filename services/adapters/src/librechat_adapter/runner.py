"""一轮导入：决定全量/增量 → 读取导出流 → 写入 → 只在整轮完整成功后推进水位。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ear_storage_client import Query, StorageClient
from ear_storage_client.remote_errors import ServiceUnavailable

from librechat_adapter.engine import (
    ExportFailed,
    Ingestor,
    IngestReport,
    RetryLater,
    SourceVersionError,
    StorageRejected,
    StreamIncomplete,
)
from librechat_adapter.registry import CHAT_TYPES
from librechat_adapter.sources import ExportRequest, ExportSource, SourceUnavailable
from librechat_adapter.state import AlreadyRunning, RunState, State
from librechat_adapter.volume import VolumeError
from librechat_adapter.wire import ProtocolError

EXIT_OK = 0
EXIT_RETRY = 1
EXIT_CONFIG = 2
EXIT_VERSION = 3


def preflight(storage: StorageClient) -> None:
    """显式验证三类聊天数据的读权限与登记；不能用“按 id 读不到”冒充没有数据。"""
    for data_type in CHAT_TYPES:
        storage.query(Query(data_type, limit=1))


@dataclass(slots=True)
class RunResult:
    mode: str | None
    status: str
    exit_code: int
    report: IngestReport | None = None
    advanced: bool = False

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "mode": self.mode,
            "watermark_advanced": self.advanced,
        }
        if self.report is not None:
            out["report"] = self.report.summary()
        return out


class Runner:
    def __init__(
        self,
        *,
        source: ExportSource,
        ingestor: Ingestor,
        state: RunState | None = None,
        full_every: timedelta | None = timedelta(hours=168),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.source = source
        self.ingestor = ingestor
        self.state = state
        self.full_every = full_every
        self.clock = clock

    def run_once(self, *, force_full: bool = False) -> RunResult:
        try:
            with self.state.lock() if self.state is not None else nullcontext():
                return self._run(force_full)
        except AlreadyRunning:
            return RunResult(None, "busy", EXIT_RETRY)

    def _plan(self, force_full: bool, state: State, now: datetime) -> ExportRequest:
        due = self.full_every is not None and (
            state.last_full_at is None or now - state.last_full_at >= self.full_every
        )
        if force_full or state.watermark is None or due:
            return ExportRequest("full")
        return ExportRequest("incremental", state.watermark)

    def _run(self, force_full: bool) -> RunResult:
        now = self.clock()
        state = self.state.load() if self.state is not None else State()
        request = self._plan(force_full, state, now)
        report: IngestReport | None = None
        try:
            with self.source.open(request) as lines:
                report = self.ingestor.ingest(lines)
        except SourceVersionError as exc:
            return RunResult(request.mode, "version_mismatch", EXIT_VERSION, exc.report)
        except ExportFailed as exc:
            retry = exc.code == "runtime_error"
            status = "export_failed" if retry else "export_rejected"
            return RunResult(request.mode, status, EXIT_RETRY if retry else EXIT_CONFIG, exc.report)
        except ProtocolError as exc:
            return RunResult(request.mode, "protocol_error", EXIT_CONFIG, exc.report)
        except VolumeError:
            return RunResult(request.mode, "volume_error", EXIT_CONFIG)
        except StorageRejected as exc:
            return RunResult(request.mode, "storage_rejected", EXIT_RETRY, exc.report)
        except (StreamIncomplete, RetryLater, SourceUnavailable) as exc:
            return RunResult(request.mode, "incomplete", EXIT_RETRY, exc.report)
        except ServiceUnavailable:
            return RunResult(request.mode, "storage_unavailable", EXIT_RETRY, report)
        if not report.complete:
            return RunResult(request.mode, "incomplete", EXIT_RETRY, report)
        advanced = False
        if self.state is not None and report.watermark is not None:
            self.state.save(
                State(
                    watermark=report.watermark,
                    last_full_at=now if request.mode == "full" else state.last_full_at,
                )
            )
            advanced = True
        if report.rejected:
            return RunResult(request.mode, "complete_with_rejections", EXIT_RETRY, report, advanced)
        return RunResult(request.mode, "complete", EXIT_OK, report, advanced)
