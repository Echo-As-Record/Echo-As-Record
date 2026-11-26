"""摄入引擎对合成导出流的行为：协议错误、完整性、重试、超长文本、批量与存储故障。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import DataChanged, FileRef
from ear_storage_client import Query
from ear_storage_client.memory import MemoryClient
from ear_storage_client.remote_errors import ResourceLimit, ServiceUnavailable
from librechat_adapter import Ingestor, LibreChatConfig, PrivateVolume
from librechat_adapter.engine import (
    ExportFailed,
    RetryLater,
    SourceVersionError,
    StorageRejected,
    StreamIncomplete,
)
from librechat_adapter.testing import (
    attachment,
    blob,
    conversation,
    end,
    header,
    join,
    line,
    memory_storage,
    message,
)
from librechat_adapter.wire import ProtocolError, StreamError


def _volume(tmp_path: Path) -> PrivateVolume:
    root = tmp_path / "vol"
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    return PrivateVolume(root)


def _items(client: MemoryClient, data_type: str) -> list[Any]:
    return list(client.query(Query(data_type, limit=1000)).items)


def _tmp_files(volume: PrivateVolume) -> list[Path]:
    return list((volume.root / "librechat" / "tmp").iterdir())


# -- protocol ------------------------------------------------------------------------------------
def test_stream_must_start_with_a_supported_header(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    with pytest.raises(ProtocolError):
        Ingestor(client, volume).ingest([conversation(), end()])
    with pytest.raises(SourceVersionError):
        Ingestor(client, volume).ingest([header(version="0.8.8"), end()])
    bad_format = line(**{**json.loads(header()), "format": "other/9"})
    with pytest.raises(SourceVersionError):
        Ingestor(client, volume).ingest([bad_format, end()])
    ok = Ingestor(client, volume, accept_versions=frozenset({"0.8.8"})).ingest(
        [header(version="0.8.8"), end()]
    )
    assert ok.complete and ok.librechat_version == "0.8.8"


@pytest.mark.parametrize(
    "lines",
    [
        [header(), "not json\n", end()],
        [header(), line(t="mystery"), end()],
        [header(), line(no_type=True), end()],
        [header(), header(), end()],
        [header(), end(), message()],
        [header(), line(t="blob_end", blob_id=1, size=3, sha256="0" * 64), end()],
        [header(), line(t="end", complete=True, watermark="2026-11-01T00:00:00+08:00")],
    ],
)
def test_protocol_violations_abort_the_run(tmp_path: Path, lines: list[str]) -> None:
    storage, client = memory_storage()
    with pytest.raises(ProtocolError):
        Ingestor(client, _volume(tmp_path)).ingest(lines)
    assert len(storage.events) == 0


def test_error_frames_map_to_distinct_failures(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    with pytest.raises(SourceVersionError):
        Ingestor(client, volume).ingest([line(t="error", code="unsupported_version")])
    with pytest.raises(ExportFailed) as caught:
        Ingestor(client, volume).ingest([header(), line(t="error", code="runtime_error")])
    assert caught.value.code == "runtime_error"


def test_truncated_stream_writes_complete_items_but_is_not_complete(tmp_path: Path) -> None:
    storage, client = memory_storage()
    with pytest.raises(StreamIncomplete) as caught:
        Ingestor(client, _volume(tmp_path)).ingest([header(), conversation(), message()])
    assert caught.value.report.written == Counter(chat_session=1, chat_message=1)
    assert len(_items(client, "chat_message")) == 1
    # 重新导出完整流：幂等补齐，不重复
    report = Ingestor(client, _volume(tmp_path)).ingest(
        [header(), conversation(), message(), message("m2"), end()]
    )
    assert report.complete
    assert report.written == Counter(chat_message=1)
    assert len(storage.events) == 3


def test_invalid_items_are_rejected_and_counted_without_stopping(tmp_path: Path) -> None:
    _, client = memory_storage()
    stream = [
        header(),
        conversation("bad id!"),
        conversation("c1"),
        message("../etc", "c1"),
        message("m1", "c1"),
        message("m2", "c1", created_at="2026-11-01T10:00:01+08:00"),
        line(**{**json.loads(message("m3", "c1")), "text": 5}),
        end(),
    ]
    report = Ingestor(client, _volume(tmp_path)).ingest(stream)
    assert report.complete
    assert sum(report.rejected.values()) == 4
    assert all("2026" not in key and "etc" not in key for key in report.rejected)  # 不回显内容
    assert [r.payload["message_id"] for r in _items(client, "chat_message")] == ["m1"]


# -- blobs ---------------------------------------------------------------------------------------
def test_blob_roundtrip_dedups_by_content_and_handles_empty_files(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    data = bytes(range(256)) * 10
    stream = join(
        [
            header(),
            blob(1, data, chunk=700),
            blob(2, data, chunk=1000),
            blob(3, b""),
            attachment("a", blob_id=1),
            attachment("b", blob_id=2, mid="m2"),
            attachment("c", blob_id=3, mid="m3"),
            end(),
        ]
    )
    report = Ingestor(client, volume).ingest(stream)
    assert report.complete
    assert (report.blobs_new, report.blobs_reused) == (2, 1)  # 同内容只占一份
    by_id = {r.payload["file_id"]: r.payload for r in _items(client, "chat_attachment")}
    assert by_id["a"]["file_ref"]["sha256"] == by_id["b"]["file_ref"]["sha256"]
    assert by_id["c"]["file_ref"]["size"] == 0
    assert by_id["c"]["available"] is True
    assert _tmp_files(volume) == []


def test_integrity_failure_discards_bytes_and_recovers_with_a_new_revision(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    data = b"hello world" * 50
    broken = join([header(), blob(1, data, sha256="0" * 64), attachment("a", blob_id=1), end()])
    report = Ingestor(client, volume).ingest(broken)
    assert report.retry_needed and not report.complete  # 不推进水位
    first = _items(client, "chat_attachment")[0].payload
    assert first["available"] is False
    assert first["unavailable_reason"] == "integrity_mismatch"
    assert _tmp_files(volume) == []
    assert not [p for p in (volume.root / "librechat" / "blobs").rglob("*") if p.is_file()]

    good = join([header(), blob(1, data), attachment("a", blob_id=1), end()])
    report = Ingestor(client, volume).ingest(good)
    assert report.complete
    revisions = sorted(_items(client, "chat_attachment"), key=lambda r: r.payload["revision"])
    assert [r.payload["revision"] for r in revisions] == [1, 2]
    assert revisions[1].payload["available"] is True
    assert volume.verify(FileRef.model_validate(revisions[1].payload["file_ref"]))


def test_malformed_blob_streams_are_protocol_errors_and_leave_no_residue(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    ok = blob(1, b"x" * 10, chunk=4)
    cases = [
        [header(), line(t="blob", blob_id=1, seq=0, b64="@@@@")],
        [header(), ok[0], ok[2]],  # 序号跳过 1
        [header(), line(t="blob", blob_id=1, seq=3, b64="AAAA")],  # 不从 0 开始
        [header(), *ok[:-1]],  # 缺 blob_end
        [header(), *ok, line(t="blob", blob_id=1, seq=3, b64="AAAA")],  # 结束后又来数据
    ]
    for lines in cases:
        with pytest.raises(StreamError):  # 协议错误，或（缺 blob_end 时）流不完整
            Ingestor(client, volume).ingest(lines)
        assert _tmp_files(volume) == []


def test_untrusted_frame_statistics_do_not_echo_arbitrary_labels(tmp_path: Path) -> None:
    _, client = memory_storage()
    report = Ingestor(client, _volume(tmp_path)).ingest(
        join(
            [
                header(),
                attachment("a", unavailable="synthetic_private_canary"),
                line(
                    t="end",
                    complete=True,
                    watermark="2026-11-01T23:58:00.000Z",
                    counts={"skipped": {"/private/path": 1, "invalid_message": 2}},
                ),
            ]
        )
    )
    assert report.summary()["exporter_skipped"] == {"invalid_message": 2}
    assert report.summary()["unavailable"] == {"unavailable": 1}
    assert _items(client, "chat_attachment")[0].payload["unavailable_reason"] == "unavailable"


def test_oversized_blob_is_unavailable_but_not_retryable(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    config = LibreChatConfig(max_blob_bytes=100)
    stream = join([header(), blob(1, b"y" * 500, chunk=50), attachment("a", blob_id=1), end()])
    report = Ingestor(client, volume, config).ingest(stream)
    assert report.complete
    assert _items(client, "chat_attachment")[0].payload["unavailable_reason"] == "too_large"
    assert _tmp_files(volume) == []


def test_transient_unavailability_blocks_the_watermark_permanent_does_not(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    permanent = Ingestor(client, volume).ingest(
        [header(), attachment("a", unavailable="missing_on_disk"), end()]
    )
    assert permanent.complete
    transient = Ingestor(client, volume).ingest(
        [header(), attachment("b", unavailable="read_error"), end()]
    )
    assert not transient.complete and transient.retry_needed


# -- text ------------------------------------------------------------------------------------------
def test_long_text_overflows_to_the_volume_without_losing_content(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    full = "长文" * 40 + "END"
    report = Ingestor(client, volume, LibreChatConfig(max_text_chars=20)).ingest(
        [header(), message(text=full), end()]
    )
    assert report.text_overflow == 1
    payload = _items(client, "chat_message")[0].payload
    assert payload["text"] == full[:20]
    assert payload["text_truncated"] is True
    assert payload["text_chars"] == len(full)
    ref = FileRef.model_validate(payload["text_file_ref"])
    assert (volume.root / ref.path).read_text(encoding="utf-8") == full
    assert volume.verify(ref)


def test_text_is_sanitised_for_json_and_postgresql(tmp_path: Path) -> None:
    _, client = memory_storage()
    nasty = "a\x00b\ud800c\udfffd"
    Ingestor(client, _volume(tmp_path)).ingest(
        [header(), conversation(title=nasty), message(text=nasty, sender=nasty), end()]
    )
    payload = _items(client, "chat_message")[0].payload
    assert payload["text"] == "a\ufffdb\ufffdc\ufffdd"
    assert "\x00" not in json.dumps(_items(client, "chat_session")[0].payload)
    json.dumps(payload, ensure_ascii=False).encode("utf-8")  # 可编码


def test_attachment_text_prefers_extracted_then_decoded_then_metadata(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    config = LibreChatConfig(max_attachment_text_chars=10)
    stream = join(
        [
            header(),
            blob(1, b"binary\x00data" * 3),
            blob(2, b"0123456789abcdef"),
            attachment("x", blob_id=1, text="提取出来的文本，超过十个字符的部分被截断"),
            attachment("y", blob_id=1, mid="m2", media_type="application/pdf"),
            attachment("z", blob_id=2, mid="m3"),
            attachment("w", mid="m4", unavailable="bad_path"),
            end(),
        ]
    )
    Ingestor(client, volume, config).ingest(stream)
    by_id = {r.payload["file_id"]: r.payload for r in _items(client, "chat_attachment")}
    assert (by_id["x"]["text_kind"], by_id["x"]["text"]) == ("extracted", "提取出来的文本，超过")
    assert by_id["x"]["text_truncated"] is True
    assert by_id["y"]["text_kind"] == "metadata"
    assert "application/pdf" in by_id["y"]["text"]
    assert (by_id["z"]["text_kind"], by_id["z"]["text"]) == ("decoded", "0123456789")
    assert by_id["z"]["text_truncated"] is True
    assert "[unavailable: bad_path]" in by_id["w"]["text"]
    assert "file_ref" not in by_id["w"]


def test_text_like_files_are_decoded_by_extension_but_binaries_are_not(tmp_path: Path) -> None:
    _, client = memory_storage()
    stream = join(
        [
            header(),
            blob(1, b"print('hi')\n"),
            blob(2, b"\x89PNG\x00\x00"),
            attachment("code", blob_id=1, media_type="application/octet-stream", filename="a.py"),
            attachment(
                "bin", blob_id=2, mid="m2", media_type="application/octet-stream", filename="a.py"
            ),
            end(),
        ]
    )
    Ingestor(client, _volume(tmp_path)).ingest(stream)
    by_id = {r.payload["file_id"]: r.payload for r in _items(client, "chat_attachment")}
    assert (by_id["code"]["text_kind"], by_id["code"]["text"]) == ("decoded", "print('hi')\n")
    assert by_id["bin"]["text_kind"] == "metadata"  # 含 NUL 的“.py”不当文本


class _Blind:
    """get_many 永远返回空：模拟读权限缺失（① 对不可见的 id 静默过滤）。"""

    def __init__(self, inner: MemoryClient) -> None:
        self._inner = inner

    def get_many(self, ids: Any) -> dict[str, Any]:
        return {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_a_blind_revision_probe_is_reported_instead_of_losing_edits_silently(
    tmp_path: Path,
) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    Ingestor(client, volume).ingest([header(), message(text="原文"), end()])
    edited = [header(), message(text="编辑后"), end()]
    report = Ingestor(_Blind(client), volume).ingest(edited)  # type: ignore[arg-type]
    assert report.anomalies == 1 and report.retry_needed and not report.complete
    assert [r.payload["text"] for r in _items(client, "chat_message")] == ["原文"]
    # 权限恢复后，同一份编辑正常追加为 r2
    assert Ingestor(client, volume).ingest(edited).written == Counter(chat_message=1)


# -- storage ---------------------------------------------------------------------------------------
def test_large_streams_are_batched_and_probed_in_chunks(tmp_path: Path) -> None:
    storage, client = memory_storage()
    messages = [message(f"m{i}", "c1", text=f"第 {i} 条") for i in range(1100)]
    config = LibreChatConfig(batch_records=200)
    report = Ingestor(client, _volume(tmp_path), config).ingest(
        join([header(), conversation(), messages, end()])
    )
    assert report.written == Counter(chat_session=1, chat_message=1100)
    sizes = Counter(e.batch_id for e in storage.events if isinstance(e, DataChanged))
    assert max(sizes.values()) <= 200
    again = Ingestor(client, _volume(tmp_path), LibreChatConfig(batch_records=700)).ingest(
        join([header(), conversation(), messages, end()])
    )
    assert again.written == Counter() and again.unchanged["chat_message"] == 1100


class _Flaky:
    """包装内存客户端，在写入时抛出指定异常。"""

    def __init__(self, inner: MemoryClient, error: Exception) -> None:
        self._inner = inner
        self._error = error

    def write(self, ops: Any, context: Any) -> Any:
        raise self._error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_storage_unavailable_and_limits_have_distinct_outcomes(tmp_path: Path) -> None:
    _, client = memory_storage()
    volume = _volume(tmp_path)
    stream = [header(), message(), end()]
    with pytest.raises(RetryLater):
        Ingestor(_Flaky(client, ServiceUnavailable()), volume).ingest(stream)  # type: ignore[arg-type]
    with pytest.raises(StorageRejected):
        Ingestor(_Flaky(client, ResourceLimit()), volume).ingest(stream)  # type: ignore[arg-type]
    assert Ingestor(client, volume).ingest(stream).complete  # 之后正常写入，没有半截状态
    assert len(_items(client, "chat_message")) == 1


def test_only_the_adapter_may_write_chat_records(tmp_path: Path) -> None:
    from ear_storage_client import AppendRaw, Context, PermissionDenied
    from ear_storage_client.testing import make_raw

    storage, _ = memory_storage()
    rogue = make_raw("chat_message", {"text": "x"}, start="2026-11-01T10:00:00Z")
    with pytest.raises(PermissionDenied):
        storage.client("api").write([AppendRaw(rogue)], Context.system())
