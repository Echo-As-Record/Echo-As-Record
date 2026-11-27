"""私有文件卷、水位状态与来源（文件/命令）的行为。"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ear_contracts import FileRef
from librechat_adapter import Ingestor, PrivateVolume, Runner
from librechat_adapter.runner import EXIT_CONFIG, EXIT_OK, EXIT_RETRY
from librechat_adapter.sources import (
    CommandSource,
    ExportRequest,
    FileSource,
    SourceUnavailable,
)
from librechat_adapter.state import AlreadyRunning, RunState, State
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
from librechat_adapter.volume import IntegrityError, VolumeError
from librechat_adapter.wire import ProtocolError

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "vol"
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    return root


# -- volume ----------------------------------------------------------------------------------------
def test_volume_is_content_addressed_atomic_and_private(tmp_path: Path) -> None:
    volume = PrivateVolume(_root(tmp_path))
    data = b"payload" * 1000
    ref, new = volume.put_bytes(data)
    again, new_again = volume.put_bytes(data)
    assert (new, new_again) == (True, False)
    assert again == ref
    sha = hashlib.sha256(data).hexdigest()
    assert ref.path == f"librechat/blobs/{sha[:2]}/{sha[2:4]}/{sha}"
    assert (ref.sha256, ref.size) == (sha, len(data))
    target = volume.root / ref.path
    assert target.read_bytes() == data
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert stat.S_IMODE(target.parent.stat().st_mode) in {0o700, 0o750}
    assert list((volume.root / "librechat" / "tmp").iterdir()) == []
    assert volume.verify(ref)
    assert volume.read_prefix(ref, 7) == b"payload"


def test_volume_self_heals_a_corrupted_blob_and_detects_it(tmp_path: Path) -> None:
    volume = PrivateVolume(_root(tmp_path))
    ref, _ = volume.put_bytes(b"abcdef")
    target = volume.root / ref.path
    target.write_bytes(b"abc")  # 截断
    assert not volume.verify(ref)
    ref2, new = volume.put_bytes(b"abcdef")  # 大小不符：重写
    assert new and ref2 == ref and volume.verify(ref)
    target.write_bytes(b"abcxyz")  # 同长度但内容被改：verify 能发现
    assert not volume.verify(ref)
    assert volume.put_bytes(b"abcdef") == (ref, True)
    assert volume.verify(ref)


def test_volume_rejects_mismatch_unsafe_roots_and_foreign_paths(tmp_path: Path) -> None:
    volume = PrivateVolume(_root(tmp_path))
    writer = volume.begin(100)
    writer.write(b"12345")
    with pytest.raises(IntegrityError):
        writer.finish(5, "0" * 64)
    writer = volume.begin(100)
    writer.write(b"12345")
    with pytest.raises(IntegrityError):
        writer.finish(6, hashlib.sha256(b"12345").hexdigest())
    assert list((volume.root / "librechat" / "tmp").iterdir()) == []
    limited = volume.begin(3)
    with pytest.raises(VolumeError):
        limited.write(b"toolong")
    ref, _ = volume.put_bytes(b"ok")
    forged = FileRef(path="librechat/state/librechat.json", sha256=ref.sha256, size=ref.size)
    assert not volume.verify(forged)  # 只接受内容寻址位置；FileRef 本身已拒绝 .. 路径
    with pytest.raises(VolumeError):
        volume.read_prefix(FileRef(path="other/place", sha256=ref.sha256, size=2), 1)

    with pytest.raises(VolumeError):
        PrivateVolume(Path("relative/path"))
    with pytest.raises(VolumeError):
        PrivateVolume(tmp_path / "missing")
    link = tmp_path / "link"
    link.symlink_to(volume.root)
    with pytest.raises(VolumeError):
        PrivateVolume(link)
    open_root = tmp_path / "open"
    open_root.mkdir()
    open_root.chmod(0o777)
    with pytest.raises(VolumeError):
        PrivateVolume(open_root)
    with pytest.raises(VolumeError):
        PrivateVolume(_root(tmp_path), file_mode=0o4755)


def test_volume_refuses_symlinked_subdirectories(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "librechat").symlink_to(outside)
    with pytest.raises(VolumeError):
        PrivateVolume(root)


def test_stale_temp_files_are_swept(tmp_path: Path) -> None:
    volume = PrivateVolume(_root(tmp_path))
    old = volume.root / "librechat" / "tmp" / "old.part"
    fresh = volume.root / "librechat" / "tmp" / "fresh.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))
    assert volume.sweep_tmp() == 1
    assert not old.exists() and fresh.exists()


# -- runner / state --------------------------------------------------------------------------------
class _Scripted:
    """每次 open 返回下一份导出流，并记录收到的请求。"""

    def __init__(self, *streams: list[str]) -> None:
        self.streams = list(streams)
        self.requests: list[ExportRequest] = []

    @contextmanager
    def open(self, request: ExportRequest) -> Iterator[Iterator[str]]:
        self.requests.append(request)
        yield iter(self.streams.pop(0))


def _runner(
    tmp_path: Path,
    source: Any,
    *,
    full_every: timedelta | None = timedelta(hours=168),
    now: datetime = T0,
) -> tuple[Runner, RunState]:
    _, client = memory_storage()
    volume = PrivateVolume(_root(tmp_path))
    state = RunState(volume.root, "librechat")
    return (
        Runner(
            source=source,
            ingestor=Ingestor(client, volume),
            state=state,
            full_every=full_every,
            clock=lambda: now,
        ),
        state,
    )


def test_first_run_is_full_then_incremental_from_the_exporters_watermark(tmp_path: Path) -> None:
    stream = [header(), conversation(), message(), end("2026-11-02T11:58:00.000Z")]
    source = _Scripted(stream, [header(mode="incremental"), end("2026-11-02T12:03:00.000Z")])
    runner, state = _runner(tmp_path, source)
    first = runner.run_once()
    assert (first.status, first.exit_code, first.advanced) == ("complete", EXIT_OK, True)
    assert source.requests[0] == ExportRequest("full")
    saved = state.load()
    assert saved.watermark == datetime(2026, 11, 2, 11, 58, tzinfo=UTC)
    assert saved.last_full_at == T0
    assert stat.S_IMODE(state.path.stat().st_mode) == 0o600
    second = runner.run_once()
    assert second.status == "complete"
    assert source.requests[1] == ExportRequest("incremental", saved.watermark)
    assert state.load().last_full_at == T0  # 增量不更新全量时间
    assert state.load().watermark == datetime(2026, 11, 2, 12, 3, tzinfo=UTC)


def test_watermark_does_not_advance_on_incomplete_or_retryable_runs(tmp_path: Path) -> None:
    good = [header(), message(), end("2026-11-02T11:00:00.000Z")]
    truncated = [header(mode="incremental"), message("m2")]
    transient = [header(mode="incremental"), attachment("a", unavailable="read_error"), end()]
    source = _Scripted(good, truncated, transient)
    runner, state = _runner(tmp_path, source)
    runner.run_once()
    before = state.load()
    result = runner.run_once()
    assert (result.status, result.exit_code, result.advanced) == ("incomplete", EXIT_RETRY, False)
    assert state.load() == before
    result = runner.run_once()
    assert (result.exit_code, result.advanced) == (EXIT_RETRY, False)
    assert state.load() == before


def test_rejected_items_complete_the_run_but_report_failure(tmp_path: Path) -> None:
    source = _Scripted([header(), message("bad id", "c1"), end()])
    runner, state = _runner(tmp_path, source)
    result = runner.run_once()
    assert (result.status, result.exit_code, result.advanced) == (
        "complete_with_rejections",
        EXIT_RETRY,
        True,
    )
    assert state.load().watermark is not None


def test_periodic_full_resync_and_forced_full(tmp_path: Path) -> None:
    stream = [header(), end("2026-11-02T11:00:00.000Z")]
    source = _Scripted(stream, stream, stream)
    runner, state = _runner(tmp_path, source, full_every=timedelta(hours=1))
    runner.run_once()
    state.save(State(datetime(2026, 11, 2, 11, 0, tzinfo=UTC), T0 - timedelta(minutes=30)))
    runner.run_once()
    assert source.requests[1].mode == "incremental"  # 半小时前刚全量过
    state.save(State(datetime(2026, 11, 2, 11, 0, tzinfo=UTC), T0 - timedelta(hours=2)))
    runner.run_once()
    assert source.requests[2].mode == "full"
    source.streams.append(stream)
    runner.run_once(force_full=True)
    assert source.requests[3].mode == "full"


def test_lost_or_corrupt_state_falls_back_to_a_safe_full_run(tmp_path: Path) -> None:
    source = _Scripted([header(), end()])
    runner, state = _runner(tmp_path, source)
    state.path.write_text("{ not json", encoding="utf-8")
    assert state.load() == State()
    runner.run_once()
    assert source.requests[0].mode == "full"


def test_only_one_adapter_may_run_per_instance(tmp_path: Path) -> None:
    runner, state = _runner(tmp_path, _Scripted([header(), end()]))
    with state.lock():
        with pytest.raises(AlreadyRunning), state.lock():
            pass
        assert runner.run_once().status == "busy"
    assert runner.run_once().status == "complete"


def test_version_and_protocol_failures_have_their_own_exit_codes(tmp_path: Path) -> None:
    source = _Scripted(
        [header(version="9.9.9"), end()],
        [header(), "garbage\n", end()],
        [header(), line(t="error", code="bad_user")],
        [header(), line(t="error", code="runtime_error")],
    )
    runner, _ = _runner(tmp_path, source)
    assert runner.run_once().exit_code == 3
    assert runner.run_once().exit_code == EXIT_CONFIG
    assert runner.run_once().status == "export_rejected"
    assert runner.run_once().status == "export_failed"


# -- sources ---------------------------------------------------------------------------------------
def test_file_source_reads_lines_and_bounds_their_length(tmp_path: Path) -> None:
    path = tmp_path / "export.ndjson"
    path.write_text("".join([header(), end()]), encoding="utf-8")
    with FileSource(path, max_line_bytes=10_000).open(ExportRequest("full")) as lines:
        assert len(list(lines)) == 2
    with (
        FileSource(path, max_line_bytes=20).open(ExportRequest("full")) as lines,
        pytest.raises(ProtocolError),
    ):
        list(lines)
    with (
        pytest.raises(SourceUnavailable),
        FileSource(tmp_path / "missing", max_line_bytes=10).open(ExportRequest("full")),
    ):
        pass


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "exporter.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def test_command_source_passes_the_request_on_stdin_and_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAR_LIBRECHAT_STORAGE_TOKEN", "super-secret-token")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    record = tmp_path / "seen.txt"
    argv = _script(
        tmp_path,
        "import os, sys\n"
        f"open({str(record)!r}, 'w').write("
        "sys.stdin.read() + '\\n' + ','.join(sorted(os.environ)))\n"
        f"sys.stdout.write({header()!r} + {end()!r})\n",
    )
    source = CommandSource(argv, max_line_bytes=100_000)
    since = datetime(2026, 11, 2, 11, 0, 0, 123000, tzinfo=UTC)
    with source.open(ExportRequest("incremental", since)) as lines:
        assert len(list(lines)) == 2
    request, env_names = record.read_text().split("\n")
    assert '"since": "2026-11-02T11:00:00.123Z"' in request
    assert '"mode": "incremental"' in request
    assert "EAR_LIBRECHAT_STORAGE_TOKEN" not in env_names
    assert "SSH_AUTH_SOCK" in env_names


def test_command_source_failures_are_sanitised_and_retryable(tmp_path: Path) -> None:
    unreachable = CommandSource(["/nonexistent/exporter"], max_line_bytes=100)
    with pytest.raises(SourceUnavailable), unreachable.open(ExportRequest("full")):
        pass
    failing = _script(
        tmp_path,
        f"import sys\nsys.stdout.write({header()!r} + {end()!r})\n"
        "sys.stderr.write('boom')\nsys.exit(7)\n",
    )
    source = CommandSource(failing, max_line_bytes=100_000)
    with pytest.raises(SourceUnavailable), source.open(ExportRequest("full")) as lines:
        assert len(list(lines)) == 2
    assert source.returncode == 7 and source.stderr_tail == b"boom"

    hanging = _script(tmp_path, "import time\ntime.sleep(60)\n")
    started = time.monotonic()
    runner, _ = _runner(tmp_path, CommandSource(hanging, max_line_bytes=100, timeout=0.5))
    result = runner.run_once()
    assert result.status == "incomplete" and result.exit_code == EXIT_RETRY
    assert time.monotonic() - started < 20  # 超时后进程被杀，不会一直挂着


def test_command_source_rejects_empty_argv() -> None:
    with pytest.raises(ValueError):
        CommandSource([], max_line_bytes=10)
    with pytest.raises(ValueError):
        CommandSource(["ssh", ""], max_line_bytes=10)


def test_full_pipeline_through_a_command_source(tmp_path: Path) -> None:
    data = b"file bytes" * 100
    body = "".join(
        join(
            [header(), conversation(), blob(1, data), attachment("a", blob_id=1), message(), end()]
        )
    )
    argv = _script(tmp_path, f"import sys\nsys.stdout.write({body!r})\n")
    storage, client = memory_storage()
    volume = PrivateVolume(_root(tmp_path))
    runner = Runner(
        source=CommandSource(argv, max_line_bytes=1_000_000),
        ingestor=Ingestor(client, volume),
        state=RunState(volume.root, "librechat"),
    )
    result = runner.run_once()
    assert result.exit_code == EXIT_OK and result.advanced
    assert len(storage.events) == 3
