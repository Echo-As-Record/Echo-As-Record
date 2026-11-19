"""HTTP boundary; credentials are checked before parsing multipart bodies."""

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ear_ingest.delivery import Delivery
from ear_ingest.pairing import Pairing
from ear_ingest.record import component, parse
from ear_ingest.store import persist

logger = logging.getLogger("ear_ingest")


async def notify(url: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(url)
            response.raise_for_status()
    except Exception as exc:
        logger.warning(
            json.dumps(
                {
                    "module": "ingest",
                    "event_id": "notify_failed",
                    "root_feedback_id": "system",
                    "depth": 0,
                    "error_type": type(exc).__name__,
                }
            )
        )


def create_app(pairing: Pairing | None = None) -> FastAPI:
    token = os.environ.get("EAR_INGEST_TOKEN", "")
    if not token.strip():
        raise RuntimeError("EAR_INGEST_TOKEN must be configured")
    root = Path(os.environ.get("EAR_INGEST_INBOX_ROOT", "~/.local/share/ear/inbox/brain"))
    notify_url = os.environ.get("EAR_INGEST_NOTIFY_URL", "")
    event_url = os.environ.get("EAR_INGEST_EVENT_URL", "")
    if event_url and notify_url:
        raise RuntimeError("Configure only one of EAR_INGEST_EVENT_URL and EAR_INGEST_NOTIFY_URL")
    for key, url in (("EAR_INGEST_EVENT_URL", event_url), ("EAR_INGEST_NOTIFY_URL", notify_url)):
        if not url:
            continue
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise RuntimeError(f"{key} must be an HTTP(S) URL without credentials")
    delivery = Delivery(root) if event_url else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if delivery is None:
            yield
            return
        await asyncio.to_thread(delivery.recover)
        stop = asyncio.Event()
        worker = asyncio.create_task(delivery.run(event_url, stop))
        try:
            yield
        finally:
            stop.set()
            await worker

    app = FastAPI(lifespan=lifespan)
    if upstream := os.environ.get("EAR_TRANSCRIPTION_URL"):
        from ear_ingest.transcription import transcription_router

        app.include_router(transcription_router(token, upstream, pairing=pairing))

    if pairing:

        @app.post("/api/v1/pair")
        def pair(request: Request):
            try:
                device = component(request.headers.get("x-device-id", "")).lower()
            except ValueError:
                raise HTTPException(400, "Invalid device") from None
            token = pairing.claim(request.headers.get("x-ear-invitation", ""), device)
            if token is None:
                raise HTTPException(403, "Invalid or expired invitation")
            return {"receiver": pairing.id, "token": token}

    @app.get("/health")
    def health(challenge: str = "", device: str = "") -> dict:
        if not challenge:
            return {"ok": True}
        if len(challenge) != 32 or any(c not in "0123456789abcdef" for c in challenge):
            raise HTTPException(400, "Invalid challenge")
        secret = pairing.credential(device) if device and pairing else token
        if not secret or (device and not pairing):
            raise HTTPException(403, "Unknown device")
        proof = hmac.digest(secret.encode(), ("ear-health-v1:" + challenge).encode(), "sha256")
        return {"ok": True, "proof": proof.hex()}

    @app.post("/api/v1/record")
    @app.post("/api/v1/instances/{instance}/service/api/v1/record")
    async def record(request: Request, background_tasks: BackgroundTasks) -> dict:
        auth = request.headers.get("authorization", "")
        scheme, _, supplied = auth.partition(" ")
        matched = hmac.compare_digest(supplied.encode(), token.encode())
        if not matched and pairing:
            paired = pairing.credential(request.headers.get("x-device-id", "").lower())
            matched = bool(paired) and hmac.compare_digest(
                supplied.encode(), (paired or "").encode()
            )
        if scheme.lower() != "bearer" or not matched:
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        try:
            device = component(request.headers.get("x-device-id", "")).lower()
            legacy = request.path_params.get("instance")
            if legacy is not None and legacy.lower() != f"brain___{device}":
                raise ValueError("Legacy instance must match X-Device-Id")
            if (
                not request.headers.get("content-type", "")
                .lower()
                .startswith("multipart/form-data")
            ):
                raise ValueError("Expected multipart/form-data")
            async with request.form(max_files=1, max_fields=12) as form:
                payload, upload = parse(form)
                if user := request.headers.get("x-user-id"):
                    payload["user_id"] = user
                # A cancelled request must not close UploadFile while its worker is copying.
                work = asyncio.create_task(
                    run_in_threadpool(persist, root, device, payload, upload, delivery)
                )
                try:
                    result = await asyncio.shield(work)
                except asyncio.CancelledError:
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if not work.cancelled():
                        work.exception()
                    raise
        except (ValueError, OverflowError) as exc:
            raise HTTPException(400, str(exc)) from None
        if notify_url:
            background_tasks.add_task(notify, notify_url)
        return result

    return app
