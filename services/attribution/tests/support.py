"""Only synthetic inputs and the real in-memory SDK; registry changes stay local."""

from dataclasses import replace

from ear_attribution import AttributionService, MatchConfig
from ear_contracts import (
    DEFAULT_REGISTRY,
    Cause,
    DataChanged,
    DataTypeSpec,
    Registry,
    SemanticRecord,
    new_id,
)
from ear_storage_client import AppendRaw, Context, MemoryStorage, PublishReevaluation, Query
from ear_storage_client.testing import make_raw

CONFIG = MatchConfig(cosine_threshold=0.9, gps_threshold_m=100)


def registry():
    specs = dict(DEFAULT_REGISTRY.data_types)
    specs["attribution"] = replace(
        specs["attribution"], requires_dependencies=True, supports_reevaluation=True
    )
    return Registry(
        specs, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types
    ).extend_core(
        DataTypeSpec("attribution_state", "system_record", "attribution"),
        DataTypeSpec("attribution_feedback_result", "system_record", "attribution"),
    )


def setup():
    storage = MemoryStorage(registry=registry())
    client = storage.client("attribution")
    service = AttributionService.business(client, config=CONFIG)
    return storage, client, service


def voice(values=(1.0, 0.0), model="synthetic/v1"):
    return {
        "embedding": {
            "values": list(values),
            "shape": [len(values)],
            "dtype": "float32",
            "provenance": None,
        },
        "model_version": model,
    }


def append(storage, kind="voiceprint", payload=None):
    if payload is None:
        payload = (
            voice()
            if kind == "voiceprint"
            else {"latitude": 0.0, "longitude": 0.0, "accuracy_m": None}
        )
    raw = make_raw(kind, payload, start="2026-11-05T00:00:00Z")
    client = storage.client("api" if kind == "feedback" else "adapter")
    event = client.write([AppendRaw(raw)], Context.system()).check().events[0]
    assert isinstance(event, DataChanged)
    return event


def attr(client, event):
    record = client.query(Query("attribution", where={"subject_id": event.data_id})).items[0]
    assert isinstance(record, SemanticRecord)
    return record


def request(storage, attribution, cause_id=None):
    cause_id = cause_id or attribution.payload["target_id"]
    cause = storage.client("dependency").get(cause_id)
    return (
        storage.client("dependency")
        .write(
            [
                PublishReevaluation(
                    "attribution",
                    attribution.id,
                    Cause(
                        data_type=cause.data_type,
                        data_id=cause.id,
                        old_version=None,
                        new_version=cause.version,
                    ),
                    "synthetic reference change",
                    event_id=new_id(),
                )
            ],
            Context.system().deeper(),
        )
        .check()
        .events[0]
    )


def feedback(storage, service, command):
    # Same normalized null/default shape as ⑥'s stored PendingCommand.
    normalized = {
        "target_id": None,
        "subject_id": None,
        "previous_target_id": None,
        "new_target_id": None,
        "name": None,
        "gps_ids": [],
        "interval": None,
        "expected_version": None,
        "rejudge_history": False,
        **command,
    }
    event = append(
        storage,
        "feedback",
        {"module": "attribution", "command": normalized, "integration": "pending_contract"},
    )
    service.handle(event)
    return event


def present[T](value: T | None) -> T:
    assert value is not None
    return value
