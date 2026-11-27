import httpx
import pytest
from ear_api.app import create_app
from ear_api.upstream import Upstream
from ear_contracts import Source
from ear_storage_client.queries import Page
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import STORAGE_USER, headers, settings
from .test_evidence import END, START, MockWire, raw

URL = f"/v1/source-timeline?start={START}&end={END}"


def record(kind, channel="octopus", **payload):
    return raw(kind, **payload).model_copy(
        update={"source": Source(device="synthetic", channel=channel, user="default")}
    )


@pytest.fixture
def reader():
    wire = MockWire()
    config = settings("http://storage.invalid", chat_evidence_read=True)
    upstream = Upstream(config, transport=httpx.MockTransport(wire.handle))
    with TestClient(create_app(config, upstream=upstream)) as client:
        yield wire, client


def test_timeline_verifies_old_ear_transcript_provenance_and_preserves_sparse_cursor(reader):
    wire, client = reader
    audio = record("audio_ref", upstream={"service": "ear"}, file_ref={"path": "private"})
    native = record("audio_ref", upstream={"service": "ear-native"})
    old = record("transcript_segment", audio_ref_id=audio.id, text="旧录音")
    new = record("transcript_segment", audio_ref_id=native.id, text="新采集")
    wire.reader.records.update({audio.id: audio, native.id: native})
    wire.reader.query_fn = lambda q: Page((old, new), "next-page")
    path = URL + "&kind=transcript_segment&source=legacy-ear"
    result = client.get(path, headers=headers())
    assert result.status_code == 200
    assert result.json()["next_cursor"] == "next-page"
    [item] = result.json()["items"]
    assert item["id"] == old.id and item["source"] == "legacy-ear" and item["text"] == "旧录音"
    assert "audio_ref_id" not in item and "private" not in result.text
    assert wire.reader.gets == [audio.id, native.id]
    wire.reader.denied_ids.add(audio.id)
    assert client.get(path, headers=headers()).json() == {"items": [], "next_cursor": "next-page"}
    assert (
        client.get(URL + "&kind=transcript_segment", headers=headers()).json()["items"][0]["source"]
        == "other"
    )


def test_timeline_chat_is_projected_and_requires_current_type_grant(reader):
    wire, client = reader
    chat = record(
        "chat_message",
        "librechat",
        source_system="librechat",
        text="字" * 2300,
        role="assistant",
        reasoning="PRIVATE_REASONING",
        text_file_ref="PRIVATE_FILE",
        tool_calls=[{"output": "PRIVATE_TOOL_RESULT"}],
    )
    foreign = record("chat_message", "another_system", source_system="librechat", text="OTHER")
    wire.reader.query_fn = lambda _q: Page((chat, foreign), None)
    path = URL + "&kind=chat_message&source=librechat"
    assert client.get(path).status_code == 401
    assert (
        client.get(path, headers={**headers(), "Origin": "https://outside.invalid"}).status_code
        == 403
    )
    result = client.get(path, headers=headers())
    assert result.status_code == 200
    [item] = result.json()["items"]
    assert set(item) == {
        "id",
        "kind",
        "source",
        "start",
        "end",
        "title",
        "text",
        "truncated",
        "role",
    }
    assert item["role"] == "assistant" and item["truncated"] and len(item["text"]) == 2000
    assert "PRIVATE" not in result.text and "OTHER" not in result.text
    assert result.headers["cache-control"] == "no-store"
    assert all(token == STORAGE_USER for _, token in wire.requests)
    assert client.get("/v1/records/chat_message", headers=headers()).status_code == 404
    wire.reader.denied_types.add("chat_message")
    assert client.get(path, headers=headers()).status_code == 403


@pytest.mark.parametrize(
    "query",
    [
        "kind=voiceprint",
        "kind=audio_ref&source=librechat",
        "kind=audio_ref&limit=17",
        "kind=audio_ref&source=bad",
        "kind=audio_ref&start=x",
    ],
)
def test_timeline_rejects_invalid_queries_before_storage(reader, query):
    wire, client = reader
    assert client.get(URL + "&" + query, headers=headers()).status_code == 422
    assert not wire.requests


def test_timeline_chat_default_off_and_dst_window():
    config = settings("http://storage.invalid")
    with TestClient(create_app(config)) as client:
        assert client.get("/v1/session").json() == {"test_no_auth": False}
        assert client.get(URL + "&kind=chat_message", headers=headers()).status_code == 404
    from ear_api.source_timeline import window

    assert window("2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z").start.hour == 4
    with pytest.raises(ValueError):
        window("2026-11-01T04:00:00Z", "2026-11-02T07:00:00Z")


@pytest.mark.parametrize("value", ["true", 1, None])
def test_chat_capability_requires_strict_boolean(value):
    with pytest.raises(ValidationError):
        settings("http://storage.invalid", chat_evidence_read=value)


def test_session_advertises_opt_in_without_reading_storage(reader):
    wire, client = reader
    assert client.get("/v1/session").json() == {"test_no_auth": False, "chat_evidence_read": True}
    assert not wire.requests
