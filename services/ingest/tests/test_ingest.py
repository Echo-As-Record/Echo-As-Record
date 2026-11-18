import hmac
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from ear_ingest.app import create_app
from ear_ingest.store import CHUNK_SIZE, copy, persist
from fastapi.testclient import TestClient
from starlette.datastructures import Headers, UploadFile


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EAR_INGEST_TOKEN", "synthetic-secret")
    monkeypatch.setenv("EAR_INGEST_INBOX_ROOT", str(tmp_path / "inbox"))
    monkeypatch.delenv("EAR_INGEST_NOTIFY_URL", raising=False)
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield tmp_path / "inbox"
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def fields(**changes):
    result = {
        "name": "PhoneVoice",
        "text": "",
        "type": "richVoice",
        "source": "local",
        "group_id": "00000000-0000-4000-8000-000000000001",
        "segment_meta": json.dumps(
            {
                "segmentIndex": 1,
                "segmentStartMs": 1800000,
                "segmentEndMs": 3600000,
                "source": "phone_local_auto",
            }
        ),
        "segment_start_ms": "1800000",
        "segment_end_ms": "3600000",
        "timestamp": "2026-11-12T11:21:29.157Z",
    }
    result.update(changes)
    return result


def send(
    client, *, data=None, headers=None, name="segment_0002.m4a", content=b"synthetic", url=None
):
    return client.post(
        url or "/api/v1/record",
        data=fields() if data is None else data,
        headers=headers
        if headers is not None
        else {
            "Authorization": "Bearer synthetic-secret",
            "X-Device-Id": "IOS-TEST",
            "X-User-Id": "synthetic-user",
        },
        files={"file": (name, content, "audio/mp4")},
    )


def test_sample_and_legacy_parser(setup):
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"ok": True}
        response = send(client, data=fields(context='{"latitude": 0}'))
    assert response.status_code == 200, response.text
    path = Path(response.json()["path"])
    assert path.relative_to(setup).as_posix() == (
        "ios-test/2026-11-12/phone/20-21-29__00000000-0000-4000-8000-000000000001/"
        "segment_1_richVoice_from_20-51-29_to_21-21-29.json"
    )
    record = json.loads(path.read_text())
    assert record["context"] == {"latitude": 0}
    assert record["segment_meta"]["source"] == "phone_local_auto"
    assert record["segment_start_ms"] == "1800000"
    assert record["timestamp"] == "2026-11-12T11:21:29.157000+00:00"
    assert record["user_id"] == "synthetic-user"
    assert record["files"][0] == {
        "field_name": "file",
        "original_name": "segment_0002.m4a",
        "stored_name": "segment_1_from_20-51-29_to_21-21-29__01__segment_0002.m4a",
        "size": 9,
        "content_type": "audio/mp4",
    }
    # Minimal independent counterpart to Ear parsing.py + models/ingest.py.
    candidates = []
    for candidate in setup.glob("*/*/*/*/*.json"):
        item = json.loads(candidate.read_text())
        if item["type"] != "richVoice" or item.get("processing_status", "ready") != "ready":
            continue
        assert all(isinstance(item[key], str) for key in ("id", "group_id", "type", "source"))
        timestamp = datetime.fromisoformat(item["timestamp"])
        assert timestamp.tzinfo is not None
        start, end = int(item["segment_start_ms"]), int(item["segment_end_ms"])
        assert 0 <= start < end
        assert timestamp + timedelta(milliseconds=end) > timestamp + timedelta(milliseconds=start)
        file = item["files"][0]
        assert file["size"] >= 0 and file["content_type"]
        audio = candidate.parent / file["stored_name"]
        assert audio.is_file() and audio.read_bytes() == b"synthetic"
        candidates.append((item["id"], candidate.relative_to(setup).parts[0]))
    assert candidates == [(response.json()["record_id"], "ios-test")]


@pytest.mark.parametrize(
    "kind,name",
    [
        ("richVoice", "segment_0002.m4a"),
        ("phoneContext", "context_0002.jsonl.gz"),
        ("phoneIMU", "imu_0002.jsonl.gz"),
        ("phoneNotes", "notes_0002.jsonl.gz"),
    ],
)
def test_types_and_legacy_route(setup, kind, name):
    with TestClient(create_app()) as client:
        response = send(
            client,
            data=fields(type=kind),
            name=name,
            url="/api/v1/instances/brain___ios-test/service/api/v1/record",
        )
    assert response.status_code == 200
    record = json.loads(Path(response.json()["path"]).read_text())
    assert record["type"] == kind
    assert record["files"][0]["stored_name"].endswith("__01__" + name)


@pytest.mark.parametrize("auth", ["", "Bearer wrong", "Basic synthetic-secret", "Bearer"])
def test_auth(setup, auth):
    with TestClient(create_app()) as client:
        response = send(client, headers={"Authorization": auth, "X-Device-Id": "ios-test"})
    assert response.status_code == 401
    assert not setup.exists()


@pytest.mark.parametrize("device", ["", "../bad", ".", "..", "a/b", "a\\b", "a b", "a" * 129])
def test_invalid_device(setup, device):
    with TestClient(create_app()) as client:
        response = send(
            client, headers={"Authorization": "Bearer synthetic-secret", "X-Device-Id": device}
        )
    assert response.status_code == 400
    assert not setup.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"group_id": "../outside"},
        {"group_id": ".."},
        {"segment_meta": "[]"},
        {"segment_meta": '{"segmentIndex":"../escape"}'},
        {"segment_meta": "{"},
        {"segment_start_ms": "-1"},
        {"segment_end_ms": "1800000"},
        {"segment_end_ms": "nan"},
        {"timestamp": "no date"},
        {"type": "unknown"},
        {"segment_start_ms": "1"},
        {"segment_end_ms": "999999999999999"},
    ],
)
def test_invalid_fields(setup, changes):
    with TestClient(create_app()) as client:
        assert send(client, data=fields(**changes)).status_code == 400
    assert not setup.exists()


@pytest.mark.parametrize("name", ["../outside.m4a", "a/b.m4a", "..\\outside.m4a"])
def test_filename_traversal(setup, name):
    with TestClient(create_app()) as client:
        assert send(client, name=name).status_code == 400
    assert not setup.exists()


def test_symlink_escape(setup, tmp_path):
    setup.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (setup / "ios-test").symlink_to(outside, target_is_directory=True)
    with TestClient(create_app()) as client:
        assert send(client).status_code == 400
    assert not list(outside.iterdir())


def test_retransmit_restart_and_changed_names(setup):
    with TestClient(create_app()) as client:
        first = send(client).json()
        assert send(client, content=b"replacement").json() == first
    # Changed session timestamp and attachment name still have the same identity.
    with TestClient(create_app()) as client:
        second = send(
            client,
            name="replacement.m4a",
            content=b"new",
            data=fields(timestamp="2026-11-13T11:21:29.157Z"),
        ).json()
    assert second["record_id"] == first["record_id"]
    assert not Path(first["path"]).exists()
    records = list(setup.glob("*/*/*/*/*.json"))
    assert records == [Path(second["path"])]
    assert [p.read_bytes() for p in setup.rglob("*.m4a")] == [b"new"]
    assert not list(setup.rglob(".ingest-*"))


def test_missing_token(setup, monkeypatch):
    monkeypatch.delenv("EAR_INGEST_TOKEN")
    with pytest.raises(RuntimeError, match="EAR_INGEST_TOKEN"):
        create_app()


def test_streaming_copy():
    class Bounded(io.BytesIO):
        calls = 0

        def read(self, size: int | None = -1):
            assert size is not None
            assert 0 < size <= CHUNK_SIZE
            self.calls += 1
            return super().read(size)

    source = Bounded(b"x" * (CHUNK_SIZE * 3 + 7))
    destination = io.BytesIO()
    assert copy(source, destination) == CHUNK_SIZE * 3 + 7
    assert source.calls == 5
    assert destination.getvalue() == source.getvalue()


def test_streaming_upload(setup, monkeypatch):
    import ear_ingest.store as store

    original = store.copy
    sizes = []

    def observed(source, destination):
        assert source._rolled  # Starlette spools large multipart files to disk.
        size = original(source, destination)
        sizes.append(size)
        return size

    monkeypatch.setattr(store, "copy", observed)
    with TestClient(create_app()) as client:
        assert send(client, content=b"x" * (CHUNK_SIZE * 3)).status_code == 200
    assert sizes == [CHUNK_SIZE * 3]


def test_copy_failure_keeps_previous(setup, monkeypatch):
    import ear_ingest.store as store

    with TestClient(create_app()) as client:
        first = send(client).json()
        original = Path(first["path"]).read_bytes()

        def fail(source, destination):
            destination.write(b"partial")
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(store, "copy", fail)
        with pytest.raises(OSError):
            send(client, content=b"replacement")
    assert Path(first["path"]).read_bytes() == original
    assert next(setup.rglob("*.m4a")).read_bytes() == b"synthetic"
    assert not list(setup.rglob(".ingest-*"))


def test_concurrent_retransmits(setup):
    payload = fields()
    payload["segment_meta"] = json.loads(payload["segment_meta"])

    def write(index):
        upload = UploadFile(
            io.BytesIO(str(index).encode()),
            filename=f"{index}.m4a",
            headers=Headers({"content-type": "audio/mp4"}),
        )
        return persist(setup, "ios-test", payload, upload)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(write, range(8)))
    assert len({r["record_id"] for r in results}) == 1
    assert len(list(setup.rglob("*.json"))) == len(list(setup.rglob("*.m4a"))) == 1


def test_notify_failure_does_not_fail_upload(setup, monkeypatch, caplog):
    monkeypatch.setenv("EAR_INGEST_NOTIFY_URL", "http://127.0.0.1:9999/api/ingest/reconcile")
    calls = []

    async def post(self, url):
        calls.append(url)
        assert len(list(setup.rglob("*.json"))) == 1
        raise httpx.ConnectError("private-url-or-token")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with TestClient(create_app()) as client:
        assert send(client).status_code == 200
    assert len(calls) == 1
    assert "notify_failed" in caplog.text
    assert "private-url-or-token" not in caplog.text


def test_mismatched_legacy_instance(setup):
    with TestClient(create_app()) as client:
        response = send(client, url="/api/v1/instances/brain___other/service/api/v1/record")
    assert response.status_code == 400
    assert not setup.exists()


def test_missing_field(setup):
    data = fields()
    del data["group_id"]
    with TestClient(create_app()) as client:
        assert send(client, data=data).status_code == 400


def test_midnight_uses_session_date(setup):
    with TestClient(create_app()) as client:
        response = send(client, data=fields(timestamp="2026-11-12T14:51:29Z"))
    path = Path(response.json()["path"])
    assert "/2026-11-12/phone/23-51-29__" in str(path)
    assert path.name == "segment_1_richVoice_from_00-21-29_to_00-51-29.json"


def test_cross_type_collision_preserves_audio(setup):
    with TestClient(create_app()) as client:
        first = send(client).json()
        response = send(client, data=fields(type="phoneContext"), content=b"context")
    assert response.status_code == 400
    assert next(setup.rglob("*.m4a")).read_bytes() == b"synthetic"
    assert len(list(setup.rglob("*.json"))) == 1
    assert Path(first["path"]).exists()


def test_cli_options(setup, monkeypatch):
    from unittest.mock import Mock

    from ear_ingest.__main__ import main

    run = Mock()
    monkeypatch.setattr("ear_ingest.__main__.uvicorn.run", run)
    monkeypatch.setattr("sys.argv", ["ear-ingest", "serve", "--host", "0.0.0.0", "--port", "8792"])
    main()
    assert run.call_args.kwargs == {"host": "0.0.0.0", "port": 8792}


def test_cancelled_request_drains_copy(setup, monkeypatch):
    import asyncio
    from threading import Event

    import ear_ingest.store as store

    entered, release = Event(), Event()
    original = store.copy

    def delayed(source, destination):
        entered.set()
        assert release.wait(3)
        assert not source.closed
        return original(source, destination)

    monkeypatch.setattr(store, "copy", delayed)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/api/v1/record",
                    data=fields(),
                    headers={"Authorization": "Bearer synthetic-secret", "X-Device-Id": "ios-test"},
                    files={"file": ("segment_0002.m4a", b"synthetic", "audio/mp4")},
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert len(list(setup.rglob("*.json"))) == 1
    assert next(setup.rglob("*.m4a")).read_bytes() == b"synthetic"


def test_receiver_challenge(setup):
    nonce = "a" * 32
    with TestClient(create_app()) as client:
        response = client.get("/health", params={"challenge": nonce})
        expected = hmac.digest(b"synthetic-secret", ("ear-health-v1:" + nonce).encode(), "sha256")
        assert response.json() == {"ok": True, "proof": expected.hex()}
        assert client.get("/health?challenge=invalid").status_code == 400


def test_directed_delivery_after_restart_and_context_dependency(setup, monkeypatch):
    from ear_ingest.delivery import Delivery

    url = "http://legacy.test/internal/api/ingest/brain-event"
    monkeypatch.setenv("EAR_INGEST_EVENT_URL", url)
    # TestClient without lifespan: simulate an unavailable dispatcher.
    uploader = TestClient(create_app())
    delivery = Delivery(setup)
    observed = []

    def legacy(request):
        event = json.loads(request.content)
        assert str(request.url) == url
        assert "authorization" not in request.headers
        assert event["event_type"] == "brain.record.received"
        payload = event["payload"]
        assert Path(payload["record_path"]).is_file()
        assert Path(payload["file_path"]).is_file()
        first = not observed
        observed.append(payload["type"])
        if first:
            return httpx.Response(
                200, json={"status": "retry", "reason": "matching_audio_capture_not_ready"}
            )
        return httpx.Response(202, json={"status": "accepted", "reason": "ok", "capture_id": "c1"})

    with httpx.Client(transport=httpx.MockTransport(legacy)) as downstream:
        for kind in ("phoneContext", "phoneIMU", "phoneNotes"):
            assert send(uploader, data=fields(type=kind), name=kind + ".gz").is_success
        assert delivery.dispatch(downstream, url)
        assert not delivery.dispatch(downstream, url)
        assert send(uploader).is_success
        restarted = Delivery(setup)
        restarted.recover()
        assert restarted.dispatch(downstream, url)
        assert restarted.dispatch(downstream, url)
        assert observed == ["phoneContext", "richVoice", "phoneContext"]
        assert not restarted.dispatch(downstream, url)
        assert send(uploader).is_success
        assert not restarted.dispatch(downstream, url)
        with restarted.connect() as db:
            assert [
                (r[0], r[1]) for r in db.execute("SELECT kind,state FROM jobs ORDER BY kind")
            ] == [
                ("phoneContext", "accepted"),
                ("phoneIMU", "archived"),
                ("phoneNotes", "archived"),
                ("richVoice", "accepted"),
            ]
            assert [
                tuple(r) for r in db.execute("SELECT kind,capture_id FROM receipts ORDER BY seq")
            ] == [("richVoice", "c1"), ("phoneContext", "c1")]
        assert send(uploader, content=b"changed bytes").is_success
        assert restarted.dispatch(downstream, url)
        assert not restarted.dispatch(downstream, url)


def test_recover_interrupted_publication(setup, monkeypatch):
    from ear_ingest.delivery import Delivery

    monkeypatch.setenv("EAR_INGEST_EVENT_URL", "http://legacy.test/event")
    uploader = TestClient(create_app())
    replace = os.replace

    def fail_before_record(source, target):
        if str(target).endswith(".json"):
            raise OSError("simulated interruption")
        replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_before_record)
        with pytest.raises(OSError, match="simulated interruption"):
            send(uploader)
    durable = Delivery(setup)
    durable.recover()
    durable.recover()
    record = json.loads(next(setup.rglob("*.json")).read_text())
    with durable.connect() as db:
        assert db.execute("SELECT count(*) FROM publications").fetchone()[0] == 0
        assert tuple(db.execute("SELECT record_id,state FROM jobs").fetchone()) == (
            record["id"],
            "pending",
        )
    assert send(uploader).json()["record_id"] == record["id"]


@pytest.mark.parametrize("reply", [503, "broken", "paused", "rejected"])
def test_delivery_outcome_does_not_change_upload_ack(setup, monkeypatch, reply):
    from ear_ingest.delivery import Delivery

    url = "http://legacy.test/event"
    monkeypatch.setenv("EAR_INGEST_EVENT_URL", url)
    uploader = TestClient(create_app())
    result = send(uploader)
    assert result.is_success
    durable = Delivery(setup)

    def transport(request):
        if reply == "broken":
            return httpx.Response(200, content=b"invalid")
        if reply == 503:
            return httpx.Response(503)
        state = "retry" if reply == "paused" else "rejected"
        return httpx.Response(200, json={"status": state, "reason": reply})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        assert durable.dispatch(client, url)
        assert not durable.dispatch(client, url)
    with durable.connect() as db:
        assert tuple(db.execute("SELECT state,attempts FROM jobs").fetchone()) == (
            "rejected" if reply == "rejected" else "pending",
            1,
        )
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


def test_session_wide_phone_notes_matches_original_sender(setup, monkeypatch):
    from ear_ingest.delivery import Delivery

    monkeypatch.setenv("EAR_INGEST_EVENT_URL", "http://legacy.test/event")
    uploader = TestClient(create_app())
    zero = dict(
        segment_start_ms="0",
        segment_end_ms="0",
        segment_meta=json.dumps({"segmentIndex": 0, "segmentStartMs": 0, "segmentEndMs": 0}),
    )
    notes = send(
        uploader, data=fields(type="phoneNotes", **zero), name="marks.json", content=b'{"marks":[]}'
    )
    assert notes.is_success
    for kind in ("richVoice", "phoneContext", "phoneIMU"):
        assert send(uploader, data=fields(type=kind, **zero)).status_code == 400
    with Delivery(setup).connect() as db:
        assert tuple(db.execute("SELECT kind,state,segment_index FROM jobs").fetchone()) == (
            "phoneNotes",
            "archived",
            0,
        )


def test_event_and_legacy_reconcile_are_mutually_exclusive(setup, monkeypatch):
    monkeypatch.setenv("EAR_INGEST_EVENT_URL", "http://legacy.test/event")
    monkeypatch.setenv("EAR_INGEST_NOTIFY_URL", "http://legacy.test/reconcile")
    with pytest.raises(RuntimeError, match="Configure only one"):
        create_app()


def test_receiver_pairing(tmp_path, setup):
    from urllib.parse import parse_qs, urlsplit

    from ear_ingest.pairing import Pairing

    receiver = Pairing(tmp_path / "pair")
    query = parse_qs(urlsplit(receiver.invite("https://{receiver}.example.org")).query)
    assert query["relay"] == [f"https://{receiver.id}.example.org"]
    assert Pairing(receiver.directory).certificate()[2] == query["pin"][0]
    client = TestClient(create_app(receiver))
    headers = {"X-Ear-Invitation": query["code"][0], "X-Device-Id": "ear-ios-a"}
    reply = client.post("/api/v1/pair", headers=headers).json()
    assert reply["receiver"] == receiver.id
    assert client.post("/api/v1/pair", headers=headers).json() == reply
    headers["X-Device-Id"] = "ear-ios-b"
    assert client.post("/api/v1/pair", headers=headers).status_code == 403
    token = reply["token"]
    nonce = "a" * 32
    proof = client.get("/health", params={"device": "ear-ios-a", "challenge": nonce}).json()
    assert (
        proof["proof"]
        == hmac.digest(token.encode(), f"ear-health-v1:{nonce}".encode(), "sha256").hex()
    )
    headers = {"X-Device-Id": "ear-ios-b", "Authorization": f"Bearer {token}"}
    assert send(client, headers=headers).status_code == 401
    headers["X-Device-Id"] = "ear-ios-a"
    assert send(client, headers=headers).status_code == 200


def test_fresh_inbox_receives_decodable_audio_and_context(setup):
    import gzip
    import wave

    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    context = gzip.compress(b'{"context":{"latitude":0,"longitude":0}}\n')
    with TestClient(create_app()) as client:
        for kind, name, payload in [
            ("richVoice", "segment_0002.wav", audio.getvalue()),
            ("phoneContext", "context_0002.jsonl.gz", context),
        ]:
            reply = send(client, data=fields(type=kind), name=name, content=payload)
            assert reply.status_code == 200
            record_path = Path(reply.json()["path"])
            record = json.loads(record_path.read_text())
            assert record_path.is_relative_to(setup)
            assert record["id"] == reply.json()["record_id"]
            assert (record_path.parent / record["files"][0]["stored_name"]).read_bytes() == payload
    assert len(list(setup.glob("*/*/*/*/*.json"))) == 2
