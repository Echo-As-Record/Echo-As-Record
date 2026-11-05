"""Feedback-only, deployment-internal management; no CRUD/credential transport.

All SQL state changes share the writer linearization gate. Broker readiness runs
outside that gate and is revalidated against identity/config/revision afterwards.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from dataclasses import asdict, replace
from typing import Any

from ear_contracts import DataChanged, NewRawRecord, Source, new_id, utcnow
from ear_storage_client import (
    AppendRaw,
    Context,
    InvalidRequest,
    MarkProcessed,
    PermissionDenied,
    PutSystemRecord,
    WriteOp,
)
from ear_storage_client.consumer_wire import ConsumerRequest, DeliveryCommand, RegisterConsumer
from ear_storage_client.engine import canonical_json

from ear_storage.auth import Principal, authenticate, token_digest
from ear_storage.backend import PostgresClient, PostgresStorage
from ear_storage.codec import EVENT_ADAPTER, encode_request
from ear_storage.consumer_broker import ConsumerUnavailable
from ear_storage.consumers import ConsumerService, approved_filters
from ear_storage.management_input import ManagementInput
from ear_storage.reader import ConnectionBoundReader
from ear_storage.repository import registry_from_db
from ear_storage.schema import check_schema, connection, create_module, one, writer

FEEDBACK = "plugin_management_feedback"
RESULT = "plugin_management_result"
CONSUMER = "storage.management"


def require_admin(principal: Principal) -> None:
    if principal.module != "api" or not principal.admin:
        raise PermissionDenied("access denied")


def prepare_feedback(conn, principal: Principal, ops) -> None:
    for op in ops:
        if not isinstance(op, AppendRaw) or op.record.data_type != FEEDBACK:
            continue
        require_admin(principal)
        body = ManagementInput.model_validate(op.record.payload)
        text = canonical_json(body.payload())
        # Check both raw-ID and dedup aliases BEFORE accepting any raw receipt.
        rows = conn.execute(
            "SELECT i.id,a.principal_id,a.request_id,a.body_text FROM storage.identity i "
            "LEFT JOIN storage.management_authority a ON a.feedback_id=i.id "
            "WHERE i.id=%s OR (i.data_type=%s AND i.adapter_version=%s AND i.dedup_key=%s)",
            (op.record.id, FEEDBACK, op.record.adapter_version, op.record.dedup_key),
        ).fetchall()
        for row in rows:
            if row[1:] != (principal.id, body.request_id, text):
                raise PermissionDenied("access denied")
        old = conn.execute(
            "SELECT feedback_id,body_text FROM storage.management_authority "
            "WHERE principal_id=%s AND request_id=%s",
            (principal.id, body.request_id),
        ).fetchone()
        if old is not None and (old[1] != text or old[0] not in {r[0] for r in rows}):
            raise InvalidRequest("management request reused")


def write_internal(conn, backend, ops: list[WriteOp], feedback_id: str) -> None:
    import hashlib

    context = Context(root_feedback_id=feedback_id, depth=0)
    digest = hashlib.sha256(encode_request(ops, context).encode()).hexdigest()
    PostgresClient(backend, "storage")._write_locked(conn, ops, context, new_id(), digest).check()


def attach_feedback(conn, backend, principal: Principal, ops, result) -> None:
    if not result.ok:
        return
    for op, receipt in zip(ops, result.results, strict=True):
        if not isinstance(op, AppendRaw) or op.record.data_type != FEEDBACK:
            continue
        body = ManagementInput.model_validate(op.record.payload)
        text = canonical_json(body.payload())
        old = conn.execute(
            "SELECT principal_id,request_id,body_text FROM storage.management_authority "
            "WHERE feedback_id=%s",
            (receipt.id,),
        ).fetchone()
        if old is not None:
            if old != (principal.id, body.request_id, text):
                raise PermissionDenied("access denied")
            continue
        if receipt.outcome != "written" or receipt.event_id is None:
            raise PermissionDenied("access denied")
        conn.execute(
            "INSERT INTO storage.management_authority VALUES (%s,%s,%s,%s)",
            (receipt.id, principal.id, body.request_id, text),
        )
        result_id = new_id()
        conn.execute(
            "INSERT INTO storage.management_tasks(feedback_id,event_id,result_id) VALUES "
            "(%s,%s,%s)",
            (receipt.id, receipt.event_id, result_id),
        )
        write_internal(
            conn,
            backend,
            [
                PutSystemRecord(
                    RESULT,
                    result_id,
                    {
                        "feedback_id": receipt.id,
                        "request_id": body.request_id,
                        "module": body.module,
                        "action": body.action,
                        "status": "accepted",
                        "stage": "accepted",
                    },
                )
            ],
            receipt.id,
        )


def submit(client, body: ManagementInput) -> dict[str, Any]:
    # Reuse the original raw envelope on response loss; authority is rechecked by write().
    with client._session() as (conn, principal, _reader):
        require_admin(principal)
        old = conn.execute(
            "SELECT feedback_id,body_text FROM storage.management_authority "
            "WHERE principal_id=%s AND request_id=%s",
            (principal.id, body.request_id),
        ).fetchone()
        text = canonical_json(body.payload())
        if old is not None:
            if old[1] != text:
                raise InvalidRequest("management request reused")
            task = one(
                conn,
                "SELECT result_id FROM storage.management_tasks WHERE feedback_id=%s",
                (old[0],),
            )
            return {"status": "accepted", "feedback_id": old[0], "result_id": task[0]}
    now = utcnow()
    raw = NewRawRecord(
        id=new_id(),
        data_type=FEEDBACK,
        event_start=now,
        event_end=now,
        source=Source(device="storage_management_api", channel="management", user="default"),
        adapter_version="management-v1",
        dedup_key=principal.id + ":" + body.request_id,
        payload=body.payload(),
    )
    receipt = client.write([AppendRaw(raw)], Context.system()).check()[0]
    with client._session() as (conn, principal, _reader):
        row = one(
            conn,
            "SELECT result_id FROM storage.management_tasks WHERE feedback_id=%s",
            (receipt.id,),
        )
        return {"status": "accepted", "feedback_id": receipt.id, "result_id": row[0]}


class ManagementFailure(Exception):
    pass


class ResourcesPending(ManagementFailure):
    pass


def _permissions(conn, module: str) -> tuple[set[str], set[str]]:
    rows = conn.execute(
        "SELECT data_type,can_subscribe FROM storage.module_permissions WHERE module=%s", (module,)
    ).fetchall()
    return {r[0] for r in rows}, {r[0] for r in rows if r[1]}


def _replace_permissions(conn, body: ManagementInput) -> None:
    known = {r[0] for r in conn.execute("SELECT name FROM storage.registry")}
    if not set(body.read_types) <= known:
        raise ManagementFailure("unknown_permission_type")
    conn.execute("DELETE FROM storage.module_permissions WHERE module=%s", (body.module,))
    for dtype in sorted(set(body.read_types)):
        conn.execute(
            "INSERT INTO storage.module_permissions VALUES (%s,%s,%s)",
            (body.module, dtype, dtype in body.subscribe_types),
        )
    # Existing identities never keep broader grants; deployment-issued identities remain separate.
    for (pid,) in conn.execute(
        "SELECT id FROM storage.principals WHERE module=%s", (body.module,)
    ).fetchall():
        conn.execute("DELETE FROM storage.type_grants WHERE principal_id=%s", (pid,))
        for dtype in sorted(set(body.read_types)):
            conn.execute(
                "INSERT INTO storage.type_grants VALUES (%s,%s,%s)",
                (pid, dtype, dtype in body.subscribe_types),
            )


def _status(conn, module: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT enabled,resources_ready,revision,recovery_pending,ever_enabled FROM "
        "storage.module_lifecycle WHERE module=%s",
        (module,),
    ).fetchone()
    if row is None:
        raise ManagementFailure("plugin_not_registered")
    reads, subs = _permissions(conn, module)
    return dict(
        enabled=row[0],
        resources_ready=row[1],
        ever_enabled=row[4],
        revision=row[2],
        need_recovery_decision=row[3],
        online=None,
        read_types=sorted(reads),
        subscribe_types=sorted(subs),
    )


def _missed(conn, module: str) -> bool:
    row = one(
        conn,
        "SELECT interrupted_seq,recovery_types_text FROM storage.module_lifecycle WHERE module=%s",
        (module,),
    )
    if row[0] is None:
        return False
    types = json.loads(row[1])
    return bool(
        conn.execute(
            "SELECT 1 FROM storage.outbox WHERE commit_seq>%s "
            "AND "
            "COALESCE(event_text::jsonb->>'data_type', "
            "event_text::jsonb->>'target_type')=ANY(%s) LIMIT 1",
            (row[0], types),
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM storage.consumers c JOIN storage.outbox o "
            "ON o.commit_seq>c.creation_cursor WHERE c.module=%s "
            "AND COALESCE(o.event_text::jsonb->>'data_type', "
            "o.event_text::jsonb->>'target_type')=ANY(%s) "
            "AND NOT EXISTS(SELECT 1 FROM storage.processed p "
            "WHERE p.consumer=c.name AND p.event_id=o.event_id) "
            "AND NOT EXISTS(SELECT 1 FROM storage.consumer_deliveries d "
            "WHERE d.consumer=c.name AND d.event_id=o.event_id AND d.state='acked') LIMIT 1",
            (module, types),
        ).fetchone()
    )


def _interrupt(conn, module: str, types: set[str]) -> None:
    seq = one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]
    old = one(
        conn, "SELECT recovery_types_text FROM storage.module_lifecycle WHERE module=%s", (module,)
    )
    types |= set(json.loads(old[0]))
    conn.execute(
        "UPDATE storage.module_lifecycle SET recovery_pending=true, "
        "interrupted_seq=COALESCE(interrupted_seq,%s), recovery_types_text=%s WHERE module=%s",
        (seq, canonical_json(sorted(types)), module),
    )


class ManagementProcessor:
    def __init__(self, backend: PostgresStorage, consumers: ConsumerService | None = None):
        self.backend, self.consumers = backend, consumers

    def _task(self, conn, feedback_id):
        row = conn.execute(
            "SELECT a.body_text,p.active,p.admin,p.module,t.status,t.result_id,t.event_id "
            "FROM storage.management_authority a JOIN storage.principals p ON p.id=a.principal_id "
            "JOIN storage.management_tasks t USING(feedback_id) WHERE a.feedback_id=%s",
            (feedback_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest("unknown management feedback")
        return row, ManagementInput.model_validate_json(row[0])

    def reconcile(self, conn, result_id: str | None = None) -> None:
        """Storage-owned recovery, including crash after nack and final lease expiry.

        Use the latest scoped delivery, so an approved replay supersedes older DLQ
        entries. No broker I/O, business event or successful consumption marker.
        The caller holds the writer gate; status reads and workers share this path.
        """
        tasks = conn.execute(
            "SELECT t.feedback_id,d.delivery_id,d.state,d.attempt,"
            "d.lease_until<=clock_timestamp(),c.config_text "
            "FROM storage.management_tasks t JOIN storage.consumers c ON c.name=%s "
            "JOIN LATERAL (SELECT * FROM storage.consumer_deliveries d "
            "WHERE d.consumer=c.name AND d.event_id=t.event_id "
            "ORDER BY d.queue_seq DESC LIMIT 1) d ON true "
            "WHERE t.status IN ('accepted','pending') "
            "AND (%s::text IS NULL OR t.result_id=%s)",
            (CONSUMER, result_id, result_id),
        ).fetchall()
        for fid, did, state, attempt, expired, config in tasks:
            exhausted = (
                state in {"retry", "inflight"}
                and expired
                and attempt >= json.loads(config)["policy"]["max_attempts"]
            )
            if state != "dead" and not exhausted:
                continue
            if exhausted:
                conn.execute(
                    "UPDATE storage.consumer_deliveries SET state='dead',"
                    "error_code='lease_expired',ack_pending=(broker_reply IS NOT NULL) "
                    "WHERE consumer=%s AND delivery_id=%s",
                    (CONSUMER, did),
                )
            row, body = self._task(conn, fid)
            self._record(
                conn,
                fid,
                row,
                body,
                "failed",
                "recovery_required",
                "delivery_lease_exhausted" if exhausted else "delivery_dead",
            )

    def _authorized(self, conn, row) -> bool:
        if not (row[1] and row[2] and row[3] == "api"):
            return False
        previous = ConnectionBoundReader(conn).get_system_record(row[5])
        approval = previous.payload.get("recovery_feedback_id") if previous else None
        if approval:
            approver, _ = self._task(conn, approval)
            return bool(approver[1] and approver[2] and approver[3] == "api")
        return True

    def _retry_task(self, conn, feedback_id, body, worker_token):
        """An admin feedback approves ONLY this task's original consumer DLQ.

        Original request/feedback/event/result/resource identities are retained.
        Approval is checked again when the original task actually resumes. A
        repeated approval feedback cannot enqueue twice or grant any permission.
        Worker credentials must still belong to the original consumer principal.
        """
        if self.consumers is None or worker_token is None:
            raise ResourcesPending("management_worker_required")
        worker = authenticate(conn, token_digest(worker_token))
        if worker.module != "storage" or worker.subscribe_types != frozenset({FEEDBACK}):
            raise PermissionDenied("management worker scope required")
        consumer = self.consumers.owned(conn, worker, CONSUMER)
        self.reconcile(conn)
        try:
            target, original = self._task(conn, body.target_feedback_id)
        except InvalidRequest:
            raise ManagementFailure("retry_target_unavailable") from None
        if original.module != body.module:
            raise ManagementFailure("retry_target_mismatch")
        if not (target[1] and target[2] and target[3] == "api"):
            raise ManagementFailure("original_authority_revoked")
        previous = ConnectionBoundReader(conn).get_system_record(target[5])
        assert previous is not None
        if target[4] == "succeeded":
            raise ManagementFailure("task_already_completed")
        if target[4] != "failed" or previous.payload["stage"] != "recovery_required":
            raise ManagementFailure("task_not_retryable")
        dead = conn.execute(
            "SELECT delivery_id,state FROM storage.consumer_deliveries "
            "WHERE consumer=%s AND event_id=%s ORDER BY queue_seq DESC LIMIT 1",
            (CONSUMER, target[6]),
        ).fetchone()
        if dead is None or dead[1] != "dead":
            raise ManagementFailure("task_not_retryable")
        # Same authorization and queue semantics as consumer replay, inside the
        # task/result transaction. No global event republish and no HTTP self-call.
        self.consumers.event(
            conn, worker, registry_from_db(conn), json.loads(consumer[3]), target[6]
        )
        conn.execute(
            "INSERT INTO storage.consumer_deliveries"
            "(consumer,delivery_id,event_id,replay_of,state) VALUES (%s,%s,%s,%s,'retry')",
            (CONSUMER, new_id(), target[6], dead[0]),
        )
        payload = self._record(
            conn, body.target_feedback_id, target, original, "pending", "recovery_queued"
        )
        payload["recovery_feedback_id"] = feedback_id
        current = ConnectionBoundReader(conn).get_system_record(target[5])
        assert current is not None
        write_internal(
            conn,
            self.backend,
            [PutSystemRecord(RESULT, target[5], payload, expected_revision=current.revision)],
            body.target_feedback_id,
        )

    def _record(self, conn, feedback_id, row, body, status, stage, error=None):
        if status == "failed" and stage != "recovery_required" and body.action == "set_permissions":
            from ear_storage.reconfiguration import cancel

            cancel(conn, feedback_id)
        payload = dict(
            feedback_id=feedback_id,
            request_id=body.request_id,
            module=body.module,
            action=body.action,
            status=status,
            stage=stage,
            error_code=error,
        )
        with contextlib.suppress(ManagementFailure):
            payload.update(_status(conn, body.module))
        previous = ConnectionBoundReader(conn).get_system_record(row[5])
        assert previous is not None
        if "recovery_feedback_id" in previous.payload:
            payload["recovery_feedback_id"] = previous.payload["recovery_feedback_id"]
        if body.target_feedback_id is not None:
            payload["target_feedback_id"] = body.target_feedback_id
        ops: list[WriteOp] = [
            PutSystemRecord(RESULT, row[5], payload, expected_revision=previous.revision)
        ]
        if status in {"succeeded", "failed"} and stage != "recovery_required":
            ops.append(MarkProcessed(CONSUMER, row[6]))
        write_internal(conn, self.backend, ops, feedback_id)
        conn.execute(
            "UPDATE storage.management_tasks SET status=%s,stage=%s,error_code=%s WHERE "
            "feedback_id=%s",
            (status, stage, error, feedback_id),
        )
        return payload

    def process(self, feedback_id: str, *, worker_token: str | None = None) -> dict[str, Any]:
        plan = None
        with connection(self.backend._dsn) as conn, writer(conn):
            check_schema(conn)
            self.reconcile(conn)
            row, body = self._task(conn, feedback_id)
            if row[4] in {"succeeded", "failed"}:
                result = ConnectionBoundReader(conn).get_system_record(row[5])
                assert result is not None
                return result.payload
            if not self._authorized(conn, row):
                return self._record(
                    conn, feedback_id, row, body, "failed", "authority", "authority_revoked"
                )
            try:
                if body.action == "set_permissions":
                    from ear_storage.reconfiguration import withdraw

                    withdraw(conn, body)
                # Restrictions above survive a failed resource preflight/savepoint.
                with conn.transaction():
                    if body.action == "retry_task":
                        self._retry_task(conn, feedback_id, body, worker_token)
                    elif body.action in {"rotate_credentials", "revoke_credentials"}:
                        from ear_storage.credentials import stage_credentials

                        try:
                            stage_credentials(
                                conn,
                                feedback_id,
                                body.module,
                                revoke=body.action == "revoke_credentials",
                            )
                        except InvalidRequest:
                            raise ManagementFailure("credential_identity_not_ready") from None
                    elif body.action == "set_permissions":
                        from ear_storage.reconfiguration import prepare

                        plan = prepare(self, conn, feedback_id, body)
                    elif body.action == "enable":
                        plan = self._prepare_enable(conn, body)
                    else:
                        self._apply(conn, body)
                    if plan is None:
                        self._audit(conn, feedback_id, body)
                        stage = (
                            "claim_authorized"
                            if body.action == "rotate_credentials"
                            else "complete"
                        )
                        return self._record(conn, feedback_id, row, body, "succeeded", stage)
            except ResourcesPending as exc:
                return self._record(conn, feedback_id, row, body, "pending", "resources", str(exc))
            except ManagementFailure as exc:
                return self._record(conn, feedback_id, row, body, "failed", "validation", str(exc))
            self._record(conn, feedback_id, row, body, "pending", "resources")
        assert plan is not None
        try:
            if body.action == "set_permissions":
                from ear_storage.reconfiguration import install

                install(self, plan)
            else:
                assert self.consumers is not None
                self.consumers.broker.call("verify")
            if body.action != "set_permissions" and plan["config"] is not None:
                assert self.consumers is not None
                cfg = plan["config"]
                self.consumers.broker.call(
                    "create",
                    durable=plan["generation"],
                    filters=cfg["filters"],
                    start_seq=cfg["start_seq"],
                )
        except ConsumerUnavailable:
            with connection(self.backend._dsn) as conn, writer(conn):
                row, body = self._task(conn, feedback_id)
                if row[4] in {"succeeded", "failed"}:
                    result = ConnectionBoundReader(conn).get_system_record(row[5])
                    assert result is not None
                    return result.payload
                return self._record(
                    conn, feedback_id, row, body, "pending", "resources", "broker_unavailable"
                )
        with connection(self.backend._dsn) as conn, writer(conn):
            row, body = self._task(conn, feedback_id)
            if row[4] in {"succeeded", "failed"}:
                result = ConnectionBoundReader(conn).get_system_record(row[5])
                assert result is not None
                return result.payload
            if not self._authorized(conn, row):
                return self._record(
                    conn, feedback_id, row, body, "failed", "authority", "authority_revoked"
                )
            try:
                with conn.transaction():
                    if body.action == "set_permissions":
                        from ear_storage.reconfiguration import activate

                        activate(self, conn, feedback_id, body, plan)
                        self._audit(conn, feedback_id, body)
                        return self._record(conn, feedback_id, row, body, "succeeded", "complete")
                    current = self._prepare_enable(conn, body)
                    if current != plan:
                        raise ManagementFailure("resource_configuration_changed")
                    if _status(conn, body.module)["enabled"]:
                        self._audit(conn, feedback_id, body)
                        return self._record(conn, feedback_id, row, body, "succeeded", "complete")
                    seq = one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]
                    conn.execute(
                        "UPDATE storage.module_lifecycle SET enabled=true,ever_enabled=true, "
                        "recovery_pending=false,interrupted_seq=NULL,revision=revision+1 "
                        "WHERE module=%s",
                        (body.module,),
                    )
                    conn.execute(
                        "UPDATE storage.consumers SET ready=true,creation_cursor=CASE WHEN %s "
                        "THEN creation_cursor ELSE %s END WHERE name=%s",
                        (plan["ever_enabled"], seq, body.module + ".managed"),
                    )
                    self._audit(conn, feedback_id, body)
                    return self._record(conn, feedback_id, row, body, "succeeded", "complete")
            except ResourcesPending as exc:
                return self._record(conn, feedback_id, row, body, "pending", "resources", str(exc))
            except ManagementFailure as exc:
                return self._record(conn, feedback_id, row, body, "failed", "validation", str(exc))

    def _audit(self, conn, feedback_id, body):
        state = (
            {"target_feedback_id": body.target_feedback_id}
            if body.action == "retry_task"
            else _status(conn, body.module)
        )
        conn.execute(
            "INSERT INTO storage.management_audit VALUES (%s,%s,%s,%s,%s)",
            (
                feedback_id,
                body.module,
                state.get("revision", 0),
                body.action,
                canonical_json(state),
            ),
        )

    def _apply(self, conn, body):
        if body.action == "register_plugin":
            registry = registry_from_db(conn)
            if (
                body.module in {"core", "llm_gateway", "adapters"}
                or conn.execute(
                    "SELECT 1 FROM storage.modules WHERE module=%s", (body.module,)
                ).fetchone()
            ):
                raise ManagementFailure("namespace_unavailable")
            if any(t.name in registry for t in body.types):
                raise ManagementFailure("namespace_unavailable")
            if one(conn, "SELECT to_regnamespace(%s)", ("ear_m_" + body.module,))[0] is not None:
                raise ManagementFailure("namespace_unavailable")
            # Never accept core ownership from request JSON.
            create_module(conn, body.module)
            for item in body.types:
                conn.execute(
                    "INSERT INTO storage.registry VALUES (%s,%s,false)",
                    (item.name, canonical_json(item.spec(body.module).to_json())),
                )
            conn.execute(
                "INSERT INTO storage.module_lifecycle(module,resources_ready) VALUES (%s,true)",
                (body.module,),
            )
            _replace_permissions(conn, body)
            return
        state = _status(conn, body.module)
        if body.action == "status":
            return
        _reads, subs = _permissions(conn, body.module)
        if body.action == "disable":
            if state["enabled"]:
                _interrupt(conn, body.module, subs)
            conn.execute(
                "UPDATE storage.module_lifecycle SET enabled=false,revision=revision+1 "
                "WHERE module=%s",
                (body.module,),
            )

    def _prepare_enable(self, conn, body):
        if conn.execute(
            "SELECT 1 FROM storage.consumer_reconfigurations WHERE module=%s AND state='prepared'",
            (body.module,),
        ).fetchone():
            raise ResourcesPending("reconfiguration_in_progress")
        state = _status(conn, body.module)
        if state["need_recovery_decision"] and _missed(conn, body.module):
            raise ManagementFailure("need_recovery_decision")
        if self.consumers is None:
            raise ResourcesPending("broker_not_configured")
        reads, subs = _permissions(conn, body.module)
        identities = conn.execute(
            "SELECT id FROM storage.principals WHERE module=%s AND active ORDER BY id",
            (body.module,),
        ).fetchall()
        if len(identities) != 1 or not state["resources_ready"]:
            raise ManagementFailure("identity_not_ready")
        pid = identities[0][0]
        grants = dict(
            conn.execute(
                "SELECT data_type,can_subscribe FROM storage.type_grants WHERE principal_id=%s",
                (pid,),
            ).fetchall()
        )
        if set(grants) != reads or {n for n, s in grants.items() if s} != subs:
            raise ManagementFailure("identity_permissions_not_ready")
        plan: dict[str, Any] = dict(
            revision=state["revision"],
            principal=pid,
            config=None,
            generation=None,
            ever_enabled=state["ever_enabled"],
        )
        if not subs:
            if conn.execute(
                "SELECT 1 FROM storage.consumers WHERE name=%s",
                (body.module + ".managed",),
            ).fetchone():
                raise ManagementFailure("consumer_reconfiguration_required")
            return plan
        name = body.module + ".managed"
        reserved = conn.execute(
            "SELECT principal_id FROM storage.consumer_identities WHERE name=%s", (name,)
        ).fetchone()
        if reserved is not None and reserved[0] != pid:
            raise ManagementFailure("consumer_owner_conflict")
        principal = Principal(pid, body.module, frozenset(reads), frozenset(subs))
        filters = approved_filters(
            principal,
            registry_from_db(conn),
            ["data_changed", "reevaluation_requested"],
            self.consumers.relay.prefix,
        )
        policy = asdict(self.consumers.policy)
        policy.pop("history_modules")
        old = conn.execute(
            "SELECT principal_id,generation,config_text,destination,enabled,ready FROM "
            "storage.consumers WHERE name=%s",
            (name,),
        ).fetchone()
        if old:
            cfg = json.loads(old[2])
            if (
                old[0] != pid
                or cfg["filters"] != filters
                or cfg["policy"] != policy
                or old[3] != self.consumers.relay.destination
                or not old[4]
                or (state["enabled"] and not old[5])
            ):
                raise ManagementFailure("consumer_configuration_changed")
            generation = old[1]
        else:
            if state["enabled"]:
                raise ManagementFailure("consumer_configuration_changed")
            cfg = dict(
                policy=policy,
                event_types=["data_changed", "reevaluation_requested"],
                from_beginning=False,
                start_seq=one(
                    conn,
                    "SELECT COALESCE(max(ack_seq),0)+1 FROM storage.outbox WHERE ack_stream=%s",
                    (self.consumers.relay.stream,),
                )[0],
                filters=filters,
            )
            generation = "c_" + secrets.token_hex(24)
            seq = one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]
            conn.execute(
                "INSERT INTO "
                "storage.consumers(name,principal_id,module,generation,config_text,"
                "destination,creation_cursor) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    name,
                    pid,
                    body.module,
                    generation,
                    canonical_json(cfg),
                    self.consumers.relay.destination,
                    seq,
                ),
            )
        plan.update(config=cfg, generation=generation)
        return plan

    def poll_once(self, token: str) -> int:
        if self.consumers is None:
            raise ConsumerUnavailable("management consumer not configured")
        with connection(self.backend._dsn) as conn, writer(conn):
            principal = authenticate(conn, token_digest(token))
            if principal.module != "storage" or principal.subscribe_types != frozenset({FEEDBACK}):
                raise PermissionDenied("management worker scope required")
            self.reconcile(conn)
        worker = ConsumerService(
            self.backend._dsn,
            self.consumers.relay,
            replace(self.consumers.policy, history_modules=frozenset({"storage"})),
        )
        ready = worker.register(
            token,
            RegisterConsumer(consumer=CONSUMER, event_types=["data_changed"], from_beginning=True),
        )
        if ready["status"] != "ready":
            return 0
        response = self.consumers.fetch(token, ConsumerRequest(consumer=CONSUMER))
        delivery = response.get("delivery")
        if delivery is None:
            return 0
        event = EVENT_ADAPTER.validate_python(delivery["event"])
        if not isinstance(event, DataChanged) or event.data_type != FEEDBACK:
            raise PermissionDenied("management feedback only")
        result = self.process(event.data_id, worker_token=token)
        command = DeliveryCommand(consumer=CONSUMER, delivery_token=delivery["delivery_token"])
        try:
            finished = self.consumers.finish(token, command, nack=result["status"] == "pending")
            if finished["status"] == "dead":
                with connection(self.backend._dsn) as conn, writer(conn):
                    self.reconcile(conn)
        finally:
            # finish may commit dead then fail flushing its broker ack.
            with connection(self.backend._dsn) as conn, writer(conn):
                self.reconcile(conn)
        return 1
