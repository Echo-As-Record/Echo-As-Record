"""Synthetic owners using ONLY SDK interfaces, not ②③④ implementations."""

from typing import Literal

from ear_contracts import (
    Cause,
    DataChanged,
    DataTypeSpec,
    ReevaluationRequested,
    ReevaluationResult,
    SemanticRecord,
    VersionRef,
    new_id,
    utcnow,
)
from ear_dependency import DependencyService
from ear_dependency.domain import Step, Trigger
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    DependencyInput,
    FinishReevaluation,
    MarkProcessed,
    MemoryStorage,
    RegisterDependencies,
    Supersede,
    Update,
    WriteOp,
)
from ear_storage_client.testing import make_raw


class World:
    def __init__(self, **options):
        self.storage = MemoryStorage(duplicate_delivery=True)
        self.client = self.storage.client("dependency")
        self.service = DependencyService(self.client, full_visibility=True, **options)
        self.ids: dict[str, str] = {}
        self.types: dict[str, str] = {}
        self.add_type("plugin_a", "alpha")
        self.add_type("plugin_internal", "alpha")
        self.add_type("plugin_b", "beta")
        self.add_type("plugin_c", "gamma")

    def add_type(self, name: str, owner: str, *, capable: bool = True):
        specs = [
            DataTypeSpec(
                name, "semantic", owner, supports_reevaluation=capable, requires_dependencies=True
            )
        ]
        if owner + "_completion" not in self.storage.registry:
            specs.append(
                DataTypeSpec(
                    owner + "_completion", "system_record", owner, reevaluation_result=True
                )
            )
        # Public memory fixture registration, NOT production authorization.
        self.storage.registry = self.storage.registry.extend(*specs)

    def seed(self, name: str, data_type: str = "person") -> str:
        ident = new_id()
        owner = self.storage.registry.writer_of(data_type)
        self.storage.client(owner).write(
            [
                Create(data_type, ident, {"name": name, "references": []}),
            ],
            Context.system(),
        ).check()
        self.ids[name], self.types[name] = ident, data_type
        return ident

    def node(
        self,
        name: str,
        inputs: list[str],
        data_type: str = "plugin_a",
        fields: tuple[str, ...] | None = None,
    ) -> str:
        ident = new_id()
        refs = []
        for input_name in inputs:
            source = self.client.get(self.ids[input_name])
            assert source is not None
            refs.append(
                DependencyInput(
                    VersionRef(data_type=source.data_type, id=source.id, version=source.version),
                    fields,
                )
            )
        self.storage.client(self.storage.registry.writer_of(data_type)).write(
            [
                Create(data_type, ident, {"value": 0}),
                RegisterDependencies(data_type, ident, refs),
            ],
            Context.system(),
        ).check()
        self.ids[name], self.types[name] = ident, data_type
        return ident

    def feedback(self) -> str:
        raw = make_raw("feedback", {"synthetic": True}, start=utcnow())
        self.storage.client("api").write([AppendRaw(raw)], Context.system()).check()
        return raw.id

    def change(
        self,
        name: str,
        *,
        root: str = "system",
        skip: bool = False,
        payload: dict | None = None,
        terminate: bool = False,
    ) -> DataChanged:
        ident = self.ids[name]
        current = self.client.get(ident)
        assert current is not None
        owner = self.storage.registry.writer_of(current.data_type)
        op = (
            Supersede(current.data_type, ident, current.version, skip_history=skip)
            if terminate
            else Update(
                current.data_type,
                ident,
                current.version,
                payload or {**current.payload, "name": new_id()},
                skip_history=skip,
            )
        )
        result = self.storage.client(owner).write([op], Context(root)).check()
        event = result.events[0]
        assert isinstance(event, DataChanged)
        self.service.handle(event)
        return event

    def requests(self, name: str | None = None) -> list[ReevaluationRequested]:
        return [
            e
            for e in self.storage.events
            if isinstance(e, ReevaluationRequested)
            and (name is None or e.target_id == self.ids[name])
        ]

    def steps(self, name: str | None = None) -> list[Step]:
        return [
            r
            for r in self.service.records()
            if isinstance(r, Step) and (name is None or r.target_id == self.ids[name])
        ]

    def triggers(self) -> list[Trigger]:
        return [r for r in self.service.records() if isinstance(r, Trigger)]

    def finish(
        self,
        name: str,
        outcome: Literal["changed", "unchanged", "failed", "not_applicable"],
        disposition: Literal["kept_protected", "terminated", "unavailable"] | None = None,
        extra_causes: tuple[Cause, ...] = (),
    ):
        request = self.requests(name)[-1]
        current = self.client.get(request.target_id)
        assert isinstance(current, SemanticRecord)
        owner = self.storage.registry.writer_of(current.data_type)
        client = self.storage.client(owner)
        ops: list[WriteOp] = []
        after = current.version
        if outcome in {"changed", "unchanged"}:
            after += outcome == "changed"
            if outcome == "changed":
                ops.append(
                    Update(
                        current.data_type,
                        current.id,
                        current.version,
                        {"value": current.payload["value"] + 1},
                    )
                )
            refs = []
            for edge in client.dependencies_of(current.id):
                source = client.get(edge.depends_on.id)
                assert isinstance(source, SemanticRecord)
                if not source.superseded:
                    refs.append(
                        DependencyInput(
                            VersionRef(
                                data_type=source.data_type, id=source.id, version=source.version
                            ),
                            edge.fields,
                        )
                    )
            ops.append(RegisterDependencies(current.data_type, current.id, refs))
        elif disposition == "terminated":
            ops.append(Supersede(current.data_type, current.id, current.version))
            after += 1
        result = ReevaluationResult(
            request_event_id=request.event_id,
            target_type=current.data_type,
            target_id=current.id,
            outcome=outcome,
            before_version=current.version,
            after_version=after,
            root_feedback_id=request.root_feedback_id,
            depth=request.depth,
            additional_causes=request.additional_causes,
            completed_at=utcnow(),
            disposition=disposition,
            reason_code="synthetic" if disposition else None,
            error_code="synthetic_failure" if outcome == "failed" else None,
            extra_causes=extra_causes,
        )
        ops.extend(
            [FinishReevaluation(result), MarkProcessed(owner + ".fixture", request.event_id)]
        )
        client.write(ops, Context.of(request)).check()

    def ticks(self, count: int = 3):
        for _ in range(count):
            self.service.tick()

    def rebuild(self, **options):
        self.client = self.storage.client("dependency")
        self.service = DependencyService(self.client, full_visibility=True, **options)
