"""仅 tmp_path 合成文件；验证禁止的文件连 open/hash 都不能进入。"""

import os
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
from ear_adapter import InboxAudioLocator
from ear_adapter.ear_api import Capture, CaptureEvidence
from ear_adapter.source import AudioAccessError
from ear_adapter.testing import AUDIO_BYTES, AUDIO_FILE, evidence_json


@pytest.fixture
def capture():
    return CaptureEvidence.model_validate(evidence_json()).of_type("capture", Capture)[0]


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / AUDIO_FILE.path
    path.parent.mkdir(parents=True)
    path.write_bytes(AUDIO_BYTES)
    return path


def spy_open(monkeypatch, before=None):
    original = os.open
    opened = []

    def track(path, flags, *args, **kwargs):
        opened.append(str(path))
        if before is not None:
            before(path, flags)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", track)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {track})
    return opened


def test_only_allowlisted_audio_opened_even_when_other_hashes_match(
    tmp_path, audio, capture, monkeypatch
):
    for name in ("000_asr.txt", "000.json", "000.db", "000.mp3", "000.m4a.txt", "000"):
        (audio.parent / name).write_bytes(AUDIO_BYTES)
    opened = spy_open(monkeypatch)
    assert InboxAudioLocator(tmp_path).locate(capture) == AUDIO_FILE
    assert audio.name in opened
    assert not any(name.startswith("000") for name in opened)


@pytest.mark.parametrize("field", ["instance_id", "group_id"])
@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "/tmp", "../outside", "x/y", "x\\y", "*", "?", "[x]", "x\nprivate", "x\x00y"],
)
def test_unsafe_components_rejected_before_any_io(tmp_path, capture, monkeypatch, field, value):
    opened = spy_open(monkeypatch)
    capture = capture.model_copy(update={field: value})
    with pytest.raises(AudioAccessError):
        InboxAudioLocator(tmp_path).locate(capture)
    assert opened == []


@pytest.mark.parametrize(
    "level", ["root", "inbox", "brain", "instance", "date", "collection", "group", "audio"]
)
def test_symlink_at_every_level_not_opened(tmp_path, audio, capture, monkeypatch, level):
    paths = [tmp_path, *[tmp_path.joinpath(*Path(AUDIO_FILE.path).parts[:i]) for i in range(1, 8)]]
    index = ["root", "inbox", "brain", "instance", "date", "collection", "group", "audio"].index(
        level
    )
    victim = paths[index]
    target = tmp_path.parent / (tmp_path.name + "-outside")
    victim.rename(target)
    victim.symlink_to(target, target_is_directory=target.is_dir())
    opened = spy_open(monkeypatch)
    assert InboxAudioLocator(tmp_path).locate(capture) is None
    assert victim.name not in opened
    assert target.name not in opened


def test_root_ancestor_symlink_and_parent_traversal_rejected(tmp_path, audio, capture, monkeypatch):
    alias = tmp_path.parent / (tmp_path.name + "-alias")
    alias.symlink_to(tmp_path, target_is_directory=True)
    opened = spy_open(monkeypatch)
    assert InboxAudioLocator(alias / "inbox").locate(capture) is None
    assert alias.name not in opened
    opened.clear()
    with pytest.raises(AudioAccessError):
        InboxAudioLocator(tmp_path / ".." / tmp_path.name).locate(capture)
    assert opened == []


@pytest.mark.parametrize("kind", ["directory", "fifo", "socket", "hardlink"])
def test_nonregular_and_hardlinked_audio_never_opened(tmp_path, audio, capture, monkeypatch, kind):
    audio.unlink()
    sock = None
    if kind == "directory":
        audio.mkdir()
    elif kind == "fifo":
        os.mkfifo(audio)
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX)
        short = tmp_path / "s"
        sock.bind(str(short))
        short.rename(audio)
    else:
        private = tmp_path / "private-transcript.txt"
        private.write_bytes(AUDIO_BYTES)
        os.link(private, audio)
    try:
        opened = spy_open(monkeypatch)
        assert InboxAudioLocator(tmp_path).locate(capture) is None
        assert audio.name not in opened
    finally:
        if sock:
            sock.close()


@pytest.mark.parametrize("mode", ["size", "hash"])
def test_mismatch_never_produces_reference(tmp_path, audio, capture, monkeypatch, mode):
    audio.write_bytes(b"x" * (len(AUDIO_BYTES) if mode == "hash" else 1))
    opened = spy_open(monkeypatch)
    assert InboxAudioLocator(tmp_path).locate(capture) is None
    assert (audio.name in opened) == (mode == "hash")


@pytest.mark.parametrize(
    "update",
    [
        {"media_type": "text/plain"},
        {"media_type": "audio/unknown"},
        {"checksum": "sha256:private"},
        {"size_bytes": -1},
    ],
)
def test_unsupported_metadata_fails_before_io(tmp_path, capture, monkeypatch, update):
    capture = capture.model_copy(update={"audio_ref": capture.audio_ref.model_copy(update=update)})
    opened = spy_open(monkeypatch)
    with pytest.raises(AudioAccessError):
        InboxAudioLocator(tmp_path).locate(capture)
    assert opened == []


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "regular"])
def test_race_after_stat_does_not_read_replacement(
    tmp_path, audio, capture, monkeypatch, replacement
):
    outside = tmp_path / "private.txt"
    outside.write_bytes(AUDIO_BYTES)

    def replace(name, flags):
        if name == audio.name:
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            audio.rename(audio.with_suffix(".old"))
            if replacement == "symlink":
                audio.symlink_to(outside)
            elif replacement == "fifo":
                os.mkfifo(audio)
            else:
                audio.write_bytes(AUDIO_BYTES)

    spy_open(monkeypatch, replace)
    read = Mock(side_effect=AssertionError("replacement must not be read"))
    monkeypatch.setattr(os, "read", read)
    if replacement == "symlink":
        with pytest.raises(AudioAccessError):
            InboxAudioLocator(tmp_path).locate(capture)
    else:
        assert InboxAudioLocator(tmp_path).locate(capture) is None
    read.assert_not_called()


def test_directory_race_cannot_escape(tmp_path, audio, capture, monkeypatch):
    group = audio.parent
    target = tmp_path / "outside"

    def replace(name, flags):
        if name == group.name:
            assert flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY
            group.rename(target)
            group.symlink_to(target, target_is_directory=True)

    spy_open(monkeypatch, replace)
    read = Mock(side_effect=AssertionError("outside audio must not be read"))
    monkeypatch.setattr(os, "read", read)
    assert InboxAudioLocator(tmp_path).locate(capture) is None
    read.assert_not_called()


@pytest.mark.parametrize("change", ["grow", "replace"])
def test_change_during_hash_rejected(tmp_path, audio, capture, monkeypatch, change):
    from ear_adapter import source

    original = source._sha256_fd

    def mutate(fd, size):
        digest = original(fd, size)
        if change == "grow":
            with audio.open("ab") as stream:
                stream.write(b"more")
        else:
            audio.rename(audio.with_suffix(".old"))
            audio.write_bytes(AUDIO_BYTES)
        return digest

    monkeypatch.setattr(source, "_sha256_fd", mutate)
    assert InboxAudioLocator(tmp_path).locate(capture) is None


def test_no_secure_fd_support_fails_closed(tmp_path, capture, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(AudioAccessError):
        InboxAudioLocator(tmp_path).locate(capture)


def track_fds(monkeypatch):
    """核对本次 locator 打开的每个 fd，避免全进程计数受并行线程影响。"""
    original_open, original_close = os.open, os.close
    live = set()

    def open_fd(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        live.add(fd)
        return fd

    def close_fd(fd):
        live.discard(fd)
        return original_close(fd)

    monkeypatch.setattr(os, "open", open_fd)
    monkeypatch.setattr(os, "close", close_fd)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_fd})
    return live


def test_directory_fstat_failure_closes_every_opened_fd(tmp_path, audio, capture, monkeypatch):
    live = track_fds(monkeypatch)
    original = os.fstat
    target = audio.parent.stat().st_ino

    def fail(fd):
        info = original(fd)
        if info.st_ino == target:
            raise OSError("sentinel-private-path")
        return info

    monkeypatch.setattr(os, "fstat", fail)
    try:
        with pytest.raises(AudioAccessError) as exc:
            InboxAudioLocator(tmp_path).locate(capture)
        assert "sentinel" not in str(exc.value)
        assert not live
    finally:
        # 即使回归红了也不让测试本身泄漏 fd。
        for fd in tuple(live):
            os.close(fd)


@pytest.mark.parametrize("level", ["root", "group"])
@pytest.mark.parametrize("at", ["list", "file_open"])
def test_open_directory_moved_outside_root_is_not_read(
    tmp_path, audio, capture, monkeypatch, level, at
):
    from ear_adapter import source

    victim = tmp_path if level == "root" else audio.parent
    target = tmp_path.parent / (tmp_path.name + "-detached")
    victim_inode = victim.stat().st_ino
    live = track_fds(monkeypatch)
    moved = False

    def move():
        nonlocal moved
        if not moved:
            moved = True
            victim.rename(target)
            # 同名替代目录不能给旧 fd 授权。
            victim.mkdir()

    if at == "list":
        original_listdir = os.listdir

        def listdir(fd):
            # Root is traversed by openat, not listed. Move it at the first
            # descendant enumeration; otherwise this case never injects a race.
            if level == "root" or os.fstat(fd).st_ino == victim_inode:
                move()
            return original_listdir(fd)

        monkeypatch.setattr(os, "listdir", listdir)
        monkeypatch.setattr(os, "supports_fd", os.supports_fd | {listdir})
    else:
        original_open = os.open

        def open_fd(name, *args, **kwargs):
            fd = original_open(name, *args, **kwargs)
            if name == audio.name:
                move()
            return fd

        monkeypatch.setattr(os, "open", open_fd)
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {open_fd})
    read = Mock(wraps=os.read)
    monkeypatch.setattr(source.os, "read", read)
    with pytest.raises(AudioAccessError):
        InboxAudioLocator(tmp_path).locate(capture)
    assert moved
    read.assert_not_called()
    assert not live


@pytest.mark.parametrize("outcome", ["success", "missing", "read_error", "list_error"])
def test_locator_closes_resources_on_all_paths(tmp_path, audio, capture, monkeypatch, outcome):
    live = track_fds(monkeypatch)
    if outcome == "missing":
        audio.unlink()
    elif outcome == "read_error":
        monkeypatch.setattr(os, "read", Mock(side_effect=OSError("sentinel-private-path")))
    elif outcome == "list_error":
        fail = Mock(side_effect=OSError("sentinel-private-path"))
        monkeypatch.setattr(os, "listdir", fail)
        monkeypatch.setattr(os, "supports_fd", os.supports_fd | {fail})
    if outcome.endswith("error"):
        with pytest.raises(AudioAccessError):
            InboxAudioLocator(tmp_path).locate(capture)
    else:
        assert InboxAudioLocator(tmp_path).locate(capture) == (
            AUDIO_FILE if outcome == "success" else None
        )
    assert not live
