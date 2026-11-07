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
    ReevaluationResult,
    Registry,
    SemanticRecord,
    SystemRecord,
    is_id,
    new_id,
)

from ear_storage_client import Context, InvalidRequest, Query, WriteOp, WriteResult
from ear_storage_client.consumer_wire import RegisterConsumer
from ear_storage_client.engine import canonical_json
from ear_storage_client.errors import ConflictError, PermissionDenied
from ear_storage_client.preflight import LLMCallPreflight
from ear_storage_client.protocol import EventHandler
from ear_storage_client.queries import DependencySet, Page, StaleDependency
from ear_storage_client.remote_errors import (
    AuthenticationError,
    CommitUnknown,
    DeliveryExpired,
    ProtocolError,
    ResourceLimit,
    ServiceUnavailable,
    SnapshotUnavailable,
)
from ear_storage_client.wire import (
    EVENT_ADAPTER,
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

    def type_revision(self, data_type: str) -> int:
        return self._read("type_revision", data_type)

    def get(self, id: str) -> Record | None:
        return self._read("get", id)

    def get_version(self, id: str, version: int) -> Record | None:
        return self._read("get_version", id, version)

    def history(self, id: str) -> list[SemanticRecord]:
        return self._read("history", id)

    def get_many(self, ids: Sequence[str]) -> dict[str, Record]:
        return self._read("get_many", ids)

    def get_reevaluation_result(self, request_event_id: str) -> ReevaluationResult | None:
        return self._read("get_reevaluation_result", request_event_id)

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
        self._handlers: dict[str, EventHandler] = {}
        self._completions: dict[str, tuple[str, str]] = {}
        super().__init__(self)
        if module is not None:
            try:
                if self.module != module:
                    raise PermissionDenied("authenticated module does not match expectation")
            except Exception:
                self.close()
                raise

    def call_id_occupied(self, call_id: str) -> bool:
        """Gateway-only authoritative occupancy; old servers fail closed, no fallback."""
        import json

        from ear_contracts import is_id

        if not is_id(call_id):
            raise InvalidRequest("invalid call id")
        response = self._request("POST", "/v1/llm/call-id-check", json.dumps({"call_id": call_id}))
        value = load_json(response.text)
        if (
            not isinstance(value, dict)
            or set(value) != {"occupied"}
            or type(value["occupied"]) is not bool
        ):
            raise ProtocolError("invalid occupancy response")
        return value["occupied"]

    def preflight_llm_call(self, call: LLMCallPreflight) -> LLMCallPreflight:
        call = LLMCallPreflight.model_validate(call.model_dump())
        response = self._request("POST", "/v1/llm/preflight", call.model_dump_json())
        try:
            result = LLMCallPreflight.model_validate(load_json(response.text))
            if result.model_dump(exclude={"additional_causes"}) != call.model_dump(
                exclude={"additional_causes"}
            ) or any(t not in result.additional_causes for t in call.additional_causes):
                raise ValueError
            return result
        except (InvalidRequest, ValueError, KeyError, TypeError):
            raise ProtocolError("invalid preflight response") from None

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
        if response.status_code == 409 and path in {"/v1/consumers/ack", "/v1/consumers/nack"}:
            try:
                data = load_json(response.text)
                if not isinstance(data, dict):
                    raise ValueError
            except (InvalidRequest, ValueError):
                raise ProtocolError("invalid completion error") from None
            if data.get("code") == "delivery_expired":
                raise DeliveryExpired("delivery expired")
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

    def list_plugin_directory(self, *, limit: int = 50, after: str = "") -> dict[str, Any]:
        """Admin-only metadata; no fallback to the business ACL-filtered registry."""
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or not isinstance(after, str)
            or (after and re.fullmatch(r"[a-z][a-z0-9_]{0,54}", after) is None)
        ):
            raise InvalidRequest("invalid directory pagination")
        data = self._metadata(f"/v1/management/plugins?limit={limit}&after={after}")
        try:
            items, following = data["items"], data["next_after"]
            if not isinstance(items, list) or len(items) > limit:
                raise ValueError
            names = []
            for item in items:
                name = item["module"]
                if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,54}", name) is None:
                    raise ValueError
                if name <= (names[-1] if names else after):
                    raise ValueError
                names.append(name)
                if (
                    (item["enabled"] is not None and type(item["enabled"]) is not bool)
                    or item["online"] is not None
                    or item["status_source"] != "storage_management"
                    or not isinstance(item["types"], list)
                    or len(item["types"]) > 128
                ):
                    raise ValueError
                allowed = {
                    "module",
                    "types",
                    "enabled",
                    "online",
                    "status_source",
                    "revision",
                    "resources_ready",
                    "ever_enabled",
                    "need_recovery_decision",
                    "read_types",
                    "subscribe_types",
                }
                if not set(item) <= allowed:
                    raise ValueError
                for key in ("resources_ready", "ever_enabled", "need_recovery_decision"):
                    if key in item and type(item[key]) is not bool:
                        raise ValueError
                if "revision" in item and (
                    type(item["revision"]) is not int or item["revision"] < 1
                ):
                    raise ValueError
                for key in ("read_types", "subscribe_types"):
                    if key in item and (
                        not isinstance(item[key], list)
                        or len(item[key]) > 512
                        or any(not isinstance(v, str) or len(v) > 128 for v in item[key])
                    ):
                        raise ValueError
                for spec in item["types"]:
                    if (
                        set(spec)
                        != {"name", "kind", "supports_reevaluation", "requires_dependencies"}
                        or not isinstance(spec["name"], str)
                        or len(spec["name"]) > 128
                        or spec["kind"] not in {"raw_record", "semantic", "rule", "system_record"}
                        or type(spec["supports_reevaluation"]) is not bool
                        or type(spec["requires_dependencies"]) is not bool
                    ):
                        raise ValueError
            if following is not None and (len(names) != limit or following != names[-1]):
                raise ValueError
            return {"items": items, "next_after": following}
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("invalid plugin directory response") from None

    def list_history_reevaluations(
        self, job_id: str | None = None, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        return load_json(
            self._request(
                "POST",
                "/v1/history/reevaluations/list",
                canonical_json({"job_id": job_id, "limit": limit, "cursor": cursor}),
            ).text
        )

    def attach_history_request(
        self, job_id: str, item_id: str, request_event_id: str
    ) -> dict[str, Any]:
        if not all(is_id(x) for x in (job_id, item_id, request_event_id)):
            raise InvalidRequest("invalid history assignment")
        return load_json(
            self._request(
                "POST",
                "/v1/history/jobs/" + job_id + "/requests",
                canonical_json({"item_id": item_id, "request_event_id": request_event_id}),
            ).text
        )

    def claim_history_assignment(self, job_id: str, *, consumer: str) -> dict[str, Any]:
        if not is_id(job_id):
            raise InvalidRequest("invalid history job")
        return load_json(
            self._request(
                "POST",
                "/v1/history/jobs/" + job_id + "/assignments/claim",
                canonical_json({"consumer": consumer}),
            ).text
        )

    def history_job(self, job_id: str) -> dict[str, Any]:
        if not is_id(job_id):
            raise InvalidRequest("invalid job id")
        return load_json(self._request("GET", "/v1/history/jobs/" + job_id).text)

    def claim_history_job(self, job_id: str) -> dict[str, Any]:
        if not is_id(job_id):
            raise InvalidRequest("invalid job id")
        return load_json(self._request("POST", "/v1/history/jobs/" + job_id + "/claim", "{}").text)

    def commit_history_job(
        self,
        job_id: str,
        claim_token: str,
        fence: int,
        ops: Sequence[WriteOp],
        context: Context,
        *,
        request_id: str,
        outcome: str = "completed",
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if not is_id(job_id) or not is_id(request_id):
            raise InvalidRequest("invalid history request id")
        body = {
            "claim_token": claim_token,
            "fence": fence,
            "request_id": request_id,
            "batch": load_json(encode_request(tuple(ops), context)),
            "outcome": outcome,
            "error_code": error_code,
        }
        response = self._request(
            "POST",
            "/v1/history/jobs/" + job_id + "/commit",
            canonical_json(body),
            request_id=request_id,
        )
        try:
            result = load_json(response.text)
            if result["status"] not in {"completed", "failed", "blocked", "conflict"}:
                raise ValueError
            return result
        except (InvalidRequest, ValueError, KeyError, TypeError):
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

    def _consumer(self, operation: str, consumer: str, **values: Any) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/consumers/" + operation,
            canonical_json({"codec": 1, "consumer": consumer, **values}),
        )
        try:
            data = load_json(response.text)
            if type(data.get("codec")) is not int or data["codec"] != 1:
                raise ValueError
            statuses = {
                "register": {"ready", "pending"},
                "fetch": {"delivery", "idle", "busy", "pending", "progress"},
                "ack": {"acked"},
                "nack": {"retry", "dead"},
                "replay": {"queued"},
            }
            if operation == "deadletters":
                if not isinstance(data.get("deadletters"), list):
                    raise ValueError
            elif data.get("status") not in statuses[operation]:
                raise ValueError
            if operation == "fetch" and (
                "delivery" not in data
                or (data["status"] == "delivery") != isinstance(data["delivery"], dict)
            ):
                raise ValueError
            if operation == "replay" and not is_id(data.get("delivery_id")):
                raise ValueError
            return data
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProtocolError("invalid consumer response") from None

    def subscribe(
        self,
        consumer: str,
        handler: EventHandler,
        *,
        event_types: Sequence[str] | None = None,
        from_beginning: bool = False,
    ) -> None:
        """Register only; explicit poll_once/run_until_idle drives handlers, no threads.

        Handlers MUST read current state and call WriteResult.check() after atomically
        writing results + MarkProcessed. Normal return acks; a swallowed write conflict
        cannot be detected by this SDK. Registration pending raises ServiceUnavailable;
        repeating the same registration resumes the persisted server intent.
        """
        if consumer in self._handlers or not callable(handler):
            raise InvalidRequest("consumer already registered or invalid handler")
        try:
            parsed = RegisterConsumer.model_validate(
                {
                    "consumer": consumer,
                    "event_types": list(event_types)
                    if event_types is not None
                    else ["data_changed", "reevaluation_requested"],
                    "from_beginning": from_beginning,
                }
            )
        except ValueError:
            raise InvalidRequest("invalid consumer registration") from None
        data = self._consumer(
            "register", consumer, event_types=parsed.event_types, from_beginning=from_beginning
        )
        if data.get("status") != "ready":
            raise ServiceUnavailable("consumer initialization pending; retry registration")
        self._handlers[consumer] = handler

    def _poll(self, consumer: str) -> tuple[int, bool]:
        if consumer not in self._handlers:
            raise InvalidRequest("consumer has no local handler")
        completion = self._completions.get(consumer)
        if completion is not None:
            operation, delivery_token = completion
            # Only this authenticated, explicitly expired receipt is discarded.
            with suppress(DeliveryExpired):
                self._consumer(operation, consumer, delivery_token=delivery_token)
            del self._completions[consumer]
            return 0, True
        data = self._consumer("fetch", consumer)
        delivery = data.get("delivery")
        if delivery is None:
            return 0, data.get("status") == "progress"
        try:
            event = EVENT_ADAPTER.validate_python(delivery["event"])
            delivery_token = delivery["delivery_token"]
            if (
                not isinstance(delivery_token, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", delivery_token) is None
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("invalid delivery response") from None
        operation = "ack"
        try:
            if not self.is_processed(consumer, event.event_id):
                self._handlers[consumer](event)
        except ConflictError as exc:
            if exc.conflict.reason != "already_processed" or not self.is_processed(
                consumer, event.event_id
            ):
                operation = "nack"
        except Exception:
            operation = "nack"
        # Retain receipt until HTTP completion succeeds; lost ack response never
        # reruns a handler within this client. Restart relies on committed markers.
        self._completions[consumer] = (operation, delivery_token)
        # Next poll rechecks processed state; never replay business writes here.
        with suppress(DeliveryExpired):
            self._consumer(operation, consumer, delivery_token=delivery_token)
        del self._completions[consumer]
        return 1, True

    def poll_once(self, consumer: str | None = None) -> int:
        """At most one delivery per selected consumer. Exceptions persist a server retry."""
        return sum(
            self._poll(name)[0] for name in ([consumer] if consumer else list(self._handlers))
        )

    def run_until_idle(self, max_deliveries: int = 100) -> int:
        """Bounded round-robin pump; returns on idle/backoff, never sleeps or spins forever.

        max_deliveries also bounds polling rounds (e.g. filtering a historic backlog).
        A delivery is never fetched then dropped when the budget is exhausted.
        """
        if type(max_deliveries) is not int or not 1 <= max_deliveries <= 10000:
            raise InvalidRequest("invalid delivery budget")
        count = 0
        for _ in range(max_deliveries):
            progress = False
            for name in list(self._handlers):
                if count >= max_deliveries:
                    return count
                delivered, advanced = self._poll(name)
                count += delivered
                progress |= advanced
            if not progress:
                break
        return count

    def deadletters(self, consumer: str) -> list[dict[str, Any]]:
        return self._consumer("deadletters", consumer)["deadletters"]

    def replay_deadletter(self, consumer: str, delivery_id: str) -> str:
        if not is_id(delivery_id):
            raise InvalidRequest("invalid delivery id")
        return self._consumer("replay", consumer, delivery_id=delivery_id)["delivery_id"]
