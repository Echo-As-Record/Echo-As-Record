"""Synthetic-only tests for explicit raw capture acquisition."""

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ear_adapter import native_channels as channels
from ear_adapter.convert import FileRef
from ear_adapter.native_channels import (
    CaptureInput,
    ChannelInputError,
    OctopusInboxChannel,
    SourceChannel,
    verified_audio,
)

BASE = "inbox/brain/ios-test/2026-11-12/phone/20-21-29__synthetic-session"
LOCATOR = f"{BASE}/segment_1_richVoice_from_20-51-29_to_21-21-29.json"
AUDIO_NAME = "segment_1_from_20-51-29_to_21-21-29__01__segment_0002.m4a"
AUDIO = b"synthetic-audio"
SHA = "ef746a1a59afecba50d3ce36e3506ee52784236e91034569ccb54f73fbc3c632"


def _manifest() -> dict[str, Any]:
    # Dedicated ingest leaves numeric multipart offsets as strings. The local
    # date and names intentionally come from a different zone than timestamp.
    return {
        "id": "scoped-record",
        "type": "richVoice",
        "group_id": "synthetic-session",
        "name": "PhoneVoice",
        "source": "local",
        "timestamp": "2026-11-12T11:21:29.157+00:00",
        "segment_start_ms": "1800000",
        "segment_end_ms": "3600000",
        "segment_meta": {"segmentIndex": 1, "segmentStartMs": 1800000, "segmentEndMs": 3600000},
        "files": [
            {
                "field_name": "file",
                "original_name": "segment_0002.m4a",
                "stored_name": AUDIO_NAME,
                "size": len(AUDIO),
                "content_type": "audio/mp4",
            }
        ],
    }


def _write(root: Path, record: dict[str, Any] | None = None) -> tuple[Path, Path]:
    record_path = root / LOCATOR
    audio = record_path.with_name(AUDIO_NAME)
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(AUDIO)
    record_path.write_text(json.dumps(_manifest() if record is None else record), encoding="utf-8")
    return record_path, audio


def _check_error(exc, code: str) -> None:
    assert exc.value.code == code
    assert str(exc.value) == code
    assert exc.value.__cause__ is None


def test_dedicated_ingest_is_explicit_and_matches_old_ear_capture_id(tmp_path, monkeypatch):
    _write(tmp_path)

    def unexpected_enumeration(*args, **kwargs):
        pytest.fail("explicit channel must not enumerate an inbox")

    channel: SourceChannel = OctopusInboxChannel(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(os, "listdir", unexpected_enumeration)
        patch.setattr(os, "scandir", unexpected_enumeration)
        capture = channel.load(LOCATOR)
        assert capture == channel.load(LOCATOR)
    assert channel.channel_id == "octopus"
    assert capture.capture_id == "cap_3ea702ddf2581ad34f7f28ec"
    assert capture.device_id == "ios-test"
    assert capture.record_id == "scoped-record"
    assert capture.group_id == "synthetic-session"
    assert capture.started_at == datetime(2026, 11, 12, 11, 51, 29, 157000, UTC)
    assert capture.ended_at == datetime(2026, 11, 12, 12, 21, 29, 157000, UTC)
    assert capture.media_type == "audio/mp4"
    assert capture.file_ref == FileRef(f"{BASE}/{AUDIO_NAME}", SHA, len(AUDIO))
    with verified_audio(tmp_path, capture) as stream:
        assert stream.mode == "rb"
        assert stream.tell() == 0
        assert stream.read() == AUDIO
        assert stream.seek(0) == 0
        assert stream.read(2) == AUDIO[:2]
        assert not stream.writable()
    assert stream.closed


def test_octopus_phone_handler_shape_with_meta_offsets_and_public_descriptor(tmp_path):
    record = _manifest()
    record["timestamp"] = "2026-11-12T20:21:29.157+09:00"
    del record["segment_start_ms"], record["segment_end_ms"]
    del record["files"][0]["content_type"]
    record["attachments"] = [
        {
            "id": "file_001",
            "kind": "audio",
            "name": "segment_0002.m4a",
            "mime_type": "audio/mp4",
            "size": len(AUDIO),
            "url": "/unused/synthetic/attachment",
        }
    ]
    _write(tmp_path, record)
    capture = OctopusInboxChannel(tmp_path).load(LOCATOR)
    assert capture.started_at == datetime(2026, 11, 12, 11, 51, 29, 157000, UTC)
    assert capture.ended_at == datetime(2026, 11, 12, 12, 21, 29, 157000, UTC)
    assert capture.capture_id == "cap_3ea702ddf2581ad34f7f28ec"
    assert capture.media_type == "audio/mp4"
    assert capture.file_ref.sha256 == SHA


def test_audio_revision_changes_old_ear_capture_identity(tmp_path):
    manifest, audio = _write(tmp_path)
    channel = OctopusInboxChannel(tmp_path)
    first = channel.load(LOCATOR)
    audio.write_bytes(b"changed-bytes!!")
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    second = channel.load(LOCATOR)
    assert second.capture_id != first.capture_id
    assert second.record_id == first.record_id
    assert second.file_ref.sha256 == hashlib.sha256(audio.read_bytes()).hexdigest()


def test_other_channel_can_supply_a_valid_capture_and_pinned_stream(tmp_path):
    class OtherChannel:
        channel_id = "other"

        def load(self, record_locator: str) -> CaptureInput:
            return CaptureInput(
                "other-cap",
                "other-device",
                "other-record",
                "other-group",
                datetime.fromisoformat("2026-11-12T02:00:00-05:00"),
                datetime.fromisoformat("2026-11-12T02:01:00-05:00"),
                "audio/wav",
                FileRef(record_locator, SHA, len(AUDIO)),
            )

    root = tmp_path / "unrelated"
    root.mkdir()
    (root / "another file.wav").write_bytes(AUDIO)
    channel: SourceChannel = OtherChannel()
    capture = channel.load("another file.wav")
    assert capture.started_at == datetime(2026, 11, 12, 7, 0, tzinfo=UTC)
    assert capture.started_at.tzinfo is UTC
    assert capture.ended_at.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        capture.capture_id = "mutated"  # pyright: ignore[reportAttributeAccessIssue]
    with verified_audio(root, capture) as stream:
        assert stream.read() == AUDIO


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("capture_id", "oops\x1fsecret", "invalid_input"),
        ("device_id", "relative/path", "invalid_input"),
        ("record_id", "x" * 129, "invalid_input"),
        ("group_id", "..", "invalid_input"),
        ("started_at", datetime(2026, 11, 12, 12), "invalid_time"),
        ("ended_at", datetime(2026, 11, 12, 11, tzinfo=UTC), "invalid_time"),
        ("media_type", "application/json", "invalid_attachment"),
        ("file_ref", FileRef("/private.wav", SHA, 5), "invalid_attachment"),
        ("file_ref", FileRef("./private.m4a", SHA, 5), "invalid_attachment"),
        ("file_ref", FileRef("a//private.m4a", SHA, 5), "invalid_attachment"),
        ("file_ref", FileRef("a/../private.m4a", SHA, 5), "invalid_attachment"),
        ("file_ref", FileRef("private.m4a", SHA.upper(), 5), "invalid_attachment"),
        ("file_ref", FileRef("private.m4a", SHA, 0), "invalid_attachment"),
        ("file_ref", FileRef("private.wav", SHA, 5), "invalid_attachment"),
    ],
)
def test_capture_validates_even_for_non_octopus_constructors(tmp_path, field, value, code):
    _write(tmp_path)
    capture = OctopusInboxChannel(tmp_path).load(LOCATOR)
    with pytest.raises(ChannelInputError) as exc:
        replace(capture, **{field: value})
    _check_error(exc, code)


@pytest.mark.parametrize(
    "locator",
    [
        "",
        "/" + LOCATOR,
        "./" + LOCATOR,
        LOCATOR.replace("/brain/", "//brain/"),
        LOCATOR.replace("/brain/", "/brain/../"),
        LOCATOR.replace("/", "\\"),
        LOCATOR.replace("inbox/brain/", ""),
        LOCATOR.replace("phone", "record"),
        LOCATOR.replace("11-12", "02-30"),
        LOCATOR.replace("20-21-29", "26-21-29"),
        LOCATOR.replace("20-51-29", "20-51-69"),
        LOCATOR.replace(".json", ".m4a"),
        LOCATOR.replace(".json", "\ud800.json"),
        LOCATOR.replace("richVoice", "phoneContext"),
    ],
)
def test_rejects_out_of_scope_locators_without_disclosing_them(tmp_path, locator):
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(locator)
    _check_error(exc, "invalid_locator")


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"type": "phoneIMU"}, "unsupported_record"),
        ({"processing_status": "pending"}, "unsupported_record"),
        ({"processing_status": "failed"}, "unsupported_record"),
        ({"group_id": "other-group"}, "invalid_manifest"),
        ({"id": "unsafe\x1fprivate"}, "invalid_manifest"),
        ({"timestamp": "2026-11-12T11:21:29.157"}, "invalid_time"),
        ({"timestamp": "2026-11-12T11:21:29+01:99"}, "invalid_time"),
        ({"timestamp": "2026-11-12"}, "invalid_time"),
        ({"timestamp": "2026-02-30T11:21:29Z"}, "invalid_time"),
        ({"timestamp": "9999-12-31T23:59:59Z"}, "invalid_time"),
        ({"segment_start_ms": -1}, "invalid_time"),
        ({"segment_start_ms": True}, "invalid_time"),
        ({"segment_start_ms": 1800000.0}, "invalid_time"),
        ({"segment_start_ms": "1800000 "}, "invalid_time"),
        ({"segment_end_ms": "1800000", "segment_meta": {"segmentIndex": 1}}, "invalid_time"),
        ({"segment_end_ms": "1799999", "segment_meta": {"segmentIndex": 1}}, "invalid_time"),
        ({"segment_end_ms": "9999999999999999"}, "invalid_time"),
        ({"segment_meta": {"segmentIndex": 2}}, "invalid_time"),
        ({"segment_meta": {"segmentIndex": 1, "segment_index": 2}}, "invalid_time"),
        ({"segment_meta": "private invalid metadata"}, "invalid_time"),
        ({"files": []}, "invalid_attachment"),
        ({"files": [None]}, "invalid_attachment"),
        ({"files": _manifest()["files"] * 2}, "invalid_attachment"),
        ({"attachments": [{"kind": "audio", "size": 1}]}, "file_mismatch"),
        ({"attachments": [{}, {}]}, "invalid_attachment"),
        ({"attachments": {"unexpected": "private"}}, "invalid_attachment"),
    ],
)
def test_rejects_invalid_manifest_fields(tmp_path, patch, code):
    _write(tmp_path, _manifest() | patch)
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, code)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xffprivate",
        b'{"id": "one", "id": "two"}',
        b'{"private": NaN}',
        b"[]",
        b"[" * 1500 + b"0" + b"]" * 1500,
    ],
)
def test_rejects_malformed_or_ambiguous_json(tmp_path, raw):
    manifest, _ = _write(tmp_path)
    manifest.write_bytes(raw)
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, "invalid_manifest")


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"size": True}, "invalid_attachment"),
        ({"size": "15"}, "invalid_attachment"),
        ({"size": 0}, "invalid_attachment"),
        ({"size": 1}, "file_mismatch"),
        ({"sha256": "f" * 64}, "file_mismatch"),
        ({"sha256": None}, "invalid_attachment"),
        ({"content_type": "video/mp4"}, "invalid_attachment"),
        ({"content_type": "audio/wav"}, "invalid_attachment"),
        ({"field_name": "other"}, "invalid_attachment"),
        ({"stored_name": "../private.m4a"}, "invalid_attachment"),
        ({"stored_name": "/private.m4a"}, "invalid_attachment"),
        ({"stored_name": "private.m4a"}, "invalid_attachment"),
        ({"stored_name": AUDIO_NAME.removesuffix(".m4a") + ".json"}, "invalid_attachment"),
    ],
)
def test_rejects_attachment_declaration_mismatch(tmp_path, fields, code):
    record = _manifest()
    record["files"][0].update(fields)
    _write(tmp_path, record)
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, code)


@pytest.mark.parametrize("which", ["metadata", "audio"])
def test_bounds_metadata_and_audio(tmp_path, monkeypatch, which):
    manifest, audio = _write(tmp_path)
    if which == "metadata":
        monkeypatch.setattr(channels, "MAX_METADATA_BYTES", manifest.stat().st_size - 1)
    else:
        # The declaration stays within the limit; the physical file exceeds it.
        with audio.open("ab") as stream:
            stream.write(b"extra")
        monkeypatch.setattr(channels, "MAX_AUDIO_BYTES", len(AUDIO))
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, "file_limit")


@pytest.mark.parametrize("target", ["record", "audio"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_unsafe_manifest_or_audio_leaf_is_never_read(tmp_path, target, kind):
    manifest, audio = _write(tmp_path)
    victim = manifest if target == "record" else audio
    other = tmp_path / "private-outside-inbox"
    victim.rename(other)
    if kind == "symlink":
        victim.symlink_to(other)
    elif kind == "hardlink":
        victim.hardlink_to(other)
    elif kind == "fifo":
        os.mkfifo(victim)
    else:
        victim.mkdir()
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, "unsafe_file")


def test_ancestor_and_root_symlinks_are_rejected(tmp_path):
    root = tmp_path / "data"
    manifest, _ = _write(root)
    capture = OctopusInboxChannel(root).load(LOCATOR)
    linked_root = tmp_path / "root-link"
    linked_root.symlink_to(root, target_is_directory=True)
    directory = manifest.parent
    moved = tmp_path / "private-dir"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    for candidate in (linked_root, root):
        with pytest.raises(ChannelInputError) as exc:
            OctopusInboxChannel(candidate).load(LOCATOR)
        _check_error(exc, "unsafe_file")
        with pytest.raises(ChannelInputError) as exc, verified_audio(candidate, capture):
            pytest.fail("symlinked root/ancestor must not yield bytes")
        _check_error(exc, "unsafe_file")


def test_same_size_audio_written_after_its_hashless_manifest_is_not_accepted(tmp_path):
    manifest, audio = _write(tmp_path)
    when = manifest.stat().st_mtime_ns + 10_000_000
    audio.write_bytes(b"changed-bytes!!")
    assert audio.stat().st_size == len(AUDIO)
    os.utime(audio, ns=(when, when))
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, "file_changed")


@pytest.mark.parametrize("target", ["manifest", "audio", "ancestor"])
def test_load_revalidates_pinned_pair_during_hash(tmp_path, monkeypatch, target):
    manifest, audio = _write(tmp_path)
    original_hash = channels._sha256_fd

    def exchange_during_hash(fd: int, size: int) -> str | None:
        result = original_hash(fd, size)
        if target == "ancestor":
            directory = manifest.parent
            directory.rename(tmp_path / "moved")
            _write(tmp_path)
        else:
            victim = manifest if target == "manifest" else audio
            replacement = tmp_path / "replacement"
            replacement.write_bytes(victim.read_bytes())
            replacement.replace(victim)
        return result

    monkeypatch.setattr(channels, "_sha256_fd", exchange_during_hash)
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, "file_changed")


@pytest.mark.parametrize("mode", ["content", "size", "hardlink"])
def test_reacquisition_rejects_invalid_audio_before_yield(tmp_path, mode):
    _, audio = _write(tmp_path)
    capture = OctopusInboxChannel(tmp_path).load(LOCATOR)
    if mode == "hardlink":
        (tmp_path / "reference").hardlink_to(audio)
    else:
        audio.write_bytes(b"changed-bytes!!" if mode == "content" else b"short")
    with pytest.raises(ChannelInputError) as exc, verified_audio(tmp_path, capture):
        pytest.fail("invalid audio must not be exposed")
    _check_error(exc, "unsafe_file" if mode == "hardlink" else "file_mismatch")


@pytest.mark.parametrize("mode", ["in-place", "replacement", "ancestor"])
def test_verified_audio_checks_again_before_context_returns(tmp_path, mode):
    manifest, audio = _write(tmp_path)
    capture = OctopusInboxChannel(tmp_path).load(LOCATOR)
    with pytest.raises(ChannelInputError) as exc, verified_audio(tmp_path, capture) as stream:
        assert stream.read() == AUDIO
        if mode == "in-place":
            audio.write_bytes(b"changed-bytes!!")
        elif mode == "replacement":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(AUDIO)
            replacement.replace(audio)
            assert stream.seek(0) == 0
            assert stream.read() == AUDIO
        else:
            manifest.parent.rename(tmp_path / "moved")
            _write(tmp_path)
    assert stream.closed
    _check_error(exc, "file_changed")


def test_verified_audio_does_not_convert_consumer_errors(tmp_path):
    _write(tmp_path)
    capture = OctopusInboxChannel(tmp_path).load(LOCATOR)
    with (
        pytest.raises(OSError, match="consumer failure"),
        verified_audio(tmp_path, capture) as stream,
    ):
        raise OSError("consumer failure")
    assert stream.closed


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "regular"])
def test_replacement_between_lstat_and_open_does_not_read_audio(tmp_path, monkeypatch, replacement):
    _, audio = _write(tmp_path)
    original_open = os.open
    original = tmp_path / "displaced"
    audio_read = False

    def replace_then_open(name, flags, *args, **kwargs):
        if name == audio.name:
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            audio.rename(original)
            if replacement == "symlink":
                audio.symlink_to(original)
            elif replacement == "fifo":
                os.mkfifo(audio)
            else:
                audio.write_bytes(AUDIO)
        return original_open(name, flags, *args, **kwargs)

    def unexpected_hash(fd, size):
        nonlocal audio_read
        audio_read = True
        return None

    monkeypatch.setattr(os, "open", replace_then_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {replace_then_open})
    monkeypatch.setattr(channels, "_sha256_fd", unexpected_hash)
    with pytest.raises(ChannelInputError) as exc:
        OctopusInboxChannel(tmp_path).load(LOCATOR)
    _check_error(exc, "unsafe_file" if replacement == "symlink" else "file_changed")
    assert original.exists()
    assert not audio_read


def test_declared_digest_is_checked_and_file_reference_survives_byte_identical_reacquisition(
    tmp_path,
):
    record = _manifest()
    record["files"][0]["sha256"] = SHA
    manifest, audio = _write(tmp_path, record)
    channel = OctopusInboxChannel(tmp_path)
    capture = channel.load(LOCATOR)
    replacement = tmp_path / "republished-bytes"
    replacement.write_bytes(AUDIO)
    when = manifest.stat().st_mtime_ns + 10_000_000
    os.utime(replacement, ns=(when, when))
    replacement.replace(audio)
    assert channel.load(LOCATOR) == capture
    with verified_audio(tmp_path, capture) as stream:
        assert stream.read() == AUDIO
