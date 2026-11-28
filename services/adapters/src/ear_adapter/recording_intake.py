"""Private, durable native recording intake; no network, discovery, or legacy service."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal, NoReturn

from ear_contracts import is_id, new_id

from ear_adapter.native_channels import MAX_AUDIO_BYTES, ChannelInputError, _parent

MAX_CONTEXT_BYTES = 16 << 20
# Only for comparing an assertion to decoded media, not for comparing two assertions.
MEDIA_END_TOLERANCE = timedelta(milliseconds=1)
DECODED_END_KEY = "decoded_ended_at"
_MAX_CONTEXT_LINE = 256 << 10
_MAX_RESULT_BYTES = 16 << 20
_BLOCK = 128 << 10
_MAX_ATTEMPTS = 3
_DB = "intake.sqlite3"
_LOCK = "worker.lock"
_BLOBS = "blobs"
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_AUDIO = {"audio/wav": ".wav", "audio/mp4": ".m4a"}
_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SOURCE_ID = re.compile(r"[A-Za-z0-9._:-]{1,512}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class IntakeConflict(ValueError):
    """Content, source binding, or capture timing conflicts with an accepted recording."""


class IntakeInvalid(ValueError):
    """Invalid metadata or stream; diagnostics never include input values."""


class IntakeStorageError(RuntimeError):
    """Unsafe or unavailable local storage. No durable acceptance is claimed."""


class WorkerBusy(RuntimeError):
    """Another process already holds the spool's worker lock."""


@dataclass(frozen=True, slots=True)
class Receipt:
    id: str
    duplicate: bool
    state: str


def _component(value: str) -> bool:
    return (
        isinstance(value, str)
        and _COMPONENT.fullmatch(value) is not None
        and value
        not in {
            ".",
            "..",
        }
    )


def phone_source_id(device: str, group: str, segment_index: int, kind: str) -> str:
    """Stable identity for validated iOS/Octopus phone fields, independent of record ID/path."""
    if (
        not _component(device)
        or not _component(group)
        or type(segment_index) is not int
        or not 0 <= segment_index <= 2**31 - 1
        or kind not in ("audio", "context")
    ):
        raise IntakeInvalid("invalid phone identity")
    key = f"{device.lower()}:{group}:{segment_index}:{kind}"
    return "phone-v1:" + hashlib.sha256(key.encode("ascii")).hexdigest()


def _utc(value: str) -> str:
    if isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None:
        try:
            return (
                datetime.fromisoformat(value.upper())
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, ValueError):
            pass
    raise IntakeInvalid("invalid recording time")


def _end(value: str | None, start: str) -> str | None:
    if value is None:
        return None
    end = _utc(value)
    if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
        raise IntakeInvalid("invalid recording interval")
    return end


def _media(media_type: str, kind: str) -> tuple[str, str, int]:
    if not isinstance(media_type, str):
        raise IntakeInvalid("unsupported recording media")
    if kind == "audio":
        media = "audio/wav" if media_type == "audio/x-wav" else media_type
        if media in _AUDIO:
            return media, _AUDIO[media], MAX_AUDIO_BYTES
    if kind == "context" and media_type in ("application/gzip", "application/x-gzip"):
        return "application/gzip", ".jsonl.gz", MAX_CONTEXT_BYTES
    raise IntakeInvalid("unsupported recording media")


def _reject_constant(_: str) -> NoReturn:
    raise ValueError


def _validate_payload(file: BinaryIO, media: str, size: int) -> None:
    file.seek(0)
    if media == "application/gzip":
        # Check complete gzip/JSONL framing, not the meaning of the archived samples.
        # Bound both the inflated total and each JSON line, independent of the input cap.
        remaining = MAX_CONTEXT_BYTES
        try:
            with gzip.GzipFile(fileobj=file) as uncompressed:
                while line := uncompressed.readline(min(remaining, _MAX_CONTEXT_LINE) + 1):
                    remaining -= len(line)
                    if (
                        remaining < 0
                        or len(line) > _MAX_CONTEXT_LINE
                        or not line.endswith(b"\n")
                        or not isinstance(
                            json.loads(line.decode("utf-8"), parse_constant=_reject_constant), dict
                        )
                    ):
                        raise ValueError
            if remaining == MAX_CONTEXT_BYTES:
                raise ValueError
        except (ValueError, EOFError, gzip.BadGzipFile, zlib.error, RecursionError):
            raise IntakeInvalid("invalid phone context") from None
        return
    header = file.read(12)
    if media == "audio/wav":
        valid = len(header) == 12 and header[:4] in (b"RIFF", b"RF64") and header[8:12] == b"WAVE"
    else:
        # Recorders normally emit ftyp first. Permit bounded leading padding boxes.
        offset = 0
        valid = False
        while len(header) == 12:
            length = int.from_bytes(header[:4], "big")
            atom = header[4:8]
            if length < 8 or offset + length > size:
                break
            if atom == b"ftyp":
                valid = length >= 16 and size > offset + length
                break
            if atom not in (b"free", b"skip", b"wide") or offset + length > _BLOCK:
                break
            offset += length
            file.seek(offset)
            header = file.read(12)
    if not valid:
        raise IntakeInvalid("invalid audio container header")


def _copy(stream: BinaryIO, dest: BinaryIO, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        want = min(_BLOCK, limit - size + 1)
        try:
            chunk = stream.read(want)
        except Exception:
            raise IntakeInvalid("recording stream read failed") from None
        if not isinstance(chunk, bytes) or len(chunk) > want:
            raise IntakeInvalid("invalid recording stream")
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise IntakeInvalid("recording exceeds size limit")
        dest.write(chunk)
        digest.update(chunk)
    if not size:
        raise IntakeInvalid("empty recording")
    return digest.hexdigest(), size


def _private_directory(fd: int) -> None:
    if (info := os.fstat(fd)).st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise IntakeStorageError("recording spool directories must be owner-only (0700)")


def _regular(info: os.stat_result, mode: int) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise IntakeStorageError("unsafe recording spool file")


def _check_file(parent: int, name: str, mode: int) -> os.stat_result:
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _regular(info, mode)
    return info


def _create_file(parent: int, name: str) -> int:
    return os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)


def _open_file(parent: int, name: str, mode: int, flags: int = _READ) -> int:
    before = _check_file(parent, name, mode)
    fd = os.open(name, flags, dir_fd=parent)
    try:
        after = os.fstat(fd)
        _regular(after, mode)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise IntakeStorageError("recording spool file changed")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _blob_name(digest: str, media: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise IntakeStorageError("invalid stored recording digest")
    return digest + _AUDIO.get(media, ".jsonl.gz")


@contextmanager
def _verified_blob(parent: int, name: str, digest: str, size: int) -> Iterator[BinaryIO]:
    fd = _open_file(parent, name, 0o400)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if before.st_size != size or hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise IntakeStorageError("recording spool content mismatch")

        def fingerprint(info: os.stat_result) -> tuple[int, ...]:
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

        expected = fingerprint(before)
        if expected != fingerprint(os.fstat(fd)):
            raise IntakeStorageError("recording spool file changed")
        stream.seek(0)
        yield stream
        linked = _check_file(parent, name, 0o400)
        if expected != fingerprint(os.fstat(fd)) or expected != fingerprint(linked):
            raise IntakeStorageError("recording spool file changed")


@contextmanager
def _safe_io() -> Iterator[None]:
    try:
        yield
    except (OSError, ChannelInputError, sqlite3.Error):
        raise IntakeStorageError("recording spool unavailable or unsafe") from None


class RecordingIntake:
    """One configured private directory on a trusted local POSIX volume.

    Create only the final directory; its existing ancestry must not contain
    symlinks. All publishers/workers must share this spool and its SQLite DB.
    Any HTTP authentication, source path validation, and rate limits are outside it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        if not self.root.name or ".." in self.root.parts:
            raise IntakeStorageError("invalid recording spool location")
        with _safe_io(), _parent(self.root.parent, ()) as (parent, _):
            with suppress(FileExistsError):
                os.mkdir(self.root.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        with self._directories(create=True) as (db_dir, blobs):
            # Refuse accidental cold reinitialization of a populated spool.
            try:
                _check_file(db_dir, _DB, 0o600)
            except FileNotFoundError:
                if os.listdir(blobs):
                    raise IntakeStorageError("recording spool database is missing") from None
                with suppress(FileExistsError):
                    os.close(_create_file(db_dir, _DB))
            with self._db(write=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if os.listdir(blobs):
                        raise IntakeStorageError("recording spool database is uninitialized")
                    db.execute("""CREATE TABLE jobs (
                        seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                        digest TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
                        kind TEXT NOT NULL, media TEXT NOT NULL,
                        started_at TEXT NOT NULL, ended_at TEXT,
                        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                        ready_at REAL NOT NULL DEFAULT 0, error TEXT, result TEXT
                    )""")
                    db.execute("CREATE INDEX ready_jobs ON jobs(state, seq) WHERE state='queued'")
                    db.execute("""CREATE TABLE source_bindings (
                        source TEXT NOT NULL, source_id TEXT NOT NULL,
                        job_id TEXT NOT NULL REFERENCES jobs(id), PRIMARY KEY(source, source_id)
                    )""")
                    db.execute("PRAGMA user_version=1")
                elif version != 1:
                    raise IntakeStorageError("unsupported recording spool version")
            os.fsync(db_dir)

    @contextmanager
    def _directories(self, *, create: bool = False) -> Iterator[tuple[int, int]]:
        with _safe_io(), _parent(self.root, ()) as (root, _):
            _private_directory(root)
            try:
                blob_dir = os.open(_BLOBS, _READ | os.O_DIRECTORY, dir_fd=root)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(_BLOBS, 0o700, dir_fd=root)
                os.fsync(root)
                blob_dir = os.open(_BLOBS, _READ | os.O_DIRECTORY, dir_fd=root)
            try:
                _private_directory(blob_dir)
                yield root, blob_dir
            finally:
                os.close(blob_dir)

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with _safe_io(), _parent(self.root, ()) as (root, _):
            _private_directory(root)
            _check_file(root, _DB, 0o600)
            with suppress(FileNotFoundError):
                _check_file(root, _DB + "-journal", 0o600)
            for suffix in ("-wal", "-shm"):
                with suppress(FileNotFoundError):
                    os.stat(_DB + suffix, dir_fd=root, follow_symlinks=False)
                    raise IntakeStorageError("unexpected recording spool journal mode")
            uri = (self.root / _DB).as_uri() + "?mode=rw"
            with closing(sqlite3.connect(uri, uri=True, timeout=15, isolation_level=None)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA synchronous=EXTRA")
                db.execute("PRAGMA foreign_keys=ON")
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise IntakeStorageError("unexpected recording spool journal mode")
                if write:
                    db.execute("BEGIN IMMEDIATE")
                with db:
                    yield db

    @contextmanager
    def stage_upload(self) -> Iterator[BinaryIO]:
        """Anonymous request staging on the private spool volume, before acceptance."""
        with self._directories() as (root, _):
            stage = ".request-" + uuid.uuid4().hex
            fd = _create_file(root, stage)
            try:
                # Unlink before writing any bytes. This stage is never a blob or a job.
                os.unlink(stage, dir_fd=root)
                with open(fd, "w+b", buffering=0, closefd=False) as stream:
                    yield stream
            finally:
                os.close(fd)

    def accept(
        self,
        stream: BinaryIO,
        *,
        source: str,
        source_id: str | None,
        started_at: str,
        media_type: str,
        ended_at: str | None = None,
        kind: str = "audio",
    ) -> Receipt:
        """Copy from the current stream position. Return only after file and queue commit."""
        if not _component(source) or (
            source_id is not None
            and (
                not isinstance(source_id, str)
                or _SOURCE_ID.fullmatch(source_id) is None
                or source_id in (".", "..")
            )
        ):
            raise IntakeInvalid("invalid recording source")
        start = _utc(started_at)
        end = _end(ended_at, start)
        media, suffix, limit = _media(media_type, kind)
        if source_id is not None:
            # Known identity metadata can be rejected before reading any bytes.
            # The final binding is still checked in the serialized write transaction.
            with self._db() as db:
                prior = db.execute(
                    "SELECT j.started_at, j.kind, j.media, j.ended_at FROM source_bindings s "
                    "JOIN jobs j ON s.job_id=j.id WHERE s.source=? AND s.source_id=?",
                    (source, source_id),
                ).fetchone()
                if prior is not None:
                    if (prior["started_at"], prior["kind"], prior["media"]) != (start, kind, media):
                        raise IntakeConflict("recording source metadata conflict")
                    if end is not None and prior["ended_at"] not in (None, end):
                        raise IntakeConflict("recording end conflict")
        with self._directories() as (_, blobs):
            stage = ".upload-" + uuid.uuid4().hex
            fd = _create_file(blobs, stage)
            try:
                with os.fdopen(fd, "w+b") as file:
                    digest, size = _copy(stream, file, limit)
                    _validate_payload(file, media, size)
                    file.flush()
                    os.fchmod(file.fileno(), 0o400)
                    os.fsync(file.fileno())
                with self._db(write=True) as db:
                    receipt = self._accept(
                        db,
                        blobs,
                        stage,
                        digest,
                        suffix,
                        size,
                        source,
                        source_id,
                        start,
                        end,
                        kind,
                        media,
                    )
                return receipt
            finally:
                # A crash can leave an unreferenced stage or blob, never a job without
                # a durable blob. No online deletion of content-addressed names.
                with suppress(FileNotFoundError):
                    os.unlink(stage, dir_fd=blobs)

    @staticmethod
    def _accept(
        db: sqlite3.Connection,
        blobs: int,
        stage: str,
        digest: str,
        suffix: str,
        size: int,
        source: str,
        source_id: str | None,
        start: str,
        end: str | None,
        kind: str,
        media: str,
    ) -> Receipt:
        row = db.execute(
            "SELECT id, started_at, ended_at, kind, media, size, state FROM jobs WHERE digest=?",
            (digest,),
        ).fetchone()
        binding = None
        if source_id is not None:
            binding = db.execute(
                "SELECT job_id FROM source_bindings WHERE source=? AND source_id=?",
                (source, source_id),
            ).fetchone()
        if binding is not None and (row is None or binding["job_id"] != row["id"]):
            raise IntakeConflict("recording source identity conflict")
        if row is not None and (row["started_at"], row["kind"], row["media"], row["size"]) != (
            start,
            kind,
            media,
            size,
        ):
            raise IntakeConflict("recording content metadata conflict")
        if row is not None and end is not None:
            asserted = row["ended_at"]
            if asserted is None:
                # Web-first: no end was asserted. Don't bind this claim until the
                # worker has saved a decoded end for these same immutable bytes.
                decoded = db.execute(
                    "SELECT json_extract(result, ?) FROM jobs WHERE id=?",
                    (f"$.{DECODED_END_KEY}", row["id"]),
                ).fetchone()[0]
                if not isinstance(decoded, str):
                    raise IntakeConflict("recording end cannot be verified")
                try:
                    verified = datetime.fromisoformat(decoded)
                    if verified.utcoffset() != timedelta(0) or verified <= datetime.fromisoformat(
                        start
                    ):
                        raise ValueError
                except ValueError:
                    raise IntakeStorageError("invalid saved recording end") from None
                if abs(datetime.fromisoformat(end) - verified) > MEDIA_END_TOLERANCE:
                    raise IntakeConflict("recording end conflict")
                # Record the first assertion, not a new publication time. The
                # result and queue state remain untouched, even after publication.
                db.execute("UPDATE jobs SET ended_at=? WHERE id=?", (end, row["id"]))
            elif asserted != end:
                raise IntakeConflict("recording end conflict")
        name = digest + suffix
        try:
            with _verified_blob(blobs, name, digest, size):
                pass
        except FileNotFoundError:
            if row is not None:
                raise IntakeStorageError("accepted recording spool file is missing") from None
            # All code paths creating this destination are serialized by SQLite.
            os.rename(stage, name, src_dir_fd=blobs, dst_dir_fd=blobs)
        # Also required for a reused orphan from an interrupted earlier accept.
        os.fsync(blobs)
        if row is None:
            job_id, state = new_id(), "queued" if kind == "audio" else "archived"
            db.execute(
                "INSERT INTO jobs (id, digest, size, kind, media, started_at, ended_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, digest, size, kind, media, start, end, state),
            )
        else:
            job_id, state = row["id"], row["state"]
        if source_id is not None and binding is None:
            db.execute("INSERT INTO source_bindings VALUES (?, ?, ?)", (source, source_id, job_id))
        return Receipt(job_id, row is not None, state)

    def get(self, id: str) -> dict[str, object] | None:
        """Public status projection. No source metadata, file refs, or recognition results."""
        if not isinstance(id, str) or not is_id(id):
            return None
        with self._db() as db:
            row = db.execute("SELECT id, state, attempts, error FROM jobs WHERE id=?", (id,))
            match = row.fetchone()
            return None if match is None else dict(match)

    @contextmanager
    def worker(self) -> Iterator[RecordingWorker]:
        """Hold the single worker lock until exit; recover jobs left by a dead worker."""
        with self._directories() as (root, _):
            with suppress(FileExistsError):
                os.close(_create_file(root, _LOCK))
            fd = _open_file(root, _LOCK, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise WorkerBusy("recording spool already has a worker") from None
                worker = RecordingWorker(self)
                worker._recover()
                try:
                    yield worker
                finally:
                    worker._active = False
            finally:
                os.close(fd)


class ProcessingRetry(RuntimeError):
    """A trusted prepare callback can explicitly request a bounded, safe retry."""


class PublicationRejected(RuntimeError):
    """A trusted publisher has positive evidence that its batch was rejected atomically."""


@dataclass(frozen=True, slots=True, repr=False)
class RecordingJob:
    """Private worker descriptor; content path is relative to the intake root."""

    id: str
    started_at: str
    ended_at: str | None  # Source assertion, never the worker-derived publication boundary.
    media_type: str
    relative_path: str
    sha256: str
    size: int
    attempt: int
    _owner: object = field(repr=False, compare=False)


type _Result = dict[str, object]
type _Prepare = Callable[[RecordingJob, BinaryIO], _Result]
type _Publish = Callable[[RecordingJob, _Result], None]
type _Finish = Literal["succeeded", "failed", "retry", "blocked"]


class RecordingWorker:
    """Create with RecordingIntake.worker(); serialize calls in one worker loop."""

    def __init__(self, intake: RecordingIntake) -> None:
        self._intake = intake
        self._lease = object()
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise IntakeStorageError("recording worker is no longer active")

    def _require_job(self, job: RecordingJob) -> None:
        self._require_active()
        if not isinstance(job, RecordingJob) or job._owner is not self._lease:
            raise IntakeStorageError("invalid recording worker claim")

    def _recover(self) -> None:
        with self._intake._db(write=True) as db:
            db.execute("""
                UPDATE jobs SET state='blocked', error='commit_unknown' WHERE state='committing'
                """)
            db.execute(
                "UPDATE jobs SET state='failed', error='retry_exhausted' "
                "WHERE state='processing' AND attempts>=?",
                (_MAX_ATTEMPTS,),
            )
            db.execute("""
                UPDATE jobs SET state='queued', ready_at=0, error='interrupted'
                WHERE state='processing'
                """)

    def claim(self) -> RecordingJob | None:
        """Atomically claim one ready audio job. An archived Context is never claimed."""
        self._require_active()
        with self._intake._db(write=True) as db:
            job = db.execute(
                "SELECT * FROM jobs WHERE state='queued' AND ready_at<=? ORDER BY seq LIMIT 1",
                (time.time(),),
            ).fetchone()
            if job is None:
                return None
            attempt = job["attempts"] + 1
            db.execute(
                "UPDATE jobs SET state='processing', attempts=?, error=NULL WHERE id=?",
                (attempt, job["id"]),
            )
            media = job["media"]
            if media not in _AUDIO or job["kind"] != "audio":
                raise IntakeStorageError("invalid stored recording media")
            return RecordingJob(
                job["id"],
                job["started_at"],
                job["ended_at"],
                media,
                f"{_BLOBS}/{_blob_name(job['digest'], media)}",
                job["digest"],
                job["size"],
                attempt,
                self._lease,
            )

    @contextmanager
    def open_audio(self, job: RecordingJob) -> Iterator[BinaryIO]:
        """A verified readonly stream, kept open for one pre-publication callback."""
        self._require_job(job)
        with (
            self._intake._directories() as (_, blobs),
            _verified_blob(
                blobs, _blob_name(job.sha256, job.media_type), job.sha256, job.size
            ) as file,
        ):
            yield file

    def get_result(self, id: str) -> _Result | None:
        """Private persisted result, available even for blocked jobs. Never serve via HTTP."""
        self._require_active()
        with self._intake._db() as db:
            row = db.execute("SELECT result FROM jobs WHERE id=?", (id,)).fetchone()
            if row is None or row["result"] is None:
                return None
            return json.loads(row["result"])

    def save_result(self, job: RecordingJob, result: _Result) -> None:
        """Freeze the bounded JSON result; it survives retries and commit uncertainty."""
        self._require_job(job)
        try:
            if type(result) is not dict:
                raise ValueError
            wire = json.dumps(result, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            if len(wire) > _MAX_RESULT_BYTES:
                raise ValueError
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise IntakeInvalid("invalid recording result") from None
        with self._intake._db(write=True) as db:
            changed = db.execute(
                "UPDATE jobs SET result=? WHERE id=? AND attempts=? "
                "AND state='processing' AND result IS NULL",
                (wire, job.id, job.attempt),
            )
            if changed.rowcount != 1:
                raise IntakeStorageError("recording claim cannot save a new result")

    def begin_commit(self, job: RecordingJob) -> None:
        """Durably fence before the first call that may change authoritative storage."""
        self._require_job(job)
        with self._intake._db(write=True) as db:
            changed = db.execute(
                "UPDATE jobs SET state='committing' WHERE id=? AND attempts=? "
                "AND state='processing' AND result IS NOT NULL",
                (job.id, job.attempt),
            )
            if changed.rowcount != 1:
                raise IntakeStorageError("recording claim cannot begin publication")

    def finish(self, job: RecordingJob, *, state: _Finish) -> None:
        """Set a terminal state or retry *before* committing. Stale claims fail closed.

        In `committing`, success/rejection must be known; otherwise use `blocked`.
        """
        self._require_job(job)
        if state not in ("succeeded", "failed", "retry", "blocked"):
            raise IntakeInvalid("invalid recording finish state")
        with self._intake._db(write=True) as db:
            claimed = db.execute(
                "SELECT state, result IS NOT NULL AS saved FROM jobs WHERE id=? AND attempts=?",
                (job.id, job.attempt),
            ).fetchone()
            if claimed is None or claimed["state"] not in ("processing", "committing"):
                raise IntakeStorageError("recording claim is not current")
            committing = claimed["state"] == "committing"
            if (
                (committing and state == "retry")
                or (not committing and state == "blocked")
                or (state == "succeeded" and not claimed["saved"])
            ):
                raise IntakeStorageError("invalid recording claim transition")
            delay = 0
            error = None
            next_state = state
            if state == "retry":
                if job.attempt >= _MAX_ATTEMPTS:
                    next_state, error = "failed", "retry_exhausted"
                else:
                    next_state, error = "queued", "processing_retry"
                    delay = 2**job.attempt
            elif state == "failed":
                error = "publication_rejected" if committing else "processing_failed"
            elif state == "blocked":
                error = "commit_unknown"
            db.execute(
                "UPDATE jobs SET state=?, error=?, ready_at=? WHERE id=?",
                (next_state, error, time.time() + delay if delay else 0, job.id),
            )

    def run_one(self, prepare: _Prepare, publish: _Publish | None = None) -> bool:
        """Process one claim; callbacks are trusted and must not log their inputs.

        Prepare may recognize/decode/read authoritative storage but must NEVER
        write it. Publish performs the authoritative write and returns None only
        on known success. Exceptions after the fence block (except an explicit
        PublicationRejected). No arbitrary exception text is retained.
        """
        job = self.claim()
        if job is None:
            return False
        result = self.get_result(job.id)
        if result is None:
            try:
                with self.open_audio(job) as stream:
                    prepared = prepare(job, stream)
                self.save_result(job, prepared)
            except ProcessingRetry:
                self.finish(job, state="retry")
                return True
            except IntakeStorageError:
                # Leave an interrupted pre-commit claim recoverable on worker restart.
                raise
            except Exception:
                self.finish(job, state="failed")
                return True
            # Use the committed serialization even in the uninterrupted case.
            result = self.get_result(job.id)
            if result is None:
                raise IntakeStorageError("recording result was not saved")
        elif publish is not None:
            # Prepared results survive crashes, but the authoritative path must
            # still refer to the very same immutable bytes before publishing them.
            with self.open_audio(job):
                pass
        if publish is not None:
            self.begin_commit(job)
            try:
                if publish(job, result) is not None:
                    raise ValueError
            except PublicationRejected:
                self.finish(job, state="failed")
                return True
            except Exception:
                self.finish(job, state="blocked")
                return True
        self.finish(job, state="succeeded")
        return True
