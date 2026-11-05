"""① 存储与事件的客户端。

各模块依赖 StorageClient（protocol.py）；开发和测试时用 MemoryStorage（memory.py）。
接口说明见 services/storage/ai-docs/interface.md。
"""

from ear_contracts import TracedCause

from ear_storage_client.completion import reevaluation_result
from ear_storage_client.context import Context
from ear_storage_client.errors import (
    ConflictError,
    InvalidRequest,
    PermissionDenied,
    StorageError,
)
from ear_storage_client.memory import DeadLetter, MemoryBus, MemoryClient, MemoryStorage
from ear_storage_client.ops import (
    AppendRaw,
    AssertTypeRevision,
    Create,
    DependencyInput,
    FinishReevaluation,
    MarkProcessed,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
    TypeRevision,
    Update,
    WriteOp,
)
from ear_storage_client.protocol import (
    EventHandler,
    Reader,
    StorageClient,
    iter_query,
    write_with_retry,
)
from ear_storage_client.queries import (
    MAX_LIMIT,
    DependencySet,
    Page,
    Query,
    StaleDependency,
    TimeRange,
)
from ear_storage_client.remote_errors import (
    AuthenticationError,
    CommitUnknown,
    DeliveryExpired,
    ProtocolError,
    ResourceLimit,
    ServiceUnavailable,
    SnapshotUnavailable,
)
from ear_storage_client.results import (
    RETRYABLE,
    Conflict,
    ConflictReason,
    OpResult,
    Outcome,
    WriteResult,
)

__all__ = [
    "MAX_LIMIT",
    "RETRYABLE",
    "AppendRaw",
    "AssertTypeRevision",
    "AuthenticationError",
    "CommitUnknown",
    "Conflict",
    "ConflictError",
    "ConflictReason",
    "Context",
    "Create",
    "DeadLetter",
    "DeliveryExpired",
    "DependencyInput",
    "DependencySet",
    "EventHandler",
    "FinishReevaluation",
    "InvalidRequest",
    "MarkProcessed",
    "MemoryBus",
    "MemoryClient",
    "MemoryStorage",
    "OpResult",
    "Outcome",
    "Page",
    "PermissionDenied",
    "ProtocolError",
    "PublishReevaluation",
    "PutSystemRecord",
    "Query",
    "Reader",
    "RegisterDependencies",
    "ResourceLimit",
    "ServiceUnavailable",
    "SnapshotUnavailable",
    "StaleDependency",
    "StorageClient",
    "StorageError",
    "Supersede",
    "TimeRange",
    "TracedCause",
    "TypeRevision",
    "Update",
    "WriteOp",
    "WriteResult",
    "iter_query",
    "reevaluation_result",
    "write_with_retry",
]
