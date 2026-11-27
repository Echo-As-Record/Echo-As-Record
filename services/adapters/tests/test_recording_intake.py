from __future__ import annotations

import gzip
import io
import os
import sqlite3
import stat
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import BinaryIO

import pytest
from ear_adapter import recording_intake as ri
from ear_adapter.recording_intake import (
    IntakeConflict,
    IntakeInvalid,
    IntakeStorageError,
    ProcessingRetry,
    PublicationRejected,
    RecordingIntake,
    RecordingJob,
    WorkerBusy,
    phone_source_id,
)
from ear_storage_client import CommitUnknown

START = "2026-01-02T03:04:05Z"
END = "2026-01-02T03:04:07Z"


def wav(byte: bytes = b"\x01") -> bytes:
    dest = io.BytesIO()
    with wave.open(dest, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", ""))
        writer.writeframes(byte * 64000)
    return dest.getvalue()


def submit(
    intake: RecordingIntake, data: bytes | None = None, **metadata: str | None
) -> ri.Receipt:
    fields = dict(source="web", source_id=None, started_at=START, media_type="audio/wav")
    fields.update(metadata)
    return intake.accept(io.BytesIO(wav() if data is None else data), **fields)  # type: ignore


def test_duplicate_canonical_start_different_source_and_end(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    intake = RecordingIntake(root)
    first = submit(intake, ended_at=END)
    again = submit(
        RecordingIntake(root),
        source="phone",
        source_id="segment:1",
        started_at="2026-01-01T20:04:05.000000-07:00",
        ended_at="2026-01-02T03:04:08Z",
        media_type="audio/x-wav",
    )
    assert (first.id, False, "queued") == (first.id, first.duplicate, first.state)
    assert again == ri.Receipt(first.id, True, "queued")
    assert intake.get(first.id) == dict(id=first.id, state="queued", attempts=0, error=None)
    assert intake.get("../some/private/input") is None
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    with intake.worker() as worker:
        job = worker.claim()
        assert job is not None and job.ended_at == END and job.started_at == START
        assert job.media_type == "audio/wav"
        path = root / job.relative_path
        assert path.read_bytes() == wav()
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert worker.claim() is None


def test_conflicting_timestamp_never_binds_new_source_id(tmp_path: Path) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    submit(intake)
    with pytest.raises(IntakeConflict, match="metadata"):
        submit(intake, source="phone", source_id="not-yet", started_at=END)
    assert not submit(intake, wav(b"\x02"), source="phone", source_id="not-yet").duplicate


def test_duplicate_binds_new_identity_and_changed_content_conflicts(tmp_path: Path) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    first = submit(intake, source_id="same")
    assert submit(intake, source="phone", source_id="same").id == first.id
    with pytest.raises(IntakeConflict, match="identity"):
        submit(intake, wav(b"\x02"), source="phone", source_id="same")
    assert len(list((intake.root / "blobs").iterdir())) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "2026-01-02",
        "2026-01-02T03:04:05",
        "2026-01-02 03:04:05Z",
        "2026-03-00T03:04:05Z",
        "0001-01-01T00:00:00+01:00",
        "2026-01-02T03:04:05+24:00",
        "2026-01-02T03:04:05.0000001Z",
    ],
)
def test_invalid_start_never_reads_stream(tmp_path: Path, bad: str) -> None:
    class Unreadable(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            pytest.fail("invalid metadata read from stream")

    intake = RecordingIntake(tmp_path / "spool")
    with pytest.raises(IntakeInvalid, match="time"):
        intake.accept(
            Unreadable(), source="web", source_id=None, started_at=bad, media_type="audio/wav"
        )


@pytest.mark.parametrize(
    "metadata",
    [
        dict(source="/bad"),
        dict(source_id="../secret"),
        dict(ended_at=START),
        dict(ended_at="2026-01-01T03:04:00Z"),
        dict(media_type="application/gzip"),
        dict(kind="other"),
    ],
)
def test_other_invalid_metadata_never_reads(tmp_path: Path, metadata: dict[str, str]) -> None:
    class Unreadable(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            pytest.fail("invalid metadata read from stream")

    intake = RecordingIntake(tmp_path / "spool")
    fields = dict(source="web", source_id=None, started_at=START, media_type="audio/wav")
    fields.update(metadata)
    with pytest.raises(IntakeInvalid):
        intake.accept(Unreadable(), **fields)  # type: ignore


@pytest.mark.parametrize("data", [b"", b"RIFF\x00\x00\x00\x00NOPE", b"private file contents"])
def test_invalid_audio_does_not_publish_blob(tmp_path: Path, data: bytes) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    with pytest.raises(IntakeInvalid):
        submit(intake, data)
    assert not list((intake.root / "blobs").iterdir())


def test_mp4_header(tmp_path: Path) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    # Synthetic admission header, not a decoded audio track.
    payload = b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A \x00\x00\x00\x08mdat"
    assert submit(intake, payload, media_type="audio/mp4").state == "queued"
    with pytest.raises(IntakeInvalid):
        submit(intake, wav(), media_type="audio/mp4")


def test_limits_and_stream_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    monkeypatch.setattr(ri, "MAX_AUDIO_BYTES", 16)
    sizes: list[int | None] = []

    class ReadSizes(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            sizes.append(n)
            return super().read(n)

    with pytest.raises(IntakeInvalid, match="size limit"):
        intake.accept(
            ReadSizes(wav()), source="web", source_id=None, started_at=START, media_type="audio/wav"
        )
    assert sizes == [17]

    class BadStream(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            raise OSError("private filename")

    with pytest.raises(IntakeInvalid, match=r"^recording stream read failed$"):
        intake.accept(
            BadStream(), source="web", source_id=None, started_at=START, media_type="audio/wav"
        )
    assert not list((intake.root / "blobs").iterdir())


def test_phone_identity_and_archive(tmp_path: Path) -> None:
    audio_key = phone_source_id("Some-Phone", "Group", 0, "audio")
    ctx_key = phone_source_id("some-phone", "Group", 0, "context")
    assert audio_key == phone_source_id("some-phone", "Group", 0, "audio")
    assert audio_key != phone_source_id("some-phone", "group", 0, "audio")
    assert audio_key != ctx_key
    intake = RecordingIntake(tmp_path / "spool")
    ctx = submit(
        intake,
        gzip.compress(b'{"synthetic":true}\n'),
        source="phone",
        source_id=ctx_key,
        kind="context",
        media_type="application/gzip",
    )
    assert ctx.state == "archived" and not ctx.duplicate
    with intake.worker() as worker:
        assert worker.claim() is None
    assert intake.get(ctx.id) == dict(id=ctx.id, state="archived", attempts=0, error=None)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not gzip",
        gzip.compress(b""),
        gzip.compress(b"[]\n"),
        gzip.compress(b"{}"),
        gzip.compress(b"{}\n")[:-1],
        gzip.compress(b'{"number":NaN}\n'),
    ],
)
def test_context_rejects_incomplete_or_not_json_lines(tmp_path: Path, data: bytes) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    with pytest.raises(IntakeInvalid):
        submit(intake, data, media_type="application/gzip", kind="context")


def test_context_inflated_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ri, "MAX_CONTEXT_BYTES", 1024)
    small_compressed = gzip.compress(b"{}\n" * 1024)
    assert len(small_compressed) < 1024
    intake = RecordingIntake(tmp_path / "spool")
    with pytest.raises(IntakeInvalid, match="phone context"):
        submit(intake, small_compressed, media_type="application/gzip", kind="context")


@pytest.mark.parametrize("where", ["root", "ancestor", "blobs", "db", "journal", "lock", "blob"])
def test_symlinks_are_rejected(tmp_path: Path, where: str) -> None:
    root = tmp_path / "spool"
    external = tmp_path / "outside"
    external.mkdir(mode=0o700)
    if where == "ancestor":
        root.symlink_to(external)
        with pytest.raises(IntakeStorageError):
            RecordingIntake(root / "spool")
        assert not list(external.iterdir())
        return
    if where == "root":
        root.symlink_to(external)
        with pytest.raises(IntakeStorageError):
            RecordingIntake(root)
        return
    intake = RecordingIntake(root)
    if where == "blobs":
        (root / "blobs").rmdir()
        (root / "blobs").symlink_to(external)
    elif where in ("db", "journal", "lock"):
        name = {"db": "intake.sqlite3", "journal": "intake.sqlite3-journal", "lock": "worker.lock"}[
            where
        ]
        path = root / name
        path.unlink(missing_ok=True)
        path.symlink_to(external)
    else:
        submit(intake)
        blob = next((root / "blobs").iterdir())
        blob.unlink()
        blob.symlink_to(external)
    with pytest.raises(IntakeStorageError):
        if where == "lock":
            with intake.worker():
                pytest.fail("unsafe worker lock")
        else:
            submit(intake)
    assert not list(external.iterdir())


def test_insecure_modes_and_hard_links(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir(mode=0o755)
    with pytest.raises(IntakeStorageError):
        RecordingIntake(root)
    root.chmod(0o700)
    intake = RecordingIntake(root)
    submit(intake)
    blob = next((root / "blobs").iterdir())
    os.link(blob, tmp_path / "second-link")
    with pytest.raises(IntakeStorageError):
        submit(intake)


def test_concurrent_duplicate_receipts_one_job(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    barrier = Barrier(6)

    def send(index: int) -> ri.Receipt:
        barrier.wait(timeout=5)
        return submit(RecordingIntake(root), source_id=f"request-{index}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(send, range(6)))
    assert len({r.id for r in receipts}) == 1
    assert sum(not r.duplicate for r in receipts) == 1
    assert len(list((root / "blobs").iterdir())) == 1


def test_fsync_directory_failure_leaves_reusable_orphan_without_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    original = os.fsync

    def failed_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("private filesystem path")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failed_fsync)
        with pytest.raises(IntakeStorageError, match=r"^recording spool unavailable or unsafe$"):
            submit(intake, source_id="retry")
    with intake.worker() as worker:
        assert worker.claim() is None
    assert len(list((intake.root / "blobs").iterdir())) == 1
    assert not submit(RecordingIntake(intake.root), source_id="retry").duplicate
    assert submit(intake, source_id="retry").duplicate


def test_queue_failure_rolls_back_job_and_binding(tmp_path: Path) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    with sqlite3.connect(intake.root / "intake.sqlite3") as db:
        db.execute("""CREATE TRIGGER fail_binding BEFORE INSERT ON source_bindings
                      BEGIN SELECT RAISE(ABORT, 'sensitive injected failure'); END""")
        with pytest.raises(IntakeStorageError, match=r"^recording spool unavailable or unsafe$"):
            submit(intake, source_id="retry")
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM source_bindings").fetchone() == (0,)
        db.execute("DROP TRIGGER fail_binding")
    assert not submit(intake, source_id="retry").duplicate


def test_worker_lifetime_and_persisted_preparation(tmp_path: Path) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    receipt = submit(intake)
    with intake.worker() as worker:
        job = worker.claim()
        assert job is not None
        with pytest.raises(WorkerBusy), intake.worker():
            pass
        with worker.open_audio(job) as stream:
            assert stream.read() == wav()
        worker.save_result(job, {"transcript": "private words"})
        with pytest.raises(IntakeStorageError):
            worker.save_result(job, {"transcript": "overwritten"})
    with pytest.raises(IntakeStorageError):
        worker.claim()

    def no_prepare(_job: RecordingJob, _stream: BinaryIO) -> dict[str, object]:
        pytest.fail("saved result was not reused")

    def publish(job: RecordingJob, result: dict[str, object]) -> None:
        assert job.attempt == 2 and result == {"transcript": "private words"}
        assert intake.get(job.id) == dict(id=job.id, state="committing", attempts=2, error=None)

    with RecordingIntake(intake.root).worker() as restarted:
        with pytest.raises(IntakeStorageError):
            restarted.finish(job, state="failed")
        assert restarted.run_one(no_prepare, publish)
        assert not restarted.run_one(no_prepare, publish)
    assert intake.get(receipt.id) == dict(id=receipt.id, state="succeeded", attempts=2, error=None)


def _process_death(root: Path, marker: Path, state: str) -> None:
    with RecordingIntake(root).worker() as worker:
        job = worker.claim()
        assert job is not None
        worker.save_result(job, {"private": "persisted recognition"})
        worker.begin_commit(job)
        if state == "after_write":
            with marker.open("ab") as f:
                f.write(b"write\n")
                f.flush()
                os.fsync(f.fileno())
        os._exit(53)


@pytest.mark.parametrize("state", ["before_write", "after_write"])
def test_process_death_after_fence_never_replays(tmp_path: Path, state: str) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    receipt = submit(intake)
    marker = tmp_path / "authoritative-write"
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; from pathlib import Path; "
            'runpy.run_path(sys.argv[1])["_process_death"]('
            "Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])",
            __file__,
            str(intake.root),
            str(marker),
            state,
        ],
        timeout=15,
        check=False,
    )
    assert process.returncode == 53
    assert marker.exists() == (state == "after_write")
    assert intake.get(receipt.id) == dict(id=receipt.id, state="committing", attempts=1, error=None)
    with RecordingIntake(intake.root).worker() as worker:
        assert worker.claim() is None
        assert worker.get_result(receipt.id) == {"private": "persisted recognition"}
    assert intake.get(receipt.id) == dict(
        id=receipt.id, state="blocked", attempts=1, error="commit_unknown"
    )
    assert submit(intake).state == "blocked"
    if marker.exists():
        assert marker.read_bytes() == b"write\n"


def _prepare(_job: RecordingJob, _audio: BinaryIO) -> dict[str, object]:
    return {"private": "model output"}


@pytest.mark.parametrize(
    "error",
    [
        CommitUnknown("private id"),
        RuntimeError("private error"),
        PublicationRejected("private rejection"),
    ],
)
def test_publish_exception_preserves_result(tmp_path: Path, error: Exception) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    receipt = submit(intake)

    def publisher(_job: RecordingJob, _result: dict[str, object]) -> None:
        raise error

    with intake.worker() as worker:
        assert worker.run_one(_prepare, publisher)
        assert worker.get_result(receipt.id) == {"private": "model output"}
        assert not worker.run_one(_prepare, publisher)
    rejected = isinstance(error, PublicationRejected)
    assert intake.get(receipt.id) == dict(
        id=receipt.id,
        state="failed" if rejected else "blocked",
        attempts=1,
        error="publication_rejected" if rejected else "commit_unknown",
    )


def test_retry_is_bounded_with_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    receipt = submit(intake)
    now = 100.0
    monkeypatch.setattr(ri.time, "time", lambda: now)

    def retry(_job: RecordingJob, _audio: BinaryIO) -> dict[str, object]:
        raise ProcessingRetry("private retry reason")

    with intake.worker() as worker:
        assert worker.run_one(retry)
        assert not worker.run_one(retry)
        now += 2
        assert worker.run_one(retry)
        assert not worker.run_one(retry)
        now += 4
        assert worker.run_one(retry)
        assert not worker.run_one(retry)
    assert intake.get(receipt.id) == dict(
        id=receipt.id, state="failed", attempts=3, error="retry_exhausted"
    )


def test_worker_cannot_retry_after_entering_commit(tmp_path: Path) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    submit(intake)
    with intake.worker() as worker:
        job = worker.claim()
        assert job is not None
        with pytest.raises(IntakeStorageError):
            worker.begin_commit(job)
        worker.save_result(job, {})
        worker.begin_commit(job)
        with pytest.raises(IntakeStorageError):
            worker.finish(job, state="retry")


def test_corrupted_blob_never_reaches_prepare(tmp_path: Path) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    receipt = submit(intake)
    blob = next((intake.root / "blobs").iterdir())
    blob.chmod(0o600)
    blob.write_bytes(wav(b"\x99"))
    blob.chmod(0o400)
    with intake.worker() as worker:
        with pytest.raises(IntakeStorageError, match="content mismatch"):
            worker.run_one(_prepare)
        assert worker.get_result(receipt.id) is None


def test_bound_identity_metadata_conflict_never_reads(tmp_path: Path) -> None:
    class Unreadable(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            pytest.fail("conflicting source metadata read from stream")

    intake = RecordingIntake(tmp_path / "spool")
    submit(intake, source_id="bound")
    with pytest.raises(IntakeConflict, match="source metadata"):
        intake.accept(
            Unreadable(), source="web", source_id="bound", started_at=END, media_type="audio/wav"
        )


def test_fsync_file_failure_leaves_no_job_or_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = RecordingIntake(tmp_path / "spool")
    original = os.fsync

    def failed_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("private filesystem path")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failed_fsync)
        with pytest.raises(IntakeStorageError):
            submit(intake, source_id="retry")
    assert not list((intake.root / "blobs").iterdir())
    with intake.worker() as worker:
        assert worker.claim() is None
    assert not submit(intake, source_id="retry").duplicate
