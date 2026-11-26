"""导出流 `ear-librechat-export/1` 的帧定义与解析。

一行一个 JSON 对象，`t` 表示帧类型。本模块只做结构与类型校验，不信任流里的任何字段：
标识符限制为安全字符集，时间必须是以 Z 结尾的 UTC 字符串，文件只通过 blob 帧传字节。
单条会话/消息/附件帧校验失败只报告为 `RejectedFrame`（计数、跳过），头、blob、尾部帧
校验失败则是协议错误，整轮中止。错误信息只含帧类型和字段名，不回显内容。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
)

_IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class StreamError(Exception):
    """本轮导出流无法完成的一切原因的基类；`report` 是已处理部分的统计（若有）。"""

    report: Any = None


class ProtocolError(StreamError):
    """导出流违反约定。固定消息，不含流里的内容。"""


def _ident(value: Any) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value) or value in (".", ".."):
        raise ValueError("unsafe identifier")
    return value


def _utc(value: Any) -> Any:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be an RFC 3339 UTC string ending in Z")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("timestamp must carry a timezone")
    return value.astimezone(UTC)


Ident = Annotated[str, AfterValidator(_ident)]
Ts = Annotated[datetime, BeforeValidator(_utc), AfterValidator(_to_utc)]
ShortStr = Annotated[StrictStr, StringConstraints(max_length=2000)]
OptStr = ShortStr | None


class _Frame(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class HeaderFrame(_Frame):
    t: Literal["header"]
    format: StrictStr
    exporter_version: StrictStr
    librechat_version: StrictStr
    data_schemas_version: OptStr = None
    mode: Literal["full", "incremental"]
    since: Ts | None = None
    started_at: Ts
    account_ref: StrictStr
    source_counts: dict[str, StrictInt] = Field(default_factory=dict)


class ConversationFrame(_Frame):
    t: Literal["conversation"]
    conversation_id: Ident
    title: ShortStr = ""
    endpoint: OptStr = None
    model: OptStr = None
    agent_id: OptStr = None
    tags: list[ShortStr] = Field(default_factory=list, max_length=50)
    created_at: Ts
    updated_at: Ts
    is_temporary: StrictBool = False
    is_archived: StrictBool = False
    expired_at: Ts | None = None


class ToolCall(_Frame):
    name: StrictStr = ""
    args: StrictStr = ""
    output: StrictStr = ""
    truncated: StrictBool = False


class MessageFrame(_Frame):
    t: Literal["message"]
    conversation_id: Ident
    message_id: Ident
    parent_message_id: Ident | None = None
    role: Literal["user", "assistant"]
    sender: OptStr = None
    model: OptStr = None
    endpoint: OptStr = None
    created_at: Ts
    updated_at: Ts
    text: StrictStr
    text_source: Literal["content", "text"] = "text"
    text_truncated: StrictBool = False
    reasoning: StrictStr = ""
    reasoning_truncated: StrictBool = False
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=100)
    part_types: dict[str, StrictInt] = Field(default_factory=dict)
    attachment_file_ids: list[Ident] = Field(default_factory=list, max_length=500)
    error: StrictBool = False
    unfinished: StrictBool = False
    finish_reason: OptStr = None
    is_temporary: StrictBool = False


class AttachmentFrame(_Frame):
    t: Literal["attachment"]
    conversation_id: Ident
    message_id: Ident
    message_created_at: Ts
    ref_kind: Literal["upload", "generated", "inline", "unlinked"]
    file_id: Ident
    filename: Annotated[StrictStr, StringConstraints(max_length=500)] = ""
    media_type: Annotated[StrictStr, StringConstraints(pattern=_MEDIA_TYPE.pattern)]
    declared_bytes: StrictInt | None = None
    source: OptStr = None
    context: OptStr = None
    status: OptStr = None
    created_at: Ts | None = None
    updated_at: Ts | None = None
    text: StrictStr | None = None
    text_truncated: StrictBool = False
    text_format: OptStr = None
    blob_id: StrictInt | None = None
    unavailable: Annotated[StrictStr, StringConstraints(pattern=_REASON.pattern)] | None = None


class BlobFrame(_Frame):
    t: Literal["blob"]
    blob_id: StrictInt = Field(ge=1)
    seq: StrictInt = Field(ge=0)
    b64: StrictStr


class BlobEndFrame(_Frame):
    t: Literal["blob_end"]
    blob_id: StrictInt = Field(ge=1)
    size: StrictInt = Field(ge=0)
    sha256: Annotated[StrictStr, StringConstraints(pattern=_SHA256.pattern)]


class BlobAbortFrame(_Frame):
    t: Literal["blob_abort"]
    blob_id: StrictInt = Field(ge=1)


class ConversationEndFrame(_Frame):
    t: Literal["conversation_end"]
    conversation_id: Ident
    messages: StrictInt = 0


class EndFrame(_Frame):
    t: Literal["end"]
    complete: StrictBool
    counts: dict[str, Any] = Field(default_factory=dict)
    suspect_empty: StrictBool = False
    watermark: Ts
    finished_at: Ts | None = None


class ErrorFrame(_Frame):
    t: Literal["error"]
    code: Annotated[StrictStr, StringConstraints(pattern=_REASON.pattern)]
    message: Annotated[StrictStr, StringConstraints(max_length=300)] = ""


class RejectedFrame:
    """单个条目校验失败：跳过并计数，不影响其余条目。"""

    __slots__ = ("kind", "reason")

    def __init__(self, kind: str, reason: str) -> None:
        self.kind = kind
        self.reason = reason


Frame = (
    HeaderFrame
    | ConversationFrame
    | MessageFrame
    | AttachmentFrame
    | BlobFrame
    | BlobEndFrame
    | BlobAbortFrame
    | ConversationEndFrame
    | EndFrame
    | ErrorFrame
)

_ITEM_FRAMES: dict[str, type[_Frame]] = {
    "conversation": ConversationFrame,
    "message": MessageFrame,
    "attachment": AttachmentFrame,
}
_STRUCTURAL_FRAMES: dict[str, type[_Frame]] = {
    "header": HeaderFrame,
    "blob": BlobFrame,
    "blob_end": BlobEndFrame,
    "blob_abort": BlobAbortFrame,
    "conversation_end": ConversationEndFrame,
    "end": EndFrame,
    "error": ErrorFrame,
}


def _fields(exc: ValidationError) -> str:
    # 只取顶层字段名：嵌套位置（字典键、数组下标）可能来自数据，不进入日志。
    names = sorted({str(err["loc"][0]) if err["loc"] else "?" for err in exc.errors()})
    return ",".join(names[:6])


_UNSAFE = re.compile(r"[\ud800-\udfff\x00]")


def clean_text(text: str) -> str:
    """孤立代理项和 NUL 不能进 UTF-8、JSON 或 PostgreSQL：替换为 U+FFFD。"""
    return _UNSAFE.sub("\ufffd", text)


def _scrub(value: Any) -> Any:
    """校验前清洗条目帧里的所有字符串：pydantic 不接受孤立代理项，会把整条消息拒掉。"""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {clean_text(k) if isinstance(k, str) else k: _scrub(v) for k, v in value.items()}
    return value


def parse_frame(line: str) -> Frame | RejectedFrame:
    try:
        raw = json.loads(line)
    except (ValueError, RecursionError):
        raise ProtocolError("导出流含非 JSON 行") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("t"), str):
        raise ProtocolError("导出流的行缺少帧类型 t")
    kind: str = raw["t"]
    if kind in _ITEM_FRAMES:
        try:
            return _ITEM_FRAMES[kind].model_validate(_scrub(raw))  # type: ignore[return-value]
        except ValidationError as exc:
            return RejectedFrame(kind, _fields(exc))
    if kind in _STRUCTURAL_FRAMES:
        try:
            return _STRUCTURAL_FRAMES[kind].model_validate(raw)  # type: ignore[return-value]
        except ValidationError as exc:
            raise ProtocolError(f"{kind} 帧字段无效：{_fields(exc)}") from None
    raise ProtocolError("导出流含未知帧类型")
