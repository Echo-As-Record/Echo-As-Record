"""Activity floor through the ordinary extraction and timer paths, with synthetic raw data."""

from datetime import timedelta

import pytest
from ear_contracts import new_id
from ear_extraction.demo import fixture_registry, submit
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.orchestration import ExtractionService
from ear_extraction.persistence import records
from ear_extraction.worker import Schedule, tick
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage, Query
from ear_storage_client.testing import make_raw, ts

START = ts("2026-11-05T09:00:00Z")
SCOPE = Interval(start=START, end=START + timedelta(hours=1))


class Gateway:
    def __init__(self):
        self.calls = []
        self.spans = []

    def complete(self, call):
        self.calls.append(call)
        if call.purpose == "segmentation":
            return {"activities": self.spans, "empty_reason": "" if self.spans else "empty"}
        if call.purpose == "selection":
            return {"rule_ids": [rule["id"] for rule in call.data["rules"]]}
        assert call.purpose == "generation"
        return {"body_md": "Synthetic notes", "evidence_ids": [call.data["evidence"][0]["id"]]}


def span(raw, *, padded=False):
    return {
        **(
            SCOPE.model_dump(mode="json")
            if padded
            else Interval(start=raw.event_start, end=raw.event_end).model_dump(mode="json")
        ),
        "title_md": "Synthetic activity",
        "evidence_ids": [raw.id],
    }


def setup(*, transcript_only=False, **config):
    store = MemoryStorage(fixture_registry())
    gateway = Gateway()
    service = ExtractionService(
        store.client("extraction"),
        gateway,
        clock=lambda: SCOPE.end,
        transcript_only=transcript_only,
        **config,
    )
    service.subscribe()
    service.storage.write(
        [Create("rule", new_id(), Rule(scope="synthetic", instruction="take notes").model_dump())],
        Context.system(),
    ).check()
    return store, gateway, service


def add_raw(store, offset, duration):
    start = START + timedelta(seconds=offset)
    raw = make_raw(
        "transcript_segment",
        {"text": "Synthetic evidence"},
        start=start,
        end=start + timedelta(seconds=duration),
    )
    store.client("adapter").write([AppendRaw(raw)], Context.system()).check()
    return raw


@pytest.mark.parametrize("transcript_only", [False, True])
@pytest.mark.parametrize(
    "minutes,seconds,kept",
    [
        (None, 599.999999, False),
        (None, 600, True),
        (5, 299.999999, False),
        (5, 300, True),
    ],
)
def test_evidence_duration_inclusive_without_padding(transcript_only, minutes, seconds, kept):
    config = {} if minutes is None else {"activity_min_duration_minutes": minutes}
    store, gateway, service = setup(transcript_only=transcript_only, **config)
    row = add_raw(store, 0, seconds)
    gateway.spans = [span(row, padded=not kept)]  # Proposing the whole hour cannot earn time.
    id = submit(store, Command(kind="extract", interval=SCOPE))
    service.drain()
    run = service.storage.get_system_record(id)
    assert run is not None and run.payload["status"] == "succeeded"
    assert [c.purpose for c in gateway.calls] == ["segmentation"] + (
        ["selection", "generation"] if kept else []
    )
    for kind in ("activity", "rule_selection", "artifact"):
        assert len(records(service.storage, kind)) == kept
    assert service.storage.get(row.id) is not None
    if kept:
        [activity] = records(service.storage, "activity")
        assert (activity.event_start, activity.event_end) == (row.event_start, row.event_end)
    else:
        assert run.payload["detail"] == {
            "activities": 0,
            "empty_reason": "no_supported_activity",
            "below_min_duration_candidates": 1,
            "below_min_duration_rows": 1,
        }


@pytest.mark.parametrize("case", ["empty", "all_short", "mixed"])
@pytest.mark.parametrize("transcript_only", [False, True])
def test_generic_reextract_never_retires_family_for_short_or_empty_output(case, transcript_only):
    store, gateway, service = setup(transcript_only=transcript_only)
    a, b = add_raw(store, 0, 600), add_raw(store, 600, 20)
    gateway.spans = [span(a)]
    submit(store, Command(kind="extract", interval=SCOPE))
    service.drain()
    before = {
        kind: records(service.storage, kind) for kind in ("activity", "artifact", "rule_selection")
    }
    assert all(len(rows) == 1 for rows in before.values())
    gateway.calls.clear()
    gateway.spans = {"empty": [], "all_short": [span(b)], "mixed": [span(a), span(b)]}[case]
    preview = service.preview_reextract(SCOPE)["versions"]
    id = submit(store, Command(kind="reextract", interval=SCOPE, preview=preview))
    service.drain()
    run = service.storage.get_system_record(id)
    assert run is not None and run.payload["status"] == "blocked"
    assert run.payload["error"] == "reextract_incomplete_activity_replacement"
    assert [c.purpose for c in gateway.calls] == ["segmentation"]
    for kind, rows in before.items():
        assert records(service.storage, kind) == rows
        assert service.storage.get(rows[0].id) == rows[0]


def test_scheduled_all_short_is_successful_empty_coverage_not_an_automatic_replay():
    store, gateway, service = setup()
    row = add_raw(store, 0, 599)
    gateway.spans = [span(row)]
    timer = Schedule(START, timedelta(hours=1))
    tick(service, timer)
    [run] = service.runs()
    assert run.payload["scheduled"] and run.payload["status"] == "succeeded"
    assert run.payload["detail"]["empty_reason"] == "no_supported_activity"
    assert [c.purpose for c in gateway.calls] == ["segmentation"]
    assert service.storage.count(Query("activity")) == 0
    assert service.storage.get(row.id) is not None
    tick(service, timer)
    assert service.runs() == [run] and len(gateway.calls) == 1
