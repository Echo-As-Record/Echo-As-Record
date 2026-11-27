"""Opt-in registration and least-privilege reads against isolated PostgreSQL."""

import pytest
from ear_contracts import DEFAULT_REGISTRY
from ear_contracts.optional_sources import CHAT_TYPES, chat_source_registry
from ear_storage import AuthOperator, StorageService, bootstrap, migrate
from ear_storage.health import HealthProbe
from ear_storage_client import AppendRaw, Context, PermissionDenied, Query
from ear_storage_client.testing import make_raw

from .test_relay import env as env

pytestmark = pytest.mark.postgres


def test_optional_registry_retains_default_health_and_requires_explicit_acl(pg_dsn):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    assert not set(CHAT_TYPES) & DEFAULT_REGISTRY.data_types.keys()
    assert HealthProbe(pg_dsn).ready()
    op, service = AuthOperator(pg_dsn), StorageService(pg_dsn)
    preexisting = op.issue_principal("extraction", read_types=("transcript_segment",))
    bootstrap(pg_dsn, chat_source_registry())
    bootstrap(pg_dsn, chat_source_registry())
    bootstrap(pg_dsn)
    assert HealthProbe(pg_dsn).ready()
    importer = op.issue_principal("adapter", read_types=CHAT_TYPES)
    channel = service.client(importer.token)
    record = make_raw(
        "chat_message",
        {"text": "fixture", "conversation_id": "c", "message_id": "m", "role": "user"},
        start="2026-11-22T01:00:00Z",
    )
    channel.write([AppendRaw(record)], Context.system()).check()
    existing = service.client(preexisting.token)
    with pytest.raises(PermissionDenied):
        existing.query(Query("chat_message"))
    authorized = service.client(op.issue_principal("extraction", read_types=CHAT_TYPES).token)
    assert [r.id for r in authorized.query(Query("chat_message")).items] == [record.id]
    with pytest.raises(PermissionDenied):
        authorized.write([AppendRaw(record)], Context.system()).check()


@pytest.mark.nats
def test_import_file_and_emit_private_metadata_only_over_real_http_and_jetstream(env, tmp_path):
    import asyncio
    import hashlib

    from ear_storage.relay import RelayStore, run_relay
    from ear_storage_client.remote import RemoteStorageClient
    from librechat_adapter import Ingestor, PrivateVolume
    from librechat_adapter.testing import attachment, blob, end, header, join, message

    from .test_http_postgres import live_server
    from .test_relay import reader

    dsn, _, broker = env
    bootstrap(dsn, chat_source_registry())
    principal = AuthOperator(dsn).issue_principal("adapter", read_types=CHAT_TYPES)
    data = b"synthetic raw attachment"
    stamp = "2026-11-22T14:00:00Z"
    lines = join(
        [
            header(),
            blob(1, data),
            attachment(
                "f", mid="m", cid="c", blob_id=1, filename="notes.txt", message_created_at=stamp
            ),
            message("m", "c", "test-private-body", created_at=stamp, updated_at=stamp),
            end(),
        ]
    )
    volume = PrivateVolume(tmp_path)
    with (
        live_server(StorageService(dsn)) as address,
        RemoteStorageClient(principal.token, address) as storage,
    ):
        assert Ingestor(storage, volume).ingest(lines).complete
        rows = storage.query(Query("chat_attachment")).items
        assert len(rows) == 1
        ref = rows[0].payload["file_ref"]
        assert ref["sha256"] == hashlib.sha256(data).hexdigest()
        assert (tmp_path / ref["path"]).read_bytes() == data
        assert not Ingestor(storage, volume).ingest(lines).written

    async def published():
        assert (await run_relay(RelayStore(dsn, broker), once=True)).delivered == 2
        async with reader(broker) as js:
            for seq in (1, 2):
                event = await js.get_msg(broker.stream, seq=seq)
                assert event.data is not None and b"test-private-body" not in event.data
                assert event.subject in {
                    broker.prefix + ".data_changed.chat_attachment.adapter",
                    broker.prefix + ".data_changed.chat_message.adapter",
                }

    asyncio.run(published())
