"""Recoverable broker intents. Called only by the trusted management processor.

A consumer retains its owner, receipts and delivery IDs. New durables replay the
oldest still-needed stream sequence; DB per-type commit watermarks and original
consumer event identity exclude historical/new-type and already completed work.
Retired resources are retained (no destructive broker compensation). Hard limits
fail closed until deployment maintenance can safely retire them.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict

from ear_storage_client import PermissionDenied
from ear_storage_client.engine import canonical_json

from ear_storage.auth import Principal
from ear_storage.consumers import approved_filters
from ear_storage.management import (
    ManagementFailure,
    ResourcesPending,
    _interrupt,
    _permissions,
    _replace_permissions,
    _status,
)
from ear_storage.repository import registry_from_db
from ear_storage.schema import one

MAX_CONSUMERS = 64
MAX_GENERATIONS = 32


def identities(conn, module):
    """Fence every owner, grant and credential transition, including inactive ABA."""
    return [
        list(row)
        for row in conn.execute(
            "SELECT p.id,p.active,COALESCE(jsonb_agg(jsonb_build_array(g.data_type,"
            "g.can_subscribe) ORDER BY g.data_type) FILTER(WHERE g.data_type IS NOT NULL),"
            "'[]'::jsonb),p.credential_epoch FROM storage.principals p "
            "LEFT JOIN storage.type_grants g "
            "ON g.principal_id=p.id WHERE p.module=%s GROUP BY p.id,p.active ORDER BY p.id",
            (module,),
        ).fetchall()
    ]


def consumer_snapshot(conn, module):
    rows = conn.execute(
        "SELECT name,principal_id,generation,config_text,destination,enabled,ready,fence "
        "FROM storage.consumers WHERE module=%s ORDER BY name LIMIT %s",
        (module, MAX_CONSUMERS + 1),
    ).fetchall()
    if len(rows) > MAX_CONSUMERS:
        raise ResourcesPending("consumer_limit")
    return [list(row) for row in rows]


def withdraw(conn, body):
    """Commit restrictions even when later staging/preflight fails.

    Called outside the resource savepoint, under the writer gate. Expansion is
    never performed here. All validation preceding a withdrawal is DB-only.
    """
    state = _status(conn, body.module)
    reads, subs = _permissions(conn, body.module)
    if not set(body.read_types) <= set(registry_from_db(conn).data_types):
        raise ManagementFailure("unknown_permission_type")
    removed = (reads - set(body.read_types)) | (subs - set(body.subscribe_types))
    if not removed:
        return
    if state["ever_enabled"]:
        _interrupt(conn, body.module, removed)
    restricted = body.model_copy(
        update={
            "read_types": tuple(reads & set(body.read_types)),
            "subscribe_types": tuple(subs & set(body.subscribe_types)),
        }
    )
    _replace_permissions(conn, restricted)
    conn.execute(
        "UPDATE storage.module_lifecycle SET revision=revision+1,resources_ready=false "
        "WHERE module=%s",
        (body.module,),
    )
    conn.execute(
        "UPDATE storage.consumers SET ready=false,fence=fence+1,lease_until='-infinity' "
        "WHERE module=%s",
        (body.module,),
    )


def cancel(conn, feedback_id):
    """Retain resources and pending work, but release terminal stale intents."""
    row = conn.execute(
        "SELECT plan_text FROM storage.consumer_reconfigurations "
        "WHERE feedback_id=%s AND state='prepared'",
        (feedback_id,),
    ).fetchone()
    if row is None:
        return
    plan = json.loads(row[0])
    for resource in plan["resources"]:
        # Restore only the unchanged old consumer. Never undo a disable,
        # withdrawal, concurrent register or credential/lifecycle change.
        if resource.get("restore_ready"):
            snapshot = next(r for r in plan["consumers"] if r[0] == resource["name"])
            conn.execute(
                "UPDATE storage.consumers c SET ready=true,fence=fence+1 "
                "FROM storage.module_lifecycle m WHERE c.module=m.module "
                "AND m.enabled AND m.resources_ready AND m.revision=%s "
                "AND c.name=%s AND c.generation=%s AND c.fence=%s",
                (plan["revision"], resource["name"], resource["old_generation"], snapshot[7]),
            )
        conn.execute(
            "UPDATE storage.consumer_generations SET state='retired' "
            "WHERE generation=%s AND state='prepared'",
            (resource["generation"],),
        )
    conn.execute(
        "UPDATE storage.consumer_reconfigurations SET state='cancelled' WHERE feedback_id=%s",
        (feedback_id,),
    )


def prepare(processor, conn, feedback_id, body):
    old = conn.execute(
        "SELECT plan_text FROM storage.consumer_reconfigurations WHERE feedback_id=%s",
        (feedback_id,),
    ).fetchone()
    if old:
        return json.loads(old[0])
    if conn.execute(
        "SELECT 1 FROM storage.consumer_reconfigurations WHERE module=%s AND state='prepared'",
        (body.module,),
    ).fetchone():
        raise ResourcesPending("reconfiguration_in_progress")
    state = _status(conn, body.module)
    reads, subs = _permissions(conn, body.module)
    registry = registry_from_db(conn)
    if not set(body.read_types) <= set(registry.data_types):
        raise ManagementFailure("unknown_permission_type")
    recovery = set(
        json.loads(
            one(
                conn,
                "SELECT recovery_types_text FROM storage.module_lifecycle WHERE module=%s",
                (body.module,),
            )[0]
        )
    )
    added = (set(body.read_types) - reads) | (set(body.subscribe_types) - subs)
    if added & recovery:
        from ear_storage.history import decision

        approved = decision(conn, body)
        if approved is None:
            raise ManagementFailure("need_recovery_decision")
    rows = consumer_snapshot(conn, body.module)
    if rows and processor.consumers is None:
        raise ResourcesPending("broker_not_configured")
    conn.execute(
        "UPDATE storage.module_lifecycle SET revision=revision+1 WHERE module=%s",
        (body.module,),
    )
    resources = []
    for name, pid, generation, text, destination, _enabled, ready, _fence in rows:
        service = processor.consumers
        assert service is not None
        cfg = json.loads(text)
        policy = asdict(service.policy)
        policy.pop("history_modules")
        if destination != service.relay.destination or cfg["policy"] != policy:
            raise ResourcesPending("consumer_configuration_changed")
        count = one(
            conn, "SELECT count(*) FROM storage.consumer_generations WHERE consumer=%s", (name,)
        )[0]
        if count >= MAX_GENERATIONS:
            raise ResourcesPending("generation_limit")
        conn.execute(
            "INSERT INTO storage.consumer_generations VALUES (%s,%s,%s,%s,'active',NULL) "
            "ON CONFLICT(generation) DO NOTHING",
            (name, generation, text, destination),
        )
        old_types = {f.rsplit(".", 2)[1] for f in cfg["filters"]} - {"__none__"}
        for dtype in old_types:
            conn.execute(
                "INSERT INTO storage.consumer_type_cursors "
                "SELECT name,%s,creation_cursor FROM storage.consumers WHERE name=%s "
                "ON CONFLICT DO NOTHING",
                (dtype, name),
            )
        principal = Principal(
            pid, body.module, frozenset(body.read_types), frozenset(body.subscribe_types)
        )
        try:
            filters = approved_filters(
                principal, registry, cfg["event_types"], service.relay.prefix
            )
        except PermissionDenied:
            # An empty JetStream filter means ALL subjects; use an unregistered sentinel instead.
            filters = [service.relay.prefix + ".data_changed.__none__.*"]
        if filters == cfg["filters"] and ready:
            continue
        new_types = {f.rsplit(".", 2)[1] for f in filters} - {"__none__"}
        # Never use an ack floor as a DB commit cursor. Unrelayed old commits
        # enter at/after the current stream tail, still subject to the DB watermark.
        start = one(
            conn,
            "SELECT COALESCE(min(o.ack_seq), "
            "(SELECT COALESCE(max(ack_seq),0)+1 FROM storage.outbox WHERE ack_stream=%s)) "
            "FROM storage.outbox o JOIN storage.consumer_type_cursors t ON "
            "t.consumer=%s AND t.data_type=COALESCE(o.event_text::jsonb->>'data_type',"
            "o.event_text::jsonb->>'target_type') WHERE o.commit_seq>t.effective_commit_seq "
            "AND t.data_type=ANY(%s) AND o.ack_stream=%s "
            "AND NOT EXISTS(SELECT 1 FROM storage.processed p WHERE p.consumer=%s "
            "AND p.event_id=o.event_id) AND NOT EXISTS(SELECT 1 FROM storage.consumer_deliveries d "
            "WHERE d.consumer=%s AND d.event_id=o.event_id AND d.state IN ('acked','dead'))",
            (
                service.relay.stream,
                name,
                sorted(old_types & new_types),
                service.relay.stream,
                name,
                name,
            ),
        )[0]
        target = "c_" + secrets.token_hex(24)
        new_cfg = {**cfg, "filters": filters, "start_seq": start}
        conn.execute(
            "INSERT INTO storage.consumer_generations VALUES (%s,%s,%s,%s,'prepared',%s)",
            (name, target, canonical_json(new_cfg), destination, feedback_id),
        )
        conn.execute(
            "UPDATE storage.consumers SET ready=false,fence=fence+1,lease_until='-infinity' "
            "WHERE name=%s",
            (name,),
        )
        resources.append(
            dict(
                name=name,
                generation=target,
                config=new_cfg,
                added_types=sorted(new_types - old_types),
                old_generation=generation,
                restore_ready=ready,
            )
        )
    plan = dict(
        revision=state["revision"] + 1,
        identities=identities(conn, body.module),
        consumers=consumer_snapshot(conn, body.module),
        resources=resources,
    )
    conn.execute(
        "INSERT INTO storage.consumer_reconfigurations(feedback_id,module,plan_text) "
        "VALUES (%s,%s,%s)",
        (feedback_id, body.module, canonical_json(plan)),
    )
    return plan


def install(processor, plan):
    if not plan["resources"]:
        return
    service = processor.consumers
    assert service is not None
    service.broker.call("verify")
    for resource in plan["resources"]:
        cfg = resource["config"]
        service.broker.call(
            "create",
            durable=resource["generation"],
            filters=cfg["filters"],
            start_seq=cfg["start_seq"],
        )


def activate(processor, conn, feedback_id, body, plan):
    state = _status(conn, body.module)
    if (
        state["revision"] != plan["revision"]
        or identities(conn, body.module) != plan["identities"]
        or consumer_snapshot(conn, body.module) != plan["consumers"]
    ):
        raise ManagementFailure("resource_configuration_changed")
    # Resource readiness is not identity activation. An initially inactive owner
    # may finish configuration, but revoke/rotate during I/O changes the epoch
    # above and must abort, even if active remained false throughout. Only a new
    # authorized credential claim may reactivate it; module enable stays separate.
    from ear_storage.history import decision, switch

    approved = decision(conn, body)
    recovered = set(json.loads(approved[2])) if approved else set()
    seq = one(conn, "SELECT commit_seq FROM storage.meta WHERE singleton")[0]
    for resource in plan["resources"]:
        name = resource["name"]
        conn.execute(
            "UPDATE storage.consumer_generations SET state='retired' WHERE generation=%s",
            (resource["old_generation"],),
        )
        conn.execute(
            "UPDATE storage.consumer_generations SET state='active' WHERE generation=%s",
            (resource["generation"],),
        )
        for dtype in resource["added_types"]:
            conn.execute(
                "INSERT INTO storage.consumer_type_cursors VALUES (%s,%s,%s) "
                "ON CONFLICT(consumer,data_type) DO UPDATE SET "
                "effective_commit_seq=EXCLUDED.effective_commit_seq",
                (name, dtype, approved[3] if approved and dtype in recovered else seq),
            )
        conn.execute(
            "UPDATE storage.consumers SET generation=%s,config_text=%s,ready=true,"
            "fence=fence+1,lease_until='-infinity' WHERE name=%s",
            (resource["generation"], canonical_json(resource["config"]), name),
        )
    switch(conn, body)
    _replace_permissions(conn, body)
    # Unrelated recovery inputs are intentionally not cleared by granting a new type.
    conn.execute(
        "UPDATE storage.module_lifecycle SET resources_ready=true WHERE module=%s", (body.module,)
    )
    conn.execute(
        "UPDATE storage.consumer_reconfigurations SET state='active' WHERE feedback_id=%s",
        (feedback_id,),
    )
