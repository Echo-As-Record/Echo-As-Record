import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from ear_api.app import create_app
from ear_contracts import new_id
from ear_storage_client import AppendRaw, Context, Create
from ear_storage_client.testing import make_raw
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from .conftest import ADMIN, headers, tcp_server

START = "2026-11-05T09:00:00Z"
END = "2026-11-05T09:01:00Z"


def test_stdio_workflow(gateway):
    http, fake, config = gateway
    source = fake.storage.client("adapter")
    voice, audio, old, new = (new_id() for _ in range(4))
    source.write(
        [
            AppendRaw(make_raw("voiceprint", id=voice, start=START)),
            AppendRaw(
                make_raw(
                    "audio_ref",
                    {"file_ref": {"path": "synthetic.m4a"}},
                    id=audio,
                    start=START,
                    end=END,
                )
            ),
            *[
                AppendRaw(
                    make_raw(
                        "transcript_segment",
                        {
                            "text": "合成句子",
                            "audio_ref_id": audio,
                            "voiceprint_id": voice,
                        },
                        start="2026-11-05T09:00:12Z",
                        end="2026-11-05T09:00:15Z",
                    )
                )
                for _ in range(2)
            ],
        ],
        Context.system(),
    ).check()
    fake.storage.client("attribution").write(
        [
            Create("person", old, {"name": "甲"}),
            Create("person", new, {"name": "乙"}),
            Create(
                "attribution",
                new_id(),
                {"subject_id": voice, "target_type": "person", "target_id": old},
            ),
        ],
        Context.system(),
    ).check()
    assert http.get("/v1/evidence").status_code == 401
    assert http.get("/v1/persons", headers=headers(), params={"name": "乙"}).status_code == 403

    async def workflow(url):
        params = StdioServerParameters(
            command=str(Path(sys.executable).with_name("ear-mcp")),
            env={"EAR_API_URL": url, "EAR_API_TOKEN": ADMIN},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == {
                "find_evidence",
                "find_person",
                "new_request_id",
                "correct_attribution",
                "feedback_status",
            }

            async def call(tool, **args) -> Any:
                result = await session.call_tool(tool, args)
                assert not result.isError and isinstance(result.content[0], TextContent), result
                body = result.content[0].text
                return body if tool == "new_request_id" else json.loads(body)

            query = dict(start=START, end=END, phrase="句子", limit=1)
            first = await call("find_evidence", **query)
            second = await call("find_evidence", **query, cursor=first["next_cursor"])
            row = first["items"][0]
            assert second["next_cursor"] is None
            assert row["transcript_segment_id"] != second["items"][0]["transcript_segment_id"]
            assert row["playback"] == {
                "file_ref": {"path": "synthetic.m4a"},
                "start_seconds": 12,
                "end_seconds": 15,
            }
            person = (await call("find_person", name="乙"))["items"][0]
            command = dict(
                request_id=await call("new_request_id"),
                voiceprint_id=row["voiceprint_id"],
                previous_target_id=row["attribution"]["person_id"],
                new_target_id=person["id"],
            )
            assert (command["voiceprint_id"], command["previous_target_id"], person["id"]) == (
                voice,
                old,
                new,
            )
            accepted = await call("correct_attribution", **command)
            assert accepted == await call("correct_attribution", **command)
            status = await call("feedback_status", feedback_id=accepted["feedback_id"])
            assert status["status"] == "pending" and status["integration"] == "pending_contract"

    with tcp_server(create_app(config)) as url:
        asyncio.run(workflow(url))
    assert len(fake.receipts) == 1


def test_project_command_lists_tools_without_dotenv(monkeypatch):
    """The exact checked-in command starts without deploy/.env or API credentials."""
    import os

    root = Path(__file__).resolve().parents[3]
    definition = json.loads((root / ".mcp.json").read_text())["mcpServers"]["ear"]
    assert "--env-file" not in definition["args"]
    monkeypatch.delenv("EAR_API_TOKEN", raising=False)
    monkeypatch.delenv("EAR_API_URL", raising=False)

    async def handshake():
        params = StdioServerParameters(
            command=definition["command"],
            args=definition["args"],
            cwd=str(root),
            env={
                key: os.environ[key] for key in ("UV_CACHE_DIR", "UV_OFFLINE") if key in os.environ
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert {tool.name for tool in (await session.list_tools()).tools} == {
                "find_evidence",
                "find_person",
                "new_request_id",
                "correct_attribution",
                "feedback_status",
            }
            result = await session.call_tool("new_request_id", {})
            assert not result.isError

    asyncio.run(handshake())
