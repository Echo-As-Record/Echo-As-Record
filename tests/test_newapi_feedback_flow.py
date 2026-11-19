"""Authenticated manual extraction through ⑥, with entirely in-process I/O."""

from __future__ import annotations

import json
from contextlib import ExitStack, closing, nullcontext
from pathlib import Path
from typing import Any

import httpx
from ear_api.app import create_app as create_api
from ear_api.config import Settings
from ear_api.upstream import Upstream
from ear_contracts import is_id, new_id
from ear_extraction.demo import fixture
from ear_extraction.model_io import SYSTEM
from ear_extraction.orchestration import ExtractionService
from ear_llm_gateway import Gateway, GatewayClient
from ear_llm_gateway.http import create_app as create_gateway
from ear_llm_gateway.journal import Journal
from ear_llm_gateway.openai_compatible import OpenAICompatibleProvider
from ear_storage_client import Query
from ear_storage_client.wire import decode_read, decode_request, encode_read_result, encode_result
from fastapi.testclient import TestClient

API_TOKEN, STORAGE_TOKEN, CALLER_TOKEN = (c * 43 for c in "asc")
ORIGIN = "http://ear-api.invalid"
AUTH = {"Authorization": f"Bearer {API_TOKEN}", "Origin": ORIGIN}
INTERVAL = {"start": "2026-11-05T09:00:00Z", "end": "2026-11-05T10:00:00Z"}
MARKDOWN = "## 合成组会\n\n- 合成人物甲整理实验结果。"


def test_authenticated_manual_extraction_with_identical_retry(tmp_path: Path):
    store, ids = fixture()
    api_store = store.client("api")
    extraction = store.client("extraction")
    audit = store.client("llm_gateway")
    writes: list[bytes] = []
    stages: list[str] = []
    lose_next_write_reply = False
    rule_id = ""

    def storage_http(request: httpx.Request) -> httpx.Response:
        """Wire-v1 simulator backed by real append/dedup and memory bus delivery."""
        nonlocal lose_next_write_reply
        assert request.url.host == "ear-storage.invalid"
        assert request.headers["authorization"] == f"Bearer {STORAGE_TOKEN}"
        match request.method, request.url.path:
            case "GET", "/v1/identity":
                return httpx.Response(200, json={"codec": 1, "module": "api"})
            case "POST", "/v1/read":
                parsed, args = decode_read(request.content)
                assert parsed.snapshot_id is None and parsed.method.value in {"get", "query"}
                result = getattr(api_store, parsed.method.value)(**args)
                return httpx.Response(200, content=encode_read_result(parsed.method, result))
            case "POST", "/v1/batches":
                writes.append(request.content)
                wire = json.loads(request.content)
                batch_id = wire.pop("request_id")
                ops, context = decode_request(json.dumps(wire))
                assert batch_id == context.root_feedback_id and is_id(batch_id)
                result = api_store.write(ops, context).check()
                if lose_next_write_reply:
                    lose_next_write_reply = False
                    raise httpx.ReadError("synthetic lost acknowledgement", request=request)
                return httpx.Response(200, content=encode_result(result))
            case _:
                raise AssertionError(
                    f"unexpected storage request: {request.method} {request.url.path}"
                )

    def provider_http(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        wire = json.loads(request.content)
        prompt = json.loads(wire["messages"][-1]["content"])
        data, stage = prompt["data"], prompt["output_schema"]["title"]
        stages.append(stage)
        if stage == "Segmentation":
            assert data["interval"] == INTERVAL
            output = {
                "activities": [
                    {**INTERVAL, "title_md": "合成组会", "evidence_ids": [ids["transcript"]]}
                ]
            }
        elif stage == "Selection":
            assert [rule["id"] for rule in data["rules"]] == [rule_id]
            output = {"rule_ids": [rule_id]}
        else:
            assert stage == "Generated" and data["rule"]["id"] == rule_id
            output = {"body_md": MARKDOWN, "evidence_ids": [ids["transcript"]]}
        reply = {
            "id": f"synthetic-{stage}",
            "object": "chat.completion",
            "created": 1,
            "model": "synthetic/notes",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(output)},
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(reply).encode()),
        )

    settings = Settings.model_validate(
        {
            "storage_url": "http://ear-storage.invalid",
            "public_origin": ORIGIN,
            "static_dir": None,
            "test_no_auth": False,
            "bindings": [
                {
                    "subject": "synthetic-user",
                    "role": "user",
                    "token": API_TOKEN,
                    "storage_token": STORAGE_TOKEN,
                }
            ],
        }
    )
    app = create_api(settings, upstream=Upstream(settings, httpx.MockTransport(storage_http)))
    rule_body = {
        "request_id": new_id(),
        "module": "extraction",
        "command": {
            "kind": "rule_create",
            "rule": {"scope": "Synthetic meeting", "instruction": "Summarize in Markdown"},
        },
    }
    extract_body = {
        "request_id": new_id(),
        "module": "extraction",
        "command": {"kind": "extract", "interval": INTERVAL},
    }
    assert len({rule_body["request_id"], extract_body["request_id"]}) == 2

    with ExitStack() as stack:
        journal = stack.enter_context(closing(Journal(str(tmp_path / "gateway.sqlite3"))))
        api = stack.enter_context(TestClient(app, base_url=ORIGIN))

        def caller(token: str):
            assert token == CALLER_TOKEN
            return nullcontext(extraction)

        gateway = Gateway(
            callers=caller,
            writer=audit,
            journal=journal,
            aliases={"default": "synthetic/notes"},
            provider=OpenAICompatibleProvider(
                "synthetic-provider-fixture", transport=httpx.MockTransport(provider_http)
            ),
        )
        gateway_api = stack.enter_context(TestClient(create_gateway(gateway)))

        def model_http(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST" and request.url.path == "/v1/complete"
            return gateway_api.post(
                request.url.path,
                headers={
                    "authorization": request.headers["authorization"],
                    "content-type": "application/json",
                },
                content=request.content,
            )

        def post(body: dict[str, Any]) -> httpx.Response:
            return api.post("/v1/feedback", headers=AUTH, json=body)

        def status(feedback_id: str, expected: str) -> dict[str, Any]:
            response = api.get(f"/v1/feedback/{feedback_id}/status", headers=AUTH)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["feedback_id"] == feedback_id and data["status"] == expected
            assert data["status_scope"] == "feedback_processing_not_external_execution"
            return data

        def only_record(kind: str) -> dict[str, Any]:
            response = api.get(f"/v1/records/{kind}", headers=AUTH)
            assert response.status_code == 200, response.text
            [item] = response.json()["items"]
            return item

        model = stack.enter_context(
            GatewayClient(
                "http://ear-gateway.invalid",
                CALLER_TOKEN,
                system=SYSTEM,
                transport=httpx.MockTransport(model_http),
            )
        )
        worker = ExtractionService(extraction, model)
        worker.subscribe()
        for body in (rule_body, extract_body):
            assert api.post("/v1/feedback", json=body).status_code == 401
        assert not writes and not stages

        rule = post(rule_body)
        assert rule.status_code == 202 and rule.json()["business_status"] == "pending"
        rule_feedback = rule.json()["feedback_id"]
        status(rule_feedback, "pending")
        store.run_until_idle()
        status(rule_feedback, "queued")
        worker.drain()
        status(rule_feedback, "succeeded")
        saved_rule = only_record("rule")
        rule_id = saved_rule["id"]
        assert saved_rule["root_feedback_id"] == rule_feedback
        assert saved_rule["payload"]["action"] is None and not stages

        lose_next_write_reply = True
        unknown = post(extract_body)
        assert unknown.status_code == 503
        assert unknown.json() == {"code": "acceptance_unknown_replay_identical_request"}
        accepted = post(extract_body)
        assert accepted.status_code == 202 and accepted.json()["business_status"] == "pending"
        feedback_id = accepted.json()["feedback_id"]
        assert feedback_id == accepted.json()["root_feedback_id"] != rule_feedback
        path = f"/v1/feedback/{feedback_id}/status"
        assert api.get(path).status_code == 401
        assert status(feedback_id, "pending")["completion_source"] is None
        store.run_until_idle()
        run_id = status(feedback_id, "queued")["run_id"]
        assert not stages
        worker.drain()
        done = status(feedback_id, "succeeded")
        assert done["completion_source"] == "extraction_run" and done["run_id"] == run_id

        replay = post(extract_body)
        assert replay.status_code == 202 and replay.json() == accepted.json()
        assert len(writes) == 4 and writes[1] == writes[2] == writes[3]
        store.run_until_idle()
        worker.drain()
        assert status(feedback_id, "succeeded") == done
        assert len(worker.runs()) == 2

        activity, artifact = only_record("activity"), only_record("artifact")
        assert activity["payload"]["title_md"] == "合成组会"
        assert activity["root_feedback_id"] == artifact["root_feedback_id"] == feedback_id
        assert artifact["payload"]["activity_id"] == activity["id"]
        assert artifact["payload"]["rule_id"] == rule_id
        assert artifact["payload"]["run_id"] == run_id
        assert artifact["payload"]["body_md"] == MARKDOWN
        assert artifact["payload"]["evidence_ids"] == [ids["transcript"]]

    assert not store.dead_letters
    assert len(api_store.query(Query("feedback")).items) == 2
    assert not extraction.query(Query("action_suggestion", include_superseded=True)).items
    assert stages == ["Segmentation", "Selection", "Generated"]
    calls = audit.query(Query("llm_call")).items
    assert len(calls) == 3
    assert {call.payload["root_feedback_id"] for call in calls} == {feedback_id}
