"""Run the local native recording worker. No legacy inference or discovery."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import ssl
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from ear_storage_client.remote import RemoteStorageClient

from ear_adapter.config import AdapterConfig
from ear_adapter.native_channels import _DirectoryChain, _parent, _parse_manifest, _pin
from ear_adapter.native_pipeline import NATIVE_ADAPTER_VERSION
from ear_adapter.recognition_decode import FFmpegDecoder
from ear_adapter.recognition_long import ChunkedRecognitionProvider
from ear_adapter.recognition_process import RecognitionProcess
from ear_adapter.recording_intake import RecordingIntake, WorkerBusy
from ear_adapter.recording_worker import NativeRecordingPreparation, NativeRecordingPublication

_HELP = "Usage: ear-native-worker --config /absolute/private.json [--once | --watch]"
_KEYS = {
    "spool_root",
    "scratch_dir",
    "adapter_version",
    "offline_isolated",
    "launcher_argv",
    "ffmpeg_path",
    "ffprobe_path",
    "storage_url",
    "storage_token",
}
_PROFILE = re.compile(re.escape(NATIVE_ADAPTER_VERSION) + r"\+[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_ORIGIN = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?/?")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_CHANNEL = "recording_intake"
_MAX_CONFIG_BYTES = 32 << 10
_POLL_SECONDS = 1.0
_MAX_BURST = 16


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    spool_root: Path
    scratch_dir: Path
    version: str
    command: tuple[str, ...]
    ffmpeg: str
    ffprobe: str
    storage_url: str
    storage_token: str


def _text(value: object, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not value.isprintable()
        or len(value) > limit
        or len(value.encode("utf-8")) > limit
    ):
        raise ValueError
    return value


def _absolute(value: object) -> Path:
    text = _text(value, 4096)
    root, *parts = text.split("/")
    if (
        root
        or not 1 <= len(parts) <= 32
        or any(p in {"", ".", ".."} or len(p.encode("utf-8")) > 255 for p in parts)
    ):
        raise ValueError
    return Path(text)


@contextmanager
def _trusted_directory(path: Path) -> Iterator[tuple[int, _DirectoryChain]]:
    with _parent(path, ()) as (fd, chain):
        for _, _, directory in chain:
            info = os.fstat(directory)
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                raise ValueError
        yield fd, chain


def _private_directory(path: Path) -> Path:
    with _trusted_directory(path) as (fd, _):
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError
    return path


def _executable(value: object) -> str:
    path = _absolute(value)
    with _trusted_directory(path.parent), _trusted_directory(path.resolve(strict=True).parent):
        info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
        or not os.access(path, os.X_OK)
    ):
        raise ValueError
    # Preserve operator-selected paths (including a venv interpreter symlink).
    return str(path)


def _storage_origin(value: object) -> str:
    text = _text(value, 512)
    if _ORIGIN.fullmatch(text) is None:
        raise ValueError
    url = urlsplit(text)
    host = url.hostname
    if host is None or url.port == 0:
        raise ValueError
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            url.scheme != "https"
            or len(host) > 253
            or not all(_LABEL.fullmatch(label) for label in host.split("."))
        ):
            raise ValueError from None
    else:
        if url.scheme == "http" and not address.is_loopback:
            raise ValueError
    return text


def _settings(filename: str) -> Settings:
    path = _absolute(filename)
    # Keep the directory chain pinned through the shared regular-file checks.
    with (
        _trusted_directory(path.parent) as (parent, chain),
        _pin(parent, chain, path.name, limit=_MAX_CONFIG_BYTES) as pinned,
    ):
        info = pinned.before
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        value = _parse_manifest(pinned.read_manifest())
    if value.keys() != _KEYS or value["offline_isolated"] is not True:
        raise ValueError
    version = _text(value["adapter_version"], 160)
    token = _text(value["storage_token"], 43)
    if _PROFILE.fullmatch(version) is None or _TOKEN.fullmatch(token) is None:
        raise ValueError
    args = value["launcher_argv"]
    if not isinstance(args, list) or not 1 <= len(args) <= 64:
        raise ValueError
    command = tuple(_text(arg, 4096) for arg in args)
    if any(
        arg.split("=", 1)[0] in {"--started-at", "--ended-at"} or token in arg for arg in command
    ):
        raise ValueError
    return Settings(
        spool_root=_private_directory(_absolute(value["spool_root"])),
        scratch_dir=_private_directory(_absolute(value["scratch_dir"])),
        version=version,
        command=(_executable(command[0]), *command[1:]),
        ffmpeg=_executable(value["ffmpeg_path"]),
        ffprobe=_executable(value["ffprobe_path"]),
        storage_url=_storage_origin(value["storage_url"]),
        storage_token=token,
    )


class _Shutdown:
    requested = False

    def request(self, *_: object) -> None:
        # A signal handler must not raise, acquire a lock, or interrupt publication.
        self.requested = True


@contextmanager
def _shutdown_signals() -> Iterator[_Shutdown]:
    stop = _Shutdown()
    term = signal.signal(signal.SIGTERM, stop.request)
    try:
        interrupt = signal.signal(signal.SIGINT, stop.request)
        try:
            yield stop
        finally:
            signal.signal(signal.SIGINT, interrupt)
    finally:
        signal.signal(signal.SIGTERM, term)


def _transport() -> httpx.HTTPTransport:
    # Use the system trust locations compiled into OpenSSL, not environment
    # overrides, proxies or TLS key logging. Never downgrade HTTPS.
    paths = ssl.get_default_verify_paths()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cafile = paths.openssl_cafile if os.path.isfile(paths.openssl_cafile) else None
    capath = paths.openssl_capath if os.path.isdir(paths.openssl_capath) else None
    if cafile is not None or capath is not None:
        tls.load_verify_locations(cafile=cafile, capath=capath)
    return httpx.HTTPTransport(retries=0, trust_env=False, verify=tls)


def _run(settings: Settings, stop: _Shutdown, *, watch: bool) -> str:
    provider = ChunkedRecognitionProvider(
        RecognitionProcess(settings.command),
        FFmpegDecoder(settings.ffmpeg, settings.ffprobe, timeout=60),
        scratch_dir=str(settings.scratch_dir),
    )
    prepare = NativeRecordingPreparation(
        provider,
        AdapterConfig(
            adapter_version=settings.version, channel=_CHANNEL, data_root=settings.spool_root
        ),
        normalized_duration=lambda job, audio: provider.decoded_duration(
            audio, media_type=job.media_type, size=job.size
        ),
    )
    if stop.requested:
        return "stopped"
    with (
        _transport() as transport,
        RemoteStorageClient(
            settings.storage_token, settings.storage_url, module="adapter", transport=transport
        ) as storage,
    ):
        if stop.requested:
            return "stopped"
        publish = NativeRecordingPublication(storage)
        with RecordingIntake(settings.spool_root).worker() as worker:
            while not stop.requested:
                for _ in range(_MAX_BURST if watch else 1):
                    if stop.requested:
                        break
                    claimed = worker.run_one(prepare, publish)
                    if not watch:
                        return "processed" if claimed else "idle"
                    if not claimed:
                        break
                if not stop.requested:
                    time.sleep(_POLL_SECONDS)
    return "stopped"


def _report(status: str, code: int) -> int:
    print(
        json.dumps(
            {
                "module": "adapter",
                "event_id": None,
                "root_feedback_id": "system",
                "depth": 0,
                "status": status,
            }
        )
    )
    return code


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--help"]:
        print(_HELP)
        return 0
    if (
        len(args) not in (2, 3)
        or args[0] != "--config"
        or (len(args) == 3 and args[2] not in {"--once", "--watch"})
    ):
        return _report("invalid_arguments", 2)
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with _shutdown_signals() as stop:
            try:
                if sys.platform != "linux":
                    raise ValueError
                settings = _settings(args[1])
            except (OSError, ValueError, TypeError, RecursionError):
                return _report("invalid_config", 2)
            return _report(_run(settings, stop, watch=args[2:] == ["--watch"]), 0)
    except WorkerBusy:
        return _report("busy", 1)
    except Exception:
        # No private path, SDK response, credentials or model diagnostics on stdout/stderr.
        return _report("worker_unavailable", 1)
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())
