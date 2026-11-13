"""Map HTTP boundary, coordinate conversion and normalized payloads."""

from __future__ import annotations

import json
import math
from importlib.resources import files
from itertools import pairwise
from typing import Any, Protocol

import httpx


class ConfigurationError(Exception):
    """Operator action required; never turn this into a failed GPS result."""


class TransientError(Exception):
    """Retry within the per-point budget."""


# Natural Earth public-domain 1:110m land polygons; see implementation.md for limits.
_CHINA = json.loads(files("geo_adapter").joinpath("china.json").read_text())["coordinates"]


def _inside(lat: float, lon: float, ring: list[list[float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in pairwise(ring):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def in_china(latitude: float, longitude: float) -> bool:
    """Local land-mask routing, not a statement about territorial boundaries."""
    return any(
        _inside(latitude, longitude, polygon[0])
        and not any(_inside(latitude, longitude, hole) for hole in polygon[1:])
        for polygon in _CHINA
    )


def gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    if not in_china(latitude, longitude):
        return latitude, longitude
    x, y = longitude - 105, latitude - 35
    lat = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    lon = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    wave = (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    lat += wave + (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    lat += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    lon += wave + (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    lon += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    rad = latitude / 180 * math.pi
    magic = 1 - 0.00669342162296594323 * math.sin(rad) ** 2
    lat = (
        lat
        * 180
        / ((6378245 * (1 - 0.00669342162296594323)) / (magic * math.sqrt(magic)) * math.pi)
    )
    lon = lon * 180 / (6378245 / math.sqrt(magic) * math.cos(rad) * math.pi)
    return latitude + lat, longitude + lon


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def distance(value: Any) -> float | None:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


class Provider(Protocol):
    name: str
    language: str
    cache_namespace: str

    def coordinate(self, lat: float, lon: float) -> dict: ...
    def reverse(self, coordinate: dict) -> dict: ...


def empty(provider: Provider | None, coordinate: dict | None, status: str) -> dict:
    return dict.fromkeys(
        (
            "formatted_address",
            "country",
            "province",
            "city",
            "district",
            "township",
            "street",
            "poi_name",
            "poi_type",
            "poi_distance_m",
        )
    ) | {
        "status": status,
        "provider": provider.name if provider else "invalid",
        "language": provider.language if provider else None,
        "query_coordinate": coordinate,
    }


def request(client: httpx.Client, url: str, params: dict, *, no_result: bool = False) -> dict:
    try:
        response = client.get(
            url, params=params, headers={"User-Agent": "EchoAsRecord-GeoAdapter/0.1"}
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError()
        if no_result and response.status_code == 404:
            # Nominatim only: distinguish an explicit no-result body from a wrong URL.
            body = response.json()
            if isinstance(body, dict) and body.get("error") == "Unable to geocode":
                return body
        if response.status_code >= 300:
            raise ConfigurationError()
        body = response.json()
        if not isinstance(body, dict):
            raise TransientError()
        return body
    except (httpx.TransportError, ValueError):
        raise TransientError() from None


MUNICIPALITIES = frozenset({"北京市", "上海市", "天津市", "重庆市"})


class AmapProvider:
    name = "amap"
    language = "zh-CN"
    cache_namespace = "amap:v1"

    def __init__(self, client: httpx.Client, key: str):
        self.client, self.key = client, key

    def coordinate(self, lat: float, lon: float) -> dict:
        a, b = gcj02(lat, lon)
        return {"latitude": a, "longitude": b, "crs": "GCJ-02" if in_china(lat, lon) else "WGS84"}

    def reverse(self, coordinate: dict) -> dict:
        body = request(
            self.client,
            "https://restapi.amap.com/v3/geocode/regeo",
            {
                "key": self.key,
                "location": f"{coordinate['longitude']},{coordinate['latitude']}",
                "extensions": "all",
                "output": "JSON",
            },
        )
        if "status" not in body:
            raise TransientError()
        if body.get("status") != "1":
            if str(body.get("infocode")) in {"10002", "10004", "10016", "10019", "10020", "10021"}:
                raise TransientError()
            raise ConfigurationError()
        geo = body.get("regeocode")
        if not isinstance(geo, dict):
            raise TransientError()
        address = geo.get("addressComponent", {})
        pois = geo.get("pois", [])
        if not isinstance(address, dict) or not isinstance(pois, list):
            raise TransientError()
        result = empty(self, coordinate, "ok")
        result["formatted_address"] = text(geo.get("formatted_address"))
        for name in ("country", "province", "city", "district", "township"):
            result[name] = text(address.get(name))
        # 直辖市的 city 由高德返回空数组，城市即省级名称。
        if result["city"] is None and result["province"] in MUNICIPALITIES:
            result["city"] = result["province"]
        street = address.get("streetNumber", {})
        result["street"] = text(street.get("street")) if isinstance(street, dict) else None
        candidates = [p for p in pois if isinstance(p, dict) and text(p.get("name"))]
        if candidates:
            poi = min(
                candidates,
                key=lambda p: d if (d := distance(p.get("distance"))) is not None else math.inf,
            )
            result.update(
                poi_name=text(poi.get("name")),
                poi_type=text(poi.get("type")),
                poi_distance_m=distance(poi.get("distance")),
            )
        if not any(
            result[k] for k in ("formatted_address", "poi_name", "country", "province", "city")
        ):
            result["status"] = "no_result"
        return result


class NominatimProvider:
    name = "nominatim"
    # 与高德一致优先中文；OSM 没有中文名时 Nominatim 返回当地名称。
    language = "zh-CN"

    def __init__(self, client: httpx.Client, url: str):
        parsed = httpx.URL(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError()
        self.client, self.url = client, url.rstrip("/") + "/reverse"
        self.cache_namespace = "nominatim:v1:" + self.url

    def coordinate(self, lat: float, lon: float) -> dict:
        return {"latitude": lat, "longitude": lon, "crs": "WGS84"}

    def reverse(self, coordinate: dict) -> dict:
        body = request(
            self.client,
            self.url,
            {
                "lat": coordinate["latitude"],
                "lon": coordinate["longitude"],
                "format": "jsonv2",
                "addressdetails": 1,
                "accept-language": self.language,
            },
            no_result=True,
        )
        if body.get("error") == "Unable to geocode":
            return empty(self, coordinate, "no_result")
        if "error" in body or not isinstance(body.get("address"), dict):
            raise TransientError()
        address = body["address"]
        result = empty(self, coordinate, "ok")
        result.update(
            formatted_address=text(body.get("display_name")),
            country=text(address.get("country")),
            province=text(address.get("state")),
            city=text(address.get("city") or address.get("town") or address.get("village")),
            district=text(address.get("city_district") or address.get("county")),
            township=text(address.get("suburb")),
            street=text(address.get("road")),
            poi_name=text(body.get("name")),
            poi_type=text(body.get("type")),
        )
        return result


def select(providers: list[Provider], lat: float, lon: float) -> Provider:
    if not providers:
        raise ConfigurationError()
    preferred = "amap" if in_china(lat, lon) else "nominatim"
    return next((p for p in providers if p.name == preferred), providers[0])
