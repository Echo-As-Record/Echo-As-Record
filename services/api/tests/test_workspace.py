"""Actual flat static routing and Boundary headers through the real ASGI app."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import pytest
from ear_api.app import create_app
from ear_api.boundary import HEADERS
from ear_storage_client.queries import Page
from fastapi.testclient import TestClient

from .conftest import headers, settings
from .test_evidence import evidence_http as evidence_http


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []
        self.inline_handlers: list[str] = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key.lower().startswith("on"):
                self.inline_handlers.append(key)
            if (tag, key) in {("script", "src"), ("link", "href")} and value:
                self.urls.append(value)


def test_workspace_entry_and_assets_are_exact_flat_files_with_boundary_csp(evidence_http):
    client, wire, config = evidence_http
    assert config.static_dir is not None
    result = client.get("/workspace/")
    assert result.status_code == 200
    assert result.content == (config.static_dir / "workspace.html").read_bytes()
    assert result.headers["content-type"].startswith("text/html")
    parsed = Assets()
    parsed.feed(result.text)
    assert parsed.urls == ["workspace.css", "workspace.js"]
    assert parsed.inline_handlers == []
    for url in parsed.urls:
        resolved = urljoin(str(result.url), url)
        assert urlsplit(resolved).netloc == "testserver"
        asset = client.get(resolved)
        assert asset.status_code == 200
        assert asset.content == (config.static_dir / url).read_bytes()
        assert (
            "text/css" in asset.headers["content-type"]
            if url.endswith(".css")
            else "javascript" in asset.headers["content-type"]
        )
        for key, value in HEADERS.items():
            assert asset.headers[key] == value
    for key, value in HEADERS.items():
        assert result.headers[key] == value
    assert "'unsafe-inline'" not in result.headers["content-security-policy"]
    assert "'unsafe-eval'" not in result.headers["content-security-policy"]
    assert not wire.requests  # Public shell does not authenticate or preload data.


@pytest.mark.parametrize(
    "path",
    [
        "/workspace",
        "/workspace/index.html",
        "/workspace/ws-app.js",
        "/workspace/workspace/index.html",
        "/workspace/app.js",
        "/workspace/styles.css",
        "/workspace/unknown",
        "/workspace/%2e%2e/app.py",
        "/workspace/.env",
    ],
)
def test_workspace_is_not_legacy_or_arbitrary_static_directory(evidence_http, path):
    client, wire, _ = evidence_http
    result = client.get(path, follow_redirects=False)
    assert result.status_code == 404 and result.json() == {"code": "not_found"}
    assert result.headers["content-security-policy"] == HEADERS["Content-Security-Policy"]
    assert not wire.requests


@pytest.mark.parametrize(
    "path", ["/workspace/", "/workspace/workspace.js", "/workspace/workspace.css"]
)
def test_workspace_head_and_non_read_methods(evidence_http, path):
    client, _, _ = evidence_http
    result = client.head(path)
    assert result.status_code == 200 and result.content == b""
    assert result.headers["content-security-policy"] == HEADERS["Content-Security-Policy"]
    assert client.post(path).status_code == 405
    assert client.put(path).status_code == 405


def test_plugins_preserved_byte_for_byte(evidence_http):
    client, wire, config = evidence_http
    assert config.static_dir is not None
    for path, filename in [
        ("/plugins/", "index.html"),
        ("/plugins/app.js", "app.js"),
        ("/plugins/core.js", "core.js"),
        ("/plugins/styles.css", "styles.css"),
    ]:
        result = client.get(path)
        assert result.status_code == 200
        assert result.content == (config.static_dir / filename).read_bytes()
        assert result.headers["content-security-policy"] == HEADERS["Content-Security-Policy"]
    assert client.get("/plugins", follow_redirects=False).status_code == 404
    assert not wire.requests


def test_static_disabled_disables_both_surfaces():
    config = settings("http://storage.invalid", static_dir=None)
    with TestClient(create_app(config)) as client:
        for path in ("/workspace/", "/workspace/workspace.js", "/plugins/", "/plugins/app.js"):
            result = client.get(path)
            assert result.status_code == 404


def test_workspace_does_not_fall_back_when_flat_entry_missing(tmp_path):
    # A legacy index must not become the workspace entry, even if it exists.
    (tmp_path / "index.html").write_text("legacy prototype", encoding="utf-8")
    config = settings("http://storage.invalid")
    assert config.static_dir is not None
    config = config.model_copy(update={"static_dir": tmp_path})
    with TestClient(create_app(config)) as client:
        assert client.get("/workspace/").status_code == 404
        assert client.get("/workspace/index.html").status_code == 404


@pytest.mark.parametrize("path", ["/workspace/", "/workspace/workspace.js", "/plugins/"])
def test_cross_origin_static_request_denied(evidence_http, path):
    client, wire, _ = evidence_http
    result = client.get(path, headers={"Origin": "https://untrusted.invalid"})
    assert result.status_code == 403 and result.json() == {"code": "cross_origin_denied"}
    assert "access-control-allow-origin" not in result.headers
    assert not wire.requests


def test_existing_authorized_full_record_contract_is_not_silently_projected(evidence_http):
    client, wire, _ = evidence_http
    audio = next(
        row for row in wire.reader.records.values() if row and row.data_type == "audio_ref"
    )
    person = next(row for row in wire.reader.records.values() if row and row.data_type == "person")
    for row in (audio, person):
        result = client.get(f"/v1/records/{row.data_type}/{row.id}", headers=headers())
        assert result.status_code == 200 and result.json() == row.model_dump(mode="json")
    assert "file_ref" in audio.payload
    # Projection is evidence-only, not a new field-level ACL for ordinary readers.
    wire.reader.query_fn = lambda q: Page((audio,), None)
    result = client.get("/v1/records/audio_ref", headers=headers())
    assert result.status_code == 200
    assert result.json()["items"] == [audio.model_dump(mode="json")]
    assert result.json()["consistency"] == "live"


def test_full_record_version_contract_and_authorization_unchanged(evidence_http):
    client, wire, _ = evidence_http
    person = next(row for row in wire.reader.records.values() if row and row.data_type == "person")
    url = f"/v1/records/person/{person.id}?version=1"
    result = client.get(url, headers=headers())
    assert result.status_code == 200 and result.json() == person.model_dump(mode="json")
    wire.revoked.add(configured_token := wire.requests[-1][1])
    assert client.get(url, headers=headers()).status_code == 401
    assert wire.requests[-1] == ("/v1/identity", configured_token)


def test_workspace_conditional_response_keeps_boundary_headers(evidence_http):
    client, _, _ = evidence_http
    asset = client.get("/workspace/workspace.js")
    result = client.get("/workspace/workspace.js", headers={"If-None-Match": asset.headers["etag"]})
    assert result.status_code == 304
    for key, value in HEADERS.items():
        assert result.headers[key] == value
