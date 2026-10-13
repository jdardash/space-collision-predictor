"""Tests for the globe pass panel (src/sda/templates/static/js/passpanel.js).

The panel is a plain browser script with no bundler, so the tests drive it
through Node: the pure helpers directly, and ``init`` against a tiny fake DOM
(see the second half of this file). Everything is offline and deterministic.
The whole module skips when Node is not installed.

The last tests post the exact bodies the JavaScript builds to the real FastAPI
router, so the script and the route cannot drift apart without a red test.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.demo_tles import DEMO_TLE_TEXT
from sda.routes.passes import router
from sda.tle_store import TLEStore

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src" / "sda" / "templates" / "static" / "js" / "passpanel.js"
)

ISS_NORAD_ID = 25544
DEMO_EPOCH = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)

# Captured from POST /passes and POST /lookangles on a live server for
# NORAD 25544 over (48.15, 11.58, 0.5 km). Two passes, trimmed to what the
# panel reads plus the keys it must ignore.
PASS_ONE = {
    "norad_id": 25544,
    "satellite_name": "ISS (ZARYA)",
    "observer_name": "observer",
    "min_elevation_deg": 10.0,
    "rise_time": "2026-10-08T08:19:42.278131Z",
    "rise_azimuth_deg": 231.921,
    "rise_compass": "SW",
    "culmination_time": "2026-10-08T08:23:04.695163Z",
    "max_elevation_deg": 60.523,
    "culmination_azimuth_deg": 151.259,
    "culmination_compass": "SSE",
    "culmination_range_km": 488.151,
    "set_time": "2026-10-08T08:26:27.278131Z",
    "set_azimuth_deg": 71.162,
    "set_compass": "ENE",
    "duration_s": 405.0,
    "partial": False,
    "sunlit": True,
    "sun_elevation_deg": 25.764,
    "visible": False,
    "visibility_score": 0.6362,
    "samples": [],
}
PASS_TWO = {
    **PASS_ONE,
    "rise_time": "2026-10-08T09:56:40.403131Z",
    "rise_azimuth_deg": 274.033,
    "rise_compass": "W",
    "culmination_time": "2026-10-08T10:00:00.191694Z",
    "max_elevation_deg": 49.039,
    "culmination_azimuth_deg": 350.89,
    "culmination_compass": "N",
    "culmination_range_km": 556.369,
    "set_time": "2026-10-08T10:03:20.715631Z",
    "set_azimuth_deg": 67.837,
    "set_compass": "ENE",
    "duration_s": 400.3,
    "sun_elevation_deg": 34.238,
    "visibility_score": 0.5724,
}
PASSES_RESPONSE = {
    "observer": {
        "name": "observer", "latitude_deg": 48.15, "longitude_deg": 11.58, "altitude_km": 0.5,
    },
    "start": "2026-10-08T07:07:00.090631+00:00",
    "hours": 24.0,
    "min_elevation_deg": 10.0,
    "norad_ids": [25544],
    "count": 2,
    "visible_count": 0,
    "passes": [PASS_ONE, PASS_TWO],
}
LOOKANGLES_RESPONSE = {
    "observer": PASSES_RESPONSE["observer"],
    "time": "2026-10-08T07:07:00.211041+00:00",
    "count": 1,
    "look_angles": [
        {
            "norad_id": 25544,
            "name": "ISS (ZARYA)",
            "time": "2026-10-08T07:07:00.211041Z",
            "azimuth_deg": 51.5451,
            "elevation_deg": -35.6503,
            "range_km": 8135.7266,
            "range_rate_km_s": 5.547759,
            "compass": "NE",
            "above_horizon": False,
        }
    ],
}


def js(expr: str, data: Any = None) -> Any:
    """Evaluate a JS expression with ``P`` (the exported object) and ``D`` (data)."""
    script = (
        "global.window = global;\n"
        f"require({json.dumps(SCRIPT.as_posix())});\n"
        "var P = window.SDAPassPanel;\n"
        "var D = JSON.parse(require('fs').readFileSync(0, 'utf8') || 'null');\n"
        f"process.stdout.write(JSON.stringify({expr}));\n"
    )
    done = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if done.returncode != 0:
        pytest.fail(f"node failed for {expr!r}:\n{done.stderr.strip()[-1500:]}")
    return json.loads(done.stdout) if done.stdout else None


# ---------------------------------------------------------------------------
# File and export contract
# ---------------------------------------------------------------------------


def test_script_parses_under_node_check():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    done = subprocess.run(
        [NODE, "--check", str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr


def test_script_is_ascii_and_free_of_banned_tokens():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.isascii()
    assert chr(0x2014) not in text
    assert "innerHTML" not in text
    assert "'use strict'" in text


def test_exports_init_and_the_pure_helpers():
    names = js("Object.keys(P).sort()")
    for expected in (
        "init", "validateObserver", "buildPassRequest", "buildLookAnglesRequest",
        "passRows", "formatDuration", "lookAnglesLine", "describeError",
        "geolocationMessage", "selectedNoradId", "zoneLabel",
    ):
        assert expected in names
    assert js("typeof P.init") == "function"


# ---------------------------------------------------------------------------
# Observer validation
# ---------------------------------------------------------------------------


def test_validate_observer_accepts_strings_and_converts_metres_to_km():
    out = js("P.validateObserver({lat: '48.15', lon: ' 11.58 ', alt: '520'})")
    assert out == {
        "ok": True,
        "observer": {"latitude_deg": 48.15, "longitude_deg": 11.58, "altitude_km": 0.52},
    }


def test_validate_observer_accepts_numbers_and_blank_altitude_means_ground_level():
    out = js("P.validateObserver({lat: -33.9, lon: 151.2, alt: ''})")
    assert out["ok"] is True
    assert out["observer"] == {
        "latitude_deg": -33.9, "longitude_deg": 151.2, "altitude_km": 0
    }
    assert js("P.validateObserver({lat: 1, lon: 2})")["observer"]["altitude_km"] == 0


# (field, typed value, accepted). Shared with the route tests at the bottom: every
# value the panel accepts must be a 200 from the route and every value it
# rejects must be a 422 there, so the limits cannot drift apart.
RANGE_CASES = [
    ("lat", "90", True),
    ("lat", "-90", True),
    ("lat", "90.0001", False),
    ("lat", "-90.5", False),
    ("lon", "360", True),
    ("lon", "-360", True),
    ("lon", "360.01", False),
    ("lon", "-361", False),
    ("alt", "-500", True),
    ("alt", "-500.5", False),
    ("alt", "100000", True),
    ("alt", "100001", False),
]


@pytest.mark.parametrize(("field", "value", "ok"), RANGE_CASES)
def test_validate_observer_range_limits_match_the_route(field, value, ok):
    base = {"lat": "10", "lon": "20", "alt": "0"}
    base[field] = value
    out = js("P.validateObserver(D)", base)
    assert out["ok"] is ok
    if not ok:
        assert isinstance(out["error"], str)
        assert out["error"].endswith(".")
        assert "\n" not in out["error"]


@pytest.mark.parametrize("bad", ["abc", "12abc", "1e", "NaN", "Infinity", "", "   ", "--5"])
def test_validate_observer_rejects_non_numeric_latitude_and_longitude(bad):
    for field in ("lat", "lon"):
        base = {"lat": "10", "lon": "20", "alt": "0"}
        base[field] = bad
        out = js("P.validateObserver(D)", base)
        assert out["ok"] is False
        word = "latitude" if field == "lat" else "longitude"
        assert word in out["error"].lower()


def test_validate_observer_rejects_non_numeric_altitude_but_not_blank():
    bad = js("P.validateObserver({lat: '1', lon: '2', alt: 'high'})")
    assert bad["ok"] is False
    assert "altitude" in bad["error"].lower()


def test_validate_observer_survives_garbage_input():
    assert js("P.validateObserver(null)")["ok"] is False
    assert js("P.validateObserver(undefined)")["ok"] is False
    assert js("P.validateObserver({lat: {}, lon: [], alt: null})")["ok"] is False


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

OBSERVER = {"latitude_deg": 48.15, "longitude_deg": 11.58, "altitude_km": 0.52}


def test_build_pass_request_defaults_leave_window_to_the_route():
    body = js("P.buildPassRequest(25544, D)", OBSERVER)
    assert body == {"observer": OBSERVER, "norad_id": 25544}


def test_build_pass_request_options_and_id_coercion():
    body = js(
        "P.buildPassRequest('25544', D, {hours: 12, minElevationDeg: 5,"
        " start: '2026-03-22T12:00:00Z'})",
        OBSERVER,
    )
    assert body == {
        "observer": OBSERVER,
        "norad_id": 25544,
        "hours": 12,
        "min_elevation_deg": 5,
        "start": "2026-03-22T12:00:00Z",
    }


@pytest.mark.parametrize("bad_id", ["null", "0", "-5", "'abc'", "1.5", "undefined", "NaN"])
def test_build_requests_refuse_a_bad_norad_id(bad_id):
    assert js(f"P.buildPassRequest({bad_id}, D)", OBSERVER) is None
    assert js(f"P.buildLookAnglesRequest({bad_id}, D)", OBSERVER) is None


def test_build_pass_request_does_not_alias_the_observer():
    out = js(
        "(function () { var b = P.buildPassRequest(25544, D); D.latitude_deg = 0;"
        " return b.observer.latitude_deg; })()",
        OBSERVER,
    )
    assert out == 48.15


def test_build_look_angles_request_shape():
    body = js("P.buildLookAnglesRequest(25544, D)", OBSERVER)
    assert body == {"observer": OBSERVER, "norad_ids": [25544]}
    timed = js("P.buildLookAnglesRequest(25544, D, {time: '2026-03-22T12:00:00Z'})", OBSERVER)
    assert timed["time"] == "2026-03-22T12:00:00Z"


def test_selected_norad_id_reads_the_engine():
    assert js("P.selectedNoradId({selectedId: 25544})") == 25544
    assert js("P.selectedNoradId({selectedId: '25544'})") == 25544
    for empty in ("null", "undefined", "{}", "{selectedId: null}", "{selectedId: 0}",
                  "{selectedId: 'x'}"):
        assert js(f"P.selectedNoradId({empty})") is None


# ---------------------------------------------------------------------------
# Display rows
# ---------------------------------------------------------------------------


def test_pass_rows_from_a_real_response_in_utc():
    rows = js("P.passRows(D, {timeZone: 'UTC'})", PASSES_RESPONSE)
    assert len(rows) == 2
    first = rows[0]
    assert first["rise"] == {
        "date": "2026-10-08", "time": "08:19:42", "compass": "SW", "azimuth": "232",
    }
    assert first["peak"] == {
        "date": "2026-10-08", "time": "08:23:04", "compass": "SSE", "azimuth": "151",
        "elevation": "60.5", "range": "488",
    }
    assert first["set"] == {
        "date": "2026-10-08", "time": "08:26:27", "compass": "ENE", "azimuth": "71",
    }
    assert first["duration"] == "6m 45s"
    assert first["light"] == "Sunlit, bright sky"
    assert first["partial"] is False
    assert rows[1]["peak"]["elevation"] == "49.0"
    assert rows[1]["duration"] == "6m 40s"


def test_pass_rows_show_local_time_for_the_named_zone():
    rows = js("P.passRows(D, {timeZone: 'America/New_York'})", PASSES_RESPONSE)
    assert rows[0]["rise"]["time"] == "04:19:42"  # EDT, UTC-4 on 2026-10-08
    assert rows[0]["rise"]["date"] == "2026-10-08"
    kolkata = js("P.passRows(D, {timeZone: 'Asia/Kolkata'})", PASSES_RESPONSE)
    assert kolkata[0]["rise"]["time"] == "13:49:42"  # UTC+5:30


def test_pass_rows_carry_the_date_across_midnight():
    crossing = copy.deepcopy(PASSES_RESPONSE)
    crossing["passes"] = [{
        **PASS_ONE,
        "rise_time": "2026-10-08T23:58:10Z",
        "culmination_time": "2026-10-09T00:01:00Z",
        "set_time": "2026-10-09T00:03:50Z",
        "duration_s": 340.0,
    }]
    row = js("P.passRows(D, {timeZone: 'UTC'})[0]", crossing)
    assert row["rise"]["date"] == "2026-10-08"
    assert row["peak"]["date"] == "2026-10-09"
    assert row["set"]["date"] == "2026-10-09"
    # 24:00 must never appear; hour 0 is "00".
    assert row["peak"]["time"] == "00:01:00"


def test_pass_rows_tolerate_missing_optional_fields():
    sparse = copy.deepcopy(PASSES_RESPONSE)
    gone = ("sunlit", "visible", "visibility_score", "sun_elevation_deg", "partial",
            "culmination_range_km", "rise_compass", "set_azimuth_deg")
    for key in gone:
        del sparse["passes"][1][key]
    rows = js("P.passRows(D, {timeZone: 'UTC'})", sparse)
    assert len(rows) == 2
    row = rows[1]
    assert row["light"] == ""
    assert row["partial"] is False
    assert row["rise"]["compass"] == "--"
    assert row["set"]["azimuth"] == "--"
    assert row["peak"]["range"] == "--"
    # What is present still renders.
    assert row["rise"]["time"] == "09:56:40"
    assert row["peak"]["elevation"] == "49.0"
    assert row["duration"] == "6m 40s"


def test_pass_rows_tolerate_a_missing_time_and_bad_shapes():
    broken = copy.deepcopy(PASSES_RESPONSE)
    del broken["passes"][0]["set_time"]
    broken["passes"][1]["rise_time"] = "not a date"
    rows = js("P.passRows(D, {timeZone: 'UTC'})", broken)
    assert rows[0]["set"]["time"] == "--"
    assert rows[0]["set"]["date"] == "--"
    assert rows[1]["rise"]["time"] == "--"
    for shape in (None, {}, {"passes": None}, {"passes": []}, [], "text", 5):
        assert js("P.passRows(D, {timeZone: 'UTC'})", shape) == []


@pytest.mark.parametrize(
    ("flags", "light"),
    [
        ({"sunlit": True, "visible": True}, "Visible, sunlit against a dark sky"),
        ({"sunlit": True, "visible": False}, "Sunlit, bright sky"),
        ({"sunlit": False, "visible": False}, "In Earth shadow"),
        ({"sunlit": False}, "In Earth shadow"),
        ({"visible": True}, "Visible, sunlit against a dark sky"),
    ],
)
def test_pass_rows_light_label(flags, light):
    data = copy.deepcopy(PASSES_RESPONSE)
    for key in ("sunlit", "visible"):
        data["passes"][0].pop(key, None)
    data["passes"][0].update(flags)
    assert js("P.passRows(D, {timeZone: 'UTC'})[0].light", data) == light


def test_pass_rows_flag_a_partial_pass():
    data = copy.deepcopy(PASSES_RESPONSE)
    data["passes"][0]["partial"] = True
    rows = js("P.passRows(D, {timeZone: 'UTC'})", data)
    assert rows[0]["partial"] is True
    assert rows[1]["partial"] is False


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (405.0, "6m 45s"),
        (400.3, "6m 40s"),
        (0, "0s"),
        (7, "7s"),
        (59, "59s"),
        (59.6, "1m 00s"),
        (60, "1m 00s"),
        (3599.4, "59m 59s"),
        (3600, "1h 00m"),
        (3725, "1h 02m"),
        (86400, "24h 00m"),
        (-5, "--"),
        (None, "--"),
        ("abc", "--"),
    ],
)
def test_format_duration(seconds, text):
    assert js("P.formatDuration(D)", seconds) == text


def test_format_duration_rejects_non_finite_numbers():
    assert js("P.formatDuration(NaN)") == "--"
    assert js("P.formatDuration(Infinity)") == "--"


def test_zone_label_names_the_zone_and_its_abbreviation():
    assert js("P.zoneLabel('America/New_York', new Date('2026-10-08T12:00:00Z'))") == (
        "America/New_York (EDT)"
    )
    assert js("P.zoneLabel('America/New_York', new Date('2026-01-08T12:00:00Z'))") == (
        "America/New_York (EST)"
    )
    assert js("P.zoneLabel('UTC', new Date('2026-10-08T12:00:00Z'))") == "UTC"
    # With no argument it reads the browser zone and never throws.
    assert isinstance(js("P.zoneLabel()"), str)


# ---------------------------------------------------------------------------
# Look angle line
# ---------------------------------------------------------------------------


def test_look_angles_line_below_the_horizon():
    line = js("P.lookAnglesLine(D, 25544)", LOOKANGLES_RESPONSE)
    assert line == "az 51.5 deg NE, el -35.7 deg (below horizon), range 8136 km"


def test_look_angles_line_above_the_horizon():
    up = copy.deepcopy(LOOKANGLES_RESPONSE)
    entry = up["look_angles"][0]
    entry.update(azimuth_deg=200.04, elevation_deg=33.25, range_km=712.4,
                 compass="SSW", above_horizon=True)
    line = js("P.lookAnglesLine(D, 25544)", up)
    assert line == "az 200.0 deg SSW, el 33.3 deg (above horizon), range 712 km"


def test_look_angles_line_is_null_when_there_is_nothing_to_show():
    errored = copy.deepcopy(LOOKANGLES_RESPONSE)
    errored["look_angles"] = [{"norad_id": 25544, "name": "ISS", "error": "decayed"}]
    assert js("P.lookAnglesLine(D, 25544)", errored) is None
    assert js("P.lookAnglesLine(D, 99999)", LOOKANGLES_RESPONSE) is None
    for shape in (None, {}, {"look_angles": []}, {"look_angles": None}, [], "x"):
        assert js("P.lookAnglesLine(D, 25544)", shape) is None


# ---------------------------------------------------------------------------
# Error sentences
# ---------------------------------------------------------------------------


def test_describe_error_for_a_422_names_the_offending_field():
    body = {
        "detail": [{
            "type": "less_than_equal",
            "loc": ["body", "observer", "latitude_deg"],
            "msg": "Input should be less than or equal to 90",
            "input": 91,
        }]
    }
    assert js("P.describeError(422, D)", body) == (
        "The server rejected observer.latitude_deg: Input should be less than or equal to 90."
    )


def test_describe_error_for_404_and_400_uses_the_server_detail():
    assert js("P.describeError(404, D)", {"detail": "Satellite 99 not found"}) == (
        "Satellite 99 not found."
    )
    assert js("P.describeError(400, D)", {"detail": "Provide norad_id or norad_ids"}) == (
        "Provide norad_id or norad_ids."
    )
    assert js("P.describeError(404, D)", {"detail": "Already has a stop."}) == (
        "Already has a stop."
    )


def test_describe_error_for_network_rate_limit_and_server_faults():
    assert js("P.describeError(0, null)") == "Could not reach the server."
    assert js("P.describeError(429, D)", {"detail": "Rate limit exceeded",
                                         "retry_after_seconds": 12}) == (
        "Too many requests, try again in 12 s."
    )
    assert js("P.describeError(429, null)") == "Too many requests, try again shortly."
    assert js("P.describeError(500, null)") == "The server failed to answer (HTTP 500)."
    assert js("P.describeError(503, D)", {"unexpected": "shape"}) == (
        "The server failed to answer (HTTP 503)."
    )


def test_describe_error_always_returns_one_short_line():
    long_detail = "x" * 900 + "\nsecond line"
    out = js("P.describeError(400, D)", {"detail": long_detail})
    assert "\n" not in out
    assert len(out) <= 160
    assert out.endswith(".")
    odd = js("P.describeError(422, D)", {"detail": [{"msg": "bad"}]})
    assert odd == "The server rejected the request: bad."
    assert js("P.describeError(422, D)", {"detail": "plain words"}) == "plain words."
    assert js("P.describeError(undefined, undefined)") == "Could not reach the server."


@pytest.mark.parametrize(
    ("code", "fragment"),
    [(1, "denied"), (2, "unavailable"), (3, "timed out"), (99, "unavailable")],
)
def test_geolocation_message(code, fragment):
    out = js(f"P.geolocationMessage({code})")
    assert fragment in out.lower()
    assert out.endswith(".")


# ---------------------------------------------------------------------------
# init() against a tiny fake DOM
# ---------------------------------------------------------------------------
#
# The fakes are just big enough for the panel: elements with children, text,
# listeners and a dispatch that hands back what each listener returned (the
# click handlers return their promise, so a test can await the whole request
# chain). fetch, Cesium, the viewer and geolocation are scripted per test.

DOM_PRELUDE = r"""
global.window = global;
var D = JSON.parse(require('fs').readFileSync(0, 'utf8') || 'null') || {};

function El(tag) {
  var self = this;
  this.tagName = String(tag).toUpperCase();
  this.children = [];
  this.listeners = {};
  this.style = {};
  this.attrs = {};
  this.className = '';
  this.value = '';
  this.disabled = false;
  this._text = '';
  Object.defineProperty(this, 'textContent', {
    get: function () {
      var parts = [];
      if (self._text) parts.push(self._text);
      self.children.forEach(function (c) { parts.push(c.textContent); });
      return parts.join(' ');
    },
    set: function (v) { self._text = String(v); self.children = []; }
  });
}
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.focus = function () {};
El.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};
El.prototype.dispatch = function (type, ev) {
  var e = ev || {};
  e.stopped = 0;
  e.stopPropagation = function () { e.stopped += 1; };
  e.preventDefault = function () {};
  return (this.listeners[type] || []).map(function (fn) { return fn(e); });
};

var head = new El('head');
global.document = {
  head: head,
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    return head.children.filter(function (c) { return c.id === id; })[0] || null;
  }
};

function findAll(root, pred, out) {
  out = out || [];
  if (pred(root)) out.push(root);
  root.children.forEach(function (c) { findAll(c, pred, out); });
  return out;
}
function byName(root, name) {
  return findAll(root, function (e) { return e.name === name; })[0];
}
function button(root, label) {
  return findAll(root, function (e) {
    return e.tagName === 'BUTTON' && e.textContent.indexOf(label) >= 0;
  })[0];
}
function inputs(root) {
  return findAll(root, function (e) { return e.tagName === 'INPUT'; });
}
function buttons(root) {
  return findAll(root, function (e) { return e.tagName === 'BUTTON'; });
}
function settle() { return new Promise(function (r) { setTimeout(r, 0); }); }

var intel = [], fetchCalls = [], added = [], removed = [];
var responses = D.responses || {};
global.fetch = function (url, init) {
  fetchCalls.push({
    url: url,
    method: init && init.method,
    contentType: init && init.headers && init.headers['Content-Type'],
    body: init && init.body ? JSON.parse(init.body) : null
  });
  var path = url.replace(/^.*(\/passes|\/lookangles)$/, '$1');
  var r = responses[path] || { status: 200, body: {} };
  if (r.reject) return Promise.reject(new TypeError('Failed to fetch'));
  return Promise.resolve({
    ok: r.status >= 200 && r.status < 300,
    status: r.status,
    json: function () {
      return r.nobody ? Promise.reject(new Error('no body')) : Promise.resolve(r.body);
    }
  });
};

var world = { picked: { lat: Math.PI / 6, lon: -Math.PI / 4 }, pickArgs: null };
global.Cesium = {
  Cartesian3: { fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h }; } },
  Cartesian2: function (x, y) { this.x = x; this.y = y; },
  Color: { fromCssColorString: function (s) { return { css: s }; } },
  Cartographic: {
    fromCartesian: function (c) { return { latitude: c.lat, longitude: c.lon, height: 0 }; }
  }
};
var viewer = {
  camera: {
    positionCartographic: { latitude: Math.PI / 3, longitude: Math.PI / 2, height: 2e7 },
    pickEllipsoid: function (pos) { world.pickArgs = pos; return world.picked; }
  },
  scene: { canvas: { clientWidth: 800, clientHeight: 600 }, globe: { ellipsoid: {} } },
  entities: {
    add: function (def) { var e = { def: def }; added.push(e); return e; },
    remove: function (e) { removed.push(e); return true; }
  }
};
function liveMarkers() {
  return added.filter(function (e) { return removed.indexOf(e) < 0; });
}

var geo = { calls: 0, mode: 'ok' };
Object.defineProperty(global, 'navigator', {
  configurable: true, writable: true,
  value: {
    geolocation: {
      getCurrentPosition: function (ok, fail) {
        geo.calls += 1;
        if (geo.mode === 'denied') return fail({ code: 1 });
        return ok({ coords: { latitude: 48.1372, longitude: 11.5755, altitude: 519.6 } });
      }
    }
  }
});

function makeCtx(over) {
  var ctx = {
    viewer: viewer,
    satEngine: {
      byNoradId: {
        25544: { noradId: 25544, name: 'ISS (ZARYA)' },
        99999: { noradId: 99999, name: 'OTHER SAT' }
      },
      selectedId: 25544
    },
    selectSatellite: function () {},
    addIntelItem: function (t) { intel.push(t); },
    api: '',
    isStatic: false,
    mount: new El('div')
  };
  for (var k in over) ctx[k] = over[k];
  return ctx;
}
function fill(ctx, lat, lon, alt) {
  byName(ctx.mount, 'latitude').value = lat;
  byName(ctx.mount, 'longitude').value = lon;
  byName(ctx.mount, 'altitude').value = alt;
}
function runPasses(ctx) {
  return Promise.all(button(ctx.mount, 'PASSES FOR SELECTED').dispatch('click')).then(settle);
}
"""

# The panel prints times in the browser's zone; pin it so the assertions are exact.
NODE_ENV_TZ = "America/New_York"


def panel(scenario: str, data: Any = None) -> Any:
    """Run a scenario (an async function body) against the fake DOM, return its result."""
    script = (
        DOM_PRELUDE
        + f"require({json.dumps(SCRIPT.as_posix())});\n"
        + "var P = window.SDAPassPanel;\n"
        + "(async function () {\n"
        + scenario
        + "\n})().then(function (r) { process.stdout.write(JSON.stringify(r)); },"
        + " function (e) { console.error((e && e.stack) || e); process.exit(1); });\n"
    )
    env = {**os.environ, "TZ": NODE_ENV_TZ}
    done = subprocess.run(
        [NODE, "-e", script],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if done.returncode != 0:
        pytest.fail(f"node scenario failed:\n{done.stderr.strip()[-2000:]}")
    return json.loads(done.stdout)


OK_RESPONSES = {
    "/passes": {"status": 200, "body": PASSES_RESPONSE},
    "/lookangles": {"status": 200, "body": LOOKANGLES_RESPONSE},
}


def test_static_demo_shows_one_note_and_does_nothing_else():
    out = panel(
        """
        var ctx = makeCtx({ isStatic: true });
        P.init(ctx);
        await settle();
        return {
          text: ctx.mount.textContent, inputs: inputs(ctx.mount).length,
          buttons: buttons(ctx.mount).length, fetches: fetchCalls.length,
          markers: added.length, geo: geo.calls
        };
        """
    )
    assert "live server" in out["text"].lower()
    assert out["inputs"] == out["buttons"] == out["fetches"] == out["markers"] == out["geo"] == 0


def test_init_never_throws_on_a_bad_context():
    out = panel(
        """
        var results = [];
        function attempt(label, fn) {
          try { fn(); results.push([label, 'ok']); }
          catch (e) { results.push([label, 'threw: ' + e.message]); }
        }
        attempt('null', function () { P.init(null); });
        attempt('undefined', function () { P.init(); });
        attempt('empty', function () { P.init({}); });
        attempt('no viewer', function () { P.init(makeCtx({ viewer: null })); });
        attempt('no engine', function () { P.init(makeCtx({ satEngine: null })); });
        attempt('mount breaks', function () {
          var m = new El('div');
          m.appendChild = function () { throw new Error('boom'); };
          P.init(makeCtx({ mount: m }));
        });
        return results;
        """
    )
    assert all(state == "ok" for _, state in out), out


def test_panel_uses_the_page_markup_and_never_asks_for_location_on_load():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        var panelEl = ctx.mount.children[0];
        var h3 = findAll(ctx.mount, function (e) { return e.tagName === 'H3'; })[0];
        var bodies = findAll(ctx.mount, function (e) { return e.className === 'panel-body'; });
        return {
          panelClass: panelEl.className, hasH3: !!h3, bodies: bodies.length,
          buttonClasses: buttons(ctx.mount).map(function (b) { return b.className; }),
          names: inputs(ctx.mount).map(function (i) { return i.name; }),
          geo: geo.calls, fetches: fetchCalls.length, markers: added.length,
          style: head.children.length ? head.children[0].tagName : null
        };
        """
    )
    assert out["panelClass"] == "panel"
    assert out["hasH3"] is True
    assert out["bodies"] == 1
    assert all(cls.split()[0] == "mode-btn" for cls in out["buttonClasses"])
    assert out["names"] == ["latitude", "longitude", "altitude"]
    assert out["geo"] == out["fetches"] == out["markers"] == 0
    assert out["style"] == "STYLE"


def test_passes_flow_calls_the_server_and_lists_local_times():
    out = panel(
        """
        var ctx = makeCtx({ api: '/base' });
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '520');
        byName(ctx.mount, 'latitude').dispatch('change');
        await runPasses(ctx);
        return {
          calls: fetchCalls, text: ctx.mount.textContent, intel: intel,
          live: liveMarkers().map(function (e) { return e.def; })
        };
        """,
        {"responses": OK_RESPONSES},
    )
    urls = sorted(call["url"] for call in out["calls"])
    assert urls == ["/base/lookangles", "/base/passes"]
    by_url = {call["url"]: call for call in out["calls"]}
    assert by_url["/base/passes"]["method"] == "POST"
    assert by_url["/base/passes"]["contentType"] == "application/json"
    assert by_url["/base/passes"]["body"] == {"observer": OBSERVER, "norad_id": 25544}
    assert by_url["/base/lookangles"]["body"] == {"observer": OBSERVER, "norad_ids": [25544]}

    text = out["text"]
    assert "America/New_York (EDT)" in text
    assert "04:19:42" in text and "04:23:04" in text and "04:26:27" in text
    assert "60.5" in text and "SSE" in text and "6m 45s" in text
    assert "Sunlit, bright sky" in text
    assert "ISS (ZARYA)" in text
    assert "2 passes" in text and "24 h" in text and "10 deg" in text
    assert "az 51.5 deg NE, el -35.7 deg (below horizon), range 8136 km" in text

    assert len(out["intel"]) == 1
    assert "ISS (ZARYA)" in out["intel"][0]
    assert len(out["live"]) == 1
    assert out["live"][0]["position"] == {"lon": 11.58, "lat": 48.15, "h": 520}


def test_no_selection_says_so_and_sends_nothing():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '0');
        ctx.satEngine.selectedId = null;
        await runPasses(ctx);
        return { text: ctx.mount.textContent, fetches: fetchCalls.length };
        """
    )
    assert "select a satellite" in out["text"].lower()
    assert out["fetches"] == 0


def test_selection_is_read_at_click_time():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '0');
        ctx.satEngine.selectedId = 99999;
        await runPasses(ctx);
        return fetchCalls.map(function (c) { return c.body.norad_id || c.body.norad_ids[0]; });
        """,
        {"responses": OK_RESPONSES},
    )
    assert out == [99999, 99999]


def test_invalid_observer_is_stopped_before_any_request():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '999', '11.58', '0');
        await runPasses(ctx);
        return {
          text: ctx.mount.textContent, fetches: fetchCalls.length, live: liveMarkers().length
        };
        """
    )
    assert "Latitude must be between" in out["text"]
    assert out["fetches"] == 0
    assert out["live"] == 0


def test_observer_marker_is_replaced_then_removed_as_the_inputs_change():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        var lat = byName(ctx.mount, 'latitude');
        fill(ctx, '10', '20', '0');
        lat.dispatch('change');
        var first = liveMarkers().map(function (e) { return e.def.position; });
        lat.value = '30';
        lat.dispatch('change');
        var second = liveMarkers().map(function (e) { return e.def.position; });
        lat.value = 'abc';
        lat.dispatch('change');
        var third = liveMarkers().length;
        return { first: first, second: second, third: third, fetches: fetchCalls.length };
        """
    )
    assert out["first"] == [{"lon": 20, "lat": 10, "h": 0}]
    assert out["second"] == [{"lon": 20, "lat": 30, "h": 0}]
    assert out["third"] == 0
    assert out["fetches"] == 0


def test_failures_become_one_sentence_and_clear_the_old_list():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '520');
        var seen = {};
        await runPasses(ctx);
        seen.ok = ctx.mount.textContent;
        for (var name in D.cases) {
          responses['/passes'] = D.cases[name];
          await runPasses(ctx);
          seen[name] = ctx.mount.textContent;
        }
        return seen;
        """,
        {
            "responses": OK_RESPONSES,
            "cases": {
                "missing": {"status": 404, "body": {"detail": "Satellite 25544 not found"}},
                "network": {"reject": True},
                "invalid": {"status": 422, "body": {"detail": [{
                    "loc": ["body", "observer", "longitude_deg"], "msg": "Input should be bad",
                }]}},
                "html500": {"status": 500, "nobody": True},
            },
        },
    )
    assert "04:19:42" in out["ok"]
    assert "Satellite 25544 not found." in out["missing"]
    assert "Could not reach the server." in out["network"]
    assert "The server rejected observer.longitude_deg: Input should be bad." in out["invalid"]
    assert "The server failed to answer (HTTP 500)." in out["html500"]
    for name in ("missing", "network", "invalid", "html500"):
        assert "04:19:42" not in out[name], f"{name} left the previous list on screen"


def test_a_lookangles_failure_does_not_hide_the_pass_list():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '520');
        var seen = [];
        responses['/lookangles'] = { status: 404, body: { detail: 'Satellite 25544 not found' } };
        await runPasses(ctx);
        seen.push(ctx.mount.textContent);
        responses['/lookangles'] = { reject: true };
        await runPasses(ctx);
        seen.push(ctx.mount.textContent);
        return seen;
        """,
        {"responses": OK_RESPONSES},
    )
    for text in out:
        assert "04:19:42" in text
        assert "NOW" not in text
        assert "range 8136 km" not in text


def test_an_empty_pass_list_says_so():
    empty = {**PASSES_RESPONSE, "count": 0, "passes": []}
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '520');
        await runPasses(ctx);
        return ctx.mount.textContent;
        """,
        {"responses": {**OK_RESPONSES, "/passes": {"status": 200, "body": empty}}},
    )
    assert "No passes" in out
    assert "10 deg" in out and "24 h" in out


def test_a_late_answer_for_an_earlier_click_is_ignored():
    second = copy.deepcopy(PASSES_RESPONSE)
    second["passes"] = [{**PASS_ONE, "satellite_name": "OTHER SAT", "norad_id": 99999}]
    second["count"] = 1
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '0');
        var pending = [];
        global.fetch = function (url, init) {
          return new Promise(function (resolve) {
            pending.push({ url: url, id: JSON.parse(init.body).norad_id, resolve: resolve });
          });
        };
        function answer(p, body) {
          p.resolve({
            ok: true, status: 200, json: function () { return Promise.resolve(body); }
          });
        }
        var a = runPasses(ctx);
        ctx.satEngine.selectedId = 99999;
        var b = runPasses(ctx);
        var passes = pending.filter(function (p) { return /passes$/.test(p.url); });
        var looks = pending.filter(function (p) { return /lookangles$/.test(p.url); });
        looks.forEach(function (p) { answer(p, { look_angles: [] }); });
        answer(passes[1], D.second);
        await settle();
        answer(passes[0], D.first);
        await settle();
        await Promise.all([a, b]);
        return { text: ctx.mount.textContent, n: passes.length };
        """,
        {"first": PASSES_RESPONSE, "second": second},
    )
    assert out["n"] == 2
    assert "OTHER SAT" in out["text"]
    assert "2 passes" not in out["text"]


def test_view_centre_fill_uses_the_point_under_the_screen_centre_then_the_camera():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        var seen = {};
        button(ctx.mount, 'VIEW').dispatch('click');
        seen.first = [byName(ctx.mount, 'latitude').value, byName(ctx.mount, 'longitude').value,
                      byName(ctx.mount, 'altitude').value];
        seen.pick = { x: world.pickArgs.x, y: world.pickArgs.y };
        seen.marker = liveMarkers().map(function (e) { return e.def.position; });
        world.picked = undefined;
        button(ctx.mount, 'VIEW').dispatch('click');
        seen.second = [byName(ctx.mount, 'latitude').value, byName(ctx.mount, 'longitude').value];
        seen.markers = liveMarkers().length;
        seen.fetches = fetchCalls.length;
        return seen;
        """
    )
    assert out["first"] == ["30", "-45", "0"]
    assert out["pick"] == {"x": 400, "y": 300}
    assert out["marker"] == [{"lon": -45, "lat": 30, "h": 0}]
    assert out["second"] == ["60", "90"]
    assert out["markers"] == 1
    assert out["fetches"] == 0


def test_geolocation_runs_only_on_click_and_fills_the_fields():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        var seen = { before: geo.calls };
        button(ctx.mount, 'MY LOCATION').dispatch('click');
        seen.after = geo.calls;
        seen.fields = [byName(ctx.mount, 'latitude').value, byName(ctx.mount, 'longitude').value,
                       byName(ctx.mount, 'altitude').value];
        seen.markers = liveMarkers().length;
        return seen;
        """
    )
    assert out["before"] == 0
    assert out["after"] == 1
    assert out["fields"] == ["48.1372", "11.5755", "520"]
    assert out["markers"] == 1


def test_geolocation_refusal_and_absence_become_one_sentence():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '1', '2', '3');
        var seen = {};
        geo.mode = 'denied';
        button(ctx.mount, 'MY LOCATION').dispatch('click');
        seen.denied = ctx.mount.textContent;
        seen.fields = byName(ctx.mount, 'latitude').value;
        global.navigator = {};
        button(ctx.mount, 'MY LOCATION').dispatch('click');
        seen.absent = ctx.mount.textContent;
        return seen;
        """
    )
    assert "Location permission was denied." in out["denied"]
    assert out["fields"] == "1"
    assert "Your location is unavailable." in out["absent"]


def test_keys_typed_in_the_inputs_stay_inside_the_inputs():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        var result = [];
        inputs(ctx.mount).forEach(function (input) {
          ['keydown', 'keyup', 'keypress'].forEach(function (type) {
            var ev = { key: 'a' };
            input.dispatch(type, ev);
            result.push(ev.stopped);
          });
        });
        return result;
        """
    )
    assert len(out) == 9
    assert all(count >= 1 for count in out)


def test_enter_in_an_input_runs_the_query():
    out = panel(
        """
        var ctx = makeCtx();
        P.init(ctx);
        fill(ctx, '48.15', '11.58', '0');
        var done = byName(ctx.mount, 'altitude').dispatch('keydown', { key: 'Enter' });
        await Promise.all(done);
        await settle();
        return fetchCalls.map(function (c) { return c.url; }).sort();
        """,
        {"responses": OK_RESPONSES},
    )
    assert out == ["/lookangles", "/passes"]


# ---------------------------------------------------------------------------
# The bodies the script builds against the real FastAPI route
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A minimal app carrying only the passes router, backed by the demo catalog."""
    import sda.api

    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    monkeypatch.setattr(sda.api, "store", store)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _pass_body(observer: dict[str, float], **opts: Any) -> dict[str, Any]:
    body = js(
        f"P.buildPassRequest({ISS_NORAD_ID}, D.observer, D.opts)",
        {"observer": observer, "opts": opts},
    )
    assert body is not None
    return body


def test_route_accepts_the_pass_body_and_the_rows_read_the_real_answer(client):
    body = _pass_body(OBSERVER, start=DEMO_EPOCH.isoformat(), hours=24)
    resp = client.post("/passes", json=body)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["count"] >= 1, "the fixed window should hold at least one pass"

    rows = js("P.passRows(D, {timeZone: 'UTC'})", payload)
    assert len(rows) == payload["count"]
    for row in rows:
        for leg in ("rise", "peak", "set"):
            assert all(value != "--" for value in row[leg].values()), (leg, row)
        assert row["duration"] != "--"
        assert row["light"] != ""
    # The echo the panel prints in its header is really in the response.
    assert isinstance(payload["hours"], int | float)
    assert isinstance(payload["min_elevation_deg"], int | float)


def test_route_accepts_the_default_pass_body_with_no_window(client):
    body = _pass_body(OBSERVER)
    assert set(body) == {"observer", "norad_id"}
    resp = client.post("/passes", json=body)
    assert resp.status_code == 200, resp.text
    assert {"hours", "min_elevation_deg", "count", "passes"} <= set(resp.json())


def test_route_accepts_the_lookangles_body_and_the_line_reads_the_answer(client):
    body = js(
        f"P.buildLookAnglesRequest({ISS_NORAD_ID}, D, {{time: '{DEMO_EPOCH.isoformat()}'}})",
        OBSERVER,
    )
    resp = client.post("/lookangles", json=body)
    assert resp.status_code == 200, resp.text
    line = js(f"P.lookAnglesLine(D, {ISS_NORAD_ID})", resp.json())
    assert isinstance(line, str)
    assert line.startswith("az ")
    assert "horizon), range" in line


@pytest.mark.parametrize(("field", "value", "accepted"), RANGE_CASES)
def test_observer_limits_agree_between_the_script_and_the_route(client, field, value, accepted):
    typed = {"lat": "10", "lon": "20", "alt": "0"}
    typed[field] = value
    checked = js("P.validateObserver(D)", typed)
    assert checked["ok"] is accepted
    if accepted:
        body = _pass_body(checked["observer"], start=DEMO_EPOCH.isoformat(), hours=1)
        assert client.post("/passes", json=body).status_code == 200
    else:
        # Send the out-of-range value anyway: the route must refuse it too.
        observer = {
            "latitude_deg": float(typed["lat"]),
            "longitude_deg": float(typed["lon"]),
            "altitude_km": float(typed["alt"]) / 1000.0,
        }
        body = {"observer": observer, "norad_id": ISS_NORAD_ID, "hours": 1}
        assert client.post("/passes", json=body).status_code == 422


def test_error_sentences_match_what_the_route_really_sends(client):
    missing = client.post("/passes", json=_pass_body(OBSERVER) | {"norad_id": 99999999})
    assert missing.status_code == 404
    assert js("P.describeError(404, D)", missing.json()) == "Satellite 99999999 not found."

    bad_lat = {**OBSERVER, "latitude_deg": 91.0}
    invalid = client.post("/passes", json={"observer": bad_lat, "norad_id": ISS_NORAD_ID})
    assert invalid.status_code == 422
    sentence = js("P.describeError(422, D)", invalid.json())
    assert sentence.startswith("The server rejected observer.latitude_deg: ")
    assert sentence.endswith(".")

    empty = client.post("/passes", json={"observer": OBSERVER})
    assert empty.status_code == 400
    assert js("P.describeError(400, D)", empty.json()) == "Provide norad_id or norad_ids."

    unknown = client.post("/lookangles", json={"observer": OBSERVER, "norad_ids": [99999999]})
    assert unknown.status_code == 404
    assert js("P.describeError(404, D)", unknown.json()) == "Satellite 99999999 not found."
