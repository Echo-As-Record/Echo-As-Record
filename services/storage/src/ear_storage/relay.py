"""Deployment-internal ordered outbox relay; no plugin API or consumer credentials.

A DB lease fences progress, not the broker: a slow expired publisher can duplicate
its event after takeover. It cannot acknowledge or advance to another event.
PubAck then DB commit is deliberately at-least-once, even beyond dedup windows.
Default persistence waits for the file write, not necessarily an fsync: the
server's sync_interval still governs power-loss durability. StreamInfo does not
expose that server setting, so this relay neither verifies nor changes it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from ear_contracts import DataChanged
from nats.aio.client import Client
from nats.js.api import DiscardPolicy, PubAck, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

from ear_storage.codec import EVENT_ADAPTER
from ear_storage.schema import check_schema, connection, one


class StreamMismatch(RuntimeError):
    def __init__(self) -> None:
        super().__init__("relay stream configuration mismatch")


@dataclass(frozen=True)
class RelayConfig:
    servers: tuple[str, ...] = field(repr=False)
    token: str | None = field(default=None, repr=False)
    stream: str = "EAR_EVENTS"
    prefix: str = "ear.events"
    lease_seconds: float = 30
    timeout: float = 5
    retry_base: float = 1
    retry_max: float = 60
    batch_size: int = 100
    max_bytes: int = 256 * 1024 * 1024
    max_msgs: int = 1_000_000
    duplicate_window: float = 120

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", self.stream):
            raise ValueError("invalid relay stream name")
        if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,7}", self.prefix):
            raise ValueError("invalid relay subject prefix")
        if len(self.prefix) > 128 or not self.servers:
            raise ValueError("invalid relay configuration")
        for server in self.servers:
            try:
                url = urlsplit(server)
                valid = (
                    url.scheme in {"nats", "tls"}
                    and url.hostname
                    and url.port
                    and not (url.username or url.password or url.path or url.query or url.fragment)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("invalid relay server; credentials must be separate")
        if self.token is not None and not self.token:
            raise ValueError("empty relay token")
        values = (
            self.timeout,
            self.lease_seconds,
            self.retry_base,
            self.retry_max,
            self.duplicate_window,
        )
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("invalid relay timing")
        if not (
            self.timeout < self.lease_seconds <= 3600 and self.retry_base <= self.retry_max <= 3600
        ):
            raise ValueError("invalid relay timing bounds")
        if not 1 <= self.batch_size <= 1000 or self.max_bytes < 1 or self.max_msgs < 1:
            raise ValueError("invalid relay capacity")

    @property
    def destination(self) -> str:
        # Never expose server addresses or credentials in health/status.
        return hashlib.sha256(
            json.dumps((sorted(self.servers), self.stream, self.prefix)).encode()
        ).hexdigest()

    def stream_config(self) -> StreamConfig:
        return StreamConfig(
            name=self.stream,
            subjects=[self.prefix + ".>"],
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.NEW,
            max_bytes=self.max_bytes,
            max_msgs=self.max_msgs,
            max_age=0,
            max_msgs_per_subject=-1,
            max_msg_size=-1,
            num_replicas=1,
            duplicate_window=self.duplicate_window,
            deny_delete=True,
            deny_purge=True,
            # Omit on the wire for 2.11; pin against future SDK defaults. On 2.12+
            # this selects default persistence, never early-ack async mode.
            persist_mode=None,
        )


def validate_stream(actual: StreamConfig, config: RelayConfig) -> None:
    # The server can omit default; nats-py may return a string or its str Enum.
    # Reject unknown values too: an unrecognized mode is not a durability promise.
    if actual.persist_mode not in (None, "default"):
        raise StreamMismatch()
    expected = config.stream_config()
    keys = (
        "name",
        "subjects",
        "storage",
        "retention",
        "discard",
        "max_bytes",
        "max_msgs",
        "max_age",
        "max_msgs_per_subject",
        "max_msg_size",
        "num_replicas",
        "duplicate_window",
        "deny_delete",
        "deny_purge",
    )
    if any(getattr(actual, key) != getattr(expected, key) for key in keys):
        raise StreamMismatch()
    if any(
        getattr(actual, key, None)
        for key in (
            "no_ack",
            "sealed",
            "mirror",
            "sources",
            "republish",
            "subject_transform",
            "allow_rollup_hdrs",
            "discard_new_per_subject",
            "template_owner",
            "allow_msg_ttl",
            "allow_msg_schedules",
            "allow_atomic",
            "allow_batched",
            "allow_msg_counter",
            "subject_delete_marker_ttl",
        )
    ):
        raise StreamMismatch()


class Publisher(Protocol):
    async def publish(self, subject: str, payload: bytes, event_id: str) -> PubAck: ...


class JetStreamPublisher:
    def __init__(self, config: RelayConfig) -> None:
        self.config = config
        self._nc: Client | None = None

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None

    async def publish(self, subject: str, payload: bytes, event_id: str) -> PubAck:
        async def quiet_error(_error: Exception) -> None:
            # Driver errors may include addresses/tokens; only the DB generic code is exposed.
            pass

        try:
            if self._nc is None or self._nc.is_closed:
                self._nc = Client()
                await self._nc.connect(
                    servers=list(self.config.servers),
                    token=self.config.token,
                    connect_timeout=math.ceil(self.config.timeout),
                    allow_reconnect=False,
                    error_cb=quiet_error,
                )
            js = self._nc.jetstream(timeout=self.config.timeout)
            try:
                info = await js.stream_info(self.config.stream)
            except NotFoundError:
                info = await js.add_stream(config=self.config.stream_config())
            # Never update an existing stream, including on a concurrent creator race.
            validate_stream(info.config, self.config)
            return await js.publish(
                subject,
                payload,
                timeout=self.config.timeout,
                stream=self.config.stream,
                headers={"Nats-Msg-Id": event_id},
            )
        except BaseException:
            await self.close()
            raise


@dataclass(frozen=True)
class Claim:
    commit_seq: int
    ordinal: int
    event_id: str
    event_text: str = field(repr=False)
    token: int
    attempts: int


@dataclass(frozen=True)
class RelaySummary:
    delivered: int = 0
    status: str = "idle"


class RelayStore:
    def __init__(self, dsn: str, config: RelayConfig) -> None:
        self._dsn, self.config = dsn, config
        with connection(dsn) as conn:
            check_schema(conn)

    def claim(self) -> Claim | None:
        # Independent short row lock, NEVER a transaction around broker I/O.
        with connection(self._dsn) as conn, conn.transaction():
            state = one(
                conn,
                "SELECT fence,lease_until>clock_timestamp(),paused,destination "
                "FROM storage.relay_state WHERE singleton FOR UPDATE",
            )
            if state[3] is not None and state[3] != self.config.destination:
                raise StreamMismatch()
            if state[1] or state[2]:
                return None
            row = conn.execute(
                "SELECT commit_seq,ordinal,event_id,event_text,attempts,"
                "next_attempt_at<=clock_timestamp() FROM storage.outbox "
                "WHERE delivered_at IS NULL ORDER BY commit_seq,ordinal LIMIT 1"
            ).fetchone()
            # Never skip a delayed/failed head in favour of a newer ready event.
            if row is None or not row[5]:
                return None
            token = state[0] + 1
            conn.execute(
                "UPDATE storage.relay_state SET fence=%s,"
                "lease_until=clock_timestamp()+%s*interval '1 second',"
                "destination=%s WHERE singleton",
                (token, self.config.lease_seconds, self.config.destination),
            )
            conn.execute(
                "UPDATE storage.outbox SET claim_token=%s,attempts=attempts+1 "
                "WHERE commit_seq=%s AND ordinal=%s",
                (token, row[0], row[1]),
            )
            return Claim(row[0], row[1], row[2], row[3], token, row[4] + 1)

    def subject(self, claim: Claim) -> str:
        event = EVENT_ADAPTER.validate_json(claim.event_text)
        if event.event_id != claim.event_id:
            raise ValueError("outbox identity mismatch")
        dtype = event.data_type if isinstance(event, DataChanged) else event.target_type
        with connection(self._dsn) as conn:
            row = conn.execute(
                "SELECT spec_text FROM storage.registry WHERE name=%s", (dtype,)
            ).fetchone()
        if row is None:
            raise ValueError("outbox type missing")
        owner = json.loads(row[0])["writer"]
        # Match the public type grammar without a relay-only length cap. Module
        # identifier/DDL bounds are enforced at bootstrap, not on data type names.
        if not all(
            isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9_]*", v) for v in (dtype, owner)
        ):
            raise ValueError("invalid outbox routing metadata")
        return f"{self.config.prefix}.{event.event_type}.{dtype}.{owner}"

    def finish(self, claim: Claim, *, ack: PubAck | None = None, error: str | None = None) -> bool:
        if ack is not None and (ack.stream != self.config.stream or ack.seq < 1):
            raise ValueError("invalid publish acknowledgment")
        if (ack is None) == (error is None):
            raise ValueError("finish requires exactly one outcome")
        if error not in (None, "publish_failed", "invalid_event", "stream_mismatch"):
            raise ValueError("unsafe relay error code")
        with connection(self._dsn) as conn, conn.transaction():
            state = one(
                conn,
                "SELECT fence,lease_until>clock_timestamp(),paused "
                "FROM storage.relay_state WHERE singleton FOR UPDATE",
            )
            if state[0] != claim.token or not state[1] or state[2]:
                return False
            delay = min(
                self.config.retry_max, self.config.retry_base * 2 ** min(claim.attempts - 1, 20)
            )
            result = conn.execute(
                "UPDATE storage.outbox SET delivered_at=CASE WHEN %s THEN clock_timestamp() END,"
                "ack_stream=%s,ack_seq=%s,last_error=%s,"
                "next_attempt_at=clock_timestamp()+%s*interval '1 second' "
                "WHERE commit_seq=%s AND ordinal=%s AND claim_token=%s AND delivered_at IS NULL",
                (
                    ack is not None,
                    ack.stream if ack else None,
                    ack.seq if ack else None,
                    error,
                    delay,
                    claim.commit_seq,
                    claim.ordinal,
                    claim.token,
                ),
            )
            conn.execute("UPDATE storage.relay_state SET lease_until='-infinity' WHERE singleton")
            return result.rowcount == 1

    def pause(self, paused: bool) -> None:
        # Internal operation: fences progress, not an external broker send.
        with connection(self._dsn) as conn, conn.transaction():
            conn.execute(
                "UPDATE storage.relay_state SET paused=%s,fence=fence+1,"
                "lease_until='-infinity' WHERE singleton",
                (paused,),
            )

    def health(self) -> dict[str, Any]:
        with connection(self._dsn) as conn, conn.transaction():
            check_schema(conn)
            state = one(
                conn,
                "SELECT paused,lease_until>clock_timestamp() "
                "FROM storage.relay_state WHERE singleton",
            )
            counts = one(
                conn,
                "SELECT count(*) FILTER (WHERE delivered_at IS NULL),"
                "count(*) FILTER (WHERE delivered_at IS NOT NULL) FROM storage.outbox",
            )
            head = conn.execute(
                "SELECT commit_seq,ordinal,attempts,last_error,"
                "CASE WHEN isfinite(next_attempt_at) THEN next_attempt_at::text END "
                "FROM storage.outbox WHERE delivered_at IS NULL ORDER BY commit_seq,ordinal LIMIT 1"
            ).fetchone()
        # No event bytes, raw exceptions, DSNs, tokens, or untrusted identifiers.
        safe_error = (
            head[3]
            if head and head[3] in {"publish_failed", "invalid_event", "stream_mismatch"}
            else None
        )
        return {
            "scope": "database_relay_state",
            "broker_probed": False,
            "paused": state[0],
            "leased": state[1],
            "pending": counts[0],
            "delivered": counts[1],
            "head": None
            if head is None
            else {
                "commit_seq": head[0],
                "ordinal": head[1],
                "attempts": head[2],
                "last_error": safe_error,
                "next_attempt_at": head[4],
            },
            "ready": not state[0] and safe_error is None,
            "status": "paused"
            if state[0]
            else "in_flight"
            if state[1]
            else "retry"
            if head and (safe_error or head[2])
            else "pending"
            if head
            else "idle",
        }


class Relay:
    def __init__(self, store: RelayStore, publisher: Publisher) -> None:
        self.store, self.publisher = store, publisher

    async def run_once(self) -> RelaySummary:
        delivered = 0
        for _ in range(self.store.config.batch_size):
            claim = await asyncio.to_thread(self.store.claim)
            if claim is None:
                return RelaySummary(delivered, "idle")
            try:
                subject = await asyncio.to_thread(self.store.subject, claim)
            except Exception:
                await asyncio.to_thread(self.store.finish, claim, error="invalid_event")
                return RelaySummary(delivered, "invalid_event")
            try:
                # Total network bound, including connection, stream validation and PubAck.
                async with asyncio.timeout(self.store.config.timeout):
                    ack = await self.publisher.publish(
                        subject, claim.event_text.encode(), claim.event_id
                    )
                if ack.stream != self.store.config.stream or ack.seq < 1:
                    raise ValueError("invalid acknowledgment")
            except Exception as exc:
                error = "stream_mismatch" if isinstance(exc, StreamMismatch) else "publish_failed"
                await asyncio.to_thread(self.store.finish, claim, error=error)
                return RelaySummary(delivered, error)
            # A DB failure here intentionally leaves the lease/pending row for retry.
            if not await asyncio.to_thread(self.store.finish, claim, ack=ack):
                return RelaySummary(delivered, "fenced")
            delivered += 1
        return RelaySummary(delivered, "batch_limit")


async def run_relay(store: RelayStore, *, once: bool = False) -> RelaySummary:
    publisher = JetStreamPublisher(store.config)
    try:
        relay = Relay(store, publisher)
        while True:
            result = await relay.run_once()
            if once:
                return result
            await asyncio.sleep(0.25 if result.delivered else 1)
    finally:
        await publisher.close()
