"""Independent CLI; all credentials remain in the environment."""

from __future__ import annotations

import argparse
import math
import os
import signal
import sqlite3
from pathlib import Path
from threading import Event

import httpx
from ear_adapter.__main__ import _emit, _interval
from ear_storage_client import InvalidRequest, PermissionDenied
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import (
    AuthenticationError,
    CommitUnknown,
    ProtocolError,
    ResourceLimit,
    ServiceUnavailable,
)

from geo_adapter.providers import AmapProvider, ConfigurationError, NominatimProvider, Provider
from geo_adapter.runtime import Cache, Runner


def number(name: str, default: str, low: float, high: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or not low <= value <= high:
        raise ConfigurationError()
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="geo-adapter")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("dry-run")
    commands.add_parser("once")
    commands.add_parser("run").add_argument("--interval", type=_interval, default=60.0)
    args = parser.parse_args(argv)
    stop = Event()
    previous = {
        sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    cache: Cache | None = None
    try:
        url, token = os.environ["GEO_ADAPTER_STORAGE_URL"], os.environ["GEO_ADAPTER_STORAGE_TOKEN"]
        if not url or not token:
            raise ConfigurationError()
        timeout = number("GEO_ADAPTER_TIMEOUT", "10", 0.1, 60)
        delay = number("GEO_ADAPTER_DELAY", "1", 0, 60)
        max_wait = number("GEO_ADAPTER_MAX_WAIT_SECONDS", "21600", 0, math.inf)
        attempts = number("GEO_ADAPTER_ATTEMPTS", "3", 1, 5)
        if not attempts.is_integer():
            raise ConfigurationError()
        with httpx.Client(timeout=timeout, follow_redirects=False) as http:
            providers: list[Provider] = []
            if key := os.environ.get("GEO_ADAPTER_AMAP_KEY"):
                providers.append(AmapProvider(http, key))
            if base := os.environ.get("GEO_ADAPTER_NOMINATIM_URL"):
                providers.append(NominatimProvider(http, base))
            if not providers:
                raise ConfigurationError()
            cache = Cache(
                Path(os.environ.get("GEO_ADAPTER_DATA_ROOT", "~/.local/share/ear/geo")).expanduser()
            )
            while not stop.is_set():
                try:
                    with RemoteStorageClient(token, url, module="adapter") as storage:
                        runner = Runner(
                            storage,
                            providers,
                            cache,
                            delay=delay,
                            attempts=int(attempts),
                            max_wait=max_wait,
                        )
                        while not stop.is_set():
                            report = runner.run_once(
                                should_stop=stop.is_set, dry_run=args.command == "dry-run"
                            )
                            _emit("geocode_scan", **report)
                            if args.command != "run":
                                return int(bool(report.get("failed")))
                            stop.wait(args.interval)
                except (ServiceUnavailable, CommitUnknown):
                    _emit("geocode_storage_pending")
                    if args.command != "run":
                        return 1
                    stop.wait(args.interval)
    except (
        ConfigurationError,
        httpx.InvalidURL,
        AuthenticationError,
        PermissionDenied,
        InvalidRequest,
        ProtocolError,
        ResourceLimit,
        ValueError,
        KeyError,
        OSError,
        sqlite3.Error,
    ) as exc:
        _emit("geocode_configuration_error", error=type(exc).__name__)
        return 2
    finally:
        if cache is not None:
            cache.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
