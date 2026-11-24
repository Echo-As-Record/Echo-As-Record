"""Offline extraction through the direct-provider wire format and real gateway."""

from __future__ import annotations

import json
from contextlib import closing, nullcontext
from pathlib import Path

import httpx
from ear_contracts import LLMCallRecord, LLMUsage
from ear_extraction.demo import fixture, submit
from ear_extraction.domain import Command, Interval, Rule
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import ExtractionService
from ear_extraction.persistence import records
from ear_llm_gateway import CallRequest, Gateway, GatewayClient
from ear_llm_gateway.http import create_app
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.openai_compatible import OpenAICompatibleProvider
from ear_storage_client import AuthenticationError, Query
from ear_storage_client.testing import ts
from fastapi.testclient import TestClient


def test_newapi_extraction_persists_linked_markdown_without_action(tmp_path: Path):
    store, ids = fixture()
    extraction = store.client("extraction")
    audit_writer = store.client("llm_gateway")
    caller_token, provider_token = "t" * 43, "synthetic-newapi-token"
    model = "synthetic/notes-model"
    interval = Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))
    markdown = "## 合成组会\n\n- 合成人物甲整理实验结果。"
    usage = LLMUsage(input_tokens=21, output_tokens=9, total_tokens=30)
    calls: list[CallRequest] = []
    replies: list[str] = []
    rule_id: str | None = None

    def caller(token: str):
        if token != caller_token:
            raise AuthenticationError("unexpected synthetic caller")
        return nullcontext(extraction)

    def newapi(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {provider_token}"
        wire = json.loads(request.content)
        assert wire["model"] == model and wire["stream"] is False
        assert wire["response_format"] == {"type": "json_object"}
        assert wire["messages"][0] == {"role": "system", "content": SYSTEM}
        prompt = json.loads(wire["messages"][-1]["content"])
        data, schema = prompt["data"], prompt["output_schema"]
        stage = schema["title"]
        replies.append(stage)
        if stage == "Segmentation":
            assert set(data) == {"interval", "evidence"}
            assert [raw["id"] for raw in data["evidence"]] == [ids["transcript"]]
            output = {
                "activities": [
                    {
                        **data["interval"],
                        "title_md": "合成组会",
                        "evidence_ids": [ids["transcript"]],
                    }
                ]
            }
        elif stage == "Selection":
            assert rule_id is not None and [rule["id"] for rule in data["rules"]] == [rule_id]
            output = {"rule_ids": [rule_id]}
        else:
            assert stage == "Generated" and data["rule"]["id"] == rule_id
            assert data["entities"] == [
                {"id": ids["person"], "data_type": "person", "payload": {"name": "合成人物甲"}}
            ]
            output = {"body_md": markdown, "evidence_ids": [ids["transcript"]]}
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(
                json.dumps(
                    {
                        "id": f"chatcmpl-synthetic-{stage}",
                        "object": "chat.completion",
                        "created": 1,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(output, ensure_ascii=False),
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30},
                    }
                ).encode()
            ),
        )

    with closing(Journal(str(tmp_path / "gateway.sqlite3"))) as journal:
        gateway = Gateway(
            callers=caller,
            writer=audit_writer,
            journal=journal,
            aliases={"default": model},
            provider=OpenAICompatibleProvider(
                provider_token, transport=httpx.MockTransport(newapi)
            ),
        )
        with TestClient(create_app(gateway)) as gateway_http:

            def dispatch(request: httpx.Request) -> httpx.Response:
                assert request.method == "POST" and request.url.path == "/v1/complete"
                call = CallRequest.model_validate_json(request.content)
                calls.append(call)
                assert call.run_id is not None
                pending = extraction.get_system_record(call.run_id)
                assert pending is not None
                intent = pending.payload["gateway_calls"][-1]
                assert (intent["call_id"], intent["state"]) == (call.call_id, "sending")
                return gateway_http.post(
                    request.url.path,
                    headers={
                        "authorization": request.headers["authorization"],
                        "content-type": "application/json",
                    },
                    content=request.content,
                )

            with GatewayClient(
                "http://testserver",
                caller_token,
                system=SYSTEM,
                transport=httpx.MockTransport(dispatch),
            ) as client:
                service = ExtractionService(extraction, client)
                service.subscribe()
                submit(
                    store,
                    Command(
                        kind="rule_create",
                        rule=Rule(scope="Synthetic meeting", instruction="Write a Markdown note"),
                    ),
                )
                service.drain()
                [rule] = records(extraction, "rule")
                rule_id = rule.id
                run_id = submit(store, Command(kind="extract", interval=interval))
                service.drain()

    run = extraction.get_system_record(run_id)
    assert run is not None and run.payload["status"] == "succeeded", run
    [activity] = records(extraction, "activity")
    [selection] = records(extraction, "rule_selection")
    [artifact] = records(extraction, "artifact")
    assert activity.payload["title_md"] == "合成组会"
    assert activity.payload["evidence_ids"] == [ids["transcript"]]
    assert selection.payload == {"activity_id": activity.id, "rule_ids": [rule.id]}
    assert artifact.payload["activity_id"] == activity.id
    assert (artifact.payload["rule_id"], artifact.payload["rule_version"]) == (
        rule.id,
        rule.version,
    )
    assert artifact.payload["run_id"] == run.id
    assert artifact.payload["body_md"] == markdown
    assert artifact.payload["evidence_ids"] == [ids["transcript"]]
    assert not extraction.query(Query("action_suggestion", include_superseded=True)).items

    assert replies == ["Segmentation", "Selection", "Generated"]
    assert [call.purpose for call in calls] == ["segmentation", "selection", "generation"]
    call_ids = [call.call_id for call in calls]
    assert len(set(call_ids)) == 3
    assert [(i["call_id"], i["state"]) for i in run.payload["gateway_calls"]] == [
        (call_id, "returned") for call_id in call_ids
    ]
    assert {row.id for row in audit_writer.query(Query("llm_call")).items} == set(call_ids)
    for call in calls:
        assert call.run_id == run.id and call.model == "default"
        assert call.root_feedback_id == run.payload["root_feedback_id"] == artifact.root_feedback_id
        record = audit_writer.get_system_record(call.call_id)
        assert record is not None and record.writer == "llm_gateway"
        audit = LLMCallRecord.model_validate(record.payload)
        assert (audit.status, audit.model, audit.usage) == ("success", model, usage)
        assert (audit.caller_module, audit.purpose) == ("extraction", call.purpose)
        assert (audit.root_feedback_id, audit.depth) == (call.root_feedback_id, 0)
        assert "request_text" not in record.payload and "response_text" not in record.payload
