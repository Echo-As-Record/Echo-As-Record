"""Durable HTTP proxy. All broker I/O is outside DB transactions/writer gate.

The DB owns one active delivery per consumer, including retry delay. Broker
redelivery is only an ingress mechanism; attempts and dead letters are durable.
An expired HTTP lease may repeat the SAME event, never a different active event.
Consumers have immutable broker generations, owned by a principal, not just a module.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

from ear_contracts import DataChanged, Event, Registry, new_id
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.consumer_wire import (
    ConsumerRequest,
    DeliveryCommand,
    RegisterConsumer,
    ReplayCommand,
)
from ear_storage_client.engine import canonical_json
from ear_storage_client.remote_errors import DeliveryExpired
from pydantic import ValidationError

from ear_storage.auth import Principal, authenticate, token_digest
from ear_storage.codec import EVENT_ADAPTER
from ear_storage.consumer_broker import BrokerDelivery, ConsumerBroker, ConsumerUnavailable
from ear_storage.relay import RelayConfig
from ear_storage.repository import registry_from_db
from ear_storage.schema import check_schema, connection, one, writer


@dataclass(frozen=True)
class ConsumerPolicy:
    delivery_lease: float = 30
    ack_wait: float = 60
    poll_timeout: float = 0.2
    max_attempts: int = 5
    retry_base: float = 1
    retry_max: float = 60
    history_modules: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        values = (
            self.delivery_lease,
            self.ack_wait,
            self.poll_timeout,
            self.retry_base,
            self.retry_max,
        )
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("invalid consumer timing")
        if not (
            self.poll_timeout < self.delivery_lease <= self.ack_wait <= 3600
            and self.retry_base <= self.retry_max <= 3600
            and type(self.max_attempts) is int
            and 1 <= self.max_attempts <= 100
        ):
            raise ValueError("invalid consumer policy")


def approved_filters(
    principal: Principal, registry: Registry, kinds: Sequence[str], prefix: str
) -> list[str]:
    filters = []
    for dtype in sorted(principal.subscribe_types & principal.read_types):
        spec = registry.get(dtype)
        if "data_changed" in kinds:
            filters.append(f"{prefix}.data_changed.{dtype}.*")
        if (
            "reevaluation_requested" in kinds
            and spec.writer == principal.module
            and spec.supports_reevaluation
        ):
            filters.append(f"{prefix}.reevaluation_requested.{dtype}.{principal.module}")
    if not filters:
        raise PermissionDenied("access denied")
    return filters


def authorize_event(
    principal: Principal, registry: Registry, config: dict[str, Any], event: Event, prefix: str
) -> None:
    dtype = event.data_type if isinstance(event, DataChanged) else event.target_type
    spec = registry.get(dtype)
    subject = f"{prefix}.{event.event_type}.{dtype}."
    subject += "*" if isinstance(event, DataChanged) else spec.writer
    if (
        event.event_type not in config["event_types"]
        or subject not in config["filters"]
        or dtype not in principal.subscribe_types
    ):
        raise PermissionDenied("access denied")
    principal.require_read([dtype])
    if not isinstance(event, DataChanged):
        if spec.writer != principal.module or not spec.supports_reevaluation:
            raise PermissionDenied("access denied")
        # Cause IDs/descriptions are not a public channel to bypass read grants.
        principal.require_read([event.cause.data_type])


class ConsumerService:
    def __init__(self, dsn: str, relay: RelayConfig, policy: ConsumerPolicy | None = None) -> None:
        self.dsn, self.relay, self.policy = dsn, relay, policy or ConsumerPolicy()
        if not self.policy.poll_timeout < relay.timeout < self.policy.delivery_lease:
            raise ValueError("broker timeout must fit delivery lease")
        self.broker = ConsumerBroker(relay, ack_wait=self.policy.ack_wait)

    @contextmanager
    def session(self, token: str):
        with connection(self.dsn) as conn, writer(conn):
            check_schema(conn)
            principal = authenticate(conn, token_digest(token))
            recovery = conn.execute(
                "SELECT recovery_pending FROM storage.module_lifecycle WHERE module=%s",
                (principal.module,),
            ).fetchone()
            if recovery and recovery[0]:
                raise PermissionDenied("need_recovery_decision")
            yield conn, principal, registry_from_db(conn)

    def owned(self, conn, principal: Principal, name: str):
        row = conn.execute(
            "SELECT principal_id,module,generation,config_text,destination,creation_cursor,"
            "ready,enabled,fence,lease_until>clock_timestamp() FROM storage.consumers "
            "WHERE name=%s FOR UPDATE",
            (name,),
        ).fetchone()
        if row is None or row[0] != principal.id or not principal.owns_consumer(name) or not row[7]:
            raise PermissionDenied("access denied")
        expected_policy = asdict(self.policy)
        expected_policy.pop("history_modules")
        if json.loads(row[3])["policy"] != expected_policy:
            raise ConsumerUnavailable("consumer policy mismatch")
        if row[4] != self.relay.destination:
            raise ConsumerUnavailable("consumer destination mismatch")
        return row

    def register(self, token: str, request: RegisterConsumer) -> dict[str, Any]:
        with self.session(token) as (conn, principal, registry):
            if not principal.owns_consumer(request.consumer):
                raise PermissionDenied("access denied")
            if request.from_beginning and (
                principal.module not in self.policy.history_modules
                or not any(
                    s.writer == principal.module and registry.is_core(n)
                    for n, s in registry.data_types.items()
                )
            ):
                raise PermissionDenied("history replay not authorized")
            reserved = conn.execute(
                "SELECT principal_id FROM storage.consumer_identities WHERE name=%s",
                (request.consumer,),
            ).fetchone()
            if reserved is not None and reserved[0] != principal.id:
                raise PermissionDenied("access denied")
            kinds = sorted(set(request.event_types))
            existing = conn.execute(
                "SELECT 1 FROM storage.consumers WHERE name=%s", (request.consumer,)
            ).fetchone()
            if existing:
                row = self.owned(conn, principal, request.consumer)
                cfg = json.loads(row[3])
                if cfg["event_types"] != kinds or cfg["from_beginning"] != request.from_beginning:
                    raise InvalidRequest("consumer configuration is immutable")
                # Fresh authorization without silently expanding an existing subscription.
                current = approved_filters(principal, registry, kinds, self.relay.prefix)
                if not set(cfg["filters"]) <= set(current):
                    raise PermissionDenied("access denied")
                durable = row[2]
            else:
                if (
                    reserved is None
                    and conn.execute(
                        "SELECT 1 FROM storage.processed WHERE consumer=%s LIMIT 1",
                        (request.consumer,),
                    ).fetchone()
                ):
                    # Old P1 markers have no principal provenance. Never silently adopt them.
                    raise InvalidRequest("legacy consumer requires deployment adoption")
                persisted_policy = asdict(self.policy)
                persisted_policy.pop("history_modules")
                cfg = {
                    "policy": persisted_policy,
                    "event_types": kinds,
                    "from_beginning": request.from_beginning,
                    "start_seq": 1
                    if request.from_beginning
                    else one(
                        conn,
                        "SELECT COALESCE(max(ack_seq),0)+1 FROM storage.outbox WHERE ack_stream=%s",
                        (self.relay.stream,),
                    )[0],
                    "filters": approved_filters(principal, registry, kinds, self.relay.prefix),
                }
                durable = "c_" + secrets.token_hex(24)
                cursor = (
                    0
                    if request.from_beginning
                    else one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]
                )
                conn.execute(
                    "INSERT INTO storage.consumers(name,principal_id,module,generation,config_text,"
                    "destination,creation_cursor) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        request.consumer,
                        principal.id,
                        principal.module,
                        durable,
                        canonical_json(cfg),
                        self.relay.destination,
                        cursor,
                    ),
                )
            conn.execute(
                "UPDATE storage.consumers SET ready=false WHERE name=%s", (request.consumer,)
            )
        # Persisted intent survives create failure or lost create response. Same durable on retry.
        try:
            self.broker.call(
                "create", durable=durable, filters=cfg["filters"], start_seq=cfg["start_seq"]
            )
        except ConsumerUnavailable:
            return {"codec": 1, "status": "pending"}
        with self.session(token) as (conn, principal, registry):
            current_row = self.owned(conn, principal, request.consumer)
            if current_row[2] != durable:
                return {"codec": 1, "status": "pending"}
            if not set(cfg["filters"]) <= set(
                approved_filters(principal, registry, kinds, self.relay.prefix)
            ):
                raise PermissionDenied("access denied")
            conn.execute(
                "UPDATE storage.consumers SET ready=true WHERE name=%s", (request.consumer,)
            )
        return {"codec": 1, "status": "ready"}

    def _cursor(self, conn, name, event, fallback):
        dtype = event.data_type if isinstance(event, DataChanged) else event.target_type
        cursor = conn.execute(
            "SELECT effective_commit_seq FROM storage.consumer_type_cursors "
            "WHERE consumer=%s AND data_type=%s",
            (name, dtype),
        ).fetchone()
        return cursor[0] if cursor else fallback

    def event(self, conn, principal, registry, cfg, event_id: str) -> Event:
        row = conn.execute(
            "SELECT event_text FROM storage.outbox WHERE event_id=%s AND delivered_at IS NOT NULL",
            (event_id,),
        ).fetchone()
        if row is None:
            raise ConsumerUnavailable("event not verified relayed")
        event = EVENT_ADAPTER.validate_json(row[0])
        if event.event_id != event_id:
            raise ConsumerUnavailable("event identity mismatch")
        authorize_event(principal, registry, cfg, event, self.relay.prefix)
        return event

    def _offer(self, conn, principal, registry, row, name: str, delivery):
        did, eid, attempt, state, available, live = delivery
        event = self.event(conn, principal, registry, json.loads(row[3]), eid)
        if (state == "inflight" and live) or not available:
            return {"codec": 1, "status": "busy", "delivery": None}
        if conn.execute(
            "SELECT 1 FROM storage.processed WHERE consumer=%s AND event_id=%s", (name, eid)
        ).fetchone():
            conn.execute(
                "UPDATE storage.consumer_deliveries SET state='acked',"
                "ack_pending=(broker_reply IS NOT NULL) WHERE consumer=%s AND delivery_id=%s",
                (name, did),
            )
            return {"codec": 1, "status": "progress", "delivery": None}
        if attempt >= self.policy.max_attempts:
            conn.execute(
                "UPDATE storage.consumer_deliveries SET state='dead',error_code='lease_expired',"
                "ack_pending=(broker_reply IS NOT NULL) WHERE consumer=%s AND delivery_id=%s",
                (name, did),
            )
            return {"codec": 1, "status": "progress", "delivery": None}
        delivery_token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE storage.consumer_deliveries SET state='inflight',attempt=attempt+1,"
            "token=%s,lease_until=clock_timestamp()+%s*interval '1 second' "
            "WHERE consumer=%s AND delivery_id=%s",
            (delivery_token, self.policy.delivery_lease, name, did),
        )
        conn.execute(
            "INSERT INTO storage.consumer_receipts(token,consumer,delivery_id) VALUES (%s,%s,%s)",
            (delivery_token, name, did),
        )
        # Strip future/extra event fields rather than granting an arbitrary payload channel.
        body = event.model_dump(mode="json", include=set(type(event).model_fields))
        if not isinstance(event, DataChanged):
            body["cause"] = event.cause.model_dump(
                mode="json", include=set(type(event.cause).model_fields)
            )
        return {
            "codec": 1,
            "status": "delivery",
            "delivery": {
                "event": body,
                "delivery_token": delivery_token,
                "attempt": attempt + 1,
                "delivery_id": did,
                "lease_seconds": self.policy.delivery_lease,
            },
        }

    @staticmethod
    def _cleanup_pending(reason: str) -> None:
        # Server-side status only: never expose old event/route/receipt metadata.
        logging.getLogger(__name__).warning(
            json.dumps(
                {
                    "module": "storage",
                    "event_id": None,
                    "root_feedback_id": "system",
                    "depth": 0,
                    "status": "retired_ack_cleanup_pending",
                    "reason": reason,
                }
            )
        )

    def _flush(self, token: str, name: str) -> None:
        with self.session(token) as (conn, principal, registry):
            row = self.owned(conn, principal, name)
            # One broker operation per call. Prioritize the active generation;
            # SQL excludes retired ordinary completions lacking CURRENT rights,
            # rather than loading an unbounded queue or selecting its oldest
            # denied row repeatedly. Full event/route validation follows below.
            # Unknown/foreign generations and invalid records are NOT exclusions.
            pending = conn.execute(
                "SELECT d.delivery_id,d.event_id,d.broker_reply,d.historical_filtered,"
                "d.broker_subject,d.broker_generation,g.config_text,g.destination,g.state "
                "FROM storage.consumer_deliveries d "
                "LEFT JOIN storage.consumer_generations g ON "
                "g.consumer=d.consumer AND g.generation=d.broker_generation "
                "LEFT JOIN storage.outbox o ON o.event_id=d.event_id "
                "WHERE d.consumer=%s AND d.ack_pending AND (d.broker_generation=%s "
                "OR g.generation IS NULL OR g.state<>'retired' OR g.destination<>%s "
                "OR o.event_id IS NULL OR o.delivered_at IS NULL OR d.historical_filtered "
                "OR (COALESCE(o.event_text::jsonb->>'data_type',"
                "o.event_text::jsonb->>'target_type')=ANY(%s) AND "
                "(o.event_text::jsonb->>'event_type'='data_changed' OR "
                "o.event_text::jsonb->'cause'->>'data_type'=ANY(%s)))) "
                "ORDER BY (d.broker_generation=%s) DESC NULLS LAST,d.queue_seq LIMIT 1",
                (
                    name,
                    row[2],
                    self.relay.destination,
                    sorted(principal.read_types & principal.subscribe_types),
                    sorted(principal.read_types),
                    row[2],
                ),
            ).fetchone()
            if conn.execute(
                "SELECT 1 FROM storage.consumer_deliveries WHERE consumer=%s "
                "AND ack_pending AND broker_generation<>%s LIMIT 1",
                (name, row[2]),
            ).fetchone():
                self._cleanup_pending("deferred")
            if pending is None:
                return
            retired = pending[5] != row[2]
            if retired and (
                pending[6] is None
                or pending[7] != self.relay.destination
                or pending[8] != "retired"
            ):
                raise ConsumerUnavailable("invalid ack generation")
            cfg = json.loads(pending[6] if retired else row[3])
            stored = conn.execute(
                "SELECT event_text,commit_seq FROM storage.outbox "
                "WHERE event_id=%s AND delivered_at IS NOT NULL",
                (pending[1],),
            ).fetchone()
            if stored is None:
                raise ConsumerUnavailable("event not verified relayed")
            event = EVENT_ADAPTER.validate_json(stored[0])
            if event.event_id != pending[1]:
                raise ConsumerUnavailable("event identity mismatch")
            if pending[3]:
                if stored[1] > self._cursor(conn, name, event, row[5]):
                    raise ConsumerUnavailable("invalid historical exclusion")
            else:
                # Normal completions never inherit the historical-filter exemption.
                # Use the receipt's generation, but recheck current principal grants.
                authorize_event(principal, registry, cfg, event, self.relay.prefix)
            self._verify_route(registry, cfg, event, pending[4])
        try:
            self.broker.call("ack", reply=pending[2])
        except ConsumerUnavailable:
            if not retired:
                raise
            self._cleanup_pending("broker_unavailable")
            return
        with self.session(token) as (conn, principal, _registry):
            self.owned(conn, principal, name)
            conn.execute(
                "UPDATE storage.consumer_deliveries SET ack_pending=false "
                "WHERE consumer=%s AND delivery_id=%s AND broker_reply=%s "
                "AND broker_generation=%s",
                (name, pending[0], pending[2], pending[5]),
            )

    def _verify_route(self, registry, cfg, event: Event, subject: str) -> None:
        dtype = event.data_type if isinstance(event, DataChanged) else event.target_type
        owner = registry.get(dtype).writer
        route = f"{self.relay.prefix}.{event.event_type}.{dtype}."
        subscription = route + ("*" if isinstance(event, DataChanged) else owner)
        if (
            subject != route + owner
            or event.event_type not in cfg["event_types"]
            or subscription not in cfg["filters"]
        ):
            raise ConsumerUnavailable("invalid event route")

    def fetch(self, token: str, request: ConsumerRequest) -> dict[str, Any]:
        name = request.consumer
        self._flush(token, name)
        with self.session(token) as (conn, principal, registry):
            row = self.owned(conn, principal, name)
            if not row[6]:
                return {"codec": 1, "status": "pending", "delivery": None}
            live = conn.execute(
                "SELECT delivery_id,event_id,attempt,state,true,true "
                "FROM storage.consumer_deliveries WHERE consumer=%s AND state='inflight' "
                "AND lease_until>clock_timestamp() LIMIT 1",
                (name,),
            ).fetchone()
            if live:
                return self._offer(conn, principal, registry, row, name, live)
            delivery = conn.execute(
                "SELECT delivery_id,event_id,attempt,state,available_at<=clock_timestamp(),"
                "lease_until>clock_timestamp() FROM storage.consumer_deliveries "
                "WHERE consumer=%s AND state IN ('retry','inflight') ORDER BY queue_seq LIMIT 1",
                (name,),
            ).fetchone()
            if delivery:
                return self._offer(conn, principal, registry, row, name, delivery)
            if row[9]:
                return {"codec": 1, "status": "busy", "delivery": None}
            fence = row[8] + 1
            conn.execute(
                "UPDATE storage.consumers SET "
                "fence=%s,lease_until=clock_timestamp()+%s*interval '1 second' WHERE name=%s",
                (fence, self.policy.delivery_lease, name),
            )
            durable = row[2]
        try:
            message: BrokerDelivery | None = self.broker.call(
                "fetch", durable=durable, timeout=self.policy.poll_timeout
            )
            with self.session(token) as (conn, principal, registry):
                row = self.owned(conn, principal, name)
                if row[8] != fence or not row[9] or row[2] != durable or not row[6]:
                    return {"codec": 1, "status": "busy", "delivery": None}
                if message is None:
                    return {"codec": 1, "status": "idle", "delivery": None}
                try:
                    decoded = EVENT_ADAPTER.validate_json(message.data)
                except ValidationError:
                    raise ConsumerUnavailable("unverified broker event") from None
                stored = conn.execute(
                    "SELECT event_text,commit_seq,delivered_at FROM storage.outbox WHERE "
                    "event_id=%s",
                    (decoded.event_id,),
                ).fetchone()
                if (
                    stored is None
                    or stored[2] is None
                    or EVENT_ADAPTER.validate_json(stored[0]) != decoded
                ):
                    raise ConsumerUnavailable("unverified broker event")
                self._verify_route(registry, json.loads(row[3]), decoded, message.subject)
                # No HTTP access to events committed before this subscription's creation cursor.
                # Broker may have received these later; cursor is defined under the writer gate.
                old = stored[1] <= self._cursor(conn, name, decoded, row[5])
                if not old:
                    authorize_event(
                        principal, registry, json.loads(row[3]), decoded, self.relay.prefix
                    )
                existing = conn.execute(
                    "SELECT delivery_id,state FROM storage.consumer_deliveries "
                    "WHERE consumer=%s AND event_id=%s AND replay_of IS NULL",
                    (name, decoded.event_id),
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE storage.consumer_deliveries SET "
                        "broker_reply=%s,broker_generation=%s,ack_pending=true "
                        "WHERE consumer=%s AND delivery_id=%s AND state IN ('acked','dead')",
                        (message.reply, durable, name, existing[0]),
                    )
                    return {"codec": 1, "status": "progress", "delivery": None}
                did = new_id()
                conn.execute(
                    "INSERT INTO "
                    "storage.consumer_deliveries(consumer,delivery_id,event_id,state,"
                    "broker_reply,ack_pending,historical_filtered,broker_subject,"
                    "broker_generation) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        name,
                        did,
                        decoded.event_id,
                        "acked" if old else "retry",
                        message.reply,
                        old,
                        old,
                        message.subject,
                        durable,
                    ),
                )
                if old:
                    return {"codec": 1, "status": "progress", "delivery": None}
                return self._offer(
                    conn,
                    principal,
                    registry,
                    row,
                    name,
                    (did, decoded.event_id, 0, "retry", True, False),
                )
        finally:
            # Only this poll's fence can release it. Never hold locks during network I/O.
            with connection(self.dsn) as conn:
                conn.execute(
                    "UPDATE storage.consumers SET lease_until='-infinity' WHERE name=%s AND "
                    "fence=%s",
                    (name, fence),
                )

    def finish(self, token: str, request: DeliveryCommand, *, nack: bool) -> dict[str, Any]:
        with self.session(token) as (conn, principal, registry):
            row = self.owned(conn, principal, request.consumer)
            delivery = conn.execute(
                "SELECT d.delivery_id,d.event_id,d.attempt,d.state,"
                "d.lease_until>clock_timestamp(),d.token "
                "FROM storage.consumer_receipts r JOIN storage.consumer_deliveries d "
                "ON (d.consumer,d.delivery_id)=(r.consumer,r.delivery_id) "
                "WHERE r.consumer=%s AND r.token=%s",
                (request.consumer, request.delivery_token),
            ).fetchone()
            if delivery is None:
                raise PermissionDenied("access denied")
            self.event(conn, principal, registry, json.loads(row[3]), delivery[1])
            if delivery[5] != request.delivery_token:
                raise DeliveryExpired("delivery expired")
            desired = (
                "dead"
                if nack and delivery[2] >= self.policy.max_attempts
                else "retry"
                if nack
                else "acked"
            )
            if delivery[3] != "inflight":
                if delivery[3] != desired:
                    if not delivery[4]:
                        raise DeliveryExpired("delivery expired")
                    raise InvalidRequest("delivery already completed")
            else:
                if not delivery[4]:
                    raise DeliveryExpired("delivery expired")
                delay = min(
                    self.policy.retry_max, self.policy.retry_base * 2 ** min(delivery[2] - 1, 20)
                )
                conn.execute(
                    "UPDATE storage.consumer_deliveries SET state=%s,"
                    "available_at=clock_timestamp()+%s*interval '1 second',"
                    "ack_pending=(%s AND broker_reply IS NOT NULL),error_code=%s "
                    "WHERE consumer=%s AND delivery_id=%s",
                    (
                        desired,
                        delay,
                        desired in {"acked", "dead"},
                        "handler_failed" if nack else None,
                        request.consumer,
                        delivery[0],
                    ),
                )
        # DB completion first; failure here is retryable without running handler again.
        if desired in {"acked", "dead"}:
            self._flush(token, request.consumer)
        return {"codec": 1, "status": desired}

    def deadletters(self, token: str, request: ConsumerRequest) -> dict[str, Any]:
        with self.session(token) as (conn, principal, registry):
            row = self.owned(conn, principal, request.consumer)
            rows = conn.execute(
                "SELECT delivery_id,event_id,attempt,error_code FROM storage.consumer_deliveries "
                "WHERE consumer=%s AND state='dead' ORDER BY delivery_id LIMIT 100",
                (request.consumer,),
            ).fetchall()
            result = []
            for did, eid, attempt, error in rows:
                try:
                    event = self.event(conn, principal, registry, json.loads(row[3]), eid)
                except PermissionDenied:
                    continue
                # No exception message/cause, change_description, root IDs or event body.
                result.append(
                    {
                        "delivery_id": did,
                        "event_id": eid,
                        "event_type": event.event_type,
                        "attempts": attempt,
                        "error_code": error,
                    }
                )
            return {"codec": 1, "deadletters": result}

    def replay(self, token: str, request: ReplayCommand) -> dict[str, Any]:
        with self.session(token) as (conn, principal, registry):
            row = self.owned(conn, principal, request.consumer)
            # Management recovery requires a new approved feedback, not a raw
            # transport replay that could lose the task/result recovery stage.
            if request.consumer == "storage.management":
                raise InvalidRequest("management_feedback_required")
            if row[9]:
                raise ConsumerUnavailable("consumer poll in progress; retry replay")
            original = conn.execute(
                "SELECT event_id FROM storage.consumer_deliveries WHERE consumer=%s "
                "AND delivery_id=%s AND state='dead'",
                (request.consumer, request.delivery_id),
            ).fetchone()
            if original is None:
                raise PermissionDenied("access denied")
            self.event(conn, principal, registry, json.loads(row[3]), original[0])
            # Replay identity is separate, event identity unchanged. No stream-wide republish.
            existing = conn.execute(
                "SELECT delivery_id FROM storage.consumer_deliveries WHERE consumer=%s "
                "AND replay_of=%s AND state IN ('retry','inflight')",
                (request.consumer, request.delivery_id),
            ).fetchone()
            did = existing[0] if existing else new_id()
            if existing is None:
                conn.execute(
                    "INSERT INTO "
                    "storage.consumer_deliveries(consumer,delivery_id,event_id,replay_of,state) "
                    "VALUES (%s,%s,%s,%s,'retry')",
                    (request.consumer, did, original[0], request.delivery_id),
                )
            return {"codec": 1, "delivery_id": did, "status": "queued"}
