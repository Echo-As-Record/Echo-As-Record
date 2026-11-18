import httpx
import pytest
from ear_ingest.transcription import transcription_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "upstream",
    [
        "https://127.0.0.1:443",
        "http://outside.test:80",
        "http://127.0.0.1:80/path",
        "http://token@127.0.0.1:80",
        "http://localhost:80",
        "http://127.0.0.1:80?callback=x",
    ],
)
def test_rejects_nonlocal_or_ambiguous_origin(upstream):
    with pytest.raises(ValueError):
        transcription_router("test", upstream)


def test_forwarder_only_authenticates_bounded_pcm_and_does_not_redirect():
    calls = []

    def endpoint(request):
        calls.append(request)
        return (
            httpx.Response(307, headers={"location": "http://outside.test"})
            if request.method == "PUT"
            else httpx.Response(200, json={"utterances": []})
        )

    app = FastAPI()
    app.include_router(
        transcription_router("test", "http://[::1]:8793/", transport=httpx.MockTransport(endpoint))
    )
    client = TestClient(app)
    path = "/api/v1/transcription/test-example"
    assert client.put(path + "/chunks/0").status_code == 401
    headers = {"Authorization": "Bearer test", "Content-Type": "application/octet-stream"}
    assert client.put(path + "/chunks/0", headers=headers, content=bytes(64002)).status_code == 413
    assert client.put(path + "/chunks/0", headers=headers, content=b"x").status_code == 400
    assert not calls
    assert client.put(path + "/chunks/0", headers=headers, content=b"xx").status_code == 502
    assert len(calls) == 1 and calls[0].url.host == "::1"
    response = client.get(path + "?limit=1&ignored=1", headers=headers)
    assert str(calls[-1].url).endswith("test-example?limit=1")
    assert response.json() == {"utterances": []} and response.headers["Cache-Control"] == "no-store"
