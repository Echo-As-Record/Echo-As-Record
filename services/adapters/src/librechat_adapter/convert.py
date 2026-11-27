"""导出帧 → 原始数据草稿（确定性 id、去重键、指纹、修订）。

三类记录都用同一套修订规则，所有判断只依赖内容，不依赖本地状态：

- 一条逻辑对象（会话、消息、消息的某个附件）有基础键 `base_key`；它的内容指纹 `fingerprint`
  是不含易变字段（来源 updatedAt）的规范 JSON 的 SHA-256。
- 第 n 次修订的去重键是 `<device>:<base_key>:r<n>`，id 由 (事件时间, 类型, 版本, 去重键) 确定。
  事件时间对一个对象的所有修订相同（消息/附件取消息创建时间，会话取创建时间）。
- 写入前沿 r1、r2、… 探测 ① 里已有的修订，取最后一个；指纹相同就跳过，不同则写 r(n+1)。
  因此：重放不新增；编辑产生新修订；内容改回旧值（A→B→A）也是新修订；仅 updatedAt 变化
  （点赞、元数据）不新增；本地状态丢失后全量重导同样安全。读取方取同一对象 revision 最大的一条。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from ear_adapter.ids import deterministic_id
from ear_contracts import FileRef, NewRawRecord, Source

from librechat_adapter.config import CHANNEL, LibreChatConfig
from librechat_adapter.registry import CHAT_ATTACHMENT, CHAT_MESSAGE, CHAT_SESSION
from librechat_adapter.volume import PrivateVolume
from librechat_adapter.wire import AttachmentFrame, ConversationFrame, MessageFrame, clean_text

SOURCE_SYSTEM = "librechat"
_TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
        "application/x-javascript",
        "application/sql",
        "application/x-sh",
        "application/x-ndjson",
        "application/csv",
    }
)


_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
        ".html",
        ".htm",
        ".log",
        ".tex",
        ".sql",
        ".sh",
        ".py",
        ".js",
        ".mjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".go",
        ".rs",
    }
)


def clean(text: str) -> str:
    """孤立代理项和 NUL 不能进 JSON/PostgreSQL：替换为 U+FFFD（wire 在校验前已清洗一遍）。"""
    return clean_text(text)


def _opt(text: str | None, limit: int = 2000) -> str | None:
    return None if text is None else clean(text)[:limit]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def iso_z(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_like(media_type: str, filename: str = "") -> bool:
    """按媒体类型或常见文本扩展名判断；真正解码前还会检查是否含 NUL（二进制）。"""
    return (
        media_type.startswith("text/")
        or media_type in _TEXT_TYPES
        or media_type.endswith(("+json", "+xml"))
        or PurePosixPath(filename.lower()).suffix in _TEXT_EXTENSIONS
    )


@dataclass(frozen=True, slots=True)
class BlobInfo:
    file_ref: FileRef
    new: bool = False


@dataclass(frozen=True, slots=True)
class Draft:
    data_type: str
    base_key: str
    event_time: datetime
    fingerprint: str
    payload: dict[str, Any]
    size: int
    label: str
    """会话 id，仅用于日志计数。"""

    @property
    def identity(self) -> tuple[str, str]:
        return (self.data_type, self.base_key)


def dedup_key(draft: Draft, revision: int, config: LibreChatConfig) -> str:
    return f"{config.device}:{draft.base_key}:r{revision}"


def record_id(draft: Draft, revision: int, config: LibreChatConfig) -> str:
    return deterministic_id(
        draft.event_time,
        draft.data_type,
        config.adapter_version,
        dedup_key(draft, revision, config),
    )


def build_record(draft: Draft, revision: int, config: LibreChatConfig) -> NewRawRecord:
    return NewRawRecord(
        id=record_id(draft, revision, config),
        data_type=draft.data_type,
        event_start=draft.event_time,
        event_end=draft.event_time,
        source=Source(device=config.device, channel=CHANNEL, user=config.user),
        adapter_version=config.adapter_version,
        dedup_key=dedup_key(draft, revision, config),
        payload={**draft.payload, "revision": revision, "content_sha256": draft.fingerprint},
    )


def _draft(
    data_type: str,
    base_key: str,
    event_time: datetime,
    content: dict[str, Any],
    volatile: dict[str, Any],
    label: str,
) -> Draft:
    payload = {**content, **volatile, "source_system": SOURCE_SYSTEM}
    return Draft(
        data_type=data_type,
        base_key=base_key,
        event_time=event_time,
        fingerprint=fingerprint(content),
        payload=payload,
        size=len(canonical(payload)),
        label=label,
    )


# -- chat_session -----------------------------------------------------------------------------
def session_draft(frame: ConversationFrame) -> Draft:
    content: dict[str, Any] = {
        "conversation_id": frame.conversation_id,
        "title": clean(frame.title),
        "tags": [clean(t) for t in frame.tags],
        "is_temporary": frame.is_temporary,
        "is_archived": frame.is_archived,
        "source_created_at": iso_z(frame.created_at),
    }
    for key, value in (
        ("endpoint", frame.endpoint),
        ("model", frame.model),
        ("agent_id", frame.agent_id),
    ):
        if value is not None:
            content[key] = _opt(value)
    volatile: dict[str, Any] = {"source_updated_at": iso_z(frame.updated_at)}
    if frame.expired_at is not None:
        volatile["expired_at"] = iso_z(frame.expired_at)
    return _draft(
        CHAT_SESSION,
        f"session:{frame.conversation_id}",
        frame.created_at,
        content,
        volatile,
        frame.conversation_id,
    )


# -- chat_message -----------------------------------------------------------------------------
def message_draft(
    frame: MessageFrame, config: LibreChatConfig, volume: PrivateVolume
) -> tuple[Draft, bool]:
    """返回 (草稿, 全文是否因超长转存到了文件卷)。"""
    full = clean(frame.text)
    overflow = len(full) > config.max_text_chars
    content: dict[str, Any] = {
        "text": full[: config.max_text_chars] if overflow else full,
        "role": frame.role,
        "conversation_id": frame.conversation_id,
        "message_id": frame.message_id,
        "source_created_at": iso_z(frame.created_at),
        "text_chars": len(full),
        "text_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
        "text_truncated": overflow or frame.text_truncated,
        "attachment_ids": list(frame.attachment_file_ids),
        "error": frame.error,
        "unfinished": frame.unfinished,
        "is_temporary": frame.is_temporary,
    }
    if overflow:
        ref, _ = volume.put_bytes(full.encode("utf-8"))
        content["text_file_ref"] = ref.model_dump()
    for key, value in (
        ("parent_message_id", frame.parent_message_id),
        ("sender", frame.sender),
        ("model", frame.model),
        ("endpoint", frame.endpoint),
        ("finish_reason", frame.finish_reason),
    ):
        if value is not None:
            content[key] = _opt(value, 512)
    if frame.reasoning:
        reasoning = clean(frame.reasoning)
        content["reasoning"] = reasoning[: config.max_aux_chars]
        content["reasoning_truncated"] = (
            frame.reasoning_truncated or len(reasoning) > config.max_aux_chars
        )
    if frame.tool_calls:
        content["tool_calls"] = [
            {
                "name": clean(call.name)[:200],
                "args": clean(call.args),
                "output": clean(call.output),
                "truncated": call.truncated,
            }
            for call in frame.tool_calls
        ]
    if frame.part_types:
        content["part_types"] = dict(sorted(frame.part_types.items()))
    volatile = {"source_updated_at": iso_z(frame.updated_at)}
    draft = _draft(
        CHAT_MESSAGE,
        f"message:{frame.conversation_id}:{frame.message_id}",
        frame.created_at,
        content,
        volatile,
        frame.conversation_id,
    )
    return draft, overflow


# -- chat_attachment --------------------------------------------------------------------------
def _attachment_text(
    frame: AttachmentFrame,
    blob: BlobInfo | None,
    config: LibreChatConfig,
    volume: PrivateVolume,
) -> tuple[str, str, bool]:
    """(text, kind, truncated)：LibreChat 提取的文本 > 文本类文件的前缀 > 安全元数据。"""
    cap = config.max_attachment_text_chars
    if frame.text:
        text = clean(frame.text)
        return text[:cap], "extracted", frame.text_truncated or len(text) > cap
    if blob is not None and text_like(frame.media_type, frame.filename):
        data = volume.read_prefix(blob.file_ref, cap * 4 + 4)
        if b"\x00" not in data:
            decoded = clean(data.decode("utf-8", errors="replace"))
            cut = len(data) < blob.file_ref.size or len(decoded) > cap
            if cut:
                decoded = decoded[:cap].rstrip("\ufffd")
            return decoded, "decoded", cut
    size = blob.file_ref.size if blob is not None else frame.declared_bytes
    name = clean(frame.filename) or frame.file_id
    detail = f"{frame.media_type}, {size} bytes" if size is not None else frame.media_type
    note = f" [unavailable: {frame.unavailable}]" if blob is None and frame.unavailable else ""
    return f"{name} ({detail}){note}", "metadata", False


def attachment_draft(
    frame: AttachmentFrame,
    blob: BlobInfo | None,
    unavailable: str | None,
    config: LibreChatConfig,
    volume: PrivateVolume,
) -> Draft:
    text, kind, cut = _attachment_text(frame, blob, config, volume)
    size = blob.file_ref.size if blob is not None else frame.declared_bytes
    content: dict[str, Any] = {
        "text": text,
        "text_kind": kind,
        "text_truncated": cut,
        "conversation_id": frame.conversation_id,
        "message_id": frame.message_id,
        "file_id": frame.file_id,
        "filename": clean(frame.filename),
        "media_type": frame.media_type,
        "origin": frame.ref_kind,
        "available": blob is not None,
        "source_created_at": iso_z(frame.message_created_at),
    }
    if size is not None:
        content["size"] = size
    if blob is not None:
        content["file_ref"] = blob.file_ref.model_dump()
    else:
        content["unavailable_reason"] = unavailable or frame.unavailable or "unavailable"
    if frame.context is not None:
        content["source_context"] = _opt(frame.context, 100)
    volatile: dict[str, Any] = {}
    if frame.updated_at is not None:
        volatile["source_updated_at"] = iso_z(frame.updated_at)
    if frame.created_at is not None:
        volatile["file_created_at"] = iso_z(frame.created_at)
    return _draft(
        CHAT_ATTACHMENT,
        f"attachment:{frame.conversation_id}:{frame.message_id}:{frame.file_id}",
        frame.message_created_at,
        content,
        volatile,
        frame.conversation_id,
    )
