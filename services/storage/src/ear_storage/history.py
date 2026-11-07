"""Bounded history authorization and durable candidates, not event-body replay.

Execution is in history_execution; storage never computes plugin business or
publishes scheduler requests. Outbox has no cleanup; future retention must
advance coverage_lower_bound and reject incomplete recovery coverage.
"""

from __future__ import annotations

import json

from ear_contracts import DataChanged, is_id, new_id
from ear_storage_client import FinishReevaluation, InvalidRequest, MarkProcessed, PermissionDenied
from ear_storage_client.engine import canonical_json
from psycopg import sql

from ear_storage.auth import Principal
from ear_storage.codec import EVENT_ADAPTER
from ear_storage.schema import one, table

MAX_ITEMS = 10000
MAX_BYTES = 4 * 1024 * 1024


def excluded(conn, module, event_id, consumer=None):
    """Authoritative audit range, not a delivery-state shortcut or business ack."""
    # Genuine old-revision migration fixtures; a v11 public session checks all tables.
    if one(conn, "SELECT to_regclass('storage.recovery_exclusions')")[0] is None:
        return False
    if conn.execute(
        "SELECT 1 FROM storage.recovery_excluded_events x "
        "JOIN storage.recovery_decisions d USING(decision_id) "
        "WHERE d.module=%s "
        "AND (d.mode='skip' OR (d.mode='catch_up' AND d.state='switched')) AND x.event_id=%s "
        "AND (%s::text IS NULL OR x.consumer=%s) LIMIT 1",
        (module, event_id, consumer, consumer),
    ).fetchone():
        return True
    return bool(
        conn.execute(
            "SELECT 1 FROM storage.recovery_exclusions x "
            "JOIN storage.recovery_decisions d USING(decision_id) "
            "JOIN storage.outbox o ON o.commit_seq>x.lower_seq AND o.commit_seq<=x.upper_seq "
            "AND COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type')"
            "=x.data_type WHERE d.module=%s "
            "AND (d.mode='skip' OR (d.mode='catch_up' AND d.state='switched')) AND o.event_id=%s "
            "AND (%s::text IS NULL OR x.consumer=%s) LIMIT 1",
            (module, event_id, consumer, consumer),
        ).fetchone()
    )


def guard_submission(conn, principal, ops, context):
    events: list[tuple[str, str | None]] = [
        (op.event_id, op.consumer) for op in ops if isinstance(op, MarkProcessed)
    ]
    events.extend(
        (op.result.request_event_id, None) for op in ops if isinstance(op, FinishReevaluation)
    )
    if context.request_event_id is not None:
        events.append((context.request_event_id, None))
    if not events:
        # Direct business writes without event metadata remain normal authorized writes.
        # This is a trusted-plugin protocol, NOT an arbitrary-script sandbox.
        return
    pending = conn.execute(
        "SELECT recovery_pending FROM storage.module_lifecycle WHERE module=%s",
        (principal.module,),
    ).fetchone()
    if pending and pending[0]:
        from ear_storage.history_scheduler import linked_submission

        if any(not linked_submission(conn, principal, eid) for eid, _ in events):
            raise PermissionDenied("need_recovery_decision")
    for eid, consumer in events:
        if excluded(conn, principal.module, eid, consumer):
            raise PermissionDenied("event_excluded_by_recovery")


def _bounded(rows):
    from ear_storage.management import ManagementFailure

    if len(rows) > MAX_ITEMS or len(canonical_json(rows).encode()) > MAX_BYTES:
        raise ManagementFailure("history_input_limit")
    return rows


def _ranges(conn, module, types, watermark):
    from ear_storage.management import ManagementFailure

    rows = conn.execute(
        "SELECT c.name,t.data_type,COALESCE(tc.effective_commit_seq,c.creation_cursor),%s "
        "FROM storage.consumers c CROSS JOIN unnest(%s::text[]) AS t(data_type) "
        "LEFT JOIN storage.consumer_type_cursors tc ON tc.consumer=c.name "
        "AND tc.data_type=t.data_type WHERE c.module=%s AND "
        "(tc.data_type IS NOT NULL OR EXISTS (SELECT 1 FROM "
        "jsonb_array_elements_text(c.config_text::jsonb->'filters') f "
        "WHERE split_part(f.value,'.',array_length(string_to_array(f.value,'.'),1)-1)"
        "=t.data_type)) LIMIT %s",
        (watermark, types, module, MAX_ITEMS + 1),
    ).fetchall()
    if any(r[2] > watermark for r in rows):
        raise ManagementFailure("invalid_history_boundary")
    return _bounded(rows)


def _events(conn, module, types, watermark):
    # Include pre-disable backlog, retries, inflight, unfetched AND unrelayed rows.
    # A request enqueue marker/transport ack is NOT authoritative completion.
    query = sql.SQL(
        "SELECT DISTINCT o.event_text FROM storage.outbox o "
        "JOIN storage.consumers c ON c.module=%s "
        "LEFT JOIN storage.consumer_type_cursors tc ON tc.consumer=c.name AND tc.data_type="
        "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type') "
        "WHERE o.commit_seq>COALESCE(tc.effective_commit_seq,c.creation_cursor) "
        "AND o.commit_seq<=%s AND "
        "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type')=ANY(%s) "
        "AND (tc.data_type IS NOT NULL OR EXISTS (SELECT 1 FROM "
        "jsonb_array_elements_text(c.config_text::jsonb->'filters') f WHERE "
        "split_part(f.value,'.',array_length(string_to_array(f.value,'.'),1)-1)="
        "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type'))) "
        "AND (CASE WHEN o.event_text::jsonb->>'event_type'='reevaluation_requested' "
        "THEN NOT EXISTS(SELECT 1 FROM storage.request_completions r WHERE r.event_id=o.event_id) "
        "ELSE NOT EXISTS(SELECT 1 FROM storage.processed p WHERE p.consumer=c.name "
        "AND p.event_id=o.event_id) AND NOT EXISTS(SELECT 1 FROM storage.consumer_deliveries d "
        "WHERE d.consumer=c.name AND d.event_id=o.event_id AND d.state='acked') END) "
        "LIMIT %s"
    )
    params = (module, watermark, types, MAX_ITEMS + 1)
    count, size = one(
        conn,
        sql.SQL("SELECT count(*),COALESCE(sum(octet_length(event_text)),0) FROM ({}) q").format(
            query
        ),
        params,
    )
    from ear_storage.management import ManagementFailure

    if count > MAX_ITEMS or size > MAX_BYTES:
        raise ManagementFailure("history_input_limit")
    rows = conn.execute(query, params).fetchall()
    return [EVENT_ADAPTER.validate_json(r[0]) for r in _bounded(rows)]


def _indirect_events(conn, module, types, watermark):
    """A revoked read-only origin can block requests for another subscribed type.

    Record exact affected events, never advance that unrelated target's cursor.
    Completed/acked events may be excluded from transport but never gain a fake
    business completion. Work planning separately checks the authoritative index.
    """
    query = sql.SQL(
        "SELECT c.name,o.event_text FROM storage.outbox o "
        "JOIN storage.consumers c ON c.module=%s "
        "LEFT JOIN storage.consumer_type_cursors tc ON tc.consumer=c.name AND tc.data_type="
        "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type') "
        "WHERE o.commit_seq>COALESCE(tc.effective_commit_seq,c.creation_cursor) "
        "AND o.commit_seq<=%s AND NOT "
        "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type')=ANY(%s) "
        "AND (o.event_text::jsonb->'cause'->>'data_type'=ANY(%s) OR EXISTS (SELECT 1 FROM "
        "jsonb_array_elements(COALESCE(o.event_text::jsonb->'additional_causes','[]'::jsonb)) t "
        "WHERE t->'cause'->>'data_type'=ANY(%s))) "
        "AND (tc.data_type IS NOT NULL OR EXISTS (SELECT 1 FROM "
        "jsonb_array_elements_text(c.config_text::jsonb->'filters') f WHERE "
        "split_part(f.value,'.',array_length(string_to_array(f.value,'.'),1)-1)="
        "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type'))) "
        "LIMIT %s"
    )
    params = (module, watermark, types, types, types, MAX_ITEMS + 1)
    count, size = one(
        conn,
        sql.SQL("SELECT count(*),COALESCE(sum(octet_length(event_text)),0) FROM ({}) q").format(
            query
        ),
        params,
    )
    from ear_storage.management import ManagementFailure

    if count > MAX_ITEMS or size > MAX_BYTES:
        raise ManagementFailure("history_input_limit")
    return _bounded(conn.execute(query, params).fetchall())


def _objects(conn, types, start, end):
    from ear_storage.management import ManagementFailure
    from ear_storage.repository import registry_from_db

    registry = registry_from_db(conn)
    rows = []
    for dtype in types:
        spec = registry.get(dtype)
        if spec.kind == "system_record":
            raise ManagementFailure("history_business_inputs_required")
        query = sql.SQL(
            "SELECT r.data_type,r.id,r.superseded FROM {} r JOIN storage.identity i "
            "ON i.id=r.id AND i.current_seq=r.seq WHERE r.data_type=%s "
            "AND ((r.event_start<%s AND r.event_end>%s) OR "
            "(r.event_start=r.event_end AND r.event_start>=%s AND r.event_start<%s)) LIMIT %s"
        ).format(table(spec.writer, "raw_records" if spec.kind == "raw_record" else "versions"))
        rows.extend(conn.execute(query, (dtype, end, start, start, end, MAX_ITEMS + 1)).fetchall())
        _bounded(rows)
    return rows


def _stale_owned(conn, module, types, allowed):
    """Bounded current effective edges; no other owner's metadata or payload."""
    from ear_contracts import DependencyRecord, RawRecord, SemanticRecord
    from ear_storage_client.engine import State, stale

    from ear_storage.management import ManagementFailure
    from ear_storage.reader import get_record

    rows = conn.execute(
        "SELECT e.record_text FROM storage.current_dependency_edges e "
        "JOIN storage.identity i ON i.id=e.dependent_id "
        "AND i.current_seq=e.dependent_version JOIN storage.identity t ON t.id=e.target_id "
        "WHERE i.module=%s AND t.data_type=ANY(%s) LIMIT %s",
        (module, list(types), MAX_ITEMS + 1),
    ).fetchall()
    _bounded(rows)
    items = {}
    for (text,) in rows:
        edge = DependencyRecord.model_validate_json(text)
        dtype, rid = edge.dependent.data_type, edge.dependent.id
        if dtype not in allowed:
            raise ManagementFailure("history_input_not_authorized")
        target = get_record(conn, rid)
        if not isinstance(target, SemanticRecord) or target.superseded:
            continue
        inputs = conn.execute(
            "SELECT record_text FROM storage.current_dependency_edges "
            "WHERE dependent_id=%s AND dependent_version=%s LIMIT %s",
            (rid, edge.dependent.version, MAX_ITEMS + 1),
        ).fetchall()
        _bounded(inputs)
        if any(
            DependencyRecord.model_validate_json(t).depends_on.data_type not in allowed
            for (t,) in inputs
        ):
            items[(dtype, rid)] = "unavailable"
            continue
        ref = edge.depends_on
        state = State()
        current = get_record(conn, ref.id)
        previous = get_record(conn, ref.id, ref.version)
        if isinstance(current, RawRecord):
            state.raw[ref.id] = current
        elif isinstance(current, SemanticRecord):
            state.versions[ref.id] = (current,)
        if isinstance(previous, RawRecord | SemanticRecord):
            state.prior[(ref.id, ref.version)] = previous
        if stale(state, ref, edge.fields):
            items.setdefault((dtype, rid), "candidate")
    return [(dtype, rid, state) for (dtype, rid), state in items.items()]


def _save_job(
    conn,
    feedback_id,
    module,
    mode,
    types,
    watermark,
    decision=None,
    start=None,
    end=None,
    events=(),
    recovery_types=(),
    allowed=(),
):
    from ear_storage.repository import registry_from_db

    job_id = new_id()
    items = {}
    requests = []
    if mode == "history_backfill":
        for dtype, rid, terminal in _objects(conn, types, start, end):
            items[(dtype, rid)] = ["current_state", "unavailable" if terminal else "candidate"]
    else:
        registry = registry_from_db(conn)
        for event in events:
            if isinstance(event, DataChanged):
                dtype, rid = event.data_type, event.data_id
                kind = "current_state"
            else:
                dtype, rid = event.target_type, event.target_id
                if registry.get(dtype).writer != module:
                    continue
                kind = "reevaluation_needed"
                requests.append((dtype, rid, event.event_id))
            spec = registry.get(dtype)
            terminal = conn.execute(
                sql.SQL(
                    "SELECT r.superseded FROM {} r JOIN storage.identity i "
                    "ON i.id=r.id AND i.current_seq=r.seq WHERE r.id=%s"
                ).format(
                    table(spec.writer, "raw_records" if spec.kind == "raw_record" else "versions")
                ),
                (rid,),
            ).fetchone()
            key = (dtype, rid)
            if key not in items or kind == "reevaluation_needed":
                items[key] = [
                    kind,
                    "unavailable" if terminal is None or terminal[0] else "candidate",
                ]
    if mode == "catch_up":
        for dtype, rid, state in _stale_owned(conn, module, recovery_types, allowed):
            items[(dtype, rid)] = ["reevaluation_needed", state]
        types = sorted(set(types) | {dtype for dtype, _ in items})
    _bounded([list(k) + v for k, v in items.items()] + [list(r) for r in requests])
    conn.execute(
        "INSERT INTO storage.history_jobs(job_id,feedback_id,owner,mode,types_text,range_start,"
        "range_end,recovery_boundary,decision_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (job_id, feedback_id, module, mode, canonical_json(types), start, end, watermark, decision),
    )
    for (dtype, rid), (kind, state) in items.items():
        conn.execute(
            "INSERT INTO storage.history_items(job_id,data_type,object_id,item_kind,state) "
            "VALUES (%s,%s,%s,%s,%s)",
            (job_id, dtype, rid, kind, state),
        )
    for dtype, rid, eid in requests:
        conn.execute(
            "INSERT INTO storage.history_item_requests VALUES (%s,%s,%s,%s)",
            (job_id, dtype, rid, eid),
        )
    from ear_storage.history_scheduler import capture_authority

    capture_authority(conn, job_id)
    return job_id


def authorize(conn, feedback_id, body):
    """Persist once, before broker preflight/savepoints; retries retain W and job."""
    from ear_storage.management import ManagementFailure, _permissions, _status

    state = _status(conn, body.module)
    reads, _subs = _permissions(conn, body.module)
    if body.action == "history_backfill":
        if not set(body.input_types) <= reads:
            raise ManagementFailure("history_input_not_authorized")
        old = conn.execute(
            "SELECT job_id FROM storage.history_jobs WHERE feedback_id=%s", (feedback_id,)
        ).fetchone()
        if old:
            return old[0]
        watermark = one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]
        return _save_job(
            conn,
            feedback_id,
            body.module,
            body.action,
            sorted(set(body.input_types)),
            watermark,
            start=body.range_start,
            end=body.range_end,
        )
    if body.resume_choice is None:
        return None
    revision_matches = body.expected_revision == state["revision"]
    # A staged reconfiguration increments revision once. Only its exact persisted
    # intent may resume across that increment; a new disable invalidates it.
    staged = conn.execute(
        "SELECT plan_text FROM storage.consumer_reconfigurations "
        "WHERE feedback_id=%s AND state='prepared'",
        (feedback_id,),
    ).fetchone()
    if staged and body.expected_revision is not None:
        revision_matches = (
            json.loads(staged[0])["revision"] == state["revision"] == body.expected_revision + 1
        )
    if not revision_matches or not state["need_recovery_decision"]:
        raise ManagementFailure("stale_recovery_revision")
    recovery = set(
        json.loads(
            one(
                conn,
                "SELECT recovery_types_text FROM storage.module_lifecycle WHERE module=%s",
                (body.module,),
            )[0]
        )
    )
    allowed = set(body.read_types) if body.action == "set_permissions" else reads
    types = sorted(recovery & allowed)
    if not types and recovery:
        raise ManagementFailure("recovery_inputs_not_authorized")
    old = conn.execute(
        "SELECT decision_id,state FROM storage.recovery_decisions WHERE feedback_id=%s",
        (feedback_id,),
    ).fetchone()
    if old:
        if old[1] != "authorized":
            raise ManagementFailure("stale_recovery_revision")
        return old[0]
    active = conn.execute(
        "SELECT d.decision_id,d.types_text,j.state,j.execution_revision "
        "FROM storage.recovery_decisions d LEFT JOIN storage.history_jobs j USING(decision_id) "
        "WHERE d.module=%s AND d.state='authorized' LIMIT %s",
        (body.module, MAX_ITEMS + 1),
    ).fetchall()
    _bounded(active)
    for did, prior_types, job_state, execution_revision in active:
        if not set(json.loads(prior_types)) <= set(types):
            raise ManagementFailure("recovery_in_progress")
        if (
            body.resume_choice != "skip"
            and job_state not in {"failed", "blocked"}
            and execution_revision in (None, state["revision"])
        ):
            raise ManagementFailure("recovery_in_progress")
        # A NEW administrator choice explicitly replaces the previous scope.
        # Same feedback retries above retain the original boundary and plan.
        conn.execute(
            "UPDATE storage.recovery_decisions SET state='superseded' WHERE decision_id=%s", (did,)
        )
        conn.execute(
            "UPDATE storage.history_jobs SET state='blocked', "
            "error_code='recovery_superseded' WHERE decision_id=%s",
            (did,),
        )
        conn.execute(
            "UPDATE storage.history_items SET fence=fence+1,claim_hash=NULL, "
            "lease_until='-infinity' WHERE job_id IN "
            "(SELECT job_id FROM storage.history_jobs WHERE decision_id=%s)",
            (did,),
        )
    watermark = one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]
    ranges = _ranges(conn, body.module, types, watermark)
    events = (
        _events(conn, body.module, types, watermark) if body.resume_choice == "catch_up" else []
    )
    indirect = _indirect_events(conn, body.module, types, watermark)
    if body.resume_choice == "catch_up":
        by_id = {e.event_id: e for e in events}
        for consumer, text in indirect:
            event = EVENT_ADAPTER.validate_json(text)
            if isinstance(event, DataChanged):
                done = conn.execute(
                    "SELECT 1 FROM storage.processed WHERE consumer=%s AND event_id=%s "
                    "UNION ALL SELECT 1 FROM storage.consumer_deliveries "
                    "WHERE consumer=%s AND event_id=%s AND state='acked' LIMIT 1",
                    (consumer, event.event_id, consumer, event.event_id),
                ).fetchone()
            else:
                done = conn.execute(
                    "SELECT 1 FROM storage.request_completions WHERE event_id=%s",
                    (event.event_id,),
                ).fetchone()
            if not done:
                by_id[event.event_id] = event
        events = list(by_id.values())
        _bounded([e.model_dump(mode="json") for e in events])
    # Input provenance still requires read permission; skip may discard unreadable events
    # without disclosing their content to the plugin.
    principal = Principal("", body.module, frozenset(allowed), frozenset(types))
    job_types = set(types)
    for event in events:
        event_type = event.data_type if isinstance(event, DataChanged) else event.target_type
        job_types.add(event_type)
        job_types.update(t.cause.data_type for t in event.additional_causes)
        principal.require_read([event_type])
        principal.require_read(t.cause.data_type for t in event.additional_causes)
        if not isinstance(event, DataChanged):
            principal.require_read([event.cause.data_type])
            job_types.add(event.cause.data_type)
    did = new_id()
    conn.execute(
        "INSERT INTO storage.recovery_decisions(decision_id,feedback_id,module,lifecycle_revision,"
        "mode,types_text,watermark,state) VALUES (%s,%s,%s,%s,%s,%s,%s,'authorized')",
        (
            did,
            feedback_id,
            body.module,
            state["revision"],
            body.resume_choice,
            canonical_json(types),
            watermark,
        ),
    )
    if body.resume_choice == "catch_up":
        for name, dtype, lower, upper in ranges:
            conn.execute(
                "INSERT INTO storage.recovery_exclusions VALUES (%s,%s,%s,%s,%s)",
                (did, name, dtype, lower, upper),
            )
        for consumer, text in indirect:
            event = EVENT_ADAPTER.validate_json(text)
            conn.execute(
                "INSERT INTO storage.recovery_excluded_events VALUES (%s,%s,%s)",
                (did, consumer, event.event_id),
            )
        _save_job(
            conn,
            feedback_id,
            body.module,
            "catch_up",
            sorted(job_types),
            watermark,
            did,
            events=events,
            recovery_types=types,
            allowed=allowed,
        )
    else:
        for consumer, text in indirect:
            event = EVENT_ADAPTER.validate_json(text)
            conn.execute(
                "INSERT INTO storage.recovery_excluded_events VALUES (%s,%s,%s)",
                (did, consumer, event.event_id),
            )
        conn.execute(
            "UPDATE storage.consumer_deliveries d SET state='skipped',token=NULL,"
            "lease_until='-infinity',error_code='recovery_skip',"
            "ack_pending=(d.broker_reply IS NOT NULL) "
            "FROM storage.recovery_excluded_events x WHERE x.decision_id=%s "
            "AND d.consumer=x.consumer AND d.event_id=x.event_id "
            "AND d.state IN ('retry','inflight','dead')",
            (did,),
        )
        for name, dtype, lower, upper in ranges:
            conn.execute(
                "INSERT INTO storage.recovery_exclusions VALUES (%s,%s,%s,%s,%s)",
                (did, name, dtype, lower, upper),
            )
        conn.execute(
            "UPDATE storage.consumer_deliveries d SET state='skipped',token=NULL,"
            "lease_until='-infinity',error_code='recovery_skip',"
            "ack_pending=(d.broker_reply IS NOT NULL) "
            "FROM storage.recovery_exclusions x,storage.outbox o WHERE x.decision_id=%s "
            "AND d.consumer=x.consumer AND d.event_id=o.event_id "
            "AND o.commit_seq>x.lower_seq AND o.commit_seq<=x.upper_seq AND "
            "COALESCE(o.event_text::jsonb->>'data_type',o.event_text::jsonb->>'target_type')"
            "=x.data_type AND d.state IN ('retry','inflight','dead')",
            (did,),
        )
        conn.execute(
            "UPDATE storage.consumers SET fence=fence+1,lease_until='-infinity' WHERE module=%s",
            (body.module,),
        )
    return did


def decision(conn, body):
    return conn.execute(
        "SELECT decision_id,mode,types_text,watermark FROM storage.recovery_decisions "
        "WHERE module=%s AND lifecycle_revision=%s AND state='authorized'",
        (body.module, body.expected_revision),
    ).fetchone()


def switch(conn, body):
    """Called only after resource readiness; selected types only, no false completion."""
    row = decision(conn, body)
    if row is None:
        return
    if (
        row[1] == "catch_up"
        and not conn.execute(
            "SELECT 1 FROM storage.history_jobs WHERE decision_id=%s AND state='completed'",
            (row[0],),
        ).fetchone()
    ):
        return
    # Also fail closed for overlapping decisions already present before v13.
    # No cursor or delivery may advance while another recovery still needs it.
    if conn.execute(
        "SELECT 1 FROM storage.recovery_decisions WHERE module=%s "
        "AND decision_id<>%s AND state='authorized' LIMIT 1",
        (body.module, row[0]),
    ).fetchone():
        return False
    for name, dtype, upper in conn.execute(
        "SELECT consumer,data_type,upper_seq FROM storage.recovery_exclusions WHERE decision_id=%s",
        (row[0],),
    ).fetchall():
        conn.execute(
            "INSERT INTO storage.consumer_type_cursors VALUES (%s,%s,%s) "
            "ON CONFLICT(consumer,data_type) DO UPDATE SET effective_commit_seq="
            "greatest(storage.consumer_type_cursors.effective_commit_seq,EXCLUDED.effective_commit_seq)",
            (name, dtype, upper),
        )
    for dtype in json.loads(row[2]):
        conn.execute(
            "INSERT INTO storage.module_type_handoffs VALUES (%s,%s,%s,%s) "
            "ON CONFLICT(module,data_type) DO UPDATE SET watermark=EXCLUDED.watermark, "
            "decision_id=EXCLUDED.decision_id",
            (body.module, dtype, row[3], row[0]),
        )
    old = json.loads(
        one(
            conn,
            "SELECT recovery_types_text FROM storage.module_lifecycle WHERE module=%s",
            (body.module,),
        )[0]
    )
    remaining = sorted(set(old) - set(json.loads(row[2])))
    conn.execute(
        "UPDATE storage.module_lifecycle SET recovery_types_text=%s,recovery_pending=%s,"
        "interrupted_seq=CASE WHEN %s THEN interrupted_seq ELSE NULL END WHERE module=%s",
        (canonical_json(remaining), bool(remaining), bool(remaining), body.module),
    )
    conn.execute(
        "UPDATE storage.recovery_decisions SET state='switched' WHERE decision_id=%s", (row[0],)
    )
    return True


def job_status(client, job_id):
    if not is_id(job_id):
        raise InvalidRequest("invalid job id")
    with client._session() as (conn, principal, _reader):
        if (
            principal.module != "api"
            and conn.execute(
                "SELECT 1 FROM storage.history_request_links WHERE job_id=%s LIMIT 1", (job_id,)
            ).fetchone()
        ):
            from ear_storage.history_scheduler import reconcile

            reconcile(conn, principal, _reader, job_id)
        row = conn.execute(
            "SELECT owner,mode,types_text,range_start,range_end,"
            "recovery_boundary,state,feedback_id "
            "FROM storage.history_jobs WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        if row is None or not (
            principal.module in {row[0], "dependency"}
            or (principal.module == "api" and principal.admin)
        ):
            raise PermissionDenied("access denied")
        if principal.module == "dependency":
            from ear_storage.history_scheduler import _authority

            _authority(conn, principal, job_id, final_read=True)
        if principal.module != "api":
            principal.require_read(json.loads(row[2]))
        counts = dict(
            conn.execute(
                "SELECT state,count(*) FROM storage.history_items WHERE job_id=%s GROUP BY state",
                (job_id,),
            ).fetchall()
        )
        return dict(
            job_id=job_id,
            owner=row[0],
            mode=row[1],
            input_types=json.loads(row[2]),
            rangeStart=row[3].isoformat().replace("+00:00", "Z") if row[3] else None,
            rangeEnd=row[4].isoformat().replace("+00:00", "Z") if row[4] else None,
            recovery_boundary=row[5],
            state=row[6],
            feedback_id=row[7],
            items=counts,
            execution_available=True,
            requires_scheduler=bool(
                conn.execute(
                    "SELECT 1 FROM storage.history_items WHERE job_id=%s "
                    "AND item_kind='reevaluation_needed' LIMIT 1",
                    (job_id,),
                ).fetchone()
            ),
            error_code=one(
                conn, "SELECT error_code FROM storage.history_jobs WHERE job_id=%s", (job_id,)
            )[0],
        )
