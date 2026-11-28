import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from ear_adapter.recording_intake import RecordingIntake, phone_source_id
from ear_ingest import octopus_bridge, octopus_finalized
from ear_ingest.app import create_app
from fastapi.testclient import TestClient

from .test_ingest import fields
from .test_native_upload import HEADERS, audio, send

SLOT = "a" * 32
OTHER = "b" * 32
DEVICE = "synthetic-finalized"
OFFSET = timezone(timedelta(hours=-7))


def z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def private(path, data):
    path.write_bytes(data)
    path.chmod(0o600)


def line(obj):
    return json.dumps(obj).encode() + b"\n"


@pytest.fixture
def export(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    root = tmp_path / "export"
    root.mkdir(mode=0o700)
    destination = tmp_path / "queue"
    legacy = tmp_path / "unused"
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=2)
    start = now - timedelta(minutes=2)
    end = start + timedelta(seconds=2)
    data = audio()
    config = {
        "format": "octopus-raw-finalized-export.v1",
        "device": DEVICE.upper(),
        "enabled_after": z(cutoff),
    }
    event = {
        "event": "record.finalized.v1",
        "device": DEVICE,
        "group_id": "Synthetic-Group",
        "segment_index": 7,
        "type": "richVoice",
        "media_type": "audio/wav",
        "started_at": start.astimezone(OFFSET).isoformat(),
        "ended_at": end.astimezone(OFFSET).isoformat(),
        "finalized_at": z(now - timedelta(minutes=1)),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    for key in (
        "EAR_INGEST_EVENT_URL",
        "EAR_INGEST_NOTIFY_URL",
        "EAR_INGEST_NATIVE_MAX_BYTES",
        "EAR_INGEST_NATIVE_PHONE_DEVICE_ID",
        "EAR_INGEST_NATIVE_PHONE_AFTER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EAR_INGEST_TOKEN", "synthetic-secret")
    monkeypatch.setenv("EAR_INGEST_NATIVE_ROOT", str(destination))
    monkeypatch.setenv("EAR_INGEST_INBOX_ROOT", str(legacy))
    monkeypatch.setenv("EAR_INGEST_OCTOPUS_DEVICE_ID", DEVICE)
    monkeypatch.setenv("EAR_INGEST_OCTOPUS_FINALIZED_AFTER", z(cutoff))
    private(root / "scope.json", line(config))
    return SimpleNamespace(
        root=root,
        destination=destination,
        legacy=legacy,
        now=now,
        cutoff=cutoff,
        start=start,
        end=end,
        data=data,
        config=config,
        event=event,
    )


def stage(export, slot=SLOT, *, include_raw=True, **event):
    private(export.root / f"{slot}.json", line(export.event | event))
    if include_raw:
        private(export.root / f"{slot}.raw", export.data)


def run(export, capsys, **options):
    args = {"root": export.root, "uid": os.geteuid(), "lookback_minutes": 15, "emit": None}
    code = octopus_bridge.run(**(args | options))
    stdout, stderr = capsys.readouterr()
    return code, json.loads(stdout)["slots"] if stdout else {}, stderr


def test_bridge_persists_exact_physical_times_and_deduplicates_with_http(
    export, capsys, monkeypatch
):
    stage(export)
    code, slots, stderr = run(export, capsys)
    assert code == 0 and not stderr
    receipt = slots[SLOT]
    assert receipt["status"] == "accepted" and run(export, capsys)[1][SLOT] == receipt
    record_id = receipt["record_id"]
    item = RecordingIntake(export.destination).get(record_id)
    assert item is not None and item["state"] == "queued"
    with sqlite3.connect(export.destination / "intake.sqlite3") as db:
        assert db.execute("SELECT started_at, ended_at FROM jobs").fetchall() == [
            (z(export.start), z(export.end))
        ]
        assert db.execute("SELECT source, source_id FROM source_bindings").fetchall() == [
            ("phone", phone_source_id(DEVICE, "Synthetic-Group", 7, "audio"))
        ]
    # Deliberately enroll the same synthetic ID for the independent HTTP scope to
    # prove that equal, independently known timestamps still share intake identity.
    monkeypatch.setenv("EAR_INGEST_NATIVE_PHONE_DEVICE_ID", DEVICE)
    monkeypatch.setenv("EAR_INGEST_NATIVE_PHONE_AFTER", z(export.cutoff))
    with TestClient(create_app()) as client:
        direct = send(
            client,
            headers=HEADERS | {"X-Device-Id": DEVICE},
            data=fields(
                group_id=export.event["group_id"],
                timestamp=z(export.start - timedelta(seconds=10)),
                segment_meta='{"segmentIndex":7}',
                segment_start_ms="10000",
                segment_end_ms="12000",
            ),
        )
    assert direct.status_code == 200 and direct.json()["record_id"] == record_id
    assert not export.legacy.exists()

    # Same phone identity, changed bytes: reject; do not change the original receipt.
    export.data = audio(b"\x02\x00")
    stage(export, sha256=hashlib.sha256(export.data).hexdigest(), size=len(export.data))
    assert run(export, capsys)[:2] == (2, {SLOT: {"status": "rejected"}})
    assert RecordingIntake(export.destination).get(record_id) == item


@pytest.mark.parametrize(
    "initial", ["raw_only", "manifest_only", "empty", "short", "extra", "hash"]
)
def test_partial_or_mismatched_export_is_left_pending_then_retried(export, capsys, initial):
    raw = export.root / f"{SLOT}.raw"
    if initial == "raw_only":
        private(raw, export.data)
    else:
        stage(export, include_raw=False)
        if initial != "manifest_only":
            private(
                raw,
                {
                    "empty": b"",
                    "short": export.data[:-1],
                    "extra": export.data + b"x",
                    "hash": b"x" * len(export.data),
                }[initial],
            )
    assert run(export, capsys)[:2] == (1, {SLOT: {"status": "pending"}})
    assert not export.destination.exists()
    stage(export)
    assert run(export, capsys)[1][SLOT]["status"] == "accepted"
    assert raw.read_bytes() == export.data  # bridge never deletes/moves the export


def test_mutation_during_snapshot_does_not_publish(export, capsys, monkeypatch):
    stage(export)
    copy = octopus_finalized.copy_payload

    def race(stream, dst, event):
        copy(stream, dst, event)
        # Replace the source after streaming but before the pinned-file recheck.
        other = export.root / f"{OTHER}.raw"
        private(other, export.data)
        other.replace(export.root / f"{SLOT}.raw")

    monkeypatch.setattr(octopus_finalized, "copy_payload", race)
    assert run(export, capsys)[:2] == (1, {SLOT: {"status": "pending"}})
    assert not export.destination.exists()


def test_cutoff_and_lookback_precede_payload_access(export, capsys, monkeypatch):
    cutoff_end = export.cutoff - timedelta(seconds=1)
    old_end = export.now - timedelta(minutes=25)
    for slot, end, final_time in ((SLOT, cutoff_end, export.now), (OTHER, old_end, old_end)):
        # Pre-cutoff even though finalized after activation; or post-cutoff but too old.
        stage(
            export,
            slot,
            include_raw=False,
            started_at=z(end - timedelta(seconds=2)),
            ended_at=z(end),
            finalized_at=z(final_time - timedelta(milliseconds=1) if slot == SLOT else final_time),
        )
    stat_file = octopus_bridge._file_info

    def metadata_only(parent, name, uid):
        assert not name.endswith(".raw")
        return stat_file(parent, name, uid)

    monkeypatch.setattr(octopus_bridge, "_file_info", metadata_only)
    assert run(export, capsys)[:2] == (0, {k: {"status": "outside_window"} for k in (SLOT, OTHER)})
    assert not export.destination.exists()


@pytest.mark.parametrize(
    "override",
    [
        {"device": "other"},
        {"timestamp": "2026-01-01T01:00:00Z"},
        {"path": "/unused"},
        {"started_at": "2026-01-01T01:00:00"},
        {"ended_at": "2026-01-01T01:00:00-00:00"},
        {"finalized_at": "2099-01-01T00:00:00Z"},
        {"size": 512 * 2**20 + 1},
        {"event": "record.writing"},
        {"sha256": "x" * 64},
    ],
)
def test_rejects_out_of_scope_or_ambiguous_metadata(export, capsys, override):
    stage(export, **override)
    assert run(export, capsys)[:2] == (2, {SLOT: {"status": "rejected"}})
    assert not export.destination.exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "no_marker",
        "marker_mismatch",
        "legacy_shape",
        "entries",
        "slots",
        "owner",
        "permissions",
        "symlink_root",
        "mixed_root",
        "no_cutoff",
        "no_native",
        "no_lookback",
        "excess_lookback",
    ],
)
def test_bad_root_or_enumeration_fail_before_delivery(export, capsys, monkeypatch, invalid):
    stage(export, "0" * 32)
    args = {}
    marker = export.root / "scope.json"
    match invalid:
        case "no_marker":
            marker.unlink()
        case "marker_mismatch":
            private(marker, line(export.config | {"device": "other"}))
        case "legacy_shape":
            (export.root / "inbox").mkdir()
        case "entries":
            for n in range(32):
                stage(export, f"{n + 1:032x}")
        case "slots":
            for n in range(32):
                stage(export, f"{n + 1:032x}", include_raw=False)
        case "owner":
            args["uid"] = os.geteuid() + 1
        case "permissions":
            export.root.chmod(0o770)
        case "symlink_root":
            link = export.root.with_name("link")
            link.symlink_to(export.root, target_is_directory=True)
            args["root"] = link
        case "mixed_root":
            monkeypatch.setenv("EAR_INGEST_NATIVE_ROOT", str(export.root / "queue"))
        case "no_cutoff":
            monkeypatch.delenv("EAR_INGEST_OCTOPUS_FINALIZED_AFTER")
        case "no_native":
            monkeypatch.delenv("EAR_INGEST_NATIVE_ROOT")
        case "no_lookback":
            args["lookback_minutes"] = 0
        case "excess_lookback":
            args["lookback_minutes"] = 1441
    assert run(export, capsys, **args)[:2] == (2, {})
    assert not export.destination.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "mode", "manifest_symlink"])
def test_unsafe_slot_files_are_rejected(export, capsys, kind):
    stage(export, include_raw=False)
    target = export.root.with_name("not-in-export")
    private(target, export.data)
    raw = export.root / f"{SLOT}.raw"
    match kind:
        case "symlink":
            raw.symlink_to(target)
        case "hardlink":
            raw.hardlink_to(target)
        case "fifo":
            os.mkfifo(raw, mode=0o600)
        case "mode":
            private(raw, export.data)
            raw.chmod(0o666)
        case "manifest_symlink":
            manifest = export.root / f"{SLOT}.json"
            manifest.replace(target)
            manifest.symlink_to(target)
    assert run(export, capsys)[:2] == (2, {SLOT: {"status": "rejected"}})
    assert not export.destination.exists()


def test_installed_cli_producer_frame_roundtrips_without_mount_or_receiver_credentials(export):
    stage(export)
    cli = str(Path(sys.executable).with_name("ear-ingest"))
    producer_env = {
        k: os.environ[k]
        for k in (
            "EAR_INGEST_OCTOPUS_DEVICE_ID",
            "EAR_INGEST_OCTOPUS_FINALIZED_AFTER",
        )
    }
    command = [
        cli,
        "octopus-bridge",
        "--export-root",
        str(export.root),
        "--octopus-uid",
        str(os.geteuid()),
        "--lookback-minutes",
        "15",
        "--emit",
        SLOT,
    ]
    result = subprocess.run(command, env=producer_env, capture_output=True, timeout=15, check=True)
    assert result.stdout == line(export.event) + export.data and result.stderr == b""
    assert not export.destination.exists()
    receiver_env = producer_env | {"EAR_INGEST_NATIVE_ROOT": str(export.destination)}
    ids = set()
    for frame, code in ((result.stdout[:-1], 2), (result.stdout, 0), (result.stdout, 0)):
        receipt = subprocess.run(
            [cli, "octopus-finalized"],
            env=receiver_env,
            input=frame,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert receipt.returncode == code
        if code:
            assert not receipt.stdout and not export.destination.exists()
        else:
            ack = json.loads(receipt.stdout)
            assert ack["ok"] is True
            ids.add(ack["record_id"])
    local = subprocess.run(
        command[:-2], env=receiver_env, capture_output=True, timeout=15, check=True
    )
    slot = json.loads(local.stdout)["slots"][SLOT]
    assert slot["status"] == "accepted" and ids == {slot["record_id"]}
    (export.root / f"{SLOT}.raw").unlink()
    incomplete = subprocess.run(
        command, env=producer_env, capture_output=True, timeout=15, check=False
    )
    assert incomplete.returncode == 1 and not incomplete.stdout
