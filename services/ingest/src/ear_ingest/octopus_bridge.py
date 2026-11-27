"""One-shot producer from a dedicated Octopus-owned finalized raw outbox."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from ear_adapter.native_channels import ChannelInputError, _parent, _pin

from ear_ingest import native
from ear_ingest import octopus_finalized as finalized
from ear_ingest.record import component

_MAX_SLOTS = 32
_MAX_ENTRIES = 1 + 2 * _MAX_SLOTS  # Scope marker + JSON/raw pairs. No recursion.
_SLOT = re.compile(r"[0-9a-f]{32}")
_MARKER = "scope.json"
_FORMAT = "octopus-raw-finalized-export.v1"


class _Retry(Exception):
    pass


class _OutsideWindow(Exception):
    pass


def _check_owner(info: os.stat_result, uid: int) -> None:
    # Permit a specific local group to read, but not to publish or change the export.
    if info.st_uid != uid or info.st_mode & 0o027:
        raise PermissionError("Untrusted Octopus export permissions")


def _candidates(parent: int) -> list[str]:
    slots: set[str] = set()
    with os.scandir(parent) as entries:
        for count, entry in enumerate(entries, 1):
            if count > _MAX_ENTRIES:
                raise ValueError("Octopus export exceeds the one-run entry limit")
            name = entry.name
            if name == _MARKER:
                continue
            slot, ext = os.path.splitext(name)
            if not _SLOT.fullmatch(slot) or ext not in (".json", ".raw"):
                raise ValueError("Not a dedicated Octopus export directory")
            slots.add(slot)
            if len(slots) > _MAX_SLOTS:
                raise ValueError("Octopus export exceeds the one-run slot limit")
    return sorted(slots)


def _file_info(parent: int, name: str, uid: int) -> os.stat_result:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        raise _Retry from None
    _check_owner(info, uid)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Invalid Octopus export file")
    return info


@contextmanager
def _frame(
    parent: int,
    chain: list[tuple[int | None, str, int]],
    slot: str,
    uid: int,
    scope: finalized.FinalizedScope,
    limit: int,
    now: datetime,
    lookback: timedelta,
) -> Iterator[BinaryIO]:
    with tempfile.TemporaryFile() as frame:
        # Snapshot and verify source bytes before committing anything to the receiver or stdout.
        # No untrusted paths: slot names are opaque tokens and no manifest can select a filename.
        try:
            event_name, raw_name = slot + ".json", slot + ".raw"
            _file_info(parent, event_name, uid)
            with _pin(parent, chain, event_name, limit=finalized.MANIFEST_BYTES) as manifest:
                _check_owner(manifest.before, uid)
                line = manifest.read_manifest()
                with io.BytesIO(line) as stream:
                    event = finalized.read_event(stream, scope, limit)
                    if stream.read(1):
                        raise ValueError("Expected only the finalized event JSON line")
                if event.finalized > now:
                    raise ValueError("Finalization is in the future")
                if not event.in_window(scope, now, lookback):
                    raise _OutsideWindow
                frame.write(line)
                if _file_info(parent, raw_name, uid).st_size != event.size:
                    raise _Retry
                with _pin(parent, chain, raw_name, limit=limit) as payload:
                    _check_owner(payload.before, uid)
                    if payload.before.st_size != event.size:
                        raise _Retry
                    with open(payload.fd, "rb", closefd=False) as stream:
                        try:
                            finalized.copy_payload(stream, frame, event)
                        except ValueError:
                            raise _Retry from None
                    payload.check()
                manifest.check()
        except ChannelInputError as exc:
            if exc.code in {"file_changed", "file_mismatch"}:
                raise _Retry from None
            # _pin also rechecks the file and ancestors after the initial stat.
            raise
        frame.seek(0)
        yield frame


def run(*, root: Path, uid: int, lookback_minutes: int, emit: str | None) -> int:
    try:
        scope = finalized.FinalizedScope.configured()
        now = datetime.now(UTC)
        if (
            not root.is_absolute()
            or not root.name
            or not 1 <= lookback_minutes <= 1440
            or scope.cutoff > now
            or not 0 <= uid < 2**32 - 1
            or (emit is not None and not _SLOT.fullmatch(emit))
        ):
            raise ValueError("Invalid Octopus bridge scope")
        lookback = timedelta(minutes=lookback_minutes)
        if emit is None:
            options = native.config()
            if options is None:
                raise ValueError("Native intake is not enabled")
            destination, limit = options
            if root == destination or root in destination.parents or destination in root.parents:
                raise ValueError("Separate export and native intake roots are required")
        else:
            # The producer needs no receiver queue, database, bearer token or network settings.
            limit = native.max_bytes()

        with _parent(root, ()) as (parent, chain):
            _check_owner(os.fstat(parent), uid)
            with _pin(parent, chain, _MARKER, limit=finalized.MANIFEST_BYTES) as marker:
                _check_owner(marker.before, uid)
                config = json.loads(marker.read_manifest(), object_pairs_hook=finalized._unique)
                if (
                    not isinstance(config, dict)
                    or config.keys() != {"format", "device", "enabled_after"}
                    or config["format"] != _FORMAT
                    or not isinstance(config["device"], str)
                    or component(config["device"]).lower() != scope.device
                    or not isinstance(config["enabled_after"], str)
                    or native.utc_datetime(config["enabled_after"]) != scope.cutoff
                ):
                    raise ValueError("Octopus export scope marker does not match")
                slots = [emit] if emit is not None else _candidates(parent)
                results = {}
                status = 0
                for slot in slots:
                    marker.check()
                    try:
                        with _frame(parent, chain, slot, uid, scope, limit, now, lookback) as frame:
                            marker.check()
                            if emit is not None:
                                shutil.copyfileobj(frame, sys.stdout.buffer, finalized.CHUNK)
                                sys.stdout.buffer.flush()
                                return 0
                            results[slot] = {
                                "status": "accepted",
                                "record_id": finalized.publish(frame)["record_id"],
                            }
                    except _OutsideWindow:
                        results[slot] = {"status": "outside_window"}
                        if emit is not None:
                            status = 2
                    except PermissionError:
                        results[slot] = {"status": "rejected"}
                        status = 2
                    except (OSError, RuntimeError, sqlite3.Error, _Retry):
                        results[slot] = {"status": "pending"}
                        status = max(status, 1)
                    except (ValueError, OverflowError, RecursionError):
                        results[slot] = {"status": "rejected"}
                        status = 2
                if emit is None:
                    print(json.dumps({"slots": results}))
                elif status:
                    print("Octopus frame not confirmed", file=sys.stderr)
                return status
    except (ValueError, OverflowError, RecursionError, PermissionError, RuntimeError):
        print("Octopus bridge scope or export rejected", file=sys.stderr)
        return 2
    except OSError:
        print("Octopus export unavailable", file=sys.stderr)
        return 1
