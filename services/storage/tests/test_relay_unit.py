from dataclasses import replace
from unittest.mock import MagicMock

import ear_storage.relay as relay_module
import pytest
from ear_contracts import new_id
from ear_storage.relay import Claim, RelayConfig, RelayStore, StreamMismatch, validate_stream
from nats.js.api import DiscardPolicy, PersistMode, StorageType, StreamConfig


@pytest.mark.parametrize(
    "change",
    [
        {"stream": "a.b"},
        {"stream": "*"},
        {"stream": ""},
        {"stream": "A" * 65},
        {"prefix": "ear.>"},
        {"prefix": "ear..events"},
        {"prefix": "ear.secret\n"},
        {"prefix": "é"},
        {"servers": ()},
        {"servers": ("nats://token@localhost:4222",)},
        {"servers": ("http://localhost:4222",)},
        {"servers": ("nats://localhost:4222/path",)},
        {"timeout": float("nan")},
        {"lease_seconds": 1},
        {"retry_max": 0},
        {"batch_size": 0},
        {"batch_size": 1001},
        {"max_bytes": 0},
        {"token": ""},
    ],
)
def test_config_rejects_unsafe_values(change):
    with pytest.raises(ValueError):
        RelayConfig(**({"servers": ("nats://localhost:4222",)} | change))


def test_config_redacts_secrets_and_has_safe_defaults():
    config = RelayConfig(servers=("nats://privatehost:4222",), token="secret")
    assert "secret" not in repr(config) and "privatehost" not in repr(config)
    assert len(config.destination) == 64
    validate_stream(config.stream_config(), config)


@pytest.mark.parametrize(
    "change",
    [
        {"storage": StorageType.MEMORY},
        {"discard": DiscardPolicy.OLD},
        {"max_age": 60},
        {"no_ack": True},
        {"subjects": ["other.>"]},
        {"allow_rollup_hdrs": True},
        {"deny_purge": False},
        {"sealed": True},
        {"max_msgs_per_subject": 1},
        {"duplicate_window": 10},
    ],
)
def test_existing_stream_mismatch_fails_closed(change):
    config = RelayConfig(servers=("nats://localhost:4222",))
    with pytest.raises(StreamMismatch):
        validate_stream(replace(config.stream_config(), **change), config)


@pytest.mark.parametrize("mode", [None, PersistMode.DEFAULT, "default"])
def test_existing_stream_default_persistence_accepted(mode):
    config = RelayConfig(servers=("nats://localhost:4222",))
    validate_stream(replace(config.stream_config(), persist_mode=mode), config)
    response = config.stream_config().as_dict() | {"persist_mode": mode}
    validate_stream(StreamConfig.from_response(response), config)


@pytest.mark.parametrize("mode", [PersistMode.ASYNC, "async", "future_mode", "", False, 0, {}])
def test_existing_stream_unsafe_persistence_rejected(mode):
    # 2.12+ configuration guard only; the real fixture remains NATS 2.11.
    config = RelayConfig(servers=("nats://localhost:4222",))
    with pytest.raises(StreamMismatch):
        validate_stream(replace(config.stream_config(), persist_mode=mode), config)


def test_creation_pins_omitted_default_even_if_sdk_default_changes(monkeypatch):
    def future_stream_config(**kwargs):
        kwargs.setdefault("persist_mode", PersistMode.ASYNC)
        return StreamConfig(**kwargs)

    monkeypatch.setattr(relay_module, "StreamConfig", future_stream_config)
    config = RelayConfig(servers=("nats://localhost:4222",)).stream_config()
    assert config.persist_mode is None
    assert "persist_mode" not in config.as_dict()


@pytest.mark.parametrize("length", [128, 129, 256])
@pytest.mark.parametrize("event_type", ["data_changed", "reevaluation_requested"])
def test_subject_preserves_contract_type_without_relay_length_limit(
    monkeypatch, length, event_type
):
    import json

    dtype = "synthetic_" + "x" * (length - len("synthetic_"))
    event_id, data_id = new_id(), new_id()
    event = {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": "2026-11-01T00:00:00Z",
        "root_feedback_id": "system",
        "depth": 1,
    }
    if event_type == "data_changed":
        event.update(
            data_type=dtype,
            data_id=data_id,
            operation="created",
            new_version=1,
            old_version=None,
            skip_history=False,
        )
    else:
        event.update(
            target_type=dtype,
            target_id=data_id,
            change_description="synthetic",
            cause={"data_type": dtype, "data_id": data_id, "old_version": None, "new_version": 1},
        )
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ('{"writer":"extraction"}',)
    connect = MagicMock()
    connect.return_value.__enter__.return_value = conn
    monkeypatch.setattr(relay_module, "connection", connect)
    monkeypatch.setattr(relay_module, "check_schema", lambda conn: None)
    config = RelayConfig(servers=("nats://localhost:4222",))
    store = RelayStore("synthetic-unit-dsn", config)
    claim = Claim(1, 0, event_id, json.dumps(event), 1, 1)
    assert store.subject(claim) == f"{config.prefix}.{event_type}.{dtype}.extraction"
