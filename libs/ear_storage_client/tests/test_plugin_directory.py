"""Control-plane SDK wire validation; real authority is exercised by PG integration."""

import httpx
import pytest
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import ProtocolError, ServiceUnavailable


def page():
    return {
        "codec": 1,
        "items": [
            {
                "module": "demo",
                "types": [],
                "enabled": False,
                "online": None,
                "status_source": "storage_management",
                "revision": 1,
            }
        ],
        "next_after": None,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "secret",
        "online",
        "enabled",
        "revision",
        "too_many",
        "cursor",
        "order",
        "type_secret",
    ],
)
def test_directory_rejects_malformed_or_sensitive_metadata(mutation):
    value = page()
    item = value["items"][0]
    if mutation == "secret":
        item["token"] = "PRIVATE"
    elif mutation == "online":
        item["online"] = True
    elif mutation == "enabled":
        item["enabled"] = "true"
    elif mutation == "revision":
        item["revision"] = True
    elif mutation == "too_many":
        value["items"] *= 2
    elif mutation == "cursor":
        value["next_after"] = "unrelated"
    elif mutation == "order":
        item["module"] = "aaa"
    else:
        item["types"] = [{"name": "demo_item", "kind": "semantic", "secret": "PRIVATE"}]
    with (
        RemoteStorageClient(
            "a" * 43,
            "http://storage.invalid",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=value)),
        ) as client,
        pytest.raises(ProtocolError),
    ):
        client.list_plugin_directory(limit=1, after="abc")


@pytest.mark.parametrize("limit,after", [(True, ""), (0, ""), (101, ""), (1, "bad&admin=true")])
def test_directory_invalid_input_never_sent(limit, after):
    def fail(request):
        pytest.fail("invalid directory pagination reached transport")

    with (
        RemoteStorageClient(
            "a" * 43, "http://storage.invalid", transport=httpx.MockTransport(fail)
        ) as client,
        pytest.raises(InvalidRequest),
    ):
        client.list_plugin_directory(limit=limit, after=after)


@pytest.mark.parametrize("status,error", [(403, PermissionDenied), (404, ServiceUnavailable)])
def test_directory_never_falls_back_to_registry(status, error):
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
        client.list_plugin_directory()
    assert calls == ["/v1/management/plugins"]
