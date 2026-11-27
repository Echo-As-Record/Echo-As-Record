"""HTTP tests over a real private spool; Storage wire authentication is simulated."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import wave
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from ear_adapter.recording_intake import IntakeStorageError, RecordingIntake
from ear_api.app import create_app
from ear_api.upstream import Upstream
from ear_contracts import new_id
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.types import Message, Scope

from .conftest import ADMIN, STORAGE_USER, USER, headers, settings

ROUTE = "/v1/recording-uploads"
PAGE = "/recording-upload/"
ORIGIN = "http://testserver"
TIME = "2026-11-20T18:01:02-07:00"
UPLOAD = {
    **headers(),
    "Origin": ORIGIN,
    "Content-Type": "audio/wav",
    "X-Recording-Started-At": TIME,
}
OWNER = {"subject": "owner", "role": "user", "token": USER, "storage_token": STORAGE_USER}
SECOND = {"subject": "other", "role": "user", "token": "b" * 43, "storage_token": "d" * 43}


def wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\0" * 128_000)
    return out.getvalue()


class Wire:
    def __init__(self) -> None:
        self.calls = 0
        self.revoke = False
        self.deny_at: int | None = None

    def reply(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert req.url.path == "/v1/identity"
        assert req.headers["authorization"] == "Bearer " + STORAGE_USER
        if self.revoke or self.deny_at == self.calls:
            return httpx.Response(401)
        return httpx.Response(200, json={"codec": 1, "module": "api"})


def gateway(root: Path, **kwargs):
    config = settings(
        "http://upstream.invalid",
        **{"recording_intake_root": root, "recording_intake_owner": "owner", **kwargs},
    )
    wire = Wire()
    return create_app(config, upstream=Upstream(config, httpx.MockTransport(wire.reply))), wire


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, Wire, Path]]:
    root = tmp_path / "recordings"
    app, wire = gateway(root)
    with TestClient(app) as client:
        yield client, wire, root


def test_raw_wav_durable_duplicate_conflict_and_worker_states(api):
    client, wire, root = api
    data = wav()  # deliberately exceeds the ordinary JSON cap
    accepted = client.post(ROUTE, headers=UPLOAD, content=data)
    assert accepted.status_code == 202
    job = accepted.json()
    assert job == {"id": job["id"], "state": "queued", "duplicate": False}
    assert wire.calls == 2
    status_url = ROUTE + "/" + job["id"]
    status = client.get(status_url, headers=headers())
    assert status.headers["cache-control"] == "no-store"
    assert status.json() == {"id": job["id"], "state": "queued", "attempts": 0, "error": None}
    again = client.post(
        ROUTE,
        headers={**UPLOAD, "X-Recording-Started-At": "2026-11-21T01:01:02Z"},
        content=iter((data[:25000], data[25000:])),
    )
    assert again.status_code == 200
    assert again.json() == {**job, "duplicate": True}
    conflict = client.post(
        ROUTE, headers={**UPLOAD, "X-Recording-Started-At": "2026-11-21T01:01:03Z"}, content=data
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"code": "recording_conflict"}
    assert len(list((root / "blobs").iterdir())) == 1
    with RecordingIntake(root).worker() as worker:
        claim = worker.claim()
        assert claim and claim.started_at == "2026-11-21T01:01:02Z"
        assert claim.ended_at is None
        assert claim.id == job["id"] and claim.attempt == 1
        with worker.open_audio(claim) as audio:
            assert audio.read() == data
        assert client.get(status_url, headers=headers()).json()["state"] == "processing"
        worker.save_result(claim, {"private_path": str(root)})
        worker.begin_commit(claim)
        assert client.get(status_url, headers=headers()).json()["state"] == "committing"
        worker.finish(claim, state="blocked")
    result = client.get(status_url, headers=headers()).json()
    assert result == {"id": job["id"], "state": "blocked", "attempts": 1, "error": "commit_unknown"}
    assert client.get(ROUTE + "/" + new_id(), headers=headers()).status_code == 404


def test_compatible_m4a_and_invalid_container(api):
    client, _, root = api
    m4a = b"\0\0\0\x10ftypM4A \0\0\0\0\0\0\0\x08mdat"
    accepted = client.post(ROUTE, headers={**UPLOAD, "Content-Type": "audio/x-m4a"}, content=m4a)
    assert accepted.status_code == 202
    with RecordingIntake(root).worker() as worker:
        claim = worker.claim()
        assert claim and claim.media_type == "audio/mp4"
    bad = client.post(ROUTE, headers=UPLOAD, content=m4a)
    assert bad.status_code == 422 and bad.json() == {"code": "invalid_recording"}
    assert len(list((root / "blobs").iterdir())) == 1


def test_only_configured_owner_can_access_shared_audio_status(tmp_path):
    app, wire = gateway(tmp_path / "spool", bindings=[OWNER, SECOND])
    intake = RecordingIntake(tmp_path / "spool")
    phone = intake.accept(
        io.BytesIO(wav()),
        source="phone",
        source_id="phone-1",
        started_at=TIME,
        media_type="audio/wav",
    )
    context = intake.accept(
        io.BytesIO(gzip.compress(b"{}\n")),
        source="phone",
        source_id="context-1",
        started_at=TIME,
        media_type="application/gzip",
        kind="context",
    )
    with TestClient(app) as client:
        status = ROUTE + "/" + phone.id
        other = {"Authorization": "Bearer " + SECOND["token"]}
        for auth, expected in (({}, 401), (other, 403), ({"Cookie": "token=" + USER}, 401)):
            assert client.get(status, headers=auth).status_code == expected
            post_headers = {k: v for k, v in UPLOAD.items() if k != "Authorization"} | auth
            assert client.post(ROUTE, headers=post_headers, content=wav()).status_code == expected
        assert wire.calls == 0
        assert client.get(status, headers=headers()).status_code == 200
        assert client.get(ROUTE + "/" + context.id, headers=headers()).status_code == 404
        wire.revoke = True
        assert client.get(status, headers=headers()).status_code == 401
        assert client.post(ROUTE, headers=UPLOAD, content=wav()).status_code == 401


def test_post_upload_reverification_prevents_publication_after_revocation(api):
    client, wire, root = api
    wire.deny_at = 2
    assert client.post(ROUTE, headers=UPLOAD, content=wav()).status_code == 401
    assert not list((root / "blobs").iterdir())


def test_owner_config_validation_and_protected_path(tmp_path):
    root = tmp_path / "spool"
    url = "http://upstream.invalid"
    base = {"recording_intake_root": root}
    owner = settings(url, **base, bindings=[OWNER]).recording_owner
    assert owner is not None and owner.subject == "owner"
    for overrides in (
        {},
        {"recording_intake_owner": "admin"},
        {"recording_intake_owner": "missing"},
        {"bindings": [OWNER, SECOND]},
        {"bindings": [OWNER], "public_origin": None},
        {"bindings": [OWNER], "recording_intake_root": Path("relative")},
        {"bindings": [OWNER], "static_dir": tmp_path},
        {"bindings": [OWNER], "recording_upload_timeout": 0},
        {"bindings": [OWNER], "recording_upload_max_bytes": (512 << 20) + 1},
        {"recording_intake_root": None, "recording_intake_owner": "owner"},
    ):
        with pytest.raises(ValidationError):
            settings(url, **(base | overrides))
    root.mkdir(mode=0o755)
    with pytest.raises(IntakeStorageError):
        create_app(settings(url, **base, bindings=[OWNER]))


def test_test_no_auth_keeps_fixed_user_binding_and_loopback_guards(tmp_path):
    loopback = "http://127.0.0.1:8086"
    app, wire = gateway(
        tmp_path / "spool", public_origin=loopback, test_no_auth=True, test_no_auth_role="admin"
    )
    with TestClient(app, base_url=loopback) as client:
        h = {k: v for k, v in UPLOAD.items() if k != "Authorization"} | {"Origin": loopback}
        assert client.get("/v1/session").json() == {"test_no_auth": True, "recording_upload": True}
        assert (
            client.post(ROUTE, headers={**h, "Host": "evil.invalid"}, content=wav()).status_code
            == 403
        )
        assert (
            client.post(
                ROUTE, headers={**h, "Sec-Fetch-Site": "same-site"}, content=wav()
            ).status_code
            == 403
        )
        accepted = client.post(ROUTE, headers=h, content=wav())
        assert accepted.status_code == 202 and wire.calls == 2
        status = ROUTE + "/" + accepted.json()["id"]
        # Caller cannot override the configured business binding, even with an admin token.
        assert client.get(status, headers={"Authorization": "Bearer " + ADMIN}).status_code == 200
        assert client.get(status, headers={"Origin": "null"}).status_code == 403


def test_page_assets_opt_in_and_ordinary_json_limit(api):
    client, _, _ = api
    assert client.get("/v1/session").json() == {"test_no_auth": False, "recording_upload": True}
    page = client.get(PAGE)
    assert page.status_code == 200
    assert 'type="datetime-local"' in page.text and 'id="offset"' in page.text
    assert 'type="file"' in page.text and 'id="job-id"' in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get(PAGE + "recording-upload.js").status_code == 200
    assert client.get(PAGE + "recording-upload.css").status_code == 200
    assert client.get(PAGE + "app.js").status_code == 404
    assert client.get("/plugins/recording-upload.js").status_code == 404
    assert 'href="/recording-upload/"' in client.get("/results/").text
    reply = client.post("/v1/feedback", headers=headers(), json={"large": "a" * 65536})
    assert reply.status_code == 413 and reply.json() == {"code": "body_too_large"}
    with TestClient(create_app(settings("http://upstream.invalid"))) as disabled:
        assert disabled.get("/v1/session").json() == {"test_no_auth": False}
        assert disabled.get(PAGE).status_code == 404
        assert disabled.post(ROUTE, headers=UPLOAD, content=b"audio").status_code == 404
        assert disabled.get(ROUTE + "/" + new_id(), headers=headers()).status_code == 404


async def send_upload(
    app: FastAPI,
    request_headers: list[tuple[str, str]],
    messages: list[Message],
    *,
    delay: float = 0,
    query: bytes = b"",
    root_path: str = "",
):
    reads = 0
    sent: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": root_path + ROUTE,
        "root_path": root_path,
        "query_string": query,
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
        "headers": [(k.lower().encode(), v.encode()) for k, v in request_headers],
    }

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        assert messages, "upload read before validation or beyond end of stream"
        if delay:
            await asyncio.sleep(delay)
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent[0]["status"], json.loads(sent[1]["body"]), reads


@pytest.mark.parametrize(
    ("header", "values", "query", "status", "code"),
    [
        ("X-Recording-Started-At", [], b"", 422, "recording_start_required"),
        ("X-Recording-Started-At", [""], b"", 422, "recording_start_required"),
        ("X-Recording-Started-At", [TIME, TIME], b"", 422, "invalid_recording_start"),
        ("X-Recording-Started-At", [TIME[:-6]], b"", 422, "invalid_recording_start"),
        ("X-Recording-Started-At", [TIME[:-6] + "-00:00"], b"", 422, "invalid_recording_start"),
        ("X-Recording-Started-At", ["2026-02-31T18:00:00Z"], b"", 422, "invalid_recording_start"),
        ("Content-Type", ["application/json"], b"", 415, "recording_media_required"),
        ("Content-Encoding", ["gzip"], b"", 415, "encoding_not_supported"),
        ("Content-Length", ["2048"], b"", 413, "recording_too_large"),
        ("Content-Length", ["1", "3"], b"", 422, "invalid_input"),
        ("Transfer-Encoding", ["gzip"], b"", 422, "invalid_input"),
        ("Origin", [], b"", 403, "origin_required"),
        ("Origin", ["http://elsewhere.invalid"], b"", 403, "cross_origin_denied"),
        ("Origin", [ORIGIN, ORIGIN], b"", 403, "cross_origin_denied"),
        ("Sec-Fetch-Site", ["same-site"], b"", 403, "cross_origin_denied"),
        ("Host", ["elsewhere.invalid"], b"", 403, "host_denied"),
        ("Authorization", [], b"", 401, "unauthenticated"),
        ("Authorization", ["Bearer " + ADMIN], b"", 403, "recording_owner_required"),
        ("Authorization", ["Bearer " + USER] * 2, b"", 401, "unauthenticated"),
        ("Content-Type", ["audio/wav"], b"path=private.wav", 422, "invalid_input"),
        ("Content-Type", ["audio/wav"], b"token=secret", 422, "invalid_input"),
    ],
)
def test_preflight_rejects_without_receive(tmp_path, header, values, query, status, code):
    root = tmp_path / "spool"
    app, wire = gateway(root, recording_upload_max_bytes=1024)
    baseline = {"Host": "testserver", **UPLOAD}
    baseline.pop(header, None)
    h = [*baseline.items(), *((header, v) for v in values)]
    assert asyncio.run(send_upload(app, h, [], query=query)) == (status, {"code": code}, 0)
    assert wire.calls == 0
    assert not list((root / "blobs").iterdir())


@pytest.mark.parametrize(
    ("extra", "messages", "delay", "expected"),
    [
        (
            {"Transfer-Encoding": "chunked"},
            [
                {"type": "http.request", "body": b"a" * 800, "more_body": True},
                {"type": "http.request", "body": b"b" * 225, "more_body": False},
            ],
            0,
            (413, "recording_too_large", 2),
        ),
        (
            {"Content-Length": "50"},
            [{"type": "http.request", "body": b"a" * 40}],
            0,
            (422, "invalid_input", 1),
        ),
        (
            {"Content-Length": "40"},
            [{"type": "http.request", "body": b"a" * 41}],
            0,
            (422, "invalid_input", 1),
        ),
        ({}, [{"type": "http.request"}], 0, (422, "invalid_recording", 1)),
        ({}, [{"type": "http.disconnect"}], 0, (400, "recording_upload_incomplete", 1)),
        (
            {},
            [{"type": "http.request", "body": b"a", "more_body": True}],
            0.1,
            (408, "recording_upload_timeout", 1),
        ),
    ],
)
def test_stream_limits_and_no_partial_acceptance(tmp_path, extra, messages, delay, expected):
    root = tmp_path / "spool"
    app, wire = gateway(root, recording_upload_max_bytes=1024, recording_upload_timeout=0.05)
    h = list({"Host": "testserver", **UPLOAD, **extra}.items())
    status, code, reads = expected
    assert asyncio.run(send_upload(app, h, messages.copy(), delay=delay)) == (
        status,
        {"code": code},
        reads,
    )
    assert wire.calls == 1
    assert not list((root / "blobs").iterdir())


def test_root_path_does_not_bypass_origin_or_upload_admission(tmp_path):
    root = tmp_path / "spool"
    app, wire = gateway(root)
    h = [(k, v) for k, v in {"Host": "testserver", **UPLOAD}.items() if k != "Origin"]
    assert asyncio.run(send_upload(app, h, [], root_path="/prefix")) == (
        403,
        {"code": "origin_required"},
        0,
    )
    body = wav()
    messages: list[Message] = [{"type": "http.request", "body": body}]
    status, receipt, reads = asyncio.run(
        send_upload(app, [*h, ("Origin", ORIGIN)], messages, root_path="/prefix")
    )
    assert (status, receipt["state"], reads, wire.calls) == (202, "queued", 1, 2)
