"""One opt-in isolated PG/HTTP v1.2 wire test. Synthetic model; no audio/calendar."""

from datetime import timedelta

import pytest
from ear_contracts import (
    Cause,
    DataChanged,
    ReevaluationRequested,
    SemanticRecord,
    VersionRef,
    new_id,
)
from ear_contracts.optional_sources import chat_specs
from ear_extraction.demo import DemoGateway, fixture_registry
from ear_extraction.domain import Command, Interval, MixedTextScope, Rule
from ear_extraction.orchestration import (
    COMPLETION_CONSUMER,
    CONSUMER,
    ExtractionService,
    reevaluation_run_id,
)
from ear_extraction.persistence import records
from ear_extraction.segmentation import RAW_TYPES
from ear_storage import PostgresStorage, SnapshotLimits, bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.service import StorageService
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    PermissionDenied,
    PublishReevaluation,
    Query,
    RegisterDependencies,
    Update,
)
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import ResourceLimit
from ear_storage_client.testing import make_raw, ts
from ear_storage_client.wire import SnapshotProfile

from .test_http_postgres import live_server

pytestmark = pytest.mark.postgres


def test_v12_selection_and_artifact_finish_over_real_pg_http(pg_dsn):
    # Fixture-only trusted registration; DEFAULT_REGISTRY and production untouched.
    migrate(pg_dsn)
    bootstrap(pg_dsn, fixture_registry())
    store = PostgresStorage(pg_dsn)
    voice, transcript, person, attribution = [new_id() for _ in range(4)]
    store.client("adapter").write(
        [
            AppendRaw(
                make_raw(
                    "voiceprint",
                    {"speaker": "A"},
                    id=voice,
                    start="2026-11-05T09:00:00Z",
                    end="2026-11-05T09:01:00Z",
                )
            ),
            AppendRaw(
                make_raw(
                    "transcript_segment",
                    {"text": "合成待办", "voiceprint_id": voice},
                    id=transcript,
                    start="2026-11-05T09:00:00Z",
                    end="2026-11-05T09:10:00Z",
                )
            ),
        ],
        Context.system(),
    ).check()
    store.client("attribution").write(
        [
            Create("person", person, {"name": "合成人物", "references": [voice]}),
            Create(
                "attribution",
                attribution,
                {"subject_id": voice, "target_type": "person", "target_id": person},
            ),
        ],
        Context.system(),
    ).check()
    operator = AuthOperator(pg_dsn)
    issued = operator.issue_principal(
        "extraction",
        read_types=set(RAW_TYPES)
        | {
            "voiceprint",
            "person",
            "place",
            "attribution",
            "feedback",
            "activity",
            "rule",
            "rule_selection",
            "artifact",
            "action_suggestion",
            "extraction_run",
            "extraction_completion",
        },
        subscribe_types=["feedback", "artifact", "rule_selection"],
    )
    for consumer in (CONSUMER, COMPLETION_CONSUMER):
        operator.reserve_consumer_identity(consumer, issued.principal_id)
    with (
        live_server(StorageService(pg_dsn)) as url,
        RemoteStorageClient(issued.token, url) as client,
    ):
        worker = ExtractionService(client, DemoGateway())

        def command(value):
            feedback = make_raw(
                "feedback",
                {"module": "extraction", "command": value.model_dump(mode="json")},
                start="2026-11-05T10:00:00Z",
            )
            event = (
                store.client("api")
                .write([AppendRaw(feedback)], Context.feedback(feedback.id))
                .check()
                .events[0]
            )
            assert isinstance(event, DataChanged)
            worker.handle_event(event)
            worker.drain()
            run = client.get_system_record(event.event_id)
            assert run is not None and run.payload["status"] == "succeeded", run
            return run

        def request(target):
            event = (
                store.client("dependency")
                .write(
                    [
                        PublishReevaluation(
                            target.data_type,
                            target.id,
                            Cause(
                                data_type="attribution",
                                data_id=attribution,
                                old_version=None,
                                new_version=1,
                            ),
                            "synthetic request",
                        )
                    ],
                    Context.system().deeper(),
                )
                .check()
                .events[0]
            )
            assert isinstance(event, ReevaluationRequested)
            worker.handle_event(event)
            worker.drain()
            finish = client.get_reevaluation_result(event.event_id)
            assert finish is not None, client.get_system_record(reevaluation_run_id(event.event_id))
            worker.handle_event(event)
            worker.drain()
            assert client.get_reevaluation_result(event.event_id) == finish
            assert client.is_processed(COMPLETION_CONSUMER, event.event_id)
            return finish

        command(
            Command(
                kind="extract",
                interval=Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z")),
            )
        )
        selection = records(client, "rule_selection")[0]
        assert selection.payload["rule_ids"] == []
        command(
            Command(
                kind="rule_create", rule=Rule(scope="合成", instruction="合成", action="reminder")
            )
        )
        assert not records(client, "artifact")
        assert request(selection).outcome == "changed"
        artifact = records(client, "artifact")[0]
        selection = records(client, "rule_selection")[0]
        assert request(selection).outcome == "unchanged"
        assert request(artifact).outcome == "unchanged"
        deps = client.dependencies_of(artifact.id)
        assert next(d for d in deps if d.depends_on.id == person).fields == ("name",)
        assert all(d.depends_on.data_type != "rule_selection" for d in deps)
        old_person = client.get(person)
        assert isinstance(old_person, SemanticRecord)
        store.client("attribution").write(
            [Update("person", person, 1, {**old_person.payload, "name": "合成改名"})],
            Context.system(),
        ).check()
        assert request(artifact).outcome == "changed"
        rule = records(client, "rule")[0]
        command(Command(kind="rule_delete", target_id=rule.id, expected_version=1))
        finish = request(artifact)
        assert finish.outcome == "not_applicable" and finish.disposition == "terminated"
        assert finish.extra_causes and finish.additional_causes
        terminal = client.get(artifact.id)
        assert isinstance(terminal, SemanticRecord) and terminal.superseded
        assert client.get_version(artifact.id, 1) == artifact
        assert not records(client, "action_suggestion")


def test_mixed_text_graphless_snapshot_keeps_full_grant_subset_over_real_pg_http(pg_dsn):
    migrate(pg_dsn)
    bootstrap(pg_dsn, fixture_registry().extend_core(*chat_specs()))
    store = PostgresStorage(pg_dsn)
    start = ts("2026-11-22T14:00:00Z")
    interval = Interval(start=start, end=start + timedelta(minutes=30))
    transcript = make_raw(
        "transcript_segment",
        {"text": "synthetic transcript"},
        start=start,
        end=start + timedelta(minutes=1),
    )
    message = make_raw(
        "chat_message",
        {
            "conversation_id": "synthetic",
            "message_id": "one",
            "role": "user",
            "text": "synthetic message",
        },
        start=start + timedelta(minutes=12),
    )
    voiceprint = make_raw("voiceprint", {}, start=start)
    store.client("adapter").write(
        [AppendRaw(r) for r in (transcript, message, voiceprint)],
        Context.system(),
    ).check()
    local = store.client("extraction")
    rule = new_id()
    local.write(
        [Create("rule", rule, Rule(scope="synthetic", instruction="notes").model_dump())],
        Context.system(),
    ).check()
    old = [new_id() for _ in range(22)]
    local.write(
        [
            op
            for id in old
            for op in (
                Create(
                    "activity",
                    id,
                    {},
                    event_start=start - timedelta(days=1),
                    event_end=start - timedelta(hours=1),
                ),
                RegisterDependencies(
                    "activity", id, [VersionRef(data_type="rule", id=rule, version=1)]
                ),
            )
        ],
        Context.system(),
    ).check()
    feedback = make_raw(
        "feedback",
        {
            "module": "extraction",
            "command": Command(kind="extract", interval=interval).model_dump(mode="json"),
        },
        start=interval.end,
    )
    [event] = (
        store.client("api")
        .write([AppendRaw(feedback)], Context.feedback(feedback.id))
        .check()
        .events
    )
    assert isinstance(event, DataChanged)
    operator = AuthOperator(pg_dsn)
    grants = {
        "transcript_segment",
        "voiceprint",
        "chat_message",
        "chat_session",
        "chat_attachment",
        "feedback",
        "activity",
        "rule",
        "artifact",
        "extraction_run",
        "rule_selection",
    }
    issued = operator.issue_principal("extraction", read_types=grants, subscribe_types=["feedback"])
    operator.reserve_consumer_identity(CONSUMER, issued.principal_id)

    class Client(RemoteStorageClient):
        # Same existing wire profile selected by PumpStorage in mixed mode.
        def snapshot(self, *, profile: SnapshotProfile | None = None):
            return super().snapshot(profile=profile or "transcript_extraction")

    calls = []

    class Model:
        def complete(self, call):
            calls.append(call.purpose)
            if call.purpose == "segmentation":
                assert [r["id"] for r in call.data["evidence"]] == [transcript.id, message.id]
                return {
                    "activities": [
                        {
                            **call.data["interval"],
                            "title_md": "Synthetic",
                            "evidence_ids": [transcript.id, message.id],
                        }
                    ]
                }
            if call.purpose == "selection":
                return {"rule_ids": [rule]}
            return {"body_md": "Synthetic note", "evidence_ids": [message.id]}

    with (
        live_server(StorageService(pg_dsn, snapshot_limits=SnapshotLimits(max_rows=66))) as url,
        Client(issued.token, url) as client,
    ):
        with pytest.raises(ResourceLimit):
            client.create_snapshot()
        with client.snapshot() as view:
            assert view.count(Query("voiceprint")) == 1  # No implicit type filter.
            assert view.get(message.id) == client.get(message.id)
            assert view.type_revision("chat_message") == client.type_revision("chat_message")
            with pytest.raises(PermissionDenied):
                view.count(Query("gps_point"))
            with pytest.raises(PermissionDenied):
                view.dependencies_of(old[0])
        worker = ExtractionService(
            client,
            Model(),
            chat_evidence=True,
            mixed_text=MixedTextScope(feedback_id=feedback.id, interval=interval),
        )
        worker.handle_event(event)
        assert client.is_processed(CONSUMER, event.event_id)
        worker.process(event.event_id)
        run = client.get_system_record(event.event_id)
        assert run is not None and run.payload["status"] == "succeeded", run
        assert client.query(
            Query("extraction_run", where={"root_feedback_id": feedback.id})
        ).items == (run,)
        assert calls == ["segmentation", "selection", "generation"]
        [artifact] = records(client, "artifact")
        assert artifact.payload["evidence_ids"] == [message.id]
        assert len(client.dependencies_of(artifact.id)) == 2
        assert len(client.dependencies_of(old[0])) == 1
