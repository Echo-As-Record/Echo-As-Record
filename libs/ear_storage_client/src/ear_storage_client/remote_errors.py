"""Sanitized transport errors, independent of the server distribution."""

from ear_storage_client.errors import PermissionDenied, StorageError


class AuthenticationError(StorageError):
    pass


class SnapshotUnavailable(PermissionDenied):
    pass


class ResourceLimit(StorageError):
    pass


class ServiceUnavailable(StorageError):
    pass


class ProtocolError(StorageError):
    pass


class CommitUnknown(StorageError):
    """Do not re-plan automatically. Replay the SAME request_id, ops and context.

    No hidden retries. Receipt retention is a deployment concern; P1 currently
    does not prune idempotency receipts. Unknown future outcomes also fail closed.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"commit outcome unknown; replay identical request {request_id}")
