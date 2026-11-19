"""Linux single-process ③/④/⑤ host; only --config accepts runtime configuration.

Private JSON keys: module (attribution/extraction/dependency), storage_url,
storage_token, consumer, journal_namespace, state_dir (existing owned 0700).
Consumers are fixed: attribution / extraction.jobs / dependency.changes.
③ additionally requires cosine_threshold and gps_threshold_m. ④ requires
 gateway_url and model; its storage identity authenticates at ⑦ too. Optional
schedule={start: RFC3339 UTC ending Z, every_seconds: positive integer} is the
ONLY timer opt-in. No history registration, memory fallback or provider factory.

Keep the state directory/namespace with the same deployment and identity across
restarts. State identity also binds the storage origin (token rotation is allowed).
Do not share a consumer across hosts: flock is local, not a distributed lease.
Health describes the pump, NOT business completion, graph freshness or model
availability. A long in-flight operation may conservatively fail the age check.
SIGTERM/SIGINT stop admission between bounded cycles/jobs; in-flight synchronous
calls finish normally. Forced kills retain journal/extraction recovery evidence.
Local POSIX filesystem only; not a sandbox against malicious root/same-UID code.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Literal, Self

import httpx
from ear_contracts import SystemRecord
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import TERMINAL, ExtractionService
from ear_extraction.worker import Schedule, serve
from ear_storage_client import Query
from ear_storage_client.remote import RemoteStorageClient
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

CONSUMERS = {
    "attribution": "attribution",
    "extraction": "extraction.jobs",
    "dependency": "dependency.changes",
}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
MAX_CONFIG = 65536
ERROR_METADATA = (
    '{"module":"mvp_worker","event_id":null,"root_feedback_id":"system",'
    '"depth":0,"code":"mvp_worker_unavailable"}'
)


class SafeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Timer(SafeModel):
    start: str = Field(strict=True)
    every_seconds: int = Field(strict=True, ge=1, le=604800)

    def schedule(self) -> Schedule:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", self.start):
            raise ValueError("invalid_timer")
        return Schedule(datetime.fromisoformat(self.start), timedelta(seconds=self.every_seconds))

    @model_validator(mode="after")
    def valid(self) -> Self:
        self.schedule()
        return self


class Settings(SafeModel):
    module: Literal["attribution", "extraction", "dependency"]
    storage_url: str = Field(strict=True, repr=False)
    storage_token: SecretStr = Field(repr=False)
    consumer: str = Field(strict=True)
    journal_namespace: str = Field(strict=True, min_length=1, max_length=128, repr=False)
    state_dir: str = Field(strict=True, repr=False)
    poll_seconds: float = Field(default=1, strict=True, ge=0.05, le=60, allow_inf_nan=False)
    timeout: float = Field(default=35, strict=True, gt=0, le=120, allow_inf_nan=False)
    health_max_age_seconds: int = Field(default=300, strict=True, ge=5, le=3600)
    max_jobs_per_tick: int = Field(default=1, strict=True, ge=1, le=100)
    # Decide after each delivery by default: a batch can otherwise keep old
    # roots open until later corrections arrive and delay authoritative Finish
    # polling. Larger finite batches remain an explicit throughput trade-off.
    max_events_per_tick: int = Field(default=1, strict=True, ge=1, le=100)
    max_records: int = Field(default=5000, strict=True, ge=1, le=10000)
    cosine_threshold: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    gps_threshold_m: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    gateway_url: str | None = Field(default=None, strict=True, repr=False)
    model: str | None = Field(default=None, strict=True, min_length=1, max_length=128)
    schedule: Timer | None = None

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.consumer != CONSUMERS[self.module]:
            raise ValueError("invalid_consumer")
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", self.storage_token.get_secret_value()) is None:
            raise ValueError("invalid_credentials")
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.journal_namespace) is None:
            raise ValueError("invalid_namespace")
        checked_path(self.state_dir)
        if self.health_max_age_seconds <= self.poll_seconds:
            raise ValueError("invalid_health_interval")
        for address in (self.storage_url, self.gateway_url):
            if address is None:
                continue
            if any(ord(c) <= 32 or ord(c) == 127 or c in "?#" for c in address):
                raise ValueError("invalid_origin")
            url = httpx.URL(address)
            if (
                url.scheme not in {"http", "https"}
                or not url.host
                or url.userinfo
                or url.raw_path != b"/"
            ):
                raise ValueError("invalid_origin")
        if self.module == "attribution":
            from ear_attribution.matching import MatchConfig

            if self.cosine_threshold is None or self.gps_threshold_m is None:
                raise ValueError("explicit_thresholds_required")
            MatchConfig(self.cosine_threshold, self.gps_threshold_m)
        elif self.cosine_threshold is not None or self.gps_threshold_m is not None:
            raise ValueError("unexpected_thresholds")
        if self.module == "extraction":
            if self.gateway_url is None or self.model is None:
                raise ValueError("gateway_required")
        elif any(value is not None for value in (self.gateway_url, self.model, self.schedule)):
            raise ValueError("unexpected_gateway_or_timer")
        return self


def checked_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ValueError("unsafe_path")
    return path


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("unsafe_file")


def directory(path: Path, *, private: bool = False) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Walk only directory FDs, never resolve/follow a symlink or chmod existing paths."""
    fd = os.open("/", DIR_FLAGS)
    chain = []
    info = os.fstat(fd)
    try:
        for index, part in enumerate(("/", *path.parts[1:])):
            if index:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                raise ValueError("untrusted_ancestor")
            chain.append(identity(info))
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("unsafe_state_directory")
        return fd, tuple(chain)
    except BaseException:
        os.close(fd)
        raise


def read_private_at(fd: int, name: str, *, limit: int = MAX_CONFIG) -> bytes:
    opened = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=fd)
    try:
        before = os.fstat(opened)
        private_file(before)
        if before.st_size > limit:
            raise ValueError("file_too_large")
        content = os.read(opened, limit + 1)
        after = os.fstat(opened)
        named = os.stat(name, dir_fd=fd, follow_symlinks=False)
        private_file(after)
        private_file(named)
        if (
            identity(before) != identity(named)
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or len(content) != after.st_size
            or len(content) > limit
        ):
            raise ValueError("file_changed")
        return content
    finally:
        os.close(opened)


def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def load_config(value: str) -> Settings:
    path = checked_path(value)
    fd, chain = directory(path.parent)
    try:
        body = read_private_at(fd, path.name)
        fresh, current = directory(path.parent)
        os.close(fresh)
        if chain != current:
            raise ValueError("config_directory_changed")
        return Settings.model_validate(json.loads(body, object_pairs_hook=unique_object))
    finally:
        os.close(fd)


def state_binding(config: Settings) -> str:
    return hashlib.sha256(
        json.dumps(
            [config.module, config.consumer, config.journal_namespace, config.storage_url]
        ).encode()
    ).hexdigest()


class State:
    """Pinned private directory and process lock; no business data in health.json."""

    def __init__(self, config: Settings):
        self.config = config
        self.path = checked_path(config.state_dir)
        self.fd, self.chain = directory(self.path, private=True)
        self.lock: int | None = None
        try:
            self.lock = os.open(
                "runtime.lock", os.O_RDWR | os.O_CREAT | FILE_FLAGS, 0o600, dir_fd=self.fd
            )
            private_file(os.fstat(self.lock))
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.validate()
            binding = state_binding(config)
            try:
                saved = read_private_at(self.fd, "identity.json")
            except FileNotFoundError:
                self.write("identity.json", {"binding": binding})
            else:
                if json.loads(saved) != {"binding": binding}:
                    raise ValueError("state_namespace_mismatch")
        except BaseException:
            self.close()
            raise

    def validate(self) -> None:
        fd, chain = directory(self.path, private=True)
        os.close(fd)
        if chain != self.chain:
            raise ValueError("state_directory_changed")
        assert self.lock is not None
        opened = os.fstat(self.lock)
        named = os.stat("runtime.lock", dir_fd=self.fd, follow_symlinks=False)
        private_file(opened)
        private_file(named)
        if identity(opened) != identity(named):
            raise ValueError("state_lock_changed")

    def write(self, name: str, data: dict[str, Any]) -> None:
        self.validate()
        with suppress(FileNotFoundError):
            private_file(os.stat(name, dir_fd=self.fd, follow_symlinks=False))
        temporary = ".metadata-" + os.urandom(12).hex()
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS, 0o600, dir_fd=self.fd
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                private_file(os.fstat(stream.fileno()))
                stream.write(json.dumps(data, sort_keys=True).encode())
                stream.flush()
                os.fsync(stream.fileno())
            self.validate()
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self.fd)

    def health(self, status: str) -> None:
        self.write(
            "health.json",
            {
                "schema": 1,
                "module": self.config.module,
                "scope": "worker_pump_only",
                "status": status,
                "pid": os.getpid(),
                "updated_at": time.time(),
            },
        )

    def close(self) -> None:
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        os.close(self.fd)


def healthy(config: Settings) -> bool:
    fd, chain = directory(checked_path(config.state_dir), private=True)
    try:
        if json.loads(read_private_at(fd, "identity.json", limit=2048)) != {
            "binding": state_binding(config)
        }:
            return False
        lock = os.open("runtime.lock", os.O_RDONLY | FILE_FLAGS, dir_fd=fd)
        try:
            private_file(os.fstat(lock))
            if identity(os.fstat(lock)) != identity(
                os.stat("runtime.lock", dir_fd=fd, follow_symlinks=False)
            ):
                return False
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass  # A live local worker must still hold the lock.
            else:
                return False
        finally:
            os.close(lock)
        data = json.loads(read_private_at(fd, "health.json", limit=2048))
        fresh, current = directory(checked_path(config.state_dir), private=True)
        os.close(fresh)
        if chain != current or set(data) != {
            "schema",
            "module",
            "scope",
            "status",
            "pid",
            "updated_at",
        }:
            return False
        if (
            data["schema"] != 1
            or data["module"] != config.module
            or data["scope"] != "worker_pump_only"
            or data["status"] not in {"ready", "busy"}
            or type(data["pid"]) is not int
            or data["pid"] <= 0
            or type(data["updated_at"]) not in {float, int}
            or not 0 <= time.time() - data["updated_at"] <= config.health_max_age_seconds
        ):
            return False
        os.kill(data["pid"], 0)
        return True
    finally:
        os.close(fd)


class PumpStorage(RemoteStorageClient):
    """Expose swallowed handler failures as generic pump failure, after SDK NACK."""

    def __init__(self, *args, stop: Event, state: State, **kwargs):
        self.stop, self.state = stop, state
        self.handler_failed = False
        super().__init__(*args, **kwargs)

    def subscribe(self, consumer, handler, *, event_types=None, from_beginning=False):
        if from_beginning or consumer != self.state.config.consumer:
            raise ValueError("unexpected_subscription")

        def guarded(event):
            try:
                if self.stop.is_set():
                    raise RuntimeError("worker_stopping")
                handler(event)
            except Exception:
                self.handler_failed = True
                raise

        super().subscribe(consumer, guarded, event_types=event_types, from_beginning=False)

    def is_processed(self, consumer: str, event_id: str) -> bool:
        try:
            return super().is_processed(consumer, event_id)
        except Exception:
            # The SDK also NACKs read failures before entering the handler.
            self.handler_failed = True
            raise

    def poll_once(self, consumer: str | None = None) -> int:
        if consumer != self.state.config.consumer:
            raise ValueError("explicit_consumer_required")
        if self.stop.is_set():
            return 0
        self.state.health("busy")
        self.handler_failed = False
        count = super().poll_once(consumer)
        if self.handler_failed and not self.stop.is_set():
            raise RuntimeError("worker_handler_failed")
        return count

    def poll_batch(self, consumer: str, *, max_deliveries: int) -> int:
        """Bound admission before a scheduler tick; never drain a live stream forever.

        Use the guarded poll path, not SDK run_until_idle (which bypasses this
        host's failure/stop checks). Idle or filtered progress yields to tick;
        an active stream still gets a tick after at most max_deliveries polls.
        """
        count = 0
        for _ in range(max_deliveries):
            if self.stop.is_set():
                break
            delivered = self.poll_once(consumer)
            count += delivered
            if not delivered:
                break
        return count


class BoundedExtraction(ExtractionService):
    """Host-only limits; existing process/schedule semantics are not reimplemented."""

    def __init__(self, *args, config: Settings, stop: Event, state: State, **kwargs):
        super().__init__(*args, **kwargs)
        self.config, self.stop, self.state = config, stop, state

    def runs(self) -> list[SystemRecord]:
        found = []
        cursor = None
        seen = set()
        while True:
            page = self.storage.query(Query("extraction_run", limit=100, cursor=cursor))
            found.extend(item for item in page.items if isinstance(item, SystemRecord))
            if len(found) > self.config.max_records:
                raise RuntimeError("worker_record_limit")
            cursor = page.next_cursor
            if cursor is None:
                return found
            if cursor in seen or len(seen) >= self.config.max_records:
                raise RuntimeError("worker_page_limit")
            seen.add(cursor)

    def schedule(self, interval):
        if self.stop.is_set():
            return []
        return super().schedule(interval)

    def drain(self) -> None:
        if self.stop.is_set():
            return
        processed = 0
        for run in self.runs():
            if self.stop.is_set():
                break
            status = run.payload["status"]
            deadline = run.payload.get("lease_until" if status == "running" else "retry_at")
            if status in TERMINAL or (deadline and datetime.fromisoformat(deadline) > self.clock()):
                continue
            self.state.health("busy")
            self.process(run.id)
            processed += 1
            if processed >= self.config.max_jobs_per_tick:
                break
        self.state.health("ready")


@contextmanager
def stopping(stop: Event) -> Iterator[None]:
    def request_stop(_signum, _frame):
        stop.set()

    previous = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, request_stop)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run(config: Settings, stop: Event) -> None:
    if stop.is_set():
        return
    with ExitStack() as stack:
        state = State(config)
        stack.callback(state.close)
        state.health("starting")
        try:
            client = PumpStorage(
                config.storage_token.get_secret_value(),
                config.storage_url,
                module=config.module,
                timeout=config.timeout,
                transport=httpx.HTTPTransport(retries=0, trust_env=False),
                stop=stop,
                state=state,
            )
            stack.callback(client.close)
            if stop.is_set():
                state.health("stopped")
                return
            namespace = json.dumps(
                [config.journal_namespace, config.module, config.consumer, config.storage_url]
            )
            if config.module == "extraction":
                from ear_llm_gateway.client import GatewayClient

                assert config.gateway_url is not None and config.model is not None
                gateway = GatewayClient(
                    config.gateway_url,
                    config.storage_token.get_secret_value(),
                    model=config.model,
                    system=SYSTEM,
                    timeout=config.timeout,
                    transport=httpx.HTTPTransport(retries=0, trust_env=False),
                )
                stack.callback(gateway.close)
                service = BoundedExtraction(client, gateway, config=config, stop=stop, state=state)
                serve(
                    service,
                    stop,
                    schedule=config.schedule.schedule() if config.schedule else None,
                    poll_seconds=config.poll_seconds,
                )
            else:
                if config.module == "attribution":
                    from ear_attribution.matching import MatchConfig
                    from ear_attribution.service import AttributionService
                    from ear_attribution.transactions import CommitJournal

                    journal = CommitJournal(state.path / "journal", namespace=namespace)
                    stack.callback(journal.close)
                    assert (
                        config.cosine_threshold is not None and config.gps_threshold_m is not None
                    )
                    worker = AttributionService.business(
                        client,
                        config=MatchConfig(config.cosine_threshold, config.gps_threshold_m),
                        journal=journal,
                    )
                    worker.subscribe(from_beginning=False)  # recovery MUST precede registration

                    def advance():
                        return None
                else:
                    from ear_dependency.journal import CommitJournal as DependencyJournal
                    from ear_dependency.remote import RemoteDependencyService

                    dependency_journal = DependencyJournal(
                        state.path / "journal", namespace=namespace
                    )
                    stack.callback(dependency_journal.close)
                    scheduler = RemoteDependencyService(
                        client,
                        journal=dependency_journal,
                        consumer=config.consumer,
                        max_records=config.max_records,
                    )
                    scheduler.subscribe()  # authority + recovery, never a visibility override
                    advance = scheduler.tick
                while not stop.is_set():
                    if config.module == "dependency":
                        client.poll_batch(
                            config.consumer, max_deliveries=config.max_events_per_tick
                        )
                    else:
                        client.poll_once(config.consumer)
                    if not stop.is_set():
                        advance()
                        state.health("ready")
                    stop.wait(config.poll_seconds)
            state.health("stopped")
        except BaseException:
            state.health("failed")
            raise


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown argument values (potentially credentials).
        raise ValueError("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True, help="absolute path to owned 0600 JSON")
    parser.add_argument(
        "--healthcheck", action="store_true", help="local metadata only, no network"
    )
    old_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        args = parser.parse_args(argv)
        config = load_config(args.config)
        if args.healthcheck:
            return 0 if healthy(config) else 1
        stop = Event()
        with stopping(stop):
            run(config, stop)
        return 0
    except Exception:
        print(ERROR_METADATA, file=sys.stderr)
        return 1
    finally:
        logging.disable(old_logging)


if __name__ == "__main__":
    raise SystemExit(main())
