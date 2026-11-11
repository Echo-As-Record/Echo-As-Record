"""Synthetic materials and a loopback fake upstream; never Docker or OpenAI."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from ear_baselines import exam, meter
from ear_baselines.cases import write_json


@pytest.fixture(autouse=True)
def no_host_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """不读本机网卡；需要时单个测试再覆盖。"""
    monkeypatch.setattr(exam, "host_addresses", lambda: [])
    monkeypatch.delenv(exam.PROXY_ENV, raising=False)


@pytest.fixture
def paper(tmp_path: Path) -> Path:
    inventory = tmp_path / "inventory"
    (inventory / "transcripts").mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    rows = []
    for ident in ("r1", "r2"):
        prefix = f"segment_{ident}"
        for suffix in (
            "__01__segment_0001.m4a",
            "__context_0001.jsonl.gz",
            "__imu_0001.jsonl.gz",
            "_asr.txt",
            "__phoneContext_1.json",
            "__phoneIMU_1.json",
            "__richVoice_1.json",
        ):
            (inbox / (prefix + suffix)).write_text("synthetic")
        (inventory / "transcripts" / f"{ident}.md").write_text("EAR SECRET")
        rows.append(
            {
                "id": ident,
                "audio_path": str(inbox / (prefix + "__01__segment_0001.m4a")),
                "started_at": "2026-01-01T00:00:00Z",
                "duration_s": 60,
            }
        )
    write_json(inventory / "index.json", {"recordings": rows})
    q = tmp_path / "questions.json"
    write_json(
        q,
        {
            "questions": [
                {
                    "id": "B04",
                    "question": "What happened?",
                    "reference_answer": "ANSWER SECRET",
                    "type": "TYPE SECRET",
                    "evidence": [{"recording_id": "r1", "quote": "EVIDENCE SECRET"}],
                }
            ]
        },
    )
    return exam.prepare(tmp_path, "test", q, ["B04"], ["r2"])


def test_prepare_and_workspace(paper: Path) -> None:
    material = paper / "material"
    assert (material / "questions.md").read_text() == "## B04\n\nWhat happened?\n"
    assert exam.read_json(paper / "meta.json")["recording_ids"] == ["r1", "r2"]
    files = [p.name for p in (material / "recordings/r1").iterdir()]
    assert len(files) == 4
    assert not any("asr" in p or "phone" in p or "richVoice" in p for p in files)
    info = exam.read_json(material / "recordings/r1/info.json")
    assert info["started_at"] == "2026-01-01T08:00:00+08:00"
    for group in ("open", "closed"):
        work = exam.workspace(paper, group, 5) / "workspace"
        assert (work / "ear").exists() == (group == "open")
        assert not (work / "answers_key.json").exists()
        assert "ANSWER SECRET" not in "".join(p.read_text() for p in work.rglob("*.md"))
    root = paper.parent.parent
    with pytest.raises(FileExistsError):
        exam.prepare(root, "test", root / "questions.json", ["B04"])
    (root / "inventory/transcripts/r1.md").unlink()
    with pytest.raises(FileNotFoundError):
        exam.prepare(root, "test", root / "questions.json", ["B04"], force=True)
    assert (paper / "meta.json").exists()  # Failed force leaves the old exam intact.


def test_commands_and_dry_run(paper: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setenv("EAR_ADAPTER_EAR_TOKEN", "old-ear-secret")
    monkeypatch.setenv("EARbaseline_key", "real-secret")
    cmd = exam.container(paper, paper, "net", "codex", "one-time")
    env = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-e"]
    assert {s.split("=", 1)[0] for s in env} == {
        "CODEX_HOME",
        "EARbaseline_key",
        "EARbaseline_base_url",
        "TZ",
    }
    mounts = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--mount"]
    assert {s.split("dst=")[1] for s in mounts} == {"/work", "/codex-home"}
    assert "real-secret" not in str(cmd) and "old-ear-secret" not in str(cmd)
    proxy = exam.meter_command(paper / "log", "net", "one-time", "real-secret", 5, "meter")
    assert "EARbaseline_key=real-secret" in proxy
    assert "real-secret" not in exam.redacted(proxy)
    assert "one-time" not in exam.redacted(proxy)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must not invoke subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    out = exam.run(paper, "closed", dry_run=True, firewall=True)
    printed = capsys.readouterr().out
    assert "real-secret" not in printed and "old-ear-secret" not in printed
    assert "<redacted>" in printed
    assert not (out / "workspace/ear").exists()
    assert exam.read_json(out / "run.json")["dry_run"]


def test_firewall() -> None:
    add = exam.firewall_command("172.25.0.0/16", "192.168.5.0/24")
    assert add == [
        "colima",
        "ssh",
        "--",
        "sudo",
        "iptables",
        "-I",
        "DOCKER-USER",
        "-s",
        "172.25.0.0/16",
        "-d",
        "192.168.5.0/24",
        "-j",
        "DROP",
    ]
    assert exam.firewall_command("172.25.0.0/16", "192.168.5.0/24", True) == [
        "-D" if arg == "-I" else arg for arg in add
    ]


def test_run_cleanup_on_failed_isolation(paper: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARbaseline_key", "real-secret")
    home = paper / "fake-home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: home)
    calls = []

    def fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output = ""
        if cmd[1:3] == ["network", "inspect"]:
            output = json.dumps(
                [{"IPAM": {"Config": [{"Subnet": "172.25.0.0/16", "Gateway": "172.25.0.1"}]}}]
            )
        if "/opt/exam/isolation.py" in cmd:
            output = '{"ok": false}'
        assert "exec" not in cmd or "codex" not in cmd  # Codex must never start.
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(exam, "invoke", fake)
    out = exam.run(paper, "closed", firewall=True)
    info = exam.read_json(out / "run.json")
    assert "隔离自检失败" in info["error"]
    additions = [c for c in calls if "-I" in c]
    deletions = [c for c in calls if "-D" in c]
    assert len(additions) == len(deletions) == 2
    assert len([c for c in calls if c[:3] == ["docker", "rm", "-f"]]) == 3
    assert calls[-1][:3] == ["docker", "network", "rm"]
    mount = next(v for c in calls for v in c if "dst=/codex-home" in v)
    assert not Path(mount.split("src=")[1].split(",dst=")[0]).exists()


def wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\0\0" * 8000)
    return out.getvalue()


def test_duration_and_input_audio() -> None:
    audio = wav()
    assert meter.duration(audio) == pytest.approx(1)
    for key in ("messages", "input"):
        obj = {
            "model": "gpt-audio",
            key: [
                {
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(audio).decode(),
                                "format": "wav",
                            },
                        }
                    ]
                }
            ],
        }
        assert meter.request_info(json.dumps(obj).encode(), "application/json") == ("gpt-audio", 1)


def test_estimate() -> None:
    assert meter.estimate(
        "gpt-audio",
        {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_tokens_details": {"audio_tokens": 800},
            "completion_tokens_details": {"audio_tokens": 400},
        },
        0,
    ) == pytest.approx(0.0527)
    assert meter.estimate("whisper-1", {}, 120) == pytest.approx(0.012)
    assert (
        meter.estimate("unknown", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, 0) == 95
    )
    assert (
        meter.estimate(
            "unknown", {"input_tokens": 1000, "input_tokens_details": {"audio_tokens": 1000}}, 0
        )
        == 0.032
    )


@contextmanager
def server(handler: type[BaseHTTPRequestHandler]):
    try:
        service = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except PermissionError:
        pytest.skip("sandbox denies localhost bind; run fake-upstream tests outside sandbox")
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        yield service.server_port
    finally:
        service.shutdown()
        service.server_close()
        thread.join()


def post(
    port: int,
    body: bytes,
    path: str = "/v1/responses",
    content_type: str = "application/json",
    token: str = "one-time",
) -> tuple[int, bytes]:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "POST", path, body, {"Authorization": f"Bearer {token}", "Content-Type": content_type}
        )
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


class Upstream(BaseHTTPRequestHandler):
    seen: ClassVar[list[str | None]] = []
    sse = (
        b'data: {"choices":[{"delta":{"content":"secret"}}]}\n\n'
        b'data: {"usage":{"input_tokens":1000000}}\n\ndata: [DONE]\n\n'
    )

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        self.seen.append(self.headers.get("Authorization"))
        body = self.rfile.read(int(self.headers["Content-Length"]))
        streaming = b'"stream": true' in body
        payload = self.sse if streaming else b'{"text":"secret","usage":{"input_tokens":1000000}}'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_proxy_token_budget_concurrency_sse(tmp_path: Path) -> None:
    Upstream.seen = []
    with server(Upstream) as upstream:
        state = meter.Meter(
            "real-key", "one-time", 5, tmp_path / "log", f"http://127.0.0.1:{upstream}"
        )
        with server(state.handler()) as port:
            body = json.dumps({"model": "gpt-6-astra", "stream": True}).encode()
            assert post(port, body, token="bad")[0] == 401
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(lambda _: post(port, body), range(5)))
            assert sorted(s for s, _ in results) == [200, 429, 429, 429, 429]
            assert next(b for s, b in results if s == 200) == Upstream.sse
            assert (
                "本次实验的 OpenAI 预算已用完" in json.loads(results[-1][1])["error"]["message"]
                if results[-1][0] == 429
                else True
            )
        assert Upstream.seen == ["Bearer real-key"]
        assert state.total == 20
        logs = [json.loads(line) for line in state.log.read_text().splitlines()]
        assert len(logs) == 6
        assert max(row["over_budget_usd"] for row in logs) == 15
        assert "secret" not in state.log.read_text() and "real-key" not in state.log.read_text()


def test_proxy_minute_and_token_accumulation(tmp_path: Path) -> None:
    with server(Upstream) as upstream:
        state = meter.Meter(
            "real-key", "one-time", 100, tmp_path / "log", f"http://127.0.0.1:{upstream}"
        )
        with server(state.handler()) as port:
            assert post(port, b'{"model":"gpt-audio"}')[0] == 200
            boundary = "synthetic-boundary"
            body = (
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    'name="model"\r\n\r\nwhisper-1\r\n'
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    'name="file"; filename="audio.wav"\r\n'
                    "Content-Type: audio/wav\r\n\r\n"
                ).encode()
                + wav()
                + f"\r\n--{boundary}--\r\n".encode()
            )
            assert (
                post(
                    port,
                    body,
                    "/v1/audio/transcriptions",
                    f"multipart/form-data; boundary={boundary}",
                )[0]
                == 200
            )
        assert state.total == pytest.approx(2.5001)
        rows = [json.loads(s) for s in state.log.read_text().splitlines()]
        assert rows[-1]["audio_seconds"] == 1
        assert rows[-1]["estimated_usd"] == pytest.approx(0.0001)


def test_answers_and_report(paper: Path) -> None:
    out = exam.workspace(paper, "open", 5)
    answers = {
        "answers": [
            {
                "id": "B04",
                "answer": "example",
                "confidence": "中",
                "evidence": [{"recording_id": "r1", "time": "08:00:00"}],
            }
        ]
    }
    write_json(out / "answers.json", answers)
    assert (
        exam.validate_answers(out / "answers.json", exam.read_json(paper / "meta.json")) == answers
    )
    write_json(
        out / "run.json",
        {
            "elapsed_s": 2,
            "usage": {"input_tokens": 5},
            "api_estimated_usd": 0.2,
            "answers_status": "valid",
        },
    )
    text = exam.report(paper).read_text()
    assert "ANSWER SECRET" in text and "example" in text and "人工评分" in text
    answers["answers"][0]["confidence"] = "unknown"
    write_json(out / "answers.json", answers)
    with pytest.raises(ValueError):
        exam.validate_answers(out / "answers.json", exam.read_json(paper / "meta.json"))


def test_meter_disconnect_and_missing_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sockets: exercise settlement even when the client disconnects before usage arrives."""
    captured = []

    class Response(io.BytesIO):
        status = 200

        def getheader(self, name: str, default: str = "") -> str:
            return "text/event-stream"

    class Connection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def request(self, *args: Any) -> None:
            captured.append(args[-1]["Authorization"])

        def getresponse(self) -> Response:
            return Response(Upstream.sse)

        def close(self) -> None:
            pass

    class Disconnected(io.BytesIO):
        def write(self, data: Any) -> int:
            raise BrokenPipeError

    monkeypatch.setattr(meter.http.client, "HTTPSConnection", Connection)
    state = meter.Meter("real-key", "one-time", 5, tmp_path / "log")
    handler = state.handler().__new__(state.handler())
    handler.path = "/v1/responses"
    payload = b'{"model":"gpt-6-astra"}'
    from email.message import Message

    handler.headers = Message()
    handler.headers["Authorization"] = "Bearer one-time"
    handler.headers["Content-Length"] = str(len(payload))
    handler.rfile = io.BytesIO(payload)
    handler.wfile = Disconnected()
    monkeypatch.setattr(handler, "send_response", lambda *args: None)
    monkeypatch.setattr(handler, "send_header", lambda *args: None)
    monkeypatch.setattr(handler, "end_headers", lambda: None)
    handler.do_POST()  # type: ignore[attr-defined]
    assert captured == ["Bearer real-key"]
    assert state.total == 20
    assert exam.read_json(state.log)["usage"] == {"input_tokens": 1000000}

    monkeypatch.setattr(Connection, "getresponse", lambda self: Response(b"data: [DONE]\n\n"))
    state.total = 0
    handler.rfile = io.BytesIO(payload)
    handler.do_POST()  # type: ignore[attr-defined]
    assert state.total == state.budget
    assert json.loads(state.log.read_text().splitlines()[-1])["uncertain"]


@pytest.mark.parametrize("timed_out", [False, True])
def test_run_results_and_timeout_cleanup(
    paper: Path, monkeypatch: pytest.MonkeyPatch, timed_out: bool
) -> None:
    monkeypatch.setenv("EARbaseline_key", "real-key")
    home = paper / "fake-home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: home)
    calls = []

    def invoke(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output = '{"ok": true}' if "/opt/exam/isolation.py" in cmd else "0"
        return subprocess.CompletedProcess(cmd, 0, output, "")

    def codex(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        mount = next(v for v in cmd if "dst=/work" in v)
        work = Path(mount.split("src=")[1].split(",dst=")[0])
        write_json(
            work / "answers.json",
            {"answers": [{"id": "B04", "answer": "无法回答", "evidence": [], "confidence": "低"}]},
        )
        kwargs["stdout"].write('{"usage":{"input_tokens":123,"output_tokens":4}}\n')
        (work.parent / "meter.jsonl").write_text('{"total_usd":0.25,"uncertain":false}\n')
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(exam, "invoke", invoke)
    monkeypatch.setattr(subprocess, "run", codex)
    out = exam.run(paper, "open", firewall=False)
    result = exam.read_json(out / "run.json")
    assert result["timed_out"] == timed_out
    assert result["exit_code"] == (None if timed_out else 0)
    assert result["usage"]["input_tokens"] == 123
    assert result["api_estimated_usd"] == 0.25
    assert result["answers_status"] == "valid"
    assert (out / "answers.json").exists()
    assert result["cleanup_errors"] == []
    assert calls[-1][:3] == ["docker", "network", "rm"]


def test_proxy_env_and_firewall_allow_rule_order(
    paper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARbaseline_key", "real-secret")
    monkeypatch.setattr(exam, "host_addresses", lambda: ["10.0.0.9"])
    home = paper / "fake-home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/auth.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: home)
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output = ""
        if cmd[1:3] == ["network", "inspect"]:
            output = json.dumps(
                [{"IPAM": {"Config": [{"Subnet": "172.25.0.0/16", "Gateway": "172.25.0.1"}]}}]
            )
        if "/opt/exam/isolation.py" in cmd:
            assert "10.0.0.9" in cmd[-1]
            output = '{"ok": false}'
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(exam, "invoke", fake)
    exam.run(paper, "closed", proxy="http://192.168.5.2:7897")
    added = [c for c in calls if "-I" in c]
    # 放行代理端口的规则最后插入（-I 放到最前），丢弃规则包括宿主局域网地址。
    assert added[-1][-6:] == ["-p", "tcp", "--dport", "7897", "-j", "ACCEPT"]
    assert any("10.0.0.9" in c for c in added)
    deleted = [c for c in calls if "-D" in c]
    assert deleted[0][-1] == "ACCEPT" and len(deleted) == len(added)
    for cmd in calls:
        if cmd[:2] == ["docker", "run"]:
            assert "HTTPS_PROXY=http://192.168.5.2:7897" in cmd
            assert "NO_PROXY=meter,localhost,127.0.0.1" in cmd


def test_missing_sensor_files_are_recorded_not_fatal(tmp_path: Path) -> None:
    audio = tmp_path / "segment_1_from_07-57-02_to_08-00-27__01__segment_0002.m4a"
    audio.write_bytes(b"a")
    imu = tmp_path / "segment_1_from_07-57-02_to_08-00-27__01__imu_0002.jsonl.gz"
    imu.write_bytes(b"i")
    files, missing = exam.raw_files(audio)
    assert files == [audio, imu]
    assert missing == ["context"]
