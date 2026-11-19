"""Validate server coverage, never infer it from a filtered registry/empty graph."""

from typing import Any, Protocol, cast

from ear_storage_client import PermissionDenied, StorageClient
from ear_storage_client.remote_errors import AuthenticationError, ProtocolError


class SchedulerBlocked(RuntimeError):
    """A visible fail-closed scheduling condition, not an empty successful graph."""


class StatePort(Protocol):
    def scheduler_state(self) -> dict[str, Any]: ...


def state(client: StorageClient) -> dict[str, Any]:
    method = getattr(client, "scheduler_state", None)
    if not callable(method):
        raise SchedulerBlocked("scheduler_authority_unavailable")
    try:
        value = cast(StatePort, client).scheduler_state()
    except (PermissionDenied, AuthenticationError):
        raise SchedulerBlocked("coverage_denied") from None
    except ProtocolError:
        raise SchedulerBlocked("scheduler_state_invalid") from None
    if (
        not isinstance(value, dict)
        or type(value.get("codec")) is not int
        or value["codec"] != 1
        or value.get("coverage_complete") is not True
        or not isinstance(value.get("coverage_revision"), str)
        or not value["coverage_revision"]
        or not isinstance(value.get("owners"), dict)
    ):
        raise SchedulerBlocked("coverage_incomplete")
    for module, owner in value["owners"].items():
        if (
            not isinstance(module, str)
            or not isinstance(owner, dict)
            or set(owner) != {"enabled", "can_receive", "revision", "online"}
            or type(owner["enabled"]) is not bool
            or type(owner["can_receive"]) is not bool
            or owner["online"] is not None
            or (owner["can_receive"] and not owner["enabled"])
            or (
                owner["revision"] is not None
                and (type(owner["revision"]) is not int or owner["revision"] < 1)
            )
        ):
            raise SchedulerBlocked("scheduler_state_invalid")
    return value


def owner_reason(value: dict[str, Any], module: str | None) -> str | None:
    owner = value["owners"].get(module)
    if owner is None:
        return "owner_unknown"
    if not owner["enabled"]:
        return "owner_disabled"
    if not owner["can_receive"]:
        return "owner_unauthorized"
    return None
