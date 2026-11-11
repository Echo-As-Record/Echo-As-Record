"""录音浏览器：时间线转换、搜索、备注、音频只服务索引里的文件（不起 HTTP 服务）。"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

from ear_adapter.ear_api import CaptureEvidence
from ear_adapter.testing import evidence_json
from ear_baselines.player import PlayerApp, timeline

TZ = ZoneInfo("Asia/Shanghai")


def _evidence() -> CaptureEvidence:
    return CaptureEvidence.model_validate(evidence_json())


def _inventory(tmp_path: Path, audio: Path | None) -> Path:
    inv = tmp_path / "inventory"
    (inv / "transcripts").mkdir(parents=True)
    ev = _evidence()
    recs = [
        {
            "id": ev.capture_id,
            "capture_id": ev.capture_id,
            "status": "ready",
            "audio_path": str(audio) if audio else None,
        },
        {
            "id": "file-abc",
            "capture_id": None,
            "status": "not_ingested",
            "audio_path": str(tmp_path / "missing.m4a"),
        },
    ]
    (inv / "index.json").write_text(json.dumps({"recordings": recs}), "utf-8")
    (inv / "transcripts" / f"{ev.capture_id}.md").write_text(
        "# 标题含关键词\n- 10:00:01 [块1·S1] 我们下午去上课\n", "utf-8"
    )
    return inv


def test_timeline_rows_are_relative_and_sorted() -> None:
    ev = _evidence()
    result = timeline(ev, TZ)
    rows = result["rows"]
    assert rows, "合成证据里应该有发言"
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)
    speech = [r for r in rows if r["kind"] == "speech"]
    assert speech and all(r["speaker"].startswith("块") for r in speech)
    assert all(len(r["clock"]) == 8 for r in rows)
    assert result["started_at"].endswith("+08:00")


def test_detail_falls_back_to_markdown_when_ear_is_down(tmp_path: Path) -> None:
    inv = _inventory(tmp_path, None)

    def broken(_: str) -> CaptureEvidence:
        raise ConnectionError("down")

    app = PlayerApp(inv, TZ, broken)
    detail = app.detail(_evidence().capture_id)
    assert detail is not None
    assert detail["source"] == "markdown"
    assert "上课" in detail["markdown"]
    assert app.detail("nope") is None


def test_detail_uses_ear_and_caches(tmp_path: Path) -> None:
    inv = _inventory(tmp_path, None)
    calls: list[str] = []

    def read(capture_id: str) -> CaptureEvidence:
        calls.append(capture_id)
        return _evidence()

    app = PlayerApp(inv, TZ, read)
    cid = _evidence().capture_id
    assert app.detail(cid)["source"] == "ear"  # type: ignore[index]
    app.detail(cid)
    assert calls == [cid]


def test_search_and_notes(tmp_path: Path) -> None:
    inv = _inventory(tmp_path, None)
    app = PlayerApp(inv, TZ, None)
    cid = _evidence().capture_id
    hits = app.search("上课")
    assert hits == [{"id": cid, "line": "- 10:00:01 [块1·S1] 我们下午去上课"}]
    assert app.search("关键词") == []  # 标题行不算
    app.set_note(cid, "法语课")
    assert app.notes()[cid]["text"] == "法语课"
    assert app.search("法语")[0]["line"] == "备注：法语课"
    app.set_note(cid, "  ")
    assert cid not in app.notes()


def test_audio_only_for_indexed_existing_files(tmp_path: Path) -> None:
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    inv = _inventory(tmp_path, audio)
    app = PlayerApp(inv, TZ, None)
    assert app.audio_path(_evidence().capture_id) == audio
    assert app.audio_path("file-abc") is None  # 文件不存在
    assert app.audio_path("../../etc/passwd") is None
