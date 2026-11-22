"""Fixed interoperability examples; never regenerate expected bytes during tests."""

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from ear_ingest.app import create_app
from ear_ingest.pairing import Pairing
from ear_ingest.relay import create_relay, derive
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

VECTORS = Path(__file__).parent / "vectors"


def vector(name):
    return json.loads((VECTORS / f"{name}.json").read_text())


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    health = vector("health")
    monkeypatch.setenv("EAR_INGEST_TOKEN", health["secret"])
    monkeypatch.setenv("EAR_INGEST_INBOX_ROOT", str(tmp_path / "inbox"))
    monkeypatch.delenv("EAR_INGEST_EVENT_URL", raising=False)
    monkeypatch.delenv("EAR_INGEST_NOTIFY_URL", raising=False)
    monkeypatch.delenv("EAR_TRANSCRIPTION_URL", raising=False)
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    pairing = Pairing(tmp_path / "pairing")
    with pairing.connect() as db:
        db.execute("INSERT INTO devices VALUES(?,?)", (health["device"], health["secret"]))
    yield pairing, tmp_path / "inbox"
    if old_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old_tz
    time.tzset()


def test_health_vector(receiver):
    pairing, _ = receiver
    v = vector("health")
    with TestClient(create_app(pairing)) as client:
        assert client.get("/health").json() == {"ok": True}
        for device in ("", v["device"]):
            result = client.get("/health", params={"challenge": v["challenge"], "device": device})
            assert result.status_code == 200
            assert result.json() == {"ok": True, "proof": v["proof"]}
        assert client.get("/health", params={"challenge": "F" * 32}).status_code == 400
    with TestClient(create_relay(pairing, create_app(pairing))) as client:
        assert client.get(
            "/health", params={"challenge": v["challenge"], "device": v["device"]}
        ).json() == {"proof": v["proof"]}


def test_keys_and_frames():
    v = vector("relay-keys")
    for direction, expected in v["keys"].items():
        assert derive(v["secret"], direction, v["ticket"]).hex() == expected
    request = vector("relay-request")
    assert request["secret"] == v["secret"]
    assert request["ticket"] == v["ticket"]
    assert json.loads(request["metadata_utf8"]) == request["metadata"]
    assert request["frames"][0]["plaintext_hex"] == request["metadata_utf8"].encode().hex()
    assert request["frames"][1]["plaintext_hex"] == vector("upload")["body_hex"]
    assert request["frames"][-1]["plaintext_hex"] == ""
    for i, frame in enumerate(request["frames"]):
        assert frame["index"] == i
        nonce = i.to_bytes(12, "big")
        assert nonce.hex() == frame["nonce_hex"]
        plain = bytes.fromhex(frame["plaintext_hex"])
        wire = len(plain).to_bytes(4, "big") + AESGCM(bytes.fromhex(v["keys"]["request"])).encrypt(
            nonce, plain, None
        )
        assert wire.hex() == frame["wire_hex"]
    assert request["wire_hex"] == "".join(f["wire_hex"] for f in request["frames"])
    response = request["response"]
    status = str(response["status"]).encode()
    assert status.hex() == response["aad_hex"]
    assert (
        hmac.digest(bytes.fromhex(v["keys"]["receipt"]), status, "sha256").hex()
        == response["receipt"]
    )
    sealed = bytes.fromhex(response["wire_hex"])
    cipher = AESGCM(bytes.fromhex(v["keys"]["response"]))
    assert cipher.decrypt(sealed[:12], sealed[12:], status).decode() == response["plaintext_utf8"]
    with pytest.raises(InvalidTag):
        cipher.decrypt(sealed[:12], sealed[12:], b"201")


def test_relay_exact_response_and_reject_damaged_frames(receiver, monkeypatch):
    pairing, _ = receiver
    v = vector("relay-request")
    reply = v["response"]
    origin = FastAPI()
    calls = []

    @origin.post("/api/v1/record")
    async def record(request: Request):
        calls.append(await request.body())
        assert calls[-1].hex() == vector("upload")["body_hex"]
        assert request.headers["authorization"] == "Bearer " + v["secret"]
        assert request.headers["x-device-id"] == v["metadata"]["device"]
        assert request.headers["x-user-id"] == v["metadata"]["X-User-Id"]
        assert request.headers["content-type"] == v["metadata"]["Content-Type"]
        return Response(reply["plaintext_utf8"], status_code=reply["status"])

    # Only the test response nonce is fixed; production os.urandom remains unchanged.
    monkeypatch.setattr("ear_ingest.relay.os.urandom", lambda n: bytes.fromhex(reply["nonce_hex"]))
    wire = bytes.fromhex(v["wire_hex"])
    with TestClient(create_relay(pairing, origin)) as client:
        for broken in (wire[:-20], wire[:-1] + bytes([wire[-1] ^ 1]), wire + b"extra"):
            assert (
                client.post("/api/v1/relay", headers=v["headers"], content=broken).status_code
                == 400
            )
        assert calls == []
        result = client.post("/api/v1/relay", headers=v["headers"], content=wire)
    assert result.status_code == reply["status"]
    assert result.content.hex() == reply["wire_hex"]
    assert result.headers["x-ear-receipt"] == reply["receipt"]
    assert result.headers["cache-control"] == "no-store"
    assert len(calls) == 1


def test_upload_vector_direct_and_relay(receiver, monkeypatch):
    pairing, root = receiver
    v, relay = vector("upload"), vector("relay-request")
    fixed_id = uuid.UUID("00000000-0000-4000-8000-000000000003")
    monkeypatch.setattr("ear_ingest.store.uuid.uuid4", lambda: fixed_id)
    with TestClient(create_app(pairing)) as client:
        result = client.post(
            "/api/v1/record", headers=v["headers"], content=bytes.fromhex(v["body_hex"])
        )
    assert result.status_code == 200, result.text
    expected = json.loads(relay["response"]["plaintext_utf8"])
    expected["path"] = str(root / v["record_path"])
    assert result.json() == expected
    with TestClient(create_relay(pairing, create_app(pairing))) as client:
        result = client.post(
            "/api/v1/relay", headers=relay["headers"], content=bytes.fromhex(relay["wire_hex"])
        )
    assert result.status_code == 200
    plain = AESGCM(derive(relay["secret"], "response", relay["ticket"])).decrypt(
        result.content[:12], result.content[12:], b"200"
    )
    assert json.loads(plain) == expected
    assert result.headers["x-ear-receipt"] == relay["response"]["receipt"]
    record = json.loads((root / v["record_path"]).read_text())
    for key, value in v["fields"].items():
        if key == "segment_meta":
            assert record[key] == json.loads(value)
        elif key == "timestamp":
            assert record[key] == value.replace("Z", "+00:00")
        else:
            assert record[key] == value
    assert record["user_id"] == v["headers"]["X-User-Id"]
    assert (root / v["attachment_path"]).read_bytes().hex() == v["file"]["hex"]
    assert record["files"][0]["original_name"] == v["file"]["name"]
    assert record["files"][0]["content_type"] == v["file"]["content_type"]
    assert list(root.glob("*/*/phone/*/*.json")) == [root / v["record_path"]]


def test_pairing_vector(receiver, monkeypatch):
    pairing, _ = receiver
    v = vector("pairing")
    parsed = urlsplit(v["url"])
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("earrecorder", "pair", "")
    assert dict(parse_qsl(parsed.query, keep_blank_values=True)) == v["fields"]
    pairing.id = v["fields"]["receiver"]
    monkeypatch.setattr(pairing, "certificate", lambda: (None, None, v["fields"]["pin"]))
    monkeypatch.setattr("ear_ingest.pairing.secrets.token_hex", lambda n: v["fields"]["code"])
    monkeypatch.setattr("ear_ingest.pairing.time.time", lambda: v["issued_at"])
    monkeypatch.setenv("EAR_INGEST_HTTP_RELAY", v["fields"]["relay"])
    assert pairing.invite(v["fields"]["relay"]) == v["url"]
    assert hashlib.sha256(v["fields"]["code"].encode()).hexdigest() == v["invite_id"]
    with pairing.connect() as db:
        assert db.execute("SELECT expires FROM invites").fetchone()[0] == v["expires_at"]
    with TestClient(create_app(pairing)) as client:
        headers = {"X-Device-Id": v["device"], "X-Ear-Invitation": v["fields"]["code"]}
        for _ in range(2):
            result = client.post("/api/v1/pair", headers=headers)
            assert result.status_code == 200
            assert result.json() == {"receiver": v["fields"]["receiver"], "token": v["token"]}
        assert (
            client.post(
                "/api/v1/pair", headers=headers | {"X-Device-Id": "synthetic-other"}
            ).status_code
            == 403
        )
        monkeypatch.setattr("ear_ingest.pairing.time.time", lambda: v["expires_at"] + 1)
        assert client.post("/api/v1/pair", headers=headers).status_code == 403
