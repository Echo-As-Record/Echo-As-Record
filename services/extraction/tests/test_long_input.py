from ear_contracts import new_id
from ear_extraction.domain import Generated, InputBudgetExceeded, Interval
from ear_extraction.long_input import generate, segment
from ear_extraction.segmentation import validate_segments
from ear_storage_client.testing import make_raw, ts


def test_long_segmentation_splits_and_merges_without_cutting_speech():
    evidence = [
        make_raw(
            "transcript_segment",
            {"text": "合成证据"},
            start=f"2026-11-05T{hour:02d}:00:00Z",
            end=f"2026-11-05T{hour:02d}:10:00Z",
        )
        for hour in range(24)
    ]
    # Convert using the storage read shape, as actual adapters return RawRecord.
    from ear_contracts import RawRecord

    evidence = [RawRecord(**r.model_dump(), ingested_at=r.event_start) for r in evidence]
    interval = Interval(start=ts("2026-11-05T00:00:00Z"), end=ts("2026-11-06T00:00:00Z"))
    seen = []

    def ask(purpose, data, model):
        if purpose == "merge_activities":
            return model(merge=True, title_md="同一活动")
        rows = data["evidence"]
        if len(rows) > 3:
            raise InputBudgetExceeded()
        seen.extend(r["id"] for r in rows)
        return model(
            activities=[
                {
                    **data["interval"],
                    "title_md": "同一活动",
                    "evidence_ids": [r["id"] for r in rows],
                }
            ]
        )

    result = segment(ask, interval, evidence)
    assert set(seen) == {r.id for r in evidence}
    assert len(seen) == len(evidence)
    assert len(result.activities) == 1
    assert len(result.activities[0].evidence_ids) == 24
    validate_segments(result, interval, evidence)


def test_long_generation_preserves_original_evidence_ids():
    rows = [{"id": new_id()} for _ in range(12)]
    seen = []

    def ask(purpose, data, model):
        if purpose == "consolidation":
            ids = [id for p in data["partial_results"] for id in p["evidence_ids"]]
            return model(body_md="合并笔记", evidence_ids=ids)
        if len(data["evidence"]) > 2:
            raise InputBudgetExceeded()
        seen.extend(r["id"] for r in data["evidence"])
        return Generated(body_md="笔记", evidence_ids=[r["id"] for r in data["evidence"]])

    result = generate(ask, {"activity": {}, "rule": {}, "evidence": rows})
    assert set(result.evidence_ids) == set(seen) == {r["id"] for r in rows}
    assert len(seen) == len(rows)
