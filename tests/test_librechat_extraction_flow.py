"""LibreChat adapter -> ① -> extraction, end to end with synthetic frames and a fake model.

Drift guard between the two sides of the chat contract: the records here are whatever the
real adapter writes (revisions, file refs, extras and all), validated against the shared
JSON Schema, then read by the opt-in extraction flow. No network, files outside tmp, or
real conversation data.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from ear_contracts import RawRecord, new_id
from ear_contracts.optional_sources import chat_specs
from ear_extraction.chat_evidence import ASSISTANT_CLAIM, USER_PROVIDED
from ear_extraction.demo import fixture_registry, submit
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import ExtractionService
from ear_extraction.persistence import records
from ear_llm_gateway import CallRequest, GatewayClient
from ear_storage_client import Context, Create, MemoryStorage, Query
from ear_storage_client.testing import ts
from jsonschema import Draft202012Validator
from librechat_adapter import Ingestor, PrivateVolume
from librechat_adapter.testing import attachment, blob, end, header, join, line, message

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
START = ts("2026-11-05T09:00:00Z")
SCOPE = Interval(start=START, end=START + timedelta(hours=1))


def stamp(minutes: int) -> str:
    return (
        (START + timedelta(minutes=minutes))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def stream(first_text: str) -> list[str]:
    conversation = line(
        t="conversation",
        conversation_id="c1",
        title="合成会话",
        endpoint="agents",
        model="SECRET-MODEL",
        agent_id="SECRET-AGENT",
        tags=["SECRET-TAG"],
        created_at=stamp(0),
        updated_at=stamp(12),
    )
    when = {"created_at": stamp(0), "updated_at": stamp(0)}
    return join(
        [
            header(),
            conversation,
            message("m1", "c1", first_text, **when),
            blob(1, b"alpha notes\nline two\n"),
            attachment(
                "f1",
                mid="m1",
                blob_id=1,
                filename="SECRET-NAME.txt",
                message_created_at=stamp(0),
            ),
            message(
                "m2",
                "c1",
                "已经整理好并保存了周报。",
                role="assistant",
                parent_message_id="m1",
                sender="SECRET-SENDER",
                model="SECRET-MODEL",
                reasoning="SECRET-REASONING",
                tool_calls=[{"name": "SECRET-TOOL", "args": "{}", "output": "SECRET-OUT"}],
                created_at=stamp(3),
                updated_at=stamp(3),
            ),
            blob(2, b"generated report body"),
            attachment(
                "f2",
                mid="m2",
                blob_id=2,
                ref_kind="generated",
                filename="SECRET-REPORT.txt",
                message_created_at=stamp(3),
            ),
            message(
                "m3",
                "c1",
                "谢谢。",
                parent_message_id="m2",
                created_at=stamp(12),
                updated_at=stamp(12),
            ),
            end(),
        ]
    )


class Model:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def complete(self, call: Any) -> dict[str, Any]:
        self.calls.append(call)
        rows = call.data.get("evidence", [])
        if call.purpose == "segmentation":
            last = max(ts(r["event_end"]) for r in rows) + timedelta(seconds=1)
            return {
                "activities": [
                    {
                        "start": rows[0]["event_start"],
                        "end": last.isoformat().replace("+00:00", "Z"),
                        "title_md": "Synthetic activity",
                        "evidence_ids": [r["id"] for r in rows],
                    }
                ]
            }
        if call.purpose == "selection":
            return {"rule_ids": [r["id"] for r in call.data["rules"]]}
        user = next(r for r in rows if r["evidence_status"] == USER_PROVIDED)
        return {"body_md": "- synthetic note", "evidence_ids": [user["id"]]}


def test_adapter_records_satisfy_the_contract_and_drive_the_chat_flow(tmp_path: Path) -> None:
    storage = MemoryStorage(fixture_registry().extend_core(*chat_specs()))
    root = tmp_path / "volume"
    root.mkdir()
    root.chmod(0o700)
    volume = PrivateVolume(root)
    ingest = Ingestor(storage.client("adapter"), volume)
    assert ingest.ingest(stream("原始需求：整理周报")).complete
    assert ingest.ingest(stream("修改后需求：整理周报并发送给老板")).complete  # edit -> r2

    reader = storage.client("extraction")
    written = {
        kind: [r for r in reader.query(Query(kind, limit=100)).items if isinstance(r, RawRecord)]
        for kind in ("chat_session", "chat_message", "chat_attachment")
    }
    assert [len(v) for v in written.values()] == [1, 4, 2]  # m1 has r1 and r2

    # The shared contract accepts exactly what the adapter writes.
    schema = json.loads((CONTRACTS / "records/raw-record.schema.json").read_text("utf-8"))
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    for record in (r for rows in written.values() for r in rows):
        assert record.event_start == record.event_end
        errors = [e.message for e in validator.iter_errors(record.model_dump(mode="json"))]
        assert errors == [], (record.data_type, errors)

    model = Model()
    seen: list[CallRequest] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = CallRequest.model_validate_json(request.content)
        seen.append(body)
        out = model.complete(SimpleNamespace(purpose=body.purpose, data=body.data))
        return httpx.Response(
            200, json={"call_id": body.call_id, "record_saved": True, "output": out}
        )

    with GatewayClient(
        "https://module-seven.invalid",
        "t" * 43,
        system=SYSTEM,
        transport=httpx.MockTransport(respond),
    ) as gateway:
        client = storage.client("extraction")
        service = ExtractionService(client, gateway, clock=lambda: SCOPE.end, chat_evidence=True)
        service.subscribe()
        client.write(
            [Create("rule", new_id(), Rule(scope="s", instruction="notes").model_dump())],
            Context.system(),
        ).check()
        run_id = submit(storage, Command(kind="extract", interval=SCOPE))
        service.drain()
        run = client.get_system_record(run_id)
    assert run is not None and run.payload["status"] == "succeeded", run and run.payload
    assert [c.purpose for c in seen] == ["segmentation", "selection", "generation"]
    assert run.payload["detail"]["chat_evidence"] == {
        "messages": 3,
        "attachments": 2,
        "sessions": 1,
        "other_rows": 0,
    }

    wire = " ".join(c.model_dump_json() for c in seen)
    assert "修改后需求" in wire and "原始需求" not in wire  # highest revision only
    for leaked in ("SECRET", "librechat/blobs", "file_ref", "reasoning", "tool_calls"):
        assert leaked not in wire, leaked
    rows = {r["id"]: r for r in seen[0].data["evidence"]}
    latest = {r.id for r in written["chat_message"] if r.payload["revision"] == 2}
    assert latest <= set(rows)
    by_kind = {
        (r["data_type"], r["payload"].get("file_id") or r["payload"].get("role")): r[
            "evidence_status"
        ]
        for r in rows.values()
        if r["data_type"] != "chat_session"
    }
    assert by_kind[("chat_message", "assistant")] == ASSISTANT_CLAIM
    assert by_kind[("chat_attachment", "f1")] == USER_PROVIDED  # the user's upload
    assert by_kind[("chat_attachment", "f2")] == ASSISTANT_CLAIM  # generated by the assistant
    assert "alpha notes" in wire  # the decoded text of the user's file, not its bytes or path
    [activity] = records(client, "activity")
    assert set(activity.payload["evidence_ids"]) == set(rows)
