"""Trusted definitions of opt-in adapter sources (not in DEFAULT_REGISTRY)."""

from ear_contracts.registry import DataTypeSpec, Registry

CHAT_TYPES = ("chat_session", "chat_message", "chat_attachment")


def chat_specs() -> tuple[DataTypeSpec, ...]:
    return (
        DataTypeSpec("chat_session", "raw_record", "adapter", description="聊天会话元数据"),
        DataTypeSpec("chat_message", "raw_record", "adapter", description="聊天消息"),
        DataTypeSpec("chat_attachment", "raw_record", "adapter", description="聊天附件"),
    )


def chat_source_registry() -> Registry:
    """Add only the optional types; never rewrite existing core declarations."""
    return Registry({}).extend_core(*chat_specs())
