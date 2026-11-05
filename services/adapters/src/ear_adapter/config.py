"""适配器配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ADAPTER_VERSION = "ear-audio/0.1.0"
"""同一 adapter_version 下每段录音只导入一次（第一次完整跑完的结果）。

旧 Ear 重新处理同一段录音时，Evidence id 会变；不跟着重导，避免同一时间出现两份转写。
要换成新结果，升级这个版本号，显式重新导入一批（设计 3.3：重新处理产生新批次，旧数据保留）。
"""


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    adapter_version: str = ADAPTER_VERSION
    channel: str = "octopus"
    user: str = "default"
    data_root: Path = field(default_factory=lambda: Path("~/.octopus/local").expanduser())
    """file_ref.path 相对于这个目录（术语表「大文件引用」）。"""
