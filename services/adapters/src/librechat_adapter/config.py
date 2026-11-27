"""LibreChat 源适配器的配置与常量。

通用约定（与提取/界面侧一致，见 services/adapters/ai-docs/librechat-source.md）：
`chat_session`、`chat_message`、`chat_attachment` 三类原始数据，写入方都是 `adapter`，
经和其他适配器相同的 ① AppendRaw 写入；文件字节进入 Ear 私有文件卷，payload 只带 `file_ref`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ADAPTER_VERSION = "librechat-chat/0.1.0"
"""同一 adapter_version 内用确定性 id 和修订号去重；升级它会产生一批新数据，旧数据保留。"""

CHANNEL = "librechat"
EXPORT_FORMAT = "ear-librechat-export/1"
REQUEST_FORMAT = "ear-librechat-export-request/1"

SUPPORTED_LIBRECHAT_VERSIONS: frozenset[str] = frozenset({"0.8.9"})
"""已对照其 data-schemas 核对过字段的 LibreChat 版本；其他版本须显式 --accept-version。"""

_DEVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class LibreChatConfig:
    adapter_version: str = ADAPTER_VERSION
    device: str = "librechat"
    """`source.device`：LibreChat 实例名，只在配置里选择，不从数据里读取。"""
    user: str = "default"
    max_text_chars: int = 200_000
    """单条 chat_message.text 的上限；更长的全文进入私有文件卷，payload 带 text_file_ref。"""
    max_aux_chars: int = 64_000
    max_attachment_text_chars: int = 20_000
    max_blob_bytes: int = 64 * 1024 * 1024
    max_line_bytes: int = 80 * 1024 * 1024
    batch_records: int = 200
    batch_bytes: int = 2 * 1024 * 1024
    supported_versions: frozenset[str] = field(default=SUPPORTED_LIBRECHAT_VERSIONS)

    def __post_init__(self) -> None:
        if not _DEVICE.fullmatch(self.device):
            raise ValueError("device 必须是 1–64 个字母数字、点、下划线或连字符")
        for name in (
            "max_text_chars",
            "max_aux_chars",
            "max_attachment_text_chars",
            "max_blob_bytes",
            "max_line_bytes",
            "batch_records",
            "batch_bytes",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须为正数")
        if self.batch_records > 1000:
            raise ValueError("batch_records 不得超过 1000（① 每批最多 2000 个操作）")
