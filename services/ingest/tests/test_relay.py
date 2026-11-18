import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from ear_ingest.app import create_app
from ear_ingest.pairing import Pairing
from ear_ingest.relay import create_relay, derive
from fastapi.testclient import TestClient


def test_reject_damaged_pair_before_redeeming(tmp_path, monkeypatch):
    monkeypatch.setenv("EAR_INGEST_TOKEN", "synthetic")
    receiver = Pairing(tmp_path)
    secret = parse_qs(urlsplit(receiver.invite("")).query)["code"][0]
    ticket = "a" * 32
    crypto = AESGCM(derive(secret, "request", ticket))
    device = "ear-ios-test"
    meta = json.dumps(dict(path="/api/v1/pair", method="POST", device=device)).encode() + b"\n"
    wire = b"".join(
        len(frame).to_bytes(4, "big") + crypto.encrypt(i.to_bytes(12, "big"), frame, None)
        for i, frame in enumerate([meta, b""])
    )
    client = TestClient(create_relay(receiver, create_app(receiver)))
    headers = {
        "X-Device-Id": device,
        "X-Ear-Request": ticket,
        "X-Ear-Invite-Id": hashlib.sha256(secret.encode()).hexdigest(),
    }
    for damaged in [wire[:-20], wire[:-1] + bytes([wire[-1] ^ 1]), wire + b"extra"]:
        assert client.post("/api/v1/relay", headers=headers, content=damaged).status_code == 400
    assert receiver.credential(device) is None
    assert (
        client.post(
            "/api/v1/relay", headers=headers | {"X-Device-Id": "other"}, content=wire
        ).status_code
        == 400
    )
    reply = client.post("/api/v1/relay", headers=headers, content=wire)
    assert reply.status_code == 200
    plain = AESGCM(derive(secret, "response", ticket)).decrypt(
        reply.content[:12], reply.content[12:], b"200"
    )
    assert json.loads(plain)["token"] == receiver.credential(device)
    assert client.post("/api/v1/record").status_code == 404


def test_http_relay_requires_exact_configured_origin(tmp_path, monkeypatch):
    import pytest

    receiver = Pairing(tmp_path)
    with pytest.raises(ValueError):
        receiver.invite("http://relay.example.org:8080")
    monkeypatch.setenv("EAR_INGEST_HTTP_RELAY", "http://relay.example.org:8080")
    assert "relay=" in receiver.invite("http://relay.example.org:8080")
    with pytest.raises(ValueError):
        receiver.invite("http://other.example.org:8080")
    assert "relay=" in receiver.invite("https://relay.example.org")
