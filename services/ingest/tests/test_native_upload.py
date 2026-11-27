import asyncio
import gzip
import hashlib
import hmac
import io
import json
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from ear_ingest.app import create_app
from ear_ingest.octopus_finalized import publish
from ear_ingest.pairing import Pairing
from ear_ingest.relay import create_relay, derive
from fastapi.testclient import TestClient

from .test_ingest import fields

HEADERS = {"Authorization": "Bearer synthetic-secret", "X-Device-Id": "IOS-TEST"}
RECORD = "/api/v1/record"
LEGACY_RECORD = "/api/v1/instances/brain___ios-test/service/api/v1/record"


@pytest.fixture
def native_root(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("EAR_INGEST_TOKEN", "synthetic-secret")
    monkeypatch.setenv("EAR_INGEST_NATIVE_ROOT", str(tmp_path / "native"))
    monkeypatch.setenv("EAR_INGEST_INBOX_ROOT", str(tmp_path / "legacy"))
    for name in ("EAR_INGEST_EVENT_URL", "EAR_INGEST_NOTIFY_URL", "EAR_INGEST_NATIVE_MAX_BYTES"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "native"


def audio(sample=b"\x01\x00"):
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(sample * 160)
    return out.getvalue()


def send(client, *, path=RECORD, content=None, media="audio/wav", data=None):
    return client.post(
        path,
        headers=HEADERS,
        data=fields() if data is None else data,
        files={"file": ("synthetic.bin", audio() if content is None else content, media)},
    )


def test_native_upload_utc_queue_retry_conflict_and_no_legacy(native_root, monkeypatch):
    from ear_adapter.recording_intake import RecordingIntake

    actual = RecordingIntake.accept
    calls = []

    def capture(self, stream, **kw):
        calls.append(kw)
        return actual(self, stream, **kw)

    monkeypatch.setattr(RecordingIntake, "accept", capture)
    data = fields(timestamp="2026-01-01T23:45:00.125-07:00", source="untrusted-input")
    with TestClient(create_app()) as client:
        challenge = "b" * 32
        assert client.get("/health", params={"challenge": challenge}).json() == {
            "ok": True,
            "proof": hmac.digest(
                b"synthetic-secret", f"ear-health-v1:{challenge}".encode(), "sha256"
            ).hex(),
        }
        response = send(client, data=data)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt == {"ok": True, "record_id": receipt["record_id"], "path": str(native_root)}
    assert Path(receipt["path"]).is_dir()
    assert calls == [
        {
            "source": "phone",
            "source_id": "phone-v1:"
            + hashlib.sha256(f"ios-test:{data['group_id']}:1:audio".encode()).hexdigest(),
            "started_at": "2026-01-02T07:15:00.125000Z",
            "ended_at": "2026-01-02T07:45:00.125000Z",
            "media_type": "audio/wav",
            "kind": "audio",
        }
    ]
    with TestClient(create_app()) as client:
        assert send(client, path=LEGACY_RECORD, data=data).json() == receipt
        changed = send(client, path=LEGACY_RECORD, data=data, content=audio(b"\x02\x00"))
        assert changed.status_code == 409 and "ok" not in changed.json()
    stored = RecordingIntake(native_root).get(receipt["record_id"])
    assert stored is not None and stored["state"] == "queued"
    assert not (native_root.parent / "legacy").exists()


def test_native_context_archived_independently(native_root):
    from ear_adapter.recording_intake import RecordingIntake

    content = gzip.compress(b'{"type":"synthetic"}\n', mtime=0)
    with TestClient(create_app()) as client:
        response = send(
            client,
            path=LEGACY_RECORD,
            data=fields(type="phoneContext"),
            media="application/gzip",
            content=content,
        )
        assert response.status_code == 200, response.text
        archived = response.json()["record_id"]
        queued = send(client).json()["record_id"]
    storage = RecordingIntake(native_root)
    assert archived != queued
    assert (item := storage.get(archived)) and item["state"] == "archived"
    assert (item := storage.get(queued)) and item["state"] == "queued"


@pytest.mark.parametrize(
    "record_type,kind,media,content,changes",
    [
        ("phoneIMU", "imu", "application/gzip", gzip.compress(b"{}\n", mtime=0), {}),
        (
            "phoneNotes",
            "notes",
            "application/json",
            b"[]",
            {
                "segment_meta": '{"segmentIndex":0,"segmentStartMs":0,"segmentEndMs":0}',
                "segment_start_ms": "0",
                "segment_end_ms": "0",
            },
        ),
    ],
)
def test_native_other_sidecar_kinds_never_fall_back(
    native_root, monkeypatch, record_type, kind, media, content, changes
):
    from ear_adapter.recording_intake import IntakeInvalid, RecordingIntake

    def refuse(self, stream, **kw):
        assert kw["kind"] == kind
        assert kw["media_type"] == media
        assert (kw["ended_at"] is None) == (kind == "notes")
        assert stream.read() == content
        raise IntakeInvalid("archive unavailable")

    monkeypatch.setattr(RecordingIntake, "accept", refuse)
    with TestClient(create_app()) as client:
        failed = send(
            client,
            path=LEGACY_RECORD,
            media=media,
            content=content,
            data=fields(type=record_type, **changes),
        )
    assert failed.status_code == 400 and "ok" not in failed.json()
    assert not list((native_root / "blobs").iterdir())
    assert not (native_root.parent / "legacy").exists()


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T12:00:00",
        "2026-01-01",
        "2026-01-01T12:00:00+01:05:30",
        "2026-01-01T12:00:00-00:00",
        "9999-12-31T23:59:00Z",
    ],
)
def test_native_requires_explicit_offset_and_rejects_overflow(native_root, value):
    with TestClient(create_app()) as client:
        assert send(client, data=fields(timestamp=value)).status_code == 400
    assert not list((native_root / "blobs").iterdir())


def test_native_rejects_invalid_audio_and_fails_without_durable_ack(native_root, monkeypatch):
    from ear_adapter.recording_intake import RecordingIntake

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        assert send(client, content=b"").status_code == 400
        assert send(client, content=b"not a recording").status_code == 400
        assert send(client, media="application/json").status_code == 400

        def unavailable(*_args, **_kw):
            raise OSError("synthetic filesystem error")

        monkeypatch.setattr(RecordingIntake, "accept", unavailable)
        response = send(client)
        assert response.status_code == 500 and b'"ok"' not in response.content
    assert not list((native_root / "blobs").iterdir())


@pytest.mark.parametrize("key", ["EAR_INGEST_EVENT_URL", "EAR_INGEST_NOTIFY_URL"])
def test_native_and_legacy_notification_fail_before_startup(native_root, monkeypatch, key):
    monkeypatch.setenv(key, "http://synthetic.invalid/")
    with pytest.raises(RuntimeError, match="incompatible"):
        create_app()
    assert not native_root.exists()
    assert not (native_root.parent / "legacy").exists()


def encoded_upload():
    # Synthetic MP4 container header: exercises the actual EarRecorder MIME, not decoding.
    mp4 = b"\x00\x00\x00\x10ftypM4A \x00\x00\x00\x00\x00\x00\x00\x0cmdatdata"
    request = httpx.Request(
        "POST",
        "http://test" + RECORD,
        data=fields(),
        files={"file": ("segment_0002.m4a", mp4, "audio/mp4")},
    )
    return request.headers["content-type"], request.read()


def test_native_counts_actual_bytes_and_authenticates_before_body(native_root, monkeypatch):
    media, body = encoded_upload()
    monkeypatch.setenv("EAR_INGEST_NATIVE_MAX_BYTES", str(len(body)))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            headers = HEADERS | {"content-type": media}

            async def must_not_read():
                raise AssertionError("unauthorized request was read")
                yield b""  # pragma: no cover

            denied = await client.post(
                RECORD,
                headers={"content-type": media, "content-length": "999999"},
                content=must_not_read(),
            )
            assert denied.status_code == 401
            rejected = await client.post(
                RECORD, headers=headers | {"content-length": "999999"}, content=must_not_read()
            )
            assert rejected.status_code == 413

            async def too_large():
                yield body
                yield b"x"

            assert (
                await client.post(RECORD, headers=headers, content=too_large())
            ).status_code == 413
            assert (
                await client.post(RECORD, headers=headers | {"content-length": "1"}, content=body)
            ).status_code == 400
            broken = body.rsplit(b"\r\n--", 1)[0]
            assert (await client.post(RECORD, headers=headers, content=broken)).status_code == 400
            assert not list((native_root / "blobs").iterdir())
            assert (await client.post(RECORD, headers=headers, content=body)).status_code == 200

    asyncio.run(run())


def test_encrypted_relay_native_durable_ack_and_limits(native_root, monkeypatch):
    from ear_adapter.recording_intake import RecordingIntake

    media, body = encoded_upload()
    monkeypatch.setenv("EAR_INGEST_NATIVE_MAX_BYTES", str(len(body)))
    pairing = Pairing(native_root.parent / "pairing")
    device = "ios-test"
    ticket = "a" * 32
    # Claim a synthetic invitation through the real HTTP pairing route.
    client = TestClient(create_app(pairing))
    code = parse_qs(urlsplit(pairing.invite("")).query)["code"][0]
    response = client.post(
        "/api/v1/pair", headers={"X-Device-Id": device, "X-Ear-Invitation": code}
    )
    secret = response.json()["token"]
    meta = (
        json.dumps(
            {"method": "POST", "path": RECORD, "device": device, "Content-Type": media}
        ).encode()
        + b"\n"
    )
    params = {"device": device, "challenge": "c" * 32}
    assert (
        client.get("/health", params=params).json()["proof"]
        == hmac.digest(secret.encode(), b"ear-health-v1:" + b"c" * 32, "sha256").hex()
    )

    def wire(payload, ticket=ticket):
        # Split the metadata, then share a frame between its LF and multipart start.
        cipher = AESGCM(derive(secret, "request", ticket))
        return b"".join(
            len(frame).to_bytes(4, "big") + cipher.encrypt(i.to_bytes(12, "big"), frame, None)
            for i, frame in enumerate((meta[:10], meta[10:] + payload[:100], payload[100:], b""))
        )

    with TestClient(create_relay(pairing, create_app(pairing))) as relay:
        headers = {"X-Device-Id": device, "X-Ear-Request": ticket}
        packet = wire(body)
        assert relay.post("/api/v1/relay", headers=headers, content=packet[:-20]).status_code == 400
        over_ticket = "b" * 32
        rejected = relay.post(
            "/api/v1/relay",
            headers=headers | {"X-Ear-Request": over_ticket},
            content=wire(body + b"x", over_ticket),
        )
        assert rejected.status_code == 413 and "X-Ear-Receipt" not in rejected.headers
        assert not list((native_root / "blobs").iterdir())
        reply = relay.post("/api/v1/relay", headers=headers, content=packet)
        assert reply.status_code == 200
        assert (
            reply.headers["X-Ear-Receipt"]
            == hmac.digest(derive(secret, "receipt", ticket), b"200", "sha256").hex()
        )
        plain = AESGCM(derive(secret, "response", ticket)).decrypt(
            reply.content[:12], reply.content[12:], b"200"
        )
        record_id = json.loads(plain)["record_id"]
        direct = client.post(
            LEGACY_RECORD,
            headers={
                "X-Device-Id": device,
                "Authorization": f"Bearer {secret}",
                "Content-Type": media,
            },
            content=body,
        )
        assert direct.status_code == 200, direct.text
        assert direct.json()["record_id"] == record_id
        assert (item := RecordingIntake(native_root).get(record_id)) and item["state"] == "queued"


def test_octopus_finalized_is_opt_in_scoped_and_verifies_bytes(native_root, monkeypatch):
    from ear_adapter.recording_intake import RecordingIntake

    now = datetime.now(UTC)
    data = audio()
    end = now - timedelta(minutes=1)
    start = end - timedelta(seconds=2)
    event = {
        "event": "record.finalized.v1",
        "device": "IOS-TEST",
        "group_id": "synthetic-group",
        "segment_index": 0,
        "type": "richVoice",
        "media_type": "audio/wav",
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "finalized_at": (now - timedelta(seconds=1)).isoformat(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }

    def submit(content: bytes = data, /, **overrides: object):
        return publish(io.BytesIO(json.dumps(event | overrides).encode() + b"\n" + content))

    with pytest.raises(ValueError, match="not enabled"):
        submit()
    monkeypatch.setenv("EAR_INGEST_OCTOPUS_DEVICE_ID", "ios-test")
    monkeypatch.setenv("EAR_INGEST_OCTOPUS_FINALIZED_AFTER", (now - timedelta(hours=1)).isoformat())
    for invalid in (
        {"device": "other"},
        {"path": "/synthetic/outside"},
        {"event": "record.writing"},
        {"finalized_at": (now + timedelta(hours=1)).isoformat()},
        {
            "ended_at": (now - timedelta(hours=2)).isoformat(),
            "started_at": (now - timedelta(hours=3)).isoformat(),
        },
        {"sha256": "0" * 64},
    ):
        with pytest.raises(ValueError):
            submit(**invalid)
    for content in (data[:-1], data + b"x"):
        with pytest.raises(ValueError):
            submit(content)
    assert not native_root.exists()
    first = submit()
    assert first == submit()
    with TestClient(create_app()) as client:
        via_http = send(
            client,
            path=LEGACY_RECORD,
            data=fields(
                timestamp=start.isoformat(),
                group_id=event["group_id"],
                segment_meta='{"segmentIndex":0}',
                segment_start_ms="0",
                segment_end_ms="2000",
            ),
        )
    assert via_http.json() == first
    assert (item := RecordingIntake(native_root).get(first["record_id"])) and item[
        "state"
    ] == "queued"
    assert not (native_root.parent / "legacy").exists()
