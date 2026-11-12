"""Scheduler SDK port: strict response validation, no fallback or cached proof."""

import copy
from typing import Any

import httpx
import pytest
from ear_storage_client import PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import ProtocolError, ServiceUnavailable


def state() -> dict[str, Any]:
    return dict(
        codec=1,
        coverage_complete=True,
        coverage_revision="current-grants",
        owners={"sample": dict(enabled=True, can_receive=True, revision=1, online=None)},
    )


def test_scheduler_port_exact_shape_and_fresh_get():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=state())

    with RemoteStorageClient(
        "a" * 43, "http://storage.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        assert client.scheduler_state() == state()
        assert client.scheduler_state() == state()
    assert len(calls) == 2
    assert all(
        r.method == "GET" and r.url.path == "/v1/scheduler/state" and not r.content for r in calls
    )


@pytest.mark.parametrize(
    "status,error", [(403, PermissionDenied), (404, ServiceUnavailable), (503, ServiceUnavailable)]
)
def test_scheduler_never_falls_back_to_partial_registry(status, error):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(status)

    with (
        RemoteStorageClient(
            "a" * 43, "http://storage.invalid", transport=httpx.MockTransport(handle)
        ) as client,
        pytest.raises(error),
    ):
        client.scheduler_state()
    assert calls == ["/v1/scheduler/state"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"codec": True},
        {"codec": 2},
        {"coverage_complete": False},
        {"coverage_complete": 1},
        {"coverage_revision": ""},
        {"coverage_revision": None},
        {"owners": []},
        {"token": "SECRET"},
        {"owners": {"bad.name": dict(enabled=True, can_receive=True, revision=1, online=None)}},
    ],
)
def test_scheduler_invalid_envelope(mutation):
    value = state() | mutation
    with (
        RemoteStorageClient(
            "a" * 43,
            "http://storage.invalid",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=value)),
        ) as client,
        pytest.raises(ProtocolError),
    ):
        client.scheduler_state()


@pytest.mark.parametrize(
    "mutation",
    [
        {"token": "SECRET"},
        {"online": True},
        {"enabled": "true"},
        {"can_receive": 1},
        {"revision": True},
        {"revision": 0},
        {"revision": "1"},
        {"enabled": False},
    ],
)
def test_scheduler_invalid_owner(mutation):
    value = copy.deepcopy(state())
    value["owners"]["sample"].update(mutation)
    with (
        RemoteStorageClient(
            "a" * 43,
            "http://storage.invalid",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=value)),
        ) as client,
        pytest.raises(ProtocolError),
    ):
        client.scheduler_state()


def test_scheduler_invalid_json_is_protocol_error():
    with (
        RemoteStorageClient(
            "a" * 43,
            "http://storage.invalid",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json")),
        ) as client,
        pytest.raises(ProtocolError),
    ):
        client.scheduler_state()
