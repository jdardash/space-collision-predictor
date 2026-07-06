"""Tests for the WorldView proxy endpoints (config, military ADS-B, traffic,
CCTV, geocode).

All tests are offline-deterministic: httpx.AsyncClient is replaced with a
fake client returning canned responses (or canned failures), so no real
network calls ever happen.
"""

from fastapi.testclient import TestClient

import sda.routes.worldview as wv
from sda.api import app

client = TestClient(app)


# --- fake httpx plumbing ---


class FakeResponse:
    def __init__(self, status_code: int, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def make_fake_client(get_handler):
    """Build a fake httpx.AsyncClient whose get() delegates to get_handler(url)."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return get_handler(url)

    return _FakeAsyncClient


def patch_get(monkeypatch, handler):
    monkeypatch.setattr(wv.httpx, "AsyncClient", make_fake_client(handler))


def _raise(url):
    raise RuntimeError("network down")


# --- /api/config ---


def test_config_defaults_to_null_tokens(monkeypatch):
    monkeypatch.delenv("CESIUM_ION_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cesium_ion_token"] is None
    assert data["google_maps_api_key"] is None


def test_config_reads_environment(monkeypatch):
    monkeypatch.setenv("CESIUM_ION_TOKEN", "ion-token-123")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "gmaps-key-456")
    resp = client.get("/api/config")
    data = resp.json()
    assert data["cesium_ion_token"] == "ion-token-123"
    assert data["google_maps_api_key"] == "gmaps-key-456"


# --- /api/military-flights ---

ADSB_PAYLOAD = {
    "ac": [
        {
            "hex": "ae1234",
            "flight": "RCH445  ",
            "lat": 39.05,
            "lon": -76.88,
            "alt_baro": 30000,
            "t": "C17",
            "squawk": "1200",
            "gs": 450.0,
            "track": 271.5,
        },
        {  # on the ground: alt_baro is the string "ground"
            "hex": "ae5678",
            "flight": "NAVY01",
            "lat": 32.7,
            "lon": -117.2,
            "alt_baro": "ground",
            "gs": 5.0,
            "track": 90.0,
        },
        {"hex": "ae9999", "flight": "NOLATLON"},  # skipped: no position
    ]
}


def test_military_flights_mapping(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResponse(200, ADSB_PAYLOAD))
    resp = client.get("/api/military-flights")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2

    f = data["flights"][0]
    assert f["icao24"] == "ae1234"
    assert f["callsign"] == "RCH445"
    assert f["type"] == "C17"
    assert f["is_military"] is True
    assert f["alt_m"] == round(30000 * 0.3048, 1)  # feet -> metres
    assert f["velocity"] == round(450.0 * 0.514444, 1)  # knots -> m/s

    grounded = data["flights"][1]
    assert grounded["alt_m"] == 0.0
    assert grounded["type"] == "MIL"  # default when 't' missing


def test_military_flights_upstream_error(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResponse(503, {}))
    data = client.get("/api/military-flights").json()
    assert data["flights"] == []
    assert "error" in data


def test_military_flights_network_failure(monkeypatch):
    patch_get(monkeypatch, _raise)
    data = client.get("/api/military-flights").json()
    assert data["flights"] == []
    assert "error" in data


# --- /api/tles ---

ISS_TLE_TEXT = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"""


def test_tles_returns_raw_lines_and_regime():
    client.post("/tle", json={"tle_text": ISS_TLE_TEXT})
    resp = client.get("/api/tles")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == len(data["satellites"]) >= 1

    iss = next(s for s in data["satellites"] if s["norad_id"] == 25544)
    assert iss["name"] == "ISS (ZARYA)"
    assert iss["line1"].startswith("1 25544")
    assert iss["line2"].startswith("2 25544")
    # ISS is LEO: ~420 km semi-major-axis altitude
    assert iss["regime"] == "LEO"
    assert iss["regime_color"] == wv.REGIME_COLORS["LEO"]


def test_tles_every_entry_has_complete_schema():
    client.post("/tle", json={"tle_text": ISS_TLE_TEXT})
    data = client.get("/api/tles").json()
    for sat in data["satellites"]:
        assert set(sat) == {
            "norad_id", "name", "line1", "line2", "regime", "regime_color"
        }
        assert sat["regime"] in wv.REGIME_COLORS


# --- /api/traffic ---

OVERPASS_PAYLOAD = {
    "elements": [
        {
            "type": "way",
            "tags": {"highway": "motorway"},
            "geometry": [
                {"lat": 30.26, "lon": -97.74},
                {"lat": 30.27, "lon": -97.73},
            ],
        },
        {
            "type": "way",
            "tags": {"highway": "primary"},
            "geometry": [{"lat": 30.25, "lon": -97.75}],  # skipped: < 2 points
        },
        {"type": "node", "lat": 30.2, "lon": -97.7},  # skipped: not a way
    ]
}

BBOX = "south=30.2&west=-97.8&north=30.3&east=-97.7"


def test_traffic_mapping(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResponse(200, OVERPASS_PAYLOAD))
    resp = client.get(f"/api/traffic?{BBOX}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    road = data["roads"][0]
    assert road["type"] == "motorway"
    assert road["coords"] == [
        {"lat": 30.26, "lon": -97.74},
        {"lat": 30.27, "lon": -97.73},
    ]


def test_traffic_rejects_empty_bbox():
    resp = client.get("/api/traffic?south=30.3&west=-97.7&north=30.2&east=-97.8")
    assert resp.status_code == 422


def test_traffic_rejects_oversized_bbox():
    resp = client.get("/api/traffic?south=30.0&west=-98.0&north=31.0&east=-97.0")
    assert resp.status_code == 422


def test_traffic_network_failure(monkeypatch):
    patch_get(monkeypatch, _raise)
    data = client.get(f"/api/traffic?{BBOX}").json()
    assert data["roads"] == []
    assert "error" in data


# --- /api/cctv ---

AUSTIN_PAYLOAD = [
    {  # GeoJSON-style point
        "camera_id": "101",
        "location_name": "LAMAR BLVD / 5TH ST",
        "location": {"type": "Point", "coordinates": [-97.75, 30.27]},
    },
    {  # flat lat/lon columns
        "camera_id": "202",
        "location_name": "CONGRESS AVE / 11TH ST",
        "location_latitude": "30.2721",
        "location_longitude": "-97.7403",
    },
    {"camera_id": "303", "location_name": "NO COORDS"},  # skipped
    {"location_name": "NO ID"},  # skipped
]

NYC_PAYLOAD = [
    {
        "id": "abc-123",
        "name": "Broadway @ 42nd",
        "latitude": 40.756,
        "longitude": -73.986,
        "imageUrl": "https://webcams.nyctmc.org/api/cameras/abc-123/image",
        "isOnline": "true",
    },
    {
        "id": "def-456",
        "name": "Offline cam",
        "latitude": 40.7,
        "longitude": -74.0,
        "imageUrl": "https://webcams.nyctmc.org/api/cameras/def-456/image",
        "isOnline": "false",  # skipped
    },
]


def test_cctv_austin_both_location_shapes(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResponse(200, AUSTIN_PAYLOAD))
    data = client.get("/api/cctv?city=austin").json()
    assert data["count"] == 2

    cam = data["cameras"][0]
    assert cam["id"] == "101"
    assert cam["lat"] == 30.27
    assert cam["lon"] == -97.75
    assert cam["image_url"] == "https://cctv.austinmobility.io/image/101.jpg"
    assert cam["city"] == "austin"

    flat = data["cameras"][1]
    assert flat["lat"] == 30.2721
    assert flat["lon"] == -97.7403


def test_cctv_nyc_skips_offline(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResponse(200, NYC_PAYLOAD))
    data = client.get("/api/cctv?city=nyc").json()
    assert data["count"] == 1
    cam = data["cameras"][0]
    assert cam["id"] == "abc-123"
    assert cam["city"] == "nyc"
    assert cam["image_url"].endswith("/image")


LONDON_PAYLOAD = [
    {
        "id": "JamCams_00001.01606",
        "commonName": "A20 Sidcup Rd",
        "lat": 51.42934,
        "lon": 0.06326,
        "additionalProperties": [
            {"key": "available", "value": "true"},
            {"key": "imageUrl", "value": "https://jamcams.tfl.gov.uk/00001.01606.jpg"},
        ],
    },
    {
        "id": "JamCams_00001.09999",
        "commonName": "Unavailable cam",
        "lat": 51.5,
        "lon": -0.1,
        "additionalProperties": [
            {"key": "available", "value": "false"},
            {"key": "imageUrl", "value": "https://jamcams.tfl.gov.uk/00001.09999.jpg"},
        ],
    },
]


def test_cctv_london_skips_unavailable(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResponse(200, LONDON_PAYLOAD))
    data = client.get("/api/cctv?city=london").json()
    assert data["count"] == 1
    cam = data["cameras"][0]
    assert cam["id"] == "JamCams_00001.01606"
    assert cam["city"] == "london"
    assert cam["image_url"].endswith(".jpg")


def test_cctv_unknown_city():
    data = client.get("/api/cctv?city=atlantis").json()
    assert data["cameras"] == []
    assert "error" in data


def test_cctv_network_failure(monkeypatch):
    patch_get(monkeypatch, _raise)
    data = client.get("/api/cctv?city=austin").json()
    assert data["cameras"] == []
    assert "error" in data


# --- /api/geocode ---

NOMINATIM_PAYLOAD = [
    {
        "display_name": "Austin, Travis County, Texas, United States",
        "type": "city",
        "lat": "30.2711286",
        "lon": "-97.7436995",
    },
    {"display_name": "Bad row without coordinates", "type": "hamlet"},  # skipped
]


def test_geocode_mapping(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResponse(200, NOMINATIM_PAYLOAD))
    data = client.get("/api/geocode?q=austin").json()
    assert data["count"] == 1
    r = data["results"][0]
    assert r["name"].startswith("Austin")
    assert r["type"] == "city"
    assert r["lat"] == 30.2711286
    assert r["lon"] == -97.7436995


def test_geocode_query_too_short():
    resp = client.get("/api/geocode?q=a")
    assert resp.status_code == 422


def test_geocode_network_failure(monkeypatch):
    patch_get(monkeypatch, _raise)
    data = client.get("/api/geocode?q=austin").json()
    assert data["results"] == []
    assert "error" in data
