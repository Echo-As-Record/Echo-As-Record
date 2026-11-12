"""All scheduler state goes through the public SDK, never a private state dict."""

from ear_contracts import SystemRecord
from ear_storage_client import PutSystemRecord, Query, Reader, iter_query

from ear_dependency.domain import SCHEMA, Step, Trigger

Record = Step | Trigger


def load(reader: Reader, limit: int) -> dict[str, tuple[Record, int]]:
    records: dict[str, tuple[Record, int]] = {}
    for item in iter_query(reader, Query("propagation_record", where={"schema_tag": SCHEMA})):
        if len(records) >= limit:
            raise RuntimeError("record_limit: no partial recovery or false completion")
        if not isinstance(item, SystemRecord):
            raise RuntimeError("invalid propagation envelope")
        cls = Step if item.payload.get("record_kind") == "step" else Trigger
        records[item.id] = (cls.model_validate(item.payload), item.revision)
    return records


def put(record: Record, revision: int | None = None) -> PutSystemRecord:
    return PutSystemRecord(
        "propagation_record", record.id, record.model_dump(mode="json"), expected_revision=revision
    )
