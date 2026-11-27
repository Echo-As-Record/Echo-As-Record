"""消费导出流：文件字节进私有文件卷，会话/消息/附件整批追加到 ①。

一个数据批次内的顺序固定为 会话 → 附件 → 消息，保证收到消息事件时它引用的附件记录已可读；
附件总是先于引用它的消息出现在流里，因此即使分批提交，附件也先于消息提交。
文件字节先落盘（校验大小和 SHA-256）再写记录，所以 file_ref 永远指向已存在的文件。
写入复用 ear_adapter 的 `append_capture_batch`：CommitUnknown 用同一 request_id 重放，
资源限制不拆批地报告，冲突和不可用都映射为明确的结果。
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ear_adapter.runtime import Failed, Ingested, Pending, append_capture_batch
from ear_storage_client import StorageClient

from librechat_adapter.config import EXPORT_FORMAT, LibreChatConfig
from librechat_adapter.convert import (
    BlobInfo,
    Draft,
    attachment_draft,
    build_record,
    message_draft,
    record_id,
    session_draft,
)
from librechat_adapter.registry import CHAT_ATTACHMENT, CHAT_MESSAGE, CHAT_SESSION
from librechat_adapter.volume import BlobWriter, IntegrityError, PrivateVolume, VolumeError
from librechat_adapter.wire import (
    AttachmentFrame,
    BlobAbortFrame,
    BlobEndFrame,
    BlobFrame,
    ConversationFrame,
    EndFrame,
    ErrorFrame,
    HeaderFrame,
    MessageFrame,
    ProtocolError,
    RejectedFrame,
    StreamError,
    parse_frame,
)

_RANK = {CHAT_SESSION: 0, CHAT_ATTACHMENT: 1, CHAT_MESSAGE: 2}
_VERSION_CODES = frozenset({"unsupported_version", "version_unreadable"})
RETRYABLE_UNAVAILABLE = frozenset(
    {"read_error", "changed_during_read", "integrity_mismatch", "aborted", "blob_missing"}
)
"""这些原因多半是暂时的：本轮写入“不可用”修订，但不推进增量水位，下轮重新导出，成功后追加新修订。"""
_EXPORTER_SKIP_CODES = frozenset(
    {
        "duplicate_file_record",
        "file_time_missing",
        "invalid_conversation",
        "invalid_file",
        "invalid_inline_image",
        "invalid_message",
        "unsupported_inline_image",
    }
)
_UNAVAILABLE_CODES = RETRYABLE_UNAVAILABLE | {
    "bad_path",
    "missing_on_disk",
    "no_file_record",
    "not_regular",
    "too_large",
    "unavailable",
    "unsupported_source",
}
_MAX_REVISIONS = 10_000


class IngestError(StreamError):
    """本轮不能完成。子类决定 CLI 退出码；消息固定，不含数据。"""


class SourceVersionError(IngestError):
    """导出流的格式或 LibreChat 版本不在已核对范围内（退出码 3）。"""


class ExportFailed(IngestError):
    """导出方报告了错误帧。"""

    def __init__(self, code: str) -> None:
        super().__init__(f"导出方失败：{code}")
        self.code = code


class StreamIncomplete(IngestError):
    """流在 end 帧之前结束；已收到的完整条目已写入，水位不推进。"""


class RetryLater(IngestError):
    """① 暂时不可用或提交结果未知；下轮重试。"""


class StorageRejected(IngestError):
    """① 拒绝写入（冲突、资源限制）；需要人处理。"""


@dataclass(slots=True)
class IngestReport:
    librechat_version: str | None = None
    mode: str | None = None
    frames: Counter[str] = field(default_factory=Counter)
    written: Counter[str] = field(default_factory=Counter)
    unchanged: Counter[str] = field(default_factory=Counter)
    deduplicated: int = 0
    anomalies: int = 0
    blobs_new: int = 0
    blobs_reused: int = 0
    blob_bytes: int = 0
    text_overflow: int = 0
    size_mismatch: int = 0
    unavailable: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    exporter_skipped: dict[str, int] = field(default_factory=dict)
    retry_needed: bool = False
    seen_end: bool = False
    suspect_empty: bool = False
    watermark: datetime | None = None

    @property
    def complete(self) -> bool:
        return self.seen_end and not self.retry_needed and not self.suspect_empty

    def summary(self) -> dict[str, Any]:
        return {
            "librechat_version": self.librechat_version,
            "mode": self.mode,
            "complete": self.complete,
            "frames": dict(self.frames),
            "written": dict(self.written),
            "unchanged": dict(self.unchanged),
            "deduplicated": self.deduplicated,
            "anomalies": self.anomalies,
            "blobs": {
                "new": self.blobs_new,
                "reused": self.blobs_reused,
                "bytes": self.blob_bytes,
            },
            "text_overflow_files": self.text_overflow,
            "size_mismatch": self.size_mismatch,
            "unavailable": dict(self.unavailable),
            "rejected_frames": dict(self.rejected),
            "exporter_skipped": self.exporter_skipped,
            "retry_needed": self.retry_needed,
            "suspect_empty": self.suspect_empty,
            "deletions": "deferred",
        }


class Ingestor:
    def __init__(
        self,
        storage: StorageClient,
        volume: PrivateVolume,
        config: LibreChatConfig | None = None,
        *,
        accept_versions: frozenset[str] = frozenset(),
    ) -> None:
        self.storage = storage
        self.volume = volume
        self.config = config or LibreChatConfig()
        self.versions = self.config.supported_versions | accept_versions

    # -- stream ---------------------------------------------------------------------------
    def ingest(self, lines: Iterable[str]) -> IngestReport:
        report = IngestReport()
        writers: dict[int, tuple[BlobWriter, int]] = {}
        done: dict[int, BlobInfo] = {}
        failed: dict[int, str] = {}
        pending: dict[tuple[str, str], Draft] = {}
        pending_bytes = 0
        header_seen = False
        try:
            for raw in lines:
                if not raw.strip():
                    continue
                if report.seen_end:
                    raise ProtocolError("end 帧之后仍有数据")
                frame = parse_frame(raw)
                kind = frame.kind if isinstance(frame, RejectedFrame) else frame.t
                report.frames[kind] += 1
                if isinstance(frame, ErrorFrame):
                    if frame.code in _VERSION_CODES:
                        raise SourceVersionError(f"导出方拒绝：{frame.code}")
                    raise ExportFailed(frame.code)
                if not header_seen:
                    if not isinstance(frame, HeaderFrame):
                        raise ProtocolError("导出流必须以 header 帧开始")
                    self._check_header(frame, report)
                    header_seen = True
                    continue
                if isinstance(frame, RejectedFrame):
                    report.rejected[f"{frame.kind}:{frame.reason}"] += 1
                elif isinstance(frame, ConversationFrame):
                    draft = session_draft(frame)
                    pending[draft.identity] = draft
                    pending_bytes += draft.size
                elif isinstance(frame, MessageFrame):
                    draft, overflow = message_draft(frame, self.config, self.volume)
                    report.text_overflow += int(overflow)
                    pending[draft.identity] = draft
                    pending_bytes += draft.size
                elif isinstance(frame, AttachmentFrame):
                    draft = self._attachment(frame, done, failed, report)
                    pending[draft.identity] = draft
                    pending_bytes += draft.size
                elif isinstance(frame, BlobFrame):
                    self._blob_chunk(frame, writers, done, failed)
                elif isinstance(frame, BlobEndFrame):
                    self._blob_end(frame, writers, done, failed, report)
                elif isinstance(frame, BlobAbortFrame):
                    entry = writers.pop(frame.blob_id, None)
                    if entry is not None:
                        entry[0].discard()
                    failed[frame.blob_id] = "aborted"
                elif isinstance(frame, EndFrame):
                    self._end(frame, report)
                elif isinstance(frame, HeaderFrame):
                    raise ProtocolError("导出流含重复的 header 帧")
                if (
                    len(pending) >= self.config.batch_records
                    or pending_bytes >= self.config.batch_bytes
                ):
                    self._flush(pending, report)
                    pending_bytes = 0
            self._flush(pending, report)
            if not report.seen_end:
                raise StreamIncomplete("导出流在 end 帧之前结束")
            if writers:
                raise ProtocolError("导出流在 blob_end 之前结束")
        except StreamError as exc:
            exc.report = report
            raise
        finally:
            for writer, _ in writers.values():
                writer.discard()
        return report

    # -- header / end ---------------------------------------------------------------------
    def _check_header(self, header: HeaderFrame, report: IngestReport) -> None:
        if header.format != EXPORT_FORMAT:
            raise SourceVersionError("导出流格式不是 ear-librechat-export/1")
        if header.librechat_version not in self.versions:
            raise SourceVersionError("LibreChat 版本不在已核对范围内")
        report.librechat_version = header.librechat_version
        report.mode = header.mode

    @staticmethod
    def _end(frame: EndFrame, report: IngestReport) -> None:
        if not frame.complete:
            raise StreamIncomplete("导出方报告导出未完成")
        report.seen_end = True
        report.watermark = frame.watermark
        report.suspect_empty = frame.suspect_empty
        skipped = frame.counts.get("skipped", {})
        if isinstance(skipped, dict):
            report.exporter_skipped = {
                k: v
                for k, v in skipped.items()
                if k in _EXPORTER_SKIP_CODES and type(v) is int and v >= 0
            }

    # -- blobs ----------------------------------------------------------------------------
    def _blob_chunk(
        self,
        frame: BlobFrame,
        writers: dict[int, tuple[BlobWriter, int]],
        done: dict[int, BlobInfo],
        failed: dict[int, str],
    ) -> None:
        if frame.blob_id in done:
            raise ProtocolError("blob 在 blob_end 之后又收到数据")
        if frame.blob_id in failed:
            return
        entry = writers.get(frame.blob_id)
        if entry is None:
            if frame.seq != 0:
                raise ProtocolError("blob 序号不是从 0 开始")
            entry = (self.volume.begin(self.config.max_blob_bytes), 0)
        writer, expected = entry
        if frame.seq != expected:
            writer.discard()
            raise ProtocolError("blob 序号不连续")
        try:
            data = base64.b64decode(frame.b64, validate=True)
        except (binascii.Error, ValueError):
            writer.discard()
            raise ProtocolError("blob 数据不是合法的 base64") from None
        try:
            writer.write(data)
        except VolumeError:
            writers.pop(frame.blob_id, None)
            failed[frame.blob_id] = "too_large"
            return
        writers[frame.blob_id] = (writer, expected + 1)

    def _blob_end(
        self,
        frame: BlobEndFrame,
        writers: dict[int, tuple[BlobWriter, int]],
        done: dict[int, BlobInfo],
        failed: dict[int, str],
        report: IngestReport,
    ) -> None:
        if frame.blob_id in failed:
            return
        entry = writers.pop(frame.blob_id, None)
        if entry is None:
            if frame.size != 0:
                raise ProtocolError("blob_end 之前没有数据")
            writer = self.volume.begin(1)  # 空文件：没有 blob 帧
        else:
            writer = entry[0]
        try:
            ref, new = writer.finish(frame.size, frame.sha256)
        except IntegrityError:
            failed[frame.blob_id] = "integrity_mismatch"
            return
        done[frame.blob_id] = BlobInfo(ref, new)
        report.blobs_new += int(new)
        report.blobs_reused += int(not new)
        report.blob_bytes += ref.size

    def _attachment(
        self,
        frame: AttachmentFrame,
        done: dict[int, BlobInfo],
        failed: dict[int, str],
        report: IngestReport,
    ) -> Draft:
        blob: BlobInfo | None = None
        reason = frame.unavailable
        if frame.blob_id is not None:
            blob = done.get(frame.blob_id)
            if blob is None:
                reason = failed.get(frame.blob_id, "blob_missing")
        elif reason is None:
            reason = "unavailable"
        if blob is None:
            if reason not in _UNAVAILABLE_CODES:
                reason = "unavailable"
            report.unavailable[reason] += 1
            report.retry_needed = report.retry_needed or reason in RETRYABLE_UNAVAILABLE
        elif frame.declared_bytes is not None and frame.declared_bytes != blob.file_ref.size:
            report.size_mismatch += 1
        return attachment_draft(frame, blob, reason, self.config, self.volume)

    # -- writing --------------------------------------------------------------------------
    def _flush(self, pending: dict[tuple[str, str], Draft], report: IngestReport) -> None:
        drafts = sorted(pending.values(), key=lambda d: _RANK[d.data_type])
        pending.clear()
        if not drafts:
            return
        latest = self._latest_revisions(drafts)
        records = []
        for draft in drafts:
            revision, known = latest[draft.identity]
            if revision > 0 and known == draft.fingerprint:
                report.unchanged[draft.data_type] += 1
            else:
                records.append(build_record(draft, revision + 1, self.config))
        if not records:
            return
        outcome = append_capture_batch("librechat", records, storage=self.storage)
        if isinstance(outcome, Ingested):
            report.written.update(outcome.written)
            report.deduplicated += outcome.deduplicated
            if outcome.deduplicated:
                # 探测认为是新修订，① 却说去重键已存在：探测漏看了已有修订（例如读权限缺失会让
                # get_many 静默返回空）。编辑可能没有落地，必须报警而不是推进水位。
                report.anomalies += outcome.deduplicated
                report.retry_needed = True
        elif isinstance(outcome, Pending):
            report.retry_needed = True
            raise RetryLater(outcome.reason)
        elif isinstance(outcome, Failed):
            raise StorageRejected(outcome.reason)
        else:  # pragma: no cover - append_capture_batch never returns AlreadyIngested
            raise StorageRejected("未知的写入结果")

    def _latest_revisions(self, drafts: list[Draft]) -> dict[tuple[str, str], tuple[int, Any]]:
        """沿 r1、r2、… 探测 ① 里已有的修订；返回每个对象的 (最大修订号, 其内容指纹)。"""
        latest: dict[tuple[str, str], tuple[int, Any]] = {d.identity: (0, None) for d in drafts}
        active = drafts
        revision = 1
        while active and revision <= _MAX_REVISIONS:
            ids = {record_id(d, revision, self.config): d for d in active}
            found: dict[str, Any] = {}
            keys = list(ids)
            for start in range(0, len(keys), 500):
                found.update(self.storage.get_many(keys[start : start + 500]))
            active = []
            for rid, draft in ids.items():
                record = found.get(rid)
                if record is None:
                    continue
                payload = getattr(record, "payload", {})
                latest[draft.identity] = (revision, payload.get("content_sha256"))
                active.append(draft)
            revision += 1
        return latest
