"""本机录音浏览器：按日期列出 inventory 里的录音，播放音频，同步显示旧 Ear 的转写。

只监听 127.0.0.1，只读：音频只服务 index.json 里列出的文件；转写从旧 Ear Evidence API 现取，
取不到时退回 inventory 的 transcripts/<id>.md。唯一会写的是 inventory/notes.json（人工备注）。

    GET  /                       页面（player.html）
    GET  /api/index              inventory/index.json
    GET  /api/recordings/<id>    这段录音的转写、环境声、位置、手机状态（相对开头的秒数）
    GET  /audio/<id>             音频（支持 Range，便于拖动）
    GET  /api/search?q=...       在转写（transcripts/*.md）和备注里搜索
    GET  /api/notes              全部备注
    PUT  /api/notes/<id>         写一条备注（{"text": "..."}）
"""

from __future__ import annotations

import importlib.resources
import json
import mimetypes
import re
import threading
from collections.abc import Callable
from datetime import datetime, tzinfo
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ear_adapter.ear_api import (
    AudioChunk,
    AudioEvent,
    CaptureEvidence,
    ContextEvent,
    ContextSnapshot,
    SensorObservation,
    SpeechSegment,
)

from ear_baselines.cases import write_json

EvidenceReader = Callable[[str], CaptureEvidence]


def timeline(evidence: CaptureEvidence, tz: tzinfo) -> dict[str, Any]:
    """把 Evidence 变成前端用的行：t 是相对录音开头的秒数，clock 是显示时区的时刻。"""
    start = evidence.started_at

    def row(t: datetime, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        return {
            "t": round((t - start).total_seconds(), 2),
            "clock": t.astimezone(tz).strftime("%H:%M:%S"),
            "kind": kind,
            "text": text,
            **extra,
        }

    chunks = sorted(evidence.of_type("audio_chunk", AudioChunk), key=lambda c: c.started_at)
    chunk_no = {c.audio_chunk_id: i for i, c in enumerate(chunks, start=1)}
    rows: list[dict[str, Any]] = []
    for s in evidence.of_type("speech_segment", SpeechSegment):
        speaker = f"块{chunk_no.get(s.audio_chunk_id, 0)}·{s.local_speaker_id}"
        rows.append(
            row(
                s.started_at,
                "speech",
                s.original_text,
                end=round((s.ended_at - start).total_seconds(), 2),
                speaker=speaker,
            )
        )
    for e in evidence.of_type("audio_event", AudioEvent):
        end = round((e.ended_at - start).total_seconds(), 2)
        rows.append(row(e.started_at, "sound", e.original_label, end=end))
    seen: set[tuple[datetime, float, float]] = set()
    for o in evidence.of_type("sensor_observation", SensorObservation):
        if o.location is None:
            continue
        key = (o.observed_at, o.location.latitude, o.location.longitude)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            row(
                o.observed_at,
                "gps",
                f"{o.location.latitude:.5f}, {o.location.longitude:.5f}",
                lat=o.location.latitude,
                lon=o.location.longitude,
            )
        )
    for c in evidence.of_type("context_snapshot", ContextSnapshot):
        state = "，".join(f"{k}={v}" for k, v in sorted(c.state.items()))
        rows.append(row(c.observed_at, "phone", state))
    for ev in evidence.of_type("context_event", ContextEvent):
        tag = "（推断）" if ev.inferred else ""
        rows.append(row(ev.occurred_at, "phone", f"{ev.event_type}{tag}：{ev.details}"))
    order = {"phone": 0, "gps": 1, "sound": 2, "speech": 3}
    rows.sort(key=lambda r: (r["t"], order[r["kind"]]))
    return {
        "capture_id": evidence.capture_id,
        "started_at": start.astimezone(tz).isoformat(),
        "availability": {a.type: a.state for a in evidence.availability},
        "rows": rows,
    }


class PlayerApp:
    """与 HTTP 无关的逻辑，便于测试。"""

    def __init__(self, inventory_dir: Path, tz: tzinfo, read_evidence: EvidenceReader | None):
        self.dir = inventory_dir
        self.tz = tz
        self.read_evidence = read_evidence
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def index(self) -> dict[str, Any]:
        return json.loads((self.dir / "index.json").read_text("utf-8"))

    def recording(self, rec_id: str) -> dict[str, Any] | None:
        for rec in self.index().get("recordings", []):
            if rec.get("id") == rec_id:
                return rec
        return None

    def audio_path(self, rec_id: str) -> Path | None:
        rec = self.recording(rec_id)
        if rec is None or not rec.get("audio_path"):
            return None
        path = Path(rec["audio_path"])
        return path if path.is_file() else None

    def detail(self, rec_id: str) -> dict[str, Any] | None:
        rec = self.recording(rec_id)
        if rec is None:
            return None
        with self._lock:
            if rec_id in self._cache:
                return self._cache[rec_id]
        result: dict[str, Any] = {"rows": [], "source": "none", "error": None}
        capture_id = rec.get("capture_id")
        if capture_id and self.read_evidence is not None:
            try:
                result = {**timeline(self.read_evidence(capture_id), self.tz), "source": "ear"}
            except Exception as exc:  # 旧 Ear 不在线时退回本地转写
                result["error"] = f"读不到旧 Ear：{exc}"
        if result["source"] == "none":
            md = self.dir / "transcripts" / f"{rec_id}.md"
            if md.exists():
                result["markdown"] = md.read_text("utf-8")
                result["source"] = "markdown"
        with self._lock:
            if result["source"] == "ear":
                self._cache[rec_id] = result
        return result

    def search(self, query: str, limit: int = 300) -> list[dict[str, str]]:
        """在 transcripts/*.md 和备注里找包含 query 的行（不区分大小写）。"""
        q = query.strip().lower()
        if not q:
            return []
        hits: list[dict[str, str]] = []
        for rec_id, note in self.notes().items():
            if q in str(note.get("text", "")).lower():
                hits.append({"id": rec_id, "line": f"备注：{note['text']}"})
        for md in sorted((self.dir / "transcripts").glob("*.md")):
            for line in md.read_text("utf-8").splitlines():
                if q in line.lower() and not line.startswith("#"):
                    hits.append({"id": md.stem, "line": line.strip()})
                    if len(hits) >= limit:
                        return hits
        return hits

    def notes(self) -> dict[str, Any]:
        path = self.dir / "notes.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    def set_note(self, rec_id: str, text: str) -> None:
        with self._lock:
            notes = self.notes()
            if text.strip():
                notes[rec_id] = {"text": text, "updated_at": datetime.now(self.tz).isoformat()}
            else:
                notes.pop(rec_id, None)
            write_json(self.dir / "notes.json", notes)


_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def make_handler(app: PlayerApp) -> type[BaseHTTPRequestHandler]:
    page = importlib.resources.files("ear_baselines").joinpath("player.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = unquote(self.path.split("?", 1)[0])
            if path in ("/", "/index.html"):
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/index":
                self._json(app.index())
            elif path == "/api/notes":
                self._json(app.notes())
            elif path == "/api/search":
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                self._json(app.search(query))
            elif path.startswith("/api/recordings/"):
                detail = app.detail(path.removeprefix("/api/recordings/"))
                self._json(
                    detail if detail is not None else {"error": "没有这段录音"},
                    200 if detail is not None else 404,
                )
            elif path.startswith("/audio/"):
                self._audio(path.removeprefix("/audio/"))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_PUT(self) -> None:
            path = unquote(self.path)
            if not path.startswith("/api/notes/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            rec_id = path.removeprefix("/api/notes/")
            if app.recording(rec_id) is None:
                self._json({"error": "没有这段录音"}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            app.set_note(rec_id, str(data.get("text", "")))
            self._json({"ok": True})

        def _audio(self, rec_id: str) -> None:
            file = app.audio_path(rec_id)
            if file is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            size = file.stat().st_size
            start, end = 0, size - 1
            match = _RANGE.match(self.headers.get("Range", ""))
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                else:
                    start = max(0, size - int(match.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(HTTPStatus.OK)
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if file.suffix.lower() == ".m4a":
                ctype = "audio/mp4"
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with file.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = f.read(min(1 << 16, remaining))
                    if not block:
                        break
                    try:
                        self.wfile.write(block)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(block)

    return Handler


def serve(app: PlayerApp, port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    print(f"录音浏览器：http://127.0.0.1:{server.server_address[1]}/  （Ctrl-C 结束）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
