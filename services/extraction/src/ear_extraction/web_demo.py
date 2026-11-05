"""Local browser demo of the original MVP; synthetic peers and in-memory sessions only."""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from contextlib import suppress
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from ear_contracts import Cause, new_id
from ear_storage_client import (
    AppendRaw,
    Context,
    Create,
    PublishReevaluation,
    Query,
    Update,
    iter_query,
)
from ear_storage_client.testing import make_raw, ts

from ear_extraction.demo import DemoGateway, fixture, submit
from ear_extraction.domain import Blocked, Call, Command, Interval, Rule
from ear_extraction.orchestration import ExtractionService
from ear_extraction.persistence import records, semantic

INTERVAL = Interval(start=ts("2026-11-05T09:00:00Z"), end=ts("2026-11-05T10:00:00Z"))
ASSETS = Path(__file__).with_name("web")


class BrowserGateway(DemoGateway):
    def complete(self, call: Call) -> dict:
        result = super().complete(call)
        if "action" in result:
            result["action"]["notes"] = result["body_md"].removeprefix("- ")
        return result


class DemoSession:
    def __init__(self):
        self.lock = threading.Lock()
        self.revision = 0
        self.last_seen = time.monotonic()
        self.reset()

    def reset(self) -> None:
        self.storage, self.ids = fixture()
        self.ids["second_person"] = new_id()
        self.storage.client("attribution").write(
            [Create("person", self.ids["second_person"], {"name": "合成人物乙", "references": []})],
            Context.system(),
        ).check()
        self.service = ExtractionService(self.storage.client("extraction"), BrowserGateway())
        self.service.subscribe()
        self.executed: dict[str, dict] = {}
        self.steps: list[dict] = []
        self.command(
            Command(
                kind="rule_create",
                rule=Rule(scope="组会", instruction="提取明确的待办及截止日期", action="reminder"),
            )
        )
        self.steps.append(
            {"title": "演示数据已就绪", "detail": "合成转写、人物归属和提取规则已载入。"}
        )

    def command(self, command: Command) -> None:
        run_id = submit(self.storage, command)
        self.service.drain()
        self.storage.run_until_idle()
        run = self.service.storage.get_system_record(run_id)
        if run is None or run.payload["status"] != "succeeded":
            raise ValueError("操作未完成，请重置后重试。")

    def state(self) -> dict:
        client = self.service.storage
        kinds = ("activity", "artifact", "action_suggestion", "person", "rule")
        state: dict = {
            kind: [r.model_dump(mode="json") for r in records(client, kind)] for kind in kinds
        }
        state["suggestion_history"] = [
            r.model_dump(mode="json")
            for r in iter_query(client, Query("action_suggestion", include_superseded=True))
        ]
        state["artifact_history"] = [
            h.model_dump(mode="json")
            for r in records(client, "artifact")
            for h in client.history(r.id)
        ]
        state["evidence"] = [
            r.model_dump(mode="json") for r in iter_query(client, Query("transcript_segment"))
        ]
        state["runs"] = [
            {
                "id": r.id,
                "status": r.payload["status"],
                "kind": r.payload["command"].get("kind"),
                "calls": len(r.payload["model_calls"]),
            }
            for r in self.service.runs()
        ]
        attr = semantic(client, self.ids["attribution"], "attribution")
        state.update(
            revision=self.revision,
            corrected=attr.payload["target_id"] == self.ids["second_person"],
            speaker_id=attr.payload["target_id"],
            steps=self.steps,
            executed=list(self.executed.values()),
        )
        return state

    def act(self, action: str, expected_revision: int, target: str | None = None) -> dict:
        with self.lock:
            if expected_revision != self.revision:
                raise ValueError("页面状态已更新，请刷新后重试。")
            if action == "reset":
                self.reset()
            elif action == "extract":
                if records(self.service.storage, "activity"):
                    raise ValueError("本场景已提取；可以纠正归属，或重置后重新演示。")
                self.command(Command(kind="extract", interval=INTERVAL))
                self.steps.append(
                    {"title": "活动提取完成", "detail": "划分 1 个组会活动，生成待办与待审核提醒。"}
                )
            elif action == "correct":
                self.correct()
            elif action in {"approve", "reject", "execute"}:
                suggestion = semantic(self.service.storage, target, "action_suggestion")
                if action == "execute":
                    if suggestion.payload["state"] != "approved":
                        raise ValueError("请先审核通过这条建议。")
                    # Fake external service with a stable idempotency key; no phone/network call.
                    self.executed.setdefault(
                        suggestion.id, {"id": suggestion.id, **suggestion.payload["action"]}
                    )
                    self.command(
                        Command(
                            kind="action_result",
                            target_id=suggestion.id,
                            expected_version=suggestion.version,
                            outcome="executed",
                            receipt=f"browser-mock:{suggestion.id}",
                        )
                    )
                    self.steps.append(
                        {
                            "title": "模拟执行成功",
                            "detail": "已写回执行回执；没有调用真实手机或日历。",
                        }
                    )
                else:
                    self.command(
                        Command(
                            kind="review_suggestion",
                            target_id=suggestion.id,
                            expected_version=suggestion.version,
                            decision="approved" if action == "approve" else "rejected",
                        )
                    )
                    self.steps.append(
                        {
                            "title": "建议已通过" if action == "approve" else "建议已拒绝",
                            "detail": "审核结果已保存。",
                        }
                    )
            else:
                raise ValueError("未知演示操作。")
            self.revision += 1
            return self.state()

    def correct(self) -> None:
        if not records(self.service.storage, "artifact"):
            raise ValueError("请先生成活动和待办。")
        client = self.storage.client("attribution")
        attr = semantic(client, self.ids["attribution"], "attribution")
        if attr.payload["target_id"] == self.ids["second_person"]:
            raise ValueError("本场景已经纠正为乙。")
        raw = make_raw(
            "feedback",
            {"module": "attribution", "mock": "correct_to_second_person"},
            start=INTERVAL.end,
            channel="api",
        )
        self.storage.client("api").write([AppendRaw(raw)], Context.feedback(raw.id)).check()
        old = semantic(client, self.ids["person"], "person")
        new = semantic(client, self.ids["second_person"], "person")
        context = Context.feedback(raw.id)
        client.write(
            [
                Update(
                    "person",
                    old.id,
                    old.version,
                    {**old.payload, "references": []},
                    skip_history=True,
                ),
                Update(
                    "person",
                    new.id,
                    new.version,
                    {**new.payload, "references": [self.ids["voice"]]},
                    skip_history=True,
                ),
                Update("attribution", attr.id, attr.version, {**attr.payload, "target_id": new.id}),
            ],
            context,
        ).check()
        # Simulated ⑤ uses shared dependency lookup, never directly edits an artifact.
        dependency = self.storage.client("dependency")
        targets = {
            d.dependent.id
            for d in dependency.stale_dependencies([attr.id])
            if d.dependent.data_type == "artifact"
        }
        dependency.write(
            [
                PublishReevaluation(
                    "artifact",
                    target,
                    Cause(
                        data_type="attribution",
                        data_id=attr.id,
                        old_version=attr.version,
                        new_version=attr.version + 1,
                    ),
                    "说话人 A 从合成人物甲纠正为合成人物乙",
                )
                for target in targets
            ],
            context.deeper(),
        ).check()
        self.storage.run_until_idle()
        self.service.drain()
        self.storage.run_until_idle()
        if any(r.payload["status"] != "succeeded" for r in self.service.runs()):
            raise ValueError("重判未完成，请查看任务状态或重置演示。")
        self.steps.append(
            {
                "title": "归属纠正并完成重判",
                "detail": "活动保持不变，待办负责人变为乙；未审核提醒重新生成。",
            }
        )


class DemoServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]):
        super().__init__(address, Handler)
        self.sessions: dict[str, DemoSession] = {}
        self.sessions_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    @property
    def demo_server(self) -> DemoServer:
        return cast(DemoServer, self.server)

    def log_message(self, format: str, *args) -> None:
        pass

    def respond(
        self, status: int, body: bytes, content_type: str, cookie: str | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
        )
        if cookie:
            self.send_header("Set-Cookie", f"ear_demo={cookie}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def session(self) -> tuple[DemoSession, str]:
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        key = cookies["ear_demo"].value if "ear_demo" in cookies else ""
        with self.demo_server.sessions_lock:
            now = time.monotonic()
            for old in list(self.demo_server.sessions):
                if now - self.demo_server.sessions[old].last_seen > 7200:
                    del self.demo_server.sessions[old]
            if key not in self.demo_server.sessions:
                if len(self.demo_server.sessions) >= 32:
                    raise ValueError("演示会话已满，请稍后再试。")
                key = secrets.token_urlsafe(24)
                self.demo_server.sessions[key] = DemoSession()
            session = self.demo_server.sessions[key]
            session.last_seen = now
            return session, key

    def trusted_host(self) -> bool:
        return urlsplit("http://" + self.headers.get("Host", "")).hostname in {
            "localhost",
            "127.0.0.1",
        }

    def do_GET(self) -> None:
        if not self.trusted_host():
            self.respond(403, b"Local access only", "text/plain")
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            try:
                session, key = self.session()
                with session.lock:
                    body = session.state()
                self.respond(
                    200, json.dumps(body, ensure_ascii=False).encode(), "application/json", key
                )
            except ValueError as exc:
                self.respond(503, json.dumps({"error": str(exc)}).encode(), "application/json")
        elif path in {"/", "/app.js", "/style.css"}:
            name = "index.html" if path == "/" else path[1:]
            mime = {
                "index.html": "text/html; charset=utf-8",
                "app.js": "text/javascript",
                "style.css": "text/css",
            }[name]
            self.respond(200, (ASSETS / name).read_bytes(), mime)
        else:
            self.respond(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if (
            not self.trusted_host()
            or self.headers.get("X-Ear-Demo") != "1"
            or (origin and origin != "http://" + self.headers.get("Host", ""))
        ):
            self.respond(403, b"Forbidden", "text/plain")
            return
        if self.path != "/api/action":
            self.respond(404, b"Not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("请求大小无效。")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or type(data.get("revision")) is not int:
                raise ValueError("请求格式无效。")
            session, key = self.session()
            body = session.act(data.get("action", ""), data["revision"], data.get("target"))
            self.respond(
                200, json.dumps(body, ensure_ascii=False).encode(), "application/json", key
            )
        except (Blocked, ValueError, KeyError, TypeError) as exc:
            self.respond(
                400,
                json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                "application/json",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ear MVP local synthetic browser demo")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with DemoServer(("127.0.0.1", args.port)) as server:
        print(f"Ear MVP demo: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()
