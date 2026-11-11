"""一轮导入：列出录音 → 判断能否导入 → 取声纹和音频位置 → 转换 → 整批写入 ①。"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from ear_contracts import DEFAULT_REGISTRY, SYSTEM, new_id
from ear_storage_client import AppendRaw, Context, PermissionDenied, Query, StorageClient
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown, ResourceLimit, ServiceUnavailable
from ear_storage_client.wire import encode_request

from ear_adapter.config import AdapterConfig
from ear_adapter.convert import Blocked, Waiting, audio_ref_id, capture_records, readiness
from ear_adapter.ear_api import SpeakerResolution
from ear_adapter.source import AudioLocator, EarSource, SourceUnavailable

logger = logging.getLogger("ear_adapter")
READ_TYPES = tuple(
    name for name, spec in DEFAULT_REGISTRY.data_types.items() if spec.writer == "adapter"
)


@dataclass(frozen=True, slots=True)
class Ingested:
    capture_id: str
    written: Counter[str]
    """按 data_type 统计本次新写入的条数。"""
    deduplicated: int


@dataclass(frozen=True, slots=True)
class AlreadyIngested:
    capture_id: str


@dataclass(frozen=True, slots=True)
class Pending:
    """暂时不能导入（感知未完成、旧 Ear 连不上、音频文件还没找到），下一轮自动重试。"""

    capture_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Failed:
    """需要人处理：旧 Ear 某阶段失败，或 ① 拒绝写入。不会被静默跳过。"""

    capture_id: str
    reason: str


Outcome = Ingested | AlreadyIngested | Pending | Failed


def ingest_capture(
    capture_id: str,
    *,
    source: EarSource,
    storage: StorageClient,
    locator: AudioLocator,
    config: AdapterConfig,
) -> Outcome:
    try:
        evidence = source.read_capture(capture_id)
        state = readiness(evidence)
        if isinstance(state, Waiting):
            return Pending(capture_id, state.reason)
        if isinstance(state, Blocked):
            return Failed(capture_id, state.reason)
        capture = state.capture
        # 整批原子写入：audio_ref 已存在就说明这段录音整批导入过。
        if storage.get(audio_ref_id(capture, config)) is not None:
            return AlreadyIngested(capture_id)
        audio_file = locator.locate(capture)
        if audio_file is None:
            return Pending(capture_id, "在 Brain inbox 里找不到大小和哈希都对得上的音频文件")
        embeddings = {
            r.resolution_id: source.read_embedding(r.resolution_id)
            for r in evidence.of_type("speaker_resolution", SpeakerResolution)
            if r.embedding_ref is not None
        }
    except SourceUnavailable:
        return Pending(capture_id, "旧 Ear 暂时不可用")
    except ServiceUnavailable:
        return Pending(capture_id, "① 暂时不可用")

    records = capture_records(evidence, audio_file=audio_file, embeddings=embeddings, config=config)
    ops = [AppendRaw(r) for r in records]
    context = Context.system()
    request_id = new_id()
    # StorageClient 的基础协议没有 request_id；仅远程 SDK 支持回执重放。
    for attempt in range(3):
        try:
            if isinstance(storage, RemoteStorageClient):
                result = storage.write(ops, context, request_id=request_id)
            else:
                result = storage.write(ops, context)
            break
        except CommitUnknown as exc:
            request_id = exc.request_id
            if attempt == 2:
                return Pending(capture_id, "① 提交结果仍未知；下一轮检查 audio_ref")
            time.sleep(0.25 * 2**attempt)
        except ServiceUnavailable:
            return Pending(capture_id, "① 暂时不可用")
        except ResourceLimit:
            # 包括 HTTP 413 与服务端操作数/投影限制；不拆批、不输出正文。
            size = len(encode_request(ops, context).encode("utf-8"))
            return Failed(
                capture_id,
                f"① 资源限制：批次 {len(ops)} 条，正文 {size} 字节（未计 request_id）；不拆批",
            )
    else:
        raise AssertionError("unreachable")
    if result.conflict is not None:
        return Failed(capture_id, f"① 拒绝写入：{result.conflict.reason}")
    written = Counter(
        r.data_type for r in result.results if r.outcome == "written" and r.data_type is not None
    )
    deduplicated = sum(1 for r in result.results if r.outcome == "deduplicated")
    return Ingested(capture_id, written, deduplicated)


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    storage_error: str | None = None
    source_error: str | None = None
    """列出录音这一步就失败时的原因（旧 Ear 连不上）。"""

    def summary(self) -> dict[str, object]:
        written: Counter[str] = Counter()
        for o in self.outcomes:
            if isinstance(o, Ingested):
                written.update(o.written)
        kinds = Counter(type(o).__name__ for o in self.outcomes)
        return {
            "captures": dict(kinds),
            "written": dict(sorted(written.items())),
            "pending": [(o.capture_id, o.reason) for o in self.outcomes if isinstance(o, Pending)],
            "failed": [(o.capture_id, o.reason) for o in self.outcomes if isinstance(o, Failed)],
            "source_error": self.source_error,
            "storage_error": self.storage_error,
        }


class Runner:
    """反复调用 run_once。进程内记住已导入的录音，避免每轮重读；重启后靠 ① 的去重兜底。"""

    def __init__(
        self,
        *,
        source: EarSource,
        storage: StorageClient,
        locator: AudioLocator,
        config: AdapterConfig,
    ) -> None:
        self.source = source
        self.storage = storage
        self.locator = locator
        self.config = config
        self.done: set[str] = set()

    def run_once(self, *, should_stop: Callable[[], bool] = lambda: False) -> Report:
        report = Report()
        try:
            for data_type in READ_TYPES:
                self.storage.query(Query(data_type, limit=1))
        except PermissionDenied:
            raise PermissionDenied("adapter 需要全部七种原始类型的 read_types 授权") from None
        except ServiceUnavailable:
            report.storage_error = "① 暂时不可用"
            return report
        try:
            summaries = list(self.source.list_captures())
        except SourceUnavailable:
            report.source_error = "旧 Ear 暂时不可用"
            _log("source_unavailable", reason=report.source_error)
            return report
        for summary in summaries:
            if should_stop():
                break
            if summary.capture_id in self.done:
                continue
            outcome = ingest_capture(
                summary.capture_id,
                source=self.source,
                storage=self.storage,
                locator=self.locator,
                config=self.config,
            )
            report.outcomes.append(outcome)
            if isinstance(outcome, Ingested | AlreadyIngested):
                self.done.add(summary.capture_id)
            _log_outcome(outcome)
        return report


def _log(message: str, **fields: object) -> None:
    entry = {"module": "adapter", "event_id": None, "root_feedback_id": SYSTEM, "depth": 0}
    entry.update(message=message, **fields)
    logger.info(json.dumps(entry, ensure_ascii=False, default=str))


def _log_outcome(outcome: Outcome) -> None:
    match outcome:
        case Ingested(capture_id, written, deduplicated):
            _log(
                "ingested", capture_id=capture_id, written=dict(written), deduplicated=deduplicated
            )
        case AlreadyIngested(capture_id):
            _log("already_ingested", capture_id=capture_id)
        case Pending(capture_id, reason):
            _log("pending", capture_id=capture_id, reason=reason)
        case Failed(capture_id, reason):
            _log("failed", capture_id=capture_id, reason=reason)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
