"""Real loopback ①/⑥/⑦ + isolated PG, optional real JetStream; synthetic model only.

Run with EAR_RUN_POSTGRES_TESTS=1 EAR_RUN_NATS_TESTS=1. No .env, external
provider, ASGI/MockTransport, fake storage, custom verifier or production grants.
Management worker steps are deliberately driven AFTER observing acceptance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import socket
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from ear_api.app import create_app as api_app
from ear_api.config import Settings
from ear_contracts import DEFAULT_REGISTRY, Cause, LLMCallRecord, Registry, TracedCause, new_id
from ear_llm_gateway import Gateway
from ear_llm_gateway.http import create_app as gateway_app
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.octopus import CHAT_PATH, OctopusProvider
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.auth import AuthOperator, token_digest
from ear_storage.http import create_app as storage_app
from ear_storage.management import FEEDBACK, RESULT, ManagementProcessor
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import Context, Create, PermissionDenied, PublishReevaluation, Query
from ear_storage_client.remote import RemoteStorageClient
from fastapi import FastAPI, Request
from nats.js.errors import NotFoundError

from .test_relay import reader

pytestmark = pytest.mark.postgres
MODEL = "synthetic/model"
READS = {"artifact", "rule", "person", "feedback"}
PROMPT = "SYNTHETIC_PRIVATE_PROMPT"
REPLY = "SYNTHETIC_PRIVATE_REPLY"


class Harness(SimpleNamespace):
    def __repr__(self):
        return "<isolated integration harness; credentials redacted>"


def headers(token):
    return {"Authorization": "Bearer " + token}


@contextmanager
def loopback(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    runner = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, timeout_keep_alive=1)
    )
    thread = threading.Thread(target=runner.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not runner.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runner.started, "integration loopback startup failed"
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        runner.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "integration loopback cleanup failed"


@pytest.fixture
def database(pg_dsn):
    # Trusted isolated test configuration, not a change to product permissions.
    types = dict(DEFAULT_REGISTRY.data_types)
    types["artifact"] = replace(types["artifact"], supports_reevaluation=True)
    migrate(pg_dsn)
    bootstrap(
        pg_dsn, Registry(types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types)
    )
    return Harness(dsn=pg_dsn, store=PostgresStorage(pg_dsn), operator=AuthOperator(pg_dsn))


@contextmanager
def api_stack(db, relay=None):
    reads = [FEEDBACK, RESULT, "feedback", "extraction_run", "activity", "rule"]
    admin = db.operator.issue_principal("api", admin=True, read_types=reads)
    user = db.operator.issue_principal("api", read_types=["feedback", "extraction_run", "rule"])
    peer = db.operator.issue_principal("api", admin=True, read_types=reads)
    tokens = {name: secrets.token_urlsafe(32) for name in ("admin", "user", "peer")}
    service = StorageService(db.dsn, relay_config=relay)
    processor = ManagementProcessor(db.store, service.consumers)
    with loopback(storage_app(service)) as url:
        settings = Settings.model_validate(
            {
                "storage_url": url,
                "bindings": [
                    {
                        "subject": name,
                        "role": "user" if name == "user" else "admin",
                        "token": tokens[name],
                        "storage_token": principal.token,
                    }
                    for name, principal in (("admin", admin), ("user", user), ("peer", peer))
                ],
            }
        )
        with (
            loopback(api_app(settings)) as api_url,
            httpx.Client(base_url=api_url, trust_env=False, timeout=15) as http,
        ):
            yield Harness(
                db=db,
                http=http,
                url=url,
                admin=admin,
                user=user,
                peer=peer,
                tokens=tokens,
                processor=processor,
                relay=relay,
            )


@pytest.fixture
def api(database):
    with api_stack(database) as stack:
        yield stack


@pytest.fixture(params=["postgres", pytest.param("jetstream", marks=pytest.mark.nats)])
def management(request, database):
    cfg = None
    if request.param == "jetstream":
        server = request.getfixturevalue("nats_server")
        suffix = new_id().replace("-", "")
        cfg = RelayConfig(
            servers=(server.url,),
            token=server.token,
            stream="EAR_IT_" + suffix,
            prefix="ear_it_" + suffix,
            timeout=2,
            max_bytes=1024 * 1024,
            max_msgs=1000,
        )
    try:
        with api_stack(database, cfg) as stack:
            stack.worker = database.operator.issue_principal(
                "storage", read_types=[FEEDBACK], subscribe_types=[FEEDBACK]
            )
            yield stack
    finally:
        if cfg is not None:

            async def cleanup():
                async with reader(cfg) as js:
                    with suppress(NotFoundError):
                        await js.delete_stream(cfg.stream)

            asyncio.run(cleanup())


def command(action="register_plugin", **changes):
    body: dict[str, Any] = {"request_id": new_id(), "action": action, "module": "synthetic_plugin"}
    if action == "register_plugin":
        body.update(
            types=[{"name": "synthetic_plugin_item", "kind": "semantic"}],
            read_types=["synthetic_plugin_item", "activity"],
            subscribe_types=["activity"],
        )
    return body | changes


def submit(stack, body):
    result = stack.http.post(
        "/v1/management/requests", json=body, headers=headers(stack.tokens["admin"])
    )
    assert result.status_code == 202, result.text
    assert result.json()["status"] == "accepted"
    return result.json()


def management_result(stack, accepted):
    response = stack.http.get(
        "/v1/management/results/" + accepted["result_id"], headers=headers(stack.tokens["admin"])
    )
    assert response.status_code == 200, response.text
    return response.json()["payload"]


def process(stack, accepted):
    if stack.relay is None:
        stack.processor.process(accepted["feedback_id"])
    else:
        outcome = asyncio.run(run_relay(RelayStore(stack.db.dsn, stack.relay), once=True))
        assert outcome.delivered == 1
        assert stack.processor.poll_once(stack.worker.token) == 1
    result = management_result(stack, accepted)
    assert result["status"] == "succeeded", result
    return result


def outbox_ids(db):
    with connection(db.dsn) as conn:
        return conn.execute(
            "SELECT event_id FROM storage.outbox ORDER BY commit_seq,ordinal"
        ).fetchall()


def test_management_register_rotate_claim_acceptance_is_not_effect(management, caplog):
    stack = management
    body = command()
    accepted = submit(stack, body)
    assert submit(stack, body) == accepted
    assert "synthetic_plugin_item" not in stack.db.store.registry
    assert management_result(stack, accepted)["status"] == "accepted"
    assert len(outbox_ids(stack.db)) == 1
    result = process(stack, accepted)
    assert result["enabled"] is False and result["online"] is None
    assert stack.db.store.registry.writer_of("synthetic_plugin_item") == "synthetic_plugin"
    assert stack.processor.process(accepted["feedback_id"]) == result
    assert len(outbox_ids(stack.db)) == 1  # Results are system records, no third event.

    rotated = submit(stack, command("rotate_credentials"))
    path = "/v1/management/credentials/" + rotated["feedback_id"] + "/claim"
    assert stack.http.post(path, json={}, headers=headers(stack.tokens["admin"])).status_code == 403
    process(stack, rotated)
    with connection(stack.db.dsn) as conn:
        assert conn.execute(
            "SELECT token_hash,active FROM storage.principals WHERE module='synthetic_plugin'"
        ).fetchone() == (None, False)
    for role in ("user", "peer"):
        assert (
            stack.http.post(path, json={}, headers=headers(stack.tokens[role])).status_code == 403
        )
    delivered = stack.http.post(path, json={}, headers=headers(stack.tokens["admin"]))
    assert delivered.status_code == 200, delivered.text
    assert delivered.headers["cache-control"] == "no-store"
    credential = delivered.json()
    assert stack.http.post(path, json={}, headers=headers(stack.tokens["admin"])).status_code == 403
    with connection(stack.db.dsn) as conn:
        assert conn.execute(
            "SELECT token_hash,active FROM storage.principals WHERE id=%s",
            (credential["principal_id"],),
        ).fetchone() == (token_digest(credential["token"]), True)
    # Claim is not enable; a disabled plugin's fresh token must still be rejected.
    with httpx.Client(base_url=stack.url, trust_env=False) as storage:
        assert storage.get("/v1/identity", headers=headers(credential["token"])).status_code == 401
    exposed = stack.http.get("/v1/management/results", headers=headers(stack.tokens["admin"]))
    assert exposed.status_code == 200
    assert credential["token"] not in exposed.text + caplog.text + repr(stack.db.store.outbox())
    assert len(outbox_ids(stack.db)) == 2
    if stack.relay is not None:

        async def verify_bus():
            async with reader(stack.relay) as js:
                assert (await js.stream_info(stack.relay.stream)).state.messages == 2
                for seq in (1, 2):
                    event = await js.get_msg(stack.relay.stream, seq=seq)
                    assert event.data is not None
                    payload = json.loads(event.data)
                    assert payload["event_type"] == "data_changed"
                    assert payload["data_type"] == FEEDBACK

        asyncio.run(verify_bus())
        assert stack.processor.poll_once(stack.worker.token) == 0
        # Real broker provisioning is required for enable. No fake resource readiness.
        enabled = submit(stack, command("enable"))
        assert management_result(stack, enabled)["status"] == "accepted"
        enabled_result = process(stack, enabled)
        assert enabled_result["enabled"] is True
        before_directory = directory_state(stack.db)
        listing = stack.http.get("/v1/plugins", headers=headers(stack.tokens["admin"]))
        assert listing.status_code == 200, listing.text
        item = listing.json()["items"][0]
        assert item["enabled"] is True and item["online"] is None
        assert item["revision"] == enabled_result["revision"]
        assert directory_state(stack.db) == before_directory
        with httpx.Client(base_url=stack.url, trust_env=False) as storage:
            assert (
                storage.get("/v1/identity", headers=headers(credential["token"])).status_code == 200
            )
            replacement = submit(stack, command("rotate_credentials"))
            # Acceptance alone must not revoke the currently working token.
            assert (
                storage.get("/v1/identity", headers=headers(credential["token"])).status_code == 200
            )
            process(stack, replacement)
            assert (
                storage.get("/v1/identity", headers=headers(credential["token"])).status_code == 401
            )
            next_path = "/v1/management/credentials/" + replacement["feedback_id"] + "/claim"
            claimed = stack.http.post(next_path, json={}, headers=headers(stack.tokens["admin"]))
            assert claimed.status_code == 200, claimed.text
            assert claimed.json()["principal_id"] == credential["principal_id"]
            assert claimed.json()["token"] != credential["token"]
            assert (
                storage.get("/v1/identity", headers=headers(claimed.json()["token"])).status_code
                == 200
            )
            assert (
                stack.http.post(
                    next_path, json={}, headers=headers(stack.tokens["admin"])
                ).status_code
                == 403
            )
        assert len(outbox_ids(stack.db)) == 4


def test_api_ordinary_denial_and_storage_admin_revocation(api):
    before = outbox_ids(api.db)
    for path in ("/v1/plugins", "/v1/management/results"):
        assert api.http.get(path, headers=headers(api.tokens["user"])).status_code == 403
    assert (
        api.http.post(
            "/v1/management/requests", json=command(), headers=headers(api.tokens["user"])
        ).status_code
        == 403
    )
    # A local admin binding cannot override a real upstream role withdrawal.
    api.db.operator.set_admin(api.admin.principal_id, admin=False)
    assert (
        api.http.post(
            "/v1/management/requests", json=command(), headers=headers(api.tokens["admin"])
        ).status_code
        == 403
    )
    assert outbox_ids(api.db) == before
    api.db.operator.revoke(api.user.principal_id)
    assert api.http.get("/v1/me", headers=headers(api.tokens["user"])).status_code == 401


def test_api_queued_authority_revoked_before_effect(management):
    accepted = submit(management, command())
    management.db.operator.set_admin(management.admin.principal_id, admin=False)
    if management.relay is not None:
        asyncio.run(run_relay(RelayStore(management.db.dsn, management.relay), once=True))
        assert management.processor.poll_once(management.worker.token) == 1
    else:
        management.processor.process(accepted["feedback_id"])
    saved = management.db.store.get_system_record(accepted["result_id"])
    assert saved is not None
    assert saved.payload["status"] == "failed"
    assert saved.payload["error_code"] == "authority_revoked"
    assert "synthetic_plugin_item" not in management.db.store.registry
    assert len(outbox_ids(management.db)) == 1


def test_api_user_feedback_persists_once_but_does_not_execute_business(api):
    body = {
        "request_id": new_id(),
        "module": "extraction",
        "command": {"kind": "rule_create", "rule": {"scope": "synthetic", "instruction": "notes"}},
    }
    auth = headers(api.tokens["user"])
    first = api.http.post("/v1/feedback", json=body, headers=auth)
    assert first.status_code == 202, first.text
    assert first.json()["business_status"] == "pending"
    assert api.http.post("/v1/feedback", json=body, headers=auth).json() == first.json()
    fid = first.json()["feedback_id"]
    status = api.http.get(f"/v1/feedback/{fid}/status", headers=auth)
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "pending"
    assert api.db.store.client("api").count(Query("feedback")) == 1
    assert api.db.store.client("extraction").count(Query("rule")) == 0
    event = next(e for _, group in api.db.store.outbox() for e in group)
    assert event.root_feedback_id == fid and event.depth == 0
    altered = body | {
        "command": {"kind": "rule_create", "rule": {"scope": "different", "instruction": "notes"}}
    }
    assert api.http.post("/v1/feedback", json=altered, headers=auth).status_code == 422
    assert len(outbox_ids(api.db)) == 1
    page = api.http.get("/plugins/")
    assert (
        page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
    )


def test_registered_plugin_is_visible_on_admin_management_listing(api):
    """The real registry must support the management page, not just FakeStorage."""
    accepted = submit(api, command())
    result = api.processor.process(accepted["feedback_id"])
    assert result["status"] == "succeeded"
    assert api.db.store.registry.writer_of("synthetic_plugin_item") == "synthetic_plugin"
    listing = api.http.get("/v1/plugins", headers=headers(api.tokens["admin"]))
    assert listing.status_code == 200, listing.text
    assert "synthetic_plugin" in {item["module"] for item in listing.json()["items"]}, (
        "registered plugin invisible to its submitting admin: /v1/plugins uses the "
        "storage read-grant-filtered registry; do not fix by granting plugin payload access"
    )


def directory_state(db):
    """Hash control-plane rows: never include credentials in assertion diagnostics."""
    from psycopg import sql

    with connection(db.dsn) as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='storage' ORDER BY tablename"
        ).fetchall()
        return {
            name: hashlib.sha256(
                repr(
                    conn.execute(
                        sql.SQL("SELECT row_to_json(t)::text FROM storage.{} t ORDER BY 1").format(
                            sql.Identifier(name)
                        )
                    ).fetchall()
                ).encode()
            ).hexdigest()
            for (name,) in tables
        }


def test_plugin_directory_without_business_reads_is_read_only(api):
    accepted = submit(api, command())
    process(api, accepted)
    private_id = new_id()
    api.db.store.client("synthetic_plugin").write(
        [Create("synthetic_plugin_item", private_id, {"private": "NOT_DIRECTORY_PAYLOAD"})],
        Context.system(),
    ).check()
    api.db.operator.set_grants(api.admin.principal_id, read_types=[])
    before = directory_state(api.db)
    with RemoteStorageClient(api.admin.token, api.url) as client:
        assert "synthetic_plugin_item" not in client.registry
        directory = client.list_plugin_directory(limit=1)
        item = directory["items"][0]
        assert item == {
            "module": "synthetic_plugin",
            "types": [
                {
                    "name": "synthetic_plugin_item",
                    "kind": "semantic",
                    "supports_reevaluation": False,
                    "requires_dependencies": False,
                }
            ],
            "enabled": False,
            "online": None,
            "revision": 1,
            "resources_ready": True,
            "ever_enabled": False,
            "need_recovery_decision": False,
            "read_types": ["activity", "synthetic_plugin_item"],
            "subscribe_types": ["activity"],
            "status_source": "storage_management",
        }
        assert directory["next_after"] is None
        # Preserve existing non-disclosure semantics, not a new ACL/error policy.
        assert client.get(private_id) is None
        assert client.history(private_id) == []
        assert client.get_many([private_id]) == {}
        for read in (
            lambda: client.query(Query("synthetic_plugin_item")),
            lambda: client.count(Query("synthetic_plugin_item")),
        ):
            with pytest.raises(PermissionDenied):
                read()
        listing = api.http.get("/v1/plugins", headers=headers(api.tokens["admin"]))
        assert listing.status_code == 200, listing.text
        assert listing.json() == directory
        assert listing.headers["cache-control"] == "no-store"
        for secret in ("NOT_DIRECTORY_PAYLOAD", api.admin.token, token_digest(api.admin.token)):
            assert secret not in listing.text
        assert "synthetic_plugin_item" not in client.registry
    assert directory_state(api.db) == before


@pytest.mark.parametrize(
    "denial", ["ordinary", "fake_admin", "role_revoked", "revoked", "non_api", "non_api_admin"]
)
def test_plugin_directory_rechecks_current_authority(api, denial):
    principal, local_role = api.admin, "admin"
    if denial in {"ordinary", "fake_admin"}:
        principal = api.user
        local_role = "user" if denial == "ordinary" else "admin"
    elif denial == "role_revoked":
        api.db.operator.set_admin(principal.principal_id, admin=False)
    elif denial == "revoked":
        api.db.operator.revoke(principal.principal_id)
    elif denial.startswith("non_api"):
        principal = api.db.operator.issue_principal("extraction")
        if denial == "non_api_admin":
            # Deliberately inconsistent trusted test fixture: admin alone is insufficient.
            with connection(api.db.dsn) as conn:
                conn.execute(
                    "UPDATE storage.principals SET admin=true WHERE id=%s",
                    (principal.principal_id,),
                )
    before = directory_state(api.db)
    expected = 401 if denial == "revoked" else 403
    with httpx.Client(base_url=api.url, trust_env=False) as storage:
        denied = storage.get("/v1/management/plugins", headers=headers(principal.token))
        assert denied.status_code == expected, denied.text
        spoofed = storage.get(
            "/v1/management/plugins?admin=true&module=api", headers=headers(principal.token)
        )
        assert spoofed.status_code in (401, 403, 422)
    config = Settings.model_validate(
        {
            "storage_url": api.url,
            "bindings": [
                {
                    "subject": "directory-test",
                    "role": local_role,
                    "token": api.tokens["admin"],
                    "storage_token": principal.token,
                }
            ],
        }
    )
    with loopback(api_app(config)) as url, httpx.Client(base_url=url, trust_env=False) as client:
        result = client.get("/v1/plugins", headers=headers(api.tokens["admin"]))
        assert result.status_code == expected, result.text
        assert "items" not in result.json()
    assert directory_state(api.db) == before


def test_plugin_directory_pagination_and_validation_real_postgres(api):
    names = ["alpha_plugin", "alpha0_plugin", "zeta_plugin"]
    for module in reversed(names):
        process(
            api,
            submit(
                api,
                command(
                    module=module,
                    types=[{"name": module + "_item", "kind": "semantic"}],
                    read_types=[],
                    subscribe_types=[],
                ),
            ),
        )
    before = directory_state(api.db)
    with RemoteStorageClient(api.admin.token, api.url) as client:
        for limit in (1, 2, 100):
            after, seen = "", []
            while True:
                page = client.list_plugin_directory(limit=limit, after=after)
                api_page = api.http.get(
                    "/v1/plugins",
                    params={"limit": limit, "after": after},
                    headers=headers(api.tokens["admin"]),
                )
                assert api_page.status_code == 200, api_page.text
                assert api_page.json() == page
                assert len(page["items"]) <= limit
                seen.extend(item["module"] for item in page["items"])
                if page["next_after"] is None:
                    break
                assert page["next_after"] > after
                after = page["next_after"]
            assert seen == sorted(names)
        assert client.list_plugin_directory(after="zzzz") == {"items": [], "next_after": None}
        assert [i["module"] for i in client.list_plugin_directory(after="beta")["items"]] == [
            "zeta_plugin"
        ]
    with httpx.Client(base_url=api.url, trust_env=False) as storage:
        for query in (
            "limit=0",
            "limit=101",
            "limit=-1",
            "limit=true",
            "limit=1.5",
            "limit=999999999999999999999",
            "limit=1&limit=2",
            "after=x&after=y",
            "after=" + "a" * 56,
            "after=INVALID",
            "after=x%27",
            "admin=true",
        ):
            for client, path, token in (
                (storage, "/v1/management/plugins", api.admin.token),
                (api.http, "/v1/plugins", api.tokens["admin"]),
            ):
                response = client.get(path + "?" + query, headers=headers(token))
                assert response.status_code == 422, (query, response.text)
    assert directory_state(api.db) == before


def test_directory_same_sdk_rechecks_revoked_role_and_token(api):
    from ear_storage_client.remote_errors import AuthenticationError

    with RemoteStorageClient(api.admin.token, api.url) as client:
        assert client.list_plugin_directory() == {"items": [], "next_after": None}
        api.db.operator.set_admin(api.admin.principal_id, admin=False)
        with pytest.raises(PermissionDenied):
            client.list_plugin_directory()
        api.db.operator.set_admin(api.admin.principal_id, admin=True)
        assert client.list_plugin_directory()["items"] == []
        api.db.operator.revoke(api.admin.principal_id)
        with pytest.raises(AuthenticationError):
            client.list_plugin_directory()


@pytest.fixture
def gateway(database, tmp_path):
    db = database
    caller = db.operator.issue_principal("extraction", read_types=READS)
    writer_id = db.operator.issue_principal("llm_gateway", read_types=READS | {"llm_call"})
    observer = db.operator.issue_principal("attribution", read_types=["llm_call"])
    service = StorageService(db.dsn)
    app = storage_app(service)
    preflights = []

    @app.middleware("http")
    async def observe_preflight(request: Request, call_next):
        if request.url.path == "/v1/llm/preflight":
            # Observe actual TCP requests; do not replace auth or the endpoint.
            preflights.append(
                (
                    hashlib.sha256(request.headers["authorization"].encode()).hexdigest(),
                    await request.json(),
                )
            )
        return await call_next(request)

    upstream = FastAPI()
    provider_requests = []
    manager_token = secrets.token_urlsafe(32)

    @upstream.post(CHAT_PATH)
    async def synthetic(request: Request):
        provider_requests.append((request.headers["authorization"], await request.json()))
        return {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps({"answer": REPLY})},
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3},
        }

    journal = Journal(str(tmp_path / "integration.sqlite3"))
    try:
        with (
            loopback(app) as url,
            loopback(upstream) as origin,
            RemoteStorageClient(writer_id.token, url) as writer,
        ):
            engine = Gateway(
                callers=lambda token: RemoteStorageClient(token, url),
                writer=writer,
                provider=OctopusProvider(
                    origin,
                    manager_token,
                    models={MODEL: "litellm"},
                    service="synthetic/integration",
                ),
                journal=journal,
                aliases={"default": MODEL},
                timeout=5,
            )
            assert engine.verifier is None
            with (
                loopback(gateway_app(engine)) as gateway_url,
                httpx.Client(base_url=gateway_url, trust_env=False, timeout=20) as http,
            ):
                yield Harness(
                    db=db,
                    http=http,
                    url=url,
                    caller=caller,
                    writer=writer,
                    observer=observer,
                    journal=journal,
                    engine=engine,
                    requests=provider_requests,
                    preflights=preflights,
                    manager_token=manager_token,
                )
    finally:
        journal.close()


def call(**changes):
    return {
        "call_id": new_id(),
        "module": "extraction",
        "purpose": "integration_test",
        "root_feedback_id": "system",
        "depth": 0,
        "system": PROMPT,
        "data": {"input": "synthetic"},
        **changes,
    }


def complete(gateway, body, token=None):
    return gateway.http.post(
        "/v1/complete", json=body, headers=headers(token or gateway.caller.token)
    )


def seed_request(gateway):
    """Commit real objects and a real ⑤ request via authenticated ① HTTP SDK."""
    target, rule, person = new_id(), new_id(), new_id()
    with RemoteStorageClient(gateway.caller.token, gateway.url) as extraction:
        extraction.write(
            [Create("artifact", target, {"synthetic": True}), Create("rule", rule, {})],
            Context.system(),
        ).check()
    attribution_id = gateway.db.operator.issue_principal("attribution", read_types=["person"])
    with RemoteStorageClient(attribution_id.token, gateway.url) as attribution:
        attribution.write([Create("person", person, {})], Context.system()).check()
    trace = TracedCause(
        cause=Cause(data_type="person", data_id=person, old_version=None, new_version=1),
        root_feedback_id="system",
        depth=2,
    )
    dependency_id = gateway.db.operator.issue_principal("dependency", read_types=READS)
    with RemoteStorageClient(dependency_id.token, gateway.url) as dependency:
        result = dependency.write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(data_type="rule", data_id=rule, old_version=None, new_version=1),
                    "synthetic private change description",
                    additional_causes=(trace,),
                )
            ],
            Context("system", 1),
        ).check()
    event = result.events[0]
    return call(
        request_event_id=event.event_id, depth=1, additional_causes=[trace.model_dump(mode="json")]
    ), {rule, person}


def test_gateway_real_caller_metadata_no_event_and_duplicate_no_model(gateway, caplog):
    body = call()
    before = outbox_ids(gateway.db)
    response = complete(gateway, body)
    assert response.status_code == 200, response.text
    assert response.json()["record_saved"] is True
    assert response.json()["output"] == {"answer": REPLY}
    saved = gateway.writer.get_system_record(body["call_id"])
    assert saved is not None and saved.writer == "llm_gateway"
    record = LLMCallRecord.model_validate(saved.payload)
    assert record.caller_module == "extraction" and record.model == MODEL
    assert record.status == "success" and record.usage is None
    assert "request_text" not in saved.payload and "response_text" not in saved.payload
    assert outbox_ids(gateway.db) == before
    for replay in (body, body | {"data": {"changed": True}}):
        duplicate = complete(gateway, replay)
        assert duplicate.status_code == 409 and "output" not in duplicate.json()
    assert len(gateway.requests) == 1
    assert gateway.requests[0][0] == "Bearer " + gateway.manager_token
    assert gateway.requests[0][1]["model"] == MODEL
    row = gateway.journal.get(body["call_id"], "extraction")
    assert row is not None and row["phase"] == "saved"
    for private in (PROMPT, REPLY, gateway.caller.token, gateway.manager_token):
        assert private not in json.dumps(saved.payload) + row["record"] + caplog.text
    result = gateway.http.post(
        f"/v1/calls/{body['call_id']}/reconcile", headers=headers(gateway.caller.token)
    )
    assert result.status_code == 200 and result.json()["response_recoverable"] is False
    assert "output" not in result.json() and len(gateway.requests) == 1


@pytest.mark.parametrize("denial", ["missing", "invalid", "spoofed", "revoked"])
def test_gateway_caller_auth_denied_before_provider(gateway, denial):
    body = call()
    if denial == "missing":
        response = gateway.http.post("/v1/complete", json=body)
    elif denial == "invalid":
        response = complete(gateway, body, secrets.token_urlsafe(32))
    elif denial == "spoofed":
        response = complete(gateway, body | {"module": "attribution"})
    else:
        gateway.db.operator.revoke(gateway.caller.principal_id)
        response = complete(gateway, body)
    assert response.status_code == (403 if denial == "spoofed" else 401), response.text
    assert not gateway.requests
    assert gateway.writer.count(Query("llm_call")) == 0
    assert gateway.journal.get(body["call_id"], "extraction") is None
    assert not outbox_ids(gateway.db)


def test_gateway_real_request_preflight_canonical_causes_and_live_grants(gateway):
    body, sources = seed_request(gateway)
    before = outbox_ids(gateway.db)
    response = complete(gateway, body)
    assert response.status_code == 200, response.text
    assert len(gateway.requests) == 1
    saved = gateway.writer.get_system_record(body["call_id"])
    assert saved is not None
    record = LLMCallRecord.model_validate(saved.payload)
    assert record.request_event_id == body["request_event_id"]
    assert {trace.cause.data_id for trace in record.additional_causes} == sources
    assert len(gateway.preflights) == 3  # admission, before model, before response
    expected_auth = hashlib.sha256(("Bearer " + gateway.caller.token).encode()).hexdigest()
    assert all(digest == expected_auth for digest, _ in gateway.preflights)
    assert all(
        "data" not in value and "system" not in value and "request_text" not in value
        for _, value in gateway.preflights
    )
    # Audit read needs all cause grants; caller needed NO llm_call grant for execution.
    with (
        RemoteStorageClient(gateway.observer.token, gateway.url) as observer,
        pytest.raises(PermissionDenied),
    ):
        observer.get_system_record(body["call_id"])
    gateway.db.operator.set_grants(gateway.caller.principal_id, read_types=READS | {"llm_call"})
    stats = gateway.http.get("/v1/stats", headers=headers(gateway.caller.token))
    assert stats.status_code == 200, stats.text
    assert stats.json()["roots"]["system"]["calls"] == 1
    gateway.db.operator.set_grants(gateway.caller.principal_id, read_types=READS - {"person"})
    denied = complete(gateway, body | {"call_id": new_id()})
    assert denied.status_code == 403, denied.text
    assert len(gateway.requests) == 1 and outbox_ids(gateway.db) == before


@pytest.mark.parametrize("mutation", ["missing_request", "wrong_depth", "missing_trace", "grant"])
def test_gateway_real_preflight_rejects_before_journal_or_model(gateway, mutation):
    body, _ = seed_request(gateway)
    if mutation == "missing_request":
        body["request_event_id"] = new_id()
    elif mutation == "wrong_depth":
        body["depth"] = 9
    elif mutation == "missing_trace":
        body["additional_causes"] = []
    else:
        gateway.db.operator.set_grants(gateway.caller.principal_id, read_types=READS - {"person"})
    before = outbox_ids(gateway.db)
    response = complete(gateway, body)
    assert response.status_code == 403, response.text
    assert len(gateway.preflights) == 1
    assert not gateway.requests and outbox_ids(gateway.db) == before
    assert gateway.journal.get(body["call_id"], "extraction") is None
    assert gateway.writer.count(Query("llm_call")) == 0
