"""Ear's stdio MCP client for the authenticated module ⑥ HTTP API."""

import os
import re
from typing import Annotated, Any

import httpx
from ear_contracts import new_id
from ear_contracts.base import Id
from mcp.server.fastmcp import FastMCP
from pydantic import Field


def server(client: httpx.Client) -> FastMCP:
    mcp = FastMCP("Ear", log_level="WARNING")

    def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if "Authorization" not in client.headers:
            raise ValueError("Set EAR_API_TOKEN to the caller token for module ⑥, then reconnect.")
        try:
            response = client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ValueError(
                "Ear API unreachable. Replay writes with the same request_id."
            ) from None
        if response.status_code != 200 and response.status_code != 202:
            # A proxy/server error page may contain private data.
            raise ValueError(f"Ear API returned HTTP {response.status_code}")
        return response.json()

    @mcp.tool()
    def find_evidence(
        start: str,
        end: str,
        phrase: Annotated[str, Field(max_length=256)] = "",
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Find transcript excerpts by UTC [start,end) ending in Z and literal phrase.
        Follow next_cursor even on empty pages. Playback offsets are relative to the audio file.
        A voiceprint may span multiple excerpts. Missing attribution cannot be corrected here.
        """
        params = {"start": start, "end": end, "phrase": phrase, "limit": str(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        return request("GET", "/v1/evidence", params=params)

    @mcp.tool()
    def find_person(name: str, cursor: str | None = None) -> dict[str, Any]:
        """Find current people by exact name. Resolve duplicate names before correcting."""
        params = {"name": name}
        if cursor is not None:
            params["cursor"] = cursor
        return request("GET", "/v1/persons", params=params)

    @mcp.tool()
    def new_request_id() -> str:
        """Allocate a UUIDv7 once before a new correction. Retain it across retries."""
        return new_id()

    @mcp.tool()
    def correct_attribution(
        request_id: Id, voiceprint_id: Id, previous_target_id: Id, new_target_id: Id
    ) -> dict[str, Any]:
        """Submit the user's confirmed speaker correction. Retry with identical IDs.
        Use the voiceprint and current person from find_evidence. Accepted means recorded.
        """
        return request(
            "POST",
            "/v1/feedback",
            json={
                "module": "attribution",
                "request_id": request_id,
                "command": {
                    "kind": "correct_attribution",
                    "subject_id": voiceprint_id,
                    "previous_target_id": previous_target_id,
                    "new_target_id": new_target_id,
                },
            },
        )

    @mcp.tool()
    def feedback_status(feedback_id: Id) -> dict[str, Any]:
        """Read processing status. Pending means no attribution result has been recorded yet."""
        return request("GET", f"/v1/feedback/{feedback_id}/status")

    return mcp


def main() -> None:
    token = os.getenv("EAR_API_TOKEN", "")
    if token and not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise SystemExit("Set EAR_API_TOKEN to a module ⑥ API bearer.")
    url = httpx.URL(os.getenv("EAR_API_URL", "http://127.0.0.1:8086"))
    if (
        not url.host
        or url.scheme not in {"https", "http"}
        or url.userinfo
        or url.raw_path != b"/"
        or url.fragment
        or (url.scheme == "http" and url.host not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise SystemExit("Set EAR_API_URL to an HTTPS origin or a loopback HTTP origin.")
    with httpx.Client(
        base_url=url,
        headers={"Authorization": "Bearer " + token} if token else {},
        trust_env=False,
        timeout=30,
    ) as client:
        server(client).run(transport="stdio")
