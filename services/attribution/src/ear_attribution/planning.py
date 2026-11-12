"""Business planners on SDK snapshots; all effects returned for one atomic commit."""

from __future__ import annotations

from ear_contracts import (
    Event,
    RawRecord,
    Record,
    ReevaluationRequested,
    SemanticRecord,
    VersionRef,
    new_id,
)
from ear_storage_client import (
    Create,
    DependencyInput,
    Reader,
    RegisterDependencies,
    Update,
    WriteOp,
)

from ear_attribution.completion import finish
from ear_attribution.matching import MatchConfig, match, reference_dependency
from ear_attribution.payloads import (
    PAIRS,
    DomainError,
    attribution_payload,
    current_attribution,
    entity,
    read_attribution,
)
from ear_attribution.state import Fence, read_fence


class BusinessPlanner:
    def __init__(self, config: MatchConfig) -> None:
        self.config = config

    def _match(
        self,
        reader: Reader,
        subject: RawRecord,
        fence: Fence,
        generation: str,
    ) -> tuple[str, list[WriteOp], list[DependencyInput]]:
        matched = match(reader, subject, self.config, fence.unknown_id)
        deps = list(matched.dependencies)
        ops: list[WriteOp] = list(fence.initial_ops)
        target = matched.target_id
        if target is None:
            if subject.data_type == "gps_point":
                target = fence.unknown_id
                # Newly initialized unknown is also a real, final-batch dependency.
                if fence.current is None:
                    deps.append(
                        DependencyInput(
                            VersionRef(data_type="place", id=target, version=1),
                            ("references", "is_unknown"),
                        )
                    )
            else:
                target = new_id()
                number = fence.allocate_person()
                ops.append(
                    Create(
                        "person",
                        target,
                        {"name": None, "display_number": number, "references": [subject.id]},
                        dedup_key=f"unnamed_person:{subject.id}:{generation}",
                    )
                )
                deps.append(
                    DependencyInput(
                        VersionRef(data_type="person", id=target, version=1), ("references",)
                    )
                )
        return target, ops, deps

    def new_record(self, reader: Reader, record: Record, event: Event) -> list[WriteOp]:
        if not isinstance(record, RawRecord) or record.data_type not in PAIRS:
            raise DomainError("invalid_subject")
        if current_attribution(reader, record.id) is not None:
            return []
        fence = read_fence(reader)
        target, ops, deps = self._match(reader, record, fence, "initial")
        id = new_id()
        return [
            fence.operation(),
            *ops,
            Create("attribution", id, attribution_payload(record, target)),
            RegisterDependencies("attribution", id, deps),
        ]

    def reevaluation(self, reader: Reader, record: Record, event: Event) -> list[WriteOp]:
        if not isinstance(record, SemanticRecord) or not isinstance(event, ReevaluationRequested):
            raise DomainError("invalid_reevaluation")
        if record.superseded:
            return [
                finish(
                    event,
                    record,
                    "not_applicable",
                    reason="target_terminated",
                    disposition="terminated",
                )
            ]
        try:
            return self._reevaluate(reader, record, event)
        except DomainError as exc:
            # Bad business inputs are terminal and visible; transport/ACL errors propagate.
            return [finish(event, record, "failed", error=str(exc))]

    def _reevaluate(
        self,
        reader: Reader,
        record: SemanticRecord,
        event: ReevaluationRequested,
    ) -> list[WriteOp]:
        attribution = read_attribution(reader, record)
        if attribution.protected is None:
            return [
                finish(
                    event,
                    record,
                    "not_applicable",
                    reason="legacy_protection_unknown",
                    disposition="kept_protected",
                )
            ]
        fence = read_fence(reader)
        if attribution.protected:
            try:
                target = entity(reader, attribution.target_id, attribution.target_type)
            except DomainError:
                return [
                    finish(
                        event,
                        record,
                        "not_applicable",
                        reason="protected_target_unavailable",
                        disposition="kept_protected",
                    )
                ]
            # No matcher runs: user policy and current target are the complete inputs.
            return [
                fence.operation(),
                *fence.initial_ops,
                Update("attribution", record.id, record.version, record.payload),
                RegisterDependencies("attribution", record.id, [reference_dependency(target)]),
                finish(event, record, "unchanged"),
            ]
        target, ops, deps = self._match(
            reader,
            attribution.subject,
            fence,
            f"reevaluation:{record.version}:{fence.reference_revision}",
        )
        # Preserve legacy/extensions; never upgrade protection by inference from references.
        payload = dict(record.payload)
        payload["target_id"] = target
        changed = payload != record.payload
        return [
            fence.operation(),
            *ops,
            Update("attribution", record.id, record.version, payload),
            RegisterDependencies("attribution", record.id, deps),
            finish(event, record, "changed" if changed else "unchanged"),
        ]

    def feedback(self, reader: Reader, record: Record, event: Event) -> list[WriteOp] | None:
        from ear_attribution.feedback import plan_feedback

        return plan_feedback(reader, record)
