"""⑥→real ① HTTP/PG→③ business→⑥; synthetic inputs, explicitly driven events.

The create_place tests additionally drive the real Relay/NATS subscription using
the existing isolated fixture. Neither path claims deployment/worker liveness,
models, calendars or production grants. Pure boundary tests run without PG;
runtime tests opt in with EAR_RUN_POSTGRES_TESTS=1 (and NATS where marked).
"""

from __future__ import annotations

import secrets
from contextlib import ExitStack
from dataclasses import replace
from typing import cast

import httpx
import pytest
from ear_api.app import create_app as api_app
from ear_api.attribution_results import INTEGRATION, RESULT_TYPE, SUPPORTED
from ear_api.business import feedback_identity, feedback_status
from ear_api.config import Settings
from ear_api.upstream import GatewayError
from ear_attribution import AttributionService, CommitJournal, MatchConfig
from ear_contracts import (
    DEFAULT_REGISTRY,
    DataChanged,
    DataTypeSpec,
    NewRawRecord,
    RawRecord,
    Registry,
    SemanticRecord,
    Source,
    SystemRecord,
    new_id,
)
from ear_contracts.registry import UnknownDataTypeError
from ear_storage import bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.http import create_app as storage_app
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    DependencyInput,
    InvalidRequest,
    MarkProcessed,
    MemoryStorage,
    PermissionDenied,
    PutSystemRecord,
    Query,
    RegisterDependencies,
    Update,
)
from ear_storage_client.queries import Page
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import ProtocolError, ServiceUnavailable
from ear_storage_client.testing import make_raw, ts
from ear_storage_client.wire import EVENT_ADAPTER
from starlette.responses import Response

from .test_api_gateway_integration import Harness, loopback
from .test_attribution_runtime import attribution_runtime as attribution_runtime
from .test_attribution_runtime import relay, worker

WHEN = "2026-11-05T09:00:00Z"
PRIVATE = "synthetic_private_body"
READS = {"feedback", RESULT_TYPE}  # Ordinary API user: explicitly just these types.
OWNER_READS = READS | {
    "voiceprint",
    "gps_point",
    "person",
    "place",
    "attribution",
    "attribution_state",
}
CONFIG = MatchConfig(cosine_threshold=0.9, gps_threshold_m=100)


def registry():
    types = dict(DEFAULT_REGISTRY.data_types)
    types["attribution"] = replace(
        types["attribution"], requires_dependencies=True, supports_reevaluation=True
    )
    # Test-only trusted registry. Not shipped core configuration or automatic registration.
    return Registry(
        types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
    ).extend_core(
        DataTypeSpec("attribution_state", "system_record", "attribution"),
        DataTypeSpec(RESULT_TYPE, "system_record", "attribution"),
    )


def raw_feedback(command=None, module="attribution"):
    raw = make_raw(
        "feedback",
        {
            "module": module,
            "command": command or {"kind": "rename", "target_id": new_id(), "name": PRIVATE},
        },
        start=WHEN,
    )
    return RawRecord(**raw.model_dump(), ingested_at=ts(WHEN))


def receipt(raw, **changes):
    return SystemRecord(
        id=new_id(),
        data_type=RESULT_TYPE,
        revision=1,
        event_start=None,
        event_end=None,
        writer="attribution",
        created_at=ts(WHEN),
        updated_at=ts(WHEN),
        payload={
            "schema_version": 1,
            "feedback_id": raw.id,
            "command_kind": raw.payload["command"]["kind"],
            "status": "succeeded",
            "error_code": None,
            "completed_at": WHEN,
            "new_target_id": raw.payload["command"].get("target_id"),
            **changes,
        },
    )


class ProjectionReader:
    """Adversarial read boundary; intentionally can violate trusted wire guarantees."""

    def __init__(self, raw, items=(), cursor=None):
        self.raw, self.items, self.cursor = raw, items, cursor
        self.registry = registry()
        self.queries = []
        self.error: Exception | None = None

    def get(self, fid):
        assert fid == self.raw.id
        return self.raw

    def query(self, query):
        self.queries.append(query)
        assert query.data_type == RESULT_TYPE
        assert dict(query.where) == {"feedback_id": self.raw.id}
        assert query.limit == 2 and query.cursor is None
        if self.error:
            raise self.error
        return Page(tuple(self.items), self.cursor)

    def is_processed(self, *_args):
        pytest.fail("MarkProcessed must never be consulted as business completion")


def projected(reader):
    return feedback_status(cast(RemoteStorageClient, reader), reader.raw.id)


def invalid(reader):
    with pytest.raises(GatewayError) as error:
        projected(reader)
    assert error.value.status == 503
    assert error.value.code == "attribution_result_invalid"
    assert PRIVATE not in str(error.value)


def test_projection_allowlist_and_pending_without_marker():
    raw = raw_feedback()
    reader = ProjectionReader(raw)
    pending = projected(reader)
    assert pending["status"] == "pending" and pending["completion_source"] is None
    assert pending["integration"] == INTEGRATION
    reader.items = [
        receipt(raw, token=PRIVATE, request_text=PRIVATE, response_text=PRIVATE, name=PRIVATE)
    ]
    done = projected(reader)
    assert done["status"] == "succeeded" and done["completion_source"] == RESULT_TYPE
    assert done["metadata"] == {"new_target_id": raw.payload["command"]["target_id"]}
    assert PRIVATE not in str(done)
    assert len(reader.queries) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", "1"),
        ("schema_version", 1.0),
        ("schema_version", 2),
        ("schema_version", None),
        ("feedback_id", new_id()),
        ("feedback_id", None),
        ("command_kind", "confirm_attribution"),
        ("command_kind", []),
        ("status", "pending"),
        ("status", "failed"),
        ("status", True),
        ("status", {}),
        ("error_code", "unsupported_command"),
        ("completed_at", "2026-02-30T00:00:00Z"),
        ("completed_at", "2026-11-05"),
        ("completed_at", "2026-11-05T09:00:00+00:00"),
        ("completed_at", 0),
        ("new_target_id", new_id()),
        ("new_target_id", PRIVATE),
        ("new_target_id", None),
        ("before_version", True),
        ("before_version", 0),
        ("after_version", "2"),
    ],
)
def test_invalid_result_payload_is_not_pending_or_success(field, value):
    raw = raw_feedback()
    invalid(ProjectionReader(raw, [receipt(raw, **{field: value})]))


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "feedback_id",
        "command_kind",
        "status",
        "error_code",
        "completed_at",
        "new_target_id",
    ],
)
def test_required_fields_cannot_be_omitted(field):
    raw = raw_feedback()
    item = receipt(raw)
    payload = dict(item.payload)
    del payload[field]
    invalid(ProjectionReader(raw, [item.model_copy(update={"payload": payload})]))


@pytest.mark.parametrize(
    "change",
    [
        {"writer": "api"},
        {"writer": "extraction"},
        {"data_type": "extraction_run"},
        {"revision": True},
        {"revision": 0},
        {"id": PRIVATE},
    ],
)
def test_foreign_or_malformed_envelope_is_not_authority(change):
    raw = raw_feedback()
    invalid(ProjectionReader(raw, [receipt(raw).model_copy(update=change)]))


@pytest.mark.parametrize("mode", ["duplicate", "cursor", "empty_cursor", "raw", "semantic"])
def test_query_must_return_one_unique_system_record(mode):
    raw = raw_feedback()
    reader = ProjectionReader(raw, [receipt(raw)])
    if mode == "duplicate":
        reader.items = [receipt(raw), receipt(raw)]
    elif mode in {"cursor", "empty_cursor"}:
        reader.cursor = "opaque"
        if mode == "empty_cursor":
            reader.items = []
    elif mode == "raw":
        reader.items = [raw]
    else:
        memory = MemoryStorage()
        from ear_storage_client import Create

        id = new_id()
        memory.client("attribution").write([Create("person", id, {})], Context.system()).check()
        reader.items = [memory.client("api").get(id)]
    invalid(reader)


@pytest.mark.parametrize(
    "spec,core",
    [
        (DataTypeSpec(RESULT_TYPE, "system_record", "api"), True),
        (DataTypeSpec(RESULT_TYPE, "raw_record", "attribution"), True),
        (DataTypeSpec(RESULT_TYPE, "system_record", "attribution"), False),
    ],
)
def test_result_requires_trusted_core_single_writer_type(spec, core):
    raw = raw_feedback()
    reader = ProjectionReader(raw, [receipt(raw)])
    reader.registry = DEFAULT_REGISTRY.extend_core(spec) if core else DEFAULT_REGISTRY.extend(spec)
    invalid(reader)


@pytest.mark.parametrize(
    "error", [PermissionDenied(PRIVATE), ProtocolError(PRIVATE), ServiceUnavailable(PRIVATE)]
)
def test_upstream_failures_propagate_not_hidden_as_pending(error):
    reader = ProjectionReader(raw_feedback())
    reader.error = error
    with pytest.raises(type(error)):
        projected(reader)


def test_missing_registry_safe_unavailable_and_no_grant_mutation():
    reader = ProjectionReader(raw_feedback())
    reader.registry = DEFAULT_REGISTRY
    with pytest.raises(GatewayError, match="attribution_result_unavailable"):
        projected(reader)
    for error in (UnknownDataTypeError(PRIVATE), InvalidRequest(PRIVATE)):
        reader.error = error
        with pytest.raises(GatewayError, match="attribution_result_unavailable"):
            projected(reader)


@pytest.mark.parametrize("module", ["dependency", "extraction"])
def test_cross_module_raw_never_reads_attribution_receipt(module):
    raw = raw_feedback(module=module)
    reader = ProjectionReader(raw, [receipt(raw)])
    if module == "dependency":
        assert projected(reader)["status"] == "pending"
        assert reader.queries == []
    else:
        # Existing extraction projection may query only extraction_run, never our receipt.
        def extraction_query(query):
            assert query.data_type == "extraction_run"
            return Page((), None)

        reader.query = extraction_query
        assert projected(reader)["status"] == "pending"


def test_owner_mismatch_command_in_raw_is_rejected():
    raw = raw_feedback({"kind": "reevaluate_related"})
    invalid(ProjectionReader(raw, [receipt(raw)]))


@pytest.mark.parametrize("code", ["unsupported_command", PRIVATE, "a" * 43])
def test_rejected_codes_are_allowlisted_not_arbitrary_text_or_tokens(code):
    raw = raw_feedback()
    reader = ProjectionReader(
        raw, [receipt(raw, status="rejected", error_code=code, detail=PRIVATE)]
    )
    done = projected(reader)
    assert done["status"] == "rejected" and done["metadata"] == {}
    assert done["error_code"] == (code if code == "unsupported_command" else "attribution_rejected")
    assert PRIVATE not in str(done) and "a" * 43 not in str(done)


@pytest.fixture
def api_attribution_runtime(pg_dsn, tmp_path):
    migrate(pg_dsn)
    bootstrap(pg_dsn, registry())
    operator = AuthOperator(pg_dsn)
    principals = {
        "api": operator.issue_principal("api", read_types=READS),
        "owner": operator.issue_principal(
            "attribution",
            read_types=OWNER_READS,
            subscribe_types=["voiceprint", "gps_point", "feedback", "attribution"],
        ),
        "adapter": operator.issue_principal("adapter", read_types=["voiceprint", "gps_point"]),
    }
    operator.reserve_consumer_identity("attribution", principals["owner"].principal_id)
    storage = storage_app(StorageService(pg_dsn))
    fault = Harness(armed=False, committed=True, bodies=[])

    @storage.middleware("http")
    async def lose_acceptance(request, call_next):
        match = (
            request.url.path == "/v1/batches"
            and request.headers.get("authorization") == "Bearer " + principals["api"].token
        )
        if match:
            fault.bodies.append(await request.json())
        lose = match and fault.armed
        if lose:
            fault.armed = False
            if not fault.committed:
                return Response(PRIVATE, status_code=503)
        response = await call_next(request)
        if lose:
            assert response.status_code == 200
            return Response(PRIVATE, status_code=200)  # Commit happened; receipt is lost.
        return response

    with ExitStack() as stack:
        url = stack.enter_context(loopback(storage))
        clients = {
            name: stack.enter_context(RemoteStorageClient(p.token, url))
            for name, p in principals.items()
        }
        token = secrets.token_urlsafe(32)
        settings = Settings.model_validate(
            {
                "storage_url": url,
                "bindings": [
                    {
                        "subject": "synthetic_user",
                        "role": "user",
                        "token": token,
                        "storage_token": principals["api"].token,
                    }
                ],
            }
        )
        api_url = stack.enter_context(loopback(api_app(settings)))
        http = stack.enter_context(
            httpx.Client(
                base_url=api_url,
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
                timeout=10,
            )
        )
        journal = CommitJournal(
            tmp_path / "private-api-attribution", namespace="synthetic-api-second"
        )
        stack.callback(journal.close)
        worker = AttributionService.business(clients["owner"], config=CONFIG, journal=journal)
        yield Harness(
            dsn=pg_dsn,
            http=http,
            clients=clients,
            worker=worker,
            operator=operator,
            principals=principals,
            fault=fault,
        )


def post(rt, command, module="attribution", request_id=None):
    body = {"request_id": request_id or new_id(), "module": module, "command": command}
    response = rt.http.post("/v1/feedback", json=body)
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["status"] == "accepted" and accepted["business_status"] == "pending"
    return accepted["feedback_id"]


def status(rt, fid):
    return rt.http.get(f"/v1/feedback/{fid}/status")


def event_for(rt, fid):
    with connection(rt.dsn) as conn:
        events = [
            EVENT_ADAPTER.validate_json(row[0])
            for row in conn.execute(
                "SELECT event_text FROM storage.outbox ORDER BY commit_seq,ordinal"
            )
        ]
    return next(
        e
        for e in events
        if isinstance(e, DataChanged) and e.data_type == "feedback" and e.data_id == fid
    )


def seed(rt, values=(1.0, 0.0)):
    raw = make_raw(
        "voiceprint",
        {
            "embedding": {"values": list(values), "shape": [2], "dtype": "float32"},
            "model_version": "synthetic/v1",
        },
        start=WHEN,
    )
    event = rt.clients["adapter"].write([AppendRaw(raw)], Context.system()).check().events[0]
    rt.worker.handle(event)
    attr = rt.clients["owner"].query(Query("attribution", where={"subject_id": raw.id})).items[0]
    assert isinstance(attr, SemanticRecord)
    return raw.id, attr


@pytest.mark.postgres
def test_real_five_commands_pending_to_authoritative_result_and_replay(api_attribution_runtime):
    rt = api_attribution_runtime
    subject, attr = seed(rt)
    _, other = seed(rt, (0.0, 1.0))
    old, target = attr.payload["target_id"], other.payload["target_id"]
    commands = [
        {
            "kind": "correct_attribution",
            "subject_id": subject,
            "previous_target_id": old,
            "new_target_id": target,
        },
        {"kind": "confirm_attribution", "subject_id": subject, "target_id": target},
        {
            "kind": "remove_reference",
            "subject_id": subject,
            "target_id": target,
            "rejudge_history": False,
        },
        {
            "kind": "add_reference",
            "subject_id": subject,
            "target_id": target,
            "rejudge_history": True,
        },
        {"kind": "rename", "target_id": target, "name": PRIVATE},
    ]
    for command in commands:
        request_id = new_id()
        fid = post(rt, command, request_id=request_id)
        pending = status(rt, fid)
        assert pending.status_code == 200 and pending.json()["status"] == "pending"
        event = event_for(rt, fid)
        rt.worker.handle(event)
        response = status(rt, fid)
        assert response.status_code == 200, response.text
        done = response.json()
        assert done["status"] == "succeeded" and done["completion_source"] == RESULT_TYPE
        assert done["command_kind"] == command["kind"] and done["error_code"] is None
        assert PRIVATE not in response.text
        rt.worker.handle(event)
        assert status(rt, fid).json() == done
        assert post(rt, command, request_id=request_id) == fid
        assert status(rt, fid).json() == done
    current = rt.clients["owner"].get(attr.id)
    assert current.payload["target_id"] == target and current.payload["user_protected"] is True
    assert subject not in rt.clients["owner"].get(old).payload["references"]
    assert subject in rt.clients["owner"].get(target).payload["references"]
    assert rt.clients["owner"].get(target).payload["name"] == PRIVATE
    # Granting result read does NOT authorize the raw input or any other business type.
    for dtype in ("voiceprint", "person", "attribution_state", "llm_call"):
        with pytest.raises(PermissionDenied):
            rt.clients["api"].query(Query(dtype))
    assert set(rt.clients["api"].registry.data_types) == READS | {"plugin_management_feedback"}


@pytest.mark.postgres
def test_real_unsupported_rejected_and_dependency_marker_is_not_completion(api_attribution_runtime):
    rt = api_attribution_runtime
    commands = [
        {"kind": "reattribute_range", "interval": {"start": WHEN, "end": "2026-11-05T10:00:00Z"}},
    ]
    for command in commands:
        fid = post(rt, command)
        rt.worker.handle(event_for(rt, fid))
        response = status(rt, fid)
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        assert response.json()["error_code"] == "unsupported_command"
        assert PRIVATE not in response.text
    for module, command in [
        (
            "dependency",
            {
                "kind": "reevaluate_related",
                "previous_target_id": new_id(),
                "new_target_id": new_id(),
            },
        ),
        ("attribution", {"kind": "rename", "target_id": new_id(), "name": PRIVATE}),
    ]:
        fid = post(rt, command, module=module)
        event = event_for(rt, fid)
        rt.clients["owner"].write(
            [MarkProcessed("attribution", event.event_id)], Context.feedback(fid)
        ).check()
        assert status(rt, fid).json()["status"] == "pending"
        if module == "dependency":
            raw = rt.clients["api"].get(fid)
            rt.clients["owner"].write(
                [PutSystemRecord(RESULT_TYPE, new_id(), receipt(raw).payload)],
                Context.feedback(fid),
            ).check()
            assert status(rt, fid).json()["status"] == "pending"
            assert status(rt, fid).json()["integration"] == "pending_contract"


@pytest.mark.postgres
def test_real_read_revocation_denied_not_cached_or_succeeded(api_attribution_runtime):
    rt = api_attribution_runtime
    _, attr = seed(rt)
    fid = post(rt, {"kind": "rename", "target_id": attr.payload["target_id"], "name": PRIVATE})
    rt.worker.handle(event_for(rt, fid))
    assert status(rt, fid).json()["status"] == "succeeded"
    rt.operator.set_grants(rt.principals["api"].principal_id, read_types=["feedback"])
    denied = status(rt, fid)
    assert denied.status_code == 403 and denied.json() == {"code": "forbidden"}
    rt.operator.set_grants(rt.principals["api"].principal_id, read_types=READS)
    assert status(rt, fid).json()["status"] == "succeeded"
    rt.operator.revoke(rt.principals["api"].principal_id)
    assert status(rt, fid).status_code == 401


@pytest.mark.postgres
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("kind", ["rename", "create_place"])
def test_real_lost_acceptance_replays_identical_canonical_request(
    api_attribution_runtime, committed, kind
):
    rt = api_attribution_runtime
    if kind == "rename":
        _, attr = seed(rt)
        command = {"kind": kind, "target_id": attr.payload["target_id"], "name": PRIVATE}
    else:
        command = {"kind": kind, "gps_ids": [place_gps(rt)], "name": PRIVATE}
    body = {
        "request_id": new_id(),
        "module": "attribution",
        "command": command,
    }
    rt.fault.bodies.clear()
    rt.fault.armed, rt.fault.committed = True, committed
    lost = rt.http.post("/v1/feedback", json=body)
    assert lost.status_code == 503 and lost.json() == {
        "code": "acceptance_unknown_replay_identical_request"
    }
    accepted = rt.http.post("/v1/feedback", json=body)
    assert accepted.status_code == 202
    fid = accepted.json()["feedback_id"]
    assert accepted.json()["integration"] == INTEGRATION
    assert rt.fault.bodies[0] == rt.fault.bodies[1]
    assert rt.clients["api"].count(Query("feedback")) == 1
    raw = rt.clients["api"].get(fid)
    assert raw.payload["integration"] == "pending_contract"
    rt.worker.handle(event_for(rt, fid))
    assert status(rt, fid).json()["status"] == "succeeded"
    changed = {**body, "command": {**body["command"], "name": "changed"}}
    assert rt.http.post("/v1/feedback", json=changed).status_code == 422


@pytest.mark.postgres
@pytest.mark.parametrize("kind", ["rename", "create_place"])
def test_real_preupgrade_fingerprint_replay(api_attribution_runtime, kind):
    rt = api_attribution_runtime
    request_id, target = new_id(), new_id()
    gps_ids = [new_id(), new_id()] if kind == "create_place" else []
    fid, when = feedback_identity("synthetic_user", request_id)
    # Exact 79e8dc3 normalized payload, not rebuilt with the new submit function.
    command = {
        "kind": kind,
        "target_id": target if kind == "rename" else None,
        "subject_id": None,
        "previous_target_id": None,
        "new_target_id": None,
        "name": PRIVATE,
        "gps_ids": gps_ids,
        "interval": None,
        "rejudge_history": False,
        "expected_version": None,
    }
    raw = NewRawRecord(
        id=fid,
        data_type="feedback",
        event_start=when,
        event_end=when,
        source=Source(device="api_synthetic_user", channel="api", user="default"),
        adapter_version="api-v1",
        dedup_key="synthetic_user:" + request_id,
        payload={"module": "attribution", "command": command, "integration": "pending_contract"},
    )
    rt.clients["api"].write([AppendRaw(raw)], Context.feedback(fid), request_id=fid).check()
    public = {"kind": kind, "name": PRIVATE}
    public.update({"target_id": target} if kind == "rename" else {"gps_ids": gps_ids})
    assert post(rt, public, request_id=request_id) == fid
    assert rt.fault.bodies[-1] == rt.fault.bodies[-2]
    assert rt.clients["api"].count(Query("feedback")) == 1


@pytest.mark.postgres
def test_real_forgery_wrong_writer_raw_collision_duplicates_and_bad_payload(
    api_attribution_runtime,
):
    rt = api_attribution_runtime
    fid = post(rt, {"kind": "rename", "target_id": new_id(), "name": PRIVATE})
    raw = rt.clients["api"].get(fid)
    payload = receipt(raw).payload
    with pytest.raises(PermissionDenied):
        rt.clients["api"].write(
            [PutSystemRecord(RESULT_TYPE, new_id(), payload)], Context.feedback(fid)
        )
    # A raw feedback containing a claimed receipt does not count, even with matching fid.
    fake = make_raw("feedback", payload, start=WHEN)
    rt.clients["api"].write([AppendRaw(fake)], Context.system()).check()
    assert status(rt, fid).json()["status"] == "pending"
    rid = new_id()
    rt.clients["owner"].write(
        [PutSystemRecord(RESULT_TYPE, rid, {**payload, "schema_version": True, "token": PRIVATE})],
        Context.feedback(fid),
    ).check()
    bad = status(rt, fid)
    assert bad.status_code == 503 and bad.json() == {"code": "attribution_result_invalid"}
    rt.clients["owner"].write(
        [PutSystemRecord(RESULT_TYPE, rid, payload, expected_revision=1)], Context.feedback(fid)
    ).check()
    assert status(rt, fid).json()["status"] == "succeeded"
    rt.clients["owner"].write(
        [PutSystemRecord(RESULT_TYPE, new_id(), payload)], Context.feedback(fid)
    ).check()
    duplicate = status(rt, fid)
    assert duplicate.status_code == 503 and duplicate.json() == {
        "code": "attribution_result_invalid"
    }


@pytest.mark.parametrize(
    "kind", ["correct_attribution", "confirm_attribution", "add_reference", "remove_reference"]
)
@pytest.mark.parametrize(
    "mutation",
    [
        "valid",
        "missing_id",
        "bad_subject",
        "bad_target",
        "bad_previous",
        "bad_id",
        "bool_version",
        "backward",
        "jump",
        "missing_version",
    ],
)
def test_success_metadata_strict_and_bound_to_command(kind, mutation):
    subject, target, previous = new_id(), new_id(), new_id()
    command = {
        "kind": kind,
        "subject_id": subject,
        "target_id": target,
        "new_target_id": target,
        "previous_target_id": previous,
    }
    raw = raw_feedback(command)
    metadata = {
        "subject_id": subject,
        "previous_target_id": previous if kind == "correct_attribution" else target,
        "new_target_id": target,
        "attribution_id": new_id(),
        "before_version": 2,
        "after_version": 3 if kind in {"correct_attribution", "confirm_attribution"} else 2,
    }
    item = receipt(raw, **metadata)
    payload = dict(item.payload)
    if mutation == "missing_id":
        del payload["attribution_id"]
    elif mutation == "missing_version":
        del payload["before_version"]
    elif mutation == "bad_subject":
        payload["subject_id"] = new_id()
    elif mutation == "bad_target":
        payload["new_target_id"] = new_id()
    elif mutation == "bad_previous":
        payload["previous_target_id"] = new_id()
    elif mutation == "bad_id":
        payload["attribution_id"] = PRIVATE
    elif mutation == "bool_version":
        payload["after_version"] = True
    elif mutation == "backward":
        payload["after_version"] = 1
    elif mutation == "jump":
        payload["after_version"] = 4
    reader = ProjectionReader(raw, [item.model_copy(update={"payload": payload})])
    if mutation == "valid" or (kind == "remove_reference" and mutation == "bad_previous"):
        assert projected(reader)["status"] == "succeeded"
    else:
        invalid(reader)


def test_unsupported_command_cannot_claim_success():
    raw = raw_feedback({"kind": "reattribute_range"})
    invalid(ProjectionReader(raw, [receipt(raw, new_target_id=new_id())]))


@pytest.mark.parametrize("code", [None, "", True, {}, PRIVATE + "\n", "a" * 101])
def test_rejection_requires_bounded_code(code):
    raw = raw_feedback()
    invalid(ProjectionReader(raw, [receipt(raw, status="rejected", error_code=code)]))


@pytest.mark.postgres
@pytest.mark.parametrize("kind", ["rename", "create_place"])
def test_real_old_registry_no_result_grant_keeps_admission_and_denies_projection(pg_dsn, kind):
    migrate(pg_dsn)
    bootstrap(pg_dsn, DEFAULT_REGISTRY)
    principal = AuthOperator(pg_dsn).issue_principal("api", read_types=["feedback"])
    with ExitStack() as stack:
        url = stack.enter_context(loopback(storage_app(StorageService(pg_dsn))))
        token = secrets.token_urlsafe(32)
        settings = Settings.model_validate(
            {
                "storage_url": url,
                "bindings": [
                    {
                        "subject": "old",
                        "role": "user",
                        "token": token,
                        "storage_token": principal.token,
                    }
                ],
            }
        )
        api_url = stack.enter_context(loopback(api_app(settings)))
        http = stack.enter_context(
            httpx.Client(
                base_url=api_url, headers={"Authorization": "Bearer " + token}, trust_env=False
            )
        )
        command = {"kind": kind, "name": PRIVATE}
        command.update({"target_id": new_id()} if kind == "rename" else {"gps_ids": [new_id()]})
        body = {
            "request_id": new_id(),
            "module": "attribution",
            "command": command,
        }
        accepted = http.post("/v1/feedback", json=body)
        assert accepted.status_code == 202
        fid = accepted.json()["feedback_id"]
        assert http.post("/v1/feedback", json=body).json() == accepted.json()
        response = http.get(f"/v1/feedback/{fid}/status")
        assert response.status_code == 403 and response.json() == {"code": "forbidden"}
        # Old deployments require explicit registry/bootstrap + read grant from the owner.
        with RemoteStorageClient(principal.token, url) as client:
            assert RESULT_TYPE not in client.registry
            record = client.get(fid)
            assert record is not None
            assert record.payload["integration"] == "pending_contract"


def test_status_never_echoes_raw_integration_or_accepts_another_feedback_id():
    raw = raw_feedback(module="dependency")
    raw = raw.model_copy(update={"payload": {**raw.payload, "integration": PRIVATE}})
    reader = ProjectionReader(raw)
    assert PRIVATE not in str(projected(reader))
    reader.raw = raw.model_copy(update={"id": new_id()})

    # A mismatching GET response is not allowed to redirect authority to another fid.
    def mismatched_get(fid):
        return raw

    reader.get = mismatched_get
    with pytest.raises(GatewayError) as error:
        projected(reader)
    assert error.value.status == 404


@pytest.mark.parametrize("count", [1, 2, 100])
def test_create_place_typed_projection_preserves_order_and_omits_body(count):
    ids, attrs = [new_id() for _ in range(count)], [new_id() for _ in range(count)]
    raw = raw_feedback({"kind": "create_place", "name": PRIVATE, "gps_ids": ids})
    item = receipt(
        raw,
        new_target_id=new_id(),
        subject_ids=ids,
        attribution_ids=attrs,
        name=PRIVATE,
        token=PRIVATE,
        request_text=PRIVATE,
        response_text=PRIVATE,
        body={"secret": PRIVATE},
    )
    assert "create_place" in SUPPORTED and "reattribute_range" not in SUPPORTED
    done = projected(ProjectionReader(raw, [item]))
    assert done["status"] == "succeeded" and done["integration"] == INTEGRATION
    assert done["metadata"] == {
        "new_target_id": item.payload["new_target_id"],
        "subject_ids": ids,
        "attribution_ids": attrs,
    }
    assert done["metadata"]["subject_ids"] is not item.payload["subject_ids"]
    assert done["metadata"]["attribution_ids"] is not item.payload["attribution_ids"]
    assert PRIVATE not in str(done)


@pytest.mark.parametrize("field", ["subject_ids", "attribution_ids"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1,
        "not-an-array",
        {},
        [],
        [None],
        [True],
        [[]],
        [{}],
        [PRIVATE],
        ["12345678-1234-4234-8234-123456789012"],
        [new_id()] * 2,
        [new_id() for _ in range(101)],
    ],
)
def test_create_place_arrays_are_bounded_unique_uuidv7_lists(field, value):
    ids = [new_id(), new_id()]
    raw = raw_feedback({"kind": "create_place", "name": PRIVATE, "gps_ids": ids})
    item = receipt(
        raw, new_target_id=new_id(), subject_ids=ids, attribution_ids=[new_id(), new_id()]
    )
    item = item.model_copy(update={"payload": {**item.payload, field: value}})
    invalid(ProjectionReader(raw, [item]))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_target",
        "missing_subjects",
        "missing_attributions",
        "reordered_subjects",
        "other_subject",
        "short_attributions",
        "long_attributions",
        "extra_scalar",
        "extra_version",
        "raw_empty",
        "raw_duplicate",
        "raw_not_list",
        "raw_bad_id",
        "raw_name_empty",
        "raw_name_missing",
        "raw_name_long",
        "raw_name_type",
        "raw_guard",
    ],
)
def test_create_place_success_bound_to_stored_command_and_exact_metadata(mutation):
    ids = [new_id(), new_id()]
    command = {"kind": "create_place", "name": PRIVATE, "gps_ids": ids}
    raw = raw_feedback(command)
    item = receipt(
        raw, new_target_id=new_id(), subject_ids=ids, attribution_ids=[new_id(), new_id()]
    )
    payload = dict(item.payload)
    if mutation.startswith("missing_"):
        del payload[
            {
                "missing_target": "new_target_id",
                "missing_subjects": "subject_ids",
                "missing_attributions": "attribution_ids",
            }[mutation]
        ]
    elif mutation == "reordered_subjects":
        payload["subject_ids"] = ids[::-1]
    elif mutation == "other_subject":
        payload["subject_ids"] = [ids[0], new_id()]
    elif mutation == "short_attributions":
        payload["attribution_ids"] = [new_id()]
    elif mutation == "long_attributions":
        payload["attribution_ids"] = [new_id(), new_id(), new_id()]
    elif mutation == "extra_scalar":
        payload["subject_id"] = ids[0]
    elif mutation == "extra_version":
        payload["before_version"] = 1
    else:
        changes = {
            "raw_empty": {"gps_ids": []},
            "raw_duplicate": {"gps_ids": [ids[0], ids[0]]},
            "raw_not_list": {"gps_ids": ids[0]},
            "raw_bad_id": {"gps_ids": [PRIVATE]},
            "raw_name_empty": {"name": " "},
            "raw_name_missing": {"name": None},
            "raw_name_long": {"name": "x" * 257},
            "raw_name_type": {"name": []},
            "raw_guard": {"expected_version": 1},
        }
        raw = raw.model_copy(
            update={"payload": {**raw.payload, "command": {**command, **changes[mutation]}}}
        )
    invalid(ProjectionReader(raw, [item.model_copy(update={"payload": payload})]))


@pytest.mark.parametrize(
    "code",
    [
        "invalid_gps_ids",
        "duplicate_gps_ids",
        "protected_attribution_conflict",
        "legacy_protection_unknown",
        "invalid_candidate",
        "invalid_expected_version",
        "reference_owner_conflict",
        "unsupported_command",
    ],
)
def test_create_place_rejections_are_public_without_success_metadata(code):
    raw = raw_feedback({"kind": "create_place", "name": PRIVATE, "gps_ids": [new_id()]})
    item = receipt(
        raw,
        status="rejected",
        error_code=code,
        new_target_id=new_id(),
        detail=PRIVATE,
        body={"name": PRIVATE},
    )
    done = projected(ProjectionReader(raw, [item]))
    assert done["status"] == "rejected" and done["error_code"] == code
    assert done["metadata"] == {} and PRIVATE not in str(done)


@pytest.fixture
def api_place_runtime(attribution_runtime):
    """Reuse real broker fixture; add only loopback ⑥ and a least-privilege API identity."""
    rt = attribution_runtime
    principal = rt.operator.issue_principal("api", read_types=READS)
    with ExitStack() as stack:
        client = stack.enter_context(RemoteStorageClient(principal.token, rt.url))
        token = secrets.token_urlsafe(32)
        settings = Settings.model_validate(
            {
                "storage_url": rt.url,
                "bindings": [
                    {
                        "subject": "synthetic_place",
                        "role": "user",
                        "token": token,
                        "storage_token": principal.token,
                    }
                ],
            }
        )
        url = stack.enter_context(loopback(api_app(settings)))
        http = stack.enter_context(
            httpx.Client(
                base_url=url,
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
                timeout=10,
            )
        )
        service, journal = worker(rt)
        stack.callback(journal.close)
        yield Harness(
            **{
                **vars(rt),
                "http": http,
                "worker": service,
                "clients": {**rt.clients, "api": client},
                "api_principal": principal,
            }
        )


def place_gps(rt):
    raw = make_raw("gps_point", {"latitude": 0, "longitude": 0}, start=WHEN)
    rt.clients["adapter"].write([AppendRaw(raw)], Context.system()).check()
    return raw.id


def pump_place(rt):
    assert relay(rt).delivered > 0
    rt.clients["owner"].run_until_idle()
    assert not rt.clients["owner"].deadletters("attribution")


@pytest.mark.postgres
@pytest.mark.nats
def test_real_api_create_place_broker_projection_replay_and_user_protection(api_place_runtime):
    rt = api_place_runtime
    owner = rt.clients["owner"]
    rt.worker.subscribe()
    a, b = place_gps(rt), place_gps(rt)
    old = new_id()
    owner.write(
        [
            Create(
                "place", old, {"name": "Synthetic old", "references": [a, b], "is_unknown": False}
            )
        ],
        Context.system(),
    ).check()
    pump_place(rt)
    original = {r.payload["subject_id"]: r for r in owner.query(Query("attribution")).items}
    assert set(original) == {a, b}
    command = {"kind": "create_place", "name": PRIVATE, "gps_ids": [b, a]}
    request_id = new_id()
    fid = post(rt, command, request_id=request_id)
    pending = status(rt, fid)
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    assert pending.json()["completion_source"] is None
    assert pending.json()["integration"] == INTEGRATION
    raw = rt.clients["api"].get(fid)
    assert raw.payload["integration"] == "pending_contract"
    assert raw.payload["command"]["gps_ids"] == [b, a]
    pump_place(rt)
    response = status(rt, fid)
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["status"] == "succeeded" and done["completion_source"] == RESULT_TYPE
    assert done["command_kind"] == "create_place" and done["error_code"] is None
    target_id = done["metadata"]["new_target_id"]
    assert done["metadata"] == {
        "new_target_id": target_id,
        "subject_ids": [b, a],
        "attribution_ids": [original[b].id, original[a].id],
    }
    assert PRIVATE not in response.text
    target = owner.get(target_id)
    assert target.payload == {"name": PRIVATE, "references": [b, a], "is_unknown": False}
    assert owner.get(old).payload["references"] == []
    for subject in (b, a):
        current = owner.get(original[subject].id)
        assert current.payload["target_id"] == target_id
        assert current.payload["user_protected"] is True
        assert current.payload["protection_feedback_id"] == fid
        assert owner.get_version(current.id, original[subject].version) == original[subject]
        deps = owner.dependencies_of(current.id)
        assert len(deps) == 1 and deps[0].depends_on.id == target_id
        assert set(deps[0].fields or ()) == {"references", "is_unknown"}
    snapshot = {dtype: owner.query(Query(dtype)).items for dtype in ("place", "attribution")}
    assert post(rt, command, request_id=request_id) == fid
    rt.worker.handle(event_for(rt, fid))  # duplicate delivery after actual broker consumption
    assert status(rt, fid).json() == done
    assert rt.clients["api"].get(fid) == raw
    assert rt.clients["api"].count(Query("feedback")) == 1
    assert rt.clients["api"].count(Query(RESULT_TYPE)) == 1
    changed = {
        "request_id": request_id,
        "module": "attribution",
        "command": {**command, "gps_ids": [a, b]},
    }
    assert rt.http.post("/v1/feedback", json=changed).status_code == 422
    conflict = post(rt, {**command, "gps_ids": [a]})
    pump_place(rt)
    rejected = status(rt, conflict)
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["error_code"] == "protected_attribution_conflict"
    assert rejected.json()["metadata"] == {} and PRIVATE not in rejected.text
    for dtype, records in snapshot.items():
        assert owner.query(Query(dtype)).items == records
    for dtype in ("place", "gps_point", "attribution", "attribution_state", "llm_call"):
        with pytest.raises(PermissionDenied):
            rt.clients["api"].query(Query(dtype))
    rt.operator.set_grants(rt.api_principal.principal_id, read_types=["feedback"])
    denied = status(rt, fid)
    assert denied.status_code == 403 and denied.json() == {"code": "forbidden"}
    assert post(rt, command, request_id=request_id) == fid


@pytest.mark.postgres
@pytest.mark.nats
def test_real_api_create_place_undeployed_pending_guard_rejection_and_forgery(api_place_runtime):
    rt = api_place_runtime
    gps = place_gps(rt)
    command = {"kind": "create_place", "name": PRIVATE, "gps_ids": [gps]}
    fid = post(rt, command)
    # Constructed worker is not subscribed/deployed: publishing is not completion.
    assert relay(rt).delivered > 0
    assert status(rt, fid).json()["status"] == "pending"
    raw = rt.clients["api"].get(fid)
    payload = receipt(
        raw, new_target_id=new_id(), subject_ids=[gps], attribution_ids=[new_id()]
    ).payload
    with pytest.raises(PermissionDenied):
        rt.clients["api"].write(
            [PutSystemRecord(RESULT_TYPE, new_id(), payload)], Context.feedback(fid)
        )
    fake = make_raw("feedback", payload, start=WHEN)
    rt.clients["api"].write([AppendRaw(fake)], Context.system()).check()
    assert status(rt, fid).json()["status"] == "pending"
    rt.operator.set_grants(rt.api_principal.principal_id, read_types=["feedback"])
    denied = status(rt, fid)
    assert denied.status_code == 403 and denied.json() == {"code": "forbidden"}
    rt.operator.set_grants(rt.api_principal.principal_id, read_types=READS)
    # First subscription receives future changes only; it is not history authorization.
    # The earlier unconsumed request must not silently turn into success. A fresh
    # request now reaches the real consumer and can select a GPS with no attribution.
    pending_fid = fid
    rt.worker.subscribe()
    fid = post(rt, command)
    pump_place(rt)
    assert status(rt, pending_fid).json()["status"] == "pending"
    done = status(rt, fid)
    assert done.status_code == 200 and done.json()["status"] == "succeeded"
    snapshot = {
        dtype: rt.clients["owner"].query(Query(dtype)).items for dtype in ("place", "attribution")
    }
    guarded = post(rt, {**command, "expected_version": 1})
    pump_place(rt)
    rejected = status(rt, guarded)
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert rejected.json()["error_code"] == "invalid_expected_version"
    assert rejected.json()["metadata"] == {}
    for dtype, records in snapshot.items():
        assert rt.clients["owner"].query(Query(dtype)).items == records
    # A trusted-owner but malformed receipt still cannot claim success at the API boundary.
    bad_fid = post(rt, command)
    raw = rt.clients["api"].get(bad_fid)
    bad = receipt(
        raw,
        new_target_id=new_id(),
        subject_ids=[new_id()],
        attribution_ids=[new_id()],
        token=PRIVATE,
    )
    rt.clients["owner"].write(
        [PutSystemRecord(RESULT_TYPE, bad.id, bad.payload)], Context.feedback(bad_fid)
    ).check()
    response = status(rt, bad_fid)
    assert response.status_code == 503 and response.json() == {"code": "attribution_result_invalid"}


@pytest.mark.postgres
@pytest.mark.nats
@pytest.mark.parametrize("protection", ["missing", "null"])
def test_real_api_create_place_legacy_protection_rejects_atomically(api_place_runtime, protection):
    rt = api_place_runtime
    owner = rt.clients["owner"]
    rt.worker.subscribe()
    a, b = place_gps(rt), place_gps(rt)
    pump_place(rt)
    old = owner.query(Query("attribution", where={"subject_id": b})).items[0]
    payload = dict(old.payload)
    if protection == "missing":
        del payload["user_protected"]
    else:
        payload["user_protected"] = None
    # Synthetic pre-upgrade row, keeping its existing full dependency set.
    deps = [DependencyInput(d.depends_on, d.fields) for d in owner.dependencies_of(old.id)]
    owner.write(
        [
            Update("attribution", old.id, old.version, payload),
            RegisterDependencies("attribution", old.id, deps),
        ],
        Context.system(),
    ).check()
    snapshot = {
        dtype: owner.query(Query(dtype)).items
        for dtype in ("place", "attribution", "attribution_state")
    }
    command = {"kind": "create_place", "name": PRIVATE, "gps_ids": [a, b]}
    request_id = new_id()
    fid = post(rt, command, request_id=request_id)
    pump_place(rt)
    response = status(rt, fid)
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert response.json()["error_code"] == "legacy_protection_unknown"
    assert response.json()["metadata"] == {} and PRIVATE not in response.text
    for dtype, records in snapshot.items():
        assert owner.query(Query(dtype)).items == records
    assert post(rt, command, request_id=request_id) == fid
    rt.worker.handle(event_for(rt, fid))
    assert status(rt, fid).json() == response.json()
