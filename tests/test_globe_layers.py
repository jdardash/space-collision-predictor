"""Tests for the WorldView data-feed panel in static/js/layers.js.

Offline and deterministic. The script is plain browser JavaScript with no
bundler, so the logic is exercised through small Node harnesses: pure parsing
and selection helpers are called directly, and the panel itself runs against a
fake DOM, a fake Cesium and a fake fetch with a controllable clock. The whole
module skips when Node is not installed.

Fixtures marked "real" are trimmed copies of what the local proxies returned on
2026-10-08. Fixtures marked "from handler" mirror the response shape the proxy
handler in sda/routes/worldview.py builds, for feeds whose upstream was down or
whose key was absent when probed.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import sda.routes.worldview as wv
from sda.api import app

NODE = shutil.which("node")
LAYERS_JS = Path(__file__).resolve().parent.parent / "src/sda/templates/static/js/layers.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

FEED_KEYS = [
    "launches", "spaceweather", "volcanoes", "weatheralerts", "gdelt",
    "radio", "bikeshare", "military", "fires", "ais",
]
POINT_FEEDS = [k for k in FEED_KEYS if k != "spaceweather"]

PRELUDE = r"""
const fs = require('fs');
const DATA = JSON.parse(fs.readFileSync(0, 'utf8') || 'null');
global.window = global;
function out(x) { process.stdout.write(JSON.stringify(x)); }
function main(fn) {
  Promise.resolve().then(fn).catch(function (e) {
    process.stderr.write(String((e && e.stack) || e));
    process.exit(1);
  });
}
require(process.env.LAYERS_JS);
const L = global.SDALayers;
"""


def run_node(body: str, data=None, kit: str = ""):
    """Run `body` after the prelude and return the JSON it prints."""
    script = PRELUDE + kit + "\nmain(async function () {\n" + body + "\n});\n"
    env = dict(os.environ, LAYERS_JS=str(LAYERS_JS))
    proc = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(f"node exited {proc.returncode}\n{proc.stderr[-2500:]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


# --- the test that fails on the half-written file ---


def test_layers_js_passes_node_check():
    proc = subprocess.run(
        [NODE, "--check", str(LAYERS_JS)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr


# --- source hygiene ---


def test_source_is_ascii_and_free_of_unsafe_patterns():
    text = LAYERS_JS.read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in text), "non-ASCII character in layers.js"
    for banned in (
        "inner" + "HTML",
        "outer" + "HTML",
        "insertAdjacent",
        "document.write",
        "eval(",
        "keydown",
        "keyup",
        "keypress",
        "celestrak",
    ):
        assert banned not in text, f"layers.js must not contain {banned!r}"


def test_module_is_one_strict_iife_on_window():
    text = LAYERS_JS.read_text(encoding="utf-8")
    assert "'use strict'" in text
    assert "window.SDALayers" in text
    assert "import " not in text and "export " not in text


# --- registry against the real FastAPI app ---


def _feeds():
    return run_node("out(L.FEEDS.map(function (f) { return f; }));")


def test_registry_has_the_ten_feeds_with_sane_limits():
    feeds = _feeds()
    assert [f["key"] for f in feeds] == FEED_KEYS
    for feed in feeds:
        assert feed["endpoint"].startswith("/api/")
        assert 0 < feed["cap"] <= 500
        # a non-zero refresh interval is never shorter than five minutes
        assert feed["refreshS"] == 0 or feed["refreshS"] >= 300
    keyed = {f["key"]: f["keyEnv"] for f in feeds if f["keyEnv"]}
    assert keyed == {"fires": "FIRMS_MAP_KEY", "ais": "AISSTREAM_API_KEY"}


def test_every_registry_endpoint_is_a_served_get_route_with_matching_params():
    spec = app.openapi()["paths"]
    camera = {"lat": 38.9, "lon": -77.0}
    urls = run_node(
        "out(L.FEEDS.map(function (f) { return [f.key, L.buildPath(f.key, DATA)]; }));",
        camera,
    )
    assert len(urls) == len(FEED_KEYS)
    for key, url in urls:
        parsed = urlparse(url)
        assert parsed.path in spec, f"{key}: {parsed.path} is not served by the app"
        op = spec[parsed.path].get("get")
        assert op is not None, f"{key}: {parsed.path} has no GET operation"
        declared = {p["name"]: p.get("required", False) for p in op.get("parameters", [])}
        sent = set(parse_qs(parsed.query))
        assert sent <= set(declared), f"{key}: sends unknown params {sent - set(declared)}"
        required = {name for name, req in declared.items() if req}
        assert required <= sent, f"{key}: missing required params {required - sent}"


def test_registry_city_keys_match_the_server_gbfs_cities():
    cities = run_node("out(Object.keys(L.CITIES).sort());")
    assert cities == sorted(wv._GBFS_CITIES)


# --- fixtures ---

LAUNCHES = {  # real
    "launches": [
        {
            "id": "a1bb0984-19d4-4b47-9bba-706d225573e2",
            "name": "Long March 12 | Unknown Payload",
            "net": "2026-10-09T19:25:00Z",
            "window_start": "2026-10-09T19:20:00Z",
            "status": "Go",
            "provider": "China Aerospace Science and Technology Corporation",
            "mission": "Unknown Payload",
            "orbit": "N/A",
            "pad": "Commercial LC-2",
            "site": "Wenchang Space Launch Site, People's Republic of China",
            "lat": 19.59755,
            "lon": 110.936481,
        },
        {
            "id": "da43dc2c-00af-4cc1-8ef1-90a60b2eb9e5",
            "name": "Falcon 9 Block 5 | SDA Tranche 1 Transport Layer A",
            "net": "2026-10-10T07:29:00Z",
            "window_start": "2026-10-10T07:29:00Z",
            "status": "Go",
            "provider": "SpaceX",
            "mission": "SDA Tranche 1 Transport Layer A",
            "orbit": "PO",
            "pad": "Space Launch Complex 4E",
            "site": "Vandenberg SFB, CA, USA",
            "lat": 34.632,
            "lon": -120.611,
        },
    ],
    "count": 2,
    "source": "Launch Library 2 (thespacedevs)",
}

VOLCANOES = {  # real
    "volcanoes": [
        {"name": "Stromboli", "country": "Italy", "type": "Stratovolcano",
         "elevation_m": 924, "last_eruption": "2026", "lat": 38.789, "lon": 15.213},
        {"name": "Chaine des Puys", "country": "France", "type": "Lava dome(s)",
         "elevation_m": 1464, "last_eruption": "-4040", "lat": 45.786, "lon": 2.981},
        {"name": "Olot Volcanic Field", "country": "Spain", "type": "Volcanic field",
         "elevation_m": 893, "last_eruption": "", "lat": 42.17, "lon": 2.53},
    ],
    "count": 3,
    "source": "Smithsonian Institution Global Volcanism Program",
}

BIKESHARE = {  # real
    "stations": [
        {"id": "1822663031356509142", "name": "Matthews Ct & Coney Island Ave",
         "capacity": 0, "lat": 40.64235, "lon": -73.96951,
         "bikes_available": 0, "docks_available": 0},
        {"id": "7a6fc75f-a605-4dd6-b567-b2bbbfb862f3", "name": "65 Pl & 53 Ave",
         "capacity": 19, "lat": 40.73145, "lon": -73.90022,
         "bikes_available": 4, "docks_available": 15},
        {"id": "9be6c750-85b6-4e81-bace-1a8ffe941d7b", "name": "MacDougal St & Rockaway Ave",
         "capacity": 20, "lat": 40.68018, "lon": -73.91136,
         "bikes_available": None, "docks_available": None},
    ],
    "count": 3,
    "city": "nyc",
    "city_label": "New York (Citi Bike)",
    "cities": ["boston", "chicago", "dc", "nyc", "sf"],
    "source": "General Bikeshare Feed Specification",
}

RADIO = {  # real
    "stations": [
        {"id": "11c28f97-0177-47d3-bf88-eb24b492c34f", "name": "WAMU American University Radio",
         "country": "The United States Of America", "tags": "news,public,talk", "bitrate": 96,
         "url": "https://wamu.cdnstream1.com/wamu.mp3",
         "lat": 38.936933316017296, "lon": -77.08817571401597},
        {"id": "f604751e-e902-4e6e-9996-9d2c974d450f", "name": "Q104.3 New York's Classic Rock",
         "country": "The United States Of America", "tags": "classic rock", "bitrate": 0,
         "url": "https://stream.revma.ihrhls.com/zc1465",
         "lat": 40.71285390479267, "lon": -74.00545120239259},
    ],
    "count": 2,
    "source": "Radio Browser",
}

MILITARY = {  # real
    "sites": [
        {"id": "node/7703621098", "name": "NOAAS Thomas Jefferson", "kind": "naval_base",
         "operator": "", "lat": 36.8527056, "lon": -76.2983298},
        {"id": "node/3099892684", "name": "UNNAMED", "kind": "checkpoint",
         "operator": "", "lat": 37.3090292, "lon": -76.6815765},
        {"id": "relation/6050581", "name": "Naval Air Station Oceana", "kind": "airfield",
         "operator": "US Navy", "lat": 36.8118391, "lon": -76.0339375},
    ],
    "count": 3,
    "source": "OpenStreetMap via Overpass (ODbL)",
}

ALERTS = {  # from handler (upstream was down when probed)
    "alerts": [
        {"id": "urn:oid:2.49.0.1.840.0.1", "event": "Severe Thunderstorm Warning",
         "headline": "Severe Thunderstorm Warning until 3:00PM CDT by NWS Norman OK",
         "severity": "Severe", "urgency": "Immediate", "area": "Grady, OK; Caddo, OK",
         "expires": "2026-10-08T20:00:00-05:00", "color": "#ff8800",
         "rings": [[-98.0, 35.0, -97.0, 35.0, -97.0, 36.0, -98.0, 36.0, -98.0, 35.0]]},
        {"id": "urn:oid:2.49.0.1.840.0.2", "event": "Frost Advisory", "headline": "",
         "severity": "Minor", "urgency": "Expected", "area": "Aroostook, ME",
         "expires": "2026-10-09T13:00:00-04:00", "color": "#4ea3ff",
         "rings": [[-68.0, 46.0, -67.0, 46.0, -67.0, 47.0, -68.0, 47.0, -68.0, 46.0]]},
    ],
    "count": 2,
    "source": "NOAA National Weather Service (US only)",
}

GDELT = {  # from handler (upstream was down when probed)
    "events": [
        {"name": "Washington, District of Columbia, United States", "count": 42,
         "lat": 38.9, "lon": -77.0},
        {"name": "", "count": 7, "lat": 51.5, "lon": -0.12},
    ],
    "count": 2,
    "query": "satellite",
    "timespan": "24H",
    "source": "GDELT Project GEO 2.1",
}

FIRES = {  # from handler (key absent when probed)
    "fires": [
        {"lat": -33.5, "lon": 150.2, "brightness_k": 340.5, "frp_mw": 12.3,
         "confidence": "n", "acq_date": "2026-10-08", "acq_time": "0412",
         "daynight": "N", "satellite": "N"},
        {"lat": 61.2, "lon": -149.9, "brightness_k": 0.0, "frp_mw": 0.0,
         "confidence": "", "acq_date": "", "acq_time": "", "daynight": "", "satellite": ""},
    ],
    "count": 2,
    "key_configured": True,
    "source": "NASA FIRMS VIIRS_SNPP_NRT",
}

AIS = {  # from handler (key absent when probed)
    "vessels": [
        {"mmsi": "366999999", "name": "EVER GIVEN", "lat": 30.0, "lon": 32.5,
         "sog_kn": 12.3, "cog_deg": 87.0, "heading_deg": 86, "nav_status": 0,
         "time_utc": "2026-10-08 14:00:00 +0000 UTC"},
        {"mmsi": "211000000", "name": "", "lat": 53.5, "lon": 8.5,
         "sog_kn": 102.3, "cog_deg": 360.0, "heading_deg": 511, "nav_status": 5,
         "time_utc": ""},
    ],
    "count": 2,
    "key_configured": True,
    "connected": True,
    "error": None,
    "source": "AISStream",
}

SPACE_WEATHER_REAL = {  # real (Kp and wind came back null from the proxy)
    "kp": None, "kp_time": "", "g_scale": "",
    "solar_wind_speed_km_s": None, "solar_wind_density_p_cm3": None, "solar_wind_time": "",
    "alerts": [
        {"product_id": "EF3A", "issued": "2026-10-08 05:01:00.663",
         "headline": "Space Weather Message Code: ALTEF3"},
        {"product_id": "K04W", "issued": "2026-10-06 03:43:55.093",
         "headline": "Space Weather Message Code: WARK04"},
    ],
    "count": 2,
    "source": "NOAA SWPC",
}

SPACE_WEATHER_FULL = {  # from handler
    "kp": 5.33, "kp_time": "2026-10-08 12:00:00", "g_scale": "G1",
    "solar_wind_speed_km_s": 412.3, "solar_wind_density_p_cm3": 5.2,
    "solar_wind_time": "2026-10-08 13:55:00",
    "alerts": [], "count": 0, "source": "NOAA SWPC",
}

FIRES_NO_KEY = {  # real
    "fires": [], "count": 0, "key_configured": False,
    "error": "FIRMS_MAP_KEY is not set on the server",
}
AIS_NO_KEY = {  # real
    "vessels": [], "count": 0, "key_configured": False, "connected": False,
    "error": "AISSTREAM_API_KEY is not set on the server",
}
ALERTS_DOWN = {"alerts": [], "count": 0, "error": "api.weather.gov unavailable"}  # real
GDELT_DOWN = {  # real
    "events": [], "count": 0, "query": "satellite", "error": "GDELT unavailable",
}
RADIO_DOWN = {"stations": [], "count": 0, "error": "Radio Browser unavailable"}  # from handler
LAUNCHES_DOWN = {"launches": [], "count": 0, "error": "Launch Library unavailable"}  # real shape
SPACE_WEATHER_DOWN = {  # from handler
    "kp": None, "kp_time": "", "g_scale": "", "solar_wind_speed_km_s": None,
    "solar_wind_density_p_cm3": None, "solar_wind_time": "", "alerts": [], "count": 0,
    "source": "NOAA SWPC", "error": "SWPC unavailable",
}

RESPONSES = {
    "launches": LAUNCHES, "volcanoes": VOLCANOES, "bikeshare": BIKESHARE, "radio": RADIO,
    "military": MILITARY, "weatheralerts": ALERTS, "gdelt": GDELT, "fires": FIRES, "ais": AIS,
}
LIST_KEYS = {
    "launches": "launches", "volcanoes": "volcanoes", "bikeshare": "stations",
    "radio": "stations", "military": "sites", "weatheralerts": "alerts", "gdelt": "events",
    "fires": "fires", "ais": "vessels",
}

# key -> [(name, lat, lon, rank, detail rows)], in response order
EXPECTED = {
    "launches": [
        ("Long March 12 | Unknown Payload", 19.59755, 110.936481, None, [
            ["NET (UTC)", "2026-10-09T19:25:00Z"], ["STATUS", "Go"],
            ["PROVIDER", "China Aerospace Science and Technology Corporation"],
            ["MISSION", "Unknown Payload"], ["ORBIT", "N/A"], ["PAD", "Commercial LC-2"],
            ["SITE", "Wenchang Space Launch Site, People's Republic of China"]]),
        ("Falcon 9 Block 5 | SDA Tranche 1 Transport Layer A", 34.632, -120.611, None, [
            ["NET (UTC)", "2026-10-10T07:29:00Z"], ["STATUS", "Go"], ["PROVIDER", "SpaceX"],
            ["MISSION", "SDA Tranche 1 Transport Layer A"], ["ORBIT", "PO"],
            ["PAD", "Space Launch Complex 4E"], ["SITE", "Vandenberg SFB, CA, USA"]]),
    ],
    "volcanoes": [
        ("Stromboli", 38.789, 15.213, 2026, [
            ["COUNTRY", "Italy"], ["VOLCANO TYPE", "Stratovolcano"],
            ["ELEVATION", "924 m"], ["LAST ERUPTION", "2026"]]),
        ("Chaine des Puys", 45.786, 2.981, -4040, [
            ["COUNTRY", "France"], ["VOLCANO TYPE", "Lava dome(s)"],
            ["ELEVATION", "1464 m"], ["LAST ERUPTION", "4040 BCE"]]),
        ("Olot Volcanic Field", 42.17, 2.53, None, [
            ["COUNTRY", "Spain"], ["VOLCANO TYPE", "Volcanic field"],
            ["ELEVATION", "893 m"], ["LAST ERUPTION", "unknown"]]),
    ],
    "bikeshare": [
        ("Matthews Ct & Coney Island Ave", 40.64235, -73.96951, None, [
            ["BIKES", "0"], ["DOCKS", "0"], ["CAPACITY", "0"]]),
        ("65 Pl & 53 Ave", 40.73145, -73.90022, None, [
            ["BIKES", "4"], ["DOCKS", "15"], ["CAPACITY", "19"]]),
        ("MacDougal St & Rockaway Ave", 40.68018, -73.91136, None, [
            ["BIKES", "n/a"], ["DOCKS", "n/a"], ["CAPACITY", "20"]]),
    ],
    "radio": [
        ("WAMU American University Radio", 38.936933316017296, -77.08817571401597, None, [
            ["COUNTRY", "The United States Of America"], ["BITRATE", "96 kbps"],
            ["TAGS", "news,public,talk"], ["STREAM", "https://wamu.cdnstream1.com/wamu.mp3"]]),
        ("Q104.3 New York's Classic Rock", 40.71285390479267, -74.00545120239259, None, [
            ["COUNTRY", "The United States Of America"], ["TAGS", "classic rock"],
            ["STREAM", "https://stream.revma.ihrhls.com/zc1465"]]),
    ],
    "military": [
        ("NOAAS Thomas Jefferson", 36.8527056, -76.2983298, None, [["KIND", "naval_base"]]),
        ("Unnamed checkpoint", 37.3090292, -76.6815765, None, [["KIND", "checkpoint"]]),
        ("Naval Air Station Oceana", 36.8118391, -76.0339375, None, [
            ["KIND", "airfield"], ["OPERATOR", "US Navy"]]),
    ],
    "weatheralerts": [
        ("Severe Thunderstorm Warning", 35.5, -97.5, 3, [
            ["SEVERITY", "Severe"], ["URGENCY", "Immediate"], ["AREA", "Grady, OK; Caddo, OK"],
            ["EXPIRES", "2026-10-08T20:00:00-05:00"],
            ["HEADLINE", "Severe Thunderstorm Warning until 3:00PM CDT by NWS Norman OK"]]),
        ("Frost Advisory", 46.5, -67.5, 1, [
            ["SEVERITY", "Minor"], ["URGENCY", "Expected"], ["AREA", "Aroostook, ME"],
            ["EXPIRES", "2026-10-09T13:00:00-04:00"]]),
    ],
    "gdelt": [
        ("Washington, District of Columbia, United States", 38.9, -77.0, 42, [
            ["ARTICLES", "42"]]),
        ("Unnamed place", 51.5, -0.12, 7, [["ARTICLES", "7"]]),
    ],
    "fires": [
        ("Fire 2026-10-08 04:12 UTC", -33.5, 150.2, 12.3, [
            ["FRP", "12.3 MW"], ["BRIGHTNESS", "340.5 K"], ["CONFIDENCE", "n"],
            ["DETECTED", "2026-10-08 04:12 UTC"], ["SATELLITE", "N"], ["DAY/NIGHT", "N"]]),
        ("Fire detection", 61.2, -149.9, 0.0, []),
    ],
    "ais": [
        ("EVER GIVEN", 30.0, 32.5, None, [
            ["MMSI", "366999999"], ["SPEED", "12.3 kn"], ["COURSE", "87 deg"],
            ["HEADING", "86 deg"], ["NAV STATUS", "under way using engine"],
            ["REPORTED", "2026-10-08 14:00:00 +0000 UTC"]]),
        ("MMSI 211000000", 53.5, 8.5, None, [
            ["MMSI", "211000000"], ["NAV STATUS", "moored"]]),
    ],
}

INTERPRET_JS = """
const r = L.interpret(DATA.key, 200, DATA.response);
out({state: r.state, dropped: r.dropped, message: r.message, keyEnv: r.keyEnv || '',
     points: r.points.map(function (p) {
       return [p.name, p.lat, p.lon, p.rank, p.rows];
     })});
"""


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_each_feed_fixture_becomes_the_expected_points(key):
    got = run_node(INTERPRET_JS, {"key": key, "response": RESPONSES[key]})
    assert got["state"] == "ok"
    assert got["dropped"] == 0
    expected = [list(item) for item in EXPECTED[key]]
    assert len(got["points"]) == len(expected)
    for have, want in zip(got["points"], expected, strict=True):
        assert have[0] == want[0]
        assert have[1] == pytest.approx(want[1])
        assert have[2] == pytest.approx(want[2])
        assert have[3] == want[3]
        assert have[4] == want[4]


# --- status mapping ---

STATUS_CASES = [
    ("fires", 200, FIRES_NO_KEY, "nokey"),
    ("ais", 200, AIS_NO_KEY, "nokey"),
    ("weatheralerts", 200, ALERTS_DOWN, "down"),
    ("gdelt", 200, GDELT_DOWN, "down"),
    ("radio", 200, RADIO_DOWN, "down"),
    ("launches", 200, LAUNCHES_DOWN, "down"),
    ("spaceweather", 200, SPACE_WEATHER_DOWN, "down"),
    ("military", 422, {"detail": "Bounding box span exceeds 6.0 degrees"}, "error"),
    ("volcanoes", 500, None, "error"),
    ("volcanoes", 200, None, "error"),
    ("volcanoes", 200, {"unexpected": True}, "error"),
    ("launches", 200, LAUNCHES, "ok"),
    ("spaceweather", 200, SPACE_WEATHER_REAL, "ok"),
]


@pytest.mark.parametrize(("key", "http", "response", "state"), STATUS_CASES)
def test_response_maps_to_the_right_status(key, http, response, state):
    got = run_node(
        "const r = L.interpret(DATA.key, DATA.http, DATA.response);"
        "out({state: r.state, message: r.message, keyEnv: r.keyEnv || ''});",
        {"key": key, "http": http, "response": response},
    )
    assert got["state"] == state


@pytest.mark.parametrize(
    ("key", "env"), [("fires", "FIRMS_MAP_KEY"), ("ais", "AISSTREAM_API_KEY")]
)
def test_missing_key_names_the_key_and_says_it_is_free(key, env):
    response = FIRES_NO_KEY if key == "fires" else AIS_NO_KEY
    got = run_node(
        "const r = L.interpret(DATA.key, 200, DATA.response);"
        "out({keyEnv: r.keyEnv, message: r.message});",
        {"key": key, "response": response},
    )
    assert got["keyEnv"] == env
    assert env in got["message"]
    assert "free" in got["message"].lower()


def test_key_present_but_refused_is_down_not_missing_key():
    refused = {"fires": [], "count": 0, "key_configured": True,
               "error": "FIRMS refused the request (key invalid or transaction limit)"}
    got = run_node(
        "out(L.interpret('fires', 200, DATA).state);", refused
    )
    assert got == "down"


def test_ais_connecting_with_no_vessels_yet_is_ok_with_a_note():
    waiting = {"vessels": [], "count": 0, "key_configured": True, "connected": False,
               "error": None, "source": "AISStream"}
    got = run_node(
        "const r = L.interpret('ais', 200, DATA); out({state: r.state, note: r.note || ''});",
        waiting,
    )
    assert got["state"] == "ok"
    assert got["note"]


def test_rows_with_a_partial_error_stay_ok():
    partial = dict(AIS, error="ConnectionError: reset")
    got = run_node(
        "const r = L.interpret('ais', 200, DATA); out([r.state, r.points.length]);", partial
    )
    assert got == ["ok", 2]


# --- records without usable coordinates are dropped ---

BAD_COORD_JS = """
const out_rows = {};
for (const key of Object.keys(DATA.responses)) {
  const list = DATA.listKeys[key];
  const good = DATA.responses[key][list][0];
  const variants = [];
  const mutate = function (patch) {
    const row = JSON.parse(JSON.stringify(good));
    Object.keys(patch).forEach(function (k) { row[k] = patch[k]; });
    variants.push(row);
  };
  if (key === 'weatheralerts') {
    mutate({rings: []});
    mutate({rings: [[1, 2]]});
    mutate({rings: [['a', 'b', 'c', 'd', 'e', 'f']]});
    mutate({rings: null});
    mutate({rings: [[91, 200, 91, 200, 91, 200]]});
  } else {
    mutate({lat: null});
    mutate({lon: 'abc'});
    mutate({lat: ''});
    mutate({lon: undefined});
    mutate({lat: 91});
    mutate({lon: -181});
    mutate({lat: true});
    mutate({lat: [], lon: {}});
    mutate({lat: 0, lon: 0});
  }
  const response = JSON.parse(JSON.stringify(DATA.responses[key]));
  response[list] = [good].concat(variants);
  const r = L.interpret(key, 200, response);
  out_rows[key] = {state: r.state, kept: r.points.length, dropped: r.dropped,
                   variants: variants.length};
}
out(out_rows);
"""


def test_records_with_missing_or_non_numeric_coordinates_are_dropped():
    got = run_node(BAD_COORD_JS, {"responses": RESPONSES, "listKeys": LIST_KEYS})
    assert set(got) == set(LIST_KEYS)
    for key, result in got.items():
        assert result["state"] == "ok", key
        assert result["kept"] == 1, key
        assert result["dropped"] == result["variants"], key


def test_numeric_string_coordinates_are_accepted():
    response = {"volcanoes": [{"name": "Test", "country": "", "type": "", "elevation_m": 0,
                               "last_eruption": "", "lat": "12.5", "lon": " -77.04 "}]}
    got = run_node(
        "const p = L.interpret('volcanoes', 200, DATA).points;"
        "out([p.length, p[0].lat, p[0].lon]);",
        response,
    )
    assert got == [1, 12.5, -77.04]


TO_COORD_CASES = [
    (12.5, 90, 12.5), ("12.5", 90, 12.5), (" -77.04 ", 180, -77.04), (0, 90, 0),
    (90, 90, 90), (-180, 180, -180), (91, 90, None), (-181, 180, None),
    (None, 90, None), ("", 90, None), ("abc", 90, None), (True, 90, None),
    ([], 90, None), ({}, 90, None),
]


@pytest.mark.parametrize(("value", "limit", "expected"), TO_COORD_CASES)
def test_to_coord(value, limit, expected):
    got = run_node("out(L.toCoord(DATA.value, DATA.limit));", {"value": value, "limit": limit})
    assert got == expected


def test_to_coord_rejects_nan_and_infinity():
    got = run_node("out([L.toCoord(NaN, 90), L.toCoord(Infinity, 90), L.toCoord('NaN', 90)]);")
    assert got == [None, None, None]


# --- text and number helpers ---


def test_clean_text_strips_control_characters_and_truncates_but_keeps_markup_literal():
    got = run_node(
        "out([L.cleanText('a\\u0000b\\u0007c\\n d', 50), L.cleanText('x'.repeat(30), 10),"
        " L.cleanText(null, 10), L.cleanText(42, 10), L.cleanText(DATA.markup, 100)]);",
        {"markup": "<img src=x onerror=alert(1)>"},
    )
    assert got == ["abc d", "xxxxxxx...", "", "42", "<img src=x onerror=alert(1)>"]


def test_format_int_groups_thousands():
    got = run_node(
        "out([0, 999, 2000, 1234567].map(function (n) { return L.formatInt(n); }));"
    )
    assert got == ["0", "999", "2,000", "1,234,567"]


def test_format_clock_is_local_hh_mm():
    got = run_node("out(L.formatClock(new Date(2026, 9, 8, 14, 5)));")
    assert got == "14:05"


def test_format_countdown():
    got = run_node(
        "const now = Date.parse('2026-10-08T16:00:00Z');"
        "out(['2026-10-09T19:25:00Z', '2026-10-08T21:30:00Z', '2026-10-08T16:12:00Z',"
        " '2026-10-08T15:00:00Z', '', 'garbage'].map(function (n) {"
        " return L.formatCountdown(n, now); }));"
    )
    assert got == ["in 1d 03h", "in 5h 30m", "in 12m", "past", "", ""]


# --- request parameters from the camera ---


def test_bbox_around_stays_valid_and_within_the_server_span_limit():
    got = run_node(
        "out(DATA.map(function (c) { return L.bboxAround(c[0], c[1], 5); }));",
        [[37.0, -76.0], [89.0, 10.0], [-90.0, 0.0], [0.0, 179.5], [0.0, -180.0]],
    )
    assert got[0] == {"south": 34.5, "west": -78.5, "north": 39.5, "east": -73.5}
    for box in got:
        assert -90 <= box["south"] < box["north"] <= 90
        assert -180 <= box["west"] < box["east"] <= 180
        assert box["north"] - box["south"] <= wv._MILITARY_MAX_SPAN_DEG
        assert box["east"] - box["west"] <= wv._MILITARY_MAX_SPAN_DEG


@pytest.mark.parametrize(
    ("lat", "lon", "city"),
    [(40.7, -74.0, "nyc"), (38.9, -77.0, "dc"), (41.9, -87.6, "chicago"),
     (42.36, -71.06, "boston"), (37.77, -122.4, "sf"), (35.7, 139.7, "sf")],
)
def test_nearest_city(lat, lon, city):
    assert run_node("out(L.nearestCity(DATA[0], DATA[1]));", [lat, lon]) == city


def test_build_path_supplies_the_parameters_each_proxy_needs():
    got = run_node(
        "out(['radio', 'military', 'bikeshare', 'launches', 'fires', 'ais', 'gdelt']"
        ".map(function (k) { return L.buildPath(k, DATA); }));",
        {"lat": 36.9, "lon": -76.2},
    )
    radio, military, bike, launches, fires, ais, gdelt = (urlparse(u) for u in got)
    assert radio.path == "/api/radio"
    q = {k: v[0] for k, v in parse_qs(radio.query).items()}
    assert float(q["lat"]) == pytest.approx(36.9, abs=0.01)
    assert float(q["lon"]) == pytest.approx(-76.2, abs=0.01)
    assert 0 < int(q["limit"]) <= 200
    assert military.path == "/api/military-sites"
    box = {k: float(v[0]) for k, v in parse_qs(military.query).items()}
    assert box["north"] > box["south"] and box["east"] > box["west"]
    assert box["north"] - box["south"] <= wv._MILITARY_MAX_SPAN_DEG
    assert parse_qs(bike.query) == {"city": ["dc"]}
    assert launches.query == "" and fires.query == "" and ais.query == "" and gdelt.query == ""


def test_ring_centroid_drops_the_closing_vertex_and_handles_the_antimeridian():
    got = run_node(
        "out([L.ringCentroid([[-98, 35, -97, 35, -97, 36, -98, 36, -98, 35]]),"
        " L.ringCentroid([[179, 10, -179, 10, -179, 12, 179, 12, 179, 10]]),"
        " L.ringCentroid([]), L.ringCentroid([[1, 2]]), L.ringCentroid(null)]);"
    )
    assert got[0] == {"lat": 35.5, "lon": -97.5}
    assert got[1]["lat"] == pytest.approx(11.0)
    assert abs(got[1]["lon"]) == pytest.approx(180.0)
    assert got[2:] == [None, None, None]


# --- the cap ---

SELECT_JS = """
const pts = DATA.ranks.map(function (r, i) {
  return {name: 'p' + i, lat: DATA.lats ? DATA.lats[i] : 0, lon: DATA.lons ? DATA.lons[i] : 0,
          rank: r};
});
const sel = L.selectForDisplay(pts, DATA.cap, DATA.mode, DATA.center);
out({names: sel.shown.map(function (p) { return p.name; }), total: sel.total,
     capped: sel.capped});
"""


def test_cap_by_rank_keeps_the_highest_and_breaks_ties_in_response_order():
    got = run_node(SELECT_JS, {"ranks": [3, 9, 1, 9, 7, None], "cap": 3, "mode": "rank"})
    assert got == {"names": ["p1", "p3", "p4"], "total": 6, "capped": True}


def test_cap_by_rank_puts_unranked_points_last():
    got = run_node(SELECT_JS, {"ranks": [None, 5, None, 1], "cap": 3, "mode": "rank"})
    assert got["names"] == ["p1", "p3", "p0"]


def test_cap_by_distance_keeps_the_points_nearest_the_camera_across_the_antimeridian():
    got = run_node(
        SELECT_JS,
        {"ranks": [None, None, None], "lats": [0, 0, 0], "lons": [100, -179.9, 170],
         "cap": 2, "mode": "nearest", "center": {"lat": 0, "lon": 179.9}},
    )
    assert got["names"] == ["p1", "p2"]
    assert got["capped"] is True


def test_no_cap_hit_returns_everything_in_response_order():
    got = run_node(SELECT_JS, {"ranks": [1, 2, 3], "cap": 500, "mode": "rank"})
    assert got == {"names": ["p0", "p1", "p2"], "total": 3, "capped": False}


def test_two_thousand_stations_are_capped_to_500_and_the_panel_text_says_so():
    got = run_node(
        "const pts = []; for (let i = 0; i < 2000; i++) {"
        " pts.push({name: 's' + i, lat: i / 100, lon: 0, rank: null}); }"
        "const sel = L.selectForDisplay(pts, 500, 'nearest', {lat: 0, lon: 0});"
        "out([sel.shown.length, sel.total, sel.shown[0].name, sel.shown[499].name,"
        " L.capNote(sel.shown.length, sel.total), L.capNote(20, 20)]);"
    )
    assert got == [500, 2000, "s0", "s499", "showing 500 of 2,000", "20 shown"]


# --- refresh rules ---


def test_refresh_never_comes_sooner_than_five_minutes():
    got = run_node(
        "const f = L.feedByKey('launches');"
        "const base = {on: true, busy: false, status: 'ok', lastFetchMs: 1000000};"
        "out([L.canRefresh(null, 5), L.canRefresh(1000000, 1000000 + 299999),"
        " L.canRefresh(1000000, 1000000 + 300000),"
        " L.isDue(f, base, 1000000 + 899999), L.isDue(f, base, 1000000 + 900000),"
        " L.isDue(f, Object.assign({}, base, {status: 'down'}), 1000000 + 9e9),"
        " L.isDue(f, Object.assign({}, base, {status: 'error'}), 1000000 + 9e9),"
        " L.isDue(f, Object.assign({}, base, {busy: true}), 1000000 + 9e9),"
        " L.isDue(f, Object.assign({}, base, {on: false}), 1000000 + 9e9),"
        " L.isDue(L.feedByKey('volcanoes'), base, 1000000 + 9e9)]);"
    )
    assert got == [True, False, True, False, True, False, False, False, False, False]


# --- space weather values ---


def test_space_weather_real_fixture_has_no_kp_but_keeps_alerts():
    got = run_node("out(L.interpret('spaceweather', 200, DATA).weather);", SPACE_WEATHER_REAL)
    assert got["kp"] is None and got["windSpeed"] is None and got["windDensity"] is None
    assert [a["headline"] for a in got["alerts"]] == [
        "Space Weather Message Code: ALTEF3",
        "Space Weather Message Code: WARK04",
    ]
    assert got["alerts"][0]["issued"] == "2026-10-08 05:01:00.663"


def test_space_weather_values_are_read_when_present():
    got = run_node("out(L.interpret('spaceweather', 200, DATA).weather);", SPACE_WEATHER_FULL)
    assert got["kp"] == 5.33 and got["gScale"] == "G1"
    assert got["windSpeed"] == 412.3 and got["windDensity"] == 5.2
    assert got["alerts"] == []


def test_attribution_line_names_the_source():
    got = run_node("out(L.attributionLine('volcanoes'));")
    assert "Smithsonian" in got
    assert run_node("out(L.attributionLine('nope'));") == ""


# --- the panel against a fake DOM, fake Cesium, fake fetch and a controllable clock ---

DOM_KIT = r"""
const clock = { t: Date.parse('2026-10-08T14:00:00Z') };
Date.now = function () { return clock.t; };
const timers = [];
global.setInterval = function (fn, ms) {
  timers.push({ fn: fn, ms: ms, next: clock.t + ms });
  return timers.length;
};
global.clearInterval = function () {};
const globalListeners = [];
global.addEventListener = function (type) { globalListeners.push(type); };
const violations = [];
function flush() {
  return new Promise(function (r) { setImmediate(function () { setImmediate(r); }); });
}
async function advance(ms) {
  const end = clock.t + ms;
  for (;;) {
    let due = null;
    for (const t of timers) { if (t.next <= end && (!due || t.next < due.next)) due = t; }
    if (!due) break;
    clock.t = due.next;
    due.next += due.ms;
    due.fn();
    await flush();
  }
  clock.t = end;
  await flush();
}

class El {
  constructor(tag, doc) {
    this.tag = tag; this.ownerDocument = doc; this.children = []; this.attrs = {};
    this.style = {}; this.listeners = {}; this._text = ''; this.className = '';
    this.checked = false; this.disabled = false; this.id = ''; this.title = '';
    const self = this;
    this.classList = {
      contains: function (c) { return self.className.split(' ').indexOf(c) >= 0; },
      add: function (c) { if (!this.contains(c)) self.className = (self.className + ' ' + c).trim(); },
      remove: function (c) {
        self.className = self.className.split(' ').filter(function (x) { return x && x !== c; }).join(' ');
      },
      toggle: function (c) { if (this.contains(c)) this.remove(c); else this.add(c); }
    };
  }
  get textContent() { return this._text + this.children.map(function (c) { return c.textContent; }).join(' '); }
  set textContent(v) { this.children = []; this._text = String(v); }
  set innerHTML(v) { violations.push('inner' + 'HTML set'); throw new Error('markup injection'); }
  get innerHTML() { violations.push('inner' + 'HTML read'); throw new Error('markup injection'); }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(t) {
    const evt = { target: this, preventDefault: function () {}, stopPropagation: function () {},
                  stopImmediatePropagation: function () {} };
    (this.listeners[t] || []).forEach(function (fn) { fn(evt); });
  }
  find(pred) {
    if (pred(this)) return this;
    for (const c of this.children) { const f = c.find(pred); if (f) return f; }
    return null;
  }
  findAll(pred, acc) {
    acc = acc || [];
    if (pred(this)) acc.push(this);
    this.children.forEach(function (c) { c.findAll(pred, acc); });
    return acc;
  }
}

function makeCesium() {
  class Entities {
    constructor() { this.values = []; }
    add(o) { this.values.push(o); return o; }
    removeAll() { this.values = []; }
    suspendEvents() {}
    resumeEvents() {}
  }
  return {
    CustomDataSource: class { constructor(name) { this.name = name; this.entities = new Entities(); this.show = true; } },
    Cartesian3: { fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h || 0 }; } },
    Cartesian2: class { constructor(x, y) { this.x = x; this.y = y; } },
    Cartographic: { fromCartesian: function (c) { return { latitude: c.lat * Math.PI / 180, longitude: c.lon * Math.PI / 180 }; } },
    Color: { fromCssColorString: function (css) {
      const c = { css: css, alpha: 1, withAlpha: function (a) { return { css: css, alpha: a, withAlpha: c.withAlpha }; } };
      return c;
    } }
  };
}

function makeEnv(opts) {
  opts = opts || {};
  const doc = { listeners: [], createElement: function (tag) { return new El(tag, doc); },
                addEventListener: function (t) { doc.listeners.push(t); } };
  doc.head = new El('head', doc);
  const mount = new El('div', doc);
  if (!opts.noCesium) global.Cesium = makeCesium(); else delete global.Cesium;
  const center = opts.center || { lat: 38.9, lon: -77.0 };
  const viewer = {
    dataSources: { items: [], add: function (ds) { this.items.push(ds); return Promise.resolve(ds); },
                   remove: function (ds) { this.items = this.items.filter(function (d) { return d !== ds; }); } },
    camera: { pickEllipsoid: function () { return { lat: center.lat, lon: center.lon }; },
              positionCartographic: { latitude: 0, longitude: 0 } },
    scene: { canvas: { clientWidth: 800, clientHeight: 600 }, globe: { ellipsoid: {} } }
  };
  const calls = [];
  const times = [];
  const api = opts.api || '';
  const routes = opts.routes || {};
  global.fetch = function (url) {
    calls.push(url);
    times.push(clock.t);
    const path = url.slice(api.length).split('?')[0];
    const handler = routes[path];
    if (!handler) return Promise.reject(new Error('no route ' + path));
    return Promise.resolve().then(function () { return handler(url); }).then(function (r) {
      return { status: r.status || 200, ok: (r.status || 200) < 400,
               json: function () { return Promise.resolve(r.body); } };
    });
  };
  const intel = [];
  const ctx = { viewer: viewer, addIntelItem: function (t) { intel.push(t); }, api: api,
                isStatic: !!opts.isStatic, mount: mount };
  return { doc: doc, mount: mount, viewer: viewer, ctx: ctx, calls: calls, times: times, intel: intel };
}
function byId(env, id) { return env.mount.find(function (e) { return e.id === id; }); }
function toggle(env, key, on) { const el = byId(env, 'sdalayers-' + key); el.checked = on; el.fire('change'); }
function ds(env, key) {
  return env.viewer.dataSources.items.find(function (d) { return d.name === 'sdalayers-' + key; });
}
function entities(env, key) { const d = ds(env, key); return d ? d.entities.values : []; }
function status(env, key) { const e = byId(env, 'sdalayers-status-' + key); return e ? e.textContent : null; }
function totalEntities(env) {
  return env.viewer.dataSources.items.reduce(function (n, d) { return n + d.entities.values.length; }, 0);
}
"""  # noqa: E501


def run_panel(body: str, data=None):
    return run_node(body, data, kit=DOM_KIT)


def test_static_demo_renders_one_note_and_fetches_nothing():
    got = run_panel(
        """
        const env = makeEnv({isStatic: true});
        L.init(env.ctx);
        await flush();
        out({fetches: env.calls.length, text: env.mount.textContent,
             inputs: env.mount.findAll(function (e) { return e.tag === 'input'; }).length,
             sources: env.viewer.dataSources.items.length, timers: timers.length,
             violations: violations});
        """
    )
    assert got["fetches"] == 0
    assert got["inputs"] == 0
    assert got["sources"] == 0
    assert "live server" in got["text"]
    assert len(got["text"].strip()) < 140
    assert got["violations"] == []


def test_live_panel_has_one_unchecked_toggle_per_feed_and_fetches_nothing_at_load():
    got = run_panel(
        """
        const env = makeEnv({});
        L.init(env.ctx);
        await flush();
        await advance(3600 * 1000);
        const boxes = env.mount.findAll(function (e) { return e.tag === 'input' && e.type === 'checkbox'; });
        const top = env.mount.children[0];
        out({fetches: env.calls.length, boxes: boxes.length,
             checked: boxes.filter(function (b) { return b.checked; }).length,
             ids: boxes.map(function (b) { return b.id; }),
             entities: totalEntities(env), panelClass: top.className,
             h3: top.find(function (e) { return e.tag === 'h3'; }) !== null,
             body: top.find(function (e) { return e.className.indexOf('panel-body') >= 0; }) !== null,
             toggleRows: env.mount.findAll(function (e) { return e.className.indexOf('layer-toggle') >= 0; }).length,
             style: env.doc.head.children.map(function (c) { return [c.tag, c.textContent.slice(0, 40)]; }),
             docListeners: env.doc.listeners, globalListeners: globalListeners,
             violations: violations});
        """  # noqa: E501
    )
    assert got["fetches"] == 0
    assert got["boxes"] == 10 and got["checked"] == 0
    assert got["ids"] == [f"sdalayers-{k}" for k in FEED_KEYS]
    assert got["entities"] == 0
    assert "panel" in got["panelClass"].split()
    assert got["h3"] and got["body"]
    assert got["toggleRows"] == 10
    assert len(got["style"]) == 1 and got["style"][0][0] == "style"
    assert "sdalayers-" in got["style"][0][1] or got["style"][0][1].startswith(".sdalayers")
    key_events = [t for t in got["docListeners"] + got["globalListeners"] if t.startswith("key")]
    assert key_events == []
    assert got["violations"] == []


def test_init_without_cesium_shows_a_note_and_does_not_throw():
    got = run_panel(
        """
        const env = makeEnv({noCesium: true});
        L.init(env.ctx);
        out({text: env.mount.textContent, fetches: env.calls.length});
        """
    )
    assert got["fetches"] == 0
    assert got["text"].strip()


def test_toggle_on_fetches_once_draws_points_and_toggle_off_removes_them():
    got = run_panel(
        """
        const env = makeEnv({api: 'http://srv:1', routes: {
          '/api/launches': function () { return {body: DATA.launches}; }}});
        L.init(env.ctx);
        toggle(env, 'launches', true);
        await flush();
        const e0 = entities(env, 'launches');
        const snapshot = {
          calls: env.calls.slice(), count: e0.length, show: ds(env, 'launches').show,
          first: {name: e0[0].name, pos: e0[0].position, type: e0[0].properties.type,
                  feed: e0[0].properties.feed, rows: e0[0].properties.detail_rows,
                  px: e0[0].point.pixelSize},
          ids: e0.map(function (e) { return e.id; }),
          status: status(env, 'launches'), badge: byId(env, 'sdalayers-count-launches').textContent
        };
        toggle(env, 'launches', false);
        await flush();
        const afterOff = {count: entities(env, 'launches').length, status: status(env, 'launches'),
                          calls: env.calls.length};
        toggle(env, 'launches', true);
        await flush();
        out({snapshot: snapshot, afterOff: afterOff, afterOn: {count: entities(env, 'launches').length,
             calls: env.calls.length}, violations: violations, intel: env.intel});
        """,  # noqa: E501
        {"launches": LAUNCHES},
    )
    snap = got["snapshot"]
    assert snap["calls"] == ["http://srv:1/api/launches"]
    assert snap["count"] == 2 and snap["show"] is True
    assert snap["first"]["name"] == "Long March 12 | Unknown Payload"
    assert snap["first"]["pos"] == {"lon": 110.936481, "lat": 19.59755, "h": 0}
    assert snap["first"]["type"] == "launch" and snap["first"]["feed"] == "launches"
    assert snap["first"]["rows"] == EXPECTED["launches"][0][4]
    assert snap["first"]["px"] > 0
    assert len(set(snap["ids"])) == 2
    assert "2 shown" in snap["status"]
    assert snap["badge"].strip() == "2"
    assert got["afterOff"] == {"count": 0, "status": "", "calls": 1}
    assert got["afterOn"] == {"count": 2, "calls": 1}
    assert got["violations"] == []
    assert got["intel"]


def test_response_that_arrives_after_toggle_off_is_discarded():
    got = run_panel(
        """
        let release;
        const gate = new Promise(function (r) { release = r; });
        const env = makeEnv({routes: {
          '/api/launches': function () { return gate.then(function () { return {body: DATA}; }); }}});
        L.init(env.ctx);
        toggle(env, 'launches', true);
        await flush();
        toggle(env, 'launches', false);
        release();
        await flush();
        out({count: totalEntities(env), checked: byId(env, 'sdalayers-launches').checked});
        """,  # noqa: E501
        LAUNCHES,
    )
    assert got == {"count": 0, "checked": False}


def test_missing_key_reverts_the_toggle_names_the_key_and_never_retries():
    got = run_panel(
        """
        const env = makeEnv({routes: {
          '/api/fires': function () { return {body: DATA}; }}});
        L.init(env.ctx);
        toggle(env, 'fires', true);
        await flush();
        const box = byId(env, 'sdalayers-fires');
        const shown = status(env, 'fires');
        await advance(3 * 3600 * 1000);
        out({checked: box.checked, disabled: box.disabled, status: shown,
             entities: totalEntities(env), calls: env.calls.length, intel: env.intel});
        """,
        FIRES_NO_KEY,
    )
    assert got["checked"] is False and got["disabled"] is True
    assert "FIRMS_MAP_KEY" in got["status"] and "free" in got["status"].lower()
    assert got["entities"] == 0 and got["calls"] == 1


def test_upstream_down_shows_the_time_and_does_not_retry_in_a_loop():
    got = run_panel(
        """
        const env = makeEnv({routes: {
          '/api/gdelt': function () { return {body: DATA}; }}});
        L.init(env.ctx);
        toggle(env, 'gdelt', true);
        await flush();
        const stamp = L.formatClock(new Date(Date.now()));
        const first = status(env, 'gdelt');
        await advance(2 * 60 * 1000);
        const refresh = byId(env, 'sdalayers-refresh-gdelt');
        const early = {disabled: refresh.disabled};
        refresh.fire('click');
        await flush();
        const callsEarly = env.calls.length;
        await advance(4 * 60 * 1000);
        const late = {disabled: refresh.disabled};
        refresh.fire('click');
        await flush();
        const callsAfterManual = env.calls.length;
        await advance(3 * 3600 * 1000);
        out({stamp: stamp, first: first, checked: byId(env, 'sdalayers-gdelt').checked,
             early: early, callsEarly: callsEarly, late: late, callsAfterManual: callsAfterManual,
             callsEnd: env.calls.length});
        """,
        GDELT_DOWN,
    )
    assert "source unavailable" in got["first"]
    assert got["stamp"] in got["first"]
    assert got["checked"] is True
    assert got["early"]["disabled"] is True and got["callsEarly"] == 1
    assert got["late"]["disabled"] is False and got["callsAfterManual"] == 2
    assert got["callsEnd"] == 2


def test_one_failing_feed_leaves_the_others_alone():
    got = run_panel(
        """
        const env = makeEnv({routes: {
          '/api/launches': function () { throw new Error('boom'); },
          '/api/military-sites': function () { return {status: 422, body: {detail: 'Empty bounding box'}}; },
          '/api/volcanoes': function () { return {body: DATA.volcanoes}; }}});
        L.init(env.ctx);
        toggle(env, 'launches', true);
        toggle(env, 'military', true);
        toggle(env, 'volcanoes', true);
        toggle(env, 'gdelt', true);
        await flush();
        out({volcanoes: entities(env, 'volcanoes').length, launches: entities(env, 'launches').length,
             launchStatus: status(env, 'launches'), militaryStatus: status(env, 'military'),
             gdeltStatus: status(env, 'gdelt'), volcanoStatus: status(env, 'volcanoes'),
             violations: violations});
        """,  # noqa: E501
        {"volcanoes": VOLCANOES},
    )
    assert got["volcanoes"] == 3 and got["launches"] == 0
    assert got["launchStatus"].strip() and "shown" not in got["launchStatus"]
    assert "422" in got["militaryStatus"]
    assert got["gdeltStatus"].strip()
    assert "3 shown" in got["volcanoStatus"]
    assert got["violations"] == []


def test_two_thousand_stations_draw_500_and_the_panel_says_showing_500_of_2000():
    got = run_panel(
        """
        const stations = [];
        for (let i = 0; i < 2000; i++) {
          stations.push({id: 's' + i, name: 'S' + i, capacity: 10, lat: 39.0 + i / 1000, lon: -77.0,
                         bikes_available: 1, docks_available: 9});
        }
        const env = makeEnv({routes: {'/api/bikeshare': function () {
          return {body: {stations: stations, count: 2000, city: 'dc', city_label: 'Washington DC',
                         cities: ['dc'], source: 'x'}}; }}});
        L.init(env.ctx);
        toggle(env, 'bikeshare', true);
        await flush();
        const ents = entities(env, 'bikeshare');
        out({count: ents.length, maxIndex: Math.max.apply(null, ents.map(function (e) {
               return parseInt(e.name.slice(1), 10); })),
             status: status(env, 'bikeshare'), badge: byId(env, 'sdalayers-count-bikeshare').textContent,
             call: env.calls[0]});
        """  # noqa: E501
    )
    assert got["count"] == 500 and got["maxIndex"] == 499
    assert "showing 500 of 2,000" in got["status"]
    assert got["badge"].strip() == "500"
    assert parse_qs(urlparse(got["call"]).query) == {"city": ["dc"]}


def test_space_weather_is_shown_in_the_panel_not_on_the_globe_and_text_stays_text():
    hostile = dict(
        SPACE_WEATHER_REAL,
        alerts=[{"product_id": "X", "issued": "2026-10-08 05:01:00",
                 "headline": "<script>alert(1)</script>"}],
    )
    got = run_panel(
        """
        const env = makeEnv({routes: {'/api/space-weather': function () { return {body: DATA}; }}});
        L.init(env.ctx);
        toggle(env, 'spaceweather', true);
        await flush();
        const detail = byId(env, 'sdalayers-detail-spaceweather');
        out({entities: totalEntities(env), sources: env.viewer.dataSources.items.length,
             detail: detail.textContent, calls: env.calls, violations: violations});
        """,  # noqa: E501
        hostile,
    )
    assert got["entities"] == 0
    assert got["calls"] == ["/api/space-weather"]
    assert "Kp" in got["detail"] and "n/a" in got["detail"]
    assert "<script>alert(1)</script>" in got["detail"]
    assert got["violations"] == []


def test_space_weather_values_show_when_the_proxy_has_them():
    got = run_panel(
        """
        const env = makeEnv({routes: {'/api/space-weather': function () { return {body: DATA}; }}});
        L.init(env.ctx);
        toggle(env, 'spaceweather', true);
        await flush();
        out(byId(env, 'sdalayers-detail-spaceweather').textContent);
        """,  # noqa: E501
        SPACE_WEATHER_FULL,
    )
    assert "5.33" in got and "G1" in got and "412" in got


def test_camera_feeds_send_the_view_centre_to_the_proxy():
    got = run_panel(
        """
        const env = makeEnv({center: {lat: 36.9, lon: -76.2}, routes: {
          '/api/radio': function () { return {body: DATA.radio}; },
          '/api/military-sites': function () { return {body: DATA.military}; },
          '/api/bikeshare': function () { return {body: DATA.bikeshare}; }}});
        L.init(env.ctx);
        toggle(env, 'radio', true);
        toggle(env, 'military', true);
        toggle(env, 'bikeshare', true);
        await flush();
        out({calls: env.calls, radio: entities(env, 'radio').length,
             military: entities(env, 'military').length, bikes: entities(env, 'bikeshare').length});
        """,  # noqa: E501
        {"radio": RADIO, "military": MILITARY, "bikeshare": BIKESHARE},
    )
    assert got["radio"] == 2 and got["military"] == 3 and got["bikes"] == 3
    by_path = {urlparse(u).path: parse_qs(urlparse(u).query) for u in got["calls"]}
    assert float(by_path["/api/radio"]["lat"][0]) == pytest.approx(36.9, abs=0.01)
    assert float(by_path["/api/radio"]["lon"][0]) == pytest.approx(-76.2, abs=0.01)
    box = {k: float(v[0]) for k, v in by_path["/api/military-sites"].items()}
    assert box["south"] < 36.9 < box["north"] and box["west"] < -76.2 < box["east"]
    assert by_path["/api/bikeshare"] == {"city": ["dc"]}


def test_enabled_feeds_refresh_on_schedule_but_never_within_five_minutes():
    got = run_panel(
        """
        const env = makeEnv({routes: {
          '/api/launches': function () { return {body: DATA.launches}; },
          '/api/volcanoes': function () { return {body: DATA.volcanoes}; },
          '/api/bikeshare': function () { return {body: DATA.bikeshare}; },
          '/api/space-weather': function () { return {body: DATA.weather}; }}});
        L.init(env.ctx);
        ['launches', 'volcanoes', 'bikeshare', 'spaceweather'].forEach(function (k) { toggle(env, k, true); });
        await flush();
        await advance(14 * 60 * 1000);
        const at14 = env.calls.filter(function (u) { return u.indexOf('/api/launches') === 0; }).length;
        await advance(2 * 60 * 1000);
        const at16 = env.calls.filter(function (u) { return u.indexOf('/api/launches') === 0; }).length;
        await advance(3 * 3600 * 1000);
        const gaps = {};
        env.calls.forEach(function (u, i) {
          const p = u.split('?')[0];
          (gaps[p] = gaps[p] || []).push(env.times[i]);
        });
        const minGap = {};
        Object.keys(gaps).forEach(function (p) {
          let m = Infinity;
          for (let i = 1; i < gaps[p].length; i++) m = Math.min(m, gaps[p][i] - gaps[p][i - 1]);
          minGap[p] = m === Infinity ? null : m;
        });
        out({at14: at14, at16: at16, perPath: Object.keys(gaps).map(function (p) { return [p, gaps[p].length]; }),
             minGap: minGap});
        """,  # noqa: E501
        {"launches": LAUNCHES, "volcanoes": VOLCANOES, "bikeshare": BIKESHARE,
         "weather": SPACE_WEATHER_FULL},
    )
    assert got["at14"] == 1 and got["at16"] == 2
    counts = dict(got["perPath"])
    assert counts["/api/volcanoes"] == 1  # a static catalogue is never polled
    assert counts["/api/launches"] > 2 and counts["/api/space-weather"] > 2
    for path, gap in got["minGap"].items():
        assert gap is None or gap >= 300_000, f"{path} refreshed after {gap} ms"
