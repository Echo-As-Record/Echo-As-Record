"""Actual local HTTP boundary and independent browser demo state."""

from threading import Thread

import httpx
import pytest
from ear_extraction.web_demo import DemoServer, DemoSession


def test_demo_correction_and_execution_preserve_activity():
    session = DemoSession()
    initial = session.state()
    extracted = session.act("extract", initial["revision"])
    activity_id = extracted["activity"][0]["id"]
    old_suggestion = extracted["action_suggestion"][0]["id"]
    corrected = session.act("correct", extracted["revision"])
    assert corrected["activity"][0]["id"] == activity_id
    assert corrected["artifact"][0]["version"] == 2
    assert corrected["speaker_id"] in corrected["artifact"][0]["payload"]["body_md"]
    assert (
        next(s for s in corrected["suggestion_history"] if s["id"] == old_suggestion)["status"]
        == "superseded"
    )
    target = corrected["action_suggestion"][0]["id"]
    approved = session.act("approve", corrected["revision"], target)
    executed = session.act("execute", approved["revision"], target)
    assert len(executed["executed"]) == 1
    assert executed["action_suggestion"][0]["payload"]["state"] == "executed"
    with pytest.raises(ValueError, match="页面状态已更新"):
        session.act("execute", approved["revision"], target)
    assert len(session.state()["executed"]) == 1
    reset = session.act("reset", executed["revision"])
    assert reset["artifact"] == reset["executed"] == []
    assert not reset["corrected"]


def test_approval_is_required_and_approved_suggestion_survives_correction():
    session = DemoSession()
    extracted = session.act("extract", 0)
    target = extracted["action_suggestion"][0]["id"]
    with pytest.raises(ValueError, match="请先审核"):
        session.act("execute", 1, target)
    assert not session.executed
    session.act("approve", 1, target)
    corrected = session.act("correct", 2)
    preserved = next(s for s in corrected["action_suggestion"] if s["id"] == target)
    assert preserved["payload"]["state"] == "approved"
    assert preserved["payload"]["artifact_version"] == 1


@pytest.fixture
def server_url():
    with DemoServer(("127.0.0.1", 0)) as server:
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_http_sessions_assets_and_action_boundary(server_url):
    with (
        httpx.Client(base_url=server_url, trust_env=False) as first,
        httpx.Client(base_url=server_url, trust_env=False) as second,
    ):
        page = first.get("/")
        assert page.status_code == 200
        assert "我的活动" in page.text
        assert first.get("/app.js").status_code == 200
        assert first.get("/style.css").status_code == 200
        assert first.get("/web_demo.py").status_code == 404
        assert first.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
        state = first.get("/api/state")
        assert "HttpOnly" in state.headers["set-cookie"]
        assert state.json()["revision"] == 0
        action = {"action": "extract", "revision": 0}
        assert first.post("/api/action", json=action).status_code == 403
        assert (
            first.post(
                "/api/action",
                json=action,
                headers={"X-Ear-Demo": "1", "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        result = first.post("/api/action", json=action, headers={"X-Ear-Demo": "1"})
        assert result.status_code == 200
        assert len(result.json()["artifact"]) == 1
        assert first.get("/api/state").json()["revision"] == 1
        assert second.get("/api/state").json()["artifact"] == []
        assert (
            first.post("/api/action", json=action, headers={"X-Ear-Demo": "1"}).status_code == 400
        )
        invalid = first.post(
            "/api/action",
            json={"action": "approve", "revision": 1, "target": "missing"},
            headers={"X-Ear-Demo": "1"},
        )
        assert invalid.status_code == 400
        assert first.get("/api/state").json()["revision"] == 1
