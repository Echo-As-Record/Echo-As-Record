"""Dynamic registered plugin completion over actual PostgreSQL + HTTP + JetStream."""

from __future__ import annotations

import asyncio

import pytest
from ear_contracts import Cause, ReevaluationResult, new_id, utcnow
from ear_storage.management_input import ManagementInput
from ear_storage.relay import RelayStore, run_relay
from ear_storage_client import (
    Context,
    Create,
    FinishReevaluation,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PublishReevaluation,
    PutSystemRecord,
    RegisterDependencies,
    reevaluation_result,
)
from ear_storage_client.remote import RemoteStorageClient

from .test_http_postgres import live_server
from .test_management import command
from .test_management_integration import execute
from .test_management_integration import setup as setup
from .test_relay import env as env

pytestmark = [pytest.mark.postgres, pytest.mark.nats]
SYS = Context.system()


def test_dynamic_plugin_owns_completion_with_authorized_consumer(setup):
    _, store, cfg, service, operator, _, _ = setup
    body = ManagementInput.model_validate(
        dict(
            request_id=new_id(),
            action="register_plugin",
            module="sample",
            types=[
                dict(name="sample_item", kind="semantic", supports_reevaluation=True),
                dict(name="sample_completion", kind="system_record", reevaluation_result=False),
                dict(name="sample_job_completion", kind="system_record"),
            ],
            read_types=["sample_item", "sample_completion", "rule"],
            subscribe_types=["sample_item"],
        )
    )
    _, registered = execute(setup, body)
    assert registered["status"] == "succeeded"
    issued = operator.issue_principal(
        "sample", read_types=body.read_types, subscribe_types=body.subscribe_types
    )
    _, enabled = execute(setup, command("enable"))
    assert enabled["enabled"]
    source, target, event = new_id(), new_id(), new_id()
    store.client("extraction").write([Create("rule", source, {})], SYS).check()
    with live_server(service) as url, RemoteStorageClient(issued.token, url) as client:
        assert client.registry.writer_of("sample_completion") == "sample"
        assert client.registry.get("sample_completion").reevaluation_result
        ordinary = new_id()
        client.write([PutSystemRecord("sample_job_completion", ordinary, {})], SYS).check()
        client.write([Create("sample_item", target, {})], SYS).check()
        store.client("dependency").write(
            [
                PublishReevaluation(
                    "sample_item",
                    target,
                    Cause(data_type="rule", data_id=source, old_version=None, new_version=1),
                    "synthetic",
                    event,
                )
            ],
            SYS.deeper(),
        ).check()
        asyncio.run(run_relay(RelayStore(store._dsn, cfg), once=True))
        result = ReevaluationResult(
            request_event_id=event,
            target_type="sample_item",
            target_id=target,
            outcome="unchanged",
            before_version=1,
            after_version=1,
            root_feedback_id="system",
            depth=1,
            completed_at=utcnow(),
        )
        marker = MarkProcessed("sample.managed", event)
        with pytest.raises(InvalidRequest):
            client.write(
                [
                    PutSystemRecord("sample_completion", event, result.model_dump(mode="json")),
                    marker,
                ],
                SYS.deeper(),
            )
        receipt = client.write(
            [FinishReevaluation(result), RegisterDependencies("sample_item", target, []), marker],
            SYS.deeper(),
        ).check()
        assert receipt.events == () and reevaluation_result(client, event) == result
        assert client.is_processed("sample.managed", event)
        assert client.type_revision("sample_completion") == 1
        assert client.registry.get("sample_item").requires_dependencies is False
        # Another executor cannot use this event to complete its own record.
        alien = operator.issue_principal(
            "extraction", read_types=["sample_item", "rule"], subscribe_types=["sample_item"]
        )
        operator.reserve_consumer_identity("extraction.alien", alien.principal_id)
        with RemoteStorageClient(alien.token, url) as other, pytest.raises(PermissionDenied):
            other.write(
                [FinishReevaluation(result), MarkProcessed("extraction.alien", event)], SYS.deeper()
            )

        # A dynamically registered second plugin may read/subscribe to the same
        # data type, but never complete that type's reevaluation request.
        other_body = ManagementInput.model_validate(
            dict(
                request_id=new_id(),
                action="register_plugin",
                module="other",
                types=[
                    dict(name="other_item", kind="semantic", supports_reevaluation=True),
                    dict(name="other_completion", kind="system_record"),
                ],
                read_types=["other_item", "other_completion", "sample_item", "rule"],
                subscribe_types=["sample_item"],
            )
        )
        _, registered = execute(setup, other_body)
        assert registered["status"] == "succeeded"
        other_issued = operator.issue_principal(
            "other", read_types=other_body.read_types, subscribe_types=other_body.subscribe_types
        )
        _, enabled = execute(setup, command("enable", module="other"))
        assert enabled["enabled"]
        with RemoteStorageClient(other_issued.token, url) as other:
            with pytest.raises(PermissionDenied):
                other.write([FinishReevaluation(result)], SYS.deeper())
            with pytest.raises(PermissionDenied):
                other.write(
                    [FinishReevaluation(result), MarkProcessed("other.managed", event)],
                    SYS.deeper(),
                )
            assert other.type_revision("other_completion") == 0
        assert reevaluation_result(client, event) == result


def test_dynamic_ordinary_exact_name_valid_payload_is_untrusted(setup):
    _, _, _, service, operator, _, _ = setup
    body = ManagementInput.model_validate(
        dict(
            request_id=new_id(),
            action="register_plugin",
            module="sample",
            types=[dict(name="sample_completion", kind="system_record")],
            read_types=["sample_completion"],
            subscribe_types=[],
        )
    )
    _, registered = execute(setup, body)
    assert registered["status"] == "succeeded"
    issued = operator.issue_principal("sample", read_types=body.read_types)
    _, enabled = execute(setup, command("enable"))
    assert enabled["enabled"]
    event = new_id()
    result = ReevaluationResult(
        request_event_id=event,
        target_type="sample_item",
        target_id=new_id(),
        outcome="unchanged",
        before_version=1,
        after_version=1,
        root_feedback_id="system",
        depth=1,
        completed_at=utcnow(),
    )
    with live_server(service) as url, RemoteStorageClient(issued.token, url) as client:
        assert not client.registry.get("sample_completion").reevaluation_result
        client.write(
            [PutSystemRecord("sample_completion", event, result.model_dump(mode="json"))], SYS
        ).check()
        assert client.get_system_record(event) is not None
        assert reevaluation_result(client, event) is None
        with client.snapshot() as snap:
            assert reevaluation_result(snap, event) is None
