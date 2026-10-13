"""Tests for the WorldView proxy endpoints (config, military ADS-B, traffic,
CCTV, geocode, space weather).

All tests are offline-deterministic: httpx.AsyncClient is replaced with a
fake client returning canned responses (or canned failures), so no real
network calls ever happen.
"""

import asyncio
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import sda.api as api
import sda.routes.worldview as wv
from sda.api import app

client = TestClient(app)


# --- fake httpx plumbing ---


class FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"upstream returned {self.status_code}")


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


@pytest.mark.parametrize(
    "alt_km, eccentricity, expected",
    [
        (420.0, 0.0005, "LEO"),
        (1999.9, 0.0, "LEO"),
        (2000.0, 0.0, "MEO"),
        (20200.0, 0.01, "MEO"),
        (35485.9, 0.0, "MEO"),
        (35486.0, 0.0, "GEO"),
        # Half the real belt sits a few km under the nominal altitude.
        (35780.0, 0.0003, "GEO"),
        (35786.0, 0.0, "GEO"),
        (35800.0, 0.0, "GEO"),
        (36086.0, 0.0, "GEO"),
        (36086.1, 0.0, "HEO"),
        (20000.0, 0.7, "HEO"),
    ],
)
def test_globe_regime_bands(alt_km, eccentricity, expected):
    assert wv._classify_regime(alt_km, eccentricity) == expected


def test_page_legend_shows_the_colours_the_dots_are_drawn_in():
    """The legend is hand-written HTML; a colour changed here must change there."""
    page = (wv.STATIC_DIR.parent / "worldview.html").read_text(encoding="utf-8")
    legend = page[page.index('id="orbit-legend"'):]
    legend = legend[: legend.index("</div>")]
    assert re.findall(r"background:(#[0-9a-f]{6})", legend) == [
        wv.REGIME_COLORS[regime] for regime in ("LEO", "MEO", "GEO", "HEO")
    ]


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


# --- /worldview page: basemap choice ---


def _basemap_registry(page: str) -> str:
    start = page.index("// ===== BASEMAP =====")
    return page[start:page.index("function setBasemap", start)]


def test_worldview_page_offers_keyless_basemaps():
    """The globe has to show a real Earth with no map key configured, so every
    basemap source must be a public tile URL that carries no credential."""
    page = client.get("/worldview").text
    for name in ("earth", "daynight", "night", "hires"):
        assert f'data-basemap="{name}"' in page
        assert f"\n  {name}: {{" in _basemap_registry(page)

    registry = _basemap_registry(page)
    assert "BlueMarble_ShadedRelief_Bathymetry" in registry
    assert "VIIRS_Black_Marble" in registry
    for marker in ("key=", "token", "config."):
        assert marker not in registry
    hosts = set(re.findall(r"https://([a-z0-9.-]+)/", registry))
    assert hosts == {"gibs.earthdata.nasa.gov", "services.arcgisonline.com"}


def test_worldview_page_does_not_use_the_carto_basemap():
    """basemaps.cartocdn.com began answering every tile with an "API KEY
    REQUIRED" watermark, and it was the default, so that text covered the globe."""
    page = client.get("/worldview").text
    assert "cartocdn" not in page
    assert "var DEFAULT_BASEMAP = 'earth';" in page
    assert "var currentBasemap = DEFAULT_BASEMAP;" in page


def test_worldview_basemap_survives_a_share_link():
    """A link copied on a basemap other than the default has to reopen on it."""
    page = client.get("/worldview").text
    assert "if (currentBasemap !== DEFAULT_BASEMAP) parts.push('bm=' + currentBasemap)" in page
    assert "if (q.bm) setBasemap(q.bm" in page


def test_worldview_basemap_credit_is_shown_on_the_page():
    """Cesium's own credit box is detached, so the imagery source has to be
    named somewhere a viewer can read it."""
    page = client.get("/worldview").text
    assert 'id="basemap-credit"' in page
    assert "$('basemap-credit')" in page


# --- /static/{asset_path} ---


def test_static_serves_the_sgp4_worker():
    resp = client.get("/static/js/sgp4-worker.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    # The service worker registers at the demo root, so this header has to be
    # present for the live app to be able to widen scope the same way.
    assert resp.headers["service-worker-allowed"] == "/"
    assert "onmessage" in resp.text


def test_static_serves_the_style_and_tactical_bundles():
    """The page loads both by relative path, so /static must serve them live."""
    styles = client.get("/static/js/styles.js")
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("application/javascript")
    assert "WorldViewStyles" in styles.text

    tactical = client.get("/static/js/tactical.js")
    assert tactical.status_code == 200
    assert "WorldViewTactical" in tactical.text


def test_static_serves_the_manifest_with_its_own_media_type():
    resp = client.get("/static/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")


def test_static_rejects_directory_traversal():
    """A path escaping the static root must 404, not leak a source file."""
    for attempt in (
        "../routes/worldview.py",
        "..%2Froutes%2Fworldview.py",
        "js/../../worldview.html",
        "/etc/passwd",
    ):
        resp = client.get(f"/static/{attempt}")
        assert resp.status_code == 404, f"{attempt} was not refused"
        assert "def " not in resp.text


def test_static_refuses_unknown_extensions_and_missing_files():
    assert client.get("/static/js/does-not-exist.js").status_code == 404
    assert client.get("/static/").status_code == 404


def test_resolve_static_asset_refuses_escape_directly():
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        wv.resolve_static_asset("../worldview.html")
    assert excinfo.value.status_code == 404


# --- /api/tles?group= ---

GROUP_TLE_TEXT = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014
CSS (TIANHE)
1 48274U 21035A   24045.36462963  .00025442  00000+0  28285-3 0  9998
2 48274  41.4741 174.0952 0006405 314.3159  45.7115 15.61790468158231"""


def _clear_group_cache():
    wv._GROUP_CACHE.clear()


def test_tles_group_fetches_celestrak_and_caches(monkeypatch):
    _clear_group_cache()
    calls = []

    def handler(url):
        calls.append(url)
        return FakeResponse(200, None, GROUP_TLE_TEXT)

    patch_get(monkeypatch, handler)

    data = client.get("/api/tles?group=stations").json()
    assert data["source"] == "celestrak"
    assert data["group"] == "stations"
    assert data["count"] == len(data["satellites"]) == 2
    assert {s["norad_id"] for s in data["satellites"]} == {25544, 48274}
    assert "stations" in calls[0]

    # Second request must be served from the module cache: CelesTrak asks
    # clients not to poll, and 'active' is ~11k objects.
    again = client.get("/api/tles?group=stations").json()
    assert again["source"] == "celestrak"
    assert len(calls) == 1
    _clear_group_cache()


def test_tles_group_does_not_pollute_the_process_catalog(monkeypatch):
    """Group fetches parse through a scratch store, not the screening catalog."""
    _clear_group_cache()
    from sda.api import store

    before = len(store.get_all())
    patch_get(monkeypatch, lambda url: FakeResponse(200, None, GROUP_TLE_TEXT))
    client.get("/api/tles?group=visual")
    assert len(store.get_all()) == before
    _clear_group_cache()


REFUSAL_TEXT = "GP data has not updated since your last successful download."


def counting_group_handler(calls, status=200, text=GROUP_TLE_TEXT):
    def handler(url):
        calls.append(url)
        return FakeResponse(status, None, text)

    return handler


@pytest.fixture
def process_catalog_restored():
    saved = dict(api.store._catalog)
    yield
    api.store._catalog.clear()
    api.store._catalog.update(saved)


def test_tles_group_is_served_from_the_refresh_download_without_asking(
    monkeypatch, process_catalog_restored
):
    """The background refresh already holds every group the globe may ask for. A
    second download of one inside CelesTrak's window earns a 403, and enough of
    those get the address blocked; the route used to make it."""
    _clear_group_cache()
    calls: list[str] = []
    patch_get(monkeypatch, counting_group_handler(calls))
    asyncio.run(api._fetch_celestrak_group(wv.httpx.AsyncClient(), "stations"))
    assert len(calls) == 1

    data = client.get("/api/tles?group=stations").json()

    assert len(calls) == 1
    assert data["source"] == "celestrak"
    assert {s["norad_id"] for s in data["satellites"]} == {25544, 48274}
    _clear_group_cache()


def test_tles_group_after_a_restart_is_read_from_the_disk_copy(monkeypatch, tmp_path):
    """A restart emptied the route's own cache, and the next request for a group
    went straight back to CelesTrak whatever the cache directory said."""
    _clear_group_cache()
    monkeypatch.setenv("SDA_TLE_CACHE_DIR", str(tmp_path))
    (tmp_path / "stations.tle").write_text(GROUP_TLE_TEXT, encoding="utf-8")
    calls: list[str] = []
    patch_get(monkeypatch, counting_group_handler(calls))

    data = client.get("/api/tles?group=stations").json()

    assert calls == []
    assert data["source"] == "celestrak"
    assert data["count"] == 2
    _clear_group_cache()


def test_tles_group_remembers_a_refusal(monkeypatch, tmp_path):
    _clear_group_cache()
    monkeypatch.setenv("SDA_TLE_CACHE_DIR", str(tmp_path))
    calls: list[str] = []
    patch_get(monkeypatch, counting_group_handler(calls, 403, REFUSAL_TEXT))

    for _ in range(3):
        data = client.get("/api/tles?group=active").json()
        assert data["source"] == "fallback-catalog"

    assert len(calls) == 1
    assert (tmp_path / "active.refused").exists()
    _clear_group_cache()


@pytest.mark.parametrize(("minutes", "asks"), [(30, 0), (115, 0), (130, 0), (140, 1)])
def test_tles_group_leaves_a_due_group_to_the_background_refresh(monkeypatch, minutes, asks):
    """From minute 120 a group may be asked for again, and the refresh comes round at
    minute 125. If the globe took the group in between, the refresh would find it
    resting and the screening catalog would go a cycle without new elements."""
    _clear_group_cache()
    calls: list[str] = []
    patch_get(monkeypatch, counting_group_handler(calls))
    api._celestrak_answered_at["stations"] = time.time() - minutes * 60.0

    client.get("/api/tles?group=stations")

    assert len(calls) == asks
    _clear_group_cache()


def test_tles_group_rows_are_built_once_per_download(monkeypatch):
    """'active' is about 11,000 satrecs; the page asks again every half hour."""
    _clear_group_cache()
    patch_get(monkeypatch, counting_group_handler([]))
    built: list[int] = []
    real = wv._catalog_from_store

    def counted(store):
        built.append(1)
        return real(store)

    monkeypatch.setattr(wv, "_catalog_from_store", counted)

    first = client.get("/api/tles?group=visual").json()
    second = client.get("/api/tles?group=visual").json()

    assert first["count"] == second["count"] == 2
    assert len(built) == 1
    _clear_group_cache()


def test_tles_rejects_an_unknown_group():
    resp = client.get("/api/tles?group=definitely-not-a-group")
    assert resp.status_code == 400
    assert "Unknown group" in resp.json()["detail"]


def test_tles_group_falls_back_to_the_bundled_catalog(monkeypatch):
    _clear_group_cache()
    patch_get(monkeypatch, _raise)
    data = client.get("/api/tles?group=active").json()
    assert data["source"] == "fallback-catalog"
    assert data["group"] == "active"
    assert data["count"] >= 1
    _clear_group_cache()


def test_tles_without_a_group_still_uses_the_process_catalog():
    data = client.get("/api/tles").json()
    assert data["source"] == "catalog"
    assert data["count"] == len(data["satellites"])


# --- /api/conjunction-globe-data covariance ---


def test_conjunction_globe_data_carries_tle_age_covariance():
    """The globe draws 1 and 3 sigma shells from this block.

    There is no operator covariance in a TLE, so the server publishes the
    TLE-age sigma model explicitly labelled as an estimate rather than letting
    the client invent an uncertainty.
    """
    # The module-level client does not run the lifespan hook, so seed the same
    # bundled catalog the app loads at startup; without a pair there is nothing
    # to screen and the assertions below would vacuously pass.
    from sda.demo_tles import DEMO_TLE_TEXT

    client.post("/tle", json={"tle_text": DEMO_TLE_TEXT})

    resp = client.post(
        "/api/conjunction-globe-data", json={"hours": 24.0, "threshold_km": 10.0}
    )
    assert resp.status_code == 200
    events = resp.json()["conjunctions"]
    assert events, "demo catalog produced no conjunctions to check"

    for event in events:
        cov = event["covariance"]
        assert cov["source"] == "tle-age-estimate"
        assert cov["isotropic"] is True
        assert "sigma_from_tle_age" in cov["model"]
        assert cov["primary_sigma_km"] > 0
        assert cov["secondary_sigma_km"] > 0
        assert cov["combined_sigma_km"] > 0
        assert cov["primary_tle_age_hours"] >= 0
        assert cov["secondary_tle_age_hours"] >= 0
        # Combined sigma is the root-sum-square of the two, within rounding.
        rss = (cov["primary_sigma_km"] ** 2 + cov["secondary_sigma_km"] ** 2) ** 0.5
        assert abs(rss - cov["combined_sigma_km"]) < 1e-3


# --- /api/space-weather ---
#
# Fixtures are trimmed copies of what services.swpc.noaa.gov answered on
# 2026-10-08 (a few rows each; the solar wind rows keep only the fields the
# parser reads). The retired products/solar-wind/plasma-*.json files now answer
# 404, so the fake treats any URL it does not know the same way.

SWPC_KP_NOW = [  # products/noaa-planetary-k-index.json: a list of objects, oldest first
    {"time_tag": "2026-10-07T21:00:00", "Kp": 1.0, "a_running": 4, "station_count": 8},
    {"time_tag": "2026-10-08T00:00:00", "Kp": 1.0, "a_running": 4, "station_count": 8},
    {"time_tag": "2026-10-08T03:00:00", "Kp": 1.67, "a_running": 6, "station_count": 7},
]

SWPC_KP_OLD = [  # the earlier shape: a header row, then string values
    ["time_tag", "Kp", "a_running", "station_count"],
    ["2026-10-08 00:00:00", "3.00", "15", "8"],
    ["2026-10-08 03:00:00", "5.33", "56", "8"],
]

SWPC_WIND_NOW = [  # json/rtsw/rtsw_wind_1m.json: newest first, several spacecraft interleaved
    {"time_tag": "2026-10-08T07:34:00", "active": False, "source": "ACE",
     "proton_speed": 372.33, "proton_temperature": 58800, "proton_density": 1.23},
    {"time_tag": "2026-10-08T07:33:00", "active": True, "source": "SOLAR1",
     "proton_speed": 363.8, "proton_temperature": 78969, "proton_density": 3.87},
    {"time_tag": "2026-10-08T07:33:00", "active": False, "source": "ACE",
     "proton_speed": 381.2, "proton_temperature": 82906, "proton_density": 1.56},
    {"time_tag": "2026-10-08T07:32:00", "active": True, "source": "SOLAR1",
     "proton_speed": 369.9, "proton_temperature": 69555, "proton_density": 3.46},
]

SWPC_WIND_NULL_ROW = {  # a real SOLAR1 gap: active, but every plasma value is null
    "time_tag": "2026-10-08T03:54:00", "active": True, "source": "SOLAR1",
    "proton_speed": None, "proton_temperature": None, "proton_density": None,
}

SWPC_WIND_OLD = [  # the retired plasma-5-minute.json shape: header row, string values
    ["time_tag", "density", "speed", "temperature"],
    ["2026-10-08 13:50:00", "5.0", "410.1", "90000"],
    ["2026-10-08 13:55:00", "5.2", "412.3", "91000"],
]

SWPC_ALERTS = [
    {
        "product_id": "EF3A",
        "issue_datetime": "2026-10-08 05:01:00.663",
        "message": "Space Weather Message Code: ALTEF3\r\nSerial Number: 1111\r\n",
    },
]

SWPC_FIELDS = {
    "kp", "kp_time", "g_scale", "solar_wind_speed_km_s", "solar_wind_density_p_cm3",
    "solar_wind_time", "alerts", "count", "source",
}


@pytest.fixture
def fresh_cache():
    """The handler caches good answers for five minutes; each test starts empty."""
    wv.clear_proxy_caches()
    yield
    wv.clear_proxy_caches()


def swpc_router(kp=None, wind=None, alerts=None, seen=None):
    """Fake SWPC host. Each product is a JSON body (served 200) or an Exception
    (raised by the client); an omitted product, or any URL that is not one of the
    three, answers 404. *seen* collects every requested URL."""

    def route(url):
        if seen is not None:
            seen.append(url)
        if "k-index" in url:
            body = kp
        elif "rtsw" in url:
            body = wind
        elif "alerts" in url:
            body = alerts
        else:
            body = None
        if isinstance(body, Exception):
            raise body
        return FakeResponse(404, None) if body is None else FakeResponse(200, body)

    return route


def test_space_weather_reads_the_current_swpc_formats(monkeypatch, fresh_cache):
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, SWPC_WIND_NOW, SWPC_ALERTS))
    resp = client.get("/api/space-weather")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == SWPC_FIELDS
    assert data["kp"] == 1.67
    assert data["kp_time"] == "2026-10-08T03:00:00"
    assert data["g_scale"] == "G0"
    # The active spacecraft wins over the newer row from the standby one.
    assert data["solar_wind_speed_km_s"] == 363.8
    assert data["solar_wind_density_p_cm3"] == 3.87
    assert data["solar_wind_time"] == "2026-10-08T07:33:00"
    assert data["count"] == 1
    assert data["alerts"][0]["headline"] == "Space Weather Message Code: ALTEF3"


def test_space_weather_still_reads_the_old_header_plus_rows_format(monkeypatch, fresh_cache):
    patch_get(monkeypatch, swpc_router(SWPC_KP_OLD, SWPC_WIND_OLD, SWPC_ALERTS))
    data = client.get("/api/space-weather").json()
    assert data["kp"] == 5.33
    assert data["kp_time"] == "2026-10-08 03:00:00"
    assert data["g_scale"] == "G1"
    assert data["solar_wind_speed_km_s"] == 412.3
    assert data["solar_wind_density_p_cm3"] == 5.2
    assert data["solar_wind_time"] == "2026-10-08 13:55:00"


def test_space_weather_asks_for_a_plasma_url_that_exists(monkeypatch, fresh_cache):
    seen: list[str] = []
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, SWPC_WIND_NOW, SWPC_ALERTS, seen))
    client.get("/api/space-weather")
    assert not any("plasma-5-minute" in url for url in seen)
    assert "https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json" in seen


def test_space_weather_wind_skips_null_rows_and_ignores_row_order(monkeypatch, fresh_cache):
    newest_gap = {**SWPC_WIND_NULL_ROW, "time_tag": "2026-10-08T07:35:00"}
    rows = [newest_gap, *SWPC_WIND_NOW]
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, list(reversed(rows)), []))
    data = client.get("/api/space-weather").json()
    assert data["solar_wind_speed_km_s"] == 363.8
    assert data["solar_wind_time"] == "2026-10-08T07:33:00"


def test_space_weather_wind_uses_a_standby_spacecraft_when_none_is_active(
    monkeypatch, fresh_cache
):
    standby_only = [r for r in SWPC_WIND_NOW if r["source"] == "ACE"]
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, standby_only, []))
    data = client.get("/api/space-weather").json()
    assert data["solar_wind_speed_km_s"] == 372.33
    assert data["solar_wind_density_p_cm3"] == 1.23


def test_space_weather_kp_skips_null_and_non_finite_values(monkeypatch, fresh_cache):
    rows = [
        *SWPC_KP_NOW,
        {"time_tag": "2026-10-08T06:00:00", "Kp": None},
        {"time_tag": "2026-10-08T09:00:00", "Kp": "NaN"},
        {"time_tag": "2026-10-08T12:00:00"},
    ]
    patch_get(monkeypatch, swpc_router(rows, SWPC_WIND_NOW, []))
    resp = client.get("/api/space-weather")
    assert resp.status_code == 200
    assert resp.json()["kp"] == 1.67


def test_space_weather_wind_ignores_fill_values(monkeypatch, fresh_cache):
    """SWPC marks a missing reading with a fill value. It is a finite number, so it
    reached the strip as a solar wind of -9999.9 km/s."""
    rows = [
        {"time_tag": "2026-10-08T09:00:00", "active": True,
         "proton_speed": -9999.9, "proton_density": -9999.9},
        {"time_tag": "2026-10-08T08:59:00", "active": True,
         "proton_speed": 412.0, "proton_density": 5.1},
    ]
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, rows, []))
    data = client.get("/api/space-weather").json()
    assert data["solar_wind_speed_km_s"] == 412.0
    assert data["solar_wind_density_p_cm3"] == 5.1
    assert data["solar_wind_time"] == "2026-10-08T08:59:00"


def test_space_weather_wind_density_fill_value_reads_as_missing(monkeypatch, fresh_cache):
    rows = [{"time_tag": "2026-10-08T09:00:00", "active": True,
             "proton_speed": 412.0, "proton_density": -99999}]
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, rows, []))
    data = client.get("/api/space-weather").json()
    assert data["solar_wind_speed_km_s"] == 412.0
    assert data["solar_wind_density_p_cm3"] is None


def test_space_weather_wind_moves_to_the_standby_when_the_active_one_goes_quiet(
    monkeypatch, fresh_cache
):
    """The active spacecraft's last reading is three hours old and the standby is
    current. The strip kept showing the three-hour-old numbers."""
    rows = [
        {"time_tag": "2026-10-08T06:00:00", "active": True, "source": "ACE",
         "proton_speed": 380.0, "proton_density": 4.0},
        {"time_tag": "2026-10-08T09:00:00", "active": True, "source": "ACE",
         "proton_speed": None, "proton_density": None},
        {"time_tag": "2026-10-08T09:00:00", "active": False, "source": "DSCOVR",
         "proton_speed": 720.0, "proton_density": 18.0},
    ]
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, rows, []))
    data = client.get("/api/space-weather").json()
    assert data["solar_wind_speed_km_s"] == 720.0
    assert data["solar_wind_density_p_cm3"] == 18.0
    assert data["solar_wind_time"] == "2026-10-08T09:00:00"


def test_space_weather_does_not_take_a_boolean_or_an_impossible_value_for_a_reading(
    monkeypatch, fresh_cache
):
    kp_rows = [
        *SWPC_KP_NOW,
        {"time_tag": "2026-10-08T06:00:00", "Kp": True},
        {"time_tag": "2026-10-08T09:00:00", "Kp": -1.0},
        {"time_tag": "2026-10-08T12:00:00", "Kp": 99.0},
    ]
    wind_rows = [
        {"time_tag": "2026-10-08T09:00:00", "active": True, "proton_speed": True},
        {"time_tag": "2026-10-08T08:59:00", "active": True, "proton_speed": 401.0},
    ]
    patch_get(monkeypatch, swpc_router(kp_rows, wind_rows, []))
    data = client.get("/api/space-weather").json()
    assert data["kp"] == 1.67
    assert data["solar_wind_speed_km_s"] == 401.0


def test_space_weather_wind_density_may_be_missing(monkeypatch, fresh_cache):
    rows = [{"time_tag": "2026-10-08T07:33:00", "active": True, "proton_speed": 401.0}]
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, rows, []))
    data = client.get("/api/space-weather").json()
    assert data["solar_wind_speed_km_s"] == 401.0
    assert data["solar_wind_density_p_cm3"] is None


@pytest.mark.parametrize(
    "wind",
    [
        None,  # upstream 404
        httpx.ReadTimeout("slow"),
        RuntimeError("network down"),
        {"unexpected": "an object, not a list"},
        [SWPC_WIND_NULL_ROW],
        ["garbage", 3, None],
        [],
    ],
)
def test_space_weather_keeps_kp_and_alerts_when_the_wind_product_fails(
    monkeypatch, fresh_cache, wind
):
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, wind, SWPC_ALERTS))
    resp = client.get("/api/space-weather")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == SWPC_FIELDS  # no "error": the strip still has values to show
    assert data["kp"] == 1.67
    assert data["count"] == 1
    assert data["solar_wind_speed_km_s"] is None
    assert data["solar_wind_density_p_cm3"] is None
    assert data["solar_wind_time"] == ""


def test_space_weather_keeps_the_wind_when_kp_is_gone(monkeypatch, fresh_cache):
    patch_get(monkeypatch, swpc_router(None, SWPC_WIND_NOW, []))
    data = client.get("/api/space-weather").json()
    assert data["kp"] is None
    assert data["g_scale"] == ""
    assert data["solar_wind_speed_km_s"] == 363.8


def test_space_weather_reports_unavailable_when_everything_fails(monkeypatch, fresh_cache):
    patch_get(monkeypatch, _raise)
    resp = client.get("/api/space-weather")
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] == "SWPC unavailable"
    assert data["kp"] is None
    assert data["solar_wind_speed_km_s"] is None
    assert data["alerts"] == []
    # A total outage is not cached: the next request tries again.
    patch_get(monkeypatch, swpc_router(SWPC_KP_NOW, SWPC_WIND_NOW, SWPC_ALERTS))
    assert client.get("/api/space-weather").json()["kp"] == 1.67
