"""测试与开发用的造数据工具。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ear_contracts import NewRawRecord, Source, new_id


def ts(value: str) -> datetime:
    """'2026-10-29T06:00:00Z' → 带 UTC 时区的 datetime。"""
    if not value.endswith("Z"):
        raise ValueError("时间必须以 Z 结尾")
    return datetime.fromisoformat(value).astimezone(UTC)


def make_raw(
    data_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    start: datetime | str,
    end: datetime | str | None = None,
    id: str | None = None,
    dedup_key: str | None = None,
    adapter_version: str = "test/0.0.0",
    device: str = "test-device",
    channel: str = "octopus",
    user: str = "default",
) -> NewRawRecord:
    """构造一条待写入的原始数据。end 缺省时为单点数据；dedup_key 缺省时用 id。"""
    record_id = id or new_id()
    start_dt = ts(start) if isinstance(start, str) else start
    end_dt = start_dt if end is None else (ts(end) if isinstance(end, str) else end)
    return NewRawRecord(
        id=record_id,
        data_type=data_type,
        event_start=start_dt,
        event_end=end_dt,
        source=Source(device=device, channel=channel, user=user),
        adapter_version=adapter_version,
        dedup_key=dedup_key or record_id,
        payload=dict(payload or {}),
    )
