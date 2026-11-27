from datetime import UTC, datetime

import httpx
import pytest
from ear_api.app import create_app as api_app
from ear_api.effective_time import TimeProjection
from ear_contracts import DEFAULT_REGISTRY, DataTypeSpec, Registry, new_id
from ear_storage import PostgresStorage, bootstrap, migrate
from ear_storage.auth import AuthOperator
from ear_storage.http import create_app as storage_app
from ear_storage.service import StorageService
from ear_storage_client import AppendRaw, Context, Create, Supersede
from ear_storage_client.testing import make_raw
from fastapi.testclient import TestClient

from ...api.tests.conftest import headers, settings
from .test_api_gateway_integration import headers as headers_for
from .test_api_gateway_integration import loopback

pytestmark = pytest.mark.postgres


def test_week_projection_real_storage_and_fresh_grants(pg_dsn):
    migrate(pg_dsn)
    types = dict(DEFAULT_REGISTRY.data_types)
    types.update(
        {
            k: DataTypeSpec(k, "raw_record", "adapter")
            for k in ("chat_session", "chat_message", "chat_attachment")
        }
    )
    bootstrap(
        pg_dsn, Registry(types, DEFAULT_REGISTRY.event_publishers, DEFAULT_REGISTRY.core_types)
    )
    store, auth = PostgresStorage(pg_dsn), AuthOperator(pg_dsn)
    audio = make_raw("audio_ref", start="2026-11-02T04:55:00Z", end="2026-11-02T05:06:00Z")
    store.client("adapter").write(
        [
            AppendRaw(
                make_raw(
                    "chat_message",
                    {"source_system": "librechat", "text": "SYNTHETIC_PRIVATE"},
                    start="2026-11-01T06:03:00Z",
                    channel="librechat",
                )
            ),
            AppendRaw(
                make_raw(
                    "chat_attachment",
                    {"source_system": "wrong"},
                    start="2026-11-01T07:03:00Z",
                    channel="librechat",
                )
            ),
            AppendRaw(audio),
            AppendRaw(
                make_raw(
                    "chat_session",
                    {"source_system": "librechat"},
                    start="2026-11-02T06:00:00Z",
                    channel="librechat",
                )
            ),
        ],
        Context(root_feedback_id="system"),
    ).check()
    active, replaced = new_id(), new_id()
    extraction = store.client("extraction")
    extraction.write(
        [
            Create(
                "activity",
                active,
                {},
                event_start=datetime(2026, 11, 1, 12, 1, tzinfo=UTC),
                event_end=datetime(2026, 11, 1, 12, 5, tzinfo=UTC),
            ),
            Create(
                "activity",
                replaced,
                {},
                event_start=datetime(2026, 11, 1, 14, tzinfo=UTC),
                event_end=datetime(2026, 11, 1, 15, tzinfo=UTC),
            ),
        ],
        Context(root_feedback_id="system"),
    ).check()
    extraction.write(
        [Supersede("activity", replaced, 1)], Context(root_feedback_id="system")
    ).check()
    grants = {
        "audio_ref",
        "transcript_segment",
        "chat_session",
        "chat_message",
        "chat_attachment",
        "activity",
    }
    own = auth.issue_principal("api", read_types=grants)
    other = auth.issue_principal("api", admin=True)
    adapter = auth.issue_principal("adapter", read_types={"audio_ref"})
    seq = store.commit_seq
    with loopback(storage_app(StorageService(pg_dsn))) as origin:
        body = {
            "days": [{"start": "2026-11-02T04:00:00Z", "end": "2026-11-02T06:00:00Z"}],
            "include_chat": False,
            "shift": {"seconds": 1800, "ids": {"audio_ref": [audio.id]}},
        }
        with httpx.Client(base_url=origin, trust_env=False, headers=headers_for(own.token)) as wire:
            shifted = wire.post("/v1/coverage/week", json=body)
            assert shifted.status_code == 200, shifted.text
            assert shifted.json()["timeline"]["days"] == ["0" * 17 + "111" + "0000"]
            body["shift"] = {"seconds": 1800, "ids": {"chat_message": [audio.id]}}
            assert wire.post("/v1/coverage/week", json=body).status_code == 422
        with httpx.Client(
            base_url=origin, trust_env=False, headers=headers_for(adapter.token)
        ) as wire:
            body["shift"] = {"seconds": 1800, "ids": {"audio_ref": [audio.id]}}
            assert wire.post("/v1/coverage/week", json=body).status_code == 403
        config = settings(origin, chat_evidence_read=True)
        bindings = tuple(
            binding.model_copy(
                update={
                    "storage_token": type(binding.storage_token)(
                        other.token if binding.role == "admin" else own.token
                    )
                }
            )
            for binding in config.bindings
        )
        config = config.model_copy(update={"bindings": bindings})
        with TestClient(api_app(config)) as api:
            query = {
                "days": "2026-11-01T04:00:00Z/2026-11-02T05:00:00Z,"
                "2026-11-02T05:00:00Z/2026-11-02T06:00:00Z"
            }
            path = "/v1/week-coverage"
            assert api.get(path, params=query).status_code == 401
            assert (
                api.get(
                    path, params=query, headers={**headers(), "Origin": "https://outside.invalid"}
                ).status_code
                == 403
            )
            assert (
                api.get(
                    path,
                    params={"days": "2026-11-01T04:00:00Z/2026-11-03T05:00:00Z"},
                    headers=headers(),
                ).status_code
                == 422
            )
            response = api.get(path, params=query, headers=headers())
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            data = response.json()
            assert data == {
                "slot_seconds": 300,
                "timeline": {
                    "state": "ready",
                    "days": ["0" * 24 + "1" + "0" * 274 + "1", "11" + "0" * 10],
                },
                "activity": {"state": "ready", "days": ["0" * 96 + "1" + "0" * 203, "0" * 12]},
            }
            assert "SYNTHETIC_PRIVATE" not in response.text
            assert api.get(path, params=query, headers=headers(admin=True)).json()["timeline"] == {
                "state": "unavailable",
                "days": ["", ""],
            }
            auth.set_grants(own.principal_id, read_types=grants - {"chat_message", "activity"})
            after = api.get(path, params=query, headers=headers()).json()
            assert (
                after["timeline"]["state"] == "partial"
                and after["timeline"]["days"][0] == "0" * 299 + "1"
            )
            assert after["activity"] == {"state": "unavailable", "days": ["", ""]}
            auth.set_grants(own.principal_id, read_types=grants)
            projection = TimeProjection(1800, {"audio_ref": [audio.id]})
            with TestClient(api_app(config, time_projection=projection)) as corrected:
                on = corrected.get(path, params=query, headers=headers())
                assert on.status_code == 200, on.text
                assert on.json() == {
                    **data,
                    "timeline": {
                        "state": "ready",
                        "days": ["0" * 24 + "1" + "0" * 275, "0" * 5 + "111" + "0" * 4],
                    },
                }
                auth.set_grants(own.principal_id, read_types=grants - {"audio_ref"})
                without_audio = corrected.get(path, params=query, headers=headers())
                assert without_audio.json()["timeline"] == {
                    "state": "partial",
                    "days": ["0" * 24 + "1" + "0" * 275, "0" * 12],
                }
            auth.revoke(own.principal_id)
            assert api.get(path, params=query, headers=headers()).status_code == 401
    assert store.commit_seq == seq
