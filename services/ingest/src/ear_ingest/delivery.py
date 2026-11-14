"""Durable publication and single-record delivery to the legacy Ear event API."""

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing, contextmanager, suppress
from pathlib import Path

import httpx

logger = logging.getLogger("ear_ingest.delivery")


class Delivery:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.directory = self.root / ".ear-ingest"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = self.directory / "delivery.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS publications (
                    instance TEXT PRIMARY KEY, plan TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    instance TEXT, record_id TEXT, revision TEXT NOT NULL, kind TEXT NOT NULL,
                    group_id TEXT NOT NULL, segment_index INTEGER NOT NULL, event TEXT NOT NULL,
                    state TEXT NOT NULL, due REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (instance, record_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, due);
                CREATE TABLE IF NOT EXISTS receipts (
                    seq INTEGER PRIMARY KEY, instance TEXT NOT NULL, record_id TEXT NOT NULL,
                    kind TEXT NOT NULL, capture_id TEXT NOT NULL, accepted_at REAL NOT NULL,
                    pipeline_run_id TEXT, revision TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.database, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def prepare(self, instance, record, staged_file, target, staged_record, path, obsolete):
        # Both temporary files exist before registering the intent. Recovery can replay
        # either rename, including a crash between publishing the attachment and metadata.
        with staged_file.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256")
        digest.update(json.dumps(record, sort_keys=True).encode())
        digest.update(str(path).encode())
        payload = {key: record[key] for key in ("group_id", "name", "type", "timestamp", "source")}
        payload.update(
            collection="phone",
            record_path=str(path),
            record_id=record["id"],
            channel_id="brain",
            instance_id=instance,
            file_path=str(target),
            filename=target.name,
            processing_status="ready",
        )
        plan = {
            "moves": [[str(staged_file), str(target)], [str(staged_record), str(path)]],
            "obsolete": [str(p) for p in obsolete],
            "revision": digest.hexdigest(),
            "segment_index": record["segment_meta"]["segmentIndex"],
            "event": {
                "event_type": "brain.record.received",
                "producer": {"type": "channel", "id": "brain"},
                "payload": payload,
            },
        }
        with self.connect() as db:
            db.execute("INSERT INTO publications VALUES (?,?)", (instance, json.dumps(plan)))

    def publish(self, instance):
        # The caller holds this instance's writer lock.
        with self.connect() as db:
            row = db.execute(
                "SELECT plan FROM publications WHERE instance=?", (instance,)
            ).fetchone()
            if not row:
                return
            plan = json.loads(row["plan"])
            for source, target in plan["moves"]:
                if Path(source).exists():
                    os.replace(source, target)
                elif not Path(target).is_file():
                    raise FileNotFoundError(target)
            for path in plan["obsolete"]:
                Path(path).unlink(missing_ok=True)
            payload = plan["event"]["payload"]
            state = "pending" if payload["type"] in {"richVoice", "phoneContext"} else "archived"
            db.execute(
                """
                INSERT INTO jobs(instance, record_id, revision, kind, group_id, segment_index,
                                 event, state) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(instance,record_id) DO UPDATE SET revision=excluded.revision,
                    kind=excluded.kind, group_id=excluded.group_id,
                    segment_index=excluded.segment_index,
                    event=excluded.event, state=excluded.state, attempts=0, reason='', due=0
                WHERE jobs.revision != excluded.revision
            """,
                (
                    instance,
                    payload["record_id"],
                    plan["revision"],
                    payload["type"],
                    payload["group_id"],
                    plan["segment_index"],
                    json.dumps(plan["event"]),
                    state,
                ),
            )
            db.execute("DELETE FROM publications WHERE instance=?", (instance,))

    def recover(self):
        with self.connect() as db:
            instances = [r[0] for r in db.execute("SELECT instance FROM publications")]
        for instance in instances:
            with (self.root / instance / ".ingest.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.publish(instance)

    def dispatch(self, client: httpx.Client, url: str) -> bool:
        # Serialize consumers across uvicorn workers without locking the database while
        # Ear reads the shared inbox. A killed dispatcher releases this advisory lock.
        with (self.directory / "delivery.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            with self.connect() as db:
                job = db.execute(
                    """
                    SELECT * FROM jobs WHERE state='pending' AND due<=?
                    ORDER BY due, rowid LIMIT 1
                """,
                    (time.time(),),
                ).fetchone()
            if not job:
                return False
            try:
                response = client.post(url, json=json.loads(job["event"]))
                response.raise_for_status()
                body = response.json()
                status = body["status"]
                reason = body["reason"]
                if status not in ("accepted", "retry", "rejected") or not isinstance(reason, str):
                    raise ValueError("Invalid decision")
                if status == "accepted" and not isinstance(body.get("capture_id"), str):
                    raise ValueError("Missing capture id")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                body, status, reason = {}, "retry", type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    reason = f"http_{exc.response.status_code}"
            with self.connect() as db:
                state = "pending" if status == "retry" else status
                due = time.time() + min(300, 2 ** min(9, job["attempts"]))
                changed = db.execute(
                    """
                    UPDATE jobs SET state=?, reason=?, due=?, attempts=attempts+1
                    WHERE instance=? AND record_id=? AND revision=? AND state='pending'
                """,
                    (state, reason, due, job["instance"], job["record_id"], job["revision"]),
                )
                if status == "accepted" and changed.rowcount:
                    db.execute(
                        """
                        INSERT INTO receipts(instance, record_id, kind, capture_id, accepted_at,
                                             pipeline_run_id, revision) VALUES(?,?,?,?,?,?,?)
                    """,
                        (
                            job["instance"],
                            job["record_id"],
                            job["kind"],
                            body["capture_id"],
                            time.time(),
                            body.get("pipeline_run_id"),
                            job["revision"],
                        ),
                    )
                    if job["kind"] == "richVoice":
                        db.execute(
                            """
                            UPDATE jobs SET due=0
                            WHERE instance=? AND group_id=? AND segment_index=?
                            AND kind='phoneContext' AND state='pending'
                            AND reason='matching_audio_capture_not_ready'
                        """,
                            (job["instance"], job["group_id"], job["segment_index"]),
                        )
            return True

    async def run(self, url: str, stop: asyncio.Event):
        with httpx.Client(timeout=15, trust_env=False) as client:
            while not stop.is_set():
                try:
                    if await asyncio.to_thread(self.dispatch, client, url):
                        continue
                except (OSError, sqlite3.Error):
                    logger.exception("Delivery worker could not access the local queue")
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
