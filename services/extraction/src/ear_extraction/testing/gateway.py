"""Loopback-only ⑦ mock exercising the actual HttpGateway JSON transport."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from ear_extraction.demo import DemoGateway
from ear_extraction.domain import Call
from ear_extraction.model_io import HttpGateway


class SyntheticGateway(DemoGateway):
    def complete(self, call: Call) -> dict:
        result = super().complete(call)
        if call.purpose == "segmentation":
            result["activities"][0]["evidence_ids"] = [r["id"] for r in call.data["evidence"]]
        if call.purpose in {"generation", "reevaluation"}:
            if call.data["rule"]["payload"]["instruction"] == "仅记实验议题":
                result = {"body_md": "实验议题：结果整理。", "evidence_ids": result["evidence_ids"]}
            if call.purpose == "reevaluation":
                old = call.data["old_artifact"]
                if (
                    old["body_md"] == result["body_md"]
                    and old["rule_version"] == call.data["rule"]["version"]
                ):
                    return {"unchanged": True}
        return result


@dataclass
class GatewayProbe:
    calls: list[Call] = field(default_factory=list)
    fault: str | None = None
    faults_sent: int = 0


@contextmanager
def mock_http_gateway(fault: str | None = None) -> Iterator[tuple[HttpGateway, GatewayProbe]]:
    """Faults: one HTTP 503 or one invalid output; no real credentials or network models."""
    probe = GatewayProbe(fault=fault)
    model = SyntheticGateway()
    token = "synthetic-loopback-only"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def do_POST(self) -> None:
            if self.path != "/invoke":
                self.reply(404, {})
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.reply(401, {})
                return
            try:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not payload.pop("system", ""):
                    raise ValueError("system_required")
                call = Call.model_validate(payload)
            except (ValueError, KeyError, TypeError):
                self.reply(400, {})
                return
            probe.calls.append(call)
            if probe.fault and not probe.faults_sent:
                probe.faults_sent += 1
                if probe.fault == "http_503":
                    self.reply(503, {"error": "synthetic_unavailable"})
                else:
                    self.reply(200, {"output": {"unexpected": True}})
                return
            self.reply(200, {"output": model.complete(call)})

        def reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield (
            HttpGateway(
                f"http://127.0.0.1:{server.server_port}/invoke", token, timeout=5, trust_env=False
            ),
            probe,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
