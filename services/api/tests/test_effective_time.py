"""Synthetic tests for the optional display clock, never deployed data."""

from datetime import timedelta
from typing import cast

import httpx
import pytest
from ear_api.app import create_app
from ear_api.effective_time import TimeProjection
from ear_api.upstream import Upstream
from ear_contracts import RawRecord, new_id
from ear_storage_client import Query, TimeRange
from ear_storage_client.protocol import Reader
from ear_storage_client.queries import Page
from ear_storage_client.remote_errors import ProtocolError
from fastapi.testclient import TestClient

from .conftest import headers, settings
from .test_evidence import NOW, MockWire, ScriptedReader, raw


def record(kind: str, minute: int, duration: int = 2) -> RawRecord:
    start = NOW + timedelta(minutes=minute)
    return raw(kind, text="synthetic").model_copy(
        update={"event_start": start, "event_end": start + timedelta(minutes=duration)}
    )


def window(first: int, last: int) -> TimeRange:
    return TimeRange(NOW + timedelta(minutes=first), NOW + timedelta(minutes=last))


def z(value):
    return value.isoformat().replace("+00:00", "Z")


def physical(rows: list[RawRecord], q: Query) -> Page:
    # Storage cursors deliberately are not decimal offsets and may be sparse.
    ordered = sorted(
        (
            r
            for r in rows
            if r.data_type == q.data_type
            and (q.time is None or q.time.overlaps(r.event_start, r.event_end))
        ),
        key=lambda r: (r.event_start, r.id),
    )
    index = 0 if q.cursor is None else int(q.cursor.removeprefix("storage/page:"))
    stop = index + min(q.limit, 7)
    return Page(tuple(ordered[index:stop]), f"storage/page:{stop}" if stop < len(ordered) else None)


@pytest.mark.parametrize("minutes", [-30, 30])
@pytest.mark.parametrize("bounds", [(0, 60), (30, 60), (60, 64), (-60, 160)])
def test_merge_matches_reference_across_opaque_pages(minutes, bounds):
    kind = "transcript_segment"
    rows = [record(kind, n // 3, n % 3) for n in range(-130, 240)]
    selected = {r.id for r in rows[::4]}
    view = TimeProjection(minutes * 60, {kind: tuple(selected)})
    interval = window(*bounds)
    reader = ScriptedReader(lambda q: physical(rows, q))

    def projected_start(row):
        return row.event_start + timedelta(minutes=minutes if row.id in selected else 0)

    expected = sorted(
        (
            r
            for r in rows
            if interval.overlaps(
                projected_start(r), projected_start(r) + (r.event_end - r.event_start)
            )
        ),
        key=lambda row: (projected_start(row), row.id),
    )
    got, cursor = [], None
    for _ in range(len(rows) + 1):
        reads = len(reader.queries)
        result = view.page(
            cast(Reader, reader),
            kind,
            interval,
            5,
            cursor,
            subject="user",
            surface="source-timeline/all",
        )
        assert 1 <= len(reader.queries) - reads <= 8
        got.extend(result.items)
        if result.next_cursor is None:
            break
        assert result.next_cursor != cursor
        cursor = result.next_cursor
    else:
        pytest.fail("pagination did not terminate")
    assert got == expected  # canonical records themselves were never replaced or mutated


def test_budget_preserves_order_when_one_stream_is_sparse():
    kind = "audio_ref"
    moved, ordinary = record(kind, -1, 0), record(kind, 2, 0)
    decoys = [record(kind, -12, 0) for _ in range(40)]
    rows = [*decoys, ordinary, moved]
    reader = cast(Reader, ScriptedReader(lambda q: physical(rows, q)))
    view = TimeProjection(12 * 60, {kind: (moved.id,)})
    page = view.page(reader, kind, window(0, 12), 2, None, subject="user", surface="timeline")
    assert page.items == () and page.next_cursor
    page = view.page(
        reader, kind, window(0, 12), 2, page.next_cursor, subject="user", surface="timeline"
    )
    assert page == Page((ordinary, moved), None)


def test_rejects_bad_upstream_order():
    kind = "audio_ref"
    a, b = record(kind, 0), record(kind, 1)
    reader = cast(Reader, ScriptedReader(lambda _q: Page((b, a), None)))
    with pytest.raises(ProtocolError):
        TimeProjection(60, {kind: (a.id,)}).page(
            reader, kind, window(0, 5), 1, None, subject="user", surface="timeline"
        )


@pytest.mark.parametrize("seconds", [True, 0, 1.5, -86401])
def test_invalid_offsets(seconds):
    with pytest.raises(ValueError):
        TimeProjection(seconds, {"audio_ref": [new_id()]})


def test_invalid_scopes_and_input_copy():
    rid = new_id()
    for scope in (
        {},
        {"activity": [rid]},
        {"audio_ref": []},
        {"audio_ref": [rid, rid]},
        {"audio_ref": ["not-an-id"]},
    ):
        with pytest.raises(ValueError):
            TimeProjection(1, scope)
    ids = {"audio_ref": [rid]}
    view = TimeProjection(1, ids)
    ids["audio_ref"].clear()
    assert view.coverage_shift() == {"seconds": 1, "ids": {"audio_ref": [rid]}}


def test_opt_in_routes_leave_canonical_record_and_authorization_unchanged():
    audio, other = record("audio_ref", -12, 0), record("audio_ref", 3, 0)
    wire = MockWire()
    wire.reader.records[audio.id] = audio
    wire.reader.query_fn = lambda q: physical([audio, other], q)
    config = settings("http://storage.invalid")
    upstream = Upstream(config, transport=httpx.MockTransport(wire.handle))
    time = window(0, 12)
    params = {"start": z(time.start), "end": z(time.end), "limit": "1"}
    timeline = "/v1/source-timeline"
    with TestClient(create_app(config, upstream=upstream)) as client:
        default = client.get(timeline, params={**params, "kind": "audio_ref"}, headers=headers())
        assert default.status_code == 200, default.text
        assert [item["id"] for item in default.json()["items"]] == [other.id]
    view = TimeProjection(12 * 60, {"audio_ref": (audio.id,)})
    with TestClient(create_app(config, upstream=upstream, time_projection=view)) as client:
        response = client.get(timeline, params={**params, "kind": "audio_ref"}, headers=headers())
        assert response.status_code == 200, response.text
        assert [(item["id"], item["start"]) for item in response.json()["items"]] == [
            (audio.id, z(time.start))
        ]
        cursor = response.json()["next_cursor"]
        assert cursor
        for changed in (
            {"source": "legacy-ear"},
            {"limit": "2"},
            {"end": z(window(0, 13).end)},
            {"cursor": cursor + "x"},
        ):
            rejected = client.get(
                timeline,
                params={**params, "kind": "audio_ref", "cursor": cursor, **changed},
                headers=headers(),
            )
            assert rejected.status_code == 422
        other_subject = client.get(
            timeline,
            params={**params, "kind": "audio_ref", "cursor": cursor},
            headers=headers(admin=True),
        )
        assert other_subject.status_code == 422
        intervals = client.get("/v1/recording-intervals", params=params, headers=headers())
        assert intervals.json()["items"] == [{"start": z(time.start), "end": z(time.start)}]
        assert (
            client.get(
                "/v1/recording-intervals", params={**params, "cursor": cursor}, headers=headers()
            ).status_code
            == 422
        )
        detail = client.get(f"/v1/records/audio_ref/{audio.id}", headers=headers())
        assert detail.json()["event_start"] == z(audio.event_start)
        wire.reader.denied_types.add("audio_ref")
        request = {**params, "kind": "audio_ref", "cursor": cursor}
        assert client.get(timeline, params=request, headers=headers()).status_code == 403


def test_week_coverage_uses_only_aggregate_rpc_and_never_falls_back():
    audio = record("audio_ref", -5, 0)
    config = settings("http://storage.invalid", chat_evidence_read=True)
    wire = MockWire()
    intervals = [window(0, 60), window(60, 60 + 25 * 60)]
    days = [{"start": z(t.start), "end": z(t.end)} for t in intervals]
    query = {"days": ",".join(f"{d['start']}/{d['end']}" for d in days)}
    expected = {
        "slot_seconds": 300,
        "timeline": {"state": "partial", "days": ["1" + "0" * 11, "0" * 300]},
        "activity": {"state": "unavailable", "days": ["", ""]},
    }
    calls = []
    status = 200

    def handle(request):
        if request.url.path == "/v1/coverage/week":
            import json

            calls.append(json.loads(request.content))
            return httpx.Response(status, json=expected if status == 200 else {})
        return wire.handle(request)

    upstream = Upstream(config, transport=httpx.MockTransport(handle))
    projection = TimeProjection(5 * 60, {"audio_ref": [audio.id]})
    with TestClient(create_app(config, upstream=upstream, time_projection=projection)) as client:
        path = "/v1/week-coverage"
        assert client.get(path, params=query, headers=headers()).json() == expected
        assert calls == [{"days": days, "include_chat": True, "shift": projection.coverage_shift()}]
        status = 422  # Older Storage rejected the internal opt-in: never return unshifted bins.
        assert client.get(path, params=query, headers=headers()).status_code == 503
        assert (
            client.get(path, params={**query, "shift": "300"}, headers=headers()).status_code == 422
        )
    assert not wire.reader.queries and not wire.reader.gets
