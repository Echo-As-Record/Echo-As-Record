"""Real PostgreSQL + JetStream + HTTP trace authorization and complete execution."""

import asyncio

import pytest
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    DataChanged,
    DataTypeSpec,
    ReevaluationRequested,
    new_id,
)
from ear_storage.http import create_app
from ear_storage.relay import RelayStore, run_relay
from ear_storage.schema import bootstrap
from ear_storage_client import Context, Create, PublishReevaluation, Update
from ear_storage_client.remote import RemoteStorageClient
from fastapi.testclient import TestClient

from .test_completion import finish
from .test_consumers import command
from .test_consumers import setup as setup
from .test_http_postgres import live_server
from .test_multi_causality import trace
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]


@pytest.mark.parametrize("mode", ["success", "queued", "dlq"])
def test_http_multi_origin_delivery_execution_and_revoke(setup, mode):
    from ear_storage_client import MarkProcessed, RegisterDependencies

    dsn, store, cfg, _, service, operator, _ = setup
    bootstrap(
        dsn,
        DEFAULT_REGISTRY.extend(
            DataTypeSpec("synthetic_result", "semantic", "extraction", supports_reevaluation=True)
        ),
    )
    issued = operator.issue_principal(
        "extraction",
        read_types=["synthetic_result", "rule", "person", "extraction_completion"],
        subscribe_types=["synthetic_result"],
    )
    target, source, secondary = new_id(), new_id(), new_id()
    store.client("extraction").write(
        [Create("synthetic_result", target, {"n": 1}), Create("rule", source, {})], Context.system()
    ).check()
    store.client("attribution").write([Create("person", secondary, {})], Context.system()).check()
    with TestClient(create_app(service)) as http:
        assert (
            command(http, issued, "register", event_types=["reevaluation_requested"]).status_code
            == 200
        )
        request = (
            store.client("dependency")
            .write(
                [
                    PublishReevaluation(
                        "synthetic_result",
                        target,
                        Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                        "PRIVATE_DESCRIPTION",
                        additional_causes=(trace(secondary, dtype="person"),),
                    )
                ],
                Context(new_id(), 2),
            )
            .check()
            .events[0]
        )
        assert isinstance(request, ReevaluationRequested)
        assert asyncio.run(run_relay(RelayStore(dsn, cfg), once=True)).delivered == 4
        if mode == "queued":
            operator.set_grants(
                issued.principal_id,
                read_types=["synthetic_result", "rule", "extraction_completion"],
                subscribe_types=["synthetic_result"],
            )
            response = command(http, issued, "fetch")
            assert response.status_code == 403
            assert secondary not in response.text and "PRIVATE" not in response.text
            return
        response = command(http, issued, "fetch")
        assert response.status_code == 200, response.text
        delivery = response.json()["delivery"]
        event = ReevaluationRequested.model_validate(delivery["event"])
        assert event == request
        if mode == "dlq":
            import time

            assert (
                command(http, issued, "nack", delivery_token=delivery["delivery_token"]).status_code
                == 200
            )
            time.sleep(0.04)
            delivery = command(http, issued, "fetch").json()["delivery"]
            assert delivery is not None
            assert (
                command(http, issued, "nack", delivery_token=delivery["delivery_token"]).status_code
                == 200
            )
            operator.set_grants(
                issued.principal_id,
                read_types=["synthetic_result", "rule", "extraction_completion"],
                subscribe_types=["synthetic_result"],
            )
            response = command(http, issued, "replay", delivery_id=delivery["delivery_id"])
            assert response.status_code == 403
            assert secondary not in response.text and "PRIVATE" not in response.text
            return
        op = finish(
            event.event_id,
            target,
            target_type="synthetic_result",
            root_feedback_id=event.root_feedback_id,
            depth=event.depth,
            additional_causes=event.additional_causes,
            outcome="changed",
            after_version=2,
        )
        with live_server(service) as url, RemoteStorageClient(issued.token, url) as sdk:
            receipt = sdk.write(
                [
                    Update("synthetic_result", target, 1, {"n": 2}),
                    RegisterDependencies("synthetic_result", target, []),
                    op,
                    MarkProcessed("extraction.worker", event.event_id),
                ],
                Context.of(event),
            ).check()
            assert isinstance(receipt.events[0], DataChanged)
            assert receipt.events[0].request_event_id == event.event_id
            assert receipt.events[0].additional_causes == event.additional_causes
            with sdk.snapshot() as snapshot:
                assert snapshot.get_reevaluation_result(event.event_id) == op.result
        assert (
            command(http, issued, "ack", delivery_token=delivery["delivery_token"]).status_code
            == 200
        )
