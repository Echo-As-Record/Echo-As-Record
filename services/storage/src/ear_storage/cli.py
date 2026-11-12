"""Deployment-only entry point. Credentials come from an explicit environment variable."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict

from ear_storage_client import ModelBodyPolicy

from ear_storage.schema import bootstrap, migrate


def model_body_policy_from_env(env: Mapping[str, str]) -> ModelBodyPolicy:
    """Explicit deployment config only; lowercase true/false, decimal positive bytes.

    Unset callers means all asserted callers; empty means none; otherwise a strict
    comma-separated tuple of module names. No .env, request or header discovery.
    """
    enabled = env.get("EAR_MODEL_BODY_ENABLED", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("EAR_MODEL_BODY_ENABLED must be true or false")
    size = env.get("EAR_MODEL_BODY_MAX_BYTES", "65536")
    if re.fullmatch(r"[1-9][0-9]*", size) is None:
        raise ValueError("EAR_MODEL_BODY_MAX_BYTES must be a positive decimal integer")
    callers = env.get("EAR_MODEL_BODY_ALLOWED_CALLERS")
    return ModelBodyPolicy(
        enabled=enabled == "true",
        max_bytes=int(size),
        allowed_callers=None
        if callers is None
        else (() if callers == "" else tuple(callers.split(","))),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit internal storage administration")
    parser.add_argument(
        "command",
        choices=(
            "init-identities",
            "bootstrap-core",
            "init-core-identities",
            "snapshot-sweep",
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
    parser.add_argument("--health-port", type=int, default=8001)
    parser.add_argument("--output", type=str)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    dsn = os.environ.get("EAR_STORAGE_DSN")
    if not dsn:
        parser.error("EAR_STORAGE_DSN is required (no .env or default database is read)")
    try:
        if args.command == "bootstrap-core":
            from ear_storage.core_deployment import bootstrap_core

            created = bootstrap_core(dsn)
            print(json.dumps({"status": "created" if created else "unchanged"}))
        elif args.command == "init-core-identities":
            from pathlib import Path

            from ear_storage.core_deployment import init_core_identities

            if not args.output:
                parser.error("init-core-identities requires --output in a new private directory")
            created = init_core_identities(dsn, Path(args.output))
            print(json.dumps({"status": "created" if created else "unchanged"}))
        elif args.command == "init-identities":
            from pathlib import Path

            from ear_storage.deployment import init_identities

            if not args.output:
                parser.error("init-identities requires --output (no stdout credential delivery)")
            created = init_identities(dsn, Path(args.output))
            print(json.dumps({"status": "created" if created else "unchanged"}))
        elif args.command == "snapshot-sweep":
            from ear_storage.snapshots import sweep_expired

            print(json.dumps({"deleted": sweep_expired(dsn, batch_size=args.batch_size)}))
        elif args.command == "migrate":
            migrate(dsn)
        elif args.command == "bootstrap":
            bootstrap(dsn)
        elif args.command == "manage-worker":
            import time

            from ear_storage.backend import PostgresStorage
            from ear_storage.consumers import ConsumerService
            from ear_storage.health import WorkerHealth
            from ear_storage.management import CONSUMER, ManagementProcessor
            from ear_storage.relay import RelayConfig
            from ear_storage.schema import connection

            config = RelayConfig(
                servers=tuple(os.environ["EAR_NATS_SERVERS"].split(",")),
                token=os.environ.get("EAR_NATS_TOKEN"),
                stream=os.environ.get("EAR_NATS_STREAM", "EAR_EVENTS"),
                prefix=os.environ.get("EAR_NATS_PREFIX", "ear.events"),
            )
            worker = ManagementProcessor(PostgresStorage(dsn), ConsumerService(dsn, config))
            token = os.environ["EAR_MANAGEMENT_WORKER_TOKEN"]
            with WorkerHealth(args.host, args.health_port) as health:
                while True:
                    worker.poll_once(token)
                    with connection(dsn) as conn:
                        row = conn.execute(
                            "SELECT ready AND enabled FROM storage.consumers WHERE name=%s",
                            (CONSUMER,),
                        ).fetchone()
                    health.record_poll(row == (True,))
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
                    create_app_from_dsn(
                        dsn,
                        relay_config=relay_config,
                        model_body_policy=model_body_policy_from_env(os.environ),
                    )
                ),
                host=args.host,
                port=args.port,
                access_log=False,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=35,
                limit_concurrency=64,
            )
    except Exception as exc:
        import traceback

        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "frames": [
                        {"file": frame.filename, "line": frame.lineno}
                        for frame in traceback.extract_tb(exc.__traceback__)
                    ],
                }
            )
        )
        parser.exit(1, "storage command failed; check deployment configuration privately\n")
