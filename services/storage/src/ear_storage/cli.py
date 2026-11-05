"""Deployment-only entry point. Credentials come from an explicit environment variable."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict

from ear_storage.schema import bootstrap, migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit internal storage administration")
    parser.add_argument(
        "command",
        choices=(
            "migrate",
            "bootstrap",
            "serve",
            "relay",
            "relay-health",
            "relay-pause",
            "relay-resume",
            "manage-worker",
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    dsn = os.environ.get("EAR_STORAGE_DSN")
    if not dsn:
        parser.error("EAR_STORAGE_DSN is required (no .env or default database is read)")
    try:
        if args.command == "migrate":
            migrate(dsn)
        elif args.command == "bootstrap":
            bootstrap(dsn)
        elif args.command == "manage-worker":
            import time

            from ear_storage.backend import PostgresStorage
            from ear_storage.consumers import ConsumerService
            from ear_storage.management import ManagementProcessor
            from ear_storage.relay import RelayConfig

            config = RelayConfig(
                servers=tuple(os.environ["EAR_NATS_SERVERS"].split(",")),
                token=os.environ.get("EAR_NATS_TOKEN"),
                stream=os.environ.get("EAR_NATS_STREAM", "EAR_EVENTS"),
                prefix=os.environ.get("EAR_NATS_PREFIX", "ear.events"),
            )
            worker = ManagementProcessor(PostgresStorage(dsn), ConsumerService(dsn, config))
            token = os.environ["EAR_MANAGEMENT_WORKER_TOKEN"]
            while True:
                worker.poll_once(token)
                if args.once:
                    break
                time.sleep(0.2)
        elif args.command.startswith("relay"):
            from ear_storage.relay import RelayConfig, RelayStore, run_relay

            config = RelayConfig(
                servers=tuple(os.environ["EAR_NATS_SERVERS"].split(",")),
                token=os.environ.get("EAR_NATS_TOKEN"),
                stream=os.environ.get("EAR_NATS_STREAM", "EAR_EVENTS"),
                prefix=os.environ.get("EAR_NATS_PREFIX", "ear.events"),
            )
            store = RelayStore(dsn, config)
            if args.command == "relay":
                print(json.dumps(asdict(asyncio.run(run_relay(store, once=args.once)))))
            else:
                if args.command != "relay-health":
                    store.pause(args.command == "relay-pause")
                print(json.dumps(store.health()))
        else:
            import uvicorn

            from ear_storage.http import create_app_from_dsn
            from ear_storage.relay import RelayConfig

            servers = os.environ.get("EAR_NATS_SERVERS")
            relay_config = (
                None
                if not servers
                else RelayConfig(
                    servers=tuple(servers.split(",")),
                    token=os.environ.get("EAR_NATS_TOKEN"),
                    stream=os.environ.get("EAR_NATS_STREAM", "EAR_EVENTS"),
                    prefix=os.environ.get("EAR_NATS_PREFIX", "ear.events"),
                )
            )
            uvicorn.run(
                (
                    create_app_from_dsn(dsn)
                    if relay_config is None
                    else create_app_from_dsn(dsn, relay_config=relay_config)
                ),
                host=args.host,
                port=args.port,
                access_log=False,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=35,
                limit_concurrency=64,
            )
    except Exception:
        parser.exit(1, "storage command failed; check deployment configuration privately\n")
