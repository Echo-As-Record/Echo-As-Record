"""Synthetic scheduler/executor, actual Publish/Finish, isolated PostgreSQL/NATS."""

import json
from typing import Literal

import httpx
import pytest
from ear_contracts import Cause, ReevaluationResult, VersionRef, new_id, utcnow
from ear_storage.auth import AuthenticationError
from ear_storage.history import job_status
from ear_storage.history_execution import claim, commit
from ear_storage.http import create_app
from ear_storage.management_input import ManagementInput
from ear_storage.schema import connection, one
from ear_storage_client import (
    Context,
    Create,
    FinishReevaluation,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    RegisterDependencies,
    Update,
)
from ear_storage_client.consumer_wire import ConsumerRequest, DeliveryCommand
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.wire import encode_request
from fastapi.testclient import TestClient

from .test_history import recover, relay
from .test_management import command
from .test_management_integration import execute
from .test_management_integration import setup as setup
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()
TYPES = ["sample_item", "sample_completion", "activity"]


def arrange(setup, count=1):
    _, backend, _, service, operator, _, _ = setup
    declaration = ManagementInput.model_validate(
        dict(
            request_id=new_id(),
            action="register_plugin",
            module="sample",
            types=[
                dict(
                    name="sample_item",
                    kind="semantic",
                    supports_reevaluation=True,
                    requires_dependencies=True,
                ),
                dict(name="sample_completion", kind="system_record"),
            ],
            read_types=TYPES,
            subscribe_types=["activity", "sample_item"],
        )
    )
    assert execute(setup, declaration)[1]["status"] == "succeeded"
    plugin = operator.issue_principal(
        "sample", read_types=TYPES, subscribe_types=["activity", "sample_item"]
    )
    assert execute(setup, command("enable"))[1]["enabled"]
    operator.reserve_consumer_identity("sample.scheduler", plugin.principal_id)
    issued = operator.issue_principal("dependency", read_types=TYPES)
    scheduler = service.client(issued.token)
    client = service.client(plugin.token)
    source = new_id()
    targets = [new_id() for _ in range(count)]
    backend.client("extraction").write([Create("activity", source, {"n": 1})], SYS).check()
    for target in targets:
        client.write(
            [
                Create("sample_item", target, {"n": 1}),
                RegisterDependencies(
                    "sample_item", target, [VersionRef(data_type="activity", id=source, version=1)]
                ),
            ],
            SYS,
        ).check()
    execute(setup, command("disable"))
    root = new_id()
    backend.client("extraction").write(
        [Update("activity", source, 1, {"n": 2})], Context.feedback(root)
    ).check()
    accepted, result = recover(setup, "catch_up")
    assert result["status"] == "succeeded"
    return (
        plugin,
        issued,
        client,
        scheduler,
        source,
        targets,
        result["history_job_id"],
        accepted["feedback_id"],
        root,
    )


def publish(scheduler, source, target, root, version=2):
    eid = new_id()
    event = (
        scheduler.write(
            [
                PublishReevaluation(
                    "sample_item",
                    target,
                    Cause(
                        data_type="activity",
                        data_id=source,
                        old_version=version - 1 if version > 1 else None,
                        new_version=version,
                    ),
                    "synthetic history",
                    eid,
                )
            ],
            Context.feedback(root).deeper(),
            request_id=new_id(),
        )
        .check()
        .events[0]
    )
    return event


def finish(
    client,
    event,
    source,
    *,
    outcome: Literal["unchanged", "failed", "not_applicable"] = "unchanged",
    version=2,
):
    current = client.get(event.target_id)
    result = ReevaluationResult(
        request_event_id=event.event_id,
        target_type=event.target_type,
        target_id=event.target_id,
        outcome=outcome,
        before_version=current.version,
        after_version=current.version,
        root_feedback_id=event.root_feedback_id,
        depth=event.depth,
        completed_at=utcnow(),
        error_code="synthetic_failed" if outcome == "failed" else None,
        reason_code="protected" if outcome == "not_applicable" else None,
        disposition="kept_protected" if outcome == "not_applicable" else None,
    )
    ops = [FinishReevaluation(result), MarkProcessed("sample.scheduler", event.event_id)]
    if outcome == "unchanged":
        ops.append(
            RegisterDependencies(
                "sample_item",
                event.target_id,
                [VersionRef(data_type="activity", id=source, version=version)],
            )
        )
    return client.write(ops, Context.of(event)).check()


def finish_current(client, job, feedback):
    lease = claim(client, job)
    if lease["status"] == "claimed":
        return commit(
            client,
            job,
            dict(
                claim_token=lease["claim_token"],
                fence=lease["fence"],
                request_id=new_id(),
                batch=json.loads(encode_request([], Context.feedback(feedback))),
                outcome="completed",
                error_code=None,
            ),
        )
    return lease


def test_bridge_crash_retry_real_finish_and_release(setup):
    dsn, backend, _, service, _, _, _ = setup
    plugin, issued, client, scheduler, source, targets, job, feedback, root = arrange(setup)
    target = targets[0]
    page = scheduler.list_history_reevaluations(job)
    assert page["items"][0]["candidate_inputs"][0]["data_id"] == source
    assert "payload" not in json.dumps(page)
    finish_current(client, job, feedback)
    assert claim(client, job)["error_code"] == "scheduler_unavailable"
    event = publish(scheduler, source, target, root)
    # Crash after Publish and before attach: a new client recovers the persisted event.
    scheduler = service.client(issued.token)
    linked = scheduler.attach_history_request(job, target, event.event_id)
    assert scheduler.attach_history_request(job, target, event.event_id) == linked
    assert job_status(scheduler, job)["error_code"] == "waiting_result"
    assert service.consumers is not None
    with pytest.raises(PermissionDenied):
        service.consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
    future = new_id()
    backend.client("extraction").write([Create("activity", future, {})], SYS).check()
    finish(client, event, source)
    assert job_status(scheduler, job)["state"] == "completed"
    assert client.is_processed("sample.scheduler", event.event_id)
    assert client.get_reevaluation_result(event.event_id).root_feedback_id == root
    assert backend.get(target).version == 1
    relay(setup)
    seen = []
    for _ in range(30):
        response = service.consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))
        delivery = response["delivery"]
        if delivery:
            seen.append(delivery["event"].get("data_id"))
            service.consumers.finish(
                plugin.token,
                DeliveryCommand(
                    consumer="sample.managed", delivery_token=delivery["delivery_token"]
                ),
                nack=False,
            )
        elif response["status"] == "idle":
            break
    assert future in seen
    with connection(dsn) as conn:
        assert one(conn, "SELECT count(*) FROM storage.history_request_links")[0] == 1
        assert one(conn, "SELECT count(*) FROM storage.request_completions")[0] == 1


@pytest.mark.parametrize(
    "case", ["forged", "unrelated", "wrong_target", "wrong_module", "different", "old"]
)
def test_reject_untrusted_or_uncovered_assignment(setup, case):
    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    target = targets[0]
    event = publish(scheduler, source, target, root)
    if case == "forged":
        eid = new_id()
    elif case == "unrelated":
        alien = new_id()
        setup[1].client("extraction").write([Create("activity", alien, {})], SYS).check()
        eid = publish(scheduler, alien, target, root, version=1).event_id
    elif case == "wrong_target":
        other = new_id()
        client.write(
            [Create("sample_item", other, {}), RegisterDependencies("sample_item", other, [])], SYS
        ).check()
        eid = publish(scheduler, source, other, root).event_id
    elif case == "wrong_module":
        with pytest.raises(PermissionDenied):
            publish(client, source, target, root)
        with pytest.raises(PermissionDenied):
            client.attach_history_request(job, target, event.event_id)
        with pytest.raises(PermissionDenied):
            setup[3].client(setup[5].token).list_history_reevaluations(job)
        return
    elif case == "different":
        scheduler.attach_history_request(job, target, event.event_id)
        eid = publish(scheduler, source, target, root).event_id
    else:
        # A second explicit approval must not adopt a request from before its boundary.
        finish_current(client, job, arrange_feedback(setup, job))
        claim(client, job)
        _, replacement = recover(setup, "catch_up")
        assert replacement["status"] == "succeeded"
        job = replacement["history_job_id"]
        eid = event.event_id
    with pytest.raises((InvalidRequest, PermissionDenied)):
        scheduler.attach_history_request(job, target, eid)


def arrange_feedback(setup, job):
    with connection(setup[0]) as conn:
        return one(conn, "SELECT feedback_id FROM storage.history_jobs WHERE job_id=%s", (job,))[0]


@pytest.mark.parametrize("outcome", ["failed", "not_applicable"])
def test_terminal_non_success_holds_gate_and_failed_new_assignment(setup, outcome):
    _, _, client, scheduler, source, targets, job, feedback, root = arrange(setup)
    target = targets[0]
    finish_current(client, job, feedback)
    event = publish(scheduler, source, target, root)
    first = scheduler.attach_history_request(job, target, event.event_id)
    finish(client, event, source, outcome=outcome)
    assert job_status(scheduler, job)["state"] == ("failed" if outcome == "failed" else "blocked")
    assert execute(setup, command("status"))[1]["need_recovery_decision"]
    newer = publish(scheduler, source, target, root)
    if outcome == "not_applicable":
        with pytest.raises(InvalidRequest):
            scheduler.attach_history_request(job, target, newer.event_id)
        return
    assert (
        scheduler.attach_history_request(job, target, newer.event_id)["assignment_id"]
        != first["assignment_id"]
    )
    with pytest.raises(InvalidRequest):
        scheduler.attach_history_request(job, target, event.event_id)
    finish(client, newer, source)
    assert job_status(scheduler, job)["state"] == "completed"
    with connection(setup[0]) as conn:
        assert one(conn, "SELECT count(*) FROM storage.history_request_links")[0] == 2


def test_completed_request_then_new_input_cannot_release_mixed_job(setup):
    _, backend, _, _, _, _, _ = setup
    _, _, client, scheduler, source, targets, job, feedback, root = arrange(setup)
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    finish(client, event, source)
    assert job_status(scheduler, job)["state"] == "running"
    backend.client("extraction").write([Update("activity", source, 2, {"n": 3})], SYS).check()
    assert job_status(scheduler, job)["error_code"] == "newrequest_needed"
    finish_current(client, job, feedback)
    assert job_status(scheduler, job)["state"] != "completed"
    assert execute(setup, command("status"))[1]["need_recovery_decision"]
    newer = publish(scheduler, source, targets[0], root, version=3)
    scheduler.attach_history_request(job, targets[0], newer.event_id)
    finish(client, newer, source, version=3)
    assert job_status(scheduler, job)["state"] == "completed"


@pytest.mark.parametrize("change", ["scheduler_grant", "owner_grant", "revoke", "disable"])
def test_current_authority_revocation_on_list_link_replay_and_reconcile(setup, change):
    _, issued, client, scheduler, source, targets, job, _, root = arrange(setup)
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    if change == "scheduler_grant":
        setup[4].set_grants(issued.principal_id, read_types=["sample_item"])
    elif change == "owner_grant":
        execute(setup, command("set_permissions", read_types=("sample_item",)))
    elif change == "revoke":
        setup[4].revoke(issued.principal_id)
    else:
        execute(setup, command("disable"))
    for op in [
        lambda: scheduler.list_history_reevaluations(job),
        lambda: scheduler.attach_history_request(job, targets[0], event.event_id),
        lambda: job_status(scheduler, job),
    ]:
        with pytest.raises((PermissionDenied, AuthenticationError)):
            op()
    if change in {"owner_grant", "disable"}:
        with pytest.raises((PermissionDenied, AuthenticationError)):
            job_status(client, job)


def test_http_sdk_bounded_page_and_explicit_grants(setup):
    _, issued, _, scheduler, source, targets, job, _, root = arrange(setup, count=2)
    no_grants = setup[4].issue_principal("dependency")
    assert setup[3].client(no_grants.token).list_history_reevaluations()["items"] == []
    with TestClient(create_app(setup[3])) as http:

        def forward(request):
            response = http.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, content=response.content)

        with RemoteStorageClient(
            issued.token, "http://testserver", transport=httpx.MockTransport(forward)
        ) as remote:
            page = remote.list_history_reevaluations(job, limit=1)
            assert len(page["items"]) == 1 and page["next_cursor"]
            second = remote.list_history_reevaluations(job, limit=1, cursor=page["next_cursor"])
            assert len(second["items"]) == 1 and second["next_cursor"] is None
            assert page["items"][0]["item_id"] != second["items"][0]["item_id"]
            event = publish(remote, source, targets[0], root)
            link = remote.attach_history_request(job, targets[0], event.event_id)
            assert scheduler.attach_history_request(job, targets[0], event.event_id) == link


def test_real_existing_completion_can_attach_but_not_fake_output(setup):
    _, backend, _, _, _, _, _ = setup
    _, _, client, scheduler, source, targets, job, feedback, root = arrange(setup)
    event = publish(scheduler, source, targets[0], root)
    # Deployment-local executor uses the same Finish validator/index, not a fabricated row.
    finish(backend.client("sample"), event, source)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    assert job_status(scheduler, job)["state"] == "running"
    finish_current(client, job, feedback)
    assert job_status(scheduler, job)["state"] == "completed"
    assert backend.get(targets[0]).version == 1


def test_old_skipped_request_never_restores_an_approved_job(setup):
    _, _, client, scheduler, source, targets, _job, _, root = arrange(setup)
    event = publish(scheduler, source, targets[0], root)
    assert recover(setup, "skip")[1]["status"] == "succeeded"
    execute(setup, command("disable"))
    _, result = recover(setup, "catch_up")
    assert result["status"] == "succeeded"
    newer_job = result["history_job_id"]
    with pytest.raises(PermissionDenied):
        scheduler.attach_history_request(newer_job, targets[0], event.event_id)
    newer = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(newer_job, targets[0], newer.event_id)
    finish(client, newer, source)
    assert job_status(scheduler, newer_job)["state"] == "completed"


def test_linked_completion_retains_overlapping_recovery_gate(setup):
    _, _, client, scheduler, source, targets, job, feedback, root = arrange(setup)
    finish_current(client, job, feedback)
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    # Reproduce a persisted legacy overlap; neither job may bypass the other's hold.
    with connection(setup[0]) as conn:
        revision = one(conn, "SELECT revision FROM storage.module_lifecycle WHERE module='sample'")[
            0
        ]
        conn.execute(
            "INSERT INTO storage.recovery_decisions SELECT %s,d.feedback_id,d.module,%s,d.mode,"
            "d.types_text,d.watermark,d.coverage_lower_bound,'authorized' "
            "FROM storage.recovery_decisions d JOIN storage.history_jobs j USING(decision_id) "
            "WHERE j.job_id=%s",
            (new_id(), revision, job),
        )
    finish(client, event, source)
    assert job_status(scheduler, job)["state"] == "completed"
    assert execute(setup, command("status"))[1]["need_recovery_decision"]


def test_two_current_stale_origins_require_coverage_and_retain_roots(setup):
    from ear_contracts import TracedCause

    _, backend, _, _, _, _, _ = setup
    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    target = targets[0]
    second = new_id()
    backend.client("extraction").write([Create("activity", second, {"n": 1})], SYS).check()
    # Preserve original candidate coverage while adding a newly declared dependency.
    client.write(
        [
            RegisterDependencies(
                "sample_item",
                target,
                [
                    VersionRef(data_type="activity", id=source, version=2),
                    VersionRef(data_type="activity", id=second, version=1),
                ],
            )
        ],
        SYS,
    ).check()
    backend.client("extraction").write([Update("activity", second, 1, {"n": 2})], SYS).check()
    unrelated = publish(scheduler, second, target, root)
    with pytest.raises(InvalidRequest, match="coverage"):
        scheduler.attach_history_request(job, target, unrelated.event_id)
    event = publish(scheduler, source, target, root)
    with pytest.raises(InvalidRequest, match="coverage"):
        scheduler.attach_history_request(job, target, event.event_id)
    trace = TracedCause(
        cause=Cause(data_type="activity", data_id=second, old_version=1, new_version=2),
        root_feedback_id="system",
        depth=1,
    )
    request = (
        scheduler.write(
            [
                PublishReevaluation(
                    "sample_item",
                    target,
                    Cause(data_type="activity", data_id=source, old_version=1, new_version=2),
                    "two origins",
                    new_id(),
                    additional_causes=(trace,),
                )
            ],
            Context.feedback(root).deeper(),
        )
        .check()
        .events[0]
    )
    scheduler.attach_history_request(job, target, request.event_id)
    assert request.root_feedback_id == root and request.additional_causes == (trace,)
