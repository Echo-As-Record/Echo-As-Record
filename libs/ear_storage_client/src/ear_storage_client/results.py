"""写入结果。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Self

from ear_contracts import Event

from ear_storage_client.errors import ConflictError

Outcome = Literal["written", "unchanged", "deduplicated"]
"""一条操作的结果。

- written：已写入（新增、新版本、整体替代、登记、记录等）。
- unchanged：内容和当前相同，没有写入，也没有发事件。
- deduplicated：原始数据的去重键已存在（或同一 event_id 的请求已发布过），没有写入，
  返回已有数据的 id。这是成功，不是冲突。
"""

ConflictReason = Literal[
    "not_found",
    "version_mismatch",
    "object_superseded",
    "id_exists",
    "dedup_key_exists",
    "unique_field_taken",
    "member_taken",
    "stale_dependency",
    "revision_mismatch",
    "already_processed",
]

RETRYABLE: Final[frozenset[str]] = frozenset(
    {
        "not_found",
        "version_mismatch",
        "object_superseded",
        "dedup_key_exists",
        "unique_field_taken",
        "member_taken",
        "stale_dependency",
        "revision_mismatch",
    }
)
"""重新读取当前状态、重新计算后可能成功的冲突。

id_exists、already_processed 通常说明这件事已经做过，不应重试。
"""


@dataclass(frozen=True, slots=True)
class OpResult:
    """一条操作的结果，index 与提交的操作列表一一对应。"""

    index: int
    outcome: Outcome
    data_type: str | None
    id: str | None
    version: int | None
    """有版本的数据：操作后的当前版本；原始数据：1；系统记录：revision；其余为 None。"""
    previous_version: int | None
    """有版本的数据：操作前的版本（新增时为 None）；系统记录：操作前的 revision。"""
    event_id: str | None
    """这条操作发出的事件 id；没有发事件时为 None。"""


@dataclass(frozen=True, slots=True)
class Conflict:
    """整批被拒绝的原因。只报告第一处冲突。"""

    index: int
    reason: ConflictReason
    message: str
    data_type: str | None = None
    id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        return self.reason in RETRYABLE


@dataclass(frozen=True, slots=True)
class WriteResult:
    """一批写入的结果：要么 conflict 为 None 且 results 与操作一一对应，要么整批没有生效。"""

    batch_id: str | None
    results: tuple[OpResult, ...] = ()
    conflict: Conflict | None = None
    events: tuple[Event, ...] = ()
    """本批发出的全部事件（「数据库被修改」与「请求重新判断」），按操作顺序。"""

    @property
    def ok(self) -> bool:
        return self.conflict is None

    def check(self) -> Self:
        """有冲突时抛出 ConflictError，否则返回自身。"""
        if self.conflict is not None:
            raise ConflictError(self.conflict)
        return self

    def __getitem__(self, index: int) -> OpResult:
        return self.results[index]

    def __len__(self) -> int:
        return len(self.results)
