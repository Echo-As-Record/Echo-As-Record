"""Storage-backed, single-worker scheduler for the bounded memory acceptance slice.

No background threads, network, auto-install, global registry mutation or business
writes. Call handle(event), then tick(); owners execute and Finish independently.
All state is recoverable via a new service over the SAME surviving store. Remote
clients are deliberately refused: filtered graph reads cannot prove completeness.
Unknown publish outcomes freeze the persisted intent; absence is NOT proof of
non-commit, and there is no automatic new-ID retry.
"""

from collections.abc import Callable
from time import time

from ear_contracts import Cause, DataChanged, Event, SemanticRecord, TracedCause, new_id
from ear_storage_client import (
    CommitUnknown,
    Context,
    MarkProcessed,
    MemoryClient,
    PermissionDenied,
    PublishReevaluation,
    Reader,
    StorageClient,
    WriteOp,
)

from ear_dependency.domain import Step, Trigger, event_causes, union
from ear_dependency.graph import IncompleteGraph, closure
from ear_dependency.records import Record, load, put

TERMINAL = {"done", "unaffected"}


class DependencyService:
    """Explicit full-visibility TEST premise, not a grant or lifecycle authority.

    Only one worker may call handle/tick for this consumer at a time. CAS protects
    individual writes but is NOT a distributed lease. No historical scan occurs
    on construction. Pending records are picked up on the next bounded tick.
    """

    def __init__(
        self,
        client: StorageClient,
        *,
        full_visibility: bool = False,
        consumer: str = "dependency.changes",
        max_nodes: int = 200,
        max_records: int = 5000,
        max_depth: int = 64,
        max_generations: int = 5,
        request_timeout: float = 600,
        clock: Callable[[], float] = time,
    ) -> None:
        if not isinstance(client, MemoryClient):
            raise ValueError("scheduler_authority_unavailable: remote execution refused")
        if not full_visibility:
            raise ValueError("coverage_incomplete: full memory visibility must be explicit")
        if client.module != "dependency" or not (
            consumer == "dependency" or consumer.startswith("dependency.")
        ):
            raise ValueError("dependency identity/consumer required")
        if min(max_nodes, max_records, max_depth, max_generations, request_timeout) <= 0:
            raise ValueError("positive bounds required")
        self.client = client
        self.consumer = consumer
        self.max_nodes = max_nodes
        self.max_records = max_records
        self.max_depth = max_depth
        self.max_generations = max_generations
        self.request_timeout = request_timeout
        self.clock = clock

    def _write(self, ops: list[WriteOp], context: Context | None = None) -> None:
        # Do not hide conflicts or transport errors: event handlers must not ACK them.
        self.client.write(ops, context or Context.system()).check()

    def _save(self, record: Record, revision: int) -> None:
        self._write([put(record, revision)])

    def handle(self, event: Event) -> None:
        if not isinstance(event, DataChanged):
            return
        with self.client.snapshot() as reader:
            current = reader.get(event.data_id)  # current state BEFORE marker/gating
            if reader.is_processed(self.consumer, event.event_id):
                return
            trigger = Trigger(event=event)
            spec = self.client.registry.data_types.get(event.data_type)
            if current is None or spec is None:
                trigger.status, trigger.reason = "blocked", "coverage_incomplete"
            elif current.data_type != event.data_type or current.version < event.new_version:
                trigger.status, trigger.reason = "blocked", "event_state_mismatch"
            elif event.data_type == "feedback":
                if current.payload.get("module") == "dependency":
                    trigger.status, trigger.reason = "blocked", "feedback_not_implemented"
                else:
                    trigger.status, trigger.reason = "ignored", "other_feedback"
            elif spec.kind == "raw_record":
                trigger.status, trigger.reason = "ignored", "raw_append"
            elif spec.kind == "rule":
                trigger.status, trigger.reason = "ignored", "rule_edit"
            elif event.skip_history:
                trigger.status, trigger.reason = "ignored", "skip_history"
        # Trigger and marker are atomic; even a gated input has an explicit reason.
        result = self.client.write(
            [put(trigger), MarkProcessed(self.consumer, event.event_id)], Context.system()
        )
        if result.conflict is not None and result.conflict.reason == "already_processed":
            return
        result.check()

    def records(self) -> list[Record]:
        with self.client.snapshot() as reader:
            return [r for r, _ in load(reader, self.max_records).values()]

    def tick(self) -> None:
        """One finite pass. New work/second generations progress on subsequent calls."""
        with self.client.snapshot() as reader:
            records = load(reader, self.max_records)
        # Expand all known events BEFORE deciding any step, so simultaneous causes
        # merge and known upstream work cannot be bypassed by ordering of ticks.
        for record, _ in records.values():
            if isinstance(record, Trigger) and record.status == "open":
                self._expand(record.id)
        with self.client.snapshot() as reader:
            records = load(reader, self.max_records)
        for record, _ in records.values():
            if isinstance(record, Step) and record.successor_id is None:
                self._advance(record.id)
        self._summarize()

    def _expand(self, trigger_id: str) -> None:
        with self.client.snapshot() as reader:
            records = load(reader, self.max_records)
            trigger, rev = records[trigger_id]
            assert isinstance(trigger, Trigger)
            try:
                parents = closure(reader, trigger.event.data_id, self.max_nodes)
                objects = reader.get_many(list(parents))
                if len(objects) != len(parents):
                    raise IncompleteGraph("coverage_incomplete")
            except (IncompleteGraph, PermissionDenied) as exc:
                trigger.status = "blocked"
                trigger.reason = str(exc) if isinstance(exc, IncompleteGraph) else "read_denied"
                self._save(trigger, rev)
                return
            latest: dict[str, Step] = {}
            for record, _ in records.values():
                if isinstance(record, Step) and record.successor_id is None:
                    old = latest.get(record.target_id)
                    if old is None or record.generation > old.generation:
                        latest[record.target_id] = record
            planned: dict[str, Step] = {}
            causes = event_causes(trigger.event)
            for target in sorted(parents):
                previous = latest.get(target)
                current = objects[target]
                if previous is not None and (
                    previous.state not in TERMINAL or previous.pending_causes
                ):
                    step = previous.model_copy(deep=True)
                    if step.state in {"intent", "requested"} or step.request_event_id is not None:
                        step.pending_causes = union(step.pending_causes, causes)
                    else:
                        step.causes = union(step.causes, causes)
                else:
                    step = Step(
                        target_id=target,
                        target_type=current.data_type,
                        target_version=current.version,
                        causes=causes,
                        request_parent_id=trigger.event.request_event_id,
                        generation=1 if previous is None else previous.generation + 1,
                    )
                if trigger.event.request_event_id is not None:
                    step.upstream_request_ids = union(
                        step.upstream_request_ids, [trigger.event.request_event_id]
                    )
                step.trigger_ids = union(step.trigger_ids, [trigger.id])
                planned[target] = step
            if len(records) + sum(s.id not in records for s in planned.values()) > self.max_records:
                trigger.status, trigger.reason = "blocked", "record_limit"
                self._save(trigger, rev)
                return
            ops: list[WriteOp] = []
            for target, step in planned.items():
                # Include other roots' active inputs, not just this trigger's closure.
                inputs = {e.depends_on.id for e in reader.dependencies_of(target)}
                for source in inputs | parents[target]:
                    upstream = planned.get(source) or latest.get(source)
                    if (
                        upstream
                        and upstream.id != step.id
                        and (
                            source in planned
                            or upstream.state not in TERMINAL
                            or upstream.pending_causes
                        )
                    ):
                        step.upstream_step_ids = union(step.upstream_step_ids, [upstream.id])
                ops.append(put(step, records[step.id][1] if step.id in records else None))
            trigger.status = "expanded"
            ops.append(put(trigger, rev))
        self._write(ops)

    def _advance(self, step_id: str) -> None:
        with self.client.snapshot() as reader:
            records = load(reader, self.max_records)
            step, rev = records[step_id]
            assert isinstance(step, Step)
            if step.request_event_id is not None:
                result = reader.get_reevaluation_result(step.request_event_id)
                if result is not None and step.result is None:
                    step.result = result
                    if result.outcome == "failed":
                        step.state, step.reason = "blocked", "failed"
                    elif result.outcome == "not_applicable" and result.disposition != "terminated":
                        step.state, step.reason = "blocked", result.disposition
                    else:
                        step.state, step.reason = "done", None
                    self._save(step, rev)
                    return
                if step.state == "intent":
                    # It may have committed before a lost response. Never re-publish
                    # merely because no Finish is present. Needs operator reconciliation.
                    step.state, step.reason = "blocked", "commit_unknown"
                    self._save(step, rev)
                    return
                if step.result is None and step.state == "requested":
                    assert step.requested_at is not None
                    if self.clock() - step.requested_at >= self.request_timeout:
                        step.state, step.reason = "blocked", "timeout"
                        self._save(step, rev)
                    return
            if step.state == "done" and step.pending_causes:
                self._followup(reader, step, rev)
                return
            if step.state in TERMINAL or (
                step.state == "blocked" and step.reason != "upstream_blocked"
            ):
                return
            upstreams: list[Step] = []
            for upstream_id in step.upstream_step_ids:
                up, _ = records[upstream_id]
                assert isinstance(up, Step)
                # Pending causes must be resolved before releasing a downstream.
                while up.successor_id is not None:
                    up, _ = records[up.successor_id]
                    assert isinstance(up, Step)
                upstreams.append(up)
            if any(up.state == "blocked" for up in upstreams):
                if step.reason != "upstream_blocked":
                    step.state, step.reason = "blocked", "upstream_blocked"
                    self._save(step, rev)
                return
            if any(up.state not in TERMINAL or up.pending_causes for up in upstreams):
                return
            step.state, step.reason = "pending", None
            target = reader.get(step.target_id)
            if not isinstance(target, SemanticRecord):
                step.state, step.reason = "blocked", "coverage_incomplete"
            elif target.superseded:
                step.state, step.reason = "blocked", "target_superseded"
            else:
                inputs = reader.dependency_set(step.target_id)
                if inputs is None:
                    step.state, step.reason = "blocked", "dependencies_missing"
                else:
                    # ONLY authorized causal paths trigger a request. An unrelated
                    # stale rule/reference must not revive a path that was unchanged.
                    relevant = {t.cause.data_id for t in step.causes}
                    relevant.update(up.target_id for up in upstreams if self._changed(up))
                    stale = reader.stale_dependencies(sorted(relevant))
                    if not any(e.dependent.id == step.target_id for e in stale):
                        step.state = "unaffected"
                    else:
                        step.target_version = target.version
                        self._prepare(
                            step, upstreams, {e.depends_on.id for e in inputs.dependencies}
                        )
            if step.state != "intent":
                self._save(step, rev)
                return
            # Durable intent BEFORE publishing. A crash in this window safely
            # blocks (rather than guessing whether to send a new request).
            self._save(step, rev)
        self._publish(step, rev + 1)

    @staticmethod
    def _changed(step: Step) -> bool:
        return step.result is not None and (
            step.result.outcome == "changed" or step.result.disposition == "terminated"
        )

    def _prepare(self, step: Step, upstreams: list[Step], direct_inputs: set[str]) -> None:
        spec = self.client.registry.data_types.get(step.target_type)  # dynamic, not cached
        if spec is None or not spec.supports_reevaluation:
            step.state, step.reason = "blocked", "not_reevaluable"
            return
        step.owner = spec.writer
        origins = list(step.causes)
        derived: list[TracedCause] = []
        for up in upstreams:
            if up.request_event_id:
                step.upstream_request_ids = union(step.upstream_request_ids, [up.request_event_id])
            if self._changed(up):
                result = up.result
                assert result is not None and result.after_version is not None
                cause = TracedCause(
                    cause=Cause(
                        data_type=up.target_type,
                        data_id=up.target_id,
                        old_version=result.before_version,
                        new_version=result.after_version,
                    ),
                    root_feedback_id=result.root_feedback_id,
                    depth=result.depth + 1,
                )
                if not derived:
                    step.request_parent_id = up.request_event_id
                derived = union(derived, [cause])
                derived = union(
                    derived,
                    [t.model_copy(update={"depth": t.depth + 1}) for t in up.request_causes],
                )
                derived = union(
                    derived,
                    [t.model_copy(update={"depth": t.depth + 1}) for t in result.additional_causes],
                )
                derived = union(
                    derived,
                    [
                        TracedCause(
                            cause=c,
                            root_feedback_id=result.root_feedback_id,
                            depth=result.depth + 1,
                        )
                        for c in result.extra_causes
                    ],
                )
        # Non-direct origins have travelled through the completed parent. Preserve
        # their root/cause, but do not reset the path depth back to the seed's depth.
        advanced = []
        for trace in origins:
            matching = [
                t.depth
                for t in derived
                if t.cause == trace.cause and t.root_feedback_id == trace.root_feedback_id
            ]
            if trace.cause.data_id not in direct_inputs and matching:
                trace = trace.model_copy(update={"depth": max(trace.depth, *matching)})
            advanced.append(trace)
        origins = union(derived, advanced)
        step.causes = union(step.causes, origins)
        if len(origins) > 257 or any(t.depth > self.max_depth for t in origins):
            step.state, step.reason = "blocked", "cause_or_depth_limit"
        elif step.generation > self.max_generations:
            step.state, step.reason = "blocked", "generation_limit"
        else:
            step.state = "intent"
            step.request_causes = origins
            step.request_event_id, step.intent_id = new_id(), new_id()
            step.requested_at = self.clock()

    def _publish(self, step: Step, revision: int) -> None:
        primary, *additional = step.request_causes
        context = Context(
            primary.root_feedback_id, primary.depth, step.request_parent_id, tuple(additional)
        )
        step.state = "requested"
        try:
            self._write(
                [
                    PublishReevaluation(
                        step.target_type,
                        step.target_id,
                        primary.cause,
                        "Relevant dependencies changed; read current authorized inputs.",
                        event_id=step.request_event_id,
                        additional_causes=tuple(additional),
                    ),
                    put(step, revision),
                ],
                context,
            )
        except CommitUnknown:
            # Atomic committed step will be requested; otherwise durable intent
            # remains and reconstruction freezes it. Never blind retry/new ID.
            raise
        except PermissionDenied:
            step.state, step.reason = "blocked", "publish_denied"
            self._save(step, revision)

    def _followup(self, reader: Reader, step: Step, revision: int) -> None:
        records = load(reader, self.max_records)
        relevant = {t.cause.data_id for t in step.pending_causes}
        unresolved_upstream = False
        for upstream_id in step.upstream_step_ids:
            upstream, _ = records[upstream_id]
            assert isinstance(upstream, Step)
            while upstream.successor_id is not None:
                upstream, _ = records[upstream.successor_id]
                assert isinstance(upstream, Step)
            unresolved_upstream |= upstream.state not in TERMINAL or bool(upstream.pending_causes)
            if self._changed(upstream):
                relevant.add(upstream.target_id)
        # Absence of direct stale seed edges cannot prove absorption through a
        # parent that has not finished. Preserve a successor with the full barrier.
        stale = reader.stale_dependencies(sorted(relevant))
        if not unresolved_upstream and not any(e.dependent.id == step.target_id for e in stale):
            step.absorbed_causes = union(step.absorbed_causes, step.pending_causes)
            step.pending_causes = []
            self._save(step, revision)
            return
        if len(records) >= self.max_records:
            step.state, step.reason = "blocked", "record_limit"
            self._save(step, revision)
            return
        successor = Step(
            target_id=step.target_id,
            target_type=step.target_type,
            target_version=step.target_version,
            generation=step.generation + 1,
            trigger_ids=list(step.trigger_ids),
            causes=list(step.pending_causes),
            upstream_step_ids=list(step.upstream_step_ids),
        )
        step.successor_id = successor.id
        # Preserve the old generation's pending cause audit, but do not hold its
        # downstream forever: barriers follow successor_id until resolved.
        self._write([put(step, revision), put(successor)])

    def _summarize(self) -> None:
        with self.client.snapshot() as reader:
            records = load(reader, self.max_records)
        steps = [r for r, _ in records.values() if isinstance(r, Step)]
        for trigger, revision in records.values():
            if not isinstance(trigger, Trigger) or trigger.status not in {
                "expanded",
                "waiting",
                "blocked",
            }:
                continue
            if trigger.reason is not None:  # rejected/incomplete expansion is not an empty success
                continue
            relevant = [s for s in steps if trigger.id in s.trigger_ids and s.successor_id is None]
            summary: dict[str, int] = {}
            for step in relevant:
                key = (
                    step.reason
                    if step.state == "blocked"
                    else (step.result.outcome if step.result is not None else step.state)
                )
                summary[key or step.state] = summary.get(key or step.state, 0) + 1
            status = "closed"
            if any(s.state == "blocked" for s in relevant):
                status = "blocked"
            elif any(s.state not in TERMINAL or s.pending_causes for s in relevant):
                status = "waiting"
            if trigger.status != status or trigger.summary != summary:
                trigger.status, trigger.summary = status, summary
                self._save(trigger, revision)
