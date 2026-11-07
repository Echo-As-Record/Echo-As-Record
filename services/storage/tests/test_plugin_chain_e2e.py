"""Synthetic dependency driver, NOT module ⑤, algorithms, deployment or a sandbox.

Two isolated live-TCP scenarios; every layer has its own RemoteStorageClient.
Only core identities/infrastructure are bootstrapped. Plugins use admin feedback,
JetStream management consumption, credential claim and dynamic consumer generations.
No model calls, production business handlers, plugin backend writes or fake Finish.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import ExitStack
from datetime import UTC, datetime

import httpx
import pytest
from ear_contracts import (
    Cause,
    DataChanged,
    ReevaluationRequested,
    ReevaluationResult,
    SemanticRecord,
    TracedCause,
    VersionRef,
    new_id,
    utcnow,
)
from ear_storage.management import FEEDBACK
from ear_storage.management_input import ManagementInput
from ear_storage.relay import RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage_client import (
    AssertTypeRevision,
    Context,
    Create,
    DependencyInput,
    FinishReevaluation,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    Query,
    RegisterDependencies,
    Update,
    WriteOp,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import AuthenticationError

from .test_http_postgres import live_server
from .test_management_integration import setup as setup
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()
LAYERS = ("alpha_first", "beta_second", "alpha_third", "alpha_fourth")
CORE = ("activity", "person", "rule")
ALL = (*CORE, *LAYERS, "alpha_completion", "beta_completion")
DAY = datetime(2026, 1, 1, tzinfo=UTC)


class Chain:
    """Bounded, preselected requests only; deliberately not a DAG scheduler."""

    def __init__(self, setup, stack: ExitStack):
        self.dsn, _, self.cfg, self.service, operator, admin, self.processor = setup
        self.service_identity = id(self.service)
        self.consumer_identity = id(self.service.consumers)
        self.pid = os.getpid()
        self.started = time.monotonic()
        self.url = stack.enter_context(live_server(self.service))
        self.http = stack.enter_context(httpx.Client(base_url=self.url, trust_env=False, timeout=5))
        self.headers = {"Authorization": "Bearer " + admin.token}
        self.stack = stack
        self.admin = self.remote(admin.token)
        self.worker = operator.issue_principal(
            "storage", read_types=[FEEDBACK], subscribe_types=[FEEDBACK]
        )
        self.tokens: dict[str, str] = {}
        self.clients: dict[str, RemoteStorageClient] = {}
        self.ids = {key: new_id() for key in (*CORE, *LAYERS)}
        self.published: list[str] = []
        self.blocked: dict[str, str] = {}
        for owner, types in (("alpha", LAYERS[:1] + LAYERS[2:]), ("beta", LAYERS[1:2])):
            readable = (*CORE, *types, owner + "_completion")
            if owner == "beta":
                readable += ("alpha_first",)
            self.manage(
                owner,
                "register_plugin",
                types=[
                    dict(
                        name=t,
                        kind="semantic",
                        requires_dependencies=True,
                        supports_reevaluation=True,
                    )
                    for t in types
                ]
                + [
                    dict(name=owner + "_completion", kind="system_record", reevaluation_result=True)
                ],
                read_types=readable,
                subscribe_types=(*types, "activity", "person"),
            )
        self.manage(
            "alpha",
            "set_permissions",
            read_types=ALL,
            subscribe_types=("alpha_first", "alpha_third", "alpha_fourth", "activity", "person"),
        )
        for owner in ("alpha", "beta"):
            accepted, _ = self.manage(owner, "rotate_credentials")
            response = self.http.post(
                "/v1/management/credentials/" + accepted["feedback_id"] + "/claim",
                headers=self.headers,
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            self.tokens[owner] = response.json()["token"]
            self.manage(owner, "enable")
        assert self.tokens["alpha"] != self.tokens["beta"]
        for layer in LAYERS:
            self.clients[layer] = self.remote(self.tokens[layer.split("_")[0]])
            spec = self.clients[layer].registry.get(layer)
            assert spec.requires_dependencies and spec.supports_reevaluation
            assert self.clients[layer].registry.get(spec.writer + "_completion").reevaluation_result
        for module in ("extraction", "attribution", "dependency"):
            principal = operator.issue_principal(module, read_types=ALL)
            self.clients[module] = self.remote(principal.token)
        assert len({id(self.clients[t]) for t in LAYERS}) == 4
        self.no_restart()

    def remote(self, token):
        return self.stack.enter_context(RemoteStorageClient(token, self.url, timeout=5))

    def no_restart(self):
        assert id(self.service) == self.service_identity
        assert id(self.service.consumers) == self.consumer_identity
        assert os.getpid() == self.pid
        assert time.monotonic() - self.started < 85, "scenario exceeded bounded work budget"

    def relay(self):
        outcome = asyncio.run(run_relay(RelayStore(self.dsn, self.cfg), once=True))
        assert outcome.status != "batch_limit", "unexpected unbounded fixture workload"

    def manage(self, owner, action, **kwargs):
        body = ManagementInput.model_validate(
            dict(request_id=new_id(), module=owner, action=action, **kwargs)
        )
        response = self.http.post(
            "/v1/management/requests", headers=self.headers, json=body.payload()
        )
        assert response.status_code == 202, response.text
        accepted = response.json()
        self.relay()
        for _ in range(4):
            self.processor.poll_once(self.worker.token)
            result = self.admin.get_system_record(accepted["result_id"])
            assert result is not None
            if result.payload["status"] == "succeeded":
                return accepted, result.payload
            assert result.payload["status"] not in {"failed", "rejected"}, result.payload
        pytest.fail("management feedback did not complete in four deliveries")

    def current(self, key):
        record = self.clients["dependency"].get(self.ids[key])
        assert isinstance(record, SemanticRecord)
        return record

    def ref(self, key):
        record = self.current(key)
        return VersionRef(data_type=key, id=record.id, version=record.version)

    def seed(self):
        self.clients["extraction"].write(
            [
                Create("activity", self.ids["activity"], {"n": 1}),
                Create("rule", self.ids["rule"], {"synthetic_rule": 1}),
            ],
            SYS,
        ).check()
        self.clients["attribution"].write(
            [Create("person", self.ids["person"], {"name": "Synthetic A", "references": []})],
            SYS,
        ).check()
        for layer in LAYERS:
            self.clients[layer].write(
                [Create(layer, self.ids[layer], {"value": 1}), self.dependencies(layer)], SYS
            ).check()
        self.relay()
        for owner in ("alpha", "beta"):
            self.drain(owner)

    def dependencies(self, layer):
        inputs = {
            "alpha_first": ("activity", "person", "rule"),
            "beta_second": ("alpha_first",),
            "alpha_third": ("alpha_first", "beta_second"),
            "alpha_fourth": ("beta_second", "alpha_third"),
        }[layer]
        return RegisterDependencies(
            layer,
            self.ids[layer],
            [
                DependencyInput(self.ref(k), ("name",)) if k == "person" else self.ref(k)
                for k in inputs
            ],
        )

    def drain(self, owner, expected=None, client=None):
        client = client or self.clients["alpha_first" if owner == "alpha" else "beta_second"]
        name = owner + ".managed"
        seen = []
        for _ in range(40):
            response = client._consumer("fetch", name)
            delivery = response["delivery"]
            if delivery:
                event = delivery["event"]
                if expected is not None and event["event_id"] == expected:
                    return delivery
                assert event["event_type"] == "data_changed", "unexpected real request discarded"
                seen.append(event["data_id"])
                client._consumer("ack", name, delivery_token=delivery["delivery_token"])
            elif response["status"] == "idle":
                assert expected is None, "request missing from owner's real consumer"
                return seen
        pytest.fail("consumer exceeded finite delivery budget")

    def publish(self, layer, cause, context, waits=()):
        scheduler = self.clients["dependency"]
        for upstream in waits:
            result = scheduler.get_reevaluation_result(upstream.event_id)
            assert result is not None, "driver must query authoritative completion"
            if result.outcome not in {"changed", "unchanged"}:
                self.blocked[layer] = "synthetic_driver_upstream_failed"
                return None
        receipt = scheduler.write(
            [PublishReevaluation(layer, self.ids[layer], cause, "preselected synthetic request")],
            context,
        ).check()
        event = receipt.events[0]
        assert isinstance(event, ReevaluationRequested)
        self.published.append(event.event_id)
        self.relay()
        return event

    def finish(self, layer, event, value, *, failed=False, duplicate=False, guard=False):
        assert isinstance(event, ReevaluationRequested)
        client = self.clients[layer]
        owner = layer.split("_")[0]
        delivery = self.drain(owner, event.event_id, client)
        assert isinstance(delivery, dict)
        received = ReevaluationRequested.model_validate(delivery["event"])
        assert received == event
        if duplicate:
            # Retry before completion; the proxy suppresses already-processed deliveries.
            client._consumer("nack", owner + ".managed", delivery_token=delivery["delivery_token"])
            time.sleep(0.05)
            redelivery = self.drain(owner, event.event_id, client)
            assert isinstance(redelivery, dict) and redelivery["attempt"] == 2
            delivery = redelivery
        current = client.get(event.target_id)
        assert isinstance(current, SemanticRecord)
        context = Context.of(received)
        changed = not failed and current.payload != {"value": value}
        result = ReevaluationResult(
            request_event_id=event.event_id,
            target_type=layer,
            target_id=current.id,
            outcome="failed" if failed else "changed" if changed else "unchanged",
            before_version=current.version,
            after_version=current.version + int(changed),
            root_feedback_id=context.root_feedback_id,
            depth=context.depth,
            additional_causes=context.additional_causes,
            completed_at=utcnow(),
            error_code="synthetic_failure" if failed else None,
        )
        ops: list[WriteOp] = [
            FinishReevaluation(result),
            MarkProcessed(owner + ".managed", event.event_id),
        ]
        if not failed:
            ops = [
                Update(layer, current.id, current.version, {"value": value}),
                self.dependencies(layer),
                *ops,
            ]
        if guard:
            with client.snapshot() as snapshot:
                revision = snapshot.type_revision("rule")
                rule = snapshot.get(self.ids["rule"])
                assert isinstance(rule, SemanticRecord)
                # A write during computation proves the snapshot holds no writer gate.
                start = time.monotonic()
                self.clients["extraction"].write(
                    [Update("rule", rule.id, rule.version, {"synthetic_rule": 2})], SYS
                ).check()
                assert time.monotonic() - start < 3
                rejected = client.write([AssertTypeRevision("rule", revision), *ops], context)
                assert rejected.conflict and rejected.conflict.reason == "collection_changed"
                assert client.get_reevaluation_result(event.event_id) is None
                assert not client.is_processed(owner + ".managed", event.event_id)
            ops[1] = self.dependencies(layer)
            ops.insert(0, AssertTypeRevision("rule", client.type_revision("rule")))
        request_id = new_id()
        before_events = self.event_count()
        receipt = client.write(ops, context, request_id=request_id).check()
        assert len(receipt.events) == int(changed)
        assert self.event_count() == before_events + int(changed)
        authoritative = self.clients["dependency"].get_reevaluation_result(event.event_id)
        assert authoritative == result
        assert client.is_processed(owner + ".managed", event.event_id)
        assert len(client.history(current.id)) == current.version + int(changed)
        if duplicate:
            assert client.write(ops, context, request_id=request_id) == receipt
            assert client.is_processed(owner + ".managed", event.event_id)
            assert self.event_count() == before_events + int(changed)
            assert client.get_reevaluation_result(event.event_id) == result
            assert len(client.history(current.id)) == current.version + int(changed)
        client._consumer("ack", owner + ".managed", delivery_token=delivery["delivery_token"])
        self.no_restart()
        return receipt

    def generation(self, owner):
        with connection(self.dsn) as conn:
            row = conn.execute(
                "SELECT generation FROM storage.consumers WHERE name=%s",
                (owner + ".managed",),
            ).fetchone()
            assert row is not None
            return row[0]

    def event_count(self):
        # Read-only infrastructure assertion; never inject plugin rows or completions.
        with connection(self.dsn) as conn:
            row = conn.execute("SELECT count(*) FROM storage.outbox").fetchone()
            assert row is not None
            return row[0]


@pytest.fixture
def chain(setup):
    with ExitStack() as stack:
        instance = Chain(setup, stack)
        instance.seed()
        yield instance
        instance.no_restart()


def cause(record, old=None):
    return Cause(
        data_type=record.data_type, data_id=record.id, old_version=old, new_version=record.version
    )


def test_live_multilayer_diamond_completion_roots_and_atomic_rejections(chain):
    c = chain
    person = c.current("person")
    c.clients["attribution"].write(
        [
            Update(
                "person",
                person.id,
                1,
                {"name": "Synthetic A", "references": ["synthetic-unused-reference"]},
            )
        ],
        SYS,
    ).check()
    assert c.clients["dependency"].stale_dependencies([person.id]) == []
    assert c.published == []  # reference-only change is not a selected name input
    root_a, root_b = new_id(), new_id()
    c.clients["attribution"].write(
        [Update("person", person.id, 2, {"name": "Synthetic B", "references": []})],
        Context.feedback(root_a),
    ).check()
    activity = c.current("activity")
    c.clients["extraction"].write(
        [Update("activity", activity.id, 1, {"n": 2})], Context.feedback(root_b)
    ).check()
    stale = c.clients["dependency"].stale_dependencies([person.id, activity.id])
    assert {s.dependent.id for s in stale} == {c.ids["alpha_first"]}
    assert {s.depends_on.id for s in stale} == {person.id, activity.id}
    trace = TracedCause(cause=cause(c.current("activity"), 1), root_feedback_id=root_b, depth=1)
    first = c.publish(
        "alpha_first", cause(c.current("person"), 2), Context(root_a, 1, additional_causes=(trace,))
    )
    assert first is not None
    receipt = c.finish("alpha_first", first, 2, duplicate=True, guard=True)
    change = receipt.events[0]
    assert isinstance(change, DataChanged)
    second = c.publish(
        "beta_second",
        cause(c.current("alpha_first"), 1),
        Context.of(change).deeper(),
        waits=(first,),
    )
    assert second is not None and second.trigger_request_event_id == first.event_id
    unchanged = c.finish("beta_second", second, 1)
    assert unchanged.events == () and c.current("beta_second").version == 1
    # No data_changed exists for beta; its authoritative Finish releases this wait.
    third = c.publish(
        "alpha_third",
        cause(c.current("alpha_first"), 1),
        Context.of(second).deeper(),
        waits=(first, second),
    )
    assert third is not None
    c.finish("alpha_third", third, 2)
    fourth = c.publish(
        "alpha_fourth",
        cause(c.current("alpha_third"), 1),
        Context.of(third).deeper(),
        waits=(second, third),
    )
    assert fourth is not None
    c.finish("alpha_fourth", fourth, 2)
    for event in (first, second, third, fourth):
        result = c.clients["dependency"].get_reevaluation_result(event.event_id)
        assert result is not None
        assert {
            result.root_feedback_id,
            *(t.root_feedback_id for t in result.additional_causes),
        } == {root_a, root_b}
        assert result.additional_causes == event.additional_causes
    assert c.clients["dependency"].stale_dependencies(list(c.ids.values())) == []
    before = c.event_count()
    with pytest.raises((InvalidRequest, PermissionDenied)):
        c.clients["extraction"].write(
            [RegisterDependencies("activity", activity.id, [c.ref("alpha_first")])], SYS
        )
    # Final batch graph contains a two-node cycle; BOTH changes must roll back.
    a, b = c.current("alpha_first"), c.current("alpha_third")
    conflict = c.clients["alpha_first"].write(
        [
            Update(a.data_type, a.id, a.version, {"value": 999}),
            Update(b.data_type, b.id, b.version, {"value": 999}),
            RegisterDependencies(
                a.data_type,
                a.id,
                [VersionRef(data_type=b.data_type, id=b.id, version=b.version + 1)],
            ),
            RegisterDependencies(
                b.data_type,
                b.id,
                [VersionRef(data_type=a.data_type, id=a.id, version=a.version + 1)],
            ),
        ],
        SYS,
    )
    assert conflict.conflict and conflict.conflict.reason == "dependency_cycle"
    assert c.current("alpha_first") == a and c.current("alpha_third") == b
    assert c.event_count() == before


def test_live_failure_admin_recovery_watermark_and_finite_current_history(chain):
    c = chain
    old = {layer: c.current(layer) for layer in LAYERS}
    failed = c.publish("beta_second", cause(c.current("alpha_first")), SYS.deeper())
    assert failed is not None
    c.finish("beta_second", failed, 1, failed=True)
    before = c.event_count()
    assert (
        c.publish(
            "alpha_third",
            cause(c.current("beta_second")),
            Context.of(failed).deeper(),
            waits=(failed,),
        )
        is None
    )
    assert c.blocked == {"alpha_third": "synthetic_driver_upstream_failed"}
    assert c.event_count() == before and c.published == [failed.event_id]
    assert {layer: c.current(layer) for layer in LAYERS} == old

    _, disabled = c.manage("alpha", "disable")
    alpha = c.clients["alpha_first"]
    for operation in (
        lambda: alpha.get(old["alpha_first"].id),
        lambda: alpha._consumer("fetch", "alpha.managed"),
    ):
        with pytest.raises(AuthenticationError):
            operation()
    c.clients["attribution"].write(
        [Update("person", c.ids["person"], 1, {"name": "Synthetic skipped", "references": []})],
        SYS,
    ).check()
    deps_before = c.clients["dependency"].dependency_set(old["alpha_first"].id)
    c.manage("alpha", "enable", resume_choice="skip", expected_revision=disabled["revision"])
    assert alpha.get(old["alpha_first"].id) == old["alpha_first"]
    assert alpha.dependency_set(old["alpha_first"].id) == deps_before
    assert alpha.stale_dependencies([c.ids["person"]])
    after_skip = new_id()
    c.clients["extraction"].write([Create("activity", after_skip, {"n": 10})], SYS).check()
    c.relay()
    seen = c.drain("alpha")
    assert isinstance(seen, list) and after_skip in seen and c.ids["person"] not in seen

    # Empty beta backlog first: exactly one new finite input while disabled.
    c.drain("beta")
    _, disabled = c.manage("beta", "disable")
    historical = new_id()
    c.clients["extraction"].write(
        [Create("activity", historical, {"n": 20}, event_start=DAY, event_end=DAY)], SYS
    ).check()
    accepted, recovered = c.manage(
        "beta", "enable", resume_choice="catch_up", expected_revision=disabled["revision"]
    )
    beta = c.clients["beta_second"]
    job = recovered["history_job_id"]
    with pytest.raises(PermissionDenied):
        beta._consumer("fetch", "beta.managed")
    assert beta.history_job(job)["items"] == {"candidate": 1}
    # Mutate the candidate and add >W input BEFORE claim: read current, not old events.
    future = new_id()
    c.clients["extraction"].write(
        [Update("activity", historical, 1, {"n": 21}), Create("activity", future, {"n": 99})], SYS
    ).check()
    lease = beta.claim_history_job(job)
    assert lease["status"] == "claimed" and lease["item"]["id"] == historical
    snapshot = beta.read_snapshot(lease["snapshot"]["snapshot_id"])
    current = snapshot.get(historical)
    assert isinstance(current, SemanticRecord) and current.version == 2
    output = new_id()
    ops = [
        Create("beta_second", output, {"value": current.payload["n"]}),
        RegisterDependencies(
            "beta_second", output, [VersionRef(data_type="activity", id=historical, version=2)]
        ),
    ]
    args = (
        job,
        lease["claim_token"],
        lease["fence"],
        ops,
        Context.feedback(accepted["feedback_id"]),
    )
    rid = new_id()
    progress = beta.commit_history_job(*args, request_id=rid)
    assert progress["job_state"] == "completed" and progress["items"] == {"completed": 1}
    assert beta.commit_history_job(*args, request_id=rid) == progress
    assert beta.claim_history_job(job)["status"] == "completed"
    saved = beta.get(output)
    assert isinstance(saved, SemanticRecord) and saved.payload == {"value": 21}
    assert len(beta.history(output)) == 1
    assert c.clients["dependency"].list_history_reevaluations(job)["items"] == []
    c.relay()
    seen = c.drain("beta")
    assert isinstance(seen, list) and future in seen
    assert beta.history_job(job)["items"] == {"completed": 1}
    generation = c.generation("beta")
    c.manage(
        "beta",
        "set_permissions",
        read_types=("beta_second", "beta_completion", "alpha_first"),
        subscribe_types=("beta_second",),
    )
    assert c.generation("beta") != generation
    assert beta.get(historical) is None and beta.get_version(historical, 1) is None
    with pytest.raises(PermissionDenied):
        beta.query(Query("activity"))
    assert beta.get(output) == saved
    c.no_restart()
    c.manage("beta", "revoke_credentials")
    for operation in (lambda: beta.get(output), lambda: beta._consumer("fetch", "beta.managed")):
        with pytest.raises(AuthenticationError):
            operation()
    assert c.clients["dependency"].get(output) == saved
    assert c.current("beta_second") == old["beta_second"]
    c.no_restart()
