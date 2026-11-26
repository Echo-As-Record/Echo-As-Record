"""Optional source evidence uses existing authorization and the ASGI boundary."""

import httpx
import pytest
from ear_api.app import create_app
from ear_api.boundary import HEADERS
from ear_api.upstream import Upstream
from ear_contracts import new_id
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import STORAGE_USER, headers, settings
from .test_evidence import MockWire, raw
from .test_no_auth import ORIGIN


@pytest.mark.parametrize("test_no_auth", [False, True])
def test_source_evidence_is_off_by_default(test_no_auth):
    wire = MockWire()
    config = settings("http://storage.invalid", test_no_auth=test_no_auth, public_origin=ORIGIN)
    with TestClient(
        create_app(config, upstream=Upstream(config, transport=httpx.MockTransport(wire.handle))),
        base_url=ORIGIN,
    ) as client:
        assert client.get("/v1/session").json() == {"test_no_auth": test_no_auth}
        result = client.get(f"/v1/source-evidence/{new_id()}", headers=headers())
        assert result.status_code == 404 and result.json() == {"code": "not_found"}
        assert not wire.requests


@pytest.mark.parametrize("mode", ["bearer", "user", "admin"])
@pytest.mark.parametrize(
    ("kind", "key", "text"),
    [
        pytest.param("chat_session", "title", "合成标题", id="session"),
        pytest.param("chat_message", "text", "合成消息" * 6000, id="message"),
        pytest.param("chat_attachment", "text", "合成图片", id="attachment"),
    ],
)
def test_source_evidence_projects_only_bounded_text_with_live_ordinary_access(
    mode, kind, key, text
):
    wire = MockWire()
    row = raw(kind, **{key: text}, file_ref={"path": "private"}, filename="private")
    wire.reader.records[row.id] = row
    path = f"/v1/source-evidence/{row.id}"
    no_auth = mode != "bearer"
    config = settings(
        "http://storage.invalid",
        chat_evidence_read=True,
        public_origin=ORIGIN,
        test_no_auth=no_auth,
        test_no_auth_role="admin" if mode == "admin" else "user",
    )
    with TestClient(
        create_app(config, upstream=Upstream(config, transport=httpx.MockTransport(wire.handle))),
        base_url=ORIGIN,
    ) as client:
        session = client.get("/v1/session")
        assert session.json() == {"test_no_auth": no_auth, "chat_evidence_read": True}
        assert session.headers["cache-control"] == "no-store"
        if not no_auth:
            assert client.get(path).status_code == 401
        assert not wire.requests
        request_headers = {} if no_auth else headers()
        result = client.get(path, headers=request_headers)
        assert result.status_code == 200
        for name, value in HEADERS.items():
            assert result.headers[name] == value
        assert "access-control-allow-origin" not in result.headers
        assert result.json() == {
            "id": row.id,
            "kind": kind,
            "text": text[:20000],
            "start": "2026-11-18T00:00:10Z",
            "end": "2026-11-18T00:00:20Z",
        }
        before_generic = wire.requests.copy()
        record_path = f"/v1/records/{kind}"
        for suffix in ("", f"/{row.id}", f"/{row.id}?version=1", f"/{row.id}/history"):
            hidden = client.get(record_path + suffix, headers=request_headers)
            assert hidden.status_code == 404 and hidden.json() == {"code": "not_found"}
        assert wire.requests == before_generic
        assert wire.reader.gets == [row.id] and not wire.reader.queries
        wire.reader.denied_ids.add(row.id)
        if no_auth:
            # A caller-supplied admin bearer cannot override the fixed ordinary binding.
            request_headers = headers(admin=True)
        denied = client.get(path, headers=request_headers)
        assert denied.status_code == 403 and denied.json() == {"code": "forbidden"}
        assert denied.headers["cache-control"] == "no-store"
        wire.revoked.add(STORAGE_USER)
        revoked = client.get(path, headers=request_headers)
        assert revoked.status_code == 401 and revoked.json() == {"code": "unauthenticated"}
        assert revoked.headers["cache-control"] == "no-store"
        assert {token for _, token in wire.requests} == {STORAGE_USER}


@pytest.mark.parametrize("origin", [None, "http://192.0.2.1:18186", "http://127.0.0.2:18186"])
def test_source_evidence_no_auth_still_requires_explicit_loopback(origin):
    with pytest.raises(ValidationError, match="explicit loopback public_origin"):
        settings(
            "http://storage.invalid",
            chat_evidence_read=True,
            test_no_auth=True,
            public_origin=origin,
        )


def test_source_evidence_no_auth_rejects_invalid_requests_before_storage():
    wire = MockWire()
    config = settings(
        "http://storage.invalid", chat_evidence_read=True, test_no_auth=True, public_origin=ORIGIN
    )
    path = f"/v1/source-evidence/{new_id()}"
    with TestClient(
        create_app(config, upstream=Upstream(config, transport=httpx.MockTransport(wire.handle))),
        base_url=ORIGIN,
    ) as client:
        for invalid_headers, code in (
            ({"Host": "localhost:18186"}, "host_denied"),
            ({"Host": "127.0.0.1:8086"}, "host_denied"),
            ({"Origin": "http://127.0.0.1:9999"}, "cross_origin_denied"),
            ({"Origin": "null"}, "cross_origin_denied"),
            ({"Sec-Fetch-Site": "same-site"}, "cross_origin_denied"),
            ({"Sec-Fetch-Site": "cross-site"}, "cross_origin_denied"),
        ):
            denied = client.get(path, headers=invalid_headers)
            assert denied.status_code == 403 and denied.json() == {"code": code}
            assert denied.headers["cache-control"] == "no-store"
            assert "access-control-allow-origin" not in denied.headers
        for invalid in ("/v1/source-evidence/invalid-id", path + "?version=1"):
            result = client.get(invalid)
            assert result.status_code == 422 and result.json() == {"code": "invalid_input"}
        assert client.post(path, headers={"Origin": ORIGIN}).status_code == 405
        assert not wire.requests
        result = client.get(path, headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"})
        assert result.status_code == 404 and result.json() == {"code": "not_found"}
        audio = next(r for r in wire.reader.records.values() if r and r.data_type == "audio_ref")
        result = client.get(f"/v1/source-evidence/{audio.id}")
        assert result.status_code == 404 and result.json() == {"code": "not_found"}
