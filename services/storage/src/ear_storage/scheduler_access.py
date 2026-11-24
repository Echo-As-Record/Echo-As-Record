"""Opt-in full-graph boundary; caller holds WRITER_GATE. Never grants rights.

Legacy registries keep filtered graph/Publish semantics (not a coverage proof).
A complete trusted-bootstrap MVP core profile selects strict mode; a mixed
complete candidate fails closed instead of falling back to legacy. This applies to
ALL dependency identities in that database, regardless of their read grants.
Bootstrap persists immutable specs and is_core; ordinary plugin registration
cannot create core provenance, change core capabilities or switch this mode.
No request flags, default-registry mutation, auto-grants or online inference.

The proof concerns registered graph scope, not data freshness or recovered
history. Profile, registration and grants all participate in snapshot fencing.
Existing default databases cannot upgrade capabilities through bootstrap: that
requires an explicitly reviewed migration, outside this compatibility slice.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from typing import Any

import psycopg
from ear_contracts import (
    DEFAULT_REGISTRY,
    DataTypeSpec,
    ReevaluationRequested,
    Registry,
    new_id,
    utcnow,
)
from ear_storage_client import Context, InvalidRequest, PermissionDenied, PublishReevaluation
from ear_storage_client.engine import canonical_json

from ear_storage.auth import AuthenticationError, Principal, authenticate
from ear_storage.reader import ConnectionBoundReader
from ear_storage.repository import registry_from_db

# Control-plane feedback is raw but is not a scheduling input. No body grants.
EXCLUDED = frozenset({"plugin_management_feedback", "plugin_management_result", "llm_call"})
REQUIRED = frozenset({"propagation_record", "attribution_feedback_result"})
GRAPH_READS = frozenset({"dependency_set", "dependents_of", "stale_dependencies"})


# This bundle, not a type name or a caller-supplied flag, is deployment opt-in.
# Attribution-only and extraction-only legacy profiles do not select strict.
PROFILE = "mvp-core-scheduler-v1"
PROFILE_TYPES = (
    DataTypeSpec("attribution_state", "system_record", "attribution"),
    DataTypeSpec("attribution_feedback_result", "system_record", "attribution"),
    DataTypeSpec(
        "rule_selection",
        "semantic",
        "extraction",
        unique_fields=("activity_id",),
        requires_dependencies=True,
        supports_reevaluation=True,
    ),
)
_LOG = logging.getLogger(__name__)


def strict_profile(registry: Registry) -> str | None:
    """Return loaded core fingerprint, or None for a genuine legacy profile.

    Persisted core provenance and the complete owner/spec bundle distinguish
    candidates from genuine legacy. Missing provenance or inconsistent specs
    in a candidate are errors, NOT a downgrade. Extensions do not weaken it.
    Read the live database registry, never the caller's filtered registry view.
    """
    declared = {s.name for s in PROFILE_TYPES} <= registry.core_types
    present = all(s.name in registry.data_types for s in PROFILE_TYPES)
    # Recognize a complete candidate BEFORE validating it. Requiring every
    # is_core bit here would turn one missing provenance bit into a downgrade.
    # Names alone are not opt-in: genuine single-module profiles may coexist
    # with plugins occupying other names. Use persisted core/owner evidence.
    owned_slots = present and all(
        s.name in registry.core_types or registry.get(s.name).writer == s.writer
        for s in PROFILE_TYPES
    )
    core_slot = any(s.name in registry.core_types for s in PROFILE_TYPES)
    # A full MVP specification with *all* new provenance bits missing is also
    # suspicious. Both trusted old core modules must advertise upgraded
    # capabilities; DEFAULT + untrusted additions remains genuine legacy.
    upgraded_core = all(
        name in registry.core_types
        and name in registry.data_types
        and registry.get(name).writer == DEFAULT_REGISTRY.get(name).writer
        and (registry.get(name).requires_dependencies or registry.get(name).supports_reevaluation)
        for name in ("attribution", "artifact")
    )
    if not (declared or (owned_slots and (upgraded_core or core_slot))):
        return None
    expected = (
        *PROFILE_TYPES,
        *(
            replace(
                DEFAULT_REGISTRY.get(name),
                supports_reevaluation=True,
                requires_dependencies=True,
            )
            for name in ("attribution", "artifact")
        ),
        *(
            DEFAULT_REGISTRY.get(name)
            for name in ("attribution_completion", "extraction_completion", "propagation_record")
        ),
    )
    for spec in expected:
        actual = registry.data_types.get(spec.name)
        # Human descriptions are not capabilities. Every other spec field matters.
        if (
            spec.name not in registry.core_types
            or actual is None
            or replace(actual, description="") != replace(spec, description="")
        ):
            raise PermissionDenied("access denied")
    if registry.publishers_of("reevaluation_requested") != ("dependency",):
        raise PermissionDenied("access denied")
    fingerprint = hashlib.sha256(
        canonical_json(
            {
                "profile": PROFILE,
                "core": {n: registry.get(n).to_json() for n in sorted(registry.core_types)},
                "publishers": registry.to_json()["event_publishers"],
            }
        ).encode()
    ).hexdigest()
    _LOG.debug(
        canonical_json(
            {
                "module": "storage",
                "event": "scheduler_profile_loaded",
                "profile": PROFILE,
                "profile_fingerprint": fingerprint,
            }
        )
    )
    return fingerprint


def strict_scheduler(conn: psycopg.Connection, principal: Principal) -> bool:
    """Server-side mode selection, independent of this identity's grant subset."""
    return principal.module == "dependency" and strict_profile(registry_from_db(conn)) is not None


def require_coverage(conn: psycopg.Connection, principal: Principal) -> tuple[Registry, str]:
    """Recompute from current trusted registration and effective explicit grants.

    No partial graph, type list or missing-permission diagnostics on failure.
    This is intentionally independent of registry visibility filtering.
    """
    registry = registry_from_db(conn)
    profile = strict_profile(registry)
    if principal.module != "dependency" or profile is None:
        raise PermissionDenied("access denied")
    business = {
        name
        for name, spec in registry.data_types.items()
        if spec.emits_events and name not in EXCLUDED
    }
    owners = {registry.get(name).writer for name in business}
    results = {
        name
        for name, spec in registry.data_types.items()
        if spec.reevaluation_result and spec.writer in owners
    }
    reads = business | results | REQUIRED
    if not reads <= registry.data_types.keys() or not reads <= principal.read_types:
        raise PermissionDenied("access denied")
    if not business <= principal.subscribe_types:
        raise PermissionDenied("access denied")
    # Every reevaluable owner needs an actual protected completion type, not a
    # name suffix supplied by an arbitrary plugin.
    for name in business:
        spec = registry.get(name)
        if spec.supports_reevaluation and spec.writer + "_completion" not in results:
            raise PermissionDenied("access denied")
    fingerprint = {
        "codec": 1,
        "profile": PROFILE,
        "profile_fingerprint": profile,
        "registry": registry.to_json(),
        "core_types": sorted(registry.core_types),
        "principal": principal.id,
        "reads": sorted(principal.read_types),
        "subscriptions": sorted(principal.subscribe_types),
    }
    revision = hashlib.sha256(canonical_json(fingerprint).encode()).hexdigest()
    return registry, revision


def guard_graph(conn: psycopg.Connection, principal: Principal) -> None:
    if strict_scheduler(conn, principal):
        require_coverage(conn, principal)


def authorize_propagation_write(conn: psycopg.Connection, principal: Principal) -> None:
    """Protect the scheduler's durable queue/control plane, including receipt replay.

    Caller payloads (schema_tag, owner, caller, authority_revision, blocked status)
    are not trusted provenance. Strict mode requires complete live authority for
    EVERY propagation write, including diagnostic creation/overwrite. A revoked
    worker must retain local journal evidence and surface write_denied; it cannot
    persist a blocked diagnostic by using a weaker write path. The shared writer
    gate serializes this check with grant/lifecycle changes and the actual write.
    Legacy module ownership and deployment-only backend writes are unchanged.
    """
    if strict_profile(registry_from_db(conn)) is not None:
        require_coverage(conn, principal)


def owner_principals(conn: psycopg.Connection, module: str) -> list[Principal]:
    lifecycle = conn.execute(
        "SELECT enabled,resources_ready,recovery_pending FROM storage.module_lifecycle "
        "WHERE module=%s",
        (module,),
    ).fetchone()
    if lifecycle is not None and not (lifecycle[0] and lifecycle[1] and not lifecycle[2]):
        return []
    principals = []
    for (digest,) in conn.execute(
        "SELECT token_hash FROM storage.principals WHERE module=%s AND active "
        "AND token_hash IS NOT NULL",
        (module,),
    ).fetchall():
        try:
            principals.append(authenticate(conn, digest))
        except AuthenticationError:
            continue
    return principals


def scheduler_state(conn: psycopg.Connection, principal: Principal) -> dict[str, Any]:
    registry, revision = require_coverage(conn, principal)
    owners = {}
    modules = {
        spec.writer
        for name, spec in registry.data_types.items()
        if spec.emits_events and name not in EXCLUDED
    }
    for module in sorted(modules):
        lifecycle = conn.execute(
            "SELECT enabled,revision FROM storage.module_lifecycle WHERE module=%s",
            (module,),
        ).fetchone()
        targets = {
            name
            for name, spec in registry.data_types.items()
            if spec.writer == module and spec.supports_reevaluation
        }
        # A capability summary only, not permission to send arbitrary causes.
        # Actual publication and delivery re-use authorize_event for every origin.
        can_receive = bool(targets) and any(
            targets <= p.read_types and targets <= p.subscribe_types
            for p in owner_principals(conn, module)
        )
        owners[module] = {
            "enabled": lifecycle[0] if lifecycle else True,
            "can_receive": can_receive,
            "revision": lifecycle[1] if lifecycle else None,
            "online": None,
        }
    return {"codec": 1, "coverage_complete": True, "coverage_revision": revision, "owners": owners}


def authorize_publication(
    conn: psycopg.Connection, principal: Principal, op: PublishReevaluation, context: Context
) -> None:
    """Extra strict checks before Publish and receipt replay.

    Legacy publication still uses _write_permissions and the transaction engine;
    it never attested owner availability. Do not retrofit that breaking policy.
    """
    if not strict_scheduler(conn, principal):
        return
    from ear_storage.consumers import approved_filters, authorize_event

    registry, _ = require_coverage(conn, principal)
    spec = registry.data_types.get(op.target_type)
    if spec is None or not spec.supports_reevaluation:
        raise PermissionDenied("access denied")
    event = ReevaluationRequested(
        event_id=op.event_id or new_id(),
        occurred_at=utcnow(),
        target_type=op.target_type,
        target_id=op.target_id,
        cause=op.cause,
        change_description=op.change_description,
        root_feedback_id=context.root_feedback_id,
        depth=context.depth,
        additional_causes=context.origins_for(op.additional_causes),
        trigger_request_event_id=context.request_event_id,
    )
    # Do not let a declared cause type disguise the actual object type.
    causes = (event.cause, *(t.cause for t in event.additional_causes))
    actual = ConnectionBoundReader(conn).record_types([c.data_id for c in causes])
    if any(actual.get(c.data_id) != c.data_type for c in causes):
        raise InvalidRequest("invalid write request")
    for owner in owner_principals(conn, spec.writer):
        try:
            config = {
                "event_types": ["reevaluation_requested"],
                "filters": approved_filters(
                    owner, registry, ["reevaluation_requested"], "ear.events"
                ),
            }
            authorize_event(owner, registry, config, event, "ear.events")
        except PermissionDenied:
            continue
        return
    raise PermissionDenied("access denied")
