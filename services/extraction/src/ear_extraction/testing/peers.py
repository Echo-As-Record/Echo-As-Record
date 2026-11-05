"""Narrow mock ③⑤⑥ peers using real ① transactions/events and separate writer identities.

These local feedback payloads and propagation details are fixture contracts only.
They do not implement production attribution, dependency scheduling or action execution.
"""

from __future__ import annotations

from ear_contracts import Cause, DataChanged, Event, SemanticRecord, SystemRecord, new_id
from ear_storage_client import (
    AppendRaw,
    Context,
    MarkProcessed,
    MemoryStorage,
    PublishReevaluation,
    PutSystemRecord,
    Query,
    Update,
    WriteOp,
    iter_query,
)
from ear_storage_client.testing import make_raw

from ear_extraction.domain import Command
from ear_extraction.persistence import semantic


def feedback(storage: MemoryStorage, module: str, command: dict, *, dedup_key=None) -> str:
    raw = make_raw(
        "feedback",
        {"module": module, "command": command},
        start="2026-11-05T10:00:00Z",
        channel="mock_api",
        dedup_key=dedup_key,
    )
    storage.client("api").write([AppendRaw(raw)], Context.feedback(raw.id)).check()
    return raw.id


class MockPeers:
    def __init__(self, storage: MemoryStorage):
        self.storage = storage
        # A fake external service with a stable idempotency key, not an exactly-once claim.
        self.external_actions: dict[str, dict] = {}
        storage.client("attribution").subscribe("attribution.mock", self.attribute)
        storage.client("dependency").subscribe("dependency.mock", self.propagate)
        storage.client("api").subscribe("api.mock", self.execute)

    def attribute(self, event: Event) -> None:
        if not isinstance(event, DataChanged) or event.data_type != "feedback":
            return
        client = self.storage.client("attribution")
        raw = client.get(event.data_id)
        if raw is None or raw.payload.get("module") != "attribution":
            return
        if client.is_processed("attribution.mock", event.event_id):
            return
        command = raw.payload["command"]
        attr = semantic(client, command["target_id"], "attribution")
        old = semantic(client, attr.payload["target_id"], "person")
        new = semantic(client, command["person_id"], "person")
        voice = attr.payload["subject_id"]
        client.write(
            [
                Update(
                    "person",
                    old.id,
                    old.version,
                    {
                        **old.payload,
                        "references": [v for v in old.payload["references"] if v != voice],
                    },
                    skip_history=True,
                ),
                Update(
                    "person",
                    new.id,
                    new.version,
                    {**new.payload, "references": [*new.payload["references"], voice]},
                    skip_history=True,
                ),
                Update("attribution", attr.id, attr.version, {**attr.payload, "target_id": new.id}),
                MarkProcessed("attribution.mock", event.event_id),
            ],
            Context.feedback(raw.id),
        ).check()

    def propagate(self, event: Event) -> None:
        if (
            not isinstance(event, DataChanged)
            or event.operation == "created"
            or event.skip_history
            or event.data_type == "rule"
        ):
            return
        client = self.storage.client("dependency")
        if client.is_processed("dependency.mock", event.event_id):
            return
        targets = {
            d.dependent.id
            for d in client.stale_dependencies([event.data_id])
            if d.dependent.data_type == "artifact"
        }
        ops: list[WriteOp] = [MarkProcessed("dependency.mock", event.event_id)]
        for target in targets:
            request_id = new_id()
            cause = Cause(
                data_type=event.data_type,
                data_id=event.data_id,
                old_version=event.old_version,
                new_version=event.new_version,
            )
            ops.extend(
                [
                    PublishReevaluation(
                        "artifact", target, cause, "模拟输入变更", event_id=request_id
                    ),
                    PutSystemRecord(
                        "propagation_record",
                        new_id(),
                        {
                            "target_id": target,
                            "request_id": request_id,
                            "cause": cause.model_dump(mode="json"),
                            "root_feedback_id": event.root_feedback_id,
                            "status": "waiting",
                        },
                    ),
                ]
            )
        client.write(ops, Context.of(event).deeper()).check()

    def poll_completions(self) -> None:
        client = self.storage.client("dependency")
        for record in iter_query(client, Query("propagation_record")):
            assert isinstance(record, SystemRecord)
            if record.payload["status"] != "waiting":
                continue
            run = client.get_system_record(record.payload["request_id"])
            if run is None or run.payload["status"] not in {"succeeded", "blocked", "failed"}:
                continue
            client.write(
                [
                    PutSystemRecord(
                        "propagation_record",
                        record.id,
                        {
                            **record.payload,
                            "status": run.payload["status"],
                            "detail": run.payload.get("detail"),
                            "error": run.payload["error"],
                        },
                        expected_revision=record.revision,
                    )
                ],
                Context(run.payload["root_feedback_id"], run.payload["depth"]),
            ).check()

    def execute(self, event: Event) -> None:
        if not isinstance(event, DataChanged) or event.data_type != "action_suggestion":
            return
        client = self.storage.client("api")
        current = client.get(event.data_id)
        if (
            not isinstance(current, SemanticRecord)
            or current.superseded
            or current.payload["state"] != "approved"
        ):
            return
        self.external_actions.setdefault(current.id, current.payload["action"])
        command = Command(
            kind="action_result",
            target_id=current.id,
            expected_version=current.version,
            outcome="executed",
            receipt=f"mock-receipt:{current.id}",
        )
        feedback(
            self.storage,
            "extraction",
            command.model_dump(mode="json"),
            dedup_key=f"mock-result:{current.id}",
        )
