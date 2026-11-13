"""Synthetic map responses only; never contact external providers."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import httpx
import jsonschema
import pytest
from ear_adapter.runtime import READ_TYPES as EAR_READ_TYPES
from ear_contracts import NewRawRecord, RawRecord, Source, new_id
from ear_storage_client import AppendRaw, Context, Create, MemoryStorage, Query
from geo_adapter.providers import (
    AmapProvider,
    ConfigurationError,
    NominatimProvider,
    gcj02,
    in_china,
    select,
)
from geo_adapter.runtime import PENDING, Cache, Runner


def point(storage, *, lat=35.0, lon=139.0, at=None):
    at = at or datetime(2026, 11, 1, tzinfo=UTC)
    record = NewRawRecord(
        id=new_id(),
        data_type="gps_point",
        event_start=at,
        event_end=at,
        source=Source(device="synthetic", channel="test", user="default"),
        adapter_version="test/1",
        dedup_key=new_id(),
        payload={"latitude": lat, "longitude": lon},
    )
    storage.write([AppendRaw(record)], Context.system()).check()
    return record


def provider(handler):
    return NominatimProvider(
        httpx.Client(transport=httpx.MockTransport(handler)), "https://map.invalid"
    )


def ok(request):
    assert request.url.path == "/reverse"
    assert request.headers["user-agent"] == "EchoAsRecord-GeoAdapter/0.1"
    return httpx.Response(
        200, json={"display_name": "Synthetic address", "address": {"country": "Test"}}
    )


def test_coordinates_and_selection():
    # Conventional WGS84 reference pair at Beijing, GCJ-02 to approximately 1 cm.
    assert gcj02(39.908823, 116.397470) == pytest.approx((39.9102264981, 116.4037135824), abs=1e-8)
    assert gcj02(35, 139) == (35, 139)
    a = AmapProvider(httpx.Client(transport=httpx.MockTransport(ok)), "synthetic-key")
    n = provider(ok)
    assert select([a, n], 39, 116) is a
    assert select([a, n], 35, 139) is n
    assert select([a], 35, 139) is a
    assert select([n], 39, 116) is n
    assert "gps_geocode" not in EAR_READ_TYPES


def test_cache_restart_rescan_and_pagination(tmp_path):
    store = MemoryStorage().client("adapter")
    calls = []

    def handler(request):
        calls.append(request)
        return ok(request)

    n = provider(handler)
    cache = Cache(tmp_path)
    # More than one pending page; all round to the same request coordinate.
    for i in range(103):
        point(store, lat=35.000001 + i * 1e-8)
    assert Runner(store, [n], cache, delay=0).run_once()["written"] == 103
    assert len(calls) == 1
    assert store.count(PENDING) == 0
    cache.close()
    cache = Cache(tmp_path)
    assert Runner(store, [n], cache, delay=0).run_once()["written"] == 0
    p = point(store)
    assert Runner(store, [n], cache, delay=0).run_once()["written"] == 1
    assert len(calls) == 1
    result = store.query(Query("gps_geocode", where={"gps_point_id": p.id})).items[0]
    assert isinstance(result, RawRecord)
    assert result.event_start == result.event_end == p.event_start
    assert result.dedup_key == f"{p.id}:geocode:nominatim"
    import json
    from pathlib import Path

    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/records/raw-record.schema.json"
        ).read_text()
    )
    jsonschema.validate(result.model_dump(mode="json"), schema)
    cache.close()


@pytest.mark.parametrize(
    "status,body,expected,calls",
    [
        (200, {"error": "Unable to geocode"}, "no_result", 1),
        (404, {"error": "Unable to geocode"}, "no_result", 1),
        (503, {}, "failed", 3),
        (429, {}, "failed", 3),
        (200, {}, "failed", 3),
    ],
)
def test_results(tmp_path, status, body, expected, calls):
    count = []

    def handler(request):
        count.append(request)
        return httpx.Response(status, json=body)

    store = MemoryStorage().client("adapter")
    point(store)
    cache = Cache(tmp_path)
    Runner(store, [provider(handler)], cache, sleep=lambda _: None, max_wait=0).run_once()
    assert store.query(Query("gps_geocode")).items[0].payload["status"] == expected
    assert len(count) == calls
    assert store.count(PENDING) == 0
    cache.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_configuration_errors_do_not_write(tmp_path, status):
    store = MemoryStorage().client("adapter")
    point(store)
    cache = Cache(tmp_path)
    with pytest.raises(ConfigurationError):
        Runner(store, [provider(lambda _: httpx.Response(status, json={}))], cache).run_once()
    assert store.count(Query("gps_geocode")) == 0
    assert store.count(PENDING) == 1
    cache.close()


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"status": "0", "infocode": "10001"}, ConfigurationError),
        (
            {
                "status": "1",
                "regeocode": {"formatted_address": "", "addressComponent": {}, "pois": []},
            },
            "no_result",
        ),
        (
            {
                "status": "1",
                "regeocode": {
                    "formatted_address": "Synthetic",
                    "addressComponent": {"city": [], "streetNumber": {"street": "Example"}},
                    "pois": [
                        {"name": "Far", "distance": "42"},
                        {"name": "Near", "distance": "3", "type": "商务住宅;楼宇;商务写字楼"},
                    ],
                },
            },
            "ok",
        ),
    ],
)
def test_amap(tmp_path, body, expected):
    def handler(request):
        assert request.url.params["extensions"] == "all"
        assert "poitype" not in request.url.params
        assert request.url.params["key"] == "synthetic-key"
        return httpx.Response(200, json=body)

    a = AmapProvider(httpx.Client(transport=httpx.MockTransport(handler)), "synthetic-key")
    coord = a.coordinate(39.908823, 116.397470)
    assert coord["crs"] == "GCJ-02"
    if expected is ConfigurationError:
        with pytest.raises(ConfigurationError):
            a.reverse(coord)
    else:
        result = a.reverse(coord)
        assert result["status"] == expected
        if expected == "ok":
            assert result["building_name"] == "Near"
            assert result["building_distance_m"] == 3
            assert result["city"] is None


def test_amap_municipality_city_falls_back_to_province():
    body = {
        "status": "1",
        "regeocode": {
            "formatted_address": "北京市东城区",
            "addressComponent": {"province": "北京市", "city": [], "district": "东城区"},
            "pois": [],
        },
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    a = AmapProvider(client, "synthetic-key")
    result = a.reverse(a.coordinate(39.9075, 116.3913))
    assert result["city"] == "北京市"


def test_stop_and_resume(tmp_path):
    store = MemoryStorage().client("adapter")
    point(store)
    point(store)
    stopped = []

    def handler(request):
        stopped.append(True)
        return ok(request)

    cache = Cache(tmp_path)
    runner = Runner(store, [provider(handler)], cache, delay=0)
    assert runner.run_once(should_stop=lambda: bool(stopped))["written"] == 1
    assert store.count(PENDING) == 1
    assert Runner(store, [provider(ok)], cache, delay=0).run_once()["written"] == 1
    cache.close()


@pytest.mark.parametrize("status", ["ok", "failed", "no_result"])
def test_extraction_wait_and_evidence(tmp_path, status):
    from ear_extraction.domain import Interval, Waiting
    from ear_extraction.segmentation import inputs
    from geo_adapter.providers import empty

    store = MemoryStorage()
    adapter, attribution, extraction = (
        store.client(x) for x in ("adapter", "attribution", "extraction")
    )
    p = point(adapter)
    place = new_id()
    attribution.write(
        [
            Create("place", place, {"name": "Synthetic"}),
            Create(
                "attribution",
                new_id(),
                {"subject_id": p.id, "target_type": "place", "target_id": place},
            ),
        ],
        Context.system(),
    ).check()
    interval = Interval(start=p.event_start, end=p.event_start + timedelta(hours=1))
    with pytest.raises(Waiting, match="geocode_pending"):
        inputs(extraction, interval)
    outside = Interval(
        start=p.event_start + timedelta(hours=1), end=p.event_start + timedelta(hours=2)
    )
    assert inputs(extraction, outside).raw == []
    n = provider(ok)
    payload = empty(n, n.coordinate(35, 139), status) | {"gps_point_id": p.id}
    record = p.model_copy(
        update={
            "id": new_id(),
            "data_type": "gps_geocode",
            "dedup_key": new_id(),
            "payload": payload,
        }
    )
    adapter.write([AppendRaw(record)], Context.system()).check()
    evidence = inputs(extraction, interval).public(anonymous=True)["evidence"]
    assert any(
        r["data_type"] == "gps_geocode" and r["payload"]["status"] == status for r in evidence
    )


@pytest.mark.parametrize(
    "lat,lon", [(34.69, 135.50), (37.57, 126.98), (21.03, 105.85), (47.92, 106.92), (28.61, 77.21)]
)
def test_neighboring_countries_are_not_converted(lat, lon):
    assert not in_china(lat, lon)
    assert gcj02(lat, lon) == (lat, lon)


def test_dry_run_never_calls_map_or_writes(tmp_path):
    store = MemoryStorage().client("adapter")
    point(store)

    def forbidden(_):
        pytest.fail("dry-run must not call map")

    cache = Cache(tmp_path)
    assert Runner(store, [provider(forbidden)], cache).run_once(dry_run=True) == {"pending": 1}
    assert store.count(Query("gps_geocode")) == 0
    cache.close()


def test_timeout_budget_rate_limit_and_failed_not_cached(tmp_path):
    store = MemoryStorage().client("adapter")
    point(store)
    point(store)
    now, requests = [0.0], []

    def sleep(seconds):
        now[0] += seconds

    def handler(request):
        requests.append(now[0])
        raise httpx.ReadTimeout("SECRET coordinates", request=request)

    cache = Cache(tmp_path)
    runner = Runner(
        store,
        [provider(handler)],
        cache,
        sleep=sleep,
        clock=lambda: now[0],
        delay=2,
        max_wait=0,
    )
    report = runner.run_once()
    assert report["failed"] == 1
    assert report["deferred"] == 1
    assert len(requests) == 3
    report = runner.run_once()
    assert report["failed"] == 1
    assert len(requests) == 6
    assert all(b - a >= 2 for a, b in pairwise(requests))
    cache.close()


def test_storage_failure_preserves_cached_result_for_restart(tmp_path, monkeypatch):
    from ear_storage_client.remote_errors import ServiceUnavailable

    store = MemoryStorage().client("adapter")
    point(store)
    calls = []

    def handler(request):
        calls.append(request)
        return ok(request)

    cache = Cache(tmp_path)
    write = store.write

    def unavailable(*args, **kwargs):
        raise ServiceUnavailable("offline")

    monkeypatch.setattr(store, "write", unavailable)
    with pytest.raises(ServiceUnavailable):
        Runner(store, [provider(handler)], cache).run_once()
    assert store.count(PENDING) == 1
    assert store.count(Query("gps_geocode")) == 0
    cache.close()
    monkeypatch.setattr(store, "write", write)
    cache = Cache(tmp_path)
    assert Runner(store, [provider(handler)], cache).run_once()["written"] == 1
    assert len(calls) == 1
    cache.close()


@pytest.mark.parametrize("exhaust", [False, True])
def test_commit_unknown_replay_and_restart(tmp_path, exhaust):
    from unittest.mock import Mock

    from ear_storage_client.remote import RemoteStorageClient
    from ear_storage_client.remote_errors import CommitUnknown

    local = MemoryStorage().client("adapter")
    point(local)
    remote = Mock(spec=RemoteStorageClient)
    remote.query.side_effect = local.query
    calls = []
    receipt = None

    def write(ops, context, *, request_id):
        nonlocal receipt
        calls.append((ops, context, request_id))
        if receipt is None:
            receipt = local.write(ops, context)
            raise CommitUnknown(request_id)
        assert ops is calls[0][0] and context is calls[0][1]
        assert request_id == calls[0][2]
        if exhaust:
            raise CommitUnknown(request_id)
        return receipt

    remote.write.side_effect = write
    cache = Cache(tmp_path)
    runner = Runner(remote, [provider(ok)], cache, sleep=lambda _: None)
    if exhaust:
        with pytest.raises(CommitUnknown):
            runner.run_once()
        assert len(calls) == 3
    else:
        assert runner.run_once()["written"] == 1
        assert len(calls) == 2
    assert Runner(local, [provider(ok)], cache).run_once()["written"] == 0
    assert local.count(Query("gps_geocode")) == 1
    cache.close()


@pytest.mark.parametrize("age", [0, 21600, 21601])
def test_transient_defers_queue_and_recovers(tmp_path, age):
    ingested = datetime(2026, 11, 13, tzinfo=UTC)
    store = MemoryStorage(clock=lambda: ingested).client("adapter")
    # Old event time must not be mistaken for time spent waiting since ingestion.
    p = point(store)
    for i in range(101):
        point(store, lat=36 + i / 1000)
    requests = []
    recovered = False

    def handler(request):
        requests.append(request)
        return ok(request) if recovered else httpx.Response(503, json={})

    cache = Cache(tmp_path)
    runner = Runner(
        store,
        [provider(handler)],
        cache,
        sleep=lambda _: None,
        clock=lambda: 0,
        wall_clock=lambda: ingested + timedelta(seconds=age),
    )
    report = runner.run_once()
    expired = age > 21600
    assert report == {
        "written": int(expired),
        "failed": int(expired),
        "no_result": 0,
        "deferred": 102 - int(expired),
    }
    assert len(requests) == 3
    results = store.query(Query("gps_geocode")).items
    if expired:
        assert results[0].payload["gps_point_id"] == p.id
        assert results[0].payload["status"] == "failed"
    else:
        assert not results
    recovered = True
    report = runner.run_once()
    assert report["written"] == 102 - int(expired)
    assert report["deferred"] == 0
    assert store.count(PENDING) == 0
    assert all(
        r.payload["status"] == "ok"
        for r in store.query(Query("gps_geocode", limit=200)).items
        if not expired or r.payload["gps_point_id"] != p.id
    )
    cache.close()


@pytest.mark.parametrize(
    "lat,lon",
    [
        ("bad", 139),
        (35, None),
        (91, 139),
        (35, -181),
        (float("nan"), 139),
        (35, float("inf")),
        (True, 139),
    ],
)
def test_invalid_coordinates_fail_and_continue(tmp_path, monkeypatch, lat, lon):
    import json
    from pathlib import Path

    store = MemoryStorage().client("adapter")
    # Storage rejects non-JSON NaN/Infinity on write; inject corrupt read data
    # to exercise the runner's defensive handling without weakening storage.
    bad = point(store)
    query = store.query

    def corrupt_query(spec):
        page = query(spec)
        return replace(
            page,
            items=tuple(
                r.model_copy(update={"payload": {"latitude": lat, "longitude": lon}})
                if r.id == bad.id
                else r
                for r in page.items
            ),
        )

    monkeypatch.setattr(store, "query", corrupt_query)
    good = point(store, at=bad.event_start + timedelta(seconds=1))
    calls = []

    def handler(request):
        calls.append(request)
        return ok(request)

    cache = Cache(tmp_path)
    runner = Runner(store, [provider(handler)], cache, delay=0)
    assert runner.run_once() == {
        "written": 2,
        "failed": 1,
        "no_result": 0,
        "deferred": 0,
    }
    assert len(calls) == 1
    result = store.query(Query("gps_geocode", where={"gps_point_id": bad.id})).items[0]
    assert isinstance(result, RawRecord)
    assert result.payload["provider"] == "invalid"
    assert result.dedup_key == f"{bad.id}:geocode:invalid"
    assert all(
        value is None
        for key, value in result.payload.items()
        if key not in {"gps_point_id", "provider", "status"}
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/records/raw-record.schema.json"
        ).read_text()
    )
    jsonschema.validate(result.model_dump(mode="json"), schema)
    assert (
        store.query(Query("gps_geocode", where={"gps_point_id": good.id}))
        .items[0]
        .payload["status"]
        == "ok"
    )
    assert store.count(PENDING) == 0
    assert runner.run_once()["written"] == 0
    cache.close()


@pytest.mark.parametrize(
    "aois,pois,expected",
    [
        (
            [{"name": "Inside", "type": "120201", "distance": "0"}],
            [{"name": "Nearby", "type": "商务住宅;楼宇", "distance": "1"}],
            ("Inside", "120201", 0),
        ),
        (
            [{"name": "Farther", "type": "120300", "distance": "30"}],
            [{"name": "Office", "type": "商务住宅;楼宇;商务写字楼", "distance": "3"}],
            ("Office", "商务住宅;楼宇;商务写字楼", 3),
        ),
        (
            [],
            [{"name": "Park", "type": "Original", "typecode": "120100", "distance": "200"}],
            ("Park", "Original", 200),
        ),
        (
            [{"name": "Shop", "type": "060100", "distance": "0"}],
            [{"name": "Cafe", "type": "餐饮服务;咖啡厅", "distance": "1"}],
            (None, None, None),
        ),
        (
            [{"name": "Far", "type": "120201", "distance": "200.1"}],
            [{"name": "Farther", "type": "商务住宅;楼宇", "distance": "976"}],
            (None, None, None),
        ),
    ],
)
def test_amap_buildings(aois, pois, expected):
    body = {"status": "1", "regeocode": {"aois": aois, "pois": pois}}

    def handler(request):
        assert request.url.params["extensions"] == "all"
        assert "poitype" not in request.url.params
        return httpx.Response(200, json=body)

    a = AmapProvider(httpx.Client(transport=httpx.MockTransport(handler)), "synthetic-key")
    result = a.reverse(a.coordinate(39, 116))
    assert (
        tuple(result[k] for k in ("building_name", "building_type", "building_distance_m"))
        == expected
    )
    assert result["status"] == ("ok" if expected[0] else "no_result")


@pytest.mark.parametrize("value", [None, "", "bad", "nan", "inf", "-1"])
def test_amap_building_requires_valid_distance(value):
    body = {
        "status": "1",
        "regeocode": {"aois": [{"name": "Unknown distance", "type": "120201", "distance": value}]},
    }
    a = AmapProvider(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))),
        "synthetic-key",
    )
    result = a.reverse(a.coordinate(39, 116))
    assert result["building_name"] is None
    assert result["building_type"] is None
    assert result["building_distance_m"] is None


@pytest.mark.parametrize(
    "address,category,name,expected",
    [
        ({"building": "Address building"}, "shop", "Shop", "Address building"),
        ({"building": "Address building"}, "building", "Other", "Address building"),
        ({}, "building", "Named building", "Named building"),
        ({}, "shop", "Shop", None),
        ({}, "tourism", "Attraction", None),
        ({}, "building", "", None),
    ],
)
def test_nominatim_buildings(address, category, name, expected):
    def handler(request):
        assert request.url.params["zoom"] == "18"
        return httpx.Response(
            200, json={"address": address, "category": category, "name": name, "type": "original"}
        )

    n = provider(handler)
    result = n.reverse(n.coordinate(35, 139))
    assert result["building_name"] == expected
    assert result["building_type"] == ("original" if expected else None)
    assert result["building_distance_m"] is None


@pytest.mark.parametrize("name", ["amap", "nominatim"])
def test_building_cache_namespace_ignores_v1(tmp_path, name):
    import json

    calls = []

    def handler(request):
        calls.append(request)
        if name == "amap":
            return httpx.Response(200, json={"status": "1", "regeocode": {}})
        return ok(request)

    p = (
        AmapProvider(httpx.Client(transport=httpx.MockTransport(handler)), "synthetic-key")
        if name == "amap"
        else provider(handler)
    )
    expected = "amap:v2" if name == "amap" else "nominatim:v2:https://map.invalid/reverse"
    assert p.cache_namespace == expected
    store = MemoryStorage().client("adapter")
    point(store)
    cache = Cache(tmp_path)
    old_key = json.dumps(
        [expected.replace(":v2", ":v1"), p.language, p.coordinate(35, 139)], sort_keys=True
    )
    cache.put(old_key, {"status": "ok", "legacy": True})
    assert Runner(store, [p], cache, delay=0).run_once()["written"] == 1
    result = store.query(Query("gps_geocode")).items[0].payload
    assert "legacy" not in result
    assert "building_name" in result
    assert len(calls) == 1
    cache.close()
