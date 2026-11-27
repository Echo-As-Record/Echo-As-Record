"""LibreChat 源适配器：导出器 → 摄入引擎 → ① 的端到端行为（合成数据，Node 导出器）。"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from ear_contracts import DataChanged, FileRef
from ear_storage_client import Query
from ear_storage_client.memory import MemoryClient
from librechat_adapter import CHAT_TYPES, Ingestor, LibreChatConfig, PrivateVolume
from librechat_adapter.engine import IngestReport, SourceVersionError
from librechat_adapter.testing import (
    CHART,
    NOTES,
    OTHER_UID,
    UID,
    LibreChatFixture,
    has_node,
    make_fixture,
    memory_storage,
    run_exporter,
)

pytestmark = pytest.mark.skipif(not has_node(), reason="node is required for the exporter")

REPO = Path(__file__).resolve().parents[3]
RAW_SCHEMA = json.loads((REPO / "contracts/records/raw-record.schema.json").read_text())
RANK = {"chat_session": 0, "chat_attachment": 1, "chat_message": 2}


def _volume(tmp_path: Path) -> PrivateVolume:
    root = tmp_path / "vol"
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    return PrivateVolume(root)


def _records(client: MemoryClient, data_type: str) -> list[Any]:
    return list(client.query(Query(data_type, limit=1000)).items)


def _latest(client: MemoryClient, data_type: str, key: str, value: str) -> list[Any]:
    """同一对象的全部修订，按 revision 排序。"""
    found = [r for r in _records(client, data_type) if r.payload.get(key) == value]
    return sorted(found, key=lambda r: r.payload["revision"])


def _ingest(
    lines: Iterable[str],
    client: MemoryClient,
    volume: PrivateVolume,
    config: LibreChatConfig | None = None,
) -> IngestReport:
    return Ingestor(client, volume, config).ingest(lines)


def _export(fx: LibreChatFixture, **kwargs: Any) -> list[str]:
    code, lines = run_exporter(fx, **kwargs)
    assert code == 0
    return lines


def test_full_export_ingests_sessions_messages_attachments_and_bytes(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    _, client = memory_storage()
    volume = _volume(tmp_path)
    report = _ingest(_export(fx), client, volume)
    assert report.complete
    assert report.librechat_version == "0.8.9"
    assert report.written == Counter(chat_session=2, chat_message=4, chat_attachment=8)

    everything = [r for t in CHAT_TYPES for r in _records(client, t)]
    for record in everything:
        jsonschema.validate(record.model_dump(mode="json"), RAW_SCHEMA)
        assert record.source.channel == "librechat"
        assert record.source.user == "default"
        assert record.event_start == record.event_end  # 单点时间
        assert record.payload["revision"] == 1
        assert record.payload["source_system"] == "librechat"
    dump = json.dumps([r.model_dump(mode="json") for r in everything], ensure_ascii=False)
    # 来源里的隐藏字段、他人数据永远不出现
    for forbidden in ("TOP-SECRET", "OTHER-USER", "OTHER-USERS-SECRET", "conv-other"):
        assert forbidden not in dump

    m1 = _latest(client, "chat_message", "message_id", "m1")[0].payload
    assert m1["role"] == "user"
    assert m1["conversation_id"] == "conv-1"
    assert m1["text"] == "请阅读附件 notes.txt"
    assert "parent_message_id" not in m1  # 根消息没有父消息
    m2 = _latest(client, "chat_message", "message_id", "m2")[0].payload
    assert m2["role"] == "assistant"
    assert m2["parent_message_id"] == "m1"
    assert m2["text"] == "好的，我来看看。\n\n附件内容是 alpha。"
    assert m2["reasoning"] == "内部思考 INNER"
    assert m2["tool_calls"][0]["name"] == "web_search"
    assert m2["attachment_ids"] == ["f-2"]

    session = _latest(client, "chat_session", "conversation_id", "conv-1")[0].payload
    assert session["title"] == "合成会话 A"
    assert session["tags"] == ["t1"]

    by_file = {r.payload["file_id"]: r.payload for r in _records(client, "chat_attachment")}
    notes = by_file["f-1"]
    assert notes["available"] is True
    ref = FileRef.model_validate(notes["file_ref"])
    assert (volume.root / ref.path).read_bytes() == NOTES
    assert volume.verify(ref)
    assert notes["text_kind"] == "decoded"
    assert notes["media_type"] == "text/plain"  # 参数已去掉
    assert notes["text"] == NOTES.decode()
    assert notes["message_id"] == "m1"
    chart = by_file["f-2"]
    assert (volume.root / chart["file_ref"]["path"]).read_bytes() == CHART
    assert chart["text_kind"] == "metadata"
    assert "chart.png" in chart["text"]
    reasons = {fid: p["unavailable_reason"] for fid, p in by_file.items() if not p["available"]}
    assert reasons == {
        "f-3": "unsupported_source",
        "f-4": "bad_path",
        "f-5": "not_regular",
        "f-7": "no_file_record",
        "f-8": "missing_on_disk",
    }
    assert by_file["f-6"]["available"] is True  # 二进制附件也保留字节
    assert "file_ref" not in by_file["f-3"]


def test_batches_commit_attachments_before_messages(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    storage, client = memory_storage()
    _ingest(_export(fx), client, _volume(tmp_path))
    batches: dict[str, list[str]] = {}
    for event in storage.events:
        assert isinstance(event, DataChanged)
        assert event.batch_id is not None
        batches.setdefault(event.batch_id, []).append(event.data_type)
    assert batches
    for types in batches.values():
        assert [RANK[t] for t in types] == sorted(RANK[t] for t in types)


def test_replay_writes_nothing_and_keeps_volume_stable(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    storage, client = memory_storage()
    volume = _volume(tmp_path)
    lines = _export(fx)
    _ingest(lines, client, volume)
    events = len(storage.events)
    files = sorted(p for p in volume.root.rglob("*") if p.is_file())
    again = _ingest(lines, client, volume)
    assert again.written == Counter()
    assert again.unchanged == Counter(chat_session=2, chat_message=4, chat_attachment=8)
    assert again.blobs_new == 0
    assert len(storage.events) == events
    assert sorted(p for p in volume.root.rglob("*") if p.is_file()) == files


def test_edits_append_revisions_and_reverting_is_a_new_revision(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    _, client = memory_storage()
    volume = _volume(tmp_path)
    _ingest(_export(fx), client, volume)
    original = fx.message("m1")["text"]

    fx.message("m1")["text"] = "请阅读附件（已编辑）"
    fx.message("m1")["updatedAt"] = "2026-11-02T08:00:00.000Z"
    fx.write_db()
    report = _ingest(
        _export(fx, mode="incremental", since="2026-11-02T00:00:00.000Z"), client, volume
    )
    assert report.written == Counter(chat_message=1)
    revisions = _latest(client, "chat_message", "message_id", "m1")
    assert [r.payload["revision"] for r in revisions] == [1, 2]
    assert [r.payload["text"] for r in revisions] == [original, "请阅读附件（已编辑）"]
    assert revisions[0].id != revisions[1].id
    assert revisions[0].event_start == revisions[1].event_start  # 事件时间是消息创建时间

    # 只有 updatedAt 变化（例如点赞）：不新增
    fx.message("m1")["updatedAt"] = "2026-11-02T09:00:00.000Z"
    fx.write_db()
    report = _ingest(
        _export(fx, mode="incremental", since="2026-11-02T00:00:00.000Z"), client, volume
    )
    assert report.written == Counter()
    assert report.unchanged["chat_message"] == 1

    # 改回原文：A→B→A 是第 3 次修订，读取方取 revision 最大者
    fx.message("m1")["text"] = original
    fx.message("m1")["updatedAt"] = "2026-11-02T10:00:00.000Z"
    fx.write_db()
    _ingest(_export(fx, mode="incremental", since="2026-11-02T00:00:00.000Z"), client, volume)
    revisions = _latest(client, "chat_message", "message_id", "m1")
    assert [r.payload["revision"] for r in revisions] == [1, 2, 3]
    assert revisions[-1].payload["text"] == original
    assert revisions[-1].payload["content_sha256"] == revisions[0].payload["content_sha256"]
    assert revisions[-1].payload["source_updated_at"] > revisions[1].payload["source_updated_at"]
    assert len({r.id for r in revisions}) == 3  # 每次修订有自己的确定性 id


def test_session_title_edit_is_a_revision(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    _, client = memory_storage()
    volume = _volume(tmp_path)
    _ingest(_export(fx), client, volume)
    conv = next(c for c in fx.db["conversations"] if c["conversationId"] == "conv-1")
    conv["title"] = "新标题"
    conv["updatedAt"] = "2026-11-02T08:00:00.000Z"
    fx.write_db()
    report = _ingest(
        _export(fx, mode="incremental", since="2026-11-02T00:00:00.000Z"), client, volume
    )
    assert report.written == Counter(chat_session=1)
    titles = [
        r.payload["title"] for r in _latest(client, "chat_session", "conversation_id", "conv-1")
    ]
    assert titles == ["合成会话 A", "新标题"]


def test_overwritten_file_bytes_become_a_new_attachment_revision(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    _, client = memory_storage()
    volume = _volume(tmp_path)
    _ingest(_export(fx), client, volume)
    first = _latest(client, "chat_attachment", "file_id", "f-1")[0]
    first_path = first.payload["file_ref"]["path"]

    (fx.uploads / UID / "f1__notes.txt").write_bytes(b"rewritten bytes")
    fx.file("f-1")["updatedAt"] = "2026-11-02T08:00:00.000Z"
    fx.file("f-1")["bytes"] = len(b"rewritten bytes")
    fx.write_db()
    report = _ingest(
        _export(fx, mode="incremental", since="2026-11-02T00:00:00.000Z"), client, volume
    )
    # 消息本身没变；第二遍按“文件变化”补出附件新修订
    assert report.written == Counter(chat_attachment=1)
    revisions = _latest(client, "chat_attachment", "file_id", "f-1")
    assert [r.payload["revision"] for r in revisions] == [1, 2]
    assert revisions[1].payload["text"] == "rewritten bytes"
    assert revisions[1].payload["file_ref"]["sha256"] != revisions[0].payload["file_ref"]["sha256"]
    assert (volume.root / first_path).read_bytes() == NOTES  # 旧字节仍保留（只追加）
    assert revisions[1].event_start == revisions[0].event_start


def test_incremental_export_only_carries_changed_conversations(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    lines = _export(fx, mode="incremental", since="2026-11-01T10:04:00.000Z")
    kinds = Counter(json.loads(x)["t"] for x in lines)
    assert kinds["conversation"] == 1  # 只有 conv-1 的 m3 在窗口内
    assert kinds["message"] == 1
    assert "m4" not in "".join(lines)


def test_oversized_files_are_reported_unavailable_without_failing(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    _, client = memory_storage()
    report = _ingest(_export(fx, extra=["--max-file-bytes", "1000"]), client, _volume(tmp_path))
    assert report.complete  # too_large 是确定的，不阻塞水位
    by_file = {r.payload["file_id"]: r.payload for r in _records(client, "chat_attachment")}
    assert by_file["f-2"]["unavailable_reason"] == "too_large"
    assert by_file["f-6"]["unavailable_reason"] == "too_large"
    assert by_file["f-1"]["available"] is True


def test_unsupported_librechat_version_is_refused_before_any_data(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path, version="v0.9.0")
    code, lines = run_exporter(fx)
    assert code == 3
    assert [json.loads(x)["t"] for x in lines] == ["error"]
    storage, client = memory_storage()
    with pytest.raises(SourceVersionError):
        _ingest(lines, client, _volume(tmp_path))
    assert len(storage.events) == 0
    # 明确核对过后才可放行
    allowed = _export(fx, extra=["--allow-version", "0.9.0"])
    with pytest.raises(SourceVersionError):  # 适配器端同样要求显式接受
        _ingest(allowed, client, _volume(tmp_path))
    report = Ingestor(client, _volume(tmp_path), accept_versions=frozenset({"0.9.0"})).ingest(
        allowed
    )
    assert report.complete


def test_exporter_refuses_other_accounts_and_never_queries_without_account(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    code, lines = run_exporter(fx, user_id="not-an-object-id")
    assert code == 2
    assert json.loads(lines[-1])["code"] == "bad_user"
    # 另一个账号只看到它自己的会话
    code, lines = run_exporter(fx, user_id=OTHER_UID)
    assert code == 0
    text = "".join(lines)
    assert "OTHER-USER-TEXT" in text
    assert "请阅读附件" not in text


def test_empty_full_account_does_not_advance_a_cursor(tmp_path: Path) -> None:
    fx = make_fixture(tmp_path)
    code, lines = run_exporter(fx, user_id="000000000000000000000000")
    assert code == 0
    end = json.loads(lines[-1])
    assert end["suspect_empty"] is True
    _, client = memory_storage()
    report = _ingest(lines, client, _volume(tmp_path))
    assert report.suspect_empty and not report.complete  # 不推进水位
