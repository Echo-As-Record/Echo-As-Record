"""Read-only bounded probes. No writer gate, migration, bootstrap or diagnostics."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import psycopg
from ear_contracts import DEFAULT_REGISTRY

from ear_storage.consumer_broker import ConsumerBroker
from ear_storage.relay import RelayConfig
from ear_storage.schema import check_schema


class HealthProbe:
    def __init__(self, dsn: str, relay: RelayConfig | None = None) -> None:
        self._dsn, self._relay = dsn, relay

    def ready(self) -> bool:
        try:
            # A read-only transaction limits cumulative SQL time. No WRITER_GATE.
            with psycopg.connect(
                self._dsn,
                connect_timeout=2,
                options="-c statement_timeout=750ms -c lock_timeout=500ms "
                "-c transaction_timeout=2s -c default_transaction_read_only=on "
                "-c search_path=pg_catalog",
            ) as conn:
                check_schema(conn)
                names = {
                    row[0]
                    for row in conn.execute("SELECT name FROM storage.registry WHERE is_core")
                }
                modules = {row[0] for row in conn.execute("SELECT module FROM storage.modules")}
                if not DEFAULT_REGISTRY.core_types <= names:
                    return False
                if not {s.writer for s in DEFAULT_REGISTRY.data_types.values()} <= modules:
                    return False
            # Broker networking starts only AFTER the database connection is closed.
            if self._relay is not None:
                broker = ConsumerBroker(replace(self._relay, timeout=1.0))
                asyncio.run(self._verify_broker(broker))
            return True
        except Exception:
            return False

    @staticmethod
    async def _verify_broker(broker: ConsumerBroker) -> None:
        async with asyncio.timeout(1.5):
            await broker._call("verify")


class WorkerHealth:
    """Internal HTTP health: recent successful polling, never just a running PID."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.last_success: float | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in {"/health/live", "/health/ready"}:
                    self.send_error(404)
                    return
                live = self.path == "/health/live"
                ready = owner.ready()
                self.send_response(200 if live or ready else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    b'{"status":"alive"}'
                    if live
                    else b'{"status":"ready"}'
                    if ready
                    else b'{"status":"unavailable"}'
                )

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def record_poll(self, ready: bool) -> None:
        import time

        self.last_success = time.monotonic() if ready else None

    def ready(self) -> bool:
        import time

        return self.last_success is not None and time.monotonic() - self.last_success < 5

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.last_success = None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
