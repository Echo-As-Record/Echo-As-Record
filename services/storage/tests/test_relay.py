from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import replace

import nats
import pytest
from ear_contracts import DEFAULT_REGISTRY, Cause, DataTypeSpec, new_id
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.relay import JetStreamPublisher, Relay, RelayConfig, RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage_client import (
    Context,
    Create,
    PublishReevaluation,
    PutSystemRecord,
    Supersede,
)
from nats.js.api import DiscardPolicy
from nats.js.errors import NotFoundError

from .conftest import docker

pytestmark = [pytest.mark.nats, pytest.mark.postgres]
SYS = Context.system()


@pytest.fixture
def env(nats_server, pg_dsn):
    migrate(pg_dsn)
    bootstrap(pg_dsn)
    suffix = uuid.uuid4().hex
    config = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="EAR_IT_" + suffix,
        prefix="ear_it_" + suffix,
        timeout=2,
        lease_seconds=5,
        retry_base=0.02,
        retry_max=0.1,
        max_bytes=1024 * 1024,
        max_msgs=1000,
    )
    try:
        yield pg_dsn, PostgresStorage(pg_dsn), config
    finally:
        # Each stream reserves max_bytes against the session server's bounded store.
        # Remove only this fixture's unique stream, including its durable consumers.
        async def cleanup():
            async with reader(config) as js:
                with suppress(NotFoundError):
                    await js.delete_stream(config.stream)

        asyncio.run(cleanup())


@asynccontextmanager
async def reader(config):
    nc = await nats.connect(servers=list(config.servers), token=config.token)
    try:
        yield nc.jetstream()
    finally:
        await nc.close()


def add(store, count=1):
    return (
        store.client("extraction")
        .write(
            [Create("activity", new_id(), {"body": "private payload"}) for _ in range(count)], SYS
        )
        .check()
        .events
    )


def expire(dsn):
    with connection(dsn) as conn:
        conn.execute("UPDATE storage.relay_state SET lease_until='-infinity'")
        conn.execute("UPDATE storage.outbox SET next_attempt_at='-infinity'")


def test_end_to_end_exact_event_order_restart_batch_cursor(env):
    dsn, store, config = env
    events = (*add(store, 3), *add(store, 2))

    async def scenario():
        cfg = replace(config, batch_size=2)
        first = await run_relay(RelayStore(dsn, cfg), once=True)
        assert first.delivered == 2 and first.status == "batch_limit"
        assert (await run_relay(RelayStore(dsn, config), once=True)).delivered == 3
        assert (await run_relay(RelayStore(dsn, config), once=True)).delivered == 0
        async with reader(config) as js:
            info = await js.stream_info(config.stream)
            assert info.state.messages == 5
            for index, event in enumerate(events, 1):
                message = await js.get_msg(config.stream, seq=index)
                assert message.data is not None
                assert json.loads(message.data) == event.model_dump(mode="json")
                assert message.headers and message.headers["Nats-Msg-Id"] == event.event_id
                assert message.subject == config.prefix + ".data_changed.activity.extraction"
                assert "private" not in (message.subject or "")

    asyncio.run(scenario())
    health = RelayStore(dsn, config).health()
    assert health["pending"] == 0 and health["delivered"] == 5
    with connection(dsn) as conn:
        rows = conn.execute(
            "SELECT attempts,ack_seq FROM storage.outbox ORDER BY commit_seq,ordinal"
        ).fetchall()
    assert rows == [(1, n) for n in range(1, 6)]


def test_rollback_and_system_records_never_publish(env, monkeypatch):
    import ear_storage.backend as backend
    from ear_storage.repository import persist

    original = persist
    dsn, store, config = env

    def fail(*args):
        original(*args)
        raise RuntimeError("injected SQL rollback")

    with monkeypatch.context() as patch:
        patch.setattr(backend, "persist", fail)
        with pytest.raises(RuntimeError):
            add(store)
    store.client("extraction").write([PutSystemRecord("extraction_run", new_id(), {})], SYS).check()
    assert store.outbox() == ()
    assert asyncio.run(run_relay(RelayStore(dsn, config), once=True)).delivered == 0
    assert RelayStore(dsn, config).health()["pending"] == 0


def test_supersede_and_reevaluation_routing(env):
    dsn, store, config = env
    event = add(store)[0]
    store.client("extraction").write([Supersede("activity", event.data_id, 1)], SYS).check()
    store.client("extraction").write([Supersede("activity", event.data_id, 2)], SYS).check()
    target = add(store)[0]
    store.client("dependency").write(
        [
            PublishReevaluation(
                "activity",
                target.data_id,
                Cause(data_type="activity", data_id=event.data_id, old_version=1, new_version=2),
                "private description must not route",
            )
        ],
        Context(root_feedback_id="system", depth=1),
    ).check()

    async def scenario():
        assert (await run_relay(RelayStore(dsn, config), once=True)).delivered == 4
        async with reader(config) as js:
            msg = await js.get_msg(config.stream, seq=2)
            data = json.loads(msg.data or b"{}")
            assert (data["operation"], data["old_version"], data["new_version"]) == (
                "superseded",
                1,
                2,
            )
            msg = await js.get_msg(config.stream, seq=4)
            assert msg.subject == config.prefix + ".reevaluation_requested.activity.extraction"

    asyncio.run(scenario())


@pytest.mark.parametrize("outside_window", [False, True])
def test_real_puback_then_db_mark_failure_republishes(env, monkeypatch, outside_window):
    dsn, store, config = env
    config = replace(config, duplicate_window=0.1 if outside_window else 120)
    events = add(store, 2)
    relay_store = RelayStore(dsn, config)

    async def scenario():
        with monkeypatch.context() as patch:

            def crash(*args, **kwargs):
                raise RuntimeError("crash after real PubAck before DB mark")

            patch.setattr(relay_store, "finish", crash)
            with pytest.raises(RuntimeError):
                await run_relay(relay_store, once=True)
        assert relay_store.health()["delivered"] == 0
        async with reader(config) as js:
            assert (await js.stream_info(config.stream)).state.messages == 1
        if outside_window:
            await asyncio.sleep(0.2)
        expire(dsn)
        assert (await run_relay(RelayStore(dsn, config), once=True)).delivered == 2
        async with reader(config) as js:
            expected = 3 if outside_window else 2
            assert (await js.stream_info(config.stream)).state.messages == expected
            message = await js.get_msg(config.stream, seq=expected)
            assert json.loads(message.data or b"{}")["event_id"] == events[1].event_id

    asyncio.run(scenario())
    with connection(dsn) as conn:
        assert conn.execute("SELECT attempts FROM storage.outbox ORDER BY ordinal").fetchall() == [
            (2,),
            (1,),
        ]


def test_lost_ack_real_publish_retry(env):
    dsn, store, config = env
    add(store)

    async def scenario():
        publisher = JetStreamPublisher(config)

        class LostAck:
            async def publish(self, subject, payload, event_id):
                await publisher.publish(subject, payload, event_id)
                raise TimeoutError("sensitive broker diagnostics")

        try:
            result = await Relay(RelayStore(dsn, config), LostAck()).run_once()
            assert result.status == "publish_failed"
        finally:
            await publisher.close()
        expire(dsn)
        assert (await run_relay(RelayStore(dsn, config), once=True)).delivered == 1
        async with reader(config) as js:
            assert (await js.stream_info(config.stream)).state.messages == 1

    asyncio.run(scenario())


def test_broker_down_retry_row_and_recovery(env, nats_server):
    dsn, store, config = env
    add(store, 2)
    config = replace(config, timeout=0.4)
    docker("pause", nats_server.name)
    try:
        assert asyncio.run(run_relay(RelayStore(dsn, config), once=True)).status == "publish_failed"
    finally:
        docker("unpause", nats_server.name)
    health = RelayStore(dsn, config).health()
    assert health["pending"] == 2 and health["delivered"] == 0
    assert health["head"]["attempts"] == 1 and health["head"]["last_error"] == "publish_failed"
    assert not health["ready"] and "private" not in json.dumps(health)
    expire(dsn)
    assert asyncio.run(run_relay(RelayStore(dsn, config), once=True)).delivered == 2


def test_two_workers_fence_late_publish_cannot_advance(env):
    dsn, store, config = env
    events = add(store, 2)
    a, b = RelayStore(dsn, config), RelayStore(dsn, config)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        publisher = JetStreamPublisher(config)

        class Slow:
            async def publish(self, subject, payload, event_id):
                entered.set()
                await release.wait()
                return await publisher.publish(subject, payload, event_id)

        task = asyncio.create_task(Relay(a, Slow()).run_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert b.claim() is None
            expire(dsn)
            assert (await run_relay(b, once=True)).delivered == 2
            release.set()
            assert (await task).status == "fenced"
            async with reader(config) as js:
                assert (await js.stream_info(config.stream)).state.messages == 2
                msg = await js.get_msg(config.stream, seq=1)
                assert json.loads(msg.data or b"{}")["event_id"] == events[0].event_id
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await publisher.close()

    asyncio.run(scenario())


def test_publish_blocked_other_db_writer_succeeds_and_pause_fences(env):
    dsn, store, config = env
    add(store)
    relay_store = RelayStore(dsn, config)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        publisher = JetStreamPublisher(config)

        class Blocked:
            async def publish(self, subject, payload, event_id):
                entered.set()
                await release.wait()
                return await publisher.publish(subject, payload, event_id)

        task = asyncio.create_task(Relay(relay_store, Blocked()).run_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(asyncio.to_thread(add, store), 1)
            relay_store.pause(True)
            assert relay_store.claim() is None
            release.set()
            assert (await task).status == "fenced"
            assert relay_store.health()["pending"] == 2
            relay_store.pause(False)
            assert (await run_relay(relay_store, once=True)).delivered == 2
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await publisher.close()

    asyncio.run(scenario())


def test_existing_bad_stream_not_mutated(env):
    dsn, store, config = env
    add(store)

    async def scenario():
        async with reader(config) as js:
            bad = replace(config.stream_config(), discard=DiscardPolicy.OLD)
            before = await js.add_stream(config=bad)
            result = await run_relay(RelayStore(dsn, config), once=True)
            assert result.status == "stream_mismatch"
            after = await js.stream_info(config.stream)
            assert before.config == after.config and after.state.messages == 0

    asyncio.run(scenario())
    assert RelayStore(dsn, config).health()["head"]["last_error"] == "stream_mismatch"


def test_full_stream_rejects_new_without_evicting_accepted(env):
    dsn, store, config = env
    config = replace(config, max_msgs=1)
    events = add(store, 2)

    async def scenario():
        result = await run_relay(RelayStore(dsn, config), once=True)
        assert result.delivered == 1 and result.status == "publish_failed"
        async with reader(config) as js:
            assert (await js.stream_info(config.stream)).state.messages == 1
            msg = await js.get_msg(config.stream, seq=1)
            assert json.loads(msg.data or b"{}")["event_id"] == events[0].event_id

    asyncio.run(scenario())
    assert RelayStore(dsn, config).health()["pending"] == 1


def test_invalid_head_observable_no_skip_and_no_secret(env):
    dsn, store, config = env
    add(store, 2)
    with connection(dsn) as conn:
        conn.execute(
            "UPDATE storage.outbox SET event_text=%s WHERE ordinal=0", ('{"private":"secret"}',)
        )
    relay_store = RelayStore(dsn, config)
    assert asyncio.run(run_relay(relay_store, once=True)).status == "invalid_event"
    health = relay_store.health()
    assert health["pending"] == 2 and health["delivered"] == 0
    assert health["head"]["last_error"] == "invalid_event"
    assert "secret" not in json.dumps(health)
    with connection(dsn) as conn:
        conn.execute(
            "UPDATE storage.outbox SET next_attempt_at=clock_timestamp()+interval '1 hour' "
            "WHERE ordinal=0"
        )
    assert relay_store.claim() is None
    with connection(dsn) as conn:
        assert conn.execute("SELECT attempts FROM storage.outbox ORDER BY ordinal").fetchall() == [
            (1,),
            (0,),
        ]


def test_expired_finish_rejected_even_before_takeover(env):
    dsn, store, config = env
    add(store)
    relay_store = RelayStore(dsn, config)
    claim = relay_store.claim()
    assert claim is not None
    expire(dsn)
    assert not relay_store.finish(claim, error="publish_failed")
    next_claim = relay_store.claim()
    assert next_claim and next_claim.token > claim.token and next_claim.event_id == claim.event_id


def test_destination_change_fails_closed(env):
    from ear_storage.relay import StreamMismatch

    dsn, store, config = env
    add(store)
    assert RelayStore(dsn, config).claim()
    with pytest.raises(StreamMismatch):
        RelayStore(dsn, replace(config, stream="OTHER")).claim()


def test_real_process_exit_after_puback_then_restart(env):
    import subprocess
    import sys

    dsn, store, config = env
    event = add(store)[0]
    # Secrets are stdin only; even child diagnostics are never included in assertion output.
    code = """
import asyncio, json, os, sys
from ear_storage.relay import RelayConfig, RelayStore, Relay, JetStreamPublisher
args = json.load(sys.stdin)
config = RelayConfig(**args["config"])
class Crash(RelayStore):
    def finish(self, *args, **kwargs):
        os._exit(73)
async def main():
    await Relay(Crash(args["dsn"], config), JetStreamPublisher(config)).run_once()
asyncio.run(main())
"""
    from dataclasses import asdict

    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps({"dsn": str(dsn), "config": asdict(config)}),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 73, "isolated relay child did not reach crash window"
    assert RelayStore(dsn, config).health()["pending"] == 1

    async def scenario():
        async with reader(config) as js:
            msg = await js.get_msg(config.stream, seq=1)
            assert json.loads(msg.data or b"{}")["event_id"] == event.event_id
        expire(dsn)
        assert (await run_relay(RelayStore(dsn, config), once=True)).delivered == 1
        async with reader(config) as js:
            assert (await js.stream_info(config.stream)).state.messages == 1

    asyncio.run(scenario())


def test_infinite_bounded_retries_and_bad_ack_cannot_deliver(env):
    from nats.js.api import PubAck

    dsn, store, config = env
    add(store, 2)
    relay_store = RelayStore(dsn, config)

    class BadAck:
        async def publish(self, subject, payload, event_id):
            return PubAck(stream="wrong", seq=1)

    async def scenario():
        for _ in range(5):
            expire(dsn)
            assert (await Relay(relay_store, BadAck()).run_once()).status == "publish_failed"
        with connection(dsn) as conn:
            row = conn.execute(
                "SELECT attempts,delivered_at,"
                "extract(epoch FROM next_attempt_at-clock_timestamp()) "
                "FROM storage.outbox WHERE ordinal=0"
            ).fetchone()
            assert row and row[0] == 5 and row[1] is None and row[2] <= config.retry_max
            assert conn.execute(
                "SELECT attempts FROM storage.outbox WHERE ordinal=1"
            ).fetchone() == (0,)
        expire(dsn)
        assert (await run_relay(relay_store, once=True)).delivered == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("length", [129, 256])
def test_long_registered_type_commits_and_publishes_without_truncation(env, length):
    dsn, _, config = env
    dtype = "synthetic_" + "x" * (length - len("synthetic_"))
    registry = DEFAULT_REGISTRY.extend(DataTypeSpec(dtype, "semantic", "extraction"))
    bootstrap(dsn, registry)
    store = PostgresStorage(dsn)
    record_id = new_id()
    event = (
        store.client("extraction")
        .write([Create(dtype, record_id, {"synthetic": True})], SYS)
        .check()
        .events[0]
    )
    reeval = (
        store.client("dependency")
        .write(
            [
                PublishReevaluation(
                    dtype,
                    record_id,
                    Cause(data_type=dtype, data_id=record_id, old_version=None, new_version=1),
                    "synthetic reevaluation",
                )
            ],
            Context(root_feedback_id="system", depth=1),
        )
        .check()
        .events[0]
    )
    tail = add(store)[0]
    with connection(dsn) as conn:
        before = conn.execute(
            "SELECT event_text FROM storage.outbox ORDER BY commit_seq,ordinal"
        ).fetchall()
    assert len(before) == 3  # All writes committed before relay starts.

    async def scenario():
        result = await run_relay(RelayStore(dsn, config), once=True)
        assert result.delivered == 3 and result.status == "idle"
        async with reader(config) as js:
            assert (await js.stream_info(config.stream)).state.messages == 3
            for seq, (expected, kind, name) in enumerate(
                [
                    (event, "data_changed", dtype),
                    (reeval, "reevaluation_requested", dtype),
                    (tail, "data_changed", "activity"),
                ],
                1,
            ):
                msg = await js.get_msg(config.stream, seq=seq)
                assert msg.subject == f"{config.prefix}.{kind}.{name}.extraction"
                assert msg.data == before[seq - 1][0].encode()
                assert msg.headers and msg.headers["Nats-Msg-Id"] == expected.event_id

    asyncio.run(scenario())
    health = RelayStore(dsn, config).health()
    assert health["pending"] == 0 and health["delivered"] == 3
    with connection(dsn) as conn:
        assert conn.execute(
            "SELECT attempts,ack_seq,last_error FROM storage.outbox ORDER BY commit_seq,ordinal"
        ).fetchall() == [(1, n, None) for n in range(1, 4)]


@pytest.mark.parametrize("iteration", range(3))
def test_stream_fixture_reclaims_session_broker_reservation(env, iteration):
    _, _, config = env

    async def scenario():
        async with reader(config) as js:
            assert (await js.account_info()).streams == 0
            await js.add_stream(config=config.stream_config())
            assert (await js.account_info()).streams == 1

    asyncio.run(scenario())
