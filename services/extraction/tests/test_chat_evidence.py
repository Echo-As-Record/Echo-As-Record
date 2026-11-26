"""Opt-in chat evidence in the native general flow, with a fake model and the real SDK.

No network, credentials, files or real conversations: records are synthetic, the model is a
deterministic fake that only answers from the evidence rows it is shown, and storage is the
in-memory SDK with test-only read ceilings (MemoryStorage has no ACLs of its own).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from ear_contracts import Cause, RawRecord, new_id
from ear_contracts.optional_sources import chat_specs
from ear_extraction.chat_evidence import (
    ASSISTANT_CLAIM,
    CHAT_EVIDENCE_POLICY,
    CHAT_TYPES,
    CONTEXT_ONLY,
    USER_PROVIDED,
    read_chat_evidence,
)
from ear_extraction.course_reextract import ScopedTranscriptGuard
from ear_extraction.demo import fixture, fixture_registry, submit
from ear_extraction.domain import Blocked, Command, Interval, Rule
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import ExtractionService, reevaluation_run_id
from ear_extraction.persistence import records
from ear_extraction.planning import extract_plan
from ear_extraction.segmentation import raw_inputs
from ear_llm_gateway import CallRequest, GatewayClient
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    MemoryStorage,
    PermissionDenied,
    PublishReevaluation,
    Query,
    Reader,
    SnapshotUnavailable,
    Update,
)
from ear_storage_client.memory import MemoryClient
from ear_storage_client.testing import make_raw, ts

START = ts("2026-11-05T09:00:00Z")
SCOPE = Interval(start=START, end=START + timedelta(hours=1))
ADAPTER = "librechat-chat/0.1.0"
BASIC_PURPOSES = ["segmentation", "selection", "generation"]


def at(minutes: float = 0):
    return START + timedelta(minutes=minutes)


def sha(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


# -- synthetic chat records, written the way the LibreChat adapter writes them ---------------
def put(store: MemoryStorage, record) -> RawRecord:
    store.client("adapter").write([AppendRaw(record)], Context.system()).check()
    return record


def chat(kind: str, when, payload: dict[str, Any], key: str, **kw):
    return make_raw(
        kind,
        payload,
        start=when,
        channel="librechat",
        device=kw.pop("device", "librechat"),
        adapter_version=ADAPTER,
        dedup_key=key,
        **kw,
    )


def message(store, when, role, text, *, mid, cid="c1", parent=None, revision=1, key=None, **extra):
    payload: dict[str, Any] = {
        "text": text,
        "role": role,
        "conversation_id": cid,
        "message_id": mid,
        "revision": revision,
        "content_sha256": sha(cid, mid, revision, text),
        **extra,
    }
    if parent is not None:
        payload["parent_message_id"] = parent
    return put(
        store,
        chat("chat_message", when, payload, key or f"librechat:message:{cid}:{mid}:r{revision}"),
    )


def attachment(
    store, when, fid, text, *, mid, cid="c1", origin="upload", revision=1, key=None, **extra
):
    payload: dict[str, Any] = {
        "text": text,
        "text_kind": "extracted",
        "conversation_id": cid,
        "message_id": mid,
        "file_id": fid,
        "filename": "SECRET-FILENAME.pdf",
        "media_type": "application/pdf",
        "size": 2048,
        "origin": origin,
        "available": True,
        "file_ref": {
            "path": "librechat/blobs/3f/2a/" + "3f2a" * 16,
            "sha256": "3f2a" * 16,
            "size": 2048,
        },
        "revision": revision,
        "content_sha256": sha(cid, mid, fid, revision, text),
        **extra,
    }
    return put(
        store,
        chat(
            "chat_attachment", when, payload, key or f"librechat:att:{cid}:{mid}:{fid}:r{revision}"
        ),
    )


def session(store, when, title, *, cid="c1", revision=1, key=None, **extra):
    payload: dict[str, Any] = {
        "conversation_id": cid,
        "title": title,
        "tags": [],
        "revision": revision,
        "content_sha256": sha(cid, "session", revision, title),
        **extra,
    }
    return put(
        store, chat("chat_session", when, payload, key or f"librechat:session:{cid}:r{revision}")
    )


def conversation(store) -> dict[str, RawRecord]:
    """A 12-minute chat: user asks, assistant claims it is done, user follows up."""
    return {
        "session": session(store, at(0), "合成会话"),
        "u1": message(store, at(0), "user", "请帮我整理周报并保存。", mid="m1"),
        "a1": message(
            store, at(3), "assistant", "我已经整理好并保存了周报。", mid="m2", parent="m1"
        ),
        "u2": message(store, at(12), "user", "谢谢，下周一我会发给老板。", mid="m3", parent="m2"),
    }


# -- fake model ---------------------------------------------------------------------------------
def instant(value: str):
    return ts(value)


class Model:
    """Answers only from the evidence rows in the call; records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.activities: Callable[[list[dict]], list[dict]] | None = None
        self.note: Callable[[list[dict]], list[str]] | None = None
        self.end_at_last = False

    @staticmethod
    def grounded(row: dict) -> bool:
        return row.get("evidence_status", USER_PROVIDED) == USER_PROVIDED

    def complete(self, call) -> dict[str, Any]:
        self.calls.append(call)
        data = call.data
        match call.purpose:
            case "segmentation":
                rows = data["evidence"]
                if self.activities is not None:
                    found = self.activities(rows)
                    return {"activities": found, "empty_reason": "" if found else "synthetic_none"}
                last = max(instant(r["event_end"]) for r in rows)
                end = last if self.end_at_last else last + timedelta(seconds=1)
                return {
                    "activities": [
                        {
                            "start": rows[0]["event_start"],
                            "end": end.isoformat().replace("+00:00", "Z"),
                            "title_md": "Synthetic activity",
                            "evidence_ids": [r["id"] for r in rows],
                        }
                    ]
                }
            case "selection":
                return {"rule_ids": [r["id"] for r in data["rules"]]}
            case "generation" | "reevaluation":
                rows = data["evidence"]
                ids = (
                    self.note(rows)
                    if self.note
                    else [next((r for r in rows if self.grounded(r)), rows[0])["id"]]
                )
                return {"body_md": f"- {call.purpose} note", "evidence_ids": ids}
        raise AssertionError(call.purpose)


def build(*, chat_evidence=True, model: Model | None = None, client=None, store=None, **config):
    store = store or MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    model = model or Model()
    client = client or store.client("extraction")
    service = ExtractionService(
        client, model, clock=lambda: SCOPE.end, chat_evidence=chat_evidence, **config
    )
    service.subscribe()
    client.write(
        [Create("rule", new_id(), Rule(scope="synthetic", instruction="take notes").model_dump())],
        Context.system(),
    ).check()
    return store, model, service


def run(store, service, command=None):
    id = submit(store, command or Command(kind="extract", interval=SCOPE))
    service.drain()
    record = service.storage.get_system_record(id)
    assert record is not None
    return record


def items(service, kind):
    return records(service.storage, kind)


def evidence_of(model: Model, purpose="segmentation") -> list[dict]:
    return next(c for c in model.calls if c.purpose == purpose).data["evidence"]


class Ceiling(MemoryClient):
    """Test-only read ceiling and spy around snapshots; the in-memory SDK has no ACLs."""

    def __init__(
        self,
        storage,
        *,
        denied: Iterable[str] = (),
        error: type[Exception] = PermissionDenied,
    ):
        super().__init__(storage, "extraction")
        self.denied, self.error = set(denied), error
        self.queried: list[str] = []
        self.gets: list[str] = []
        self.get_many_calls = 0
        self.snapshots = 0

    @contextmanager
    def snapshot(self) -> Iterator[Reader]:
        self.snapshots += 1
        with super().snapshot() as reader:
            yield cast(Reader, _Spy(reader, self))


class _Spy:
    def __init__(self, reader: Reader, owner: Ceiling) -> None:
        self._reader, self._owner = reader, owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._reader, name)

    def _check(self, kind: str) -> None:
        self._owner.queried.append(kind)
        if kind in self._owner.denied:
            raise self._owner.error("test ceiling")

    def query(self, query: Query):
        self._check(query.data_type)
        return self._reader.query(query)

    def count(self, query: Query) -> int:
        self._check(query.data_type)
        return self._reader.count(query)

    def get(self, id: str):
        record = self._reader.get(id)
        if record is not None:
            self._owner.gets.append(record.data_type)
            if record.data_type in self._owner.denied:
                raise self._owner.error("test ceiling")
        return record

    def get_many(self, ids):
        self._owner.get_many_calls += 1
        found = self._reader.get_many(ids)
        if any(r.data_type in self._owner.denied for r in found.values()):
            raise self._owner.error("test ceiling")
        return found


# ===== default off =====
def test_flag_is_default_off_and_a_strict_bool():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    service = ExtractionService(storage.client("extraction"), Model())
    assert service.chat_evidence is False
    with pytest.raises(ValueError, match="chat_evidence_must_be_bool"):
        ExtractionService(storage.client("extraction"), Model(), chat_evidence="yes")  # type: ignore[arg-type]


def test_default_off_never_reads_or_sends_chat_and_adds_no_storage_reads():
    storage, ids = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    ceiling = Ceiling(storage, denied=CHAT_TYPES)
    model = Model()
    service = ExtractionService(ceiling, model, clock=lambda: SCOPE.end)
    service.subscribe()
    ceiling.write(
        [Create("rule", new_id(), Rule(scope="synthetic", instruction="notes").model_dump())],
        Context.system(),
    ).check()
    record = run(storage, service)
    assert record.payload["status"] == "succeeded"
    assert not set(ceiling.queried) & set(CHAT_TYPES) and not set(ceiling.gets) & set(CHAT_TYPES)
    assert ceiling.get_many_calls == 0
    # Another default-off path (applying a rule) is equally free of extra reads.
    apply = Command(
        kind="apply_rule",
        target_id=records(ceiling, "activity")[0].id,
        rule_id=records(ceiling, "rule")[0].id,
    )
    assert run(storage, service, apply).payload["status"] == "succeeded"
    assert ceiling.get_many_calls == 0
    assert not set(ceiling.queried) & set(CHAT_TYPES) and not set(ceiling.gets) & set(CHAT_TYPES)
    sent = json.dumps([c.data for c in model.calls], ensure_ascii=False)
    assert "周报" not in sent and "chat_message" not in sent and "evidence_policy" not in sent
    assert "chat_evidence" not in record.payload["detail"]
    assert ids["transcript"] in sent


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transcript_only": True},
        {"transcript_only": True, "transcript_summary_rule_id": new_id()},
        {"transcript_allowed_windows": (Interval(start=START, end=at(15)),)},
        {"transcript_course_sessions": True},
        {"course_blocks": True},
        {"transcript_course_reextract": True},
        {"transcript_course_reextract_draft_sha256": "0" * 64},
        {"transcript_course_reextract_reconciled_run_ids": (new_id(),)},
        {"transcript_automatic_timeline_prewrite_pins": (object(),)},
        {"transcript_automatic_timeline_gaps": (object(),)},
        {"transcript_automatic_timeline_asr_gaps": (object(),)},
    ],
)
def test_guarded_audio_only_flows_are_strictly_incompatible(kwargs):
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    with pytest.raises(ValueError, match="chat_evidence_requires_general_flow"):
        ExtractionService(storage.client("extraction"), Model(), chat_evidence=True, **kwargs)


@pytest.mark.parametrize(
    "guarded",
    [
        {"transcript_only": True},
        {"course_blocks": True},
        {"session_windows": (Interval(start=START, end=at(15)),)},
        {"scoped_transcript_guard": ScopedTranscriptGuard()},
    ],
    ids=["transcript_only", "course_blocks", "session_windows", "scoped_guard"],
)
def test_plan_level_guard_rejects_chat_in_a_guarded_plan(guarded):
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    command = Command(kind="extract", interval=SCOPE)
    with storage.client("extraction").snapshot() as reader:
        # Each of these would otherwise fail later with a different, flow-specific reason.
        with pytest.raises(Blocked, match="chat_evidence_requires_general_flow"):
            extract_plan(reader, command, new_id(), lambda *_: None, chat_evidence=True, **guarded)
        with pytest.raises(Blocked, match="chat_evidence_requires_general_flow"):
            raw_inputs(reader, SCOPE, transcript_only=True, chat_evidence=True)


def test_transcript_only_service_ignores_chat_in_the_same_store():
    storage, ids = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    ceiling = Ceiling(storage, denied=CHAT_TYPES)
    model = Model()
    # The guarded audio-only service: its read ceiling would raise if chat were touched.
    service = ExtractionService(ceiling, model, clock=lambda: SCOPE.end, transcript_only=True)
    service.subscribe()
    ceiling.write(
        [Create("rule", new_id(), Rule(scope="synthetic", instruction="notes").model_dump())],
        Context.system(),
    ).check()
    # The fixture transcript (10 minutes) satisfies the default activity floor.
    record = run(storage, service)
    assert record.payload["status"] == "succeeded", record.payload
    assert not set(ceiling.queried) & set(CHAT_TYPES)
    assert not set(ceiling.gets) & set(CHAT_TYPES)
    sent = json.dumps([c.data for c in model.calls], ensure_ascii=False)
    assert "周报" not in sent and "evidence_policy" not in sent
    assert ids["transcript"] in sent


def test_guarded_replacement_cannot_retire_chat_backed_activities():
    from ear_extraction.course_reextract import course_replacement_targets

    storage, _, service = build()
    conversation(storage)
    assert run(storage, service).payload["status"] == "succeeded"
    [activity] = items(service, "activity")
    assert set(activity.payload["evidence_ids"]) & {
        r.id for r in service.storage.query(Query("chat_message")).items
    }
    window = Interval(start=START, end=at(15))
    replacement = course_replacement_targets(service.storage, SCOPE, (window,))
    transcript_only_rows: list[RawRecord] = []  # What a guarded audio-only flow can read.
    with pytest.raises(Blocked, match="course_reextract_old_evidence_out_of_scope"):
        replacement.check_evidence(transcript_only_rows)


# ===== pure chat and mixed, in the same general flow =====
def test_pure_chat_runs_the_native_general_flow_with_a_closed_gateway_payload():
    storage, model, service = build()
    rows = conversation(storage)
    # Not in range, or not a conversation with in-range chat: never sent.
    session(storage, at(-30), "早前的会话", cid="c-old")
    message(storage, at(60), "user", "边界之外", mid="m-late")
    # In range, but a label for a conversation with no in-range chat: context for nothing.
    session(storage, at(5), "无消息的会话", cid="c-orphan")
    record = run(storage, service)
    assert record.payload["status"] == "succeeded", record.payload
    # Same purposes, same snapshot flow as a recording-only run; no guarded-flow purposes.
    assert [c.purpose for c in model.calls] == BASIC_PURPOSES
    detail = record.payload["detail"]
    assert detail["chat_evidence"] == {
        "messages": 3,
        "attachments": 0,
        "sessions": 1,
        "other_rows": 0,
    }
    assert detail["activities"] == 1 and "course_activities" not in detail
    assert not (set(detail) & {"covered_rows", "merged_boundaries", "unclassified_rows"})
    seen = evidence_of(model)
    assert [r["id"] for r in seen] == [rows[k].id for k in ("session", "u1", "a1", "u2")]
    assert {r["evidence_status"] for r in seen} == {USER_PROVIDED, ASSISTANT_CLAIM, CONTEXT_ONLY}
    first_user = seen[1]
    assert set(first_user) == {
        "id",
        "data_type",
        "event_start",
        "event_end",
        "evidence_status",
        "payload",
    }
    assert first_user["event_start"] == first_user["event_end"] == "2026-11-05T09:00:00Z"
    assert first_user["payload"] == {
        "conversation_id": "c1",
        "role": "user",
        "text": "请帮我整理周报并保存。",
        "message_id": "m1",
        "revision": 1,
    }
    assert seen[2]["payload"]["parent_message_id"] == "m1"
    # The chat policy rides every call; the host-owned system prompt is untouched.
    assert all(c.data["evidence_policy"] == CHAT_EVIDENCE_POLICY for c in model.calls)
    [activity] = items(service, "activity")
    assert (activity.event_start, activity.event_end) == (at(0), at(12) + timedelta(seconds=1))
    assert items(service, "artifact") and items(service, "rule_selection")
    # No chat text is copied into the run audit.
    audited = json.dumps(record.payload["model_calls"], ensure_ascii=False)
    assert "周报" not in audited and "evidence_policy" not in audited
    assert rows["u1"].id in audited


def test_pure_chat_does_not_wait_for_attribution_or_geocode():
    storage, _model, service = build()
    conversation(storage)
    record = run(storage, service)
    assert record.payload["status"] == "succeeded"
    artifact = items(service, "artifact")[0]
    deps = {d.depends_on.data_type for d in service.storage.dependencies_of(artifact.id)}
    assert deps == {"activity", "rule"}  # Raw evidence is never a registered dependency here.


def test_mixed_recording_and_chat_share_one_run_one_activity_and_the_same_dependencies():
    storage, ids = fixture(
        registry=fixture_registry().extend_core(*chat_specs())
    )  # transcript 09:00-09:10, voiceprint, attribution, person
    rows = conversation(storage)
    store, model, service = build(store=storage)
    record = run(store, service)
    assert record.payload["status"] == "succeeded", record.payload
    assert [c.purpose for c in model.calls] == BASIC_PURPOSES
    seen = evidence_of(model)
    by_id = {r["id"]: r for r in seen}
    assert by_id[ids["transcript"]]["data_type"] == "transcript_segment"
    # Recording rows keep their legacy full-dump shape; chat rows get the closed projection.
    assert "dedup_key" in by_id[ids["transcript"]]
    assert "dedup_key" not in by_id[rows["u1"].id] and "source" not in by_id[rows["u1"].id]
    assert record.payload["detail"]["chat_evidence"] == {
        "messages": 3,
        "attachments": 0,
        "sessions": 1,
        "other_rows": 1,
    }
    [activity] = items(service, "activity")
    assert {ids["transcript"], rows["u1"].id, rows["a1"].id} <= set(
        activity.payload["evidence_ids"]
    )
    artifact = items(service, "artifact")[0]
    deps = service.storage.dependencies_of(artifact.id)
    # Identical to a recording-only run: chat adds no dependency kinds of its own.
    assert {d.depends_on.data_type for d in deps} == {"activity", "rule", "attribution", "person"}
    generation = next(c for c in model.calls if c.purpose == "generation").data
    assert generation["attributions"] and generation["entities"]


def test_mixed_run_still_waits_for_recording_attribution():
    storage, _ = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    put(
        storage,
        make_raw("voiceprint", {"local_speaker": "B"}, start=at(20), end=at(21)),
    )
    store, model, service = build(store=storage)
    record = run(store, service)
    assert (record.payload["status"], record.payload["error"]) == (
        "waiting_attribution",
        "attributions_pending",
    )
    assert model.calls == []  # Chat does not bypass the existing readiness gate.


# ===== equal timing semantics =====
def test_chat_uses_the_same_half_open_point_semantics_as_other_point_data():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    inside = message(storage, START, "user", "起点", mid="a")
    last = message(storage, SCOPE.end - timedelta(milliseconds=1), "user", "末", mid="b")
    edge = message(storage, SCOPE.end, "user", "终点", mid="c")
    point_in = put(storage, make_raw("phone_info", {"battery": 1}, start=START))
    point_edge = put(storage, make_raw("phone_info", {"battery": 2}, start=SCOPE.end))
    with storage.client("extraction").snapshot() as reader:
        rows = raw_inputs(reader, SCOPE, chat_evidence=True)
    got = {r.id for r in rows}
    assert {inside.id, last.id, point_in.id} <= got
    assert edge.id not in got and point_edge.id not in got
    assert all(r.event_start == r.event_end for r in rows)


@pytest.mark.parametrize("kind", ["chat", "recording"])
@pytest.mark.parametrize(
    "seconds,kept", [(599.999, False), (600, True), (601, True)], ids=["below", "exact", "above"]
)
def test_activity_floor_measures_chat_and_recordings_alike(kind, seconds, kept):
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    if kind == "chat":
        message(storage, at(0), "user", "开始", mid="a")
        message(storage, at(0) + timedelta(seconds=seconds), "user", "结束", mid="b")
    else:
        put(
            storage,
            make_raw(
                "transcript_segment",
                {"text": "x"},
                start=at(0),
                end=at(0) + timedelta(seconds=seconds),
            ),
        )
    store, model, service = build(store=storage)
    record = run(store, service)
    assert record.payload["status"] == "succeeded", record.payload
    assert len(items(service, "activity")) == kept
    if not kept:
        assert record.payload["detail"]["below_min_duration_candidates"] == 1
        assert [c.purpose for c in model.calls] == ["segmentation"]


def test_an_activity_must_end_after_its_last_chat_instant_like_any_point_evidence():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    store, model, service = build(store=storage)
    model.end_at_last = True  # [start, end) would exclude the cited last instant.
    record = run(store, service)
    assert (record.payload["status"], record.payload["error"]) == ("failed", "ValueError")
    assert items(service, "activity") == [] and items(service, "artifact") == []


# ===== highest revision =====
def test_only_the_highest_revision_of_each_object_reaches_the_model_in_any_write_order():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    m3 = message(storage, at(1), "user", "第三版", mid="m1", revision=3)
    message(storage, at(1), "user", "第一版", mid="m1", revision=1)
    message(storage, at(1), "user", "第二版", mid="m1", revision=2)
    other = message(storage, at(1), "user", "另一会话同名消息", mid="m1", cid="c2")
    a2 = attachment(storage, at(1), "f1", "附件二", mid="m1", revision=2)
    attachment(storage, at(1), "f1", "附件一", mid="m1", revision=1)
    s2 = session(storage, at(0), "标题二", revision=2)
    session(storage, at(0), "标题一", revision=1)
    orphan = session(storage, at(5), "无消息的会话", cid="c-orphan")
    with storage.client("extraction").snapshot() as reader:
        chosen = read_chat_evidence(reader, SCOPE)
    assert {r.id for r in chosen} == {m3.id, other.id, a2.id, s2.id}
    assert orphan.id not in {r.id for r in chosen}
    store, model, service = build(store=storage)
    model.activities = lambda rows: []
    run(store, service)
    texts = {r["payload"].get("text") or r["payload"].get("title") for r in evidence_of(model)}
    assert texts == {"第三版", "另一会话同名消息", "附件二", "标题二"}
    revisions = {r["payload"]["revision"] for r in evidence_of(model)}
    assert revisions == {1, 2, 3}  # c2 is r1, attachment and session r2, message r3


def test_missing_revision_means_one_and_identical_replays_collapse():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    legacy = chat(
        "chat_message",
        at(1),
        {"text": "无修订", "role": "user", "conversation_id": "c1", "message_id": "m1"},
        "legacy",
    )
    put(storage, legacy)
    edited = message(storage, at(1), "user", "改后", mid="m1", revision=2)
    # Same object, same revision, same content, written twice under other dedup keys.
    first = message(storage, at(2), "user", "重放", mid="m2", key="k1")
    second = message(storage, at(2), "user", "重放", mid="m2", key="k2")
    assert first.id < second.id
    with storage.client("extraction").snapshot() as reader:
        chosen = {r.id for r in read_chat_evidence(reader, SCOPE)}
    # A record without revision is revision 1 and loses to r2; an identical replay
    # collapses deterministically onto the lowest id.
    assert chosen == {edited.id, first.id}


def test_conflicting_content_at_the_same_revision_blocks_before_any_model_call():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    message(storage, at(1), "user", "甲", mid="m1", revision=2, key="k1")
    message(storage, at(1), "user", "乙", mid="m1", revision=2, key="k2")
    store, model, service = build(store=storage)
    record = run(store, service)
    assert (record.payload["status"], record.payload["error"]) == (
        "blocked",
        "chat_revision_conflict",
    )
    assert model.calls == [] and items(service, "activity") == []


@pytest.mark.parametrize(
    "payload,when",
    [
        ({"text": "x", "role": "system", "conversation_id": "c", "message_id": "m"}, at(1)),
        ({"text": 1, "role": "user", "conversation_id": "c", "message_id": "m"}, at(1)),
        ({"text": "x", "role": "user", "conversation_id": "", "message_id": "m"}, at(1)),
        (
            {
                "text": "x",
                "role": "user",
                "conversation_id": "c",
                "message_id": "m",
                "revision": "2",
            },
            at(1),
        ),
        (
            {
                "text": "x",
                "role": "user",
                "conversation_id": "c",
                "message_id": "m",
                "revision": True,
            },
            at(1),
        ),
        (
            {"text": "x", "role": "user", "conversation_id": "c", "message_id": "m", "revision": 0},
            at(1),
        ),
        (
            {
                "text": "x",
                "role": "user",
                "conversation_id": "c",
                "message_id": "m",
                "content_sha256": "ABC",
            },
            at(1),
        ),
    ],
)
def test_malformed_chat_records_fail_closed_never_silently_skipped(payload, when):
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    put(storage, chat("chat_message", when, payload, "bad"))
    store, model, service = build(store=storage)
    record = run(store, service)
    assert (record.payload["status"], record.payload["error"]) == (
        "blocked",
        "chat_evidence_invalid",
    )
    assert model.calls == []


def test_a_chat_record_that_is_not_a_point_event_is_invalid():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    put(
        storage,
        make_raw(
            "chat_message",
            {"text": "x", "role": "user", "conversation_id": "c", "message_id": "m"},
            start=at(1),
            end=at(2),
            channel="librechat",
            adapter_version=ADAPTER,
        ),
    )
    store, _model, service = build(store=storage)
    assert run(store, service).payload["error"] == "chat_evidence_invalid"


# ===== assistant context is a claim, not a completion =====
def test_evidence_status_follows_a_positive_allowlist():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    attachment(storage, at(1), "up", "用户上传", mid="m1", origin="upload")
    attachment(storage, at(3), "gen", "助手生成", mid="m2", origin="generated")
    attachment(storage, at(3), "other", "来源不明", mid="m2", origin="inline")
    store, model, service = build(store=storage)
    model.activities = lambda rows: []
    run(store, service)
    status = {}
    for row in evidence_of(model):
        payload = row["payload"]
        label = payload.get("file_id") or payload.get("role") or "session"
        status[(row["data_type"], label)] = row["evidence_status"]
    assert status == {
        ("chat_session", "session"): CONTEXT_ONLY,
        ("chat_message", "user"): USER_PROVIDED,
        ("chat_message", "assistant"): ASSISTANT_CLAIM,
        ("chat_attachment", "up"): USER_PROVIDED,
        ("chat_attachment", "gen"): ASSISTANT_CLAIM,
        ("chat_attachment", "other"): ASSISTANT_CLAIM,
    }
    policy = CHAT_EVIDENCE_POLICY.lower()
    assert "claim" in policy and "never proof" in policy and "user_provided" in policy


def test_an_activity_from_assistant_claims_alone_is_withheld_not_recorded_as_done():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    rows = conversation(storage)
    store, model, service = build(store=storage)
    model.activities = lambda seen: [
        {
            "start": rows["a1"].event_start.isoformat().replace("+00:00", "Z"),
            "end": (rows["a1"].event_start + timedelta(minutes=15))
            .isoformat()
            .replace("+00:00", "Z"),
            "title_md": "已保存周报",
            "evidence_ids": [rows["a1"].id],
        }
    ]
    record = run(store, service)
    assert record.payload["status"] == "succeeded"
    assert record.payload["detail"]["claim_only_candidates"] == 1
    assert record.payload["detail"]["activities"] == 0
    assert items(service, "activity") == [] and items(service, "artifact") == []
    assert [c.purpose for c in model.calls] == ["segmentation"]


def test_assistant_claims_with_user_or_recording_support_are_kept_as_context():
    storage, ids = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    rows = conversation(storage)
    store, model, service = build(store=storage)
    model.activities = lambda seen: [
        {
            "start": "2026-11-05T09:00:00Z",
            "end": "2026-11-05T09:20:00Z",
            "title_md": "整理周报",
            "evidence_ids": [rows["a1"].id, ids["transcript"]],  # claim + recording
        }
    ]
    assert run(store, service).payload["status"] == "succeeded"
    assert len(items(service, "activity")) == 1


def test_a_note_cannot_rest_on_assistant_claims_alone():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    rows = conversation(storage)
    store, model, service = build(store=storage)
    model.note = lambda seen: [rows["a1"].id]
    record = run(store, service)
    assert (record.payload["status"], record.payload["error"]) == ("failed", "ValueError")
    assert items(service, "activity") == [] and items(service, "artifact") == []


def test_replacement_is_not_partial_when_a_claim_only_candidate_is_withheld():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    rows = conversation(storage)
    store, model, service = build(store=storage)
    assert run(store, service).payload["status"] == "succeeded"
    before = {k: items(service, k) for k in ("activity", "artifact", "rule_selection")}
    model.calls.clear()
    model.activities = lambda seen: [
        {
            "start": "2026-11-05T09:03:00Z",
            "end": "2026-11-05T09:20:00Z",
            "title_md": "仅助手陈述",
            "evidence_ids": [rows["a1"].id],
        }
    ]
    preview = service.preview_reextract(SCOPE)["versions"]
    record = run(store, service, Command(kind="reextract", interval=SCOPE, preview=preview))
    assert (record.payload["status"], record.payload["error"]) == (
        "blocked",
        "reextract_incomplete_activity_replacement",
    )
    for kind, old in before.items():
        assert items(service, kind) == old


# ===== permissions are visible, never an empty result =====
def test_missing_chat_read_grant_blocks_visibly():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    ceiling = Ceiling(storage, denied={"chat_message"})
    store, model, service = build(store=storage, client=ceiling)
    record = run(store, service)
    assert (record.payload["status"], record.payload["error"]) == (
        "blocked",
        "chat_evidence_not_authorized",
    )
    assert model.calls == [] and items(service, "activity") == []


def test_an_expired_snapshot_is_a_transient_failure_not_a_missing_grant():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    ceiling = Ceiling(storage, denied={"chat_message"}, error=SnapshotUnavailable)
    store, model, service = build(store=storage, client=ceiling)
    record = run(store, service)
    assert record.payload["status"] == "retry_wait"
    assert record.payload["error"] == "SnapshotUnavailable"
    assert model.calls == []


# ===== chat-backed work cannot be re-judged by a flow that cannot see chat =====
def _republish(storage, ids, target, target_type="artifact"):
    """Correct the attribution, then ask for a re-judgement like ⑤ would."""
    new_person = new_id()
    attributor = storage.client("attribution")
    attributor.write(
        [
            Create("person", new_person, {"name": "合成人物乙", "references": []}),
            Update(
                "attribution",
                ids["attribution"],
                1,
                {"subject_id": ids["voice"], "target_type": "person", "target_id": new_person},
            ),
        ],
        Context.system(),
    ).check()
    event_id = new_id()
    storage.client("dependency").write(
        [
            PublishReevaluation(
                target_type,
                target.id,
                Cause(
                    data_type="attribution",
                    data_id=ids["attribution"],
                    old_version=1,
                    new_version=2,
                ),
                "测试归属修正",
                event_id=event_id,
            )
        ],
        Context.system().deeper(),
    ).check()
    storage.run_until_idle()
    return reevaluation_run_id(event_id)


@pytest.mark.parametrize("enabled", [True, False])
def test_rejudging_chat_backed_notes_requires_the_chat_enabled_flow(enabled):
    storage, ids = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    store, _model, service = build(store=storage)
    assert run(store, service).payload["status"] == "succeeded"
    artifact = items(service, "artifact")[0]
    other_model = Model()
    other = ExtractionService(
        store.client("extraction"), other_model, clock=lambda: SCOPE.end, chat_evidence=enabled
    )
    run_id = _republish(store, ids, artifact)
    other.drain()
    record = store.client("extraction").get_system_record(run_id)
    assert record is not None
    if enabled:
        assert record.payload["status"] == "succeeded"
        assert record.payload["detail"]["outcome"] == "changed"
        assert items(service, "artifact")[0].version == 2
    else:
        assert (record.payload["status"], record.payload["error"]) == (
            "blocked",
            "chat_evidence_not_enabled",
        )
        assert other_model.calls == [] and items(service, "artifact")[0].version == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_reselecting_rules_for_a_chat_backed_activity_requires_the_chat_enabled_flow(enabled):
    storage, ids = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    store, _model, service = build(store=storage)
    assert run(store, service).payload["status"] == "succeeded"
    selection = items(service, "rule_selection")[0]
    other_model = Model()
    other = ExtractionService(
        store.client("extraction"), other_model, clock=lambda: SCOPE.end, chat_evidence=enabled
    )
    run_id = _republish(store, ids, selection, "rule_selection")
    other.drain()
    record = store.client("extraction").get_system_record(run_id)
    assert record is not None
    if enabled:
        assert record.payload["status"] == "succeeded"
        assert [c.purpose for c in other_model.calls] == ["selection"]
        assert other_model.calls[0].data["evidence"] == []  # Selection never gets chat text.
    else:
        assert (record.payload["status"], record.payload["error"]) == (
            "blocked",
            "chat_evidence_not_enabled",
        )
        assert other_model.calls == []


@pytest.mark.parametrize("enabled", [True, False])
def test_applying_a_rule_to_a_chat_backed_activity_requires_the_chat_enabled_flow(enabled):
    storage, _ = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    store, _model, service = build(store=storage)
    assert run(store, service).payload["status"] == "succeeded"
    other_model = Model()
    other = ExtractionService(
        store.client("extraction"), other_model, clock=lambda: SCOPE.end, chat_evidence=enabled
    )
    command = Command(
        kind="extract_once",
        target_id=items(service, "activity")[0].id,
        rule=Rule(scope="仅本次", instruction="整理"),
    )
    run_id = submit(store, command)
    other.drain()
    record = store.client("extraction").get_system_record(run_id)
    assert record is not None
    if enabled:
        assert record.payload["status"] == "succeeded"
        assert len(items(service, "artifact")) == 2
    else:
        assert (record.payload["status"], record.payload["error"]) == (
            "blocked",
            "chat_evidence_not_enabled",
        )
        assert other_model.calls == [] and len(items(service, "artifact")) == 1


def test_flag_off_with_revoked_chat_grants_still_fails_closed_on_chat_backed_work():
    storage, _ = fixture(registry=fixture_registry().extend_core(*chat_specs()))
    conversation(storage)
    store, _model, service = build(store=storage)
    assert run(store, service).payload["status"] == "succeeded"
    other_model = Model()
    revoked = Ceiling(store, denied=CHAT_TYPES)
    other = ExtractionService(revoked, other_model, clock=lambda: SCOPE.end)
    run_id = submit(
        store,
        Command(
            kind="apply_rule",
            target_id=items(service, "activity")[0].id,
            rule_id=items(service, "rule")[0].id,
        ),
    )
    other.drain()
    record = store.client("extraction").get_system_record(run_id)
    assert record is not None
    assert (record.payload["status"], record.payload["error"]) == (
        "blocked",
        "chat_evidence_not_enabled",
    )
    assert other_model.calls == [] and len(items(service, "artifact")) == 1


# ===== the exact payload that crosses the gateway =====
def test_gateway_request_body_is_closed_and_leaks_no_adapter_extras():
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    secrets = {
        "reasoning": "SECRET-REASONING",
        "tool_calls": [{"name": "SECRET-TOOL", "args": "SECRET-ARGS", "output": "SECRET-OUT"}],
        "sender": "SECRET-SENDER",
        "model": "SECRET-MODEL",
        "endpoint": "SECRET-ENDPOINT",
        "attachment_ids": ["SECRET-ATTACHMENT-ID"],
        "text_file_ref": {"path": "librechat/blobs/SECRET-TEXT-REF", "sha256": "0" * 64, "size": 1},
        "source_system": "SECRET-SOURCE-SYSTEM",
        "source_updated_at": "2026-11-05T09:30:00.000Z",
    }
    session(
        storage,
        at(0),
        "合成会话",
        agent_id="SECRET-AGENT",
        tags=["SECRET-TAG"],
        source_created_at="SECRET-CREATED",
    )
    message(storage, at(0), "user", "请看附件并整理。", mid="m1", **secrets)
    message(
        storage, at(3), "assistant", "已完成。", mid="m2", parent="m1", unfinished=True, **secrets
    )
    attachment(storage, at(0), "f1", "附件正文", mid="m1", source_context="SECRET-CONTEXT")
    message(storage, at(12), "user", "谢谢", mid="m3", parent="m2")
    seen: list[CallRequest] = []
    model = Model()

    def respond(request: httpx.Request) -> httpx.Response:
        body = CallRequest.model_validate_json(request.content)
        seen.append(body)
        output = model.complete(SimpleNamespace(purpose=body.purpose, data=body.data))
        return httpx.Response(
            200, json={"call_id": body.call_id, "record_saved": True, "output": output}
        )

    with GatewayClient(
        "https://module-seven.invalid",
        "t" * 43,
        system=SYSTEM,
        transport=httpx.MockTransport(respond),
    ) as gateway:
        service = ExtractionService(
            storage.client("extraction"), gateway, clock=lambda: SCOPE.end, chat_evidence=True
        )
        service.subscribe()
        service.storage.write(
            [Create("rule", new_id(), Rule(scope="s", instruction="notes").model_dump())],
            Context.system(),
        ).check()
        record = run(storage, service)
    assert record.payload["status"] == "succeeded", record.payload
    assert [c.purpose for c in seen] == BASIC_PURPOSES
    assert all(c.module == "extraction" and c.system == SYSTEM for c in seen)
    assert all(c.data["evidence_policy"] == CHAT_EVIDENCE_POLICY for c in seen)
    wire = " ".join(c.model_dump_json() for c in seen)
    for leaked in ("SECRET", "librechat/blobs", "file_ref", "filename"):
        assert leaked not in wire, leaked
    # Record envelopes (device, channel, dedup key) never accompany chat evidence rows.
    evidence_wire = json.dumps([r for c in seen for r in c.data["evidence"]], ensure_ascii=False)
    for leaked in ("dedup_key", "librechat:", "adapter_version", "source", "ingested_at"):
        assert leaked not in evidence_wire, leaked
    for needed in ("请看附件并整理。", "附件正文", "已完成。", "合成会话"):
        assert needed in wire
    segmentation = seen[0].data
    assert set(segmentation) == {"interval", "evidence", "evidence_policy"}
    by_type: dict[str, list[dict]] = {}
    for row in segmentation["evidence"]:
        by_type.setdefault(row["data_type"], []).append(row)
        assert set(row) == {
            "id",
            "data_type",
            "event_start",
            "event_end",
            "evidence_status",
            "payload",
        }
    assert set(by_type) == set(CHAT_TYPES)
    assert {frozenset(r["payload"]) for r in by_type["chat_message"]} == {
        frozenset({"conversation_id", "role", "text", "message_id", "revision"}),
        frozenset(
            {"conversation_id", "role", "text", "message_id", "parent_message_id", "revision"}
        ),
        frozenset(
            {
                "conversation_id",
                "role",
                "text",
                "message_id",
                "parent_message_id",
                "revision",
                "unfinished",
            }
        ),
    }
    assert set(by_type["chat_attachment"][0]["payload"]) == {
        "conversation_id",
        "text",
        "message_id",
        "file_id",
        "text_kind",
        "revision",
    }
    assert set(by_type["chat_session"][0]["payload"]) == {
        "conversation_id",
        "title",
        "revision",
    }
    assert seen[1].data.keys() >= {"activity", "rules", "evidence", "evidence_policy"}
    assert seen[1].data["evidence"] == []  # Selection never receives conversation text.
    assert set(seen[2].data) == {
        "activity",
        "rule",
        "evidence",
        "attributions",
        "entities",
        "evidence_policy",
    }
    # The durable intent keeps only a fingerprint, never the request or reply body.
    for intent in record.payload["gateway_calls"]:
        assert set(intent) == {"call_id", "purpose", "request_fingerprint", "state"}
        assert intent["state"] == "returned"
    assert "请看附件并整理。" not in json.dumps(record.payload, ensure_ascii=False)
