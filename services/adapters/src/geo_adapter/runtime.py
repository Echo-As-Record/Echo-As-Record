"""Bounded point processing; durable cache and storage remain separate failure domains."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ear_adapter.ids import deterministic_id
from ear_contracts import NewRawRecord, RawRecord, new_id
from ear_storage_client import AppendRaw, Context, InvalidRequest, Query, StorageClient
from ear_storage_client.remote import RemoteStorageClient
from ear_storage_client.remote_errors import CommitUnknown

from geo_adapter.providers import Provider, TransientError, empty, select

VERSION = "geo/0.1.0"
READ_TYPES = ("gps_point", "gps_geocode")
PENDING = Query("gps_point", unreferenced_by=("gps_geocode", "gps_point_id"), limit=100)


class Cache:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "geo-cache.sqlite3"
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, payload: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?)", (key, json.dumps(payload))
            )

    def close(self) -> None:
        self.db.close()


class Runner:
    def __init__(
        self,
        storage: StorageClient,
        providers: list[Provider],
        cache: Cache,
        *,
        delay: float = 1.0,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_wait: float = 21600,
    ):
        self.storage, self.providers, self.cache = storage, providers, cache
        self.delay, self.attempts, self.sleep, self.clock = delay, attempts, sleep, clock
        self.wall_clock, self.max_wait = wall_clock, max_wait
        self.next_request = 0.0

    def resolve(self, point: RawRecord) -> dict | None:
        lat, lon = point.payload.get("latitude"), point.payload.get("longitude")
        if (
            isinstance(lat, bool)
            or isinstance(lon, bool)
            or not isinstance(lat, (int, float))
            or not isinstance(lon, (int, float))
            or not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            return empty(None, None, "failed")
        provider = select(self.providers, lat, lon)
        # Round both cache keys and queries so provenance describes the actual request.
        coordinate = provider.coordinate(round(lat, 4), round(lon, 4))
        key = json.dumps([provider.cache_namespace, provider.language, coordinate], sort_keys=True)
        result = self.cache.get(key)
        if result is not None:
            return result
        for attempt in range(self.attempts):
            self.sleep(max(0, self.next_request - self.clock()))
            self.next_request = self.clock() + self.delay
            try:
                result = provider.reverse(coordinate)
            except TransientError:
                if attempt + 1 < self.attempts:
                    self.sleep(min(2**attempt, 8))
                continue
            self.cache.put(key, result)
            return result
        # Do not cache failures across unrelated points at the same coordinate.
        if (self.wall_clock() - point.ingested_at).total_seconds() > self.max_wait:
            return empty(provider, coordinate, "failed")
        return None

    def run_once(
        self, *, should_stop: Callable[[], bool] = lambda: False, dry_run: bool = False
    ) -> dict[str, int]:
        for kind in READ_TYPES:
            self.storage.query(Query(kind, limit=1))
        if dry_run:
            return {"pending": self.storage.count(PENDING)}
        counts: dict[str, int] = {"written": 0, "failed": 0, "no_result": 0, "deferred": 0}
        # Re-query the first page after writing. Offset pagination would skip shrinking results.
        while not should_stop():
            points = self.storage.query(PENDING).items
            if not points:
                break
            for point in points:
                if should_stop():
                    return counts
                if not isinstance(point, RawRecord):
                    raise InvalidRequest("expected raw GPS point")
                payload = self.resolve(point)
                if payload is None:
                    counts["deferred"] = self.storage.count(PENDING)
                    return counts
                payload = payload | {"gps_point_id": point.id}
                key = f"{point.id}:geocode:{payload['provider']}"
                record = NewRawRecord(
                    id=deterministic_id(point.event_start, "gps_geocode", VERSION, key),
                    data_type="gps_geocode",
                    event_start=point.event_start,
                    event_end=point.event_end,
                    source=point.source,
                    adapter_version=VERSION,
                    dedup_key=key,
                    payload=payload,
                )
                ops, context, request_id = [AppendRaw(record)], Context.system(), new_id()
                for attempt in range(3):
                    try:
                        if isinstance(self.storage, RemoteStorageClient):
                            result = self.storage.write(ops, context, request_id=request_id)
                        else:
                            result = self.storage.write(ops, context)
                        break
                    except CommitUnknown as exc:
                        request_id = exc.request_id
                        if attempt == 2:
                            raise
                        self.sleep(0.25 * 2**attempt)
                else:
                    raise AssertionError("unreachable")
                if result.conflict is not None:
                    raise InvalidRequest("geocode write conflict")
                counts["written"] += sum(r.outcome == "written" for r in result.results)
                if payload["status"] in {"failed", "no_result"}:
                    counts[payload["status"]] += 1
                if payload["status"] == "failed" and payload["provider"] != "invalid":
                    # Stop requesting even when the exhausted point has expired.
                    counts["deferred"] = self.storage.count(PENDING)
                    return counts
        return counts
