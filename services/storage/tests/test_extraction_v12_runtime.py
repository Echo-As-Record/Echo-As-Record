"""One opt-in isolated PG/HTTP v1.2 wire test. Synthetic model; no audio/calendar."""

import pytest
from ear_contracts import Cause, DataChanged, ReevaluationRequested, SemanticRecord, new_id
from ear_extraction.demo import DemoGateway, fixture_registry
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.orchestration import (
    COMPLETION_CONSUMER,
    CONSUMER,
    ExtractionService,
    reevaluation_run_id,
)
from ear_extraction.persistence import records
from ear_extraction.segmentation import RAW_TYPES
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.service import StorageService
from ear_storage_client import AppendRaw, Context, Create, PublishReevaluation, Update
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.testing import make_raw, ts

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
                    end="2026-11-05T09:01:00Z",
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
