"""Causal metadata has the same read boundary as its referenced input types."""

from typing import LiteralString

from ear_contracts import ReevaluationResult, Registry, SemanticRecord, SystemRecord
from psycopg import sql


def record_input_types(record, registry: Registry) -> frozenset[str]:
    if isinstance(record, SemanticRecord):
        return frozenset(t.cause.data_type for t in record.additional_causes)
    if isinstance(record, SystemRecord):
        spec = registry.data_types.get(record.data_type)
        if spec is not None and spec.reevaluation_result:
            result = ReevaluationResult.model_validate(record.payload)
            # Legacy extra_causes stay as-is; all new independently traced paths
            # must be readable, including on ordinary system-record endpoints.
            return frozenset(t.cause.data_type for t in result.additional_causes)
    if isinstance(record, ReevaluationResult):
        return frozenset(t.cause.data_type for t in record.additional_causes)
    return frozenset()


def visible_trace_sql(
    reads, *, text: LiteralString = "r.record_text", dtype: LiteralString = "r.data_type"
):
    """Internal SQL expressions only; no caller-supplied SQL or business payload paths."""
    return sql.SQL(
        "NOT EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE("
        "CASE WHEN {dtype} IN (SELECT name FROM storage.registry "
        "WHERE spec_text::jsonb->>'reevaluation_result'='true') "
        "THEN {text}::jsonb->'payload'->'additional_causes' "
        "ELSE {text}::jsonb->'additional_causes' END, '[]'::jsonb)) AS trace "
        "WHERE NOT COALESCE(trace->'cause'->>'data_type'=ANY({reads}::text[]),false))"
    ).format(text=sql.SQL(text), dtype=sql.SQL(dtype), reads=sql.Literal(sorted(reads)))
