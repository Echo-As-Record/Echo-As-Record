"""Instance-serialized legacy inbox persistence (POSIX local filesystem)."""

import fcntl
import glob
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import IO, Any

from starlette.datastructures import UploadFile

from ear_ingest.delivery import Delivery
from ear_ingest.record import component, filename, layout

CHUNK_SIZE = 1024 * 1024


def contained(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Inbox path escapes root or is a symlink")
    # Do not follow even internal symlinks: they could alias another instance.
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Symlink in inbox path")
    return path


def copy(source: IO[bytes], destination: IO[bytes]) -> int:
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        destination.write(chunk)
        size += len(chunk)
    return size


def persist(
    root: Path,
    instance: str,
    payload: dict[str, Any],
    upload: UploadFile,
    delivery: Delivery | None = None,
) -> dict:
    root = root.expanduser().resolve()
    instance_dir = contained(root, root / component(instance))
    instance_dir.mkdir(parents=True, exist_ok=True)
    lock_path = contained(root, instance_dir / ".ingest.lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if delivery:
            delivery.publish(instance)
        return _persist(root, instance_dir, payload, upload, delivery)


def _persist(
    root: Path,
    instance_dir: Path,
    payload: dict[str, Any],
    upload: UploadFile,
    delivery: Delivery | None = None,
) -> dict:
    date, session, stem, attachment = layout(payload)
    directory = contained(root, instance_dir / date / "phone" / session)
    directory.mkdir(parents=True, exist_ok=True)
    path = contained(root, directory / f"{stem}.json")
    stored_name = f"{attachment}__01__{filename(upload.filename or 'file')}"
    target = contained(root, directory / stored_name)
    previous: list[tuple[Path, dict]] = []
    referenced: set[Path] = set()
    # Session dirs end with "__<group_id>"; retries (even with a changed session timestamp)
    # stay inside them, so scan only those instead of the whole device history.
    session_glob = f"*/phone/*__{glob.escape(payload['group_id'])}/*.json"
    for candidate in instance_dir.glob(session_glob):
        if candidate.name.startswith("."):
            continue
        contained(root, candidate)
        try:
            record = json.loads(candidate.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(record, dict):
            continue
        meta = record.get("segment_meta")
        same = (
            record.get("group_id") == payload["group_id"]
            and record.get("type") == payload["type"]
            and isinstance(meta, dict)
            and meta.get("segmentIndex") == payload["segment_meta"]["segmentIndex"]
        )
        if same:
            previous.append((candidate, record))
        else:
            for entry in record.get("files", []):
                referenced.add(contained(root, candidate.parent / entry["stored_name"]))
    if target in referenced:
        raise ValueError("Attachment name already belongs to another record")
    record = dict(payload)
    record["id"] = previous[0][1]["id"] if previous else uuid.uuid4().hex
    temporaries: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".ingest-", delete=False) as stream:
            staged_file = Path(stream.name)
            temporaries.append(staged_file)
            size = copy(upload.file, stream.file)
        record["files"] = [
            {
                "field_name": "file",
                "original_name": upload.filename or "file",
                "stored_name": stored_name,
                "size": size,
                "content_type": upload.content_type or "application/octet-stream",
            }
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".ingest-", delete=False
        ) as stream:
            staged_record = Path(stream.name)
            temporaries.append(staged_record)
            json.dump(record, stream, ensure_ascii=False, indent=2)
        obsolete = []
        for old_path, old_record in previous:
            if old_path != path:
                obsolete.append(old_path)
            for entry in old_record.get("files", []):
                old_file = contained(root, old_path.parent / entry["stored_name"])
                if old_file != target and old_file not in referenced:
                    obsolete.append(old_file)
        if delivery:
            delivery.prepare(
                instance_dir.name, record, staged_file, target, staged_record, path, obsolete
            )
            temporaries.clear()  # Durable publication owns both files through crashes.
            delivery.publish(instance_dir.name)
        else:
            os.replace(staged_file, target)
            os.replace(staged_record, path)
            for old in obsolete:
                old.unlink(missing_ok=True)
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
    return {"ok": True, "record_id": record["id"], "path": str(path)}
