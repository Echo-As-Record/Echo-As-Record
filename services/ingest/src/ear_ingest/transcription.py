"""Authenticated same-origin route to the optional local transcription worker."""

import hmac
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from ear_ingest.pairing import Pairing

MAX_BYTES = 64000


def transcription_router(
    token: str,
    upstream: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    pairing: Pairing | None = None,
) -> APIRouter:
    parsed = urlsplit(upstream)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ValueError("EAR_TRANSCRIPTION_URL requires an HTTP loopback origin with port")
    base = upstream.rstrip("/")
    router = APIRouter(prefix="/api/v1/transcription")

    def validate(request: Request, stream_id: str) -> None:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        matched = hmac.compare_digest(token.encode(), value.encode())
        if not matched and pairing:
            paired = pairing.credential(request.headers.get("x-device-id", "").lower())
            matched = bool(paired) and hmac.compare_digest(value.encode(), (paired or "").encode())
        if scheme.lower() != "bearer" or not matched:
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", stream_id):
            raise HTTPException(400, "Invalid stream id")

    async def forward(method: str, path: str, body: bytes = b"", final: str = "0") -> Response:
        headers = {"Authorization": f"Bearer {token}"}
        if method == "PUT":
            headers.update({"Content-Type": "application/octet-stream", "X-Ear-Final": final})
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, transport=transport, timeout=30
        ) as client:
            try:
                reply = await client.request(method, base + path, headers=headers, content=body)
            except httpx.RequestError as exc:
                raise HTTPException(503, "Transcription worker unavailable") from exc
        if reply.is_redirect or reply.status_code >= 500:
            raise HTTPException(502, "Transcription worker error")
        return Response(
            reply.content,
            reply.status_code,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/{stream_id}")
    async def read(
        request: Request, stream_id: str, limit: int | None = Query(None, ge=1, le=1000)
    ):
        validate(request, stream_id)
        suffix = "" if limit is None else f"?limit={limit}"
        return await forward("GET", f"/api/v1/transcription/{stream_id}{suffix}")

    @router.put("/{stream_id}/chunks/{start_sample}")
    async def put(request: Request, stream_id: str, start_sample: int):
        validate(request, stream_id)
        if not 0 <= start_sample <= 2**63 - 1 - MAX_BYTES:
            raise HTTPException(400, "Invalid sample offset")
        if (
            request.headers.get("content-type", "").split(";")[0].lower()
            != "application/octet-stream"
        ):
            raise HTTPException(415, "Expected application/octet-stream")
        final = request.headers.get("x-ear-final", "0")
        if final not in {"0", "1"}:
            raise HTTPException(400, "Invalid X-Ear-Final")
        body = bytearray()
        async for packet in request.stream():
            if len(packet) + len(body) > MAX_BYTES:
                raise HTTPException(413, "At most 64000 bytes")
            body.extend(packet)
        if len(body) % 2 or (not body and final != "1"):
            raise HTTPException(400, "Invalid PCM16 frame length")
        return await forward(
            "PUT", f"/api/v1/transcription/{stream_id}/chunks/{start_sample}", bytes(body), final
        )

    return router
