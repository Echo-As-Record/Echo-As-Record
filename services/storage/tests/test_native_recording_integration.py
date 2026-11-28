"""Native phone -> shared spool <- web, then real Storage, API, and JetStream.

Run with EAR_RUN_POSTGRES_TESTS=1 EAR_RUN_NATS_TESTS=1 (fixture images only).
The WAV case uses no FFmpeg or model packages; AAC also runs when the configured
FFmpeg/ffprobe on PATH support the seekable fd protocol. No legacy system.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
import wave
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from ear_api.app import create_app as api_app
from ear_api.config import Settings
from ear_contracts import RawRecord, format_utc
from ear_ingest.app import create_app as ingest_app
from ear_storage import PostgresStorage, migrate
from ear_storage.auth import authenticate, token_digest
from ear_storage.core_deployment import bootstrap_core, init_core_identities
from ear_storage.http import create_app as storage_app
from ear_storage.relay import RelayConfig, RelayStore, run_relay
from ear_storage.schema import connection
from ear_storage.service import StorageService
from ear_storage_client import PermissionDenied, Query
from ear_storage_client.remote import RemoteStorageClient
from nats.js.errors import NotFoundError

from .test_api_gateway_integration import headers, loopback
from .test_relay import reader

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.nats,
    pytest.mark.skipif(sys.platform != "linux", reason="Linux native worker"),
]
START = datetime(2026, 1, 1, 23, 59, 58, 500000, tzinfo=UTC)
END = START + timedelta(minutes=30)
PROFILE = "ear-native/0.1.0+synthetic-postgres-nats"
PHONE_DEVICE = "synthetic-earrecorder-storage"
ROUTE = "/v1/recording-uploads"
ADAPTER_READS = {
    "audio_ref",
    "transcript_segment",
    "local_speaker",
    "voiceprint",
    "ambient_sound",
    "gps_point",
    "gps_geocode",
    "phone_info",
}
RECOGNIZER = """
import io, json, sys, wave
from datetime import datetime
from pathlib import Path
assert set(__import__('os').environ) <= {'LC_CTYPE'}
log, encoding, start_flag, start, end_flag, end = sys.argv[1:]
assert (start_flag, end_flag) == ('--started-at', '--ended-at')
with wave.open(io.BytesIO(sys.stdin.buffer.read())) as wav:
    assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
    assert wav.getnframes() == 480000
    pcm = wav.readframes(480000)
assert len(pcm) == 960000 and any(pcm)
assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() == 30
if encoding == 'wav':
    index = ((datetime.fromisoformat(start) - datetime.fromisoformat(
        '2026-01-01T23:59:58.500000Z')).total_seconds() / 30)
    assert pcm == (100 + int(index)).to_bytes(2, 'little') * 480000
with Path(log).open('a') as stream:
    print(json.dumps([start, end]), file=stream)
chunk = dict(audio_chunk_id='chunk', model_version='fixture', started_at=start, ended_at=end)
segment = dict(chunk, speech_segment_id='segment', diarization_scope_id='chunk',
               local_speaker_id='speaker', original_text='synthetic recording',
               language='en', overlap=False)
speaker = dict(resolution_id='resolution', diarization_scope_id='chunk', local_speaker_id='speaker',
               evidence_segment_ids=['segment'], embedding=None, sample_quality=None)
print(json.dumps(dict(status='complete', asr=dict(status='complete', chunks=[chunk],
                      segments=[segment]), speakers=[speaker], ambient=[])))
"""


def _audio(tmp_path, encoding):
    path = tmp_path / "phone.wav"
    ffmpeg = ffprobe = sys.executable  # The supported canonical WAV path does not invoke these.
    if encoding == "wav":
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            for window in range(60):
                wav.writeframesraw((100 + window).to_bytes(2, "little") * 480000)
        return path, "audio/wav", ffmpeg, ffprobe
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("AAC requires local FFmpeg and ffprobe")
    for binary in (ffmpeg, ffprobe):
        protocols = subprocess.check_output(
            [binary, "-hide_banner", "-protocols"], env={}, timeout=5
        )
        if b"fd" not in protocols.split():
            pytest.skip("AAC requires seekable FFmpeg/ffprobe fd protocol")
    path = tmp_path / "phone.m4a"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-filter_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=1800",
            "-c:a",
            "aac",
            "-threads",
            "1",
            "-aac_coder",
            "fast",
            "-b:a",
            "24k",
            str(path),
        ],
        check=True,
        env={},
        timeout=120,
    )
    return path, "audio/mp4", ffmpeg, ffprobe


def _post_phone(http, path, media, token):
    form = {
        "name": "PhoneVoice",
        "text": "",
        "type": "richVoice",
        "source": "local",
        "group_id": "00000000-0000-4000-8000-000000000001",
        "timestamp": "2026-01-01T16:59:28.500000-07:00",
        "segment_start_ms": "30000",
        "segment_end_ms": "1830000",
        "segment_meta": json.dumps(
            {
                "segmentIndex": 1,
                "segmentStartMs": 30000,
                "segmentEndMs": 1830000,
            }
        ),
    }
    with path.open("rb") as f:
        response = http.post(
            "/api/v1/record",
            data=form,
            files={"file": (path.name, f, media)},
            headers=headers(token) | {"X-Device-Id": PHONE_DEVICE},
        )
    assert response.status_code == 200, response.text
    return response.json()["record_id"]


def _once(config, settings):
    config.write_text(json.dumps(settings))
    completed = subprocess.run(
        [Path(sys.executable).with_name("ear-native-worker"), "--config", config, "--once"],
        capture_output=True,
        timeout=60,
        env={},
    )
    assert completed.returncode == 0 and not completed.stderr, completed.stdout
    return json.loads(completed.stdout)["status"]


def _records(web, web_auth, dtype):
    response = web.get(f"/v1/records/{dtype}", params={"limit": 100}, headers=web_auth)
    assert response.status_code == 200, response.text
    page = response.json()
    items = page.pop("items")
    assert page == {"next_cursor": None, "consistency": "live"}
    for item in items:
        assert RawRecord.model_validate(item).data_type == dtype
        assert item["source"] == {
            "device": "recording_intake",
            "channel": "recording_intake",
            "user": "default",
        }
        assert item["adapter_version"] == PROFILE and item["version"] == 1
    return items


def _check_reads(web, auth, job, path, media):
    (audio,) = _records(web, auth, "audio_ref")
    assert (audio["event_start"], audio["event_end"]) == (format_utc(START), format_utc(END))
    with path.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    assert audio["payload"] == {
        "file_ref": {
            "path": f"blobs/{digest}{path.suffix}",
            "sha256": digest,
            "size": path.stat().st_size,
        },
        "media_type": media,
        "upstream": {"service": "ear-native", "capture_id": job, "record_id": job, "group_id": job},
    }
    audio_url = f"/v1/records/audio_ref/{audio['id']}"
    detail = web.get(audio_url, headers=auth)
    assert detail.status_code == 200 and detail.json() == audio
    assert web.get(audio_url).status_code == 401
    intervals = web.get(
        "/v1/recording-intervals",
        headers=auth,
        params={"start": format_utc(START), "end": format_utc(END)},
    )
    assert intervals.status_code == 200, intervals.text
    assert intervals.json() == {
        "items": [{"start": format_utc(START), "end": format_utc(END)}],
        "next_cursor": None,
    }
    transcripts = sorted(_records(web, auth, "transcript_segment"), key=lambda r: r["event_start"])
    speakers = {s["id"]: s for s in _records(web, auth, "local_speaker")}
    assert len(transcripts) == len(speakers) == 60
    assert _records(web, auth, "voiceprint") == _records(web, auth, "ambient_sound") == []
    for i, segment in enumerate(transcripts):
        bounds = tuple(format_utc(START + timedelta(seconds=30 * j)) for j in (i, i + 1))
        payload = segment["payload"]
        speaker = speakers.pop(payload["local_speaker_id"])
        assert (segment["event_start"], segment["event_end"]) == bounds
        assert (speaker["event_start"], speaker["event_end"]) == bounds
        assert payload == {
            "text": "synthetic recording",
            "language": "en",
            "local_speaker_id": speaker["id"],
            "voiceprint_id": None,
            "overlap": False,
            "audio_ref_id": audio["id"],
            "model_version": "fixture",
            "upstream": payload["upstream"],
        }
        assert set(payload["upstream"]) == {"speech_segment_id", "audio_chunk_id"}
        assert speaker["payload"] == {
            "audio_ref_id": audio["id"],
            "label": "speaker",
            "upstream": {"diarization_scope_id": payload["upstream"]["audio_chunk_id"]},
        }
    around_midnight = web.get(
        "/v1/records/transcript_segment",
        headers=auth,
        params={"start": "2026-01-02T00:00:00Z", "end": transcripts[0]["event_end"]},
    )
    assert around_midnight.status_code == 200
    assert around_midnight.json()["items"] == transcripts[:1]  # half-open window boundary


async def _check_relay(dsn, relay, saved):
    try:
        assert (await run_relay(RelayStore(dsn, relay), once=True)).delivered == 121
        assert (await run_relay(RelayStore(dsn, relay), once=True)).delivered == 0
        async with reader(relay) as js:
            assert (await js.stream_info(relay.stream)).state.messages == 121
            for seq, event in enumerate(saved, 1):
                message = await js.get_msg(relay.stream, seq=seq)
                assert message.data is not None
                assert json.loads(message.data) == event.model_dump(mode="json")
    finally:
        async with reader(relay) as js:
            with suppress(NotFoundError):
                await js.delete_stream(relay.stream)


@pytest.mark.parametrize("encoding", ["wav", "aac"])
def test_native_phone_web_duplicate_reaches_official_reads(
    pg_dsn, nats_server, tmp_path, monkeypatch, encoding
):
    tmp_path = tmp_path.resolve()  # The worker forbids symlinked directory components.
    path, media, ffmpeg, ffprobe = _audio(tmp_path, encoding)
    migrate(pg_dsn)
    assert bootstrap_core(pg_dsn)
    bundle = tmp_path / "identities" / "identities.json"
    assert init_core_identities(pg_dsn, bundle)
    credentials = json.loads(bundle.read_text())["credentials"]
    adapter = credentials["adapter"]
    web_token, phone_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with connection(pg_dsn) as conn:
        assert set(
            conn.execute(
                "SELECT data_type,can_subscribe FROM storage.type_grants WHERE principal_id=%s",
                (adapter["principal_id"],),
            )
        ) == {(name, False) for name in ADAPTER_READS}
        principal = authenticate(conn, token_digest(adapter["token"]))
        assert principal.module == "adapter" and not principal.admin
        assert principal.read_types == ADAPTER_READS and not principal.subscribe_types
    suffix = uuid.uuid4().hex
    relay = RelayConfig(
        servers=(nats_server.url,),
        token=nats_server.token,
        stream="EAR_IT_" + suffix,
        prefix="ear_it_" + suffix,
        batch_size=128,
        max_bytes=1024 * 1024,
    )
    spool, scratch = tmp_path / "spool", tmp_path / "scratch"
    spool.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    recognizer, call_log = tmp_path / "recognizer.py", tmp_path / "calls"
    recognizer.write_text(RECOGNIZER)
    worker_config = tmp_path / "worker.json"
    worker_config.touch(mode=0o600)
    for key in list(os.environ):
        if key.startswith("EAR_INGEST_") or key == "EAR_TRANSCRIPTION_URL":
            monkeypatch.delenv(key)
    monkeypatch.setenv("EAR_INGEST_NATIVE_ROOT", str(spool))
    monkeypatch.setenv("EAR_INGEST_NATIVE_PHONE_DEVICE_ID", PHONE_DEVICE)
    monkeypatch.setenv("EAR_INGEST_NATIVE_PHONE_AFTER", format_utc(START))
    monkeypatch.setenv("EAR_INGEST_TOKEN", phone_token)
    monkeypatch.setenv("EAR_INGEST_INBOX_ROOT", str(tmp_path / "must-not-exist"))

    with loopback(storage_app(StorageService(pg_dsn, relay_config=relay))) as storage_url:
        with RemoteStorageClient(adapter["token"], storage_url, module="adapter") as client:
            assert client.module == "adapter"
            assert client.count(Query("audio_ref")) == 0
            with pytest.raises(PermissionDenied):
                client.count(Query("person"))

        def web_app(origin):
            return api_app(
                Settings.model_validate(
                    {
                        "storage_url": storage_url,
                        "recording_intake_root": spool,
                        "public_origin": origin,
                        "bindings": [
                            {
                                "subject": "owner",
                                "role": "user",
                                "token": web_token,
                                "storage_token": credentials["api_user"]["token"],
                            }
                        ],
                    }
                )
            )

        with (
            loopback(app_for_origin=web_app) as origin,
            loopback(ingest_app()) as ingest_url,
            httpx.Client(base_url=origin, timeout=30, trust_env=False) as web,
            httpx.Client(base_url=ingest_url, timeout=30, trust_env=False) as ingest,
        ):
            job = _post_phone(ingest, path, media, phone_token)
            auth = headers(web_token)

            def web_duplicate(state):
                with path.open("rb") as f:
                    duplicate = web.post(
                        ROUTE,
                        headers=auth
                        | {
                            "Origin": origin,
                            "Content-Type": media,
                            "Content-Length": str(path.stat().st_size),
                            "X-Recording-Started-At": "2026-01-01T16:59:58.500000-07:00",
                        },
                        content=iter(lambda: f.read(1 << 20), b""),
                    )
                assert duplicate.status_code == 200, duplicate.text
                assert duplicate.json() == {"id": job, "state": state, "duplicate": True}

            web_duplicate("queued")
            status = web.get(f"{ROUTE}/{job}", headers=auth)
            assert status.status_code == 200
            assert status.json() == {"id": job, "state": "queued", "attempts": 0, "error": None}
            assert _records(web, auth, "audio_ref") == []
            settings = {
                "spool_root": str(spool),
                "scratch_dir": str(scratch),
                "offline_isolated": True,
                "adapter_version": PROFILE,
                "launcher_argv": [
                    sys.executable,
                    "-I",
                    "-B",
                    str(recognizer),
                    str(call_log),
                    encoding,
                ],
                "ffmpeg_path": ffmpeg,
                "ffprobe_path": ffprobe,
                "storage_url": storage_url,
                "storage_token": adapter["token"],
            }
            assert _once(worker_config, settings) == "processed"
            status = web.get(f"{ROUTE}/{job}", headers=auth)
            assert status.status_code == 200
            assert status.json() == {"id": job, "state": "succeeded", "attempts": 1, "error": None}
            web_duplicate("succeeded")
            assert _post_phone(ingest, path, media, phone_token) == job
            assert _once(worker_config, settings) == "idle"
            _check_reads(web, auth, job, path, media)
    assert [json.loads(line) for line in call_log.read_text().splitlines()] == [
        [
            format_utc(START + timedelta(seconds=30 * i)),
            format_utc(START + timedelta(seconds=30 * (i + 1))),
        ]
        for i in range(60)
    ]
    [(sequence, events)] = PostgresStorage(pg_dsn).outbox()
    assert sequence == 1 and len(events) == 121
    assert all(e.root_feedback_id == "system" and e.depth == 0 for e in events)
    asyncio.run(_check_relay(pg_dsn, relay, events))
    assert not list(scratch.iterdir()) and not (tmp_path / "must-not-exist").exists()


_DOCKER_UPLOAD = """
import io, json, os, socket, sys, threading, time, wave
import httpx
import uvicorn
from ear_api.app import create_app
from ear_api.config import Settings
assert os.geteuid() == 10001
assert os.statvfs('/tmp').f_bavail * os.statvfs('/tmp').f_frsize == 16 << 20
config, token = json.load(sys.stdin)
with socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP) as sock:
    sock.bind(('127.0.0.1', 0))
    sock.listen(32)
    origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    app = create_app(Settings.model_validate(config | {'public_origin': origin}))
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=origin, timeout=20, trust_env=False) as web:
            frames = 1800 * 16000
            audio = io.BytesIO()
            with wave.open(audio, 'wb') as wav:
                wav.setparams((1, 2, 16000, frames, 'NONE', 'not compressed'))
                wav.writeframes(b'\\x01\\x00' * frames)
            audio.seek(0)
            req = web.post('/v1/recording-uploads', headers={
                'Authorization': 'Bearer ' + token, 'Origin': origin,
                'X-Recording-Started-At': '2026-01-01T23:59:58.500000Z',
                'Content-Type': 'audio/wav', 'Content-Length': str(44 + frames * 2),
            }, content=iter(lambda: audio.read(1 << 20), b''))
            print(json.dumps({'status': req.status_code, 'body': req.json()}))
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()
"""


@pytest.mark.skipif(not os.environ.get("EAR_NATIVE_TEST_API_IMAGE"), reason="requires API image")
def test_30_minute_web_upload_fits_spool_with_small_container_tmp(pg_dsn, tmp_path):
    """Exercise the non-editable Dockerfile.api build under the overlay's /tmp limit."""
    migrate(pg_dsn)
    assert bootstrap_core(pg_dsn)
    bundle = tmp_path.resolve() / "identities" / "identities.json"
    assert init_core_identities(pg_dsn, bundle)
    credentials = json.loads(bundle.read_text())["credentials"]
    token = secrets.token_urlsafe(32)
    name = "ear-native-api-it-" + uuid.uuid4().hex
    try:
        with loopback(storage_app(StorageService(pg_dsn))) as storage_url:
            config = {
                "storage_url": storage_url,
                "recording_intake_root": "/state/spool",
                "bindings": [
                    {
                        "subject": "owner",
                        "role": "user",
                        "token": token,
                        "storage_token": credentials["api_user"]["token"],
                    }
                ],
            }
            # Host net for the isolated Storage fixture, both listeners bound to loopback.
            # tmpfs simulates a roomy independent volume; there are no host data mounts.
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--pull=never",
                    "--network=host",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--memory=256m",
                    "--tmpfs=/tmp:size=16m,noexec,nosuid",
                    "--pids-limit=128",
                    "--tmpfs=/state:size=192m,uid=10001,gid=10001,mode=0700,nosuid,nodev",
                    "-i",
                    "--entrypoint=python",
                    os.environ["EAR_NATIVE_TEST_API_IMAGE"],
                    "-c",
                    _DOCKER_UPLOAD,
                ],
                input=json.dumps([config, token]),
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, "API image probe could not run"
            outcome = json.loads(result.stdout)
            assert outcome["status"] == 202, outcome
            assert set(outcome["body"]) == {"id", "duplicate", "state"}
            assert outcome["body"] | {"id": None} == {
                "id": None,
                "duplicate": False,
                "state": "queued",
            }
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
