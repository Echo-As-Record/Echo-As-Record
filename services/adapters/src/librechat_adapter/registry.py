"""三类聊天原始数据在 ① 登记表中的定义。

聊天是部署可选类型，不改变默认登记表。此处复用公共契约的类型定义。
"""

from __future__ import annotations

from ear_contracts import DEFAULT_REGISTRY, Registry
from ear_contracts import DataTypeSpec as DataTypeSpec
from ear_contracts.optional_sources import CHAT_TYPES as CHAT_TYPES
from ear_contracts.optional_sources import chat_specs as chat_specs

CHAT_SESSION = "chat_session"
CHAT_MESSAGE = "chat_message"
CHAT_ATTACHMENT = "chat_attachment"


def with_chat_types(registry: Registry = DEFAULT_REGISTRY) -> Registry:
    """补上缺少的聊天类型；已登记的必须是 writer=adapter 的原始数据，否则报错。"""
    missing: list[DataTypeSpec] = []
    for spec in chat_specs():
        if spec.name not in registry:
            missing.append(spec)
            continue
        have = registry.get(spec.name)
        if (have.kind, have.writer) != (spec.kind, spec.writer):
            raise ValueError(f"{spec.name} 已登记为 {have.kind}/{have.writer}，与本适配器不一致")
    return registry.extend_core(*missing) if missing else registry
