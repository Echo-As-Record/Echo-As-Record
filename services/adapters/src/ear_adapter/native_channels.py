"""Read one explicitly selected raw capture. No inbox enumeration or processing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal, NoReturn, Protocol, TypeGuard, get_args

from ear_adapter.convert import FileRef
from ear_adapter.source import (
    _AUDIO_SUFFIXES,
    AudioAccessError,
    _check_chain,
    _directory,
    _sha256_fd,
    _signature,
)

MAX_METADATA_BYTES = 1 << 20
MAX_AUDIO_BYTES = 512 << 20

ErrorCode = Literal[
    "invalid_input",
    "invalid_locator",
    "invalid_manifest",
    "unsupported_record",
    "invalid_time",
    "invalid_attachment",
    "file_limit",
    "unsafe_file",
    "file_changed",
    "file_mismatch",
]
_ERROR_CODES = frozenset(get_args(ErrorCode))


class ChannelInputError(ValueError):
    """A fixed diagnostic code; never include a path, ID, metadata, or OS error text."""

    def __init__(self, code: ErrorCode) -> None:
        self.code: ErrorCode = (
            code if isinstance(code, str) and code in _ERROR_CODES else "invalid_input"
        )
        super().__init__(self.code)


_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_CLOCK = r"(?:[01][0-9]|2[0-3])-[0-5][0-9]-[0-5][0-9]"
_SESSION = re.compile(rf"{_CLOCK}__(?P<group>.+)")
_RECORD = re.compile(
    rf"segment_(?P<index>[0-9]{{1,10}})_richVoice_(?P<span>from_{_CLOCK}_to_{_CLOCK})\.json"
)
# Use the shared suffix allowlist. WAV is canonicalized like old Ear; a generic
# upload type or a missing type (Brain multipart) falls back to the stored suffix.
_MEDIA_BY_SUFFIX = {
    suffix: media
    for media, suffixes in _AUDIO_SUFFIXES.items()
    if media != "audio/x-wav"
    for suffix in suffixes
}
_MEDIA_ALIASES = {"audio/x-wav": "audio/wav"}


def _is_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _ID.fullmatch(value) is not None and value not in {".", ".."}


def _path_parts(value: str, code: ErrorCode) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ChannelInputError(code)
    parts = tuple(value.split("/"))
    if len(parts) > 32 or not value.isprintable() or len(value.encode("utf-8")) > 4096:
        raise ChannelInputError(code)
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or len(part.encode("utf-8")) > 255
            or any(char in part for char in "\\:*?[]")
        ):
            raise ChannelInputError(code)
    return parts


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ChannelInputError("invalid_time")
    try:
        if value.utcoffset() is not None:
            return value.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        pass
    raise ChannelInputError("invalid_time") from None


@dataclass(frozen=True, slots=True)
class CaptureInput:
    capture_id: str
    device_id: str
    record_id: str
    group_id: str
    started_at: datetime
    ended_at: datetime
    media_type: str
    file_ref: FileRef

    def __post_init__(self) -> None:
        if not all(
            _is_id(x) for x in (self.capture_id, self.device_id, self.record_id, self.group_id)
        ):
            raise ChannelInputError("invalid_input")
        start, end = _utc(self.started_at), _utc(self.ended_at)
        if end <= start:
            raise ChannelInputError("invalid_time")
        object.__setattr__(self, "started_at", start)
        object.__setattr__(self, "ended_at", end)
        ref = self.file_ref
        if (
            type(ref) is not FileRef
            or type(ref.size) is not int
            or not 0 < ref.size <= MAX_AUDIO_BYTES
            or not isinstance(ref.sha256, str)
            or _SHA256.fullmatch(ref.sha256) is None
        ):
            raise ChannelInputError("invalid_attachment")
        parts = _path_parts(ref.path, "invalid_attachment")
        suffix = Path(parts[-1]).suffix.lower()
        if not isinstance(self.media_type, str) or suffix not in _AUDIO_SUFFIXES.get(
            self.media_type, ()
        ):
            raise ChannelInputError("invalid_attachment")


class SourceChannel(Protocol):
    @property
    def channel_id(self) -> str: ...

    def load(self, record_locator: str) -> CaptureInput: ...


type _DirectoryChain = list[tuple[int | None, str, int]]


@contextmanager
def _access(code: ErrorCode) -> Iterator[None]:
    try:
        yield
    except (AudioAccessError, OSError, ValueError) as exc:
        if isinstance(exc, ChannelInputError):
            raise
        raise ChannelInputError(code) from None


def _check_ancestors(chain: _DirectoryChain) -> None:
    with _access("file_changed"):
        _check_chain(chain)


@contextmanager
def _parent(root: Path, parts: tuple[str, ...]) -> Iterator[tuple[int, _DirectoryChain]]:
    # Walk from / without resolve(): a symlink anywhere in the configured root
    # or its descendants is not an acceptable read-only input view.
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or ".." in root.parts
    ):
        raise ChannelInputError("unsafe_file")
    chain: _DirectoryChain = []
    stack = ExitStack()
    try:
        with _access("unsafe_file"):
            absolute = root.absolute()
            if absolute.anchor != "/":
                raise ChannelInputError("unsafe_file")
            fd: int | None = None
            for part in (*absolute.parts, *parts):
                fd = stack.enter_context(_directory(part, fd, chain))
                if fd is None:
                    raise ChannelInputError("unsafe_file")
            assert fd is not None
            _check_ancestors(chain)
        yield fd, chain
    finally:
        with _access("unsafe_file"):
            stack.close()


@dataclass(frozen=True, slots=True)
class _PinnedFile:
    fd: int
    parent: int
    name: str
    chain: _DirectoryChain
    before: os.stat_result

    def check(self) -> None:
        _check_ancestors(self.chain)
        with _access("file_changed"):
            linked = os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)
            if _signature(self.before) != _signature(linked) or _signature(
                self.before
            ) != _signature(os.fstat(self.fd)):
                raise ChannelInputError("file_changed")
        _check_ancestors(self.chain)

    def read_manifest(self) -> bytes:
        self.check()
        with _access("unsafe_file"):
            os.lseek(self.fd, 0, os.SEEK_SET)
            with open(self.fd, "rb", closefd=False) as stream:
                data = stream.read(self.before.st_size + 1)
        self.check()
        if len(data) != self.before.st_size:
            raise ChannelInputError("file_changed")
        return data

    def digest(self) -> str:
        self.check()
        with _access("unsafe_file"):
            os.lseek(self.fd, 0, os.SEEK_SET)
            digest = _sha256_fd(self.fd, self.before.st_size)
        self.check()
        if digest is None:
            raise ChannelInputError("file_changed")
        return digest


@contextmanager
def _pin(parent: int, chain: _DirectoryChain, name: str, *, limit: int) -> Iterator[_PinnedFile]:
    fd = None
    try:
        _check_ancestors(chain)
        with _access("unsafe_file"):
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ChannelInputError("unsafe_file")
            if not 0 < before.st_size <= limit:
                raise ChannelInputError("file_limit")
            # Even if the preflight is raced with a FIFO, the open cannot block.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            pinned = _PinnedFile(fd, parent, name, chain, before)
            pinned.check()
        yield pinned
    finally:
        if fd is not None:
            with _access("unsafe_file"):
                os.close(fd)


@contextmanager
def verified_audio(root: Path, capture: CaptureInput) -> Iterator[BinaryIO]:
    """Yield the pinned, read-only stream at byte zero; verify again on successful exit.

    Consume within this scope and do not advance durable state until normal exit.
    Name/ancestor rechecks detect replacements, not a filesystem lock or protection
    against mount changes. The root must be a trusted view of the source filesystem.
    """
    ref = capture.file_ref
    parts = _path_parts(ref.path, "invalid_attachment")
    with (
        _parent(root, parts[:-1]) as (parent, chain),
        _pin(parent, chain, parts[-1], limit=MAX_AUDIO_BYTES) as audio,
    ):
        if audio.before.st_size != ref.size or audio.digest() != ref.sha256:
            raise ChannelInputError("file_mismatch")
        with ExitStack() as stream_scope:
            with _access("unsafe_file"):
                os.lseek(audio.fd, 0, os.SEEK_SET)
                stream = stream_scope.enter_context(open(audio.fd, "rb", closefd=False))
            audio.check()
            yield stream
        if audio.digest() != ref.sha256:
            raise ChannelInputError("file_changed")


def _no_constant(_: str) -> NoReturn:
    raise ChannelInputError("invalid_manifest")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChannelInputError("invalid_manifest")
        result[key] = value
    return result


def _parse_manifest(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_no_constant
        )
    except (ValueError, RecursionError):
        raise ChannelInputError("invalid_manifest") from None
    if not isinstance(value, dict):
        raise ChannelInputError("invalid_manifest")
    return value


def _segment_number(value: object, maximum: int) -> int:
    if type(value) is int and 0 <= value <= maximum:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value):
        number = int(value)
        if number <= maximum:
            return number
    raise ChannelInputError("invalid_time")


def _segment_field(
    record: dict[str, Any], meta: dict[str, Any], snake: str, camel: str, maximum: int
) -> int:
    # Dedicated ingest preserves numeric form fields as strings; Brain also
    # accepts JSON numbers and its phone handler recognizes both key spellings.
    values = {
        _segment_number(container[key], maximum)
        for container in (record, meta)
        for key in (snake, camel)
        if key in container
    }
    if len(values) != 1:
        raise ChannelInputError("invalid_time")
    return values.pop()


def _time_span(record: dict[str, Any], expected_index: int) -> tuple[datetime, datetime]:
    raw = record.get("timestamp")
    if not isinstance(raw, str) or _TIMESTAMP.fullmatch(raw) is None:
        raise ChannelInputError("invalid_time")
    meta = record.get("segment_meta", {})
    if not isinstance(meta, dict):
        raise ChannelInputError("invalid_time")
    index = _segment_field(record, meta, "segment_index", "segmentIndex", 2**31 - 1)
    start = _segment_field(record, meta, "segment_start_ms", "segmentStartMs", 10**15 - 1)
    end = _segment_field(record, meta, "segment_end_ms", "segmentEndMs", 10**15 - 1)
    if index != expected_index or end <= start:
        raise ChannelInputError("invalid_time")
    try:
        base = datetime.fromisoformat(raw).astimezone(UTC)
        return base + timedelta(milliseconds=start), base + timedelta(milliseconds=end)
    except (OverflowError, ValueError):
        raise ChannelInputError("invalid_time") from None


def _declaration(item: dict[str, Any], *, media: str, mime_key: str) -> tuple[int, str | None]:
    size = item.get("size")
    if type(size) is not int or not 0 < size <= MAX_AUDIO_BYTES:
        raise ChannelInputError("invalid_attachment")
    if mime_key in item:
        claimed = item[mime_key]
        if not isinstance(claimed, str) or (
            claimed != "application/octet-stream" and _MEDIA_ALIASES.get(claimed, claimed) != media
        ):
            raise ChannelInputError("invalid_attachment")
    sha = item.get("sha256")
    if "sha256" in item and (not isinstance(sha, str) or _SHA256.fullmatch(sha) is None):
        raise ChannelInputError("invalid_attachment")
    return size, sha


def _attachment(record: dict[str, Any], stem: str) -> tuple[str, str, int, str | None]:
    files = record.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise ChannelInputError("invalid_attachment")
    item = files[0]
    name = item.get("stored_name")
    if (
        not isinstance(name, str)
        or len(_path_parts(name, "invalid_attachment")) != 1
        or not name.startswith(stem + "__01__")
        or item.get("field_name") != "file"
    ):
        raise ChannelInputError("invalid_attachment")
    media = _MEDIA_BY_SUFFIX.get(Path(name).suffix.lower())
    if media is None:
        raise ChannelInputError("invalid_attachment")
    size, sha = _declaration(item, media=media, mime_key="content_type")
    # Brain may also serialize a public descriptor for the same file. Never
    # use its name, ID or download URL for access; cross-check declarations only.
    public = record.get("attachments", [])
    if not isinstance(public, list) or len(public) > 1:
        raise ChannelInputError("invalid_attachment")
    if public:
        entry = public[0]
        if not isinstance(entry, dict) or entry.get("kind") not in ("audio", "file"):
            raise ChannelInputError("invalid_attachment")
        other_size, other_sha = _declaration(entry, media=media, mime_key="mime_type")
        if other_size != size or (other_sha is not None and sha not in (None, other_sha)):
            raise ChannelInputError("file_mismatch")
        sha = sha or other_sha
    return name, media, size, sha


class OctopusInboxChannel:
    """Explicit local phone richVoice manifests: dedicated ingest and Octopus Brain.

    The record locator and returned file reference are both relative to the data
    root (the parent of inbox). Directory labels reflect the producer's local
    zone, not this reader's. Only the aware manifest timestamp determines UTC.
    """

    channel_id = "octopus"

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, record_locator: str) -> CaptureInput:
        parts = _path_parts(record_locator, "invalid_locator")
        if len(parts) != 7 or parts[:2] != ("inbox", "brain") or parts[4] != "phone":
            raise ChannelInputError("invalid_locator")
        device, day, session, filename = parts[2], parts[3], parts[5], parts[6]
        session_match, segment_match = _SESSION.fullmatch(session), _RECORD.fullmatch(filename)
        if not _is_id(device) or session_match is None or segment_match is None:
            raise ChannelInputError("invalid_locator")
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            raise ChannelInputError("invalid_locator") from None
        index = segment_match["index"]
        stem = f"segment_{index}_{segment_match['span']}"
        with (
            _parent(self.root, parts[:-1]) as (parent, chain),
            _pin(parent, chain, filename, limit=MAX_METADATA_BYTES) as manifest,
        ):
            original = manifest.read_manifest()
            record = _parse_manifest(original)
            if record.get("type") != "richVoice" or record.get(
                "processing_status", "ready"
            ) not in ("ready", None):
                raise ChannelInputError("unsupported_record")
            record_id, group = record.get("id"), record.get("group_id")
            if not _is_id(record_id) or not _is_id(group) or group != session_match["group"]:
                raise ChannelInputError("invalid_manifest")
            started, ended = _time_span(record, int(index))
            name, media, size, declared_sha = _attachment(record, stem)
            with _pin(parent, chain, name, limit=MAX_AUDIO_BYTES) as audio:
                manifest.check()
                if audio.before.st_size != size:
                    raise ChannelInputError("file_mismatch")
                # Both tracked writers publish the audio before publishing JSON.
                # Without a producer hash, reject a newer audio mtime: this can
                # be a same-size replacement whose manifest has not arrived yet.
                # Timestamp-preserving/matching writes cannot be made atomic here.
                if declared_sha is None and audio.before.st_mtime_ns > manifest.before.st_mtime_ns:
                    raise ChannelInputError("file_changed")
                sha = audio.digest()
                if declared_sha is not None and sha != declared_sha:
                    raise ChannelInputError("file_mismatch")
                if manifest.read_manifest() != original:
                    raise ChannelInputError("file_changed")
                audio.check()
                manifest.check()
        # Preserve old Ear's capture identity (eef64ce, ear/ingest/parsing.py):
        # exact instance and record IDs, lowercase unprefixed hex checksum.
        identity = hashlib.sha256(f"brain-capture\x1f{device}\x1f{record_id}\x1f{sha}".encode())
        return CaptureInput(
            capture_id=f"cap_{identity.hexdigest()[:24]}",
            device_id=device,
            record_id=record_id,
            group_id=group,
            started_at=started,
            ended_at=ended,
            media_type=media,
            file_ref=FileRef("/".join((*parts[:-1], name)), sha, size),
        )
