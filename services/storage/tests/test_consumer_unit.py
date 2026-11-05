from dataclasses import replace

import pytest
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, new_id
from ear_storage.auth import Principal
from ear_storage.consumers import ConsumerPolicy, approved_filters, authorize_event
from ear_storage_client import PermissionDenied
from ear_storage_client.consumer_wire import RegisterConsumer
from ear_storage_client.wire import EVENT_ADAPTER
from pydantic import ValidationError


@pytest.mark.parametrize(
    "name",
    [
        "",
        "extraction.",
        "extraction.*",
        "extraction.>",
        "extraction.a/b",
        "extraction.a\n",
        "Ex",
        "a" * 181,
    ],
)
def test_strict_consumer_names(name):
    with pytest.raises(ValidationError):
        RegisterConsumer(consumer=name)


@pytest.mark.parametrize(
    "extra",
    [
        {"codec": True},
        {"module": "api"},
        {"scope": "public"},
        {"filter_subjects": [">"]},
        {"max_attempts": 999},
        {"from_beginning": 1},
        {"event_types": []},
    ],
)
def test_closed_consumer_commands(extra):
    with pytest.raises(ValidationError):
        RegisterConsumer.model_validate({"consumer": "extraction.worker", **extra})


def test_filter_default_deny_and_explicit_subset():
    principal = Principal(
        new_id(), "extraction", frozenset(["activity", "person"]), frozenset(["activity"])
    )
    assert approved_filters(
        principal, DEFAULT_REGISTRY, ["data_changed", "reevaluation_requested"], "ear.events"
    ) == ["ear.events.data_changed.activity.*"]
    with pytest.raises(PermissionDenied):
        approved_filters(
            replace(principal, read_types=frozenset()),
            DEFAULT_REGISTRY,
            ["data_changed"],
            "ear.events",
        )


def test_reevaluation_owner_and_capability():
    registry = DEFAULT_REGISTRY.extend(
        DataTypeSpec("synthetic_result", "semantic", "extraction", supports_reevaluation=True)
    )
    p = Principal(
        new_id(), "extraction", frozenset(["synthetic_result"]), frozenset(["synthetic_result"])
    )
    filters = approved_filters(p, registry, ["reevaluation_requested"], "ear.events")
    assert filters == ["ear.events.reevaluation_requested.synthetic_result.extraction"]
    with pytest.raises(PermissionDenied):
        approved_filters(
            replace(p, module="api"), registry, ["reevaluation_requested"], "ear.events"
        )
    event = EVENT_ADAPTER.validate_python(
        {
            "event_type": "reevaluation_requested",
            "event_id": new_id(),
            "occurred_at": "2026-11-05T00:00:00Z",
            "target_type": "synthetic_result",
            "target_id": new_id(),
            "cause": {
                "data_type": "person",
                "data_id": new_id(),
                "old_version": None,
                "new_version": 1,
            },
            "change_description": "private",
            "root_feedback_id": "system",
            "depth": 1,
        }
    )
    with pytest.raises(PermissionDenied):
        authorize_event(
            p,
            registry,
            {"filters": filters, "event_types": ["reevaluation_requested"]},
            event,
            "ear.events",
        )


@pytest.mark.parametrize(
    "values",
    [
        {"delivery_lease": 0},
        {"ack_wait": 1},
        {"retry_base": float("nan")},
        {"max_attempts": True},
        {"poll_timeout": 30},
    ],
)
def test_policy_limits(values):
    with pytest.raises(ValueError):
        ConsumerPolicy(**values)


@pytest.mark.parametrize("status,code", [(403, "forbidden"), (409, "unknown")])
def test_sdk_does_not_discard_unknown_or_denied_completion(status, code):
    import httpx
    from ear_storage_client.remote import RemoteStorageClient
    from ear_storage_client.remote_errors import ServiceUnavailable

    def response(request):
        return httpx.Response(status, json={"code": code}, request=request)

    with RemoteStorageClient(
        "t" * 43, "http://test", transport=httpx.MockTransport(response)
    ) as sdk:
        sdk._handlers["extraction.worker"] = lambda event: None
        sdk._completions["extraction.worker"] = ("ack", "a" * 43)
        sdk._completions["extraction.other"] = ("nack", "b" * 43)
        before = sdk._completions.copy()
        with pytest.raises(PermissionDenied if status == 403 else ServiceUnavailable):
            sdk.poll_once("extraction.worker")
        assert sdk._completions == before


def test_sdk_expiration_discards_only_matching_completion():
    import httpx
    from ear_storage_client.remote import RemoteStorageClient

    def response(request):
        return httpx.Response(409, json={"code": "delivery_expired"}, request=request)

    with RemoteStorageClient(
        "t" * 43, "http://test", transport=httpx.MockTransport(response)
    ) as sdk:
        sdk._handlers["extraction.worker"] = lambda event: None
        sdk._completions["extraction.worker"] = ("ack", "a" * 43)
        sdk._completions["extraction.other"] = ("nack", "b" * 43)
        assert sdk.poll_once("extraction.worker") == 0
        assert sdk._completions == {"extraction.other": ("nack", "b" * 43)}


def test_reconfiguration_static_generation_fences_and_per_type_watermarks():
    import inspect

    from ear_storage import reconfiguration
    from ear_storage.consumers import ConsumerService

    prepare = inspect.getsource(reconfiguration.prepare)
    activate = inspect.getsource(reconfiguration.activate)
    fetch = inspect.getsource(ConsumerService.fetch)
    flush = inspect.getsource(ConsumerService._flush)
    assert "consumer_generations" in prepare and "'prepared'" in prepare
    assert "old_generation" in prepare and "restore_ready" in prepare
    assert "fence=fence+1" in prepare and "fence=fence+1" in activate
    assert "row[8] != fence" in fetch and "row[2] != durable" in fetch
    assert 'consumer_snapshot(conn, body.module) != plan["consumers"]' in activate
    assert 'identities(conn, body.module) != plan["identities"]' in activate
    assert "o.commit_seq>t.effective_commit_seq" in prepare
    assert "old_types & new_types" in prepare
    assert "consumer_type_cursors" in activate and "effective_commit_seq" in activate
    assert "stored[1] <= self._cursor" in fetch
    assert "stored[1] > self._cursor" in flush
    assert "authorize_event(principal, registry, cfg, event, self.relay.prefix)" in flush


def test_schema8_static_additive_migration_preserves_history():
    from ear_storage.schema import _REQUIRED_V8_COLUMNS, DDL_V8, SCHEMA_VERSION

    assert SCHEMA_VERSION == 8
    ddl = " ".join(DDL_V8)
    assert "DROP " not in ddl.upper() and "DELETE " not in ddl.upper()
    assert "historical_filtered" not in ddl
    assert "SELECT name,generation,config_text,destination,'active',NULL" in ddl
    assert "c.creation_cursor" in ddl and "broker_generation=c.generation" in ddl
    assert _REQUIRED_V8_COLUMNS["consumer_deliveries"] == "broker_generation"
    assert "effective_commit_seq" in _REQUIRED_V8_COLUMNS["consumer_type_cursors"]
