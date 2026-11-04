"""① 存储与事件的客户端。

各模块依赖 StorageClient（protocol.py）；开发和测试时用 MemoryStorage（memory.py）。
接口说明见 services/storage/ai-docs/interface.md。
"""

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
    Create,
    DependencyInput,
    MarkProcessed,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    Supersede,
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
    "Conflict",
    "ConflictError",
    "ConflictReason",
    "Context",
    "Create",
    "DeadLetter",
    "DependencyInput",
    "DependencySet",
    "EventHandler",
    "InvalidRequest",
    "MarkProcessed",
    "MemoryBus",
    "MemoryClient",
    "MemoryStorage",
    "OpResult",
    "Outcome",
    "Page",
    "PermissionDenied",
    "PublishReevaluation",
    "PutSystemRecord",
    "Query",
    "Reader",
    "RegisterDependencies",
    "StaleDependency",
    "StorageClient",
    "StorageError",
    "Supersede",
    "TimeRange",
    "Update",
    "WriteOp",
    "WriteResult",
    "iter_query",
    "write_with_retry",
]
