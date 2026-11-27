"""Ciphertext-only HTTP ingress for a paired Mac. The inner requests use the ingest API."""

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from typing import BinaryIO

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request, Response

from ear_ingest.pairing import Pairing

CHUNK = 65536
LABEL = b"ear-relay-v1/"


def derive(secret: str, direction: str, ticket: str) -> bytes:
    return hmac.digest(
        secret.encode(), LABEL + direction.encode() + b"/" + ticket.encode(), "sha256"
    )


class _UploadBudget:
    """Classify from authenticated metadata before spooling a native recording body."""

    def __init__(self, limit: int):
        self.limit = limit
        self.prefix: bytearray | None = bytearray()
        self.upload = False
        self.body_bytes = 0

    def observe(self, frame: bytes, wire_bytes: int) -> None:
        if self.prefix is not None:
            head, newline, rest = frame.partition(b"\n")
            if len(self.prefix) + len(head) >= 8192:
                raise ValueError
            self.prefix.extend(head)
            if not newline:
                return
            meta = json.loads(self.prefix)
            if not isinstance(meta, dict):
                raise ValueError
            self.upload = (meta.get("method"), meta.get("path")) == ("POST", "/api/v1/record")
            self.prefix = None
            frame = rest
        if self.upload:
            self.body_bytes += len(frame)
            # Separately bound framing overhead (one authenticated tag per frame).
            # EarRecorder sends up to 64 KiB per frame.
            if self.body_bytes > self.limit or wire_bytes > 2 * self.limit + 16384:
                raise HTTPException(413, "Native recording request too large")


async def decrypt(
    request: Request, out: BinaryIO, key: bytes, *, upload_limit: int | None = None
) -> None:
    cipher = AESGCM(key)
    buf = bytearray()
    index = 0
    wire_bytes = 0
    final = False
    budget = _UploadBudget(upload_limit) if upload_limit is not None else None
    async for part in request.stream():
        # ASGI chunk boundaries are independent of relay frames. Avoid duplicating
        # an arbitrarily large chunk in the frame buffer.
        view = memoryview(part)
        for offset in range(0, len(view), CHUNK):
            buf.extend(view[offset : offset + CHUNK])
            while len(buf) >= 4:
                size = int.from_bytes(buf[:4], "big")
                if size > CHUNK or final:
                    raise ValueError
                if len(buf) < 20 + size:
                    break
                frame = cipher.decrypt(index.to_bytes(12, "big"), bytes(buf[4 : size + 20]), None)
                wire_bytes += 20 + size
                if budget is not None:
                    budget.observe(frame, wire_bytes)
                out.write(frame)
                del buf[: size + 20]
                index += 1
                final = size == 0
    if buf or not final:
        raise ValueError
    out.seek(0)


def create_relay(pairing: Pairing, origin: FastAPI) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        async with origin.router.lifespan_context(origin):
            yield

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/health")
    def health(device: str, challenge: str):
        secret = pairing.credential(device)
        if not secret or not re.fullmatch("[0-9a-f]{32}", challenge):
            raise HTTPException(403)
        return {
            "proof": hmac.digest(
                secret.encode(), ("ear-health-v1:" + challenge).encode(), "sha256"
            ).hex()
        }

    @app.post("/api/v1/relay")
    async def relay(request: Request):
        device = request.headers.get("x-device-id", "")
        ticket = request.headers.get("x-ear-request", "")
        invitation = request.headers.get("x-ear-invite-id")
        if not re.fullmatch("[0-9a-f]{32}", ticket):
            raise HTTPException(400)
        if invitation:
            with closing(pairing.connect()) as db:
                secrets = db.execute("SELECT secret FROM invites WHERE expires>=?", (time.time(),))
                secret = next(
                    (s for (s,) in secrets if hashlib.sha256(s.encode()).hexdigest() == invitation),
                    None,
                )
        else:
            secret = pairing.credential(device)
        if not secret:
            raise HTTPException(403)
        with tempfile.TemporaryFile() as plain:
            try:
                await decrypt(
                    request,
                    plain,
                    derive(secret, "request", ticket),
                    upload_limit=getattr(origin.state, "native_upload_limit", None),
                )
                meta = json.loads(plain.readline(8192))
                if meta["device"] != device:
                    raise ValueError
                path, method = meta["path"], meta["method"]
                if invitation:
                    if (path, method) != ("/api/v1/pair", "POST"):
                        raise ValueError
                    headers = {"X-Ear-Invitation": secret}
                else:
                    if not (
                        (path, method) == ("/api/v1/record", "POST")
                        or (
                            method == "GET"
                            and re.fullmatch(r"/api/v1/transcription/[\w-]+\?limit=1", path)
                        )
                        or (
                            method == "PUT"
                            and re.fullmatch(r"/api/v1/transcription/[\w-]+/chunks/[0-9]+", path)
                        )
                    ):
                        raise ValueError
                    headers = {"Authorization": "Bearer " + secret}
                headers["X-Device-Id"] = device
                for name in ("Content-Type", "X-User-Id", "X-Ear-Final"):
                    if name in meta:
                        headers[name] = meta[name]
            except (InvalidTag, ValueError, KeyError, TypeError, UnicodeError):
                raise HTTPException(400, "Invalid envelope") from None

            async def body() -> AsyncIterator[bytes]:
                while data := plain.read(CHUNK):
                    yield data

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=origin), base_url="http://local"
            ) as client:
                result = await client.request(method, path, headers=headers, content=body())
            key = derive(secret, "response", ticket)
            nonce = os.urandom(12)
            status = str(result.status_code).encode()
            encrypted = nonce + AESGCM(key).encrypt(nonce, result.content, status)
            receipt = hmac.digest(derive(secret, "receipt", ticket), status, "sha256").hex()
            return Response(
                encrypted,
                result.status_code,
                headers={"X-Ear-Receipt": receipt, "Cache-Control": "no-store"},
            )

    return app
