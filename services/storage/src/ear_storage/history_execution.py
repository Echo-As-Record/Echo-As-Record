"""Authenticated, one-item history chunks. Computation never holds a DB transaction.

A bounded snapshot and lease are captured in one writer-gate transaction. The
claim-time versions are NOT a substitute for reading that snapshot. Conservative
collection guards cover even negative queries; ordinary dependency validation is
also performed by the existing backend. Rejudgment needs scheduler integration.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from types import SimpleNamespace

from ear_contracts import is_id
from ear_storage_client import (
    AppendRaw,
    AssertTypeRevision,
    Create,
    InvalidRequest,
    PermissionDenied,
    RegisterDependencies,
    Supersede,
    Update,
)
from ear_storage_client.engine import canonical_json
from ear_storage_client.wire import decode_request, encode_result

from ear_storage.backend import PostgresClient
from ear_storage.causality import record_input_types
from ear_storage.repository import registry_from_db
from ear_storage.schema import ProjectionLimit, one
from ear_storage.service import _write_permissions
from ear_storage.snapshots import create_lease, load_reader, validate_lease

LEASE_SECONDS = 300
MAX_ATTEMPTS = 5
TERMINAL = {"completed", "blocked", "failed"}
ERRORS = {"business_failed", "input_unavailable", "scheduler_unavailable", "needs_operator"}


def _job(conn, principal, job_id, *, final_read=False):
    if not is_id(job_id):
        raise InvalidRequest("invalid job id")
    row = conn.execute(
        "SELECT j.owner,j.types_text,j.state,j.decision_id,j.feedback_id,t.status, "
        "j.execution_revision,j.range_start,j.range_end "
        "FROM storage.history_jobs j JOIN storage.management_tasks t USING(feedback_id) "
        "WHERE job_id=%s",
        (job_id,),
    ).fetchone()
    if row is None or row[0] != principal.module or principal.module == "api":
        raise PermissionDenied("history_owner_required")
    final_read = final_read and (
        row[2] == "completed"
        or (
            row[3] is not None
            and one(
                conn, "SELECT state FROM storage.recovery_decisions WHERE decision_id=%s", (row[3],)
            )[0]
            != "authorized"
        )
    )
    if (
        not final_read
        and row[3] is not None
        and not conn.execute(
            "SELECT 1 FROM storage.recovery_decisions WHERE decision_id=%s "
            "AND state IN ('authorized','switched')",
            (row[3],),
        ).fetchone()
    ):
        raise PermissionDenied("history_authorization_changed")
    principal.require_read(json.loads(row[1]))
    # Management authorization/resource readiness must be FINISHED, not just queued.
    if row[5] != "succeeded":
        raise PermissionDenied("history_authorization_pending")
    identity = one(
        conn,
        "SELECT p.credential_epoch,l.revision FROM storage.principals p "
        "JOIN storage.module_lifecycle l ON l.module=p.module WHERE p.id=%s "
        "AND p.active AND l.enabled AND l.resources_ready",
        (principal.id,),
    )
    if not final_read and row[6] != identity[1]:
        raise PermissionDenied("history_authorization_changed")
    return row, identity


def _progress(conn, job_id):
    counts = dict(
        conn.execute(
            "SELECT state,count(*) FROM storage.history_items WHERE job_id=%s GROUP BY state",
            (job_id,),
        ).fetchall()
    )
    state = (
        "failed"
        if counts.get("failed")
        else "blocked"
        if counts.get("blocked") or counts.get("unavailable")
        else "running"
        if counts.get("claimed") or counts.get("candidate")
        else "completed"
    )
    conn.execute(
        "UPDATE storage.history_jobs SET state=%s,error_code=CASE WHEN %s "
        "THEN 'input_unavailable' ELSE error_code END WHERE job_id=%s",
        (state, bool(counts.get("unavailable")), job_id),
    )
    return state, counts


def _release_recovery(conn, job_id):
    """Only a fully completed finite set advances covered cursors; W+1 stays live."""
    from ear_storage.history import switch

    row = one(
        conn,
        "SELECT d.module,d.lifecycle_revision,d.mode,d.decision_id FROM storage.history_jobs j "
        "JOIN storage.recovery_decisions d USING(decision_id) WHERE j.job_id=%s",
        (job_id,),
    )
    if not switch(conn, SimpleNamespace(module=row[0], expected_revision=row[1])):
        return
    # Expire any pre-recovery receipts. No MarkProcessed or request completion is forged.
    conn.execute(
        "UPDATE storage.consumers SET fence=fence+1,lease_until='-infinity' WHERE module=%s",
        (row[0],),
    )
    conn.execute(
        "UPDATE storage.consumer_deliveries d SET state='skipped',token=NULL,"
        "lease_until='-infinity',error_code='history_covered',"
        "ack_pending=(broker_reply IS NOT NULL) WHERE d.state IN ('retry','inflight','dead') "
        "AND (EXISTS (SELECT 1 FROM storage.recovery_excluded_events x WHERE "
        "x.decision_id=%s AND x.consumer=d.consumer AND x.event_id=d.event_id) OR EXISTS "
        "(SELECT 1 FROM storage.recovery_exclusions x JOIN storage.outbox o "
        "ON o.commit_seq>x.lower_seq AND o.commit_seq<=x.upper_seq "
        "WHERE x.decision_id=%s AND x.consumer=d.consumer AND o.event_id=d.event_id AND "
        "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type')="
        "x.data_type))",
        (row[3], row[3]),
    )


def _finish_progress(conn, job_id, job):
    if conn.execute(
        "SELECT 1 FROM storage.history_request_links WHERE job_id=%s LIMIT 1", (job_id,)
    ).fetchone():
        from ear_storage.auth import authenticate
        from ear_storage.history_scheduler import reconcile
        from ear_storage.reader import ConnectionBoundReader

        digest = one(
            conn, "SELECT token_hash FROM storage.principals WHERE module=%s AND active", (job[0],)
        )[0]
        reconcile(conn, authenticate(conn, digest), ConnectionBoundReader(conn), job_id)
    state, counts = _progress(conn, job_id)
    if state == "completed" and job[3] is not None:
        _release_recovery(conn, job_id)
    return state, counts


def claim(client, job_id):
    with client._session() as (conn, principal, reader):
        if conn.execute(
            "SELECT 1 FROM storage.history_request_links WHERE job_id=%s LIMIT 1", (job_id,)
        ).fetchone():
            from ear_storage.history_scheduler import reconcile

            reconcile(conn, principal, reader, job_id)
        job, identity = _job(conn, principal, job_id)
        scheduler_wait = (
            conn.execute(
                "SELECT 1 FROM storage.history_items WHERE job_id=%s AND state='candidate' "
                "AND item_kind='current_state'",
                (job_id,),
            ).fetchone()
            and not conn.execute(
                "SELECT 1 FROM storage.history_items WHERE job_id=%s AND state IN "
                "('blocked','failed','unavailable') AND item_kind<>'reevaluation_needed'",
                (job_id,),
            ).fetchone()
        )
        if job[2] in TERMINAL and not (job[2] == "blocked" and scheduler_wait):
            return {
                "status": job[2],
                "item": None,
                "error_code": one(
                    conn, "SELECT error_code FROM storage.history_jobs WHERE job_id=%s", (job_id,)
                )[0],
            }
        item = conn.execute(
            "SELECT data_type,object_id,item_kind,fence,attempts,snapshot_id FROM "
            "storage.history_items WHERE job_id=%s AND (state='candidate' OR "
            "(state='claimed' AND lease_until<=clock_timestamp())) "
            "ORDER BY data_type,object_id LIMIT 1",
            (job_id,),
        ).fetchone()
        if item is None:
            state, counts = _finish_progress(conn, job_id, job)
            return {
                "status": "busy" if counts.get("claimed") else state,
                "item": None,
                "error_code": "input_unavailable" if counts.get("unavailable") else None,
            }
        dtype, rid, kind, fence, attempts, old_snapshot = item
        principal.require_read([dtype])
        current = reader.get(rid)
        principal.require_read(record_input_types(current, registry_from_db(conn)))
        error = None
        if (
            current is None
            or current.data_type != dtype
            or getattr(current, "status", None) == "superseded"
        ):
            error = "input_unavailable"
        elif kind == "reevaluation_needed":
            error = "scheduler_unavailable"
        elif attempts >= MAX_ATTEMPTS:
            error = "needs_operator"
        if error:
            conn.execute(
                "UPDATE storage.history_items SET state='blocked',error_code=%s "
                "WHERE job_id=%s AND data_type=%s AND object_id=%s",
                (error, job_id, dtype, rid),
            )
            conn.execute(
                "UPDATE storage.history_jobs SET state='blocked',error_code=%s WHERE job_id=%s",
                (error, job_id),
            )
            return {
                "status": "blocked",
                "error_code": error,
                "item": {
                    "data_type": dtype,
                    "id": rid,
                    "requires_scheduler": kind == "reevaluation_needed",
                },
            }
        assert current is not None
        # Delete only this item's previous lease, never an arbitrary caller's snapshot.
        if old_snapshot:
            conn.execute(
                "DELETE FROM storage.snapshot_leases WHERE snapshot_id=%s", (old_snapshot,)
            )
        ttl = min(LEASE_SECONDS, client._snapshot_limits.max_ttl_seconds)
        lease = create_lease(conn, principal, client._snapshot_limits, ttl)
        guards = {t: reader.type_revision(t) for t in sorted(principal.read_types)}
        token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE storage.history_items SET state='claimed',fence=fence+1,attempts=attempts+1,"
            "claim_hash=%s,principal_id=%s,credential_epoch=%s,lifecycle_revision=%s,"
            "lease_until=%s,snapshot_id=%s,current_version=%s,guards_text=%s "
            "WHERE job_id=%s AND data_type=%s AND object_id=%s",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                principal.id,
                *identity,
                lease.expires_at,
                lease.snapshot_id,
                current.version,
                canonical_json(guards),
                job_id,
                dtype,
                rid,
            ),
        )
        conn.execute("UPDATE storage.history_jobs SET state='running' WHERE job_id=%s", (job_id,))
        return {
            "status": "claimed",
            "claim_token": token,
            "fence": fence + 1,
            "snapshot": lease.to_json(),
            "item": {
                "data_type": dtype,
                "id": rid,
                "version": current.version,
                "item_kind": kind,
                "requires_scheduler": False,
            },
            "query_context": {
                "input_types": json.loads(job[1]),
                "rangeStart": job[7].isoformat().replace("+00:00", "Z") if job[7] else None,
                "rangeEnd": job[8].isoformat().replace("+00:00", "Z") if job[8] else None,
                "root_feedback_id": job[4],
                "context_types": sorted(principal.read_types),
                "snapshot_required": True,
                "context_reads_are_not_history_generation_grants": True,
            },
        }


def commit(client, job_id, body):
    """Closed request, current auth even on replay; no external I/O inside the gate."""
    from ear_storage_client.wire import check_keys

    check_keys(body, {"claim_token", "fence", "request_id", "batch", "outcome", "error_code"})
    token, fence, request_id = body.get("claim_token"), body.get("fence"), body.get("request_id")
    outcome, error = body.get("outcome"), body.get("error_code")
    if (
        not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None
        or type(fence) is not int
        or fence < 1
        or not is_id(request_id)
        or not isinstance(outcome, str)
        or outcome not in {"completed", "failed", "blocked"}
        or (outcome == "completed" and error is not None)
        or (outcome != "completed" and (not isinstance(error, str) or error not in ERRORS))
    ):
        raise InvalidRequest("invalid history chunk")
    text = canonical_json(body)
    if len(text.encode()) > client._backend.limits.max_request_bytes:
        raise ProjectionLimit("history chunk too large")
    ops, context = decode_request(canonical_json(body.get("batch")))
    if len(ops) > client._backend.limits.max_batch_ops:
        raise ProjectionLimit("history operation limit")
    if outcome != "completed" and ops:
        raise InvalidRequest("terminal failure cannot mutate business")
    if context.request_event_id is not None:
        raise InvalidRequest("scheduler_integration_required")
    allowed = (AppendRaw, Create, Update, Supersede, RegisterDependencies, AssertTypeRevision)
    if any(not isinstance(op, allowed) for op in ops):
        raise InvalidRequest("history_operation_not_allowed")
    digest = hashlib.sha256(text.encode()).hexdigest()
    claim_hash = hashlib.sha256(token.encode()).hexdigest()
    with client._session() as (conn, principal, reader):
        job, identity = _job(conn, principal, job_id)
        item = conn.execute(
            "SELECT data_type,object_id,state,fence,principal_id,credential_epoch,"
            "lifecycle_revision,lease_until>clock_timestamp(),snapshot_id,current_version,"
            "guards_text FROM storage.history_items WHERE job_id=%s AND claim_hash=%s",
            (job_id, claim_hash),
        ).fetchone()
        if (
            item is None
            or item[3] != fence
            or item[4] != principal.id
            or (item[5], item[6]) != identity
        ):
            raise PermissionDenied("history_claim_unavailable")
        principal.require_read([item[0]])
        registry = registry_from_db(conn)
        required = _write_permissions(principal, reader, registry, ops, context)
        guards = json.loads(item[10])
        principal.require_read(guards)
        old = conn.execute(
            "SELECT fingerprint,result_text,required_types_text FROM "
            "storage.history_receipts WHERE job_id=%s AND request_id=%s",
            (job_id, request_id),
        ).fetchone()
        if old:
            if item[2] == "claimed" and not item[7]:
                raise PermissionDenied("history_claim_unavailable")
            principal.require_read(json.loads(old[2]))
            if old[0] != digest:
                raise InvalidRequest("history_request_id_reused")
            return json.loads(old[1])
        if item[2] != "claimed" or not item[7] or job[2] in TERMINAL:
            raise PermissionDenied("history_claim_unavailable")
        if context.root_feedback_id != job[4] or context.depth != 0:
            raise InvalidRequest("history_context_mismatch")

        def receipt(response):
            conn.execute(
                "INSERT INTO storage.history_receipts VALUES (%s,%s,%s,%s,%s)",
                (
                    job_id,
                    request_id,
                    digest,
                    canonical_json(response),
                    canonical_json(sorted(required | frozenset(guards))),
                ),
            )
            return response

        # These comparisons happen before business writes while holding the same gate.
        # They include empty queries and any newly inserted input, not just existing deps.
        if outcome == "completed":
            validate_lease(conn, principal, item[8])
            if any(reader.type_revision(t) != rev for t, rev in guards.items()):
                return receipt(
                    {"status": "conflict", "error_code": "stale_inputs", "advanced": False}
                )
            snapshot = load_reader(conn, principal, item[8], client._snapshot_limits)
            fresh = reader.get(item[1])
            if fresh is None or fresh.version != item[9]:
                return receipt(
                    {"status": "conflict", "error_code": "stale_inputs", "advanced": False}
                )
            created = {(op.data_type, op.id) for op in ops if isinstance(op, Create)}
            for op in ops:
                if isinstance(op, RegisterDependencies):
                    for ref in op.inputs():
                        if (ref.data_type, ref.id) in created:
                            continue
                        record = snapshot.authorized_read("get", ref.id)
                        if (
                            record is None
                            or record.data_type != ref.data_type
                            or record.version != ref.version
                        ):
                            raise InvalidRequest("history_input_not_in_snapshot")
                dtype = (
                    op.record.data_type
                    if isinstance(op, AppendRaw)
                    else getattr(op, "data_type", getattr(op, "dependent_type", None))
                )
                if not isinstance(dtype, str):
                    raise InvalidRequest("invalid_history_type")
                if (
                    not isinstance(op, AssertTypeRevision)
                    and registry.get(dtype).kind == "system_record"
                ):
                    raise InvalidRequest("history_business_operations_only")
        try:
            result = PostgresClient(client._backend, principal.module)._write_locked(
                conn,
                ops,
                context,
                request_id,
                digest,
                scope="history:" + job_id,
                required_types=required | frozenset(guards),
                authorize_replay=principal.require_read,
            )
        except PermissionDenied:
            raise PermissionDenied("history_write_denied") from None
        except InvalidRequest:
            raise InvalidRequest("invalid_history_write") from None
        if not result.ok:
            # Durable conflict receipt, never progress or business mutation.
            return receipt(
                {
                    "status": "conflict",
                    "error_code": result.conflict.reason if result.conflict else "write_conflict",
                    "advanced": False,
                }
            )
        if not one(
            conn,
            "SELECT lease_until>clock_timestamp() FROM storage.history_items "
            "WHERE job_id=%s AND claim_hash=%s",
            (job_id, claim_hash),
        )[0]:
            # Raising rolls back business, backend receipt AND chunk effects.
            raise PermissionDenied("history_claim_unavailable")
        conn.execute(
            "UPDATE storage.history_items SET state=%s,error_code=%s "
            "WHERE job_id=%s AND claim_hash=%s",
            (outcome, error, job_id, claim_hash),
        )
        state, counts = _finish_progress(conn, job_id, job)
        if error:
            conn.execute(
                "UPDATE storage.history_jobs SET error_code=%s WHERE job_id=%s", (error, job_id)
            )
        response = {
            "status": outcome,
            "job_state": state,
            "items": counts,
            "advanced": True,
            "error_code": error,
            "write_result": json.loads(encode_result(result)),
        }
        receipt(response)
        conn.execute("DELETE FROM storage.snapshot_leases WHERE snapshot_id=%s", (item[8],))
        return response
