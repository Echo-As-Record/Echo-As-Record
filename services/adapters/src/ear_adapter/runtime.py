"""一轮导入：列出录音 → 判断能否导入 → 取声纹和音频位置 → 转换 → 整批写入 ①。"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field

from ear_contracts import SYSTEM
from ear_storage_client import AppendRaw, Context, StorageClient

from ear_adapter.config import AdapterConfig
from ear_adapter.convert import Blocked, Waiting, audio_ref_id, capture_records, readiness
from ear_adapter.ear_api import SpeakerResolution
from ear_adapter.source import AudioLocator, EarSource, SourceUnavailable

logger = logging.getLogger("ear_adapter")


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
    except SourceUnavailable as exc:
        return Pending(capture_id, str(exc))

    records = capture_records(evidence, audio_file=audio_file, embeddings=embeddings, config=config)
    result = storage.write([AppendRaw(r) for r in records], Context.system())
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

    def run_once(self) -> Report:
        report = Report()
        try:
            summaries = list(self.source.list_captures())
        except SourceUnavailable as exc:
            report.source_error = str(exc)
            _log("source_unavailable", reason=str(exc))
            return report
        for summary in summaries:
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
