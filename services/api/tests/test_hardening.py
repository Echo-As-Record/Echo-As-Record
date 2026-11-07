"""Security regressions using only MockTransport, in-memory storage and tmp_path."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import pytest
from ear_api.app import create_app
from ear_api.cli import load_settings, main
from ear_api.config import Binding, Settings
from ear_api.models import FeedbackInput, PendingCommand, bound_json, load_json_object
from ear_api.upstream import GatewayError, Upstream
from ear_contracts import new_id
from ear_extraction.domain import Command
from ear_storage_client import MemoryStorage, PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import ProtocolError
from ear_storage_client.wire import decode_read, decode_request, encode_read_result, encode_result
from fastapi.testclient import TestClient
from pydantic import ValidationError

USER, ADMIN, STORAGE_USER, STORAGE_ADMIN = (char * 43 for char in "uasz")
DELIVERED = "c" * 43
INTERVAL = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"}


def configuration() -> dict[str, Any]:
    return {
        "storage_url": "http://storage.invalid",
        "public_origin": "http://testserver",
        "bindings": [
            {"subject": "owner", "role": "user", "token": USER, "storage_token": STORAGE_USER},
            {"subject": "admin", "role": "admin", "token": ADMIN, "storage_token": STORAGE_ADMIN},
        ],
    }


def feedback(command: dict[str, Any], module: str = "extraction") -> FeedbackInput:
    return FeedbackInput.model_validate(
        {"request_id": new_id(), "module": module, "command": command}
    )


class Wire:
    def __init__(self) -> None:
        self.settings = Settings.model_validate(configuration())
        self.identities = {
            STORAGE_USER: {"codec": 1, "module": "api", "principal_id": new_id(), "admin": False},
            STORAGE_ADMIN: {"codec": 1, "module": "api", "principal_id": new_id(), "admin": True},
        }
        self.requests: list[httpx.Request] = []
        self.storage = MemoryStorage()
        self.reader = self.storage.client("api")
        self.snapshots: dict[str, Any] = {}
        self.post_response = httpx.Response(
            200, json={"principal_id": new_id(), "token": DELIVERED}
        )
        self.transport = httpx.MockTransport(self.handle)
        self.upstream = Upstream(self.settings, self.transport)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.headers["authorization"].removeprefix("Bearer ")
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json=self.identities[token])
        if request.url.path.startswith("/v1/management/"):
            return self.post_response
        if request.url.path == "/v1/registry":
            registry = self.reader.registry
            return httpx.Response(
                200,
                json={
                    "codec": 1,
                    "registry": registry.to_json(),
                    "core_types": sorted(registry.core_types),
                },
            )
        if request.url.path == "/v1/read":
            parsed, args = decode_read(request.content)
            reader = (
                self.snapshots.get(parsed.snapshot_id, self.reader)
                if parsed.snapshot_id is not None
                else self.reader
            )
            result = getattr(reader, parsed.method.value)(**args)
            return httpx.Response(200, content=encode_read_result(parsed.method, result))
        if request.url.path == "/v1/snapshots":
            sid = new_id()
            with self.reader.snapshot() as reader:
                self.snapshots[sid] = reader
            return httpx.Response(200, json={"codec": 1, "lease": {"snapshot_id": sid}})
        if request.url.path.startswith("/v1/snapshots/"):
            return httpx.Response(200, json={"codec": 1, "released": True})
        if request.url.path == "/v1/batches":
            body = json.loads(request.content)
            body.pop("request_id")
            ops, context = decode_request(json.dumps(body))
            return httpx.Response(200, content=encode_result(self.reader.write(ops, context)))
        raise AssertionError("unexpected mocked path")

    def app(self) -> TestClient:
        return TestClient(create_app(self.settings, upstream=self.upstream))

    def claim(self) -> dict[str, Any]:
        return self.upstream.management(
            self.settings.bindings[1],
            "/v1/management/credentials/" + new_id() + "/claim",
            {},
            claim=True,
        )

    def posts(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]


def test_sdk_constructor_only_checks_module_and_has_no_principal():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"codec": 1, "module": "api"})

    with RemoteStorageClient(
        STORAGE_ADMIN, "http://storage.invalid", module="api", transport=httpx.MockTransport(handle)
    ) as client:
        assert not hasattr(client, "principal")
    assert calls == ["/v1/identity"]


def test_verified_identity_allows_reads_and_readiness_without_real_network():
    wire = Wire()
    with wire.app() as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/v1/me", headers={"Authorization": "Bearer " + USER}).json() == {
            "subject": "owner",
            "role": "user",
        }
        assert (
            client.get("/v1/timeline", headers={"Authorization": "Bearer " + USER}).status_code
            == 200
        )
        assert (
            client.get(
                "/v1/management/results", headers={"Authorization": "Bearer " + ADMIN}
            ).status_code
            == 200
        )


@pytest.mark.parametrize("role, trusted_admin", [("user", True), ("admin", False)])
@pytest.mark.parametrize(
    "path", ["/v1/me", "/v1/timeline", "/v1/management/results", "/v1/plugins"]
)
def test_local_role_cannot_override_trusted_role_even_on_reads(role, trusted_admin, path):
    wire = Wire()
    token, upstream_token = (USER, STORAGE_USER) if role == "user" else (ADMIN, STORAGE_ADMIN)
    wire.identities[upstream_token]["admin"] = trusted_admin
    with wire.app() as client:
        result = client.get(path, headers={"Authorization": "Bearer " + token})
        assert result.status_code == 403
        assert client.get("/health/ready").status_code == 503
    assert not wire.posts()


@pytest.mark.parametrize(
    "change",
    [
        {"admin": "true"},
        {"admin": 1},
        {"module": "extraction"},
        {"codec": True},
        {"principal_id": "invalid"},
    ],
)
def test_invalid_or_wrong_identity_is_fail_closed(change):
    wire = Wire()
    wire.identities[STORAGE_ADMIN].update(change)
    assert not wire.upstream.ready()
    with pytest.raises((PermissionDenied, ProtocolError)):
        wire.claim()
    assert not wire.posts()


def test_current_wire_identity_uses_explicit_binding_not_invented_fields():
    wire = Wire()
    wire.identities[STORAGE_ADMIN] = {"codec": 1, "module": "api"}
    # Actual wire-v1 has no admin/principal_id fields. Local role is protected config;
    # POST authorization remains the storage service's responsibility, not this reply.
    assert wire.upstream.ready()
    assert wire.claim()["token"] == DELIVERED
    assert len(wire.posts()) == 1


def test_role_revocation_is_not_cached():
    wire = Wire()
    binding = wire.settings.bindings[1]
    with wire.upstream.client(binding):
        pass
    wire.identities[STORAGE_ADMIN]["admin"] = False
    with pytest.raises(PermissionDenied), wire.upstream.client(binding):
        pytest.fail("role revocation bypassed")
    with pytest.raises(PermissionDenied):
        wire.claim()
    assert not wire.posts()


def test_two_different_credentials_cannot_alias_one_principal():
    values = configuration()
    values["bindings"][1]["role"] = "user"
    wire = Wire()
    wire.settings = Settings.model_validate(values)
    wire.upstream = Upstream(wire.settings, wire.transport)
    wire.identities[STORAGE_ADMIN] = dict(wire.identities[STORAGE_USER])
    assert not wire.upstream.ready()
    with (
        pytest.raises(PermissionDenied, match="aliased"),
        wire.upstream.client(wire.settings.bindings[0]),
    ):
        pytest.fail("principal alias accepted")


def test_unconfigured_binding_and_user_management_are_rejected_before_io():
    wire = Wire()
    with pytest.raises(PermissionDenied):
        wire.upstream.management(wire.settings.bindings[0], "/v1/management/requests", {})
    foreign = Binding.model_validate(
        {
            "subject": "foreign",
            "role": "admin",
            "token": "f" * 43,
            "storage_token": "g" * 43,
        }
    )
    with pytest.raises(PermissionDenied), wire.upstream.client(foreign):
        pytest.fail("unconfigured binding accepted")
    assert not wire.requests


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b"{",
        b'{"token":123}',
        (
            '{"principal_id":"'
            + new_id()
            + '","token":"'
            + DELIVERED
            + '","token":"'
            + DELIVERED
            + '"}'
        ).encode(),
        json.dumps({"principal_id": new_id(), "token": DELIVERED, "padding": "x" * 4096}).encode(),
        ('{"principal_id":"' + new_id() + '","token":"' + DELIVERED + '","n":1e9999}').encode(),
        ('{"nested":' * 1500 + "0" + "}" * 1500).encode(),
    ],
)
def test_claim_bad_responses_are_unknown_and_never_retried(raw, caplog):
    wire = Wire()
    wire.post_response = httpx.Response(200, content=raw)
    caplog.set_level(logging.INFO)
    with pytest.raises(GatewayError) as caught:
        wire.claim()
    assert (caught.value.status, caught.value.code) == (503, "credential_delivery_unknown")
    assert len(wire.posts()) == 1
    assert DELIVERED not in str(caught.value)
    assert DELIVERED not in caplog.text


@pytest.mark.parametrize("status", [302, 404, 413, 422, 500, 503])
def test_claim_unexpected_status_is_unknown_without_redirect_or_retry(status):
    wire = Wire()
    wire.post_response = httpx.Response(
        status,
        headers={"Location": "https://do-not-contact.invalid"},
        content="PRIVATE " + DELIVERED,
    )
    with pytest.raises(GatewayError, match="credential_delivery_unknown"):
        wire.claim()
    assert len(wire.posts()) == 1
    assert {request.url.host for request in wire.requests} == {"storage.invalid"}


def test_claim_interrupted_stream_is_unknown_without_secret_or_retry(caplog):
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"token":"' + DELIVERED.encode()
            raise httpx.ReadError("PRIVATE " + DELIVERED)

    wire = Wire()
    wire.post_response = httpx.Response(200, stream=Interrupted())
    with wire.app() as client:
        result = client.post(
            "/v1/management/credentials/" + new_id() + "/claim",
            json={},
            headers={"Authorization": "Bearer " + ADMIN},
        )
    assert result.status_code == 503
    assert result.json() == {"code": "credential_delivery_unknown"}
    assert len(wire.posts()) == 1
    assert DELIVERED not in result.text + caplog.text


def test_successful_claim_projects_only_delivery_fields(caplog):
    wire = Wire()
    pid = new_id()
    wire.post_response = httpx.Response(
        200, json={"principal_id": pid, "token": DELIVERED, "secret": "PRIVATE"}
    )
    assert wire.claim() == {"principal_id": pid, "token": DELIVERED}
    assert len(wire.posts()) == 1
    assert DELIVERED not in caplog.text


@pytest.mark.parametrize(
    "path, claim, data",
    [
        ("https://do-not-contact.invalid/v1/management/requests", False, {}),
        ("/v1/management/requests?role=admin", False, {}),
        ("/v1/management/credentials/not-an-id/claim", True, {}),
        ("/v1/management/credentials/" + new_id() + "/claim", True, {"extra": 1}),
    ],
)
def test_management_paths_are_pinned_before_io(path, claim, data):
    wire = Wire()
    with pytest.raises(Exception) as caught:
        wire.upstream.management(wire.settings.bindings[1], path, data, claim=claim)
    assert "do-not-contact" not in str(caught.value)
    assert not wire.requests


PUBLIC = [
    {"kind": "extract", "interval": INTERVAL},
    {"kind": "reextract", "interval": INTERVAL, "preview": {}},
    {"kind": "apply_rule", "target_id": new_id(), "rule_id": new_id()},
    {
        "kind": "extract_once",
        "target_id": new_id(),
        "rule": {"scope": "one", "instruction": "notes"},
    },
    {"kind": "rule_create", "rule": {"scope": "meeting", "instruction": "notes", "enabled": True}},
    {
        "kind": "rule_update",
        "target_id": new_id(),
        "expected_version": 2,
        "rule": {"scope": "meeting", "instruction": "notes"},
    },
    {"kind": "rule_delete", "target_id": new_id(), "expected_version": 2},
    {
        "kind": "review_suggestion",
        "target_id": new_id(),
        "expected_version": 2,
        "decision": "approved",
    },
]


@pytest.mark.parametrize("command", PUBLIC)
def test_public_commands_preserve_existing_extraction_semantics(command):
    assert feedback(command).normalized_command() == Command.model_validate(command).model_dump(
        mode="json"
    )


@pytest.mark.parametrize("command", PUBLIC)
@pytest.mark.parametrize(
    "field, value",
    [("outcome", "executed"), ("receipt", "fake-success"), ("description", "unrelated")],
)
def test_each_public_kind_rejects_other_branch_fields_even_when_default(command, field, value):
    with pytest.raises(ValueError):
        feedback({**command, field: value}).normalized_command()


@pytest.mark.parametrize(
    "command",
    [
        {
            "kind": "action_result",
            "target_id": new_id(),
            "expected_version": 1,
            "outcome": "executed",
            "receipt": "forged",
        },
        {"kind": "reevaluate", "target_id": new_id()},
        {"kind": "extract", "interval": INTERVAL, "rule_id": None},
        {"kind": "extract", "interval": INTERVAL, "preview": {}},
        {"kind": "rule_create", "rule": {"scope": "x", "instruction": "x", "enabled": 1}},
        {"kind": "rule_create", "rule": {"scope": "x", "instruction": "x", "enabled": "true"}},
        {"kind": "rule_create", "rule": {"scope": "x", "instruction": "x", "unknown": True}},
        {"kind": "extract", "interval": {**INTERVAL, "start": 0}},
        {"kind": "extract", "interval": {**INTERVAL, "unknown": "x"}},
    ],
)
def test_internal_unsafe_or_coerced_commands_rejected(command):
    with pytest.raises(ValueError):
        feedback(command).normalized_command()


@pytest.mark.parametrize("value", [True, False, 1.0, "1", 0, -1, 2147483648, None])
def test_strict_revisions_and_preview_values(value):
    for command in [
        {"kind": "rule_delete", "target_id": new_id(), "expected_version": value},
        {"kind": "reextract", "interval": INTERVAL, "preview": {new_id(): value}},
    ]:
        with pytest.raises(ValueError):
            feedback(command).normalized_command()


@pytest.mark.parametrize(
    "key", ["not-uuid", new_id().upper(), "00000000-0000-4000-8000-000000000000", new_id() + "\n"]
)
def test_preview_keys_require_canonical_uuidv7(key):
    with pytest.raises(ValueError):
        feedback(
            {"kind": "reextract", "interval": INTERVAL, "preview": {key: 1}}
        ).normalized_command()


def test_pending_inputs_are_closed_owner_checked_and_duplicate_points_rejected():
    point = new_id()
    cases = [
        {"kind": "rename", "target_id": new_id(), "name": "name", "gps_ids": []},
        {"kind": "create_place", "name": "name", "gps_ids": [point, point]},
        {"kind": "add_reference", "target_id": new_id(), "subject_id": point, "rejudge_history": 1},
    ]
    for command in cases:
        with pytest.raises(ValidationError):
            PendingCommand.model_validate(command)
    with pytest.raises(ValueError, match="owner mismatch"):
        feedback(
            {"kind": "rename", "target_id": point, "name": "x"}, "dependency"
        ).normalized_command()


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        1 << 64,
        {1: "x"},
        {"x": {1, 2}},
        {"x": b"raw"},
        {"x": "\ud800"},
        {"Role": "admin"},
        {"x": [0] * 513},
    ],
)
def test_bound_json_rejects_non_json_authority_and_unbounded_values(value):
    with pytest.raises(ValueError):
        bound_json(value)


def test_bound_json_limits_depth_cycles_total_work_and_text():
    cycle = {}
    cycle["self"] = cycle
    for value in [cycle, {"x": [[0] * 512 for _ in range(9)]}, {"x": "x" * 8193}]:
        with pytest.raises(ValueError):
            bound_json(value)
    bound_json({"finite": 1.5, "revision": 1, "enabled": True, "none": None})


BAD_JSON = [
    b'{"x":NaN}',
    b'{"x":Infinity}',
    b'{"x":1e9999}',
    b'{"x":-1e9999}',
    b'{"x":1,"x":2}',
    b'{"x":{"y":1,"y":2}}',
    b'{"x":"\\ud800"}',
    b'{"x":"\xff"}',
    b"[]",
    b'{"x":9223372036854775808}',
    ('{"x":' * 1500 + "0" + "}" * 1500).encode(),
    ('{"x":' * 14 + "0" + "}" * 14).encode(),
]


@pytest.mark.parametrize("raw", BAD_JSON)
def test_strict_decoder_sanitizes_recursion_duplicates_nonfinite_and_unicode(raw):
    with pytest.raises(ValueError, match=r"^invalid JSON object$"):
        load_json_object(raw)


@pytest.mark.parametrize("raw", BAD_JSON)
@pytest.mark.parametrize(
    "path", ["/v1/feedback", "/v1/previews/reextract", "/v1/management/requests"]
)
def test_boundary_malformed_json_is_422_and_never_reaches_upstream(raw, path, caplog):
    wire = Wire()
    with wire.app() as client:
        result = client.post(
            path,
            content=raw,
            headers={"Authorization": "Bearer " + ADMIN, "Content-Type": "application/json"},
        )
    assert result.status_code == 422
    assert result.json() == {"code": "invalid_input"}
    assert result.headers["cache-control"] == "no-store"
    assert not wire.requests
    assert ADMIN not in result.text + caplog.text


@pytest.mark.parametrize(
    "name, values",
    [
        ("Content-Type", ["application/json", "text/plain"]),
        ("Content-Length", ["2", "2"]),
        ("Content-Encoding", ["identity", "identity"]),
        ("Host", ["testserver", "other.invalid"]),
    ],
)
def test_duplicate_framing_headers_rejected(name, values):
    wire = Wire()
    headers = [("Authorization", "Bearer " + USER)] + [(name, value) for value in values]
    with wire.app() as client:
        result = client.post("/v1/feedback", content="{}", headers=headers)
    assert result.status_code == 422
    assert not wire.requests


@pytest.mark.parametrize("length", ["-1", "+2", "nonsense", "1", "3"])
def test_invalid_or_mismatched_content_length_rejected(length):
    wire = Wire()
    with wire.app() as client:
        result = client.post(
            "/v1/feedback",
            content="{}",
            headers={"Content-Length": length, "Content-Type": "application/json"},
        )
    assert result.status_code == 422
    assert not wire.requests


@pytest.mark.parametrize("first, second", [(0, 1), (0, 2), (0, 3), (1, 3)])
def test_config_rejects_all_credential_aliases(first, second):
    values = configuration()
    credentials = [USER, STORAGE_USER, ADMIN, STORAGE_ADMIN]
    credentials[second] = credentials[first]
    values["bindings"][0].update(token=credentials[0], storage_token=credentials[1])
    values["bindings"][1].update(token=credentials[2], storage_token=credentials[3])
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(values)
    assert STORAGE_ADMIN not in str(caught.value)
    assert USER not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout": True},
        {"timeout": "10"},
        {"timeout": float("nan")},
        {"body_timeout": float("inf")},
        {"max_body_bytes": True},
        {"storage_url": "http://user:password@storage.invalid"},
        {"storage_url": "http://storage.invalid/?"},
        {"storage_url": "http://storage.invalid/#"},
        {"storage_url": "http://storage.invalid\n"},
        {"storage_url": "http://storage.invalid/%2e"},
        {"public_origin": "https://public.invalid/path"},
    ],
)
def test_invalid_configuration_is_closed_and_sanitized(changes):
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate({**configuration(), **changes})
    assert "password@" not in str(caught.value)


@pytest.mark.parametrize(
    "header",
    [
        "Bearer " + USER + "\n",
        "bearer " + USER,
        "Bearer  " + USER,
        "Bearer " + "é" * 43,
        "Bearer " + USER + ", Bearer " + ADMIN,
    ],
)
def test_authentication_has_exact_bearer_grammar(header):
    settings = Settings.model_validate(configuration())
    assert settings.authenticate([header]) is None


def test_config_loader_protected_file_duplicate_json_and_generic_errors(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "api.json"
    path.write_text(json.dumps(configuration()))
    path.chmod(0o600)
    assert load_settings(str(path)).bindings[0].subject == "owner"
    cases = [
        *BAD_JSON,
        b'{"storage_url":"http://storage.invalid","storage_url":"http://private.invalid"}',
        b"x" * 65537,
    ]
    for raw in cases:
        path.write_bytes(raw)
        with pytest.raises(ValueError, match=r"^protected config required$"):
            load_settings(str(path))
    path.write_text(json.dumps(configuration()))
    monkeypatch.setattr("sys.argv", ["ear-api", "check", "--config", str(path)])
    assert main() == 0
    path.chmod(0o640)
    assert main() == 1
    output = capsys.readouterr()
    assert USER not in output.out + output.err
    assert STORAGE_ADMIN not in output.out + output.err


def test_config_loader_refuses_symlink_fifo_hardlink_wrong_owner_and_directory(
    tmp_path, monkeypatch
):
    path = tmp_path / "api.json"
    path.write_text(json.dumps(configuration()))
    path.chmod(0o600)
    symlink = tmp_path / "link.json"
    symlink.symlink_to(path)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o600)
    for location in [symlink, fifo, tmp_path]:
        with pytest.raises(ValueError, match=r"^protected config required$"):
            load_settings(str(location))
    hardlink = tmp_path / "hardlink.json"
    os.link(path, hardlink)
    with pytest.raises(ValueError):
        load_settings(str(path))
    hardlink.unlink()
    actual = os.fstat

    def wrong_owner(fd):
        info = actual(fd)
        values = list(info)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr("ear_api.cli.os.fstat", wrong_owner)
    with pytest.raises(ValueError):
        load_settings(str(path))
