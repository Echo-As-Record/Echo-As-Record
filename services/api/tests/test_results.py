"""Results static routing uses the existing ASGI boundary and reads no records."""

import pytest
from ear_api.app import create_app
from ear_api.boundary import HEADERS
from ear_storage_client.queries import Page
from fastapi.testclient import TestClient

from .conftest import headers, settings
from .test_evidence import evidence_http as evidence_http
from .test_workspace import Assets


def test_results_home_and_same_origin_assets(evidence_http):
    client, wire, config = evidence_http
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/results/"
    assert root.headers["cache-control"] == "no-store"
    page = client.get("/results/?date=2026-10-12")
    assert page.status_code == 200
    assert config.static_dir is not None
    assert page.content == (config.static_dir / "results.html").read_bytes()
    assets = Assets()
    assets.feed(page.text)
    assert assets.inline_handlers == []
    assert assets.urls == [
        "/results/results.css",
        "/workspace/workspace.js",
        "/results/katex.min.js",
        "/results/results.js",
    ]
    for url in assets.urls:
        r = client.get(url)
        assert r.status_code == 200
        assert r.content == (config.static_dir / url.split("/")[-1]).read_bytes()
        for key, value in HEADERS.items():
            assert r.headers[key] == value
    assert not wire.requests


@pytest.mark.parametrize(
    "suffix",
    ["/", "/results.html", "/results.js", "/results.css", "/katex.min.js", "/KaTeX-LICENSE.txt"],
)
def test_results_only_static_reads(evidence_http, suffix):
    client, wire, _ = evidence_http
    path = "/results" + suffix
    head = client.head(path)
    assert head.status_code == 200 and not head.content
    assert client.post(path).status_code == 405
    assert client.put(path).status_code == 405
    assert client.get(path, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    assert not wire.requests


@pytest.mark.parametrize(
    "path",
    [
        "/results",
        "/results/index.html",
        "/results/app.js",
        "/results/.env",
        "/workspace/katex.min.js",
    ],
)
def test_results_does_not_serve_other_files(evidence_http, path):
    client, wire, _ = evidence_http
    assert client.get(path, follow_redirects=False).status_code == 404
    assert not wire.requests


def test_results_disabled_with_static_directory():
    config = settings("http://storage.invalid", static_dir=None)
    with TestClient(create_app(config)) as client:
        assert client.get("/", follow_redirects=False).status_code == 404
        assert client.get("/results/", follow_redirects=False).status_code == 404


RECORDING_QUERY = "/v1/recording-intervals?start=2026-10-29T00:00:00Z&end=2026-10-30T00:00:00Z"


def test_recording_intervals_use_existing_grant_and_project_only_times(evidence_http):
    client, wire, _ = evidence_http
    audio = next(r for r in wire.reader.records.values() if r and r.data_type == "audio_ref")
    assert "file_ref" in audio.payload

    def query_only_audio(query):
        assert query.data_type == "audio_ref" and query.time is not None
        assert query.time.start.isoformat() == "2026-10-29T00:00:00+00:00"
        assert query.time.end.isoformat() == "2026-10-30T00:00:00+00:00"
        assert query.limit == 10 and query.cursor == "cursor-a"
        return Page((audio,), "cursor-b")

    wire.reader.query_fn = query_only_audio
    path = RECORDING_QUERY + "&limit=10&cursor=cursor-a"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=headers())
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "start": audio.event_start.isoformat().replace("+00:00", "Z"),
                "end": audio.event_end.isoformat().replace("+00:00", "Z"),
            }
        ],
        "next_cursor": "cursor-b",
    }
    assert response.headers["cache-control"] == "no-store"
    wire.revoked.add(wire.requests[-1][1])
    assert client.get(path, headers=headers()).status_code == 401


@pytest.mark.parametrize("end", ["", "2026-10-29T00:00:00Z", "2026-11-01T00:00:00Z"])
def test_recording_intervals_require_bounded_nonempty_window(evidence_http, end):
    client, wire, _ = evidence_http
    path = "/v1/recording-intervals?start=2026-10-29T00:00:00Z&end=" + end
    assert client.get(path, headers=headers()).status_code == 422
    assert not wire.requests
