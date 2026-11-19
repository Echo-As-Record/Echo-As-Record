"""Admit only proven causal field transitions, independently of current stale.

Do not filter graph closure with this predicate: fresh edges remain barriers and
may become relevant when their upstream owner finishes. A missing historical
version/authority is not evidence of a change (or of an unaffected target).
"""

from collections.abc import Sequence

from ear_contracts import Cause, DataChanged, DependencyRecord, SemanticRecord, TracedCause
from ear_storage_client import Reader
from ear_storage_client.engine import canonical_json


class CausalVersionsUnavailable(RuntimeError):
    pass


def validate_creation(reader: Reader, cause: Cause | DataChanged) -> None:
    """Prove the only legal no-before transition without consulting current."""
    if (
        cause.old_version is not None
        or cause.new_version != 1
        or (isinstance(cause, DataChanged) and cause.operation != "created")
    ):
        raise CausalVersionsUnavailable("causal_versions_unavailable")
    initial = reader.get_version(cause.data_id, cause.new_version)
    if (
        not isinstance(initial, SemanticRecord)
        or initial.id != cause.data_id
        or initial.data_type != cause.data_type
        or initial.version != 1
        or initial.superseded
    ):
        raise CausalVersionsUnavailable("causal_versions_unavailable")


def changes_edge(reader: Reader, cause: Cause, edge: DependencyRecord) -> bool:
    """Compare this cause's immutable endpoints, never registered-vs-current."""
    if cause.data_id != edge.depends_on.id:
        return False
    if cause.data_type != edge.depends_on.data_type or cause.new_version is None:
        raise CausalVersionsUnavailable("causal_versions_unavailable")
    if cause.old_version is None:
        # Only creation has no before endpoint: versions start at 1. Validate
        # that immutable initial record, not current (which may have advanced
        # behind skip_history). Every valid existing edge already incorporates
        # v1, so delayed creation cannot authorize later stale debt. A missing
        # before for an update/termination, or an unprovable v1, still blocks.
        validate_creation(reader, cause)
        if edge.depends_on.version < 1:
            raise CausalVersionsUnavailable("causal_versions_unavailable")
        return False
    before = reader.get_version(cause.data_id, cause.old_version)
    after = reader.get_version(cause.data_id, cause.new_version)
    if (
        before is None
        or after is None
        or before.id != cause.data_id
        or after.id != cause.data_id
        or before.data_type != cause.data_type
        or after.data_type != cause.data_type
        or before.version != cause.old_version
        or after.version != cause.new_version
    ):
        raise CausalVersionsUnavailable("causal_versions_unavailable")
    if isinstance(after, SemanticRecord) and after.superseded:
        return True
    # This edge already incorporated this transition. A later gated change
    # must not make a completed upstream generation (kept for barriers/audit)
    # authorize its debt again. Never discard those upstream steps themselves.
    if after.version <= edge.depends_on.version:
        return False
    if edge.fields is None:
        return before.version != after.version
    return any(
        (key in before.payload) != (key in after.payload)
        or canonical_json(before.payload.get(key)) != canonical_json(after.payload.get(key))
        for key in edge.fields
    )


def causally_stale(
    reader: Reader, edges: Sequence[DependencyRecord], causes: Sequence[TracedCause]
) -> bool:
    eligible: list[DependencyRecord] = []
    for edge in edges:
        changed = False
        for trace in causes:
            # Evaluate every matching cause, including additional causes. Do not
            # short-circuit away an unknown endpoint/permission behind a true one.
            changed = changes_edge(reader, trace.cause, edge) or changed
        if changed:
            eligible.append(edge)
    if not eligible:
        return False
    stale = reader.stale_dependencies(sorted({e.depends_on.id for e in eligible}))
    # Keep the field-specific edge, not just its source id. Another edge from the
    # same input must not turn unrelated stale debt into causal authorization.
    return any(
        item.dependent == edge.dependent
        and item.depends_on == edge.depends_on
        and item.fields == edge.fields
        for item in stale
        for edge in eligible
    )
