"""合成 HTTP 替身及临时回环服务器；不读取真实源或凭据。"""

import io
import json
import threading
import traceback
import urllib.error
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest
from ear_adapter import AdapterConfig, AlreadyIngested, InboxAudioLocator, Ingested, Runner
from ear_adapter.source import HttpEarSource, SourceProtocolError, SourceUnavailable
from ear_adapter.testing import AUDIO_BYTES, AUDIO_FILE, embedding_json, evidence_json
from ear_storage_client import MemoryStorage

SENTINEL = "sentinel-private-body-and-credential"


@contextmanager
def server(respond):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            code, headers, body = respond(self.path)
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", calls
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


def json_response(data):
    return 200, {"Content-Type": "application/json"}, json.dumps(data).encode()


def mock_source(body):
    source = HttpEarSource("http://127.0.0.1:1/internal", SENTINEL)

    class Response(io.BytesIO):
        status = 200

    opener = Mock()
    opener.open.return_value = Response(body)
    source._opener = opener
    return source, opener


@pytest.mark.parametrize("method", ["capture", "embedding", "list"])
@pytest.mark.parametrize(
    "body",
    [
        b"not-json-sentinel-private-body-and-credential",
        b'"sentinel-private-body-and-credential"',
        b'{"private": "sentinel-private-body-and-credential"}',
        b"\xff",
    ],
)
def test_malformed_response_errors_sanitized(method, body):
    source, _ = mock_source(body)
    with pytest.raises(SourceProtocolError) as exc:
        if method == "capture":
            source.read_capture("cap-1")
        elif method == "embedding":
            source.read_embedding("resolution-1")
        else:
            list(source.list_captures())
    assert SENTINEL not in "".join(traceback.format_exception(exc.value))
    assert "ValidationError" not in str(exc.value)


@pytest.mark.parametrize(
    "page",
    [
        {"items": "sentinel-private-body-and-credential", "total": 1},
        {"items": [], "total": "private"},
        {"items": [{}], "total": 1},
        {"items": [], "total": -1},
        {"items": [{"capture_id": "../private"}], "total": 1},
    ],
)
def test_list_schema_sanitized(page):
    source, _ = mock_source(json.dumps(page).encode())
    with pytest.raises(SourceProtocolError):
        list(source.list_captures())


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_never_followed(code):
    with server(lambda _: json_response({"leak": True})) as (target, target_calls):
        with server(lambda _: (code, {"Location": target + "/" + SENTINEL}, SENTINEL.encode())) as (
            url,
            calls,
        ):
            source = HttpEarSource(url, SENTINEL)
            with pytest.raises(urllib.error.HTTPError) as exc:
                source.read_capture("cap-1")
            assert exc.value.code == code
            assert SENTINEL not in "".join(traceback.format_exception(exc.value))
            assert len(calls) == 1
        assert target_calls == []


def test_environment_proxy_is_never_used(monkeypatch):
    with server(lambda _: json_response({"proxy": SENTINEL})) as (proxy, proxy_calls):
        with server(lambda _: json_response(evidence_json())) as (url, calls):
            for name in (
                "http_proxy",
                "HTTP_PROXY",
                "https_proxy",
                "HTTPS_PROXY",
                "all_proxy",
                "ALL_PROXY",
            ):
                monkeypatch.setenv(name, proxy)
            for name in ("no_proxy", "NO_PROXY"):
                monkeypatch.setenv(name, "")
            assert HttpEarSource(url, SENTINEL).read_capture("cap-1").capture_id == "cap-1"
            assert calls == [("/api/captures/cap-1/evidence", "Bearer " + SENTINEL)]
        assert proxy_calls == []


@pytest.mark.parametrize("code", [401, 403, 404, 500, 503])
def test_http_errors_have_no_body_headers_url_or_reason(code):
    source, opener = mock_source(b"")
    body = io.BytesIO(SENTINEL.encode())
    headers = Message()
    headers["X-Private"] = SENTINEL
    opener.open.side_effect = urllib.error.HTTPError(
        "http://private/" + SENTINEL, code, SENTINEL, headers, body
    )
    expected = SourceUnavailable if code >= 500 else urllib.error.HTTPError
    with pytest.raises(expected) as exc:
        source.read_capture("cap-1")
    assert body.closed
    assert SENTINEL not in "".join(traceback.format_exception(exc.value))
    if isinstance(exc.value, urllib.error.HTTPError):
        assert exc.value.code == code
        assert SENTINEL not in str(exc.value.headers)
        assert exc.value.read() == b""


@pytest.mark.parametrize(
    "error", [urllib.error.URLError(SENTINEL), TimeoutError(SENTINEL), ConnectionError(SENTINEL)]
)
def test_network_errors_sanitized(error):
    source, opener = mock_source(b"")
    opener.open.side_effect = error
    with pytest.raises(SourceUnavailable) as exc:
        source.read_capture("cap-1")
    assert SENTINEL not in "".join(traceback.format_exception(exc.value))


def test_url_encoding_and_bearer_not_in_url():
    source, opener = mock_source(json.dumps(evidence_json()).encode())
    source.read_capture("opaque?#%id")
    request = opener.open.call_args.args[0]
    assert request.full_url.endswith("/api/captures/opaque%3F%23%25id/evidence")
    assert request.get_header("Authorization") == "Bearer " + SENTINEL
    assert SENTINEL not in request.full_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/private",
        "ftp://localhost/private",
        "http://user:sentinel-private-body-and-credential@localhost",
        "http://localhost/?sentinel-private-body-and-credential",
        "http://localhost/#private",
        "http://localhost:\nsentinel-private-body-and-credential",
    ],
)
def test_invalid_base_urls_fail_sanitized(url):
    with pytest.raises(SourceProtocolError) as exc:
        HttpEarSource(url, SENTINEL)
    assert SENTINEL not in "".join(traceback.format_exception(exc.value))


def test_missing_list_404_explicit_evidence_import_and_new_runner(tmp_path):
    audio = tmp_path / AUDIO_FILE.path
    audio.parent.mkdir(parents=True)
    audio.write_bytes(AUDIO_BYTES)

    def respond(path):
        if path == "/internal/api/captures/cap-1/evidence":
            return json_response(evidence_json())
        if path.startswith("/internal/api/evidence/speaker_resolution/"):
            return json_response(embedding_json(path.split("/")[-2]))
        return 404, {}, SENTINEL.encode()

    with server(respond) as (url, calls):
        source = HttpEarSource(url + "/internal", "synthetic-token")
        storage = MemoryStorage()

        def fresh():
            return Runner(
                source=source,
                storage=storage.client("adapter"),
                locator=InboxAudioLocator(tmp_path),
                config=AdapterConfig(data_root=tmp_path),
            )

        assert isinstance(fresh().run_once(capture_ids=["cap-1"]).outcomes[0], Ingested)
        events = len(storage.events)
        assert isinstance(fresh().run_once(capture_ids=["cap-1"]).outcomes[0], AlreadyIngested)
        assert len(storage.events) == events == 13
        assert len(calls) == 4  # 首轮 evidence + 两条 embedding；重启只读 evidence。
        assert all("?offset=" not in path for path, _ in calls)
        with pytest.raises(urllib.error.HTTPError) as exc:
            fresh().run_once()
        assert exc.value.code == 404
        assert calls[-1][0] == "/internal/api/captures?offset=0&limit=100"
