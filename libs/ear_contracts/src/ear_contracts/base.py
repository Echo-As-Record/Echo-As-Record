"""公共基础类型：id、时间、模块名、根源反馈 id（README 第 7 节、术语表）。"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BeforeValidator, StringConstraints

ID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
SNAKE_CASE_PATTERN: Final = r"^[a-z][a-z0-9_]*$"
_ID_RE: Final = re.compile(ID_PATTERN)

SYSTEM: Final = "system"
"""系统发起的行为（如定时提取）的 root_feedback_id。"""

MODULES: Final = (
    "storage",
    "adapter",
    "attribution",
    "extraction",
    "dependency",
    "api",
    "llm_gateway",
)
"""MVP 的模块名（术语表「模块」）。扩展模块有自己的名字，因此类型上不用枚举。"""

Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
"""UUIDv7，小写字符串。"""

SnakeCase = Annotated[str, StringConstraints(pattern=SNAKE_CASE_PATTERN)]
DataType = SnakeCase
ModuleName = SnakeCase
RootFeedbackId = Id | Literal["system"]


def is_id(value: object) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


_lock = threading.Lock()
_last_ms = 0
_counter = 0


def new_id() -> str:
    """生成 UUIDv7（RFC 9562）。同一毫秒内用 12 位计数器保证单调递增。"""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2)) & 0x7FF  # 留出递增空间
        else:
            _counter += 1
            if _counter > 0xFFF:  # 计数器用尽：借用下一毫秒
                _last_ms += 1
                _counter = 0
            ms = _last_ms
        rand_b = int.from_bytes(os.urandom(8)) & ((1 << 62) - 1)
        value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | _counter << 64 | 0b10 << 62 | rand_b
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _parse_utc(value: Any) -> Any:
    if isinstance(value, str) and not value.endswith("Z"):
        raise ValueError("时间必须是 UTC，并以 Z 结尾（RFC 3339）")
    return value


def _require_utc(value: datetime) -> datetime:
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("时间必须带 UTC 时区")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, BeforeValidator(_parse_utc), AfterValidator(_require_utc)]
"""UTC 时间。字符串输入必须以 Z 结尾；datetime 输入必须带零时区。序列化为 ...Z。"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def format_utc(value: datetime) -> str:
    """序列化为 RFC 3339 UTC 字符串，以 Z 结尾。"""
    return _require_utc(value).isoformat().replace("+00:00", "Z")
