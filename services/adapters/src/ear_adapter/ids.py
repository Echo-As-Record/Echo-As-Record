"""确定性 id：同一条原始数据每次算出的 id 都一样。

重扫时 ① 对已存在的数据返回旧 id（deduplicated）。如果 id 每次随机生成，同一批里其他记录
引用的会是这次新生成、实际没写进去的 id。所以 id 由「事件时间 + 去重键」确定地生成：
前 48 位是事件时间的毫秒数，其余随机位取自去重键的 SHA-256，版本位和变体位按 UUIDv7 设置。
"""

from __future__ import annotations

import hashlib
from datetime import datetime


def deterministic_id(
    event_start: datetime, data_type: str, adapter_version: str, dedup_key: str
) -> str:
    ms = int(event_start.timestamp() * 1000) & ((1 << 48) - 1)
    digest = hashlib.sha256(f"{data_type}\x1f{adapter_version}\x1f{dedup_key}".encode()).digest()
    rand_a = int.from_bytes(digest[:2]) & 0xFFF
    rand_b = int.from_bytes(digest[2:10]) & ((1 << 62) - 1)
    value = ms << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
