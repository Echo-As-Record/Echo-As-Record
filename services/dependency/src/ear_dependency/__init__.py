"""⑤ bounded memory core and explicit-grant single-worker remote slice."""

from ear_dependency.journal import CommitJournal
from ear_dependency.remote import RemoteDependencyService
from ear_dependency.service import DependencyService

__all__ = ["CommitJournal", "DependencyService", "RemoteDependencyService"]
