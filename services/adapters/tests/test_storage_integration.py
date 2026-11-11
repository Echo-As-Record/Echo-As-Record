"""双门控：真实回环 HTTP + 临时 PostgreSQL/NATS，仅合成数据。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import jsonschema
import pytest
from ear_adapter import AlreadyIngested, Ingested
from ear_adapter.runtime import READ_TYPES
from ear_contracts import DataChanged, new_id
from ear_storage import bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.service import StorageService
from ear_storage_client import MemoryStorage, PermissionDenied, Query
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown

# 直接复用已有夹具和 HTTP 生命周期；不复制容器管理、不改①代码。
from services.storage.tests.conftest import nats_server as nats_server
from services.storage.tests.conftest import pg_dsn as pg_dsn
from services.storage.tests.conftest import postgres_server as postgres_server
from services.storage.tests.test_http_postgres import live_server

from .test_remote import runner

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.nats,
    pytest.mark.skipif(
        os.environ.get("EAR_RUN_POSTGRES_TESTS") != "1"
        or os.environ.get("EAR_RUN_NATS_TESTS") != "1",
        reason="requires EAR_RUN_POSTGRES_TESTS=1 and EAR_RUN_NATS_TESTS=1 (isolated Docker)",
    ),
]


@pytest.fixture
def stack(pg_dsn, nats_server):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    operator = AuthOperator(pg_dsn)
    suffix = new_id().replace("-", "")
    relay = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="EAR_ADAPTER_" + suffix,
        prefix="adapter_" + suffix,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    service = StorageService(pg_dsn, relay_config=relay)
    with live_server(service) as url:
        yield operator, url, RelayStore(pg_dsn, relay)
    # 流随 session 隔离 NATS 容器销毁，不触碰外部服务。


@pytest.mark.parametrize("lose_response", [False, True])
def test_import_events_restart_and_replay(stack, monkeypatch, lose_response):
    operator, url, relay = stack
    adapter = operator.issue_principal("adapter", read_types=list(READ_TYPES))
    reads = ["voiceprint", "local_speaker", "audio_ref"]
    attribution = operator.issue_principal("attribution", read_types=reads, subscribe_types=reads)
    with (
        RemoteStorageClient(adapter.token, url) as remote,
        RemoteStorageClient(attribution.token, url) as consumer,
    ):
        seen = []

        def handler(event):
            if isinstance(event, DataChanged) and event.data_type == "voiceprint":
                voice = consumer.get(event.data_id)
                assert voice is not None
                assert consumer.get(voice.payload["local_speaker_id"]) is not None
                assert consumer.get(voice.payload["audio_ref_id"]) is not None
                seen.append(event.event_id)

        consumer.subscribe("attribution.adapter_test", handler)
        write = remote.write
        calls = []

        def fault(ops, context, *, request_id=None):
            calls.append((ops, context, request_id))
            result = write(ops, context, request_id=request_id)
            if len(calls) == 1:
                assert request_id is not None
                raise CommitUnknown(request_id)
            assert ops is calls[0][0] and context is calls[0][1] and request_id == calls[0][2]
            return result

        if lose_response:
            monkeypatch.setattr(remote, "write", fault)
        outcome = runner(remote).run_once().outcomes[0]
        expected = runner(MemoryStorage().client("adapter")).run_once().outcomes[0]
        assert isinstance(outcome, Ingested) and isinstance(expected, Ingested)
        assert outcome.written == expected.written
        schema = json.loads(
            (
                Path(__file__).resolve().parents[3] / "contracts/records/raw-record.schema.json"
            ).read_text()
        )
        for name in READ_TYPES:
            assert remote.count(Query(name)) == expected.written[name]
            for item in remote.query(Query(name)).items:
                jsonschema.validate(item.model_dump(mode="json"), schema)
        assert asyncio.run(run_relay(relay, once=True)).delivered == 13
        consumer.run_until_idle()
        assert len(set(seen)) == 2
        assert isinstance(runner(remote).run_once().outcomes[0], AlreadyIngested)
        assert asyncio.run(run_relay(relay, once=True)).delivered == 0
        assert consumer.poll_once() == 0
        for name in READ_TYPES:
            assert remote.count(Query(name)) == expected.written[name]
        if lose_response:
            assert len(calls) == 2


def test_adapter_without_read_grants(stack):
    operator, url, _ = stack
    identity = operator.issue_principal("adapter", read_types=[])
    with (
        RemoteStorageClient(identity.token, url) as remote,
        pytest.raises(PermissionDenied, match="read_types"),
    ):
        runner(remote).run_once()
