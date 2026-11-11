"""Standard-library OpenAI proxy. Serialize admission through settlement, log metadata only."""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import math
import os
import signal
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PRICES = json.loads(Path(__file__).with_name("openai_prices.json").read_text())
PATHS = {
    "/v1/responses",
    "/v1/chat/completions",
    "/v1/audio/transcriptions",
    "/v1/audio/translations",
}


def estimate(model: str, usage: dict[str, Any], seconds: float) -> float:
    models = PRICES["models"]
    price = models.get(model)
    if price is None:
        price = {
            k: max(p.get(k, 0) for p in models.values())
            for k in ("text_input", "text_output", "audio_input", "audio_output", "minute")
        }
    if "minute" in price and model in models:
        return seconds / 60 * price["minute"]
    cost = 0.0
    for direction, alias in (("input", "prompt"), ("output", "completion")):
        total = usage.get(f"{direction}_tokens", usage.get(f"{alias}_tokens", 0))
        details = (
            usage.get(
                f"{direction}_token_details",
                usage.get(f"{direction}_tokens_details", usage.get(f"{alias}_tokens_details", {})),
            )
            or {}
        )
        audio = details.get("audio_tokens", 0)
        text = details.get("text_tokens", max(0, total - audio))
        cost += (
            text * price.get(f"text_{direction}", 0) + audio * price.get(f"audio_{direction}", 0)
        ) / 1_000_000
    if model not in models:
        cost = max(cost, seconds / 60 * price["minute"])
    return cost


def duration(data: bytes) -> float:
    with tempfile.NamedTemporaryFile() as f:
        f.write(data)
        f.flush()
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                f.name,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    value = float(result.stdout.strip())
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid audio duration")
    return value


def request_info(body: bytes, content_type: str) -> tuple[str, float]:
    seconds = 0.0
    model = "unknown"
    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            if name == "model":
                model = payload.decode()
            elif name == "file":
                seconds += duration(payload)
    else:
        obj = json.loads(body)
        if not isinstance(obj, dict):
            raise ValueError("JSON object required")
        model = obj.get("model", "unknown")

        def walk(value: Any) -> None:
            nonlocal seconds
            if isinstance(value, dict):
                audio = value.get("input_audio")
                if isinstance(audio, dict) and "data" in audio:
                    seconds += duration(base64.b64decode(audio["data"], validate=True))
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(obj)
        # Background jobs and hosted tools have costs outside this synchronous meter.
        if (
            obj.get("background")
            or obj.get("tools")
            or obj.get("service_tier") not in (None, "auto", "default")
        ):
            raise ValueError("background jobs and tools are not metered")
    if not isinstance(model, str):
        raise ValueError("model must be a string")
    return model, seconds


def numeric_usage(value: Any) -> dict[str, Any]:
    """Do not allow upstream text to become a persistent log field."""
    if not isinstance(value, dict):
        return {}
    return {
        k: numeric_usage(v) if isinstance(v, dict) else v
        for k, v in value.items()
        if isinstance(v, (dict, int, float)) and isinstance(k, str)
    }


class Meter:
    def __init__(
        self,
        key: str,
        token: str,
        budget: float,
        log: Path,
        upstream: str = "https://api.openai.com",
        proxy: str | None = None,
    ) -> None:
        self.key, self.token, self.budget, self.log = key, token, budget, log
        self.proxy = urlsplit(proxy) if proxy else None
        self.upstream = urlsplit(upstream)
        if not self.upstream.hostname or self.upstream.scheme not in ("http", "https"):
            raise ValueError("invalid upstream")
        self.host = self.upstream.hostname
        self.total = 0.0
        self.lock = threading.Lock()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        meter = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def reply(self, status: int, message: str) -> None:
                body = json.dumps({"error": {"message": message}}, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self.reply(200, "ready")
                else:
                    self.reply(404, "unsupported endpoint")

            def do_POST(self) -> None:
                # Hold through upstream completion: queued calls see settled cost, not stale cost.
                with meter.lock:
                    self.forward()

            def forward(self) -> None:
                model, seconds, usage = "unknown", 0.0, {}
                status, cost, uncertain = 500, 0.0, False
                conn = None
                sent = False
                try:
                    if self.headers.get("Authorization") != f"Bearer {meter.token}":
                        status = 401
                        self.reply(status, "invalid experiment token")
                        return
                    if meter.total >= meter.budget:
                        status = 429
                        self.reply(status, "本次实验的 OpenAI 预算已用完")
                        return
                    if self.path not in PATHS:
                        status = 400
                        self.reply(status, "unsupported metered endpoint")
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        status = 400
                        self.reply(status, "invalid Content-Length")
                        return
                    if self.headers.get("Transfer-Encoding") or not 0 < length <= 512 * 1024**2:
                        status = 400
                        self.reply(status, "Content-Length required; maximum 512 MiB")
                        return
                    body = self.rfile.read(length)
                    content_type = self.headers.get("Content-Type", "application/json")
                    try:
                        model, seconds = request_info(body, content_type)
                    except (ValueError, OSError, subprocess.SubprocessError):
                        status = 400
                        self.reply(status, "invalid request or audio cannot be measured")
                        return
                    if self.path == "/v1/chat/completions" and content_type.startswith(
                        "application/json"
                    ):
                        request = json.loads(body)
                        if request.get("stream"):
                            request.setdefault("stream_options", {})["include_usage"] = True
                            body = json.dumps(request).encode()
                    cls = (
                        http.client.HTTPSConnection
                        if meter.upstream.scheme == "https"
                        else http.client.HTTPConnection
                    )
                    if meter.proxy and cls is http.client.HTTPSConnection:
                        # 宿主只能经代理上外网：连代理，再 CONNECT 到上游。
                        conn = cls(str(meter.proxy.hostname), meter.proxy.port, timeout=600)
                        conn.set_tunnel(meter.host, meter.upstream.port or 443)
                    else:
                        conn = cls(meter.host, meter.upstream.port, timeout=600)
                    sent = True
                    conn.request(
                        "POST",
                        self.path,
                        body,
                        {
                            "Authorization": f"Bearer {meter.key}",
                            "Content-Type": content_type,
                            "Accept-Encoding": "identity",
                        },
                    )
                    response = conn.getresponse()
                    status = response.status
                    self.send_response(status)
                    self.send_header(
                        "Content-Type", response.getheader("Content-Type", "application/json")
                    )
                    self.send_header("Connection", "close")
                    self.end_headers()
                    disconnected = False
                    chunks = []
                    streaming = "text/event-stream" in response.getheader("Content-Type", "")
                    while True:
                        chunk = response.readline() if streaming else response.read(65536)
                        if not chunk:
                            break
                        if streaming:
                            if chunk.startswith(b"data:"):
                                try:
                                    event = json.loads(chunk[5:])
                                    usage = (
                                        numeric_usage(
                                            event.get("usage")
                                            or event.get("response", {}).get("usage")
                                        )
                                        or usage
                                    )
                                except (ValueError, AttributeError):
                                    pass
                        else:
                            chunks.append(chunk)
                        if not disconnected:
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except OSError:
                                disconnected = True
                    if not streaming:
                        with contextlib.suppress(ValueError, AttributeError):
                            usage = numeric_usage(json.loads(b"".join(chunks)).get("usage"))
                    if 200 <= status < 300:
                        cost = estimate(model, usage, seconds)
                        if not usage and not (
                            seconds and "minute" in PRICES["models"].get(model, {})
                        ):
                            uncertain = True
                except (OSError, http.client.HTTPException):
                    uncertain = sent
                    if not sent:
                        status = 502
                        self.reply(status, "upstream unavailable")
                finally:
                    if conn:
                        conn.close()
                    # Lost usage cannot silently become free calls. Freeze remaining budget.
                    if uncertain:
                        cost = max(cost, meter.budget - meter.total)
                    meter.total += cost
                    row = {
                        "time": datetime.now(UTC).isoformat(),
                        "path": self.path if self.path in PATHS else "unsupported",
                        "model": model,
                        "audio_seconds": seconds,
                        "usage": usage,
                        "estimated_usd": cost,
                        "total_usd": meter.total,
                        "over_budget_usd": max(0, meter.total - meter.budget),
                        "status": status,
                        "uncertain": uncertain,
                    }
                    with meter.log.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")

        return Handler


def main() -> None:
    meter = Meter(
        os.environ["EARbaseline_key"],  # noqa: SIM112
        os.environ["METER_TOKEN"],
        float(os.environ["METER_BUDGET"]),
        Path("/meter.jsonl"),
        proxy=os.environ.get("HTTPS_PROXY"),
    )
    server = ThreadingHTTPServer(("0.0.0.0", 8787), meter.handler())
    server.daemon_threads = False

    def stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()  # Drain in-flight usage before the host reads meter.jsonl.


if __name__ == "__main__":
    main()
