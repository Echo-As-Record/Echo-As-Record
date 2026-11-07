"""Real loopback TCP wire-v1 simulation, not a PostgreSQL/NATS integration claim."""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from ear_api.app import create_app
from ear_api.config import Settings
from ear_contracts import DataTypeSpec, new_id
from ear_storage.management_input import ManagementInput
from ear_storage_client import MemoryStorage
from ear_storage_client.engine import canonical_json
from ear_storage_client.wire import (
    decode_read,
    decode_request,
    encode_read_result,
    encode_result,
)
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient

USER = "u" * 43
ADMIN = "a" * 43
STORAGE_USER = "s" * 43
STORAGE_ADMIN = "z" * 43


def headers(admin=False):
    return {"Authorization": "Bearer " + (ADMIN if admin else USER)}


@contextmanager
def tcp_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("server startup failed")
            time.sleep(0.01)
        else:
            raise RuntimeError("server startup timeout")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "TCP server did not stop"


class FakeStorage:
    def __init__(self):
        self.storage = MemoryStorage()
        self.client = self.storage.client("api")
        self.app = FastAPI()
        self.requests = []
        self.receipts = {}
        self.managed = {}
        self.claimed = set()
        self.snapshots = {}
        self.revoked = set()
        self.fail_after_write = False
        self.fail_claim = False
        self.upstream_admin = True
        self.bind()

    def auth(self, request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in {STORAGE_USER, STORAGE_ADMIN} or token in self.revoked:
            return None
        return token

    def bind(self):
        app = self.app

        @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
        async def route(path: str, request: Request):
            token = self.auth(request)
            self.requests.append((request.method, path, token))
            if token is None:
                return Response(status_code=401)
            if path == "v1/identity":
                return {"codec": 1, "module": "api"}
            if path == "v1/registry":
                registry = self.client.registry.extend(
                    *[
                        DataTypeSpec("demo_result", "semantic", "demo"),
                        DataTypeSpec("other_result", "semantic", "other"),
                    ]
                )
                return {
                    "codec": 1,
                    "registry": registry.to_json(),
                    "core_types": sorted(self.client.registry.core_types),
                }
            if path == "v1/batches":
                data = await request.json()
                rid = data.pop("request_id")
                text = canonical_json(data)
                key = (token, rid)
                if key in self.receipts:
                    previous, output = self.receipts[key]
                    if previous != text:
                        return Response(status_code=422)
                else:
                    ops, context = decode_request(text)
                    output = encode_result(self.client.write(ops, context))
                    self.receipts[key] = (text, output)
                if self.fail_after_write:
                    self.fail_after_write = False
                    return Response("PRIVATE upstream error", status_code=503)
                return Response(output, media_type="application/json")
            if path == "v1/read":
                parsed, args = decode_read(await request.body())
                reader = self.snapshots.get(parsed.snapshot_id, self.client)
                if parsed.method.value == "query":
                    dtype = args["query"].data_type
                    if token == STORAGE_USER and dtype == "person":
                        return Response(status_code=403)
                value = getattr(reader, parsed.method.value)(**args)
                return Response(
                    encode_read_result(parsed.method, value), media_type="application/json"
                )
            if path == "v1/snapshots":
                sid = new_id()
                # Memory snapshot is immutable after releasing its context.
                with self.client.snapshot() as reader:
                    self.snapshots[sid] = reader
                return {"codec": 1, "lease": {"snapshot_id": sid}}
            if path.startswith("v1/snapshots/"):
                self.snapshots.pop(path.rsplit("/", 1)[-1], None)
                return {"codec": 1, "released": True}
            if path.startswith("v1/management/"):
                if token != STORAGE_ADMIN or not self.upstream_admin:
                    return Response(status_code=403)
                if path == "v1/management/plugins":
                    limit = int(request.query_params.get("limit", "50"))
                    after = request.query_params.get("after", "")
                    names = [n for n in ("demo", "other") if n > after]
                    return {
                        "codec": 1,
                        "items": [
                            {
                                "module": n,
                                "types": [],
                                "enabled": None,
                                "online": None,
                                "status_source": "storage_management",
                            }
                            for n in names[:limit]
                        ],
                        "next_after": names[limit - 1] if len(names) > limit else None,
                    }
                if path == "v1/management/requests":
                    parsed = ManagementInput.model_validate(await request.json())
                    previous = self.managed.get(parsed.request_id)
                    text = canonical_json(parsed.payload())
                    if previous:
                        if previous[0] != text:
                            return Response(status_code=422)
                        result = previous[1]
                    else:
                        result = {
                            "status": "accepted",
                            "feedback_id": new_id(),
                            "result_id": new_id(),
                        }
                        self.managed[parsed.request_id] = (text, result)
                    return Response(
                        json.dumps(result), status_code=202, media_type="application/json"
                    )
                fid = path.split("/")[-2]
                if fid in self.claimed:
                    return Response(status_code=403)
                self.claimed.add(fid)
                if self.fail_claim:
                    return Response("private credential failure", status_code=503)
                return {"principal_id": new_id(), "token": "c" * 43}
            if path.startswith("v1/history/jobs/"):
                return {"job_id": path.rsplit("/", 1)[-1], "status": "planned"}
            return Response(status_code=404)


def settings(url, **kwargs):
    return Settings.model_validate(
        {
            "storage_url": url,
            "public_origin": "http://testserver",
            "bindings": [
                {"subject": "owner", "role": "user", "token": USER, "storage_token": STORAGE_USER},
                {
                    "subject": "admin",
                    "role": "admin",
                    "token": ADMIN,
                    "storage_token": STORAGE_ADMIN,
                },
            ],
            **kwargs,
        }
    )


@pytest.fixture
def gateway():
    fake = FakeStorage()
    with tcp_server(fake.app) as url:
        config = settings(url)
        app = create_app(config)
        with TestClient(app) as client:
            yield client, fake, config
