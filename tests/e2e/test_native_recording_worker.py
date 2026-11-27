"""Shared-spool HTTP producers -> packaged CLI -> loopback SDK -> MemoryStorage -> API.

No upstream legacy service, production identity, or authentic audio/model assets.
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from ear_adapter import native_worker_cli as cli
from ear_adapter.recording_intake import RecordingIntake
from ear_api.app import create_app as web_app
from ear_api.config import Settings
from ear_ingest.app import create_app as ingest_app
from ear_storage_client import MemoryStorage
from ear_storage_client.engine import canonical_json
from ear_storage_client.wire import decode_read, decode_request, encode_read_result, encode_result
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux native worker")
_START = "2026-01-01T23:59:58.500000Z"
_WEB_END = "2026-01-02T00:00:00.500438Z"  # 32007 frames, using round-to-even micros.
_ORIGIN = "http://testserver"
_ADAPTER = "w" * 43
_API_STORAGE = "s" * 43
_WEB = "u" * 43
_WEB_AUTH = {"Authorization": "Bearer " + _WEB}
_INGEST = "synthetic-phone-ingest"
_PROFILE = "ear-native/0.1.0+synthetic-worker-test"
_ROUTE = "/v1/recording-uploads"

_RECOGNIZER = r"""
import io, json, sys, wave
from datetime import datetime
from pathlib import Path
log, _, start, _, end = sys.argv[1:]
assert sys.argv[2::2] == ['--started-at', '--ended-at']
with wave.open(io.BytesIO(sys.stdin.buffer.read())) as wav:
    n = wav.getnframes()
    assert wav.getframerate() == 16000 and wav.getnchannels() == 1
    assert len(wav.readframes(n)) == n * 2
assert abs((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
           - n / 16000) <= 0.001
with Path(log).open('a') as output:
    print(n, file=output)
chunk = dict(audio_chunk_id='chunk', started_at=start, ended_at=end, model_version='fixture')
segment = dict(chunk, speech_segment_id='segment', diarization_scope_id='chunk',
               local_speaker_id='speaker', original_text='synthetic recording',
               language=None, overlap=False)
speaker = dict(resolution_id='resolution', diarization_scope_id='chunk', local_speaker_id='speaker',
               evidence_segment_ids=['segment'], embedding=None, sample_quality=None)
print(json.dumps(dict(status='complete', asr=dict(status='complete', chunks=[chunk],
                      segments=[segment]), speakers=[speaker], ambient=[])))
"""


def _wav(frames: int, sample: int = 1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, 16000, frames, "NONE", "not compressed"))
        wav.writeframes(sample.to_bytes(2, "little") * frames)
    return out.getvalue()


@pytest.fixture
def config(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    spool = tmp_path / "spool"
    scratch = tmp_path / "scratch"
    spool.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    code = tmp_path / "recognizer.py"
    code.write_text(_RECOGNIZER)
    path = tmp_path / "worker.json"
    path.touch(mode=0o600)
    return path, {
        "spool_root": str(spool),
        "scratch_dir": str(scratch),
        "offline_isolated": True,
        "adapter_version": _PROFILE,
        "launcher_argv": [sys.executable, "-I", "-B", str(code), str(tmp_path / "calls")],
        "ffmpeg_path": sys.executable,
        "ffprobe_path": sys.executable,  # never used for WAV
        "storage_url": "http://127.0.0.1:1",
        "storage_token": _ADAPTER,
    }


def _cli() -> Path:
    return Path(sys.executable).with_name("ear-native-worker")


class _StorageWire:
    def __init__(self, intake: RecordingIntake) -> None:
        self.app = FastAPI()
        self.memory = MemoryStorage()
        self.requests: list[str] = []
        self.in_write = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lose_receipt = False
        adapter = self.memory.client("adapter")
        api = self.memory.client("api")

        @self.app.get("/v1/identity")
        def identity(req: Request):
            match req.headers.get("authorization"):
                case v if v == "Bearer " + _ADAPTER:
                    return {"codec": 1, "module": "adapter"}
                case v if v == "Bearer " + _API_STORAGE:
                    return {"codec": 1, "module": "api"}
            return Response(status_code=401)

        @self.app.post("/v1/batches")
        async def write(req: Request):
            assert req.headers["authorization"] == "Bearer " + _ADAPTER
            value = await req.json()
            job = value.pop("request_id")
            state = intake.get(job)
            assert state is not None and state["state"] == "committing"
            assert job not in self.requests  # Detect retries, including unknown commit.
            self.requests.append(job)
            self.in_write.set()
            assert self.release.wait(8), "test did not release storage write"
            ops, context = decode_request(canonical_json(value))
            result = adapter.write(ops, context)
            assert result.ok
            if self.lose_receipt:
                return Response("synthetic private error", status_code=503)
            return Response(encode_result(result), media_type="application/json")

        @self.app.post("/v1/read")
        async def read(req: Request):
            assert req.headers["authorization"] == "Bearer " + _API_STORAGE
            parsed, args = decode_read(await req.body())
            result = getattr(api, parsed.method.value)(**args)
            return Response(
                encode_read_result(parsed.method, result), media_type="application/json"
            )


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(300):
                if server.started:
                    break
                if not thread.is_alive():
                    pytest.fail("fixture server died")
                time.sleep(0.01)
            else:
                pytest.fail("fixture server startup timeout")
            yield address
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "fixture server shutdown timeout"


def _worker_env(path: Path) -> dict[str, str]:
    # A bogus CA and proxy in the child must not affect its SDK connection.
    env = dict(os.environ)
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)
    return env | {
        "SSL_CERT_FILE": str(path.with_name("bogus-ca")),
        "HTTP_PROXY": "http://127.0.0.1:1",
    }


def _once(path: Path, *flags: str) -> str:
    completed = subprocess.run(
        [_cli(), "--config", path, *flags], capture_output=True, timeout=12, env=_worker_env(path)
    )
    assert completed.returncode == 0 and not completed.stderr, completed
    return json.loads(completed.stdout)["status"]


def _phone(ingest: TestClient, *, milliseconds: int, sample: int, frames: int) -> str:
    form = {
        "name": "PhoneVoice",
        "text": "",
        "type": "richVoice",
        "source": "local",
        "group_id": f"00000000-0000-4000-8000-{sample:012}",
        "timestamp": _START,
        "segment_start_ms": "0",
        "segment_end_ms": str(milliseconds),
        "segment_meta": json.dumps(
            {"segmentIndex": 0, "segmentStartMs": 0, "segmentEndMs": milliseconds}
        ),
    }
    reply = ingest.post(
        "/api/v1/record",
        data=form,
        files={"file": ("fixture.wav", _wav(frames, sample), "audio/wav")},
        headers={"Authorization": "Bearer " + _INGEST, "X-Device-Id": "synthetic-test"},
    )
    assert reply.is_success, reply.text
    return reply.json()["record_id"]


def test_packaged_worker_once_watch_and_authoritative_reads(config, tmp_path, monkeypatch):
    path, value = config
    root = tmp_path / "spool"
    intake = RecordingIntake(root)
    wire = _StorageWire(intake)
    monkeypatch.setenv("EAR_INGEST_NATIVE_ROOT", str(root))
    monkeypatch.setenv("EAR_INGEST_TOKEN", _INGEST)
    monkeypatch.setenv("EAR_INGEST_INBOX_ROOT", str(tmp_path / "must-not-exist"))
    for name in ("EAR_INGEST_EVENT_URL", "EAR_INGEST_NOTIFY_URL", "EAR_INGEST_NATIVE_MAX_BYTES"):
        monkeypatch.delenv(name, raising=False)
    with _serve(wire.app) as url:
        value["storage_url"] = url
        path.write_text(json.dumps(value))
        web_config = Settings.model_validate(
            {
                "storage_url": url,
                "public_origin": _ORIGIN,
                "recording_intake_root": root,
                "recording_intake_owner": "owner",
                "bindings": [
                    {
                        "subject": "owner",
                        "role": "user",
                        "token": _WEB,
                        "storage_token": _API_STORAGE,
                    }
                ],
            }
        )
        with TestClient(web_app(web_config)) as web, TestClient(ingest_app()) as ingest:
            reply = web.post(
                _ROUTE,
                headers={
                    **_WEB_AUTH,
                    "Origin": _ORIGIN,
                    "Content-Type": "audio/wav",
                    "X-Recording-Started-At": _START,
                },
                content=_wav(32007),
            )
            assert reply.status_code == 202, reply.text
            web_job = reply.json()["id"]
            phone_job = _phone(ingest, milliseconds=2001, sample=2, frames=32007)  # within 1 ms
            bad_phone = _phone(ingest, milliseconds=2010, sample=3, frames=32007)
            assert _once(path) == "processed"
            assert wire.requests == [web_job]
            assert web.get(f"{_ROUTE}/{phone_job}", headers=_WEB_AUTH).json()["state"] == "queued"
            assert web.get(f"{_ROUTE}/{web_job}", headers=_WEB_AUTH).json()["state"] == "succeeded"

            wire.in_write.clear()
            wire.release.clear()
            with subprocess.Popen(
                [_cli(), "--config", path, "--watch"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_worker_env(path),
            ) as proc:
                try:
                    assert wire.in_write.wait(6), "watch did not reach commit"
                    proc.send_signal(signal.SIGTERM)
                finally:
                    wire.release.set()
                    stdout, stderr = proc.communicate(timeout=12)
                assert proc.returncode == 0 and not stderr
                assert json.loads(stdout)["status"] == "stopped"
            invalid = intake.get(bad_phone)
            assert invalid is not None and invalid["state"] == "queued"
            assert _once(path, "--once") == "processed"
            invalid = intake.get(bad_phone)
            assert invalid is not None and invalid["error"] == "processing_failed"
            assert _once(path, "--once") == "idle"
            assert wire.requests == [web_job, phone_job]
            assert (tmp_path / "calls").read_text().splitlines() == ["32007", "32007"]

            assert 'href="/recording-upload/"' in web.get("/results/").text
            assert web.get("/recording-upload/").is_success
            refs = web.get("/v1/records/audio_ref", headers=_WEB_AUTH).json()["items"]
            assert {r["payload"]["upstream"]["capture_id"] for r in refs} == {web_job, phone_job}
            assert {r["event_end"] for r in refs} == {_WEB_END, "2026-01-02T00:00:00.501000Z"}
            assert all(r["adapter_version"] == _PROFILE for r in refs)
            transcripts = web.get("/v1/records/transcript_segment", headers=_WEB_AUTH).json()[
                "items"
            ]
            assert len(transcripts) == 2
            assert {r["payload"]["audio_ref_id"] for r in transcripts} == {r["id"] for r in refs}
            assert all(r["payload"]["text"] == "synthetic recording" for r in transcripts)
    assert not (tmp_path / "must-not-exist").exists()
    assert not list((tmp_path / "scratch").iterdir())


def test_committed_but_no_receipt_is_not_replayed(config, tmp_path):
    path, value = config
    intake = RecordingIntake(tmp_path / "spool")
    job = intake.accept(
        io.BytesIO(_wav(32000)),
        source="web",
        source_id=None,
        started_at=_START,
        media_type="audio/wav",
    )
    wire = _StorageWire(intake)
    wire.lose_receipt = True
    with _serve(wire.app) as url:
        value["storage_url"] = url
        path.write_text(json.dumps(value))
        assert _once(path) == "processed"
        assert intake.get(job.id) == {
            "id": job.id,
            "state": "blocked",
            "error": "commit_unknown",
            "attempts": 1,
        }
        assert _once(path) == "idle"
    assert wire.requests == [job.id]
    assert (tmp_path / "calls").read_text() == "32000\n"


@pytest.mark.parametrize(
    "field,value",
    [
        ("spool_root", "relative"),
        ("storage_url", "http://storage.test"),
        ("storage_url", "https://storage.test/subpath"),
        ("storage_url", "https://storage.test?x=z"),
        ("adapter_version", "ear-native/0.1.0"),
        ("offline_isolated", 1),
        ("launcher_argv", ["ffmpeg"]),
        ("launcher_argv", ["/bin/echo", "--started-at=x"]),
        ("ffprobe_path", "https://decoder.test/ffprobe"),
        ("unknown", True),
    ],
)
def test_rejects_bad_settings_without_printing_private_config(config, field, value, capsys):
    path, settings = config
    settings[field] = value
    path.write_text(json.dumps(settings))
    assert cli.main(["--config", str(path)]) == 2
    out = capsys.readouterr()
    assert not out.err and str(path) not in out.out and _ADAPTER not in out.out
    assert json.loads(out.out)["status"] == "invalid_config"


def test_config_file_guards(config, tmp_path):
    path, settings = config
    assert cli.main([]) == 2
    assert cli.main(["--config", str(path), "--watch", "--once"]) == 2
    path.write_text(json.dumps(settings))
    assert cli._settings(str(path)).version == _PROFILE
    path.chmod(0o644)
    assert cli.main(["--config", str(path)]) == 2
    path.chmod(0o600)
    other = tmp_path / "other.json"
    os.link(path, other)
    assert cli.main(["--config", str(path)]) == 2
    other.unlink()
    other.symlink_to(path)
    assert cli.main(["--config", str(other)]) == 2
    path.write_text(json.dumps(settings).replace("{", '{"offline_isolated":true,', 1))
    assert cli.main(["--config", str(path)]) == 2
