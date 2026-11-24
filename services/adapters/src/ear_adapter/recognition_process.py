"""Bounded Linux transport for tools/ear_legacy_recognition_worker.py.

The trusted operator supplies the full absolute launcher argv, including the
worker/interpreter (-I -B), --offline-isolated, model profile, device and asset
paths. Only --started-at/--ended-at are added here; timestamps in argv may be
visible to local process inspection. Keep argv free of credentials. Use a
matching native adapter +profile when changing the model bundle or settings.

This is not a sandbox. Before exec, the launcher must enforce OS-level no egress
(including local service sockets), a filesystem view without storage, credentials
or production data, private disposable TMPDIR, resource/cgroup and hard wall
limits, and teardown of the entire job (including on parent death). Deploy only
validated read-only source, venv and model data; model loading can execute code.
The parent passes no environment or open storage/input descriptors. It cleans up
the initial process group, not descendants that leave it; it must be the sole
child reaper, with normal SIGCHLD behavior. The I/O deadline does not interrupt
OS process creation or kernel stalls; the launcher is the hard boundary.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import struct
import subprocess
import sys
import time
import weakref
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, BinaryIO, Literal, NoReturn

from ear_contracts import format_utc

from ear_adapter.ear_api import AudioChunk, Embedding, SpeechSegment
from ear_adapter.native_channels import CaptureInput
from ear_adapter.native_pipeline import (
    AmbientEvent,
    ASRResult,
    RecognitionProvider,
    SpeakerResult,
    _ambient,
    _asr,
    _speakers,
)

# Wire bounds from the one-shot bridge; do not import the worker or its providers.
_RATE = 16_000
_MAX_PCM = 2 * _RATE * 30
_MAX_WAV = _MAX_PCM + 65_536
_MAX_JSON = 4 * 1024 * 1024
_MAX_ROWS = 1024
_DIM = 192
_BLOCK = 65_536
_PCM_FORMAT = (1, 1, _RATE, _RATE * 2, 2, 16)
_OPTIONAL_WAV_TAGS = {b"LIST", b"JUNK", b"bext", b"iXML", b"fact", b"PAD "}
_UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
_JSON_TOKENS = re.compile(r'["\\\[\]{},]')


class RecognitionProcessError(RuntimeError):
    """Diagnostics are fixed; never include argv, file paths, or child output."""


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError


def _read_wav(audio: BinaryIO, capture: CaptureInput) -> tuple[bytes, datetime, bool]:
    size = capture.file_ref.size
    _require(capture.media_type in ("audio/wav", "audio/x-wav") and 44 <= size <= _MAX_WAV)
    _require(audio.tell() == 0)
    raw = audio.read(size + 1)
    _require(len(raw) == size and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE")
    _require(struct.unpack_from("<I", raw, 4)[0] == size - 8)
    pos, formatted, pcm = 12, False, b""
    while pos < size:
        _require(pos + 8 <= size)
        tag, length = struct.unpack_from("<4sI", raw, pos)
        left = pos + 8
        pos = left + length + length % 2
        _require(pos <= size)
        if tag == b"fmt ":
            _require(not formatted and length == 16)
            _require(struct.unpack_from("<HHIIHH", raw, left) == _PCM_FORMAT)
            formatted = True
        elif tag == b"data":
            _require(formatted and not pcm and 2 * _RATE <= length <= _MAX_PCM and length % 2 == 0)
            pcm = raw[left : left + length]
        else:
            _require(tag in _OPTIONAL_WAV_TAGS)
    _require(bool(pcm))
    end = capture.started_at + timedelta(microseconds=round((len(pcm) // 2) * 1_000_000 / _RATE))
    _require(abs(capture.ended_at - end) <= timedelta(milliseconds=1))
    # Do not send even allowed WAV metadata (which might contain identifiers).
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        len(pcm) + 36,
        b"WAVE",
        b"fmt ",
        16,
        *_PCM_FORMAT,
        b"data",
        len(pcm),
    )
    return header + pcm, min(end, capture.ended_at), pcm.count(0) == len(pcm)


def _cleanup(child: subprocess.Popen[bytes]) -> None:
    assert child.stdin is not None and child.stdout is not None
    try:
        # Signal before reap so the original PID/PGID cannot be recycled here.
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    finally:
        child.stdin.close()
        child.stdout.close()
        child.wait(timeout=1)


def _exchange(child: subprocess.Popen[bytes], wav: bytes, deadline: float) -> bytearray:
    assert child.stdin is not None and child.stdout is not None
    incoming, outgoing = child.stdout.fileno(), child.stdin.fileno()
    os.set_blocking(incoming, False)
    os.set_blocking(outgoing, False)
    data = memoryview(wav)
    output = bytearray()
    offset = 0
    eof = False
    with selectors.DefaultSelector() as selector:
        selector.register(incoming, selectors.EVENT_READ)
        selector.register(outgoing, selectors.EVENT_WRITE)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecognitionProcessError("recognition worker timed out")
            # Peek without reaping. A dead leader with a descendant holding its
            # pipes is not allowed to hold up the caller until the deadline.
            exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                if exited.si_code != os.CLD_EXITED or exited.si_status or offset != len(data):
                    raise RecognitionProcessError("recognition worker failed")
                if eof:
                    return output
            ready = selector.select(0 if exited is not None else min(remaining, 0.05))
            if exited is not None and not ready:
                raise RecognitionProcessError("recognition worker left stdout open")
            for key, _ in ready:
                try:
                    if key.fd == incoming:
                        part = os.read(incoming, min(_BLOCK, _MAX_JSON + 1 - len(output)))
                        if not part:
                            selector.unregister(incoming)
                            eof = True
                        elif len(part) > _MAX_JSON - len(output):
                            raise RecognitionProcessError("recognition worker output limit")
                        else:
                            output.extend(part)
                    else:
                        offset += os.write(outgoing, data[offset : offset + _BLOCK])
                        if offset == len(data):
                            selector.unregister(outgoing)
                            child.stdin.close()
                except BlockingIOError:
                    continue


def _run(command: tuple[str, ...], wav: bytes, timeout: float) -> bytearray:
    deadline = time.monotonic() + timeout
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
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
        return _exchange(child, wav, deadline)
    finally:
        _cleanup(child)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError


def _int(token: str) -> int:
    _require(len(token) <= 20)
    return int(token)


def _float(token: str) -> float:
    _require(len(token) <= 64)
    value = float(token)
    _require(math.isfinite(value))
    return value


def _json(raw: bytearray) -> Any:
    text = raw.decode("utf-8")
    in_string, escape = False, -1
    counts: list[int] = []
    # Bound depth and container size before json.loads allocates Python objects.
    # Ignore punctuation in strings; json.loads checks the actual JSON grammar.
    for token in _JSON_TOKENS.finditer(text):
        char, pos = token[0], token.start()
        if in_string:
            if pos != escape:
                if char == "\\":
                    escape = pos + 1
                elif char == '"':
                    in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            counts.append(1)
            _require(len(counts) <= 6)
        elif char == ",":
            _require(bool(counts) and counts[-1] < _MAX_ROWS)
            counts[-1] += 1
        else:
            _require(bool(counts))
            counts.pop()
    return json.loads(
        text,
        object_pairs_hook=_unique,
        parse_constant=_reject_constant,
        parse_int=_int,
        parse_float=_float,
    )


def _object(value: Any, keys: str) -> dict[str, Any]:
    _require(type(value) is dict and value.keys() == set(keys.split()))
    return value


def _rows(value: Any, limit: int = _MAX_ROWS) -> list[Any]:
    _require(type(value) is list and len(value) <= limit)
    return value


def _text(value: Any, limit: int) -> str:
    _require(
        type(value) is str and 0 < len(value) <= limit and bool(value.strip()) and "\0" not in value
    )
    value.encode("utf-8")
    return value


def _time(value: Any) -> datetime:
    _require(type(value) is str and len(value) <= 27 and _UTC_TIME.fullmatch(value) is not None)
    return datetime.fromisoformat(value)


def _timed_row(value: Any, text_bounds: dict[str, int], extra: str = "") -> dict[str, Any]:
    row = _object(value, f"started_at ended_at model_version {' '.join(text_bounds)} {extra}")
    return {
        **row,
        **{key: _text(row[key], limit) for key, limit in text_bounds.items()},
        "started_at": _time(row["started_at"]),
        "ended_at": _time(row["ended_at"]),
        "model_version": _text(row["model_version"], 160),
    }


def _chunk(value: Any) -> AudioChunk:
    return AudioChunk(**_timed_row(value, {"audio_chunk_id": 128}))


def _segment(value: Any) -> SpeechSegment:
    row = _timed_row(
        value,
        {
            "audio_chunk_id": 128,
            "speech_segment_id": 128,
            "original_text": 16_384,
            "local_speaker_id": 128,
            "diarization_scope_id": 128,
        },
        "language overlap",
    )
    _require(type(row["overlap"]) is bool and row["diarization_scope_id"] == row["audio_chunk_id"])
    if row["language"] is not None:
        row["language"] = _text(row["language"], 64)
    return SpeechSegment(**row)


def _refs(value: Any) -> tuple[str, ...]:
    refs = tuple(_text(item, 128) for item in _rows(value))
    _require(bool(refs) and len(set(refs)) == len(refs))
    return refs


def _vector(value: Any, refs: tuple[str, ...]) -> Embedding:
    row = _object(value, "model_version shape dtype values provenance")
    shape, values = _rows(row["shape"], 1), _rows(row["values"], _DIM)
    _require(len(shape) == 1 and type(shape[0]) is int and shape[0] == _DIM)
    _require(
        row["dtype"] == "float32" and len(values) == _DIM and all(type(v) is float for v in values)
    )
    _require(math.isclose(math.hypot(*values), 1.0, abs_tol=1e-5))
    p = _object(row["provenance"], "preprocessing normalization speech_segment_ids")
    _require(
        p["preprocessing"] == "ear-speaker-concat-pcm16-mono16k-v1" and p["normalization"] == "l2"
    )
    _require(set(_refs(p["speech_segment_ids"])) <= set(refs))
    return Embedding(
        model_version=_text(row["model_version"], 160),
        shape=(_DIM,),
        dtype="float32",
        values=values,
        provenance=p,
    )


def _speaker(value: Any, groups: dict[tuple[str, str], set[str]]) -> SpeakerResult:
    row = _object(
        value,
        "resolution_id diarization_scope_id local_speaker_id "
        "evidence_segment_ids embedding sample_quality",
    )
    scope = _text(row["diarization_scope_id"], 128)
    label = _text(row["local_speaker_id"], 128)
    refs = _refs(row["evidence_segment_ids"])
    _require(set(refs) == groups.pop((scope, label), None))
    quality, embedding = row["sample_quality"], row["embedding"]
    if embedding is None:
        _require(quality is None)
    else:
        _require(type(quality) in (int, float) and 0 <= quality <= 1)
        embedding = _vector(embedding, refs)
    return SpeakerResult(_text(row["resolution_id"], 128), scope, label, refs, embedding, quality)


def _event(value: Any) -> AmbientEvent:
    row = _timed_row(value, {"audio_event_id": 128, "original_label": 256}, "taxonomy")
    _require(row["taxonomy"] == "audioset-527")
    return AmbientEvent(**row)


def _result(
    raw: bytearray,
    capture: CaptureInput,
    end: datetime,
    silent: bool,
) -> tuple[ASRResult, tuple[SpeakerResult, ...], tuple[AmbientEvent, ...]]:
    top = _object(_json(raw), "status asr speakers ambient")
    asr = _object(top["asr"], "status chunks segments")
    _require(top["status"] == asr["status"] == "complete")
    chunks = tuple(_chunk(row) for row in _rows(asr["chunks"], 0 if silent else 1))
    if not silent:
        _require(
            len(chunks) == 1
            and (chunks[0].started_at, chunks[0].ended_at) == (capture.started_at, end)
        )
    segments = tuple(_segment(row) for row in _rows(asr["segments"], 0 if silent else _MAX_ROWS))
    # Reuse native validation (IDs, spans, uniqueness, cross-row relationships).
    bounded_capture = replace(capture, ended_at=end)
    checked, _ = _asr(ASRResult("complete", chunks, segments), bounded_capture)
    groups: dict[tuple[str, str], set[str]] = {}
    for s in segments:
        groups.setdefault((s.diarization_scope_id, s.local_speaker_id), set()).add(
            s.speech_segment_id
        )
    speakers = _speakers(
        tuple(_speaker(row, groups) for row in _rows(top["speakers"], len(groups))), checked
    )
    events = tuple(_event(row) for row in _rows(top["ambient"], 0 if silent else _MAX_ROWS))
    return checked, speakers, _ambient(events, bounded_capture)


@dataclass(slots=True)
class _Staged:
    stream: weakref.ReferenceType[BinaryIO]
    capture: CaptureInput
    asr: ASRResult | None
    speakers: tuple[SpeakerResult, ...]
    ambient: tuple[AmbientEvent, ...]


class RecognitionProcess(RecognitionProvider):
    """One process per ASR call; consume later stages once, on the same capture.

    Use serially with ingest_native. The input is the borrowed, pinned stream;
    the subprocess sees only normalized audio bytes and capture UTC boundaries.
    The timeout is in seconds (default 245, maximum 300), plus bounded reap.
    """

    def __init__(self, command: tuple[str, ...], *, timeout: float = 245.0) -> None:
        if (
            sys.platform != "linux"
            or type(command) is not tuple
            or not command
            or not all(type(arg) is str and arg and "\0" not in arg for arg in command)
            or not os.path.isabs(command[0])
            or any(arg.split("=", 1)[0] in ("--started-at", "--ended-at") for arg in command)
            or type(timeout) not in (int, float)
            or not 0 < timeout <= 300
        ):
            raise ValueError("invalid recognition process configuration")
        self._command = command
        self._timeout = timeout
        self._pending: _Staged | None = None

    def _discard(self, stream: weakref.ReferenceType[BinaryIO]) -> None:
        if self._pending is not None and self._pending.stream is stream:
            self._pending = None

    def asr(self, audio: BinaryIO, capture: CaptureInput) -> ASRResult:
        self._pending = None
        failed: Literal["input", "process", "result"] = "input"
        try:
            stream = weakref.ref(audio, self._discard)
            wav, end, silent = _read_wav(audio, capture)
            command = (
                *self._command,
                "--started-at",
                format_utc(capture.started_at),
                "--ended-at",
                format_utc(capture.ended_at),
            )
            failed = "process"
            raw = _run(command, wav, self._timeout)
            failed = "result"
            asr, speakers, ambient = _result(raw, capture, end, silent)
        except RecognitionProcessError:
            raise
        except (OSError, ValueError, TypeError, OverflowError, subprocess.SubprocessError):
            # Raise outside the handler so the original exception/body is not chained.
            pass
        else:
            self._pending = _Staged(
                stream, capture, asr if asr.segments else None, speakers, ambient
            )
            return asr
        raise RecognitionProcessError(f"recognition worker {failed} rejected")

    def _stage(self, audio: BinaryIO, capture: CaptureInput) -> _Staged:
        staged, self._pending = self._pending, None
        if staged is not None and staged.stream() is audio and staged.capture is capture:
            try:
                if not audio.closed and audio.tell() == 0:
                    return staged
            except (OSError, ValueError):
                pass
        raise RecognitionProcessError("recognition stage mismatch")

    def speakers(
        self,
        audio: BinaryIO,
        capture: CaptureInput,
        asr: ASRResult,
    ) -> tuple[SpeakerResult, ...]:
        staged = self._stage(audio, capture)
        # Native validates/clones the ASR DTOs between stages, so compare by value.
        if staged.asr is None or staged.asr != asr:
            raise RecognitionProcessError("recognition stage mismatch")
        speakers, staged.speakers, staged.asr = staged.speakers, (), None
        self._pending = staged
        return speakers

    def ambient(self, audio: BinaryIO, capture: CaptureInput) -> tuple[AmbientEvent, ...]:
        staged = self._stage(audio, capture)
        if staged.asr is not None:
            raise RecognitionProcessError("recognition stage mismatch")
        return staged.ambient
