"""LibreChat 源适配器（可选适配器，不是分析插件）。

把一个个人账号的 LibreChat 会话、消息和附件字节导入 Ear：文件字节进私有文件卷，
`chat_session` / `chat_message` / `chat_attachment` 原始记录经与其他适配器相同的
AppendRaw 写入 ①。来源侧是只读、版本核对的导出器（tools/librechat_export.mjs）。
"""

from librechat_adapter.config import ADAPTER_VERSION, LibreChatConfig
from librechat_adapter.engine import Ingestor, IngestReport
from librechat_adapter.registry import (
    CHAT_ATTACHMENT,
    CHAT_MESSAGE,
    CHAT_SESSION,
    CHAT_TYPES,
    with_chat_types,
)
from librechat_adapter.runner import Runner
from librechat_adapter.volume import PrivateVolume

__all__ = [
    "ADAPTER_VERSION",
    "CHAT_ATTACHMENT",
    "CHAT_MESSAGE",
    "CHAT_SESSION",
    "CHAT_TYPES",
    "IngestReport",
    "Ingestor",
    "LibreChatConfig",
    "PrivateVolume",
    "Runner",
    "with_chat_types",
]
