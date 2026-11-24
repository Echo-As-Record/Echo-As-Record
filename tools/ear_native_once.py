"""Process exactly one privately selected Octopus raw manifest on Linux.

Usage: python tools/ear_native_once.py --config /absolute/private.json [--remote]

Keep the JSON outside Git, regular, single-link, owned by the caller and 0600.
Required keys (no other keys): record (relative Octopus manifest path), data_root
(absolute, trusted read-only view), launcher_argv (nonempty array of strings,
absolute executable first), ffmpeg_path and ffprobe_path (absolute executable
paths), adapter_version (ear-native/0.1.0+<profile>), offline_isolated (true).
Optional paired keys: storage_url (HTTPS origin or literal loopback HTTP origin)
and storage_token (internal 43-character token). Both are required by --remote.
Use a dedicated adapter storage identity; no old Ear or provider credential.
Use one writer per capture/profile, and change profile when the recognition
assets, normalization build, or settings change.

Dry-run runs recognition but writes only to disposable MemoryStorage. --remote
may send transcripts and voiceprints in one atomic batch to the specified
storage service. Neither mode scans,
schedules, or falls back to ambient configuration. Exit 0: ingested/already
ingested; 1: failed/pending (a remote commit can be unknown); 2: rejected input.

The isolation flag records an operator assertion, not a sandbox check. Provision
and verify the offline launcher, the read-only source/model/venv assets, private
scratch, OS-enforced no egress or local service access, hard resource/time limits
and whole-job teardown. For the legacy bridge, the launcher must arrange its
required offline flag and assets. Use trusted, isolated FD-capable FFmpeg tools
for M4A. Launcher argv must contain no secrets, source recording paths or capture
timestamps. The transport passes normalized audio and UTC bounds; those bounds
can be visible in the local process list. Use an operator-approved isolated host.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import ssl
import stat
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import certifi
import httpx
from ear_adapter.config import AdapterConfig
from ear_adapter.native_channels import (
    ChannelInputError,
    OctopusInboxChannel,
    _parent,
    _parse_manifest,
    _pin,
)
from ear_adapter.native_pipeline import NATIVE_ADAPTER_VERSION, ingest_native
from ear_adapter.recognition_decode import FFmpegDecoder, NormalizingRecognitionProvider
from ear_adapter.recognition_process import RecognitionProcess
from ear_adapter.runtime import AlreadyIngested, Ingested, Outcome, Pending
from ear_storage_client import MemoryStorage
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import ServiceUnavailable

_REQUIRED = {
    "record",
    "data_root",
    "launcher_argv",
    "ffmpeg_path",
    "ffprobe_path",
    "adapter_version",
    "offline_isolated",
}
_TARGET = {"storage_url", "storage_token"}
_PROFILE = re.compile(re.escape(NATIVE_ADAPTER_VERSION) + r"\+[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_ORIGIN = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?/?")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_MAX_CONFIG = 32 * 1024
_Status = Literal[
    "invalid_arguments",
    "invalid_config",
    "invalid_record",
    "failed",
    "pending",
    "ingested",
    "already_ingested",
]


@dataclass(frozen=True, slots=True, repr=False)
class _Settings:
    record: str
    root: Path
    command: tuple[str, ...]
    ffmpeg: str
    ffprobe: str
    version: str
    target: tuple[str, str] | None


def _text(value: object, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or not value.isprintable()
        or not value.strip()
        or len(value.encode("utf-8")) > limit
    ):
        raise ValueError
    return value


def _absolute(value: object) -> Path:
    text = _text(value, 4096)
    parts = text.split("/")
    if (
        parts[0]
        or any(not p or p in {".", ".."} or len(p.encode("utf-8")) > 255 for p in parts[1:])
        or len(parts) > 33
    ):
        raise ValueError
    return Path(text)


def _executable(value: object) -> str:
    path = _absolute(value)
    if not stat.S_ISREG(path.stat().st_mode) or not os.access(path, os.X_OK):
        raise ValueError
    return str(path)


def _protected_config(filename: str) -> bytes:
    path = _absolute(filename)
    # Reuse the adapter's no-follow directory walk and pinned regular-file
    # verification. Unlike a raw manifest, configuration also requires 0600.
    with _parent(path.parent, ()) as (parent, chain):
        for _, _, fd in chain:
            info = os.fstat(fd)
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                raise ValueError
        with _pin(parent, chain, path.name, limit=_MAX_CONFIG) as pinned:
            info = pinned.before
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError
            return pinned.read_manifest()


def _origin(value: object) -> str:
    text = _text(value, 512)
    if _ORIGIN.fullmatch(text) is None:
        raise ValueError
    url = urlsplit(text)
    host = url.hostname
    if host is None or (url.port is not None and url.port == 0):
        raise ValueError
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            url.scheme != "https"
            or len(host) > 253
            or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
        ):
            raise ValueError from None
    else:
        if url.scheme == "http" and not address.is_loopback:
            raise ValueError
    return text


def _settings(filename: str, *, remote: bool) -> _Settings:
    value = _parse_manifest(_protected_config(filename))
    if (
        value.keys() not in (_REQUIRED, _REQUIRED | _TARGET)
        or value["offline_isolated"] is not True
    ):
        raise ValueError
    target = None
    if value.keys() >= _TARGET:
        token = _text(value["storage_token"], 43)
        if _TOKEN.fullmatch(token) is None:
            raise ValueError
        target = (_origin(value["storage_url"]), token)
    if remote and target is None:
        raise ValueError
    version = _text(value["adapter_version"], 160)
    if _PROFILE.fullmatch(version) is None:
        raise ValueError
    argv = value["launcher_argv"]
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        raise ValueError
    command = tuple(_text(arg, 4096) for arg in argv)
    if any(
        arg.split("=", 1)[0] in {"--started-at", "--ended-at"}
        or (target is not None and target[1] in arg)
        for arg in command
    ):
        raise ValueError
    return _Settings(
        _text(value["record"], 4096),
        _absolute(value["data_root"]),
        (_executable(command[0]), *command[1:]),
        _executable(value["ffmpeg_path"]),
        _executable(value["ffprobe_path"]),
        version,
        target,
    )


def _result(status: _Status, *, remote: bool, written: int = 0, deduplicated: int = 0) -> int:
    print(
        json.dumps(
            {
                "status": status,
                "mode": "remote" if remote else "dry_run",
                "written": written,
                "deduplicated": deduplicated,
            }
        )
    )
    if status in {"ingested", "already_ingested"}:
        return 0
    return 1 if status in {"pending", "failed"} else 2


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--help"]:
        print(__doc__)
        return 0
    remote = len(args) == 3 and args[2] == "--remote"
    if len(args) != (3 if remote else 2) or args[0] != "--config":
        return _result("invalid_arguments", remote=remote)
    try:
        settings = _settings(args[1], remote=remote)
        provider = NormalizingRecognitionProvider(
            RecognitionProcess(settings.command), FFmpegDecoder(settings.ffmpeg, settings.ffprobe)
        )
    except (OSError, ValueError, TypeError, RecursionError):
        return _result("invalid_config", remote=remote)
    except Exception:
        return _result("failed", remote=remote)
    channel = OctopusInboxChannel(settings.root)
    try:
        capture = channel.load(settings.record)
        if capture.media_type not in {"audio/wav", "audio/mp4"} or not (
            timedelta(seconds=1) <= capture.ended_at - capture.started_at <= timedelta(seconds=30)
        ):
            raise ChannelInputError("unsupported_record")
    except (ChannelInputError, OSError, ValueError):
        return _result("invalid_record", remote=remote)
    except Exception:
        return _result("failed", remote=remote)
    config = AdapterConfig(
        adapter_version=settings.version, channel=channel.channel_id, data_root=settings.root
    )
    try:
        outcome: Outcome
        if remote:
            assert settings.target is not None
            url, token = settings.target
            # Do not inherit proxy, CA, or TLS key-log environment settings.
            # The SDK disables redirects.
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.load_verify_locations(cafile=certifi.where())
            with (
                httpx.HTTPTransport(retries=0, trust_env=False, verify=tls) as transport,
                RemoteStorageClient(token, url, module="adapter", transport=transport) as storage,
            ):
                outcome = ingest_native(
                    capture, root=settings.root, provider=provider, storage=storage, config=config
                )
        else:
            outcome = ingest_native(
                capture,
                root=settings.root,
                provider=provider,
                storage=MemoryStorage().client("adapter"),
                config=config,
            )
    except ServiceUnavailable:
        return _result("pending", remote=remote)
    except Exception:
        # No server body, SDK diagnostics, private paths or provider errors in output.
        return _result("failed", remote=remote)
    if isinstance(outcome, Ingested):
        return _result(
            "ingested",
            remote=remote,
            written=sum(outcome.written.values()),
            deduplicated=outcome.deduplicated,
        )
    if isinstance(outcome, AlreadyIngested):
        return _result("already_ingested", remote=remote)
    return _result("pending" if isinstance(outcome, Pending) else "failed", remote=remote)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(_result("failed", remote=sys.argv[3:] == ["--remote"])) from None
