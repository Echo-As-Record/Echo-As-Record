"""Persistent storage, authenticated HTTP clients and leases; relay is not yet implemented."""

from ear_storage.auth import AuthenticationError, AuthOperator
from ear_storage.backend import PostgresClient, PostgresStorage
from ear_storage.repository import Limits
from ear_storage.schema import ProjectionLimit, SchemaMismatch, bootstrap, migrate
from ear_storage.service import AuthenticatedClient, StorageService

__all__ = [
    "AuthOperator",
    "AuthenticatedClient",
    "AuthenticationError",
    "Limits",
    "PostgresClient",
    "PostgresStorage",
    "ProjectionLimit",
    "SchemaMismatch",
    "SnapshotLease",
    "SnapshotLimits",
    "SnapshotUnavailable",
    "StorageService",
    "bootstrap",
    "migrate",
]

from ear_storage.snapshots import SnapshotLease, SnapshotLimits, SnapshotUnavailable
