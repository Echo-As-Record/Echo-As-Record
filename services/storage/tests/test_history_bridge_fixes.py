"""P5 bridge regressions: actual owner transport, historical evidence and finality."""

import pytest
from ear_contracts import (
    Cause,
    ReevaluationRequested,
    ReevaluationResult,
    VersionRef,
    new_id,
    utcnow,
)
from ear_storage.history import job_status
from ear_storage.schema import connection
from ear_storage_client import (
    Context,
    DependencyInput,
    FinishReevaluation,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    RegisterDependencies,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient

from .test_history_scheduler import arrange, finish, finish_current, publish
from .test_http_postgres import live_server
from .test_management_integration import setup as setup
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]


@pytest.mark.parametrize("consumer", ["sample.scheduler", "sample.managed"])
def test_owner_live_http_claim_finish(setup, consumer):
    plugin, _issued, client, scheduler, source, targets, job, feedback, root = arrange(setup)
    finish_current(client, job, feedback)
    # Only scheduler sees its own Publish response. The executor receives nothing but job id.
    scheduler.attach_history_request(
        job, targets[0], publish(scheduler, source, targets[0], root).event_id
    )
    with live_server(setup[3]) as url, RemoteStorageClient(plugin.token, url) as worker:
        assignment = worker.claim_history_assignment(job, consumer=consumer)
        again = worker.claim_history_assignment(job, consumer=consumer)
        assert again == assignment
        event = ReevaluationRequested.model_validate(assignment["event"])
        assert assignment["consumer"] == consumer
        current = worker.get(event.target_id)
        actual = worker.get(event.cause.data_id)
        assert current is not None and actual is not None
        result = ReevaluationResult(
            request_event_id=event.event_id,
            target_type=event.target_type,
            target_id=event.target_id,
            outcome="unchanged",
            before_version=current.version,
            after_version=current.version,
            root_feedback_id=event.root_feedback_id,
            depth=event.depth,
            completed_at=utcnow(),
            additional_causes=event.additional_causes,
        )
        ops = [
            FinishReevaluation(result),
            MarkProcessed(assignment["consumer"], event.event_id),
            RegisterDependencies(
                event.target_type,
                event.target_id,
                [VersionRef(data_type=actual.data_type, id=actual.id, version=actual.version)],
            ),
        ]
        worker.write(ops, Context.of(event)).check()
        assert not worker.write(ops, Context.of(event)).ok
        assert worker.history_job(job)["state"] == "completed"
        with pytest.raises((InvalidRequest, PermissionDenied)):
            worker.claim_history_assignment(job, consumer=consumer)
    assert job_status(scheduler, job)["state"] == "completed"


def test_claim_requires_actual_consumer_and_owner(setup):
    plugin, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    scheduler.attach_history_request(
        job, targets[0], publish(scheduler, source, targets[0], root).event_id
    )
    for who, consumer in [
        (client, "sample.invented"),
        (scheduler, "sample.scheduler"),
        (client, "dependency.foreign"),
    ]:
        with pytest.raises(PermissionDenied):
            who.claim_history_assignment(job, consumer=consumer)
    claimed = client.claim_history_assignment(job, consumer="sample.scheduler")
    assert claimed["event"]["target_id"] == targets[0]
    from ear_storage_client.consumer_wire import ConsumerRequest

    with pytest.raises(PermissionDenied):
        setup[3].consumers.fetch(plugin.token, ConsumerRequest(consumer="sample.managed"))


def test_completion_empty_override_cannot_hide_original_input(setup):
    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    finish(client, event, source)
    client.write([RegisterDependencies("sample_item", targets[0], [])], Context.system()).check()
    setup[1].client("extraction").write(
        [Update("activity", source, 2, {"n": 3})], Context.system()
    ).check()
    assert job_status(scheduler, job)["error_code"] == "newrequest_needed"


def test_completion_fields_keep_unused_name_fresh(setup):
    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    result = ReevaluationResult(
        request_event_id=event.event_id,
        target_type="sample_item",
        target_id=targets[0],
        outcome="unchanged",
        before_version=1,
        after_version=1,
        root_feedback_id=event.root_feedback_id,
        depth=event.depth,
        completed_at=utcnow(),
    )
    client.write(
        [
            FinishReevaluation(result),
            MarkProcessed("sample.scheduler", event.event_id),
            RegisterDependencies(
                "sample_item",
                targets[0],
                [
                    DependencyInput(
                        VersionRef(data_type="activity", id=source, version=2), fields=("n",)
                    )
                ],
            ),
        ],
        Context.of(event),
    ).check()
    setup[1].client("extraction").write(
        [Update("activity", source, 2, {"n": 2, "name": "new"})], Context.system()
    ).check()
    assert job_status(scheduler, job)["error_code"] is None


def test_completed_job_never_reopens(setup):
    _, _, client, scheduler, source, targets, job, feedback, root = arrange(setup)
    finish_current(client, job, feedback)
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    finish(client, event, source)
    before = job_status(scheduler, job)
    assert before["state"] == "completed"
    with connection(setup[0]) as conn:
        cursors = conn.execute(
            "SELECT consumer,data_type,effective_commit_seq FROM storage.consumer_type_cursors "
            "ORDER BY consumer,data_type"
        ).fetchall()
    setup[1].client("extraction").write(
        [Update("activity", source, 2, {"n": 3})], Context.system()
    ).check()
    assert job_status(scheduler, job) == before
    newer = publish(scheduler, source, targets[0], root, version=3)
    with pytest.raises((InvalidRequest, PermissionDenied)):
        scheduler.attach_history_request(job, targets[0], newer.event_id)
    with connection(setup[0]) as conn:
        assert (
            conn.execute(
                "SELECT consumer,data_type,effective_commit_seq FROM storage.consumer_type_cursors "
                "ORDER BY consumer,data_type"
            ).fetchall()
            == cursors
        )


def test_extra_source_is_completion_evidence(setup):
    from ear_storage_client import Create

    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    other = new_id()
    setup[1].client("extraction").write(
        [Create("activity", other, {"n": 1})], Context.system()
    ).check()
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    result = ReevaluationResult(
        request_event_id=event.event_id,
        target_type="sample_item",
        target_id=targets[0],
        outcome="unchanged",
        before_version=1,
        after_version=1,
        root_feedback_id=event.root_feedback_id,
        depth=event.depth,
        completed_at=utcnow(),
        extra_causes=(Cause(data_type="activity", data_id=other, old_version=None, new_version=1),),
    )
    client.write(
        [
            FinishReevaluation(result),
            MarkProcessed("sample.scheduler", event.event_id),
            RegisterDependencies(
                "sample_item", targets[0], [VersionRef(data_type="activity", id=source, version=2)]
            ),
        ],
        Context.of(event),
    ).check()
    assert job_status(scheduler, job)["error_code"] is None
    setup[1].client("extraction").write(
        [Update("activity", other, 1, {"n": 2})], Context.system()
    ).check()
    assert job_status(scheduler, job)["error_code"] == "newrequest_needed"


@pytest.mark.parametrize("terminal", [False, True])
def test_extra_source_new_version_policy(setup, terminal):
    from ear_storage_client import Create, Supersede

    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    other = new_id()
    writer = setup[1].client("extraction")
    writer.write([Create("activity", other, {})], Context.system()).check()
    writer.write(
        [Supersede("activity", other, 1)] if terminal else [Update("activity", other, 1, {"n": 2})],
        Context.system(),
    ).check()
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    result = ReevaluationResult(
        request_event_id=event.event_id,
        target_type="sample_item",
        target_id=targets[0],
        outcome="unchanged",
        before_version=1,
        after_version=1,
        root_feedback_id=event.root_feedback_id,
        depth=event.depth,
        completed_at=utcnow(),
        extra_causes=(
            Cause(
                data_type="activity", data_id=other, old_version=1, new_version=2 if terminal else 1
            ),
        ),
    )
    response = client.write(
        [
            FinishReevaluation(result),
            MarkProcessed("sample.scheduler", event.event_id),
            RegisterDependencies(
                "sample_item", targets[0], [VersionRef(data_type="activity", id=source, version=2)]
            ),
        ],
        Context.of(event),
    )
    if terminal:
        response.check()
        assert job_status(scheduler, job)["error_code"] is None
        assert job_status(scheduler, job)["error_code"] is None
    else:
        assert not response.ok and response.conflict.reason == "stale_dependency"


@pytest.mark.parametrize("consumer", ["sample.scheduler", "sample.managed"])
def test_assignment_marker_scope_and_revocation(setup, consumer):
    from ear_storage.auth import AuthenticationError

    from .test_history import recover

    plugin, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    assignment = client.claim_history_assignment(job, consumer=consumer)
    assert assignment["event"]["event_id"] == event.event_id
    alien = publish(scheduler, source, targets[0], root)
    with pytest.raises(PermissionDenied):
        client.write([MarkProcessed(consumer, alien.event_id)], Context.of(alien))
    with pytest.raises(PermissionDenied):
        client.write([MarkProcessed(consumer, event.event_id)], Context.of(event))
    assert recover(setup, "skip")[1]["status"] == "succeeded"
    with pytest.raises((InvalidRequest, PermissionDenied)):
        client.claim_history_assignment(job, consumer=consumer)
    setup[4].revoke(plugin.principal_id)
    with pytest.raises(AuthenticationError):
        client.claim_history_assignment(job, consumer=consumer)


def test_range_without_decision_freezes(setup):
    from ear_storage.history_execution import claim, commit

    from .test_history_execution import arrange as range_arrange
    from .test_history_execution import chunk

    _, client, ids, job, feedback = range_arrange(setup)
    lease = claim(client, job)
    commit(client, job, chunk(client, lease, feedback)[1])
    before = job_status(client, job)
    assert before["state"] == "completed"
    setup[1].client("extraction").write(
        [Update("activity", ids[0], 1, {"n": 50})], Context.system()
    ).check()
    assert job_status(client, job) == before
    assert claim(client, job)["status"] == "completed"
    with pytest.raises(InvalidRequest, match="final"):
        client.claim_history_assignment(job, consumer="sample.managed")


def test_superseded_job_get_is_read_only(setup):
    from .test_history import recover

    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    scheduler.attach_history_request(
        job, targets[0], publish(scheduler, source, targets[0], root).event_id
    )
    assert recover(setup, "skip")[1]["status"] == "succeeded"
    first = job_status(client, job)
    assert first["error_code"] == "recovery_superseded"
    assert job_status(scheduler, job) == first
    assert job_status(client, job) == first


def test_new_trace_source_and_current_new_dependencies(setup):
    from ear_contracts import TracedCause
    from ear_storage_client import Create

    _, _, client, scheduler, source, targets, job, _, root = arrange(setup)
    other = new_id()
    writer = setup[1].client("extraction")
    writer.write([Create("activity", other, {})], Context.system()).check()
    event = publish(scheduler, source, targets[0], root)
    scheduler.attach_history_request(job, targets[0], event.event_id)
    trace = TracedCause(
        cause=Cause(data_type="activity", data_id=other, old_version=None, new_version=1),
        root_feedback_id="system",
        depth=1,
    )
    context = Context(event.root_feedback_id, event.depth, event.event_id, (trace,))
    result = ReevaluationResult(
        request_event_id=event.event_id,
        target_type="sample_item",
        target_id=targets[0],
        outcome="unchanged",
        before_version=1,
        after_version=1,
        root_feedback_id=event.root_feedback_id,
        depth=event.depth,
        completed_at=utcnow(),
        additional_causes=(trace,),
    )
    client.write(
        [
            FinishReevaluation(result),
            MarkProcessed("sample.scheduler", event.event_id),
            RegisterDependencies(
                "sample_item", targets[0], [VersionRef(data_type="activity", id=source, version=2)]
            ),
        ],
        context,
    ).check()
    assert job_status(scheduler, job)["error_code"] is None
    writer.write([Update("activity", other, 1, {"n": 2})], Context.system()).check()
    assert job_status(scheduler, job)["error_code"] == "newrequest_needed"
