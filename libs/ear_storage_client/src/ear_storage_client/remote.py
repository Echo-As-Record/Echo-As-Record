"""Synchronous HTTP wire-v1 SDK. No environment, implicit grants or automatic retry.

Server-derived module/registry are informational, not authorization capabilities.
Every request authenticates on the server, including every leased read and replay.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import Any

import httpx
from ear_contracts import (
    DependencyRecord,
    Record,
    Registry,
    SemanticRecord,
    SystemRecord,
    is_id,
    new_id,
)

from ear_storage_client import Context, InvalidRequest, Query, WriteOp, WriteResult
from ear_storage_client.engine import canonical_json
from ear_storage_client.errors import PermissionDenied
from ear_storage_client.queries import DependencySet, Page, StaleDependency
from ear_storage_client.remote_errors import (
    AuthenticationError,
    CommitUnknown,
    ProtocolError,
    ResourceLimit,
    ServiceUnavailable,
    SnapshotUnavailable,
)
from ear_storage_client.wire import (
    READ_ARGS,
    _value,
    decode_read,
    decode_read_result,
    decode_result,
    encode_request,
    load_json,
)


class RemoteReader:
    def __init__(self, client: RemoteStorageClient, snapshot_id: str | None = None) -> None:
        self._client, self.snapshot_id = client, snapshot_id

    def _read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        names = tuple(READ_ARGS[method].model_fields)
        values = {**dict(zip(names, args, strict=False)), **kwargs}
        body = {
            "codec": 1,
            "method": method,
            "args": _value(values),
            "snapshot_id": self.snapshot_id,
        }
        # Validate locally too; unknown command fields are never silently discarded.
        decode_read(canonical_json(body))
        response = self._client._request("POST", "/v1/read", canonical_json(body))
        try:
            return decode_read_result(method, response.text)
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("invalid read response") from None

    def get(self, id: str) -> Record | None:
        return self._read("get", id)

    def get_version(self, id: str, version: int) -> Record | None:
        return self._read("get_version", id, version)

    def history(self, id: str) -> list[SemanticRecord]:
        return self._read("history", id)

    def get_many(self, ids: Sequence[str]) -> dict[str, Record]:
        return self._read("get_many", ids)

    def get_system_record(self, id: str) -> SystemRecord | None:
        return self._read("get_system_record", id)

    def system_history(self, id: str) -> list[SystemRecord]:
        return self._read("system_history", id)

    def query(self, query: Query) -> Page:
        return self._read("query", query)

    def count(self, query: Query) -> int:
        return self._read("count", query)

    def dependency_set(self, dependent_id: str, version: int | None = None) -> DependencySet | None:
        return self._read("dependency_set", dependent_id, version)

    def dependencies_of(
        self, dependent_id: str, version: int | None = None
    ) -> list[DependencyRecord]:
        found = self.dependency_set(dependent_id, version)
        return [] if found is None else list(found.dependencies)

    def dependents_of(
        self, target_id: str, *, only_current_dependents: bool = True
    ) -> list[DependencyRecord]:
        return self._read(
            "dependents_of", target_id, only_current_dependents=only_current_dependents
        )

    def stale_dependencies(
        self, targets: Sequence[str], *, only_current_dependents: bool = True
    ) -> list[StaleDependency]:
        return self._read(
            "stale_dependencies", targets, only_current_dependents=only_current_dependents
        )

    def is_processed(self, consumer: str, event_id: str) -> bool:
        return self._read("is_processed", consumer, event_id)


class RemoteStorageClient(RemoteReader):
    def __init__(
        self,
        token: str,
        base_url: str,
        module: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 35.0,
    ) -> None:
        # module is only an optional expectation, never sent to the server.
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            raise AuthenticationError("invalid credentials")
        try:
            url = httpx.URL(base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.host
                or url.userinfo
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
            ):
                raise ValueError
            if not math.isfinite(timeout) or not 0 < timeout <= 120:
                raise ValueError
        except (ValueError, TypeError, httpx.InvalidURL):
            raise InvalidRequest("invalid service endpoint or timeout") from None
        self._http = httpx.Client(
            base_url=url,
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
            trust_env=False,
            follow_redirects=False,
            timeout=timeout,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        super().__init__(self)
        if module is not None:
            try:
                if self.module != module:
                    raise PermissionDenied("authenticated module does not match expectation")
            except Exception:
                self.close()
                raise

    def __repr__(self) -> str:
        return "<RemoteStorageClient credentials redacted>"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RemoteStorageClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(
        self, method: str, path: str, text: str | None = None, *, request_id: str | None = None
    ) -> httpx.Response:
        try:
            response = self._http.request(method, path, content=text)
        except httpx.HTTPError:
            if request_id is not None:
                raise CommitUnknown(request_id) from None
            raise ServiceUnavailable("storage transport unavailable") from None
        errors = {
            401: (AuthenticationError, "invalid credentials"),
            403: (PermissionDenied, "access denied"),
            422: (InvalidRequest, "invalid request"),
            410: (SnapshotUnavailable, "snapshot unavailable"),
            413: (ResourceLimit, "resource limit exceeded"),
        }
        if response.status_code in errors:
            kind, message = errors[response.status_code]
            raise kind(message)
        if response.status_code != 200:
            # 503 may have happened after commit. Never claim it is safe to re-plan.
            if request_id is not None:
                raise CommitUnknown(request_id)
            raise ServiceUnavailable("storage service unavailable")
        return response

    def _metadata(self, path: str) -> dict[str, Any]:
        response = self._request("GET", path)
        try:
            data = load_json(response.text)
            if type(data["codec"]) is not int or data["codec"] != 1:
                raise ValueError
            return data
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("invalid metadata response") from None

    @property
    def module(self) -> str:
        value = self._metadata("/v1/identity").get("module")
        if not isinstance(value, str):
            raise ProtocolError("invalid identity response")
        return value

    @property
    def registry(self) -> Registry:
        data = self._metadata("/v1/registry")
        try:
            registry = Registry.from_json(data["registry"])
            # Only authenticated server metadata restores trusted provenance;
            # ordinary Registry.from_json intentionally does not trust core flags.
            return Registry(
                registry.data_types, registry.event_publishers, frozenset(data["core_types"])
            )
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("invalid registry response") from None

    def write(
        self, ops: Sequence[WriteOp], context: Context, *, request_id: str | None = None
    ) -> WriteResult:
        request_id = new_id() if request_id is None else request_id
        if not is_id(request_id):
            raise InvalidRequest("invalid request_id")
        data = load_json(encode_request(tuple(ops), context))
        data["request_id"] = request_id
        response = self._request("POST", "/v1/batches", canonical_json(data), request_id=request_id)
        try:
            return decode_result(response.text)
        except (ValueError, KeyError, TypeError):
            # Includes unknown future ConflictReason/Outcome Literals.
            raise CommitUnknown(request_id) from None

    def create_snapshot(self, *, ttl_seconds: int | None = None) -> dict[str, Any]:
        response = self._request(
            "POST", "/v1/snapshots", canonical_json({"codec": 1, "ttl_seconds": ttl_seconds})
        )
        try:
            data = load_json(response.text)
            if type(data["codec"]) is not int or data["codec"] != 1:
                raise ValueError
            lease = data["lease"]
            if not is_id(lease["snapshot_id"]):
                raise ValueError
            return lease
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("invalid snapshot response") from None

    def read_snapshot(self, snapshot_id: str) -> RemoteReader:
        if not is_id(snapshot_id):
            raise InvalidRequest("invalid snapshot id")
        return RemoteReader(self, snapshot_id)

    def release_snapshot(self, snapshot_id: str) -> None:
        if not is_id(snapshot_id):
            raise InvalidRequest("invalid snapshot id")
        self._request("DELETE", "/v1/snapshots/" + snapshot_id)

    @contextmanager
    def snapshot(self) -> Iterator[RemoteReader]:
        lease = self.create_snapshot()
        try:
            yield self.read_snapshot(lease["snapshot_id"])
        finally:
            # Expired/revoked leases are swept by deployment maintenance. A release
            # transport failure must not mask a primary error or trigger re-planning.
            with suppress(SnapshotUnavailable, AuthenticationError, ServiceUnavailable):
                self.release_snapshot(lease["snapshot_id"])

    def subscribe(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("remote subscribe requires P2 relay and delivery protocol")
