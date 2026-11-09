"""Deployment CLI; explicit protected config file, no migrations or resource creation."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from ear_api.config import Settings
from ear_api.models import load_json_object


def load_settings(path: str) -> Settings:
    try:
        location = Path(path)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        descriptor = os.open(location, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_size > 65536
            ):
                raise ValueError
            raw = handle.read(65537)
            after = os.fstat(handle.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError
        # Pydantic's JSON parser accepts duplicate keys; use one strict bounded decoder.
        return Settings.model_validate(load_json_object(raw, authority=False))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("protected config required") from None


def main() -> int:
    parser = argparse.ArgumentParser(prog="ear-api")
    parser.add_argument("command", choices=["serve", "check", "health"])
    parser.add_argument("--config", default=os.environ.get("EAR_API_CONFIG"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8086)
    args = parser.parse_args()
    try:
        if not args.config or not 1 <= args.port <= 65535:
            raise ValueError
        settings = load_settings(args.config)
        if args.command == "health":
            from ear_api.upstream import Upstream

            healthy = Upstream(settings).ready()
            print(
                json.dumps(
                    {
                        "status": "ready" if healthy else "unavailable",
                        "scope": "api_storage_connectivity",
                    }
                )
            )
            return 0 if healthy else 1
        if args.command == "check":
            print('{"status":"configuration_valid"}')
            return 0
        import uvicorn

        from ear_api.app import create_app

        uvicorn.run(
            create_app(settings),
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
            log_level="warning",
        )
        return 0
    except Exception:
        # No config, token, body, traceback, DSN or external exception text on stderr.
        print('{"code":"api_startup_or_health_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
