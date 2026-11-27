"""Exercise the worker through the real in-memory SDK and a recording model port."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, cast

import pytest
from ear_contracts import Cause, NewRawRecord, RawRecord, SemanticRecord, format_utc, new_id
from ear_extraction.demo import fixture_registry, submit
from ear_extraction.domain import Call, Command, Interval, Rule
from ear_extraction.orchestration import ExtractionService, reevaluation_run_id
from ear_extraction.persistence import records
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    MemoryStorage,
    Page,
    PermissionDenied,
    PublishReevaluation,
    Query,
    Reader,
    Supersede,
    Update,
    iter_query,
)
from ear_storage_client.memory import MemoryClient
from ear_storage_client.testing import make_raw, ts

RANGE = Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))


class CapturingGateway:
    def __init__(self):
        self.calls: list[Call] = []
        self.merge = False
        self.forged_evidence_id: str | None = None

    def complete(self, call: Call) -> dict[str, Any]:
        self.calls.append(call)
        match call.purpose:
            case "segmentation":
                return {
                    "activities": [
                        {
                            **call.data["interval"],
                            "title_md": "Synthetic activity",
                            # The source may contain more evidence than was cited by segmentation.
                            "evidence_ids": [call.data["evidence"][0]["id"]],
                        }
                    ]
                }
            case "selection":
                return {"rule_ids": [r["id"] for r in call.data["rules"]]}
            case "generation":
                return {
                    "body_md": "Synthetic result",
                    "evidence_ids": (
                        [self.forged_evidence_id]
                        if self.forged_evidence_id
                        else [r["id"] for r in call.data["evidence"]]
                    ),
                }
            case "merge_activities":
                return {"merge": self.merge, "title_md": "Merged synthetic activity"}
            case "consolidation":
                return {
                    "body_md": "Consolidated result",
                    "evidence_ids": [
                        id for p in call.data["partial_results"] for id in p["evidence_ids"]
                    ],
                }
            case _:
                raise AssertionError(f"Unexpected model call: {call.purpose}")


class RestrictedReads:
    """Test-only permission ceiling around SDK snapshots; MemoryStorage has no ACLs."""

    def __init__(self, reader: Reader, allowed: set[str]):
        self.reader, self.allowed = reader, allowed

    def __getattr__(self, name: str) -> Any:
        return getattr(self.reader, name)

    def query(self, query: Query) -> Page:
        if query.data_type not in self.allowed or query.unreferenced_by:
            raise PermissionDenied("test denied")
        return self.reader.query(query)

    def count(self, query: Query) -> int:
        raise PermissionDenied("test denied")

    def get(self, id: str):
        record = self.reader.get(id)
        if record is not None and record.data_type not in self.allowed:
            raise PermissionDenied("test denied")
        return record


class RestrictedClient(MemoryClient):
    def __init__(self, storage: MemoryStorage, *, allow_transcript: bool = True):
        super().__init__(storage, "extraction")
        self.allowed = {"rule", "activity", "artifact", "rule_selection", "action_suggestion"}
        if allow_transcript:
            self.allowed.add("transcript_segment")

    @contextmanager
    def snapshot(self) -> Iterator[Reader]:
        with super().snapshot() as reader:
            yield cast(Reader, RestrictedReads(reader, self.allowed))


class World:
    def __init__(self, **kwargs):
        self.store = MemoryStorage(fixture_registry(), duplicate_delivery=True)
        self.client = self.store.client("extraction")
        self.gateway = CapturingGateway()
        self.service = ExtractionService(RestrictedClient(self.store), self.gateway, **kwargs)
        self.service.subscribe()

    def command(self, command: Command):
        run_id = submit(self.store, command)
        self.service.process(run_id)
        run = self.client.get_system_record(run_id)
        assert run is not None
        return run

    def extract(self, interval: Interval = RANGE):
        return self.command(Command(kind="extract", interval=interval))

    def add_rule(self, instruction: str = "Extract the decisions", **fields: Any) -> str:
        id = new_id()
        rule = Rule.model_validate(
            {"scope": "Synthetic scope", "instruction": instruction, **fields}
        )
        self.client.write([Create("rule", id, rule.model_dump())], Context.system()).check()
        return id

    def seed(
        self,
        texts: list[str],
        *,
        with_gps_and_voiceprint: bool = False,
        interval: Interval = RANGE,
        stride: float = 1,
    ) -> tuple[list[NewRawRecord], list[str]]:
        refs = {
            kind: new_id() for kind in ("audio_ref", "voiceprint", "local_speaker", "gps_point")
        }
        person_id, place_id = new_id(), new_id()
        rows = [
            make_raw(
                "transcript_segment",
                {
                    "text": text,
                    "audio_ref_id": refs["audio_ref"],
                    "voiceprint_id": refs["voiceprint"],
                    "local_speaker_id": refs["local_speaker"],
                    "language": "private-language",
                    "person_id": person_id,
                    "place_id": place_id,
                    "upstream": {"capture_id": "private-capture"},
                    "path": "/private/audio.wav",
                    "bytes": "private-audio-bytes",
                    "embedding": [0.722331],
                    "phone_info": "private-phone-in-transcript-metadata",
                },
                start=interval.start + timedelta(seconds=i * stride, microseconds=123456),
                end=interval.start + timedelta(seconds=i * stride, microseconds=654321),
                device="private-device",
                dedup_key=f"private-dedup-{i}",
                adapter_version="private-adapter-version",
            )
            for i, text in enumerate(texts)
        ]
        extras = [
            make_raw(kind, {"hidden": f"private-{kind}"}, id=id, start=RANGE.start)
            for kind, id in refs.items()
            if with_gps_and_voiceprint or kind not in {"gps_point", "voiceprint"}
        ] + [
            make_raw(kind, {"hidden": f"private-{kind}"}, start=RANGE.start)
            for kind in ("phone_info", "ambient_sound")
        ]
        self.store.client("adapter").write(
            [AppendRaw(r) for r in [*reversed(rows), *extras]], Context.system()
        ).check()
        self.store.client("attribution").write(
            [
                Create("person", person_id, {"name": "private-person"}),
                Create("place", place_id, {"name": "private-place"}),
            ],
            Context.system(),
        ).check()
        return rows, [*refs.values(), person_id, place_id, *[r.id for r in extras]]


def assert_private_fields_absent(calls: list[Call], private_ids: list[str]):
    assert calls
    wire = json.dumps([c.model_dump(mode="json") for c in calls], ensure_ascii=False)
    assert "private-" not in wire and "/private/" not in wire and "0.722331" not in wire
    assert all(id not in wire for id in private_ids)


def projected(row: NewRawRecord, *, links: bool = True) -> dict[str, Any]:
    values = {
        "text": row.payload["text"],
        "event_start": format_utc(row.event_start),
        "event_end": format_utc(row.event_end),
    }
    return {"id": row.id, **values} if links else values


def watch_snapshot(w: World, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    original_snapshot = w.service.storage.snapshot
    original_complete = w.gateway.complete
    reads: list[tuple[str, Any]] = []
    closed = False

    @contextmanager
    def snapshot() -> Iterator[Reader]:
        nonlocal closed
        assert not w.gateway.calls and not closed, "unexpected snapshot"
        with original_snapshot() as reader:

            class ReadProbe:
                def __getattr__(self, name: str):
                    def read(*args, **kwargs):
                        assert not closed and not w.gateway.calls, "late snapshot read"
                        reads.append((name, args[0] if args else None))
                        return getattr(reader, name)(*args, **kwargs)

                    return read

            yield cast(Reader, ReadProbe())
        closed = True

    def complete(call: Call):
        assert closed, "snapshot must be released before any model call"
        return original_complete(call)

    monkeypatch.setattr(w.service.storage, "snapshot", snapshot)
    monkeypatch.setattr(w.gateway, "complete", complete)
    return reads


def test_full_transcripts_only_actual_product_links_and_dependencies(monkeypatch):
    w = World(transcript_only=True)
    reads = watch_snapshot(w, monkeypatch)
    texts = [f"  合成逐字稿 {i}\n末尾 {i}。  " for i in range(103)]  # crosses SDK pagination
    texts[-1] += "合成文本内包含的人名、地点不会被删掉。"
    raw, private_ids = w.seed(texts, with_gps_and_voiceprint=True)
    rule = w.add_rule()
    action_rule = new_id()
    w.client.write(
        [
            Create(
                "rule",
                action_rule,
                Rule(
                    scope="private-action-rule", instruction="remind", action="reminder"
                ).model_dump(),
            )
        ],
        Context.system(),
    ).check()
    (run_id,) = w.service.schedule(RANGE)
    w.service.drain()
    run = w.client.get_system_record(run_id)
    assert run is not None and run.payload["status"] == "succeeded", run
    assert [c.purpose for c in w.gateway.calls] == ["segmentation", "selection", "generation"]
    assert [arg for method, arg in reads if method == "type_revision"] == ["rule"]
    assert [arg.data_type for method, arg in reads if method == "query"] == [
        "transcript_segment",
        "transcript_segment",
        "rule",
    ]
    assert_private_fields_absent(w.gateway.calls, [*private_ids, action_rule])
    segment, select, gen = [c.data for c in w.gateway.calls]
    assert segment == {
        "interval": RANGE.model_dump(mode="json"),
        "evidence": [projected(r) for r in raw],
    }
    assert select == {
        "activity": segment["interval"],
        "rules": [{"id": rule, "scope": "Synthetic scope"}],
        "evidence": [projected(r, links=False) for r in raw],
    }
    assert gen == {
        "activity": segment["interval"],
        "rule": {"instruction": "Extract the decisions"},
        "evidence": [projected(r) for r in raw],
    }
    assert len(run.payload["gateway_calls"]) == 3 and run.payload["model_calls"] == []
    assert not any(text.strip() in json.dumps(run.payload, ensure_ascii=False) for text in texts)
    activity, selection, artifact = (
        records(w.client, kind)[0] for kind in ("activity", "rule_selection", "artifact")
    )
    transcript_ids = {r.id for r in raw}
    assert activity.payload["evidence_ids"] == [raw[0].id]
    assert artifact.payload["evidence_ids"] == [r.id for r in raw]
    assert selection.payload["rule_ids"] == [rule]
    expected = {
        activity.id: transcript_ids,
        selection.id: transcript_ids | {activity.id, rule, action_rule},
        artifact.id: transcript_ids | {activity.id, rule},
    }
    for record in (activity, selection, artifact):
        refs = [d.depends_on for d in w.client.dependencies_of(record.id)]
        assert {d.id for d in refs} == expected[record.id]
        assert all(d.version == 1 for d in refs)
    # Resolve the exact IDs/version via the storage operation used by product record detail.
    api_reader = w.store.client("api")
    for source in raw:
        row = api_reader.get_version(source.id, 1)
        assert isinstance(row, RawRecord) and row.data_type == "transcript_segment"
        assert row.payload == source.payload
        assert (row.event_start, row.event_end) == (source.event_start, source.event_end)
    assert len(list(iter_query(api_reader, Query("transcript_segment")))) == len(raw)
    assert not records(w.client, "action_suggestion")


def test_default_waits_on_attribution_and_geocode_profile_skips_both():
    w = World(transcript_only=True)
    _, private_ids = w.seed(["Actual synthetic text"], with_gps_and_voiceprint=True)
    w.add_rule()
    default_gateway = CapturingGateway()
    default = ExtractionService(w.client, default_gateway)
    (default_id,) = default.schedule(RANGE)
    default.process(default_id)
    run = w.client.get_system_record(default_id)
    assert run is not None and run.payload["status"] == "waiting_attribution"
    assert not default_gateway.calls
    voiceprint, gps = private_ids[1], private_ids[3]
    person, place = private_ids[4:6]
    w.store.client("attribution").write(
        [
            Create(
                "attribution",
                new_id(),
                {"subject_id": subject, "target_id": target, "target_type": kind},
            )
            for subject, target, kind in [(voiceprint, person, "person"), (gps, place, "place")]
        ],
        Context.system(),
    ).check()
    second_id = submit(w.store, Command(kind="extract", interval=RANGE))
    default.process(second_id)
    run = w.client.get_system_record(second_id)
    assert run is not None and run.payload["status"] == "waiting_attribution"
    assert not default_gateway.calls
    assert w.extract().payload["status"] == "succeeded"
    assert_private_fields_absent(w.gateway.calls, private_ids)


@pytest.mark.parametrize("bad_input", ["absent", "malformed", "permission_denied"])
def test_missing_malformed_or_unauthorized_transcript_never_calls_model(bad_input: str):
    w = World(transcript_only=True, max_attempts=1)
    if bad_input == "malformed":
        w.store.client("adapter").write(
            [AppendRaw(make_raw("transcript_segment", {"text": ["not text"]}, start=RANGE.start))],
            Context.system(),
        ).check()
    if bad_input == "permission_denied":
        w.seed(["Valid but denied transcript"])
        w.service.storage = RestrictedClient(w.store, allow_transcript=False)
    run = w.extract()
    assert (
        run.payload["error"]
        == {
            "absent": "transcript_evidence_unavailable",
            "malformed": "transcript_evidence_invalid",
            "permission_denied": "PermissionDenied",
        }[bad_input]
    )
    assert not w.gateway.calls and not records(w.client, "activity")


def test_reextract_previews_first_and_does_not_send_old_content(monkeypatch):
    w = World(transcript_only=True)
    w.seed(["Current transcript"])
    w.add_rule()
    old_id = new_id()
    w.client.write(
        [
            Create(
                "activity",
                old_id,
                {"title_md": "private-old-activity", "evidence_ids": [new_id()]},
                event_start=RANGE.start,
                event_end=RANGE.end,
            )
        ],
        Context.system(),
    ).check()
    stale = w.command(Command(kind="reextract", interval=RANGE, preview={}))
    assert stale.payload["error"] == "reextract_preview_changed"
    assert not w.gateway.calls
    preview = w.service.preview_reextract(RANGE)
    reads = watch_snapshot(w, monkeypatch)
    run = w.command(Command(kind="reextract", interval=RANGE, preview=preview["versions"]))
    assert run.payload["status"] == "succeeded", run.payload
    assert ("get", old_id) in reads
    assert "rule_selection" in [arg.data_type for method, arg in reads if method == "query"]
    assert_private_fields_absent(w.gateway.calls, [old_id])
    old = w.client.get(old_id)
    assert isinstance(old, SemanticRecord) and old.superseded


@pytest.mark.parametrize("merge", [False, True])
def test_long_input_uses_original_transcript_windows_or_fails_closed(merge: bool):
    w = World(transcript_only=True, max_input_chars=1420, transcript_max_calls=12)
    raw, private_ids = w.seed([f"text {i}: " + "长" * 445 for i in range(4)])
    w.add_rule("instruction: " + "i" * 410)
    w.gateway.merge = merge
    run = w.extract()
    calls = w.gateway.calls
    assert_private_fields_absent(calls, private_ids)
    assert [r for c in calls if c.purpose == "segmentation" for r in c.data["evidence"]] == [
        projected(r) for r in raw
    ]
    (merge_call,) = [c for c in calls if c.purpose == "merge_activities"]
    for item in merge_call.data.values():
        assert set(item) == {"start", "end", "title_md", "description_md"}
    assert merge_call.data["right"]["start"] == format_utc(raw[2].event_start)
    if merge:
        # No partial rule selections, silent text truncation, or partial commits.
        assert run.payload["error"] == "InputBudgetExceeded"
        assert [c.purpose for c in calls] == ["segmentation", "segmentation", "merge_activities"]
        assert not records(w.client, "activity")
        return
    assert run.payload["status"] == "succeeded", run.payload
    assert [r for c in calls if c.purpose == "generation" for r in c.data["evidence"]] == [
        projected(r) for r in raw
    ]
    assert [r for c in calls if c.purpose == "selection" for r in c.data["evidence"]] == [
        projected(r, links=False) for r in raw
    ]
    parts = [p for c in calls if c.purpose == "consolidation" for p in c.data["partial_results"]]
    assert parts == [{"body_md": "Synthetic result", "evidence_ids": [r.id]} for r in raw]


def test_oversized_single_transcript_is_not_truncated_or_sent_as_empty_window():
    w = World(transcript_only=True, max_input_chars=1000)
    w.seed(["超长" * 1000])
    run = w.extract()
    assert run.payload["status"] == "failed" and run.payload["error"] == "InputBudgetExceeded"
    assert not w.gateway.calls and not records(w.client, "activity")


def test_forged_evidence_is_rejected_before_consolidation():
    w = World(transcript_only=True, max_input_chars=1420)
    _, private_ids = w.seed([f"text {i}: " + "x" * 445 for i in range(2)])
    w.add_rule("instruction: " + "i" * 410)
    w.gateway.forged_evidence_id = private_ids[0]
    run = w.extract()
    assert run.payload["status"] == "failed"
    assert [c.purpose for c in w.gateway.calls] == ["segmentation", "selection", "generation"]
    assert_private_fields_absent(w.gateway.calls, private_ids)
    assert run.payload["model_calls"] == [] and not records(w.client, "artifact")


def test_legacy_commands_and_both_reevaluation_targets_fail_closed():
    w = World(transcript_only=True)
    rows, _ = w.seed(["Synthetic text"])
    rule_id = w.add_rule()
    assert w.extract().payload["status"] == "succeeded"
    w.gateway.calls.clear()
    activity = records(w.client, "activity")[0]
    for command in (
        Command(kind="apply_rule", target_id=activity.id, rule_id=rule_id),
        Command(kind="extract_once", target_id=activity.id, rule=Rule(scope="s", instruction="i")),
        Command(
            kind="review_suggestion", target_id=new_id(), decision="approved", expected_version=1
        ),
        Command(
            kind="action_result",
            target_id=new_id(),
            expected_version=1,
            outcome="failed",
            receipt="synthetic",
        ),
    ):
        assert w.command(command).payload["error"] == "transcript_only_command_unsupported"
    for kind in ("artifact", "rule_selection"):
        target = records(w.client, kind)[0]
        emitted = (
            w.store.client("dependency")
            .write(
                [
                    PublishReevaluation(
                        kind,
                        target.id,
                        Cause(
                            data_type="transcript_segment",
                            data_id=rows[0].id,
                            old_version=None,
                            new_version=1,
                        ),
                        "private-description",
                    )
                ],
                Context.system().deeper(),
            )
            .check()
        )
        event = emitted.events[0]
        w.store.run_until_idle()
        run_id = reevaluation_run_id(event.event_id)
        w.service.process(run_id)
        run = w.client.get_system_record(run_id)
        assert run is not None and run.payload["error"] == "transcript_only_command_unsupported"
        finish = w.client.get_reevaluation_result(event.event_id)
        assert finish is not None and finish.outcome == "failed"
    assert not w.gateway.calls
    assert not records(w.client, "action_suggestion")


@pytest.mark.parametrize("row_count", [219, 142, 212])
def test_full_600_second_capture_uses_only_exact_operator_rule(row_count, monkeypatch):
    interval = Interval(start=RANGE.start, end=RANGE.start + timedelta(seconds=600))
    rule_id = new_id()
    w = World(transcript_only=True, transcript_summary_rule_id=rule_id, lease_seconds=1900)
    old = w.add_rule("private-older-test-instruction", scope="private-older-test-scope")
    old_action = w.add_rule("private-action-text", action="calendar")
    rule = Rule(scope="fixed-scope-not-needed", instruction="Produce an evidence-grounded note")
    w.client.write(
        [Create("rule", rule_id, {**rule.model_dump(), "instruction": "private-old-version"})],
        Context.system(),
    ).check()
    w.client.write([Update("rule", rule_id, 1, rule.model_dump())], Context.system()).check()
    rows, private_ids = w.seed(
        [f"合成逐字稿 {i}：这段是完整测试输入，不截断。" for i in range(row_count)],
        interval=interval,
        stride=600 / row_count,
        with_gps_and_voiceprint=True,
    )
    reads = watch_snapshot(w, monkeypatch)
    run = w.extract(interval)
    assert run.payload["status"] == "succeeded", run.payload
    assert [c.purpose for c in w.gateway.calls] == ["segmentation", "generation"]
    assert [arg for method, arg in reads if method == "type_revision"] == ["rule"]
    assert [arg for method, arg in reads if method == "get"] == [rule_id]
    assert all(arg.data_type == "transcript_segment" for method, arg in reads if method == "query")
    assert_private_fields_absent(w.gateway.calls, [old, old_action, *private_ids])
    segment_call, generation_call = w.gateway.calls
    assert segment_call.data["interval"] == interval.model_dump(mode="json")
    assert "fixed-scope-not-needed" not in generation_call.model_dump_json()
    assert generation_call.data["rule"] == {"instruction": rule.instruction}
    assert "leave action null" in generation_call.data["supplemental_instructions"]
    for call in w.gateway.calls:
        assert call.data["evidence"] == [projected(r) for r in rows]
        assert (call.root_feedback_id, call.depth) == (
            run.payload["root_feedback_id"],
            run.payload["depth"],
        )
    (activity,) = records(w.client, "activity")
    (selection,) = records(w.client, "rule_selection")
    (artifact,) = records(w.client, "artifact")
    assert selection.payload == {"activity_id": activity.id, "rule_ids": [rule_id]}
    assert (artifact.payload["rule_id"], artifact.payload["rule_version"]) == (rule_id, 2)
    assert artifact.payload["evidence_ids"] == [r.id for r in rows]
    expected = {(r.id, 1) for r in rows} | {(activity.id, 1), (rule_id, 2)}
    for record in selection, artifact:
        refs = [d.depends_on for d in w.client.dependencies_of(record.id)]
        assert {(d.id, d.version) for d in refs} == expected
    assert not records(w.client, "action_suggestion")


@pytest.mark.parametrize("state", ["missing", "superseded", "disabled", "scoped", "action"])
def test_invalid_operator_rule_blocks_before_model_egress(state):
    rule_id = new_id()
    w = World(transcript_only=True, transcript_summary_rule_id=rule_id)
    w.seed(["Synthetic transcript"])
    w.add_rule("Another eligible rule must not be used as fallback")
    if state != "missing":
        rule = Rule(
            scope="all",
            instruction="summarize",
            enabled=state != "disabled",
            activity_id=new_id() if state == "scoped" else None,
            action="reminder" if state == "action" else None,
        )
        w.client.write([Create("rule", rule_id, rule.model_dump())], Context.system()).check()
        if state == "superseded":
            w.client.write([Supersede("rule", rule_id, 1)], Context.system()).check()
    run = w.extract()
    assert run.payload["status"] == "blocked"
    assert run.payload["error"] == (
        "transcript_summary_rule_missing_or_superseded"
        if state in {"missing", "superseded"}
        else "transcript_summary_rule_ineligible"
    )
    assert not w.gateway.calls and run.payload["gateway_calls"] == []
    assert not records(w.client, "activity")


def test_concurrent_operator_rule_edit_rejects_entire_batch_without_resending(monkeypatch):
    rule_id = new_id()
    w = World(transcript_only=True, transcript_summary_rule_id=rule_id)
    w.seed(["Synthetic transcript"])
    rule = Rule(scope="all", instruction="Use snapshot version")
    w.client.write([Create("rule", rule_id, rule.model_dump())], Context.system()).check()
    original_complete = w.gateway.complete

    def complete(call: Call):
        if call.purpose == "segmentation":
            w.client.write(
                [Update("rule", rule_id, 1, {**rule.model_dump(), "instruction": "Later edit"})],
                Context.system(),
            ).check()
        return original_complete(call)

    monkeypatch.setattr(w.gateway, "complete", complete)
    run = w.extract()
    assert run.payload["status"] == "blocked"
    assert run.payload["error"] == "gateway_result_not_committed"
    assert [c.purpose for c in w.gateway.calls] == ["segmentation", "generation"]
    assert w.gateway.calls[1].data["rule"] == {"instruction": rule.instruction}
    for kind in ("activity", "rule_selection", "artifact"):
        assert not records(w.client, kind)
    w.service.drain()
    assert len(w.gateway.calls) == 2


def test_default_transcript_call_cap_stops_before_sixth_intent_or_send():
    w = World(transcript_only=True, max_input_chars=1420)
    w.seed([f"text {i}: " + "长" * 445 for i in range(4)])
    w.add_rule("instruction: " + "i" * 410)
    run = w.extract()
    assert run.payload["status"] == "failed"
    assert run.payload["error"] == "transcript_max_calls_exceeded"
    assert len(w.gateway.calls) == len(run.payload["gateway_calls"]) == 5
    assert all(intent["state"] == "returned" for intent in run.payload["gateway_calls"])
    assert run.payload["model_calls"] == []
    for kind in ("activity", "rule_selection", "artifact"):
        assert not records(w.client, kind)
    w.service.drain()
    with pytest.raises(ValueError, match="gateway_result_not_committed"):
        w.service.retry(run.id)
    assert len(w.gateway.calls) == 5


def test_call_cap_does_not_apply_to_default_profile():
    w = World(transcript_max_calls=1)
    w.service.storage = w.client
    w.seed(["Synthetic transcript"])
    w.add_rule()
    run = w.extract()
    assert run.payload["status"] == "succeeded", run.payload
    assert [c.purpose for c in w.gateway.calls] == ["segmentation", "selection", "generation"]


def test_deployment_window_guard_blocks_before_model():
    world = World(
        transcript_only=True,
        transcript_allowed_windows=(
            Interval(start=RANGE.start, end=RANGE.start + timedelta(minutes=10)),
        ),
    )
    world.seed(["synthetic transcript"])
    run = world.extract()
    assert run.payload["status"] == "blocked"
    assert run.payload["error"] == "transcript_interval_not_allowed"
    assert world.gateway.calls == []
