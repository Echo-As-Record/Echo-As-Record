"""Bounded local audio normalization for the native recognition adapter.

Supports 16 kHz mono PCM16 RIFF/WAV (the RecognitionProcess subset), and ordinary
non-fragmented, single-track M4A/audio-only MP4: native AAC-LC or ALAC at 16–48 kHz,
mono/stereo. MP4 requires trusted absolute FFmpeg and ffprobe executables with the
``fd`` protocol (tested with 7.1). No source paths, temporary disk files or network.

Borrow the verified, pinned stream at byte zero; the caller owns post-read
verification. Children get only a sealed anonymous copy on stdin (``fd:``), raw
PCM or bounded probe output on stdout, no inherited environment or extra FDs,
and no stderr. External MOV references are disabled. Python writes RIFF sizes.
The opt-in long path instead spools to unnamed private scratch, handing children
only a read-only copy of the MOV through stdin and streaming their PCM to disk.
This is not an OS sandbox. Deploy a maintained build with appropriate isolation.
Deadlines bound child I/O, not process creation, regular-file reads or kernel
stalls. Cleanup targets the initial process group; requires normal SIGCHLD and
no competing child reaper. Bind normalization/build changes to a new +profile.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import time
import weakref
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import BinaryIO

from ear_adapter.convert import FileRef
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import AmbientEvent, ASRResult, RecognitionProvider, SpeakerResult
from ear_adapter.recognition_process import _read_wav, _wav_extent

_RATE = 16_000
_INPUT_CAP = 8 << 20
_PROBE_CAP = 512 << 10
_PACKET_CAP = 4096
# Phone captures nominally close at 30 minutes; allow 60 seconds for delayed close.
LONG_MAX_SECONDS = 31 * 60
_LONG_INPUT_CAP = 128 << 20
_LONG_PROBE_CAP = 16 << 20
_LONG_PACKET_CAP = 90_000
_COPY_BLOCK = 65_536
_AAC_FRAME = 1024
_CLOCK_SLOP = timedelta(milliseconds=1)  # Same timestamp tolerance as RecognitionProcess.
_WAV_TYPES = ("audio/wav", "audio/x-wav")
# Linux UAPI <linux/fcntl.h>; some Python builds omit these constants.
_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1024 + 9)
_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008  # seal, shrink, grow, write


class RecognitionDecodeError(RuntimeError):
    """Fixed diagnostics only: no child output, executable path or capture fields."""


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError


def _input(audio: BinaryIO, size: int) -> bytes:
    _require(0 < size <= _INPUT_CAP and audio.tell() == 0)
    raw = audio.read(size + 1)
    _require(type(raw) is bytes and len(raw) == size)
    return raw


def _mdat(source: BinaryIO, file_size: int) -> tuple[int, int]:
    # Every byte of the single mdat must be accounted for by visible packets,
    # even when a MOV edit list would otherwise hide packets.
    left = 0
    seen: set[bytes] = set()
    extent = (0, 0)
    for _ in range(32):
        if left == file_size:
            break
        _require(file_size - left >= 8)
        source.seek(left)
        box = source.read(8)
        _require(len(box) == 8)
        size, tag = struct.unpack(">I4s", box)
        _require(left != 0 or tag == b"ftyp")
        _require(tag in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide"))
        head = 8
        if size == 1:
            _require(file_size - left >= 16)
            extended = source.read(8)
            _require(len(extended) == 8)
            size = struct.unpack(">Q", extended)[0]
            head = 16
        elif size == 0 and tag == b"mdat":
            size = file_size - left
        _require(head <= size <= file_size - left)
        if tag in (b"ftyp", b"moov", b"mdat"):
            _require(tag not in seen and size >= head + (8 if tag == b"ftyp" else 1))
            seen.add(tag)
        if tag == b"mdat":
            extent = (left + head, left + size)
        left += size
    _require(left == file_size and len(seen) == 3)
    return extent


def _number(text: object) -> int:
    if (
        not isinstance(text, str)
        or not 0 < len(text) <= 9
        or not text.isascii()
        or not text.isdecimal()
    ):
        raise ValueError
    return int(text)


def _ceil_frames(samples: int, rate: int) -> int:
    return (samples * _RATE + rate - 1) // rate


@dataclass(frozen=True, slots=True)
class _Track:
    codec: str
    frames: int  # presentation span rounded up to the output sample grid
    full_frames: int  # same, if the decoder emits the whole final AAC access unit


def _track(
    reply: bytearray,
    extent: tuple[int, int],
    span: timedelta,
    *,
    max_seconds: int = 30,
    packet_cap: int = _PACKET_CAP,
) -> _Track:
    info = json.loads(reply)
    streams, packets = info["streams"], info["packets"]
    _require(len(streams) == 1 and isinstance(packets, list) and 0 < len(packets) <= packet_cap)
    s = streams[0]
    codec = s["codec_name"]
    _require(
        s["codec_type"] == "audio"
        and (codec == "alac" or (codec == "aac" and s.get("profile") == "LC"))
        and type(s["channels"]) is int
        and s["channels"] in (1, 2)
        and type(s["start_pts"]) is int
        and s["start_pts"] == 0
    )
    rate = _number(s["sample_rate"])
    _require(16_000 <= rate <= 48_000 and s["time_base"] == f"1/{rate}")
    declared = s["duration_ts"]
    _require(type(declared) is int and rate <= declared <= max_seconds * rate)
    frames = _ceil_frames(declared, rate)
    _require(abs(timedelta(microseconds=round(frames * 1_000_000 / _RATE)) - span) <= _CLOCK_SLOP)

    cursor, limit = extent
    clock = tail = 0
    for i, packet in enumerate(packets):
        pos, size = _number(packet["pos"]), _number(packet["size"])
        _require(pos == cursor and 0 < size <= limit - cursor)
        cursor += size
        pts, n = packet["pts"], packet["duration"]
        _require(type(pts) is int and type(n) is int and n > 0)
        side_data = packet.get("side_data_list")
        if i == 0 and codec == "aac" and pts == -_AAC_FRAME:
            # Permit only one explicitly signalled priming frame, skipped by FFmpeg.
            _require(
                n == _AAC_FRAME
                and side_data
                == [
                    {
                        "side_data_type": "Skip Samples",
                        "skip_samples": _AAC_FRAME,
                        "discard_padding": 0,
                    }
                ]
            )
            continue
        _require(pts == clock and not side_data)
        if codec == "aac":
            _require(n == _AAC_FRAME or (i == len(packets) - 1 and n < _AAC_FRAME))
            tail = _AAC_FRAME - n
        clock += n
        _require(clock <= declared)
    _require(cursor == limit and clock == declared)
    # Only a short final AAC-LC packet can justify trim. Its duration, not the
    # manifest clock, identifies at most 1023 padding samples at source rate.
    # Never synthesize samples or trim an unexplained discrepancy.
    return _Track(codec, frames, _ceil_frames(declared + tail, rate))


def _collect(
    child: subprocess.Popen[bytes], cap: int, deadline: float, *, sink: BinaryIO | None = None
) -> tuple[bytearray, int]:
    # Unlike the model transport (_exchange), these children need seekable
    # stdin and no parent-side pipe writer. Only stdout is pumped.
    assert child.stdout is not None
    fd = child.stdout.fileno()
    os.set_blocking(fd, False)
    output = bytearray()
    total = 0
    eof = False
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecognitionDecodeError("audio decoder timed out")
            # Peek without reaping: preserve the PGID for cleanup and detect
            # a dead leader even when a descendant still holds the pipe.
            exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                if exited.si_code != os.CLD_EXITED or exited.si_status:
                    raise RecognitionDecodeError("audio decoder failed")
                if eof:
                    return output, total
            ready = sel.select(0 if exited is not None else min(remaining, 0.05))
            if exited is not None and not ready:
                raise RecognitionDecodeError("audio decoder left stdout open")
            if ready:
                try:
                    part = os.read(fd, min(_COPY_BLOCK, cap + 1 - total))
                except BlockingIOError:
                    continue
                if not part:
                    sel.unregister(fd)
                    eof = True
                elif len(part) > cap - total:
                    raise RecognitionDecodeError("audio decoder output limit")
                else:
                    if sink is None:
                        output.extend(part)
                    else:
                        _require(sink.write(part) == len(part))
                    total += len(part)


def _run_to(
    command: tuple[str, ...], source: BinaryIO, cap: int, deadline: float, *, sink: BinaryIO | None
) -> tuple[bytearray, int]:
    if deadline <= time.monotonic():
        raise RecognitionDecodeError("audio decoder timed out")
    source.seek(0)
    child = subprocess.Popen(
        command,
        stdin=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
        cwd="/",
        shell=False,
        close_fds=True,
        start_new_session=True,
        bufsize=0,
    )
    try:
        return _collect(child, cap, deadline, sink=sink)
    finally:
        try:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        finally:
            assert child.stdout is not None
            child.stdout.close()
            child.wait(timeout=1)


def _run(command: tuple[str, ...], source: BinaryIO, cap: int, deadline: float) -> bytearray:
    return _run_to(command, source, cap, deadline, sink=None)[0]


def _copy(source: BinaryIO, dest: BinaryIO, size: int, deadline: float) -> None:
    while size:
        if time.monotonic() >= deadline:
            raise RecognitionDecodeError("audio decoder timed out")
        block = source.read(min(size, _COPY_BLOCK))
        _require(0 < len(block) <= size and dest.write(block) == len(block))
        size -= len(block)


def _wav(pcm: bytearray) -> bytes:
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + len(pcm),
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            _RATE,
            _RATE * 2,
            2,
            16,
            b"data",
            len(pcm),
        )
        + pcm
    )


_GLOBAL = ("-hide_banner", "-loglevel", "error", "-max_alloc", "16777216")
_MOV = (
    "-protocol_whitelist",
    "fd",
    "-format_whitelist",
    "mov",
    "-max_streams",
    "1",
    "-err_detect",
    "explode",
    "-f",
    "mov",
    "-enable_drefs",
    "0",
    "-use_absolute_path",
    "0",
)
_PROBE_FIELDS = (
    "stream=codec_name,profile,codec_type,channels,sample_rate,time_base,start_pts,duration_ts"
    ":packet=pts,duration,pos,size:packet_side_data=side_data_type,skip_samples,discard_padding"
)


class FFmpegDecoder:
    """Decode one verified capture to canonical WAV bytes; no publish or intake.

    ``timeout`` is the shared ffprobe + FFmpeg wall-clock budget, plus cleanup.
    The private input cap is independent of upstream caps. Both the capture's
    span and the stream's presentation duration must be within 1–30 seconds
    and agree to within 1 ms (capture-clock rounding). Timestamps are not
    rewritten. WAV uses the checked no-resampling path and needs no subprocess.
    """

    def __init__(self, ffmpeg_path: str, ffprobe_path: str, *, timeout: float = 10.0) -> None:
        if (
            sys.platform != "linux"
            or not all(
                type(p) is str and os.path.isabs(p) and "\0" not in p
                for p in (
                    ffmpeg_path,
                    ffprobe_path,
                )
            )
            or type(timeout) not in (int, float)
            or not 0 < timeout <= 60
        ):
            raise ValueError("invalid audio decoder configuration")
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path
        self._timeout = timeout

    def decode(self, audio: BinaryIO, capture: CaptureInput) -> bytes:
        """Read at byte zero without closing the borrowed stream or changing its descriptor."""
        try:
            span = capture.ended_at - capture.started_at
            _require(timedelta(seconds=1) <= span <= timedelta(seconds=30))
            if capture.media_type in _WAV_TYPES:
                wav, _, _ = _read_wav(audio, capture)
                return wav
            _require(capture.media_type == "audio/mp4")
            return self._m4a(_input(audio, capture.file_ref.size), span)
        except RecognitionDecodeError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            RecursionError,
            AttributeError,
            subprocess.SubprocessError,
        ):
            pass
        raise RecognitionDecodeError("audio normalization rejected")

    def _probe_command(self) -> tuple[str, ...]:
        return (
            self._ffprobe,
            *_GLOBAL,
            "-threads",
            "1",
            *_MOV,
            "-codec_whitelist",
            "aac,alac",
            "-show_entries",
            _PROBE_FIELDS,
            "-of",
            "json=compact=1",
            "fd:",
        )

    def _pcm_command(self, codec: str) -> tuple[str, ...]:
        return (
            self._ffmpeg,
            *_GLOBAL,
            "-nostdin",
            "-xerror",
            "-filter_threads",
            "1",
            "-threads",
            "1",
            *_MOV,
            "-codec_whitelist",
            codec,
            "-c:a",
            codec,
            "-i",
            "fd:",
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-ac",
            "1",
            "-ar",
            str(_RATE),
            "-c:a",
            "pcm_s16le",
            "-threads:a",
            "1",
            "-f",
            "s16le",
            "pipe:1",
        )

    def _m4a(self, raw: bytes, span: timedelta) -> bytes:
        with os.fdopen(
            os.memfd_create("", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING), "w+b", buffering=0
        ) as source:
            _require(source.write(raw) == len(raw))
            fcntl.fcntl(source, _ADD_SEALS, _SEALS)
            extent = _mdat(source, len(raw))
            deadline = time.monotonic() + self._timeout
            reply = _run(self._probe_command(), source, _PROBE_CAP, deadline)
            track = _track(reply, extent, span)
            pcm = _run(self._pcm_command(track.codec), source, 2 * track.full_frames, deadline)
        _require(len(pcm) in (2 * track.frames, 2 * track.full_frames))
        del pcm[2 * track.frames :]
        return _wav(pcm)

    def _long_pcm(
        self,
        audio: BinaryIO,
        capture: CaptureInput,
        pcm: BinaryIO,
        *,
        deadline: float,
        scratch_dir: str | None,
    ) -> tuple[int, str]:
        """Write to an empty private scratch file. Return frame count and source codec."""
        try:
            deadline = min(deadline, time.monotonic() + self._timeout)
            size = capture.file_ref.size
            span = capture.ended_at - capture.started_at
            _require(timedelta(seconds=1) <= span <= timedelta(seconds=LONG_MAX_SECONDS))
            _require(0 < size <= _LONG_INPUT_CAP and audio.tell() == 0)
            if capture.media_type in _WAV_TYPES:
                _require(44 <= size <= (2 * _RATE * LONG_MAX_SECONDS) + 65_536)
                audio.seek(0, os.SEEK_END)
                _require(audio.tell() == size)
                audio.seek(0)
                offset, n = _wav_extent(audio, size, 2 * _RATE * LONG_MAX_SECONDS, max_chunks=8192)
                _require(size - n <= 65_536)
                frames = n // 2
                _require(
                    abs(timedelta(microseconds=round(frames * 1_000_000 / _RATE)) - span)
                    <= _CLOCK_SLOP
                )
                audio.seek(offset)
                _copy(audio, pcm, n, deadline)
                return frames, "pcm_s16le"
            _require(capture.media_type == "audio/mp4")
            # Copy bytes, not the original descriptor/path. Only the parent holds
            # the writable scratch fd; children receive a fresh read-only description.
            with tempfile.TemporaryFile(dir=scratch_dir, buffering=0) as spool:
                _copy(audio, spool, size, deadline)
                _require(not audio.read(1))
                with open(f"/proc/self/fd/{spool.fileno()}", "rb", buffering=0) as source:
                    extent = _mdat(source, size)
                    reply = _run(self._probe_command(), source, _LONG_PROBE_CAP, deadline)
                    track = _track(
                        reply,
                        extent,
                        span,
                        max_seconds=LONG_MAX_SECONDS,
                        packet_cap=_LONG_PACKET_CAP,
                    )
                    del reply
                    _, n = _run_to(
                        self._pcm_command(track.codec),
                        source,
                        2 * track.full_frames,
                        deadline,
                        sink=pcm,
                    )
            _require(n in (2 * track.frames, 2 * track.full_frames))
            pcm.truncate(2 * track.frames)
            return track.frames, track.codec
        except RecognitionDecodeError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError,
            RecursionError,
            AttributeError,
            subprocess.SubprocessError,
        ):
            pass
        raise RecognitionDecodeError("audio normalization rejected")


@dataclass(slots=True)
class _Staged:
    source: weakref.ReferenceType[BinaryIO]
    capture: CaptureInput
    normalized: io.BytesIO
    descriptor: CaptureInput
    needs_speakers: bool


class NormalizingRecognitionProvider(RecognitionProvider):
    """Wrap a trusted synchronous provider; use serially with ``ingest_native``.

    Decode once per ASR call and rewind the same private stream for later stages.
    Only the provider sees the synthetic WAV reference: it identifies in-memory
    bytes and must never be opened or persisted. Storage sees the original input.
    Normalized bytes are released on ambient, an aborted stage, or collection of
    the input stream. There is no cross-capture cache or intake history.
    """

    def __init__(self, provider: RecognitionProvider, decoder: FFmpegDecoder) -> None:
        self._provider = provider
        self._decoder = decoder
        self._pending: _Staged | None = None

    def _clear(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.normalized.close()

    def _released(self, source: weakref.ReferenceType[BinaryIO]) -> None:
        if self._pending is not None and self._pending.source is source:
            self._clear()

    def asr(self, audio: BinaryIO, capture: CaptureInput) -> ASRResult:
        self._clear()
        source = weakref.ref(audio, self._released)
        wav = self._decoder.decode(audio, capture)
        descriptor = replace(
            capture,
            media_type="audio/wav",
            file_ref=FileRef(
                "normalized.wav",
                hashlib.sha256(wav).hexdigest(),
                len(wav),
            ),
        )
        normalized = io.BytesIO(wav)
        try:
            result = self._provider.asr(normalized, descriptor)
            if result.status == "complete":
                self._pending = _Staged(
                    source, capture, normalized, descriptor, bool(result.segments)
                )
            return result
        finally:
            if self._pending is None:
                normalized.close()

    def _take(self, audio: BinaryIO, capture: CaptureInput, *, speakers: bool) -> _Staged:
        staged, self._pending = self._pending, None
        if staged is not None:
            try:
                if (
                    staged.source() is audio
                    and staged.capture is capture
                    and not audio.closed
                    and audio.tell() == 0
                    and staged.needs_speakers == speakers
                ):
                    staged.normalized.seek(0)
                    return staged
            except (OSError, ValueError):
                pass
            staged.normalized.close()
        raise RecognitionDecodeError("audio normalization stage mismatch")

    def speakers(
        self, audio: BinaryIO, capture: CaptureInput, asr: ASRResult
    ) -> tuple[SpeakerResult, ...]:
        staged = self._take(audio, capture, speakers=True)
        try:
            results = self._provider.speakers(staged.normalized, staged.descriptor, asr)
            staged.needs_speakers = False
            self._pending = staged
            return results
        finally:
            if self._pending is None:
                staged.normalized.close()

    def ambient(self, audio: BinaryIO, capture: CaptureInput) -> tuple[AmbientEvent, ...]:
        staged = self._take(audio, capture, speakers=False)
        with staged.normalized:
            return self._provider.ambient(staged.normalized, staged.descriptor)
