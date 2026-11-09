"""Remote-only access, with current trusted identity/role checks before every use."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from ear_contracts import is_id
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import (
    AuthenticationError,
    ProtocolError,
    ResourceLimit,
    ServiceUnavailable,
)

from ear_api.config import Binding, Settings
from ear_api.models import bound_json, load_json_object


class GatewayError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code
        super().__init__(code)


class Upstream:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings, self.transport = settings, transport

    @contextmanager
    def _http(self, binding: Binding) -> Iterator[httpx.Client]:
        with httpx.Client(
            base_url=self.settings.storage_url,
            timeout=self.settings.timeout,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport
            if self.transport is not None
            else httpx.HTTPTransport(retries=0),
            headers={"Authorization": "Bearer " + binding.storage_token.get_secret_value()},
        ) as client:
            yield client

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        raw = bytearray()
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > 4096:
                raise ValueError("response limit")
            raw.extend(chunk)
        return load_json_object(raw, max_bytes=4096, authority=False)

    def _identity(self, binding: Binding) -> str | None:
        # Deployed wire-v1 exposes codec/module only. The protected deployment binding
        # is the authority for the user's local API role; storage rechecks its own
        # trusted admin on every management request. Do not invent identity fields.
        # If a future server supplies principal/admin metadata, validate it strictly.
        try:
            with self._http(binding) as client, client.stream("GET", "/v1/identity") as response:
                if response.status_code == 401:
                    raise AuthenticationError("unauthenticated")
                if response.status_code == 403:
                    raise PermissionDenied("forbidden")
                if response.status_code != 200:
                    raise ServiceUnavailable("identity unavailable")
                data = self._object(response)
            if type(data.get("codec")) is not int or data["codec"] != 1:
                raise ValueError("invalid identity")
            if data.get("module") != "api":
                raise PermissionDenied("identity module mismatch")
            if "admin" in data:
                if type(data["admin"]) is not bool:
                    raise ValueError("invalid identity role")
                if data["admin"] != (binding.role == "admin"):
                    raise PermissionDenied("identity role mismatch")
            principal = data.get("principal_id")
            if "principal_id" in data and not is_id(principal):
                raise ValueError("invalid identity principal")
            return principal
        except httpx.HTTPError:
            raise ServiceUnavailable("identity unavailable") from None
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ProtocolError("trusted identity unavailable") from None

    def _configured(self, binding: Binding) -> None:
        if binding not in self.settings.bindings:
            raise PermissionDenied("unconfigured binding")

    def _verify(self, binding: Binding) -> None:
        self._configured(binding)
        principal_id = self._identity(binding)
        if principal_id is not None:
            for peer in self.settings.bindings:
                if peer == binding:
                    continue
                try:
                    other_id = self._identity(peer)
                except (AuthenticationError, PermissionDenied, ServiceUnavailable, ProtocolError):
                    # Revoking an unrelated user's binding must not lock out an admin.
                    continue
                if other_id == principal_id:
                    raise PermissionDenied("aliased identity bindings")

    @contextmanager
    def client(self, binding: Binding) -> Iterator[RemoteStorageClient]:
        self._verify(binding)
        with RemoteStorageClient(
            binding.storage_token.get_secret_value(),
            self.settings.storage_url,
            timeout=self.settings.timeout,
            transport=self.transport,
        ) as client:
            yield client

    def plugin_directory(
        self, binding: Binding, *, limit: int = 50, after: str = ""
    ) -> dict[str, Any]:
        self._configured(binding)
        if binding.role != "admin":
            raise PermissionDenied("admin required")
        # Storage rechecks live API/admin authority on this read, even when the
        # identity response only exposes module (wire-v1). Never use registry here.
        with self.client(binding) as client:
            return client.list_plugin_directory(limit=limit, after=after)

    def management(
        self,
        binding: Binding,
        path: str,
        body: dict[str, Any],
        *,
        claim: bool = False,
    ) -> dict[str, Any]:
        self._configured(binding)
        if binding.role != "admin":
            raise PermissionDenied("admin required")
        if claim:
            parts = path.split("/")
            if (
                len(parts) != 6
                or parts[:4] != ["", "v1", "management", "credentials"]
                or not is_id(parts[4])
                or parts[5] != "claim"
                or body != {}
            ):
                raise InvalidRequest("invalid claim")
        elif path != "/v1/management/requests":
            raise InvalidRequest("invalid management path")
        bound_json(body)
        self._verify(binding)
        # No retries, redirects, environment proxies, cache or body/error logging.
        # Once POST begins, malformed/oversized/lost replies are outcome UNKNOWN,
        # including single-use credential delivery. Never suggest automatic replay.
        unknown = "credential_delivery_unknown" if claim else "management_acceptance_unknown"
        try:
            with (
                self._http(binding) as client,
                client.stream("POST", path, json=body) as response,
            ):
                if response.status_code == 401:
                    raise AuthenticationError("unauthenticated")
                if response.status_code == 403:
                    raise PermissionDenied("forbidden")
                if not claim and response.status_code == 422:
                    raise InvalidRequest("invalid input")
                if not claim and response.status_code == 413:
                    raise ResourceLimit("resource limit")
                if response.status_code != (200 if claim else 202):
                    raise GatewayError(503, unknown)
                data = self._object(response)
            if claim:
                token = data.get("token")
                if (
                    not is_id(data.get("principal_id"))
                    or not isinstance(token, str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None
                ):
                    raise ValueError("invalid delivery")
                return {"principal_id": data["principal_id"], "token": token}
            if data.get("status") != "accepted" or not all(
                is_id(data.get(key)) for key in ("feedback_id", "result_id")
            ):
                raise ValueError("invalid acceptance")
            return {key: data[key] for key in ("status", "feedback_id", "result_id")}
        except (httpx.HTTPError, ValueError, TypeError, RecursionError, OverflowError):
            raise GatewayError(503, unknown) from None

    def ready(self) -> bool:
        try:
            principals = [self._identity(binding) for binding in self.settings.bindings]
            known = [value for value in principals if value is not None]
            return len(set(known)) == len(known)
        except (AuthenticationError, PermissionDenied, ServiceUnavailable, ProtocolError):
            return False
