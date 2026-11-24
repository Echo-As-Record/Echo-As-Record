"""Synthetic evidence and real ASGI/SDK tests; no sockets, services or source files."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from ear_api import evidence
from ear_api.app import create_app
from ear_api.upstream import Upstream
from ear_contracts import RawRecord, SemanticRecord, new_id
from ear_contracts.records import Source
from ear_storage_client import PermissionDenied, Query, TimeRange
from ear_storage_client.protocol import Reader
from ear_storage_client.queries import Page
from ear_storage_client.remote_errors import AuthenticationError, ProtocolError, ServiceUnavailable
from ear_storage_client.wire import decode_read, encode_read_result
from fastapi.testclient import TestClient

from .conftest import ADMIN, STORAGE_ADMIN, STORAGE_USER, USER, headers, settings

START = "2026-11-18T00:00:00Z"
END = "2026-11-19T00:00:00Z"
NOW = datetime(2026, 11, 18, tzinfo=UTC)
WINDOW = TimeRange(NOW, NOW + timedelta(days=1))
PARAMS = {"start": START, "end": END, "q": "needle"}


def raw(data_type="transcript_segment", **payload: Any) -> RawRecord:
    return RawRecord(
        id=new_id(),
        data_type=data_type,
        event_start=NOW + timedelta(seconds=10),
        event_end=NOW + timedelta(seconds=20),
        ingested_at=NOW,
        source=Source(device="synthetic", channel="test", user="default"),
        adapter_version="test",
        dedup_key=new_id(),
        payload=payload,
    )


def semantic(data_type: str, **payload: Any) -> SemanticRecord:
    return SemanticRecord(
        id=new_id(),
        data_type=data_type,
        version=1,
        status="active",
        event_start=None,
        event_end=None,
        dedup_key=None,
        payload=payload,
        writer="attribution",
        root_feedback_id="system",
        depth=0,
        skip_history=False,
        batch_id=new_id(),
        created_at=NOW,
    )


class ScriptedReader:
    """Only query/get are available: any snapshot, count or write is a test failure."""

    def __init__(self, query: Callable[[Query], Page] | None = None):
        self.queries: list[Query] = []
        self.gets: list[str] = []
        self.query_fn = query or (lambda _q: Page((), None))
        self.records: dict[str, RawRecord | SemanticRecord | None] = {}
        self.denied_types: set[str] = set()
        self.denied_ids: set[str] = set()

    def query(self, query: Query) -> Page:
        self.queries.append(query)
        if query.data_type in self.denied_types:
            raise PermissionDenied("synthetic private detail " + STORAGE_USER)
        return self.query_fn(query)

    def get(self, id: str):
        self.gets.append(id)
        if id in self.denied_ids:
            raise PermissionDenied("synthetic private detail " + STORAGE_USER)
        return self.records.get(id)


def search(reader: ScriptedReader, phrase="needle", cursor=None, limit=10):
    return evidence.search(cast(Reader, reader), WINDOW, phrase, cursor, limit)


def joined_reader() -> tuple[ScriptedReader, RawRecord, RawRecord, SemanticRecord, SemanticRecord]:
    audio = raw("audio_ref", file_ref={"path": "private/not-to-be-opened.wav"}, token=STORAGE_USER)
    audio = audio.model_copy(update={"event_start": NOW, "event_end": NOW + timedelta(seconds=30)})
    person = semantic("person", name="Synthetic Person", embedding=[0.1], token=STORAGE_USER)
    voice = new_id()
    attr = semantic("attribution", subject_id=voice, target_id=person.id, target_type="person")
    segment = raw(
        text="prefix needle suffix",
        voiceprint_id=voice,
        audio_ref_id=audio.id,
        local_speaker_id=new_id(),
        arbitrary={"token": STORAGE_USER, "embedding": [0.1]},
    )
    reader = ScriptedReader(
        lambda q: Page((segment,) if q.data_type == "transcript_segment" else (attr,), None)
    )
    reader.records = {audio.id: audio, person.id: person}
    return reader, segment, audio, attr, person


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("", END),
        (START, ""),
        (START, START),
        (END, START),
        (START, "2026-11-19T00:00:00.000001Z"),
        ("2026-11-18", END),
        (START.replace("Z", "+00:00"), END),
        (START.lower(), END),
        (START + "\n", END),
        (" " + START, END),
        ("2026-02-30T00:00:00Z", END),
        ("2026-11-18T00:00:60Z", END),
        ("２０２６-11-18T00:00:00Z", END),
        ("2026-11-18T00:00:00.1234567Z", END),
    ],
)
def test_strict_window(start, end):
    with pytest.raises(ValueError, match=r"^invalid_input$"):
        evidence.validate_window(start, end)


def test_window_endpoints_and_microseconds():
    assert evidence.validate_window(START, END) == WINDOW
    assert evidence.validate_window(START, "2026-11-18T00:00:00.000001Z").end > NOW


@pytest.mark.parametrize(
    ("phrase", "limit"),
    [("", 1), ("x" * 257, 1), (None, 1), ("x", 0), ("x", 11), ("x", True), ("x", 1.0)],
)
def test_search_rejects_invalid_input_before_reads(phrase, limit):
    reader = ScriptedReader()
    with pytest.raises(ValueError, match=r"^invalid_input$"):
        search(reader, phrase=phrase, limit=limit)
    assert not reader.queries and not reader.gets


def test_closed_projection_valid_offsets_and_no_file_access(monkeypatch):
    reader, segment, audio, attr, person = joined_reader()

    def forbidden(*args, **kwargs):
        pytest.fail("evidence must never read a file")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", forbidden)
        patch.setattr(Path, "open", forbidden)
        result = search(reader)
    assert result["consistency"] == "live_best_effort"
    assert result["truncated"] is False
    assert result["next_cursor"] is None
    assert result["report"]["stop_reason"] == "exhausted"
    assert result["report"]["calls"] == 4
    assert result["report"]["pages"] == 2
    assert result["report"]["read_rows"] == 4
    assert result["report"]["deadline_mode"] == "cooperative"
    item = result["items"][0]
    assert item == {
        "transcript_segment_id": segment.id,
        "transcript_segment_version": 1,
        "start": "2026-11-18T00:00:10Z",
        "end": "2026-11-18T00:00:20Z",
        "text": "prefix needle suffix",
        "local_speaker_id": segment.payload["local_speaker_id"],
        "voiceprint_id": segment.payload["voiceprint_id"],
        "audio_ref_id": audio.id,
        "playback": {"audio_ref_id": audio.id, "start_seconds": 10.0, "end_seconds": 20.0},
        "playback_status": "available",
        "attribution_status": "resolved",
        "attribution": {
            "id": attr.id,
            "version": 1,
            "person_id": person.id,
            "person_version": 1,
            "person_status": "active",
            "name": "Synthetic Person",
        },
    }
    encoded = json.dumps(result)
    for private in ("file_ref", "not-to-be-opened", "embedding", "arbitrary", STORAGE_USER):
        assert private not in encoded
    assert reader.queries[0].time == WINDOW
    assert reader.queries[1].where == {"subject_id": segment.payload["voiceprint_id"]}
    assert reader.queries[1].limit == 2


@pytest.mark.parametrize(
    ("audio_change", "expected"),
    [
        ({"event_start": NOW + timedelta(seconds=15)}, "invalid_offsets"),
        ({"event_end": NOW + timedelta(seconds=15)}, "invalid_offsets"),
        ({"data_type": "phone_info"}, "unavailable"),
        ({"id": new_id()}, "unavailable"),
    ],
)
def test_playback_status_does_not_invent_offsets(audio_change, expected):
    reader, _, audio, _, _ = joined_reader()
    reader.records[audio.id] = audio.model_copy(update=audio_change)
    item = search(reader)["items"][0]
    assert item["playback"] is None
    assert item["playback_status"] == expected


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"status": "superseded"}, "person_superseded"),
        ({"payload": {"name": "x" * 257}}, "invalid_name"),
        ({"payload": {"name": 123}}, "invalid_name"),
        ({"payload": {"name": None}}, "resolved"),
        ({"data_type": "place"}, "unavailable"),
        ({"id": new_id()}, "unavailable"),
    ],
)
def test_person_status(change, expected):
    reader, _, _, _, person = joined_reader()
    reader.records[person.id] = person.model_copy(update=change)
    item = search(reader)["items"][0]
    assert item["attribution_status"] == expected
    assert item["attribution"]["name"] is None


@pytest.mark.parametrize(
    "mode", ["missing", "duplicate", "continued", "superseded", "wrong_subject", "wrong_target"]
)
def test_attribution_missing_or_ambiguous(mode):
    reader, segment, _, attr, _ = joined_reader()
    attrs = (attr,)
    cursor = None
    if mode == "missing":
        attrs = ()
    elif mode == "duplicate":
        attrs = (attr, attr)
    elif mode == "continued":
        cursor = "opaque/more"
    elif mode == "superseded":
        attrs = (attr.model_copy(update={"status": "superseded"}),)
    else:
        payload = {
            **attr.payload,
            "subject_id" if mode == "wrong_subject" else "target_type": "other",
        }
        attrs = (attr.model_copy(update={"payload": payload}),)
    reader.query_fn = lambda q: (
        Page((segment,), None) if q.data_type == "transcript_segment" else Page(attrs, cursor)
    )
    item = search(reader)["items"][0]
    assert item["attribution"] is None
    assert item["attribution_status"] == (
        "ambiguous" if mode in {"duplicate", "continued"} else "unavailable"
    )


def test_no_voiceprint_missing_audio_and_bounded_literal_excerpt():
    segment = raw(text="x" * 500 + "NEEDLE .* needle" + "y" * 500, audio_ref_id=new_id())
    reader = ScriptedReader(lambda q: Page((segment,), None))
    item = search(reader, phrase=".*")["items"][0]
    assert ".*" in item["text"] and len(item["text"]) == 400
    assert item["attribution_status"] == "no_voiceprint"
    assert item["playback_status"] == "unavailable"
    assert len(reader.queries) == 1
    assert not search(reader, phrase="Needle")["items"]


@pytest.mark.parametrize("where", ["transcript_segment", "attribution", "audio", "person"])
def test_permission_errors_propagate_instead_of_empty_or_partial_success(where):
    reader, _, audio, _, person = joined_reader()
    if where in {"audio", "person"}:
        reader.denied_ids.add(audio.id if where == "audio" else person.id)
    else:
        reader.denied_types.add(where)
    with pytest.raises(PermissionDenied):
        search(reader)


@pytest.mark.parametrize("error", [AuthenticationError, ServiceUnavailable, ProtocolError])
def test_transport_errors_propagate(error):
    def fail(q):
        raise error("private diagnostic")

    with pytest.raises(error):
        search(ScriptedReader(fail))


def test_segment_budget_and_resume_after_100_nonmatches():
    rows = [raw(text="miss") for _ in range(100)] + [raw(text="needle")]
    offsets: dict[str | None, int] = {None: 0}

    def page(q):
        start = offsets[q.cursor]
        end = min(start + q.limit, len(rows))
        cursor = None if end == len(rows) else "opaque/key:" + str(end)
        if cursor:
            offsets[cursor] = end
        return Page(tuple(rows[start:end]), cursor)

    reader = ScriptedReader(page)
    first = search(reader)
    assert first["items"] == [] and first["scanned"] == 100
    assert first["report"]["stop_reason"] == "segment_limit"
    assert first["truncated"] and first["next_cursor"]
    second = search(reader, cursor=first["next_cursor"])
    assert len(second["items"]) == 1 and second["scanned"] == 1
    assert second["truncated"] is False
    assert reader.queries[-1].cursor == "opaque/key:100"


def test_result_limit_opaque_cursor_and_live_recheck():
    segment = raw(text="needle")
    reader = ScriptedReader(
        lambda q: Page((segment,), "opaque+/=next" if q.cursor is None else None)
    )
    first = search(reader, limit=1)
    assert first["report"]["stop_reason"] == "result_limit" and first["truncated"]
    assert first["next_cursor"] != "opaque+/=next"
    assert "opaque+/=next" not in json.dumps(first["report"])
    second = search(reader, cursor=first["next_cursor"], limit=1)
    assert second["truncated"] is False
    assert reader.queries[-1].cursor == "opaque+/=next"
    reader.denied_types.add("transcript_segment")
    with pytest.raises(PermissionDenied):
        search(reader, cursor=first["next_cursor"], limit=1)


def test_empty_pages_are_not_exhaustion_and_page_budget_is_bounded():
    reader = ScriptedReader()
    reader.query_fn = lambda q: Page((), "opaque:" + str(len(reader.queries)))
    result = search(reader)
    assert len(reader.queries) == evidence.MAX_PAGES == 20
    assert result["scanned"] == 0
    assert result["report"]["stop_reason"] == "page_budget"
    assert result["truncated"] and result["next_cursor"]


@pytest.mark.parametrize("incoming", [False, True])
def test_stalled_cursor_including_first_page_of_continuation(incoming):
    binding = evidence._binding(WINDOW, "needle", 1)
    cursor = evidence._encode_cursor("same", binding) if incoming else None
    reader = ScriptedReader(lambda q: Page((raw(text="needle"),) if incoming else (), "same"))
    result = search(reader, cursor=cursor, limit=1)
    assert result["report"]["stop_reason"] == "cursor_stalled"
    assert result["truncated"] and result["next_cursor"] is None
    assert len(reader.queries) == (1 if incoming else 2)


@pytest.mark.parametrize("maximum", [1, 2, 3])
def test_call_budget_during_join_has_no_unsafe_continuation(monkeypatch, maximum):
    reader, *_ = joined_reader()
    monkeypatch.setattr(evidence, "MAX_CALLS", maximum)
    result = search(reader)
    assert result["report"]["calls"] == maximum
    assert len(reader.queries) + len(reader.gets) == maximum
    assert result["report"]["stop_reason"] == "call_budget"
    assert result["items"] == [] and result["truncated"]
    assert result["next_cursor"] is None


def test_join_page_budget(monkeypatch):
    reader, *_ = joined_reader()
    monkeypatch.setattr(evidence, "MAX_PAGES", 1)
    result = search(reader)
    assert result["report"]["stop_reason"] == "page_budget"
    assert result["report"]["pages"] == 1
    assert result["next_cursor"] is None


@pytest.mark.parametrize("location", ["payload", "extra", "audio", "person", "nonmatch"])
def test_row_budget_accounts_for_all_rows_and_unknown_fields(location):
    reader, segment, audio, _, person = joined_reader()
    huge = "x" * (evidence.MAX_ROW_BYTES + 1)
    if location in {"payload", "nonmatch"}:
        segment.payload["blob"] = huge
        if location == "nonmatch":
            segment.payload["text"] = "miss"
    elif location == "extra":
        assert segment.__pydantic_extra__ is not None
        segment.__pydantic_extra__["unknown_future_field"] = huge
    else:
        (audio if location == "audio" else person).payload["blob"] = huge
    result = search(reader)
    assert result["items"] == [] and result["next_cursor"] is None
    assert result["truncated"] and result["report"]["stop_reason"] == "row_bytes"
    assert result["report"]["row_bytes"] <= evidence.MAX_ROW_BYTES
    assert result["report"]["read_rows"] >= 1


def test_midpage_stop_retains_only_completed_items_without_skipping(monkeypatch):
    one = raw(text="needle")
    two = raw(text="needle", audio_ref_id=new_id())
    reader = ScriptedReader(lambda q: Page((one, two), "later"))
    monkeypatch.setattr(evidence, "MAX_CALLS", 1)
    result = search(reader, limit=2)
    assert [r["transcript_segment_id"] for r in result["items"]] == [one.id]
    assert result["truncated"] and result["next_cursor"] is None
    assert result["report"]["stop_reason"] == "call_budget"


def test_cooperative_deadline_checks_returned_call_without_claiming_hard_timeout(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(evidence, "monotonic", lambda: clock[0])

    def delayed(q):
        clock[0] = 11.0
        return Page((raw(text="needle"),), None)

    reader = ScriptedReader(delayed)
    result = search(reader)
    assert result["truncated"] and not result["items"] and result["next_cursor"] is None
    assert result["report"]["stop_reason"] == "deadline"
    assert result["report"]["calls"] == 1
    assert result["report"]["read_rows"] == 1
    assert result["report"]["deadline_mode"] == "cooperative"


def test_response_byte_budget_discards_continuation(monkeypatch):
    reader = ScriptedReader(
        lambda q: Page(tuple(raw(text="needle" + "中" * 394) for _ in range(3)), "next")
    )
    monkeypatch.setattr(evidence, "MAX_RESPONSE_BYTES", 3600)
    result = search(reader)
    assert len(evidence._json(result)) <= 3600
    assert len(result["items"]) < 3
    assert result["truncated"] and result["next_cursor"] is None
    assert result["report"]["stop_reason"] == "response_bytes"


@pytest.mark.parametrize("cursor", ["x" * 161, "中" * 60, "\ud800"])
def test_unencodable_upstream_cursor_reports_truncated_not_exhausted(cursor):
    reader = ScriptedReader(lambda q: Page((raw(text="needle"),), cursor))
    result = search(reader, limit=1)
    assert result["truncated"] and result["next_cursor"] is None
    assert result["report"]["continuation"] == "unavailable"


@pytest.mark.parametrize("cursor", ["", "x", "a.b.c", "a." + "0" * 64, "雪.a", "a.雪", "x" * 513])
def test_malformed_cursors_are_sanitized_and_never_read(cursor):
    reader = ScriptedReader()
    with pytest.raises(ValueError, match=r"^invalid_input$"):
        search(reader, cursor=cursor)
    assert reader.queries == []


def signed_cursor(body: Any) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    signature = hmac.new(evidence._CURSOR_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


@pytest.mark.parametrize(
    "case", ["expired", "future", "bool", "empty", "long", "type", "shape", "binding"]
)
def test_malformed_signed_cursor_body(case, monkeypatch):
    monkeypatch.setattr(evidence, "timestamp", lambda: 1000.0)
    body: Any = [1100, evidence._binding(WINDOW, "needle", 10), "opaque"]
    if case in {"expired", "future", "bool"}:
        body[0] = {"expired": 1000, "future": 2000, "bool": True}[case]
    elif case in {"empty", "long", "type"}:
        body[2] = {"empty": "", "long": "x" * 161, "type": 42}[case]
    elif case == "shape":
        body = {"expiry": 1100, "binding": body[1], "raw": "opaque"}
    else:
        body[1] = "wrong-binding"
    with pytest.raises(ValueError, match=r"^invalid_input$"):
        search(ScriptedReader(), cursor=signed_cursor(body))


@pytest.mark.parametrize("change", ["phrase", "limit", "time", "key", "signature"])
def test_cursor_binds_query_and_process_key(change, monkeypatch):
    cursor = evidence._encode_cursor("opaque", evidence._binding(WINDOW, "needle", 10))
    assert cursor is not None
    window, phrase, limit = WINDOW, "needle", 10
    if change == "phrase":
        phrase = "other"
    elif change == "limit":
        limit = 1
    elif change == "time":
        window = TimeRange(NOW, NOW + timedelta(hours=1))
    elif change == "key":
        monkeypatch.setattr(evidence, "_CURSOR_KEY", b"different-process")
    else:
        cursor = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
    with pytest.raises(ValueError, match=r"^invalid_input$"):
        evidence.search(cast(Reader, ScriptedReader()), window, phrase, cursor, limit)


@pytest.mark.parametrize(
    "kind", ["too_many", "empty_cursor", "wrong_type", "outside", "nan", "unicode"]
)
def test_invalid_upstream_data_fail_closed(kind):
    segment = raw(text="needle")
    if kind == "wrong_type":
        segment = segment.model_copy(update={"data_type": "audio_ref"})
    elif kind == "outside":
        segment = segment.model_copy(update={"event_start": WINDOW.end, "event_end": WINDOW.end})
    elif kind == "nan":
        segment.payload["unknown"] = float("nan")
    elif kind == "unicode":
        segment.payload["text"] = "needle\ud800"
    reader = ScriptedReader(
        lambda q: Page(
            (segment,) * (2 if kind == "too_many" else 1), "" if kind == "empty_cursor" else None
        )
    )
    with pytest.raises(ProtocolError, match=r"^invalid_evidence_"):
        search(reader, limit=1)


class MockWire:
    """Exercise Upstream and RemoteStorageClient with authenticated, in-memory wire I/O."""

    def __init__(self):
        self.reader = joined_reader()[0]
        self.revoked: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self.read_failure: int | None = None
        self.transport_failure: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.requests.append((request.url.path, token))
        if token not in {STORAGE_USER, STORAGE_ADMIN} or token in self.revoked:
            return httpx.Response(401, text="private " + token)
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json={"codec": 1, "module": "api"})
        assert request.url.path == "/v1/read", "no snapshots, file downloads or writes"
        if self.transport_failure:
            raise self.transport_failure
        if self.read_failure:
            return httpx.Response(self.read_failure, text="private " + token)
        parsed, args = decode_read(request.content)
        assert parsed.snapshot_id is None
        assert parsed.method in {"query", "get", "get_version"}
        try:
            if parsed.method == "query":
                value = self.reader.query(args["query"])
            else:
                value = self.reader.get(args["id"])
        except PermissionDenied:
            return httpx.Response(403, text="private " + token)
        return httpx.Response(200, text=encode_read_result(parsed.method, value))


@pytest.fixture
def evidence_http(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access forbidden in these tests")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    wire = MockWire()
    config = settings("http://storage.invalid")
    upstream = Upstream(config, transport=httpx.MockTransport(wire.handle))
    with TestClient(create_app(config, upstream=upstream)) as client:
        yield client, wire, config


@pytest.mark.parametrize("admin", [False, True])
def test_http_uses_current_binding_and_closed_projection(evidence_http, admin):
    client, wire, _ = evidence_http
    result = client.get("/v1/evidence/search", params=PARAMS, headers=headers(admin))
    assert result.status_code == 200
    assert len(result.json()["items"]) == 1
    assert result.json()["consistency"] == "live_best_effort"
    assert all(token == (STORAGE_ADMIN if admin else STORAGE_USER) for _, token in wire.requests)
    assert wire.requests[0][0] == "/v1/identity"
    assert STORAGE_USER not in result.text and "file_ref" not in result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["content-security-policy"]


@pytest.mark.parametrize("mode", ["missing", "wrong", "cookie", "duplicate", "storage_token"])
def test_http_header_only_authentication(evidence_http, mode):
    client, wire, _ = evidence_http
    auth = {}
    if mode == "wrong":
        auth = {"Authorization": "Bearer " + "x" * 43}
    elif mode == "cookie":
        auth = {"Cookie": "token=" + USER}
    elif mode == "duplicate":
        auth = [("authorization", "Bearer " + USER), ("authorization", "Bearer " + ADMIN)]
    elif mode == "storage_token":
        auth = {"Authorization": "Bearer " + STORAGE_USER}
    result = client.get("/v1/evidence/search", params=PARAMS, headers=auth)
    assert result.status_code == 401 and result.json() == {"code": "unauthenticated"}
    assert result.headers["www-authenticate"] == "Bearer"
    assert not wire.requests


@pytest.mark.parametrize(
    "changes",
    [
        {"start": ""},
        {"end": ""},
        {"q": ""},
        {"q": "x" * 257},
        {"start": START.replace("Z", "+00:00")},
        {"end": "2026-11-19T00:00:01Z"},
        {"end": START},
        {"start": END},
        {"limit": "0"},
        {"limit": "11"},
        {"limit": "1.0"},
        {"limit": "+1"},
        {"limit": "true"},
        {"limit": "１"},
        {"cursor": ""},
        {"cursor": "x" * 513},
        {"token": USER},
        {"snapshot": "fake"},
    ],
)
def test_http_strict_parameters_before_upstream(evidence_http, changes):
    client, wire, _ = evidence_http
    result = client.get("/v1/evidence/search", params={**PARAMS, **changes}, headers=headers())
    assert result.status_code == 422 and result.json() == {"code": "invalid_input"}
    assert not wire.requests


@pytest.mark.parametrize("key", ["start", "end", "q", "limit", "cursor"])
def test_http_duplicate_parameters(evidence_http, key):
    client, wire, _ = evidence_http
    pairs = [*PARAMS.items(), (key, "1" if key == "limit" else "opaque")]
    if key not in PARAMS:
        pairs.append((key, "opaque"))
    result = client.get("/v1/evidence/search", params=pairs, headers=headers())
    assert result.status_code == 422 and not wire.requests


@pytest.mark.parametrize("key", ["start", "end", "q"])
def test_http_required_parameters(evidence_http, key):
    client, wire, _ = evidence_http
    result = client.get(
        "/v1/evidence/search",
        params={k: v for k, v in PARAMS.items() if k != key},
        headers=headers(),
    )
    assert result.status_code == 422 and not wire.requests


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_http_upstream_errors_are_secret_free(evidence_http, caplog, status):
    client, wire, _ = evidence_http
    wire.read_failure = status
    with caplog.at_level(logging.WARNING):
        result = client.get("/v1/evidence/search", params=PARAMS, headers=headers())
    assert result.status_code == (status if status in {401, 403} else 503)
    assert set(result.json()) == {"code"}
    for token in (USER, ADMIN, STORAGE_USER, STORAGE_ADMIN):
        assert token not in result.text and token not in caplog.text
    assert "private" not in result.text and "private" not in caplog.text


def test_http_exception_diagnostics_never_include_tokens(evidence_http, caplog):
    client, wire, _ = evidence_http
    wire.transport_failure = RuntimeError("private " + USER + STORAGE_USER)
    with caplog.at_level(logging.WARNING):
        result = client.get("/v1/evidence/search", params=PARAMS, headers=headers())
    assert result.status_code == 500 and result.json() == {"code": "internal_error"}
    assert USER not in result.text + caplog.text
    assert STORAGE_USER not in result.text + caplog.text


def test_http_live_revocation_and_no_admin_fallback(evidence_http):
    client, wire, _ = evidence_http
    assert client.get("/v1/evidence/search", params=PARAMS, headers=headers()).status_code == 200
    wire.revoked.add(STORAGE_USER)
    before = len(wire.requests)
    assert client.get("/v1/evidence/search", params=PARAMS, headers=headers()).status_code == 401
    assert wire.requests[before:] == [("/v1/identity", STORAGE_USER)]
    assert (
        client.get("/v1/evidence/search", params=PARAMS, headers=headers(True)).status_code == 200
    )


@pytest.mark.parametrize("target", ["transcript_segment", "attribution", "audio_ref", "person"])
def test_http_denied_reads_never_return_partial_success(evidence_http, target):
    client, wire, _ = evidence_http
    if target in {"audio_ref", "person"}:
        wire.reader.denied_ids.update(
            id for id, row in wire.reader.records.items() if row and row.data_type == target
        )
    else:
        wire.reader.denied_types.add(target)
    result = client.get("/v1/evidence/search", params=PARAMS, headers=headers())
    assert result.status_code == 403 and result.json() == {"code": "forbidden"}


def test_http_pagination_malformed_cursor_truncation_and_revoked_grant(evidence_http, caplog):
    client, wire, _ = evidence_http
    segment = raw(text="needle")
    wire.reader.query_fn = lambda q: Page((segment,), "opaque next" if q.cursor is None else None)
    params = {**PARAMS, "limit": "1"}
    first = client.get("/v1/evidence/search", params=params, headers=headers()).json()
    assert first["truncated"] and first["report"]["stop_reason"] == "result_limit"
    cursor = first["next_cursor"]
    second = client.get(
        "/v1/evidence/search", params={**params, "cursor": cursor}, headers=headers()
    )
    assert second.status_code == 200 and second.json()["truncated"] is False
    before = len(wire.reader.queries)
    with caplog.at_level(logging.WARNING):
        bad = client.get(
            "/v1/evidence/search", params={**params, "cursor": USER}, headers=headers()
        )
    assert bad.status_code == 422 and bad.json() == {"code": "invalid_input"}
    assert len(wire.reader.queries) == before
    assert USER not in bad.text + caplog.text
    changed = client.get(
        "/v1/evidence/search",
        params={**params, "cursor": cursor, "q": "changed"},
        headers=headers(),
    )
    assert changed.status_code == 422
    wire.reader.denied_types.add("transcript_segment")
    denied = client.get(
        "/v1/evidence/search", params={**params, "cursor": cursor}, headers=headers()
    )
    assert denied.status_code == 403


def test_http_budget_truncation_is_not_empty_exhaustion(evidence_http, monkeypatch):
    client, _, _ = evidence_http
    monkeypatch.setattr(evidence, "MAX_CALLS", 1)
    result = client.get("/v1/evidence/search", params=PARAMS, headers=headers())
    assert result.status_code == 200
    assert result.json()["items"] == []
    assert result.json()["truncated"] is True and result.json()["next_cursor"] is None
    assert result.json()["report"]["continuation"] == "unavailable"


@pytest.mark.parametrize("cursor", ["x" * 161, "中" * 60, "\ud800"])
def test_unencodable_cursor_is_not_sent_back_to_storage(cursor):
    reader = ScriptedReader(lambda q: Page((), cursor))
    result = search(reader)
    assert len(reader.queries) == 1
    assert result["report"]["stop_reason"] == "cursor_unavailable"
    assert result["report"]["continuation"] == "unavailable"
    assert result["truncated"] and result["next_cursor"] is None


def test_cursor_roundtrip_expiry_and_restart(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(evidence, "timestamp", lambda: clock[0])
    binding = evidence._binding(WINDOW, "needle", 10)
    cursor = evidence._encode_cursor("opaque", binding)
    assert cursor is not None
    clock[0] = 1899.0
    assert evidence._decode_cursor(cursor, binding) == "opaque"
    clock[0] = 1900.0
    with pytest.raises(ValueError, match=r"^invalid_input$"):
        evidence._decode_cursor(cursor, binding)


def test_empty_page_with_cursor_can_lead_to_a_match():
    row = raw(text="needle")
    reader = ScriptedReader(lambda q: Page((), "next") if q.cursor is None else Page((row,), None))
    result = search(reader)
    assert len(result["items"]) == 1 and not result["truncated"]
    assert result["report"]["pages"] == 2


def test_missing_person_is_not_a_resolved_identity():
    reader, _, _, _, person = joined_reader()
    reader.records[person.id] = None
    item = search(reader)["items"][0]
    assert item["attribution_status"] == "unavailable"
    assert item["attribution"]["person_status"] == "unavailable"
    assert item["attribution"]["name"] is None


@pytest.mark.parametrize("phrase", ["x", "中" * 256])
@pytest.mark.parametrize("limit", [1, 10])
def test_valid_query_and_limit_edges(evidence_http, phrase, limit):
    client, wire, _ = evidence_http
    wire.reader.query_fn = lambda q: Page(tuple(raw(text=phrase) for _ in range(q.limit)), None)
    result = client.get(
        "/v1/evidence/search", params={**PARAMS, "q": phrase, "limit": limit}, headers=headers()
    )
    assert result.status_code == 200 and len(result.json()["items"]) == limit


def test_response_smaller_than_metadata_fails_closed(monkeypatch):
    monkeypatch.setattr(evidence, "MAX_RESPONSE_BYTES", 1)
    with pytest.raises(ProtocolError, match=r"^evidence_response_budget_too_small$"):
        search(ScriptedReader())


def test_deep_unknown_field_is_budgeted_without_recursion_overflow():
    row = raw(text="needle")
    child = row.payload
    for _ in range(40):
        child["next"] = {}
        child = child["next"]
    result = search(ScriptedReader(lambda q: Page((row,), None)))
    assert result["truncated"] and result["report"]["stop_reason"] == "row_bytes"
    assert not result["items"]


def test_budget_before_any_call(monkeypatch):
    monkeypatch.setattr(evidence, "DEADLINE_SECONDS", 0.0)
    reader = ScriptedReader()
    result = search(reader)
    assert result["report"]["stop_reason"] == "deadline"
    assert result["report"]["calls"] == 0 and not reader.queries


@pytest.mark.parametrize("origin", ["http://testserver", "https://untrusted.invalid"])
def test_http_origin_boundary(evidence_http, origin):
    client, wire, _ = evidence_http
    result = client.get(
        "/v1/evidence/search", params=PARAMS, headers={**headers(), "Origin": origin}
    )
    assert result.status_code == (200 if origin == "http://testserver" else 403)
    if result.status_code == 403:
        assert not wire.requests
    assert "access-control-allow-origin" not in result.headers
