"""Deployment-only entry point. Credentials come from an explicit environment variable."""

from __future__ import annotations

import argparse
import os

from ear_storage.schema import bootstrap, migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit internal storage administration")
    parser.add_argument("command", choices=("migrate", "bootstrap", "serve"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    dsn = os.environ.get("EAR_STORAGE_DSN")
    if not dsn:
        parser.error("EAR_STORAGE_DSN is required (no .env or default database is read)")
    try:
        if args.command == "migrate":
            migrate(dsn)
        elif args.command == "bootstrap":
            bootstrap(dsn)
        else:
            import uvicorn

            from ear_storage.http import create_app_from_dsn

            uvicorn.run(
                create_app_from_dsn(dsn),
                host=args.host,
                port=args.port,
                access_log=False,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=35,
                limit_concurrency=64,
            )
    except Exception:
        parser.exit(1, "storage command failed; check deployment configuration privately\n")
