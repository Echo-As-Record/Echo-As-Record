"""Storage-only scheduler bridge: approved scope, durable assignment, real completion."""

from __future__ import annotations

import json

from ear_contracts import (
    DependencyRecord,
    RawRecord,
    ReevaluationRequested,
    SemanticRecord,
    is_id,
    new_id,
)
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.engine import State, canonical_json, stale

from ear_storage.auth import authenticate
from ear_storage.causality import record_input_types
from ear_storage.codec import EVENT_ADAPTER
from ear_storage.history_execution import _job, _progress, _release_recovery
from ear_storage.reader import ConnectionBoundReader
from ear_storage.repository import registry_from_db
from ear_storage.scheduler_access import guard_graph
from ear_storage.schema import ProjectionLimit, one

MAX_PAGE = 100
MAX_REFS = 256


def _refs(reader, target, edges=None):
    group = reader.dependency_set(target.id, target.version) if edges is None else None
    required = set()
    refs = []
    if edges is None:
        edges = group.dependencies if group else ()
    if len(edges) > MAX_REFS:
        raise ProjectionLimit("history dependency limit")
    for edge in edges:
        ref = edge.depends_on
        required.add(ref.data_type)
        current = reader.get(ref.id)
        previous = reader.get_version(ref.id, ref.version)
        state = State()
        if isinstance(current, SemanticRecord):
            state.versions[ref.id] = (current,)
        elif isinstance(current, RawRecord):
            state.raw[ref.id] = current
        if isinstance(previous, RawRecord | SemanticRecord):
            state.prior[(ref.id, ref.version)] = previous
        if current is None or stale(state, ref, edge.fields):
            refs.append(
                dict(
                    data_type=ref.data_type,
                    data_id=ref.id,
                    old_version=ref.version,
                    new_version=current.version if current else ref.version,
                )
            )
    return refs, required


def capture_authority(conn, job_id):
    """Called at approval (and explicit migration), never from a public read/link."""
    if one(conn, "SELECT to_regclass('storage.history_reevaluation_authority')")[0] is None:
        return
    owner, types = one(
        conn, "SELECT owner,types_text FROM storage.history_jobs WHERE job_id=%s", (job_id,)
    )
    reader = ConnectionBoundReader(conn)
    for dtype, rid in conn.execute(
        "SELECT data_type,object_id FROM storage.history_items WHERE job_id=%s "
        "AND item_kind='reevaluation_needed'",
        (job_id,),
    ).fetchall():
        target = reader.get(rid)
        refs, required = _refs(reader, target) if target else ([], set())
        required.update(json.loads(types))
        required.update((dtype, owner + "_completion"))
        for (text,) in conn.execute(
            "SELECT o.event_text FROM storage.history_item_requests r JOIN storage.outbox o "
            "ON o.event_id=r.request_id WHERE r.job_id=%s AND r.object_id=%s",
            (job_id, rid),
        ).fetchall():
            event = EVENT_ADAPTER.validate_json(text)
            if isinstance(event, ReevaluationRequested):
                refs.extend(
                    c.model_dump()
                    for c in (event.cause, *(t.cause for t in event.additional_causes))
                )
        required.update(ref["data_type"] for ref in refs)
        if len(refs) > MAX_REFS:
            raise ProjectionLimit("history cause limit")
        conn.execute(
            "INSERT INTO storage.history_reevaluation_authority VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (job_id, dtype, rid, canonical_json(refs), canonical_json(sorted(required))),
        )


def _authority(conn, actor, job_id, *, final_read=False):
    """Reauthenticate executor, including revocation, under the existing writer gate."""
    row = conn.execute(
        "SELECT owner FROM storage.history_jobs WHERE job_id=%s", (job_id,)
    ).fetchone()
    if row is None or actor.module not in {row[0], "dependency"}:
        raise PermissionDenied("history_authority_required")
    identity = conn.execute(
        "SELECT token_hash FROM storage.principals WHERE module=%s AND active", (row[0],)
    ).fetchone()
    if identity is None:
        raise PermissionDenied("history_executor_unavailable")
    owner = authenticate(conn, identity[0])
    job, _ = _job(conn, owner, job_id, final_read=final_read)
    actor.require_read(json.loads(job[1]))
    rows = conn.execute(
        "SELECT data_type,object_id,refs_text,required_types_text "
        "FROM storage.history_reevaluation_authority WHERE job_id=%s ORDER BY object_id",
        (job_id,),
    ).fetchall()
    registry = registry_from_db(conn)
    for dtype, _rid, _refs_text, required in rows:
        actor.require_read(json.loads(required))
        owner.require_read(json.loads(required))
        spec = registry.get(dtype)
        completion = registry.data_types.get(owner.module + "_completion")
        if (
            spec.writer != owner.module
            or not spec.supports_reevaluation
            or not (
                completion and completion.writer == owner.module and completion.reevaluation_result
            )
        ):
            raise PermissionDenied("history_executor_unavailable")
    return job, owner, rows


def _latest(conn, job_id, rid):
    return conn.execute(
        "SELECT assignment_id,request_event_id,required_types_text "
        "FROM storage.history_request_links "
        "WHERE job_id=%s AND object_id=%s ORDER BY ordinal DESC LIMIT 1",
        (job_id, rid),
    ).fetchone()


def _result(conn, reader, actor, owner, event_id):
    result = reader.get_reevaluation_result(event_id)
    if result is not None:
        required = record_input_types(result, registry_from_db(conn))
        required |= frozenset(c.data_type for c in result.extra_causes)
        actor.require_read(required)
        owner.require_read(required)
    return result


def _frozen(conn, job):
    return job[2] == "completed" or (
        job[3] is not None
        and one(
            conn, "SELECT state FROM storage.recovery_decisions WHERE decision_id=%s", (job[3],)
        )[0]
        != "authorized"
    )


def _completion_fresh(conn, reader, actor, owner, result, current):
    # Bind evidence to the completion commit, not a later empty/rewritten dependency set.
    seq = one(
        conn,
        "SELECT commit_seq FROM storage.request_completions WHERE event_id=%s",
        (result.request_event_id,),
    )[0]
    historical = conn.execute(
        "SELECT commit_seq FROM storage.dependency_sets WHERE dependent_id=%s "
        "AND dependent_version=%s AND commit_seq<=%s ORDER BY commit_seq DESC LIMIT 1",
        (result.target_id, result.after_version, seq),
    ).fetchone()
    edges = [
        DependencyRecord.model_validate_json(row[0])
        for row in conn.execute(
            "SELECT record_text FROM storage.dependency_edges WHERE dependent_id=%s "
            "AND dependent_version=%s AND commit_seq=%s",
            (result.target_id, result.after_version, historical[0] if historical else -1),
        ).fetchall()
    ]
    event = EVENT_ADAPTER.validate_json(
        one(
            conn,
            "SELECT event_text FROM storage.outbox WHERE event_id=%s",
            (result.request_event_id,),
        )[0]
    )
    assert isinstance(event, ReevaluationRequested)
    # Primary/inherited trigger context is not necessarily a computation input.
    inherited_sources = {
        (c.data_type, c.data_id) for c in (event.cause, *(t.cause for t in event.additional_causes))
    }
    extras = [
        *result.extra_causes,
        *(
            t.cause
            for t in result.additional_causes
            if (t.cause.data_type, t.cause.data_id) not in inherited_sources
        ),
    ]
    covered = {(e.depends_on.data_type, e.depends_on.id) for e in edges}
    extras = [c for c in extras if (c.data_type, c.data_id) not in covered]
    required = {e.depends_on.data_type for e in edges} | {c.data_type for c in extras}
    actor.require_read(required)
    owner.require_read(required)
    if current is None or current.version != result.after_version:
        return False
    if not getattr(current, "superseded", False):
        old_stale, _ = _refs(reader, current, edges)
        now_stale, required = _refs(reader, current)
        actor.require_read(required)
        owner.require_read(required)
        if historical is None or old_stale or now_stale:
            return False
    for cause in extras:
        before = reader.get_version(cause.data_id, cause.new_version)
        now = reader.get(cause.data_id)
        # Same terminal version is valid iff that exact evidence was already terminal.
        if (
            before is None
            or now is None
            or before.data_type != cause.data_type
            or now.data_type != cause.data_type
            or now.version != before.version
            or getattr(now, "superseded", False) != getattr(before, "superseded", False)
        ):
            return False
    return True


def reconcile(conn, actor, reader, job_id):
    job, owner, rows = _authority(conn, actor, job_id, final_read=True)
    if _frozen(conn, job):
        return job, owner, rows
    for dtype, rid, _, _ in rows:
        link = _latest(conn, job_id, rid)
        if link is None:
            continue
        actor.require_read(json.loads(link[2]))
        owner.require_read(json.loads(link[2]))
        result = _result(conn, reader, actor, owner, link[1])
        state, error = "blocked", "waiting_result"
        if result is not None:
            if result.outcome == "failed":
                state, error = "failed", "business_failed"
            elif result.outcome == "not_applicable":
                error = "not_applicable_" + str(result.disposition)
            else:
                current = reader.get(rid)
                if not _completion_fresh(conn, reader, actor, owner, result, current):
                    error = "newrequest_needed"
                else:
                    state, error = "completed", None
        conn.execute(
            "UPDATE storage.history_items SET state=%s,error_code=%s WHERE job_id=%s "
            "AND data_type=%s AND object_id=%s",
            (state, error, job_id, dtype, rid),
        )
    if rows and any(_latest(conn, job_id, row[1]) for row in rows):
        state, _ = _progress(conn, job_id)
        error = conn.execute(
            "SELECT error_code FROM storage.history_items WHERE job_id=%s "
            "AND error_code IS NOT NULL "
            "ORDER BY object_id LIMIT 1",
            (job_id,),
        ).fetchone()
        conn.execute(
            "UPDATE storage.history_jobs SET error_code=%s WHERE job_id=%s",
            (error[0] if error else None, job_id),
        )
        if state == "completed" and job[3] is not None:
            _release_recovery(conn, job_id)
    return job, owner, rows


def list_reevaluations(client, job_id=None, *, limit=50, cursor=None):
    if (
        (job_id is not None and not is_id(job_id))
        or type(limit) is not int
        or not 1 <= limit <= MAX_PAGE
    ):
        raise InvalidRequest("invalid history query")
    key = ("", "")
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor.split(":")) != 2:
            raise InvalidRequest("invalid history cursor")
        parts = cursor.split(":")
        if not all(is_id(x) for x in parts):
            raise InvalidRequest("invalid history cursor")
        key = (parts[0], parts[1])
    with client._session() as (conn, actor, reader):
        guard_graph(conn, actor)
        if actor.module != "dependency":
            raise PermissionDenied("scheduler_required")
        if job_id is not None:
            _authority(conn, actor, job_id)
        candidates = conn.execute(
            "SELECT a.job_id,a.object_id FROM storage.history_reevaluation_authority a "
            "JOIN storage.history_jobs j USING(job_id) "
            "JOIN storage.module_lifecycle l ON l.module=j.owner "
            "JOIN storage.management_tasks t USING(feedback_id) "
            "WHERE (%s::text IS NULL OR a.job_id=%s) AND (a.job_id,a.object_id)>(%s,%s) "
            "AND l.enabled AND l.resources_ready AND j.execution_revision=l.revision "
            "AND t.status='succeeded' AND EXISTS (SELECT 1 FROM storage.principals p "
            "WHERE p.module=j.owner AND p.active) "
            "AND (j.decision_id IS NULL OR EXISTS (SELECT 1 FROM storage.recovery_decisions d "
            "WHERE d.decision_id=j.decision_id AND d.state IN ('authorized','switched'))) "
            "AND NOT EXISTS (SELECT 1 FROM storage.history_reevaluation_authority scope "
            "CROSS JOIN LATERAL jsonb_array_elements_text(scope.required_types_text::jsonb) rt "
            "WHERE scope.job_id=j.job_id AND NOT rt.value=ANY(%s::text[])) "
            "ORDER BY a.job_id,a.object_id LIMIT %s",
            (job_id, job_id, *key, sorted(actor.read_types), limit + 1),
        ).fetchall()
        items = []
        for jid, rid in candidates[:limit]:
            try:
                job, owner, rows = _authority(conn, actor, jid)
            except PermissionDenied:
                if job_id is not None:
                    raise
                continue
            reconcile(conn, actor, reader, jid)
            dtype, _, refs, _ = next(r for r in rows if r[1] == rid)
            target = reader.get(rid)
            current_refs, required = _refs(reader, target) if target else ([], set())
            actor.require_read(required)
            owner.require_read(required)
            link = _latest(conn, jid, rid)
            state, error = one(
                conn,
                "SELECT state,error_code FROM storage.history_items "
                "WHERE job_id=%s AND object_id=%s",
                (jid, rid),
            )
            items.append(
                dict(
                    job_id=jid,
                    item_id=rid,
                    target_type=dtype,
                    target_version=target.version if target else None,
                    owner=owner.module,
                    authority_revision=job[6],
                    state=state,
                    error_code=error,
                    candidate_inputs=json.loads(refs),
                    stale_inputs=current_refs,
                    assignment_id=link[0] if link else None,
                    request_event_id=link[1] if link else None,
                )
            )
            if len(canonical_json(items).encode()) > 4 * 1024 * 1024:
                raise ProjectionLimit("history metadata limit")
        return dict(
            items=items,
            next_cursor=(items[-1]["job_id"] + ":" + items[-1]["item_id"])
            if len(candidates) > limit and items
            else None,
        )


def attach(client, job_id, item_id, request_event_id):
    from ear_storage.history import excluded

    if not all(is_id(x) for x in (job_id, item_id, request_event_id)):
        raise InvalidRequest("invalid history assignment")
    with client._session() as (conn, actor, reader):
        guard_graph(conn, actor)
        if actor.module != "dependency":
            raise PermissionDenied("scheduler_required")
        job, owner, rows = _authority(conn, actor, job_id)
        if _frozen(conn, job):
            raise InvalidRequest("history_job_final")
        authority = next((r for r in rows if r[1] == item_id), None)
        if authority is None:
            raise PermissionDenied("history_item_unavailable")
        dtype, rid, declared, required_text = authority
        # Only this authenticated scheduler's durable Publish, never a claimed event body.
        published = conn.execute(
            "SELECT o.event_text,o.commit_seq FROM storage.outbox o JOIN storage.published p "
            "ON p.event_id=o.event_id JOIN storage.idempotency i ON i.commit_seq=o.commit_seq "
            "WHERE o.event_id=%s AND i.scope=%s AND i.module='dependency'",
            (request_event_id, "principal:" + actor.id),
        ).fetchone()
        boundary = one(
            conn, "SELECT recovery_boundary FROM storage.history_jobs WHERE job_id=%s", (job_id,)
        )[0]
        if (
            published is None
            or published[1] <= boundary
            or excluded(conn, owner.module, request_event_id)
        ):
            raise PermissionDenied("history_request_unavailable")
        request = EVENT_ADAPTER.validate_json(published[0])
        if not isinstance(request, ReevaluationRequested) or (
            request.target_type,
            request.target_id,
        ) != (dtype, rid):
            raise InvalidRequest("history_request_target_mismatch")
        if request.root_feedback_id == job_id:
            raise InvalidRequest("history_request_root_mismatch")
        required = set(json.loads(required_text))
        causes = (request.cause, *(t.cause for t in request.additional_causes))
        required.update(c.data_type for c in causes)
        actor.require_read(required)
        owner.require_read(required)
        old = _latest(conn, job_id, rid)
        if old and old[1] == request_event_id:
            reconcile(conn, actor, reader, job_id)
            return dict(assignment_id=old[0], request_event_id=request_event_id, status="attached")
        target = reader.get(rid)
        if target is None or target.data_type != dtype or getattr(target, "superseded", False):
            raise InvalidRequest("history_target_unavailable")
        current_refs, input_types = _refs(reader, target)
        required.update(input_types)
        actor.require_read(required)
        owner.require_read(required)
        result = _result(conn, reader, actor, owner, request_event_id)
        if result is not None:
            seq = one(
                conn,
                "SELECT commit_seq FROM storage.request_completions WHERE event_id=%s",
                (request_event_id,),
            )[0]
            if seq <= boundary:
                raise InvalidRequest("history_result_predates_authorization")
        refs = json.loads(declared) + (current_refs if result is None else [])
        if not refs or any(
            not any(
                c.data_type == ref["data_type"]
                and c.data_id == ref["data_id"]
                and c.new_version >= ref["new_version"]
                for c in causes
            )
            for ref in refs
        ):
            raise InvalidRequest("history_cause_coverage_required")
        # Typed refs are real versions even when the original Publish had only a primary cause.
        for cause in causes:
            for version in (cause.old_version, cause.new_version):
                if version is not None:
                    record = reader.get_version(cause.data_id, version)
                    if record is None or record.data_type != cause.data_type:
                        raise InvalidRequest("history_cause_unavailable")
        if old:
            prior = _result(conn, reader, actor, owner, old[1])
            error = one(
                conn,
                "SELECT error_code FROM storage.history_items WHERE job_id=%s AND object_id=%s",
                (job_id, rid),
            )[0]
            if prior is None or (prior.outcome != "failed" and error != "newrequest_needed"):
                raise InvalidRequest("history_assignment_conflict")
            if conn.execute(
                "SELECT 1 FROM storage.history_request_links WHERE job_id=%s "
                "AND object_id=%s AND request_event_id=%s",
                (job_id, rid, request_event_id),
            ).fetchone():
                raise InvalidRequest("history_assignment_replay")
        assignment = new_id()
        conn.execute(
            "INSERT INTO storage.history_request_links(assignment_id,job_id,data_type,object_id,"
            "request_event_id,principal_id,authority_revision,required_types_text) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                assignment,
                job_id,
                dtype,
                rid,
                request_event_id,
                actor.id,
                job[6],
                canonical_json(sorted(required)),
            ),
        )
        reconcile(conn, actor, reader, job_id)
        return dict(assignment_id=assignment, request_event_id=request_event_id, status="attached")


def linked_submission(conn, principal, event_id):
    """Narrow gate exception; caller still verifies actual consumer + Finish normally."""
    row = conn.execute(
        "SELECT l.job_id,l.object_id,l.required_types_text FROM storage.history_request_links l "
        "JOIN storage.history_jobs j USING(job_id) WHERE l.request_event_id=%s AND j.owner=%s "
        "AND NOT EXISTS (SELECT 1 FROM storage.history_request_links newer WHERE "
        "newer.job_id=l.job_id AND newer.object_id=l.object_id AND newer.ordinal>l.ordinal)",
        (event_id, principal.module),
    ).fetchone()
    if row is None:
        return False
    job, _, _ = _authority(conn, principal, row[0])
    if _frozen(conn, job):
        return False
    principal.require_read(json.loads(row[2]))
    return True


def claim_assignment(client, job_id, *, consumer):
    """At-least-once handoff of a linked request, never computation or generic event reads.

    The supplied name must already be registered or deployment-reserved to this
    credential's principal. Repeated claims return the same event/consumer; the
    completion index and same-batch MarkProcessed are the commit-time arbiters.
    """
    from ear_storage_client.consumer_wire import ConsumerRequest

    from ear_storage.consumers import approved_filters, authorize_event
    from ear_storage.history import excluded

    try:
        ConsumerRequest(consumer=consumer)
    except ValueError:
        raise InvalidRequest("invalid consumer") from None
    with client._session() as (conn, actor, reader):
        _job(conn, actor, job_id)  # Owner only, never the scheduler or a self-reported module.
        job, owner, rows = _authority(conn, actor, job_id)
        if _frozen(conn, job):
            raise InvalidRequest("history_job_final")
        registered = conn.execute(
            "SELECT principal_id,enabled,config_text FROM storage.consumers WHERE name=%s",
            (consumer,),
        ).fetchone()
        reserved = conn.execute(
            "SELECT principal_id FROM storage.consumer_identities WHERE name=%s",
            (consumer,),
        ).fetchone()
        if (
            not actor.owns_consumer(consumer)
            or (registered is not None and (registered[0] != actor.id or not registered[1]))
            or (reserved is not None and reserved[0] != actor.id)
            or (registered is None and reserved is None)
        ):
            raise PermissionDenied("history_consumer_unavailable")
        registry = registry_from_db(conn)
        cfg = (
            json.loads(registered[2])
            if registered
            else {
                "event_types": ["reevaluation_requested"],
                "filters": approved_filters(
                    actor, registry, ["reevaluation_requested"], "ear.events"
                ),
            }
        )
        prefix = cfg["filters"][0].rsplit(".", 3)[0]
        for dtype, rid, _, _ in rows:
            link = _latest(conn, job_id, rid)
            if link is None:
                continue
            actor.require_read(json.loads(link[2]))
            if excluded(conn, actor.module, link[1]):
                raise PermissionDenied("history_request_unavailable")
            if _result(conn, reader, actor, owner, link[1]) is not None:
                continue
            event = EVENT_ADAPTER.validate_json(
                one(conn, "SELECT event_text FROM storage.outbox WHERE event_id=%s", (link[1],))[0]
            )
            if not isinstance(event, ReevaluationRequested) or (
                event.target_type,
                event.target_id,
            ) != (dtype, rid):
                raise PermissionDenied("history_request_unavailable")
            authorize_event(actor, registry, cfg, event, prefix)
            target = reader.get(rid)
            if target is None or getattr(target, "superseded", False):
                raise InvalidRequest("history_target_unavailable")
            for cause in (event.cause, *(t.cause for t in event.additional_causes)):
                for version in (cause.old_version, cause.new_version):
                    if version is not None:
                        record = reader.get_version(cause.data_id, version)
                        if record is None or record.data_type != cause.data_type:
                            raise InvalidRequest("history_cause_unavailable")
                        actor.require_read(record_input_types(record, registry))
            actor.require_read(record_input_types(target, registry))
            return dict(
                status="assigned",
                assignment_id=link[0],
                consumer=consumer,
                event=event.model_dump(mode="json"),
            )
        return dict(status="idle", consumer=consumer, event=None)
