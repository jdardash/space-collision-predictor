"""Tests for the globe conjunction actions panel (static/js/conjactions.js).

The panel adds three server analyses to the conjunction detail: a Pc comparison
(POST /pc/compare), avoidance options (POST /maneuver) and a hypothetical breakup preview
(POST /breakup/simulate) whose fragments are drawn on the globe. The module is plain
browser JavaScript with no bundler, so these tests run it under Node's `vm` against a
hand-written DOM, fetch, timer, Cesium and satellite.js stub (see FAKE_DOM), because jsdom
is not a dependency of this repo. They are offline and deterministic, and skip cleanly when
Node is not installed.

The fixtures below are captured responses from the real routes, not invented ones: the
one non-formation conjunction in the bundled demo catalog (SWIFT against STARLINK-36348,
TCA 2026-03-22T12:00:11Z), run through the globe-data builder, /pc/compare, /maneuver and
/breakup/simulate with the request bodies the panel itself builds. The breakup response is
trimmed to its scalars, two Gabbard points and the first three fragment element sets.

All scenarios run in one node process (one fresh `vm` context each, so no state leaks
between them); the module-scoped `ran` fixture holds their results and each test reads the
ones it needs. A scenario that throws fails only the tests that read it. The last section
posts the request bodies the JS built to the real routes through a TestClient that is not
used as a context manager, so the app lifespan (and with it any CelesTrak fetch) never runs.
"""

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.demo_tles import DEMO_TLE_TEXT
from sda.tle_store import TLEStore

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "src" / "sda" / "templates" / "static" / "js" / "conjactions.js"
PAGE = ROOT / "src" / "sda" / "templates" / "worldview.html"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Mirrors the module's own limits on purpose, so a change to either has to be made twice.
MAX_FRAGMENTS = 300
CLOUD_INTERVAL_MS = 500
REQUEST_TIMEOUT_MS = 45000
MANEUVER_TARGET_KM = 10

# --- captured fixtures ---

EVENT_RAW = json.loads(
    """
{
 "primary_norad_id": 28485,
 "primary_name": "SWIFT",
 "secondary_norad_id": 67851,
 "secondary_name": "STARLINK-36348",
 "tca": "2026-03-22T12:00:11+00:00",
 "risk": "LOW",
 "risk_color": "#66ff66",
 "miss_distance_km": 5.7923,
 "relative_velocity_km_s": 4.4948,
 "primary_position": {
  "lat": -8.9063,
  "lon": -25.8608,
  "alt_km": 383.2
 },
 "secondary_position": {
  "lat": -8.8764,
  "lon": -25.8897,
  "alt_km": 386.3
 },
 "bplane": {
  "miss_x_km": 5.497067,
  "miss_y_km": 1.603339,
  "sigma_combined_km": 0.181435
 },
 "covariance": {
  "source": "tle-age-estimate",
  "model": "sda.probability.sigma_from_tle_age (Vallado 2013 ch. 9)",
  "isotropic": true,
  "primary_sigma_km": 0.151378,
  "secondary_sigma_km": 0.100016,
  "combined_sigma_km": 0.181435,
  "primary_tle_age_hours": 16.221,
  "secondary_tle_age_hours": 8.003
 },
 "pc": 1.08864129737846e-218
}
"""
)


PLAIN_EVENT = json.loads(
    """
{
 "primary": 28485,
 "secondary": 67851,
 "primary_name": "SWIFT",
 "secondary_name": "STARLINK-36348",
 "tca": "2026-03-22T12:00:11Z",
 "miss_distance_km": 5.7923,
 "relative_velocity_km_s": 4.4948,
 "risk": "LOW",
 "collision_probability": {
  "probability": 1.08864129737846e-218,
  "miss_distance_km": 5.7922829621581435,
  "combined_sigma_km": 0.18143464308195037,
  "mahalanobis_distance": 31.56023213315119,
  "hard_body_radius_km": 0.02
 }
}
"""
)


COMPARE_OK = json.loads(
    """
{
 "probabilities": {
  "foster": 1.0881642867823453e-218,
  "chan": 1.0881819538358315e-218,
  "alfano": 1.0881819538358117e-218,
  "monte_carlo": 0.0
 },
 "method_spread": 1.0881819538358315e-218,
 "miss_distance_km": 5.726119240062156,
 "relative_speed_km_s": 4.4948,
 "hard_body_radius_km": 0.02,
 "encounter_plane": {
  "miss_2d_km": [
   1.603339,
   -5.497067
  ],
  "covariance_km2": [
   [
    0.03291849914,
    0.0
   ],
   [
    0.0,
    0.03291849914
   ]
  ],
  "aspect_ratio": 1.0,
  "covariance_remediated": false
 },
 "usage": {
  "ok": false,
  "codes": [
   "PC_DILUTION"
  ],
  "warnings": [
   {
    "code": "PC_DILUTION",
    "message": "Pc would rise if the covariance grew; this is the dilution region",
    "values": {
     "pc": 1.0881819538358117e-218,
     "max_pc": 4.487916152976224e-06,
     "scale_at_max": 498.02151208553397
    }
   }
  ],
  "encounter_duration_s": 0.24219261213308033,
  "condition_number": 1.0
 },
 "assumptions": []
}
"""
)


COMPARE_STRESS = json.loads(
    """
{
 "probabilities": {
  "foster": 0.0,
  "chan": 0.0,
  "alfano": 0.0,
  "monte_carlo": 0.0
 },
 "method_spread": 0.0,
 "miss_distance_km": 0.6,
 "relative_speed_km_s": 0.05,
 "hard_body_radius_km": 0.02,
 "encounter_plane": {
  "miss_2d_km": [
   0.0,
   -0.6
  ],
  "covariance_km2": [
   [
    4.01,
    0.0
   ],
   [
    0.0,
    2e-06
   ]
  ],
  "aspect_ratio": 1415.9802258506297,
  "covariance_remediated": false
 },
 "usage": {
  "ok": false,
  "codes": [
   "LOW_RELATIVE_VELOCITY",
   "EXTENDED_ENCOUNTER",
   "EXTREME_ASPECT_RATIO",
   "CHAN_ASPECT_RATIO",
   "HBR_COMPARABLE_TO_MISS",
   "PC_DILUTION"
  ],
  "warnings": [
   {
    "code": "LOW_RELATIVE_VELOCITY",
    "message": "relative speed is too low for the rectilinear encounter assumption",
    "values": {
     "relative_speed_km_s": 0.05,
     "limit_km_s": 0.1
    }
   },
   {
    "code": "EXTENDED_ENCOUNTER",
    "message": "encounter duration is long compared with the orbital period",
    "values": {
     "encounter_duration_s": 240.2998127340094,
     "duration_fraction_of_period": 0.044499965321112854,
     "limit": 0.02
    }
   },
   {
    "code": "EXTREME_ASPECT_RATIO",
    "message": "encounter-plane covariance is extremely elongated",
    "values": {
     "aspect_ratio": 1415.9802258506297,
     "limit": 1000.0
    }
   },
   {
    "code": "CHAN_ASPECT_RATIO",
    "message": "@CHAN_MESSAGE@",
    "values": {
     "aspect_ratio": 1415.9802258506297,
     "aspect_ratio_limit": 10.0,
     "chan_error_estimate": 4496797.0248168,
     "chan_error_estimate_limit": 0.05
    }
   },
   {
    "code": "HBR_COMPARABLE_TO_MISS",
    "message": "hard-body radius is not small compared with the miss distance",
    "values": {
     "hbr_over_miss_distance": 0.03333333333333333,
     "limit": 0.01
    }
   },
   {
    "code": "PC_DILUTION",
    "message": "Pc would rise if the covariance grew; this is the dilution region",
    "values": {
     "pc": 0.0,
     "max_pc": 9.089902057124979e-10,
     "scale_at_max": 10000.000000000027
    }
   }
  ],
  "encounter_duration_s": 240.2998127340094,
  "condition_number": 2005000.0
 },
 "assumptions": []
}
"""
)


MANEUVER_NONE = json.loads(
    """
{
 "primary": 28485,
 "secondary": 67851,
 "current_miss_km": 5.7923,
 "target_miss_km": 5.0,
 "warning": "Miss distance already exceeds target; no maneuver needed.",
 "recommended": null,
 "options": []
}
"""
)


MANEUVER_TARGET_10 = json.loads(
    """
{
 "primary": 28485,
 "secondary": 67851,
 "current_miss_km": 5.7923,
 "target_miss_km": 10.0,
 "warning": null,
 "recommended": {
  "direction": "along-track",
  "delta_v_m_s": 0.0325,
  "burn_time": "2026-03-21T12:00:11Z",
  "lead_time_hours": 24.0,
  "new_miss_distance_km": 10.0,
  "fuel_mass_kg": null
 },
 "options": [
  {
   "direction": "along-track",
   "delta_v_m_s": 0.0325,
   "burn_time": "2026-03-21T12:00:11Z",
   "lead_time_hours": 24.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "along-track",
   "delta_v_m_s": 0.065,
   "burn_time": "2026-03-22T00:00:11Z",
   "lead_time_hours": 12.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "radial",
   "delta_v_m_s": 0.0974,
   "burn_time": "2026-03-21T12:00:11Z",
   "lead_time_hours": 24.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "along-track",
   "delta_v_m_s": 0.1301,
   "burn_time": "2026-03-22T06:00:11Z",
   "lead_time_hours": 6.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "radial",
   "delta_v_m_s": 0.1948,
   "burn_time": "2026-03-22T00:00:11Z",
   "lead_time_hours": 12.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "radial",
   "delta_v_m_s": 0.3896,
   "burn_time": "2026-03-22T06:00:11Z",
   "lead_time_hours": 6.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "along-track",
   "delta_v_m_s": 0.3902,
   "burn_time": "2026-03-22T10:00:11Z",
   "lead_time_hours": 2.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "radial",
   "delta_v_m_s": 1.1688,
   "burn_time": "2026-03-22T10:00:11Z",
   "lead_time_hours": 2.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "cross-track",
   "delta_v_m_s": 4.9379,
   "burn_time": "2026-03-22T00:00:11Z",
   "lead_time_hours": 12.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "cross-track",
   "delta_v_m_s": 5.0001,
   "burn_time": "2026-03-22T10:00:11Z",
   "lead_time_hours": 2.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "cross-track",
   "delta_v_m_s": 7.8377,
   "burn_time": "2026-03-22T06:00:11Z",
   "lead_time_hours": 6.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  },
  {
   "direction": "cross-track",
   "delta_v_m_s": 9.5072,
   "burn_time": "2026-03-21T12:00:11Z",
   "lead_time_hours": 24.0,
   "new_miss_distance_km": 10.0,
   "fuel_mass_kg": null
  }
 ]
}
"""
)


BREAKUP_BASE = json.loads(
    """
{
 "event_type": "collision",
 "parent_type": "spacecraft",
 "epoch": "2026-03-22T12:00:11Z",
 "catastrophic": true,
 "specific_energy_j_per_g": 101.01613519999998,
 "effective_mass_kg": 1010.0,
 "total_fragments_modelled": 47123.8787998696,
 "n_above_1cm": 47123.8787998696,
 "n_above_10cm": 918.8424059757838,
 "fragments_sampled": 500,
 "representative_count": 94.2477575997392,
 "seed": 0,
 "summary": {
  "total_sampled_mass_kg": 6.509865669487757,
  "represented_mass_kg": 613.5402416247462,
  "median_mass_kg": 0.0003715364322468402,
  "max_characteristic_length_m": 0.28935668976819506,
  "min_characteristic_length_m": 0.010001758171906914,
  "median_delta_v_m_s": 321.3077200308746,
  "max_delta_v_m_s": 9525.736482685697,
  "median_area_to_mass_m2_kg": 0.3625114667182886
 },
 "tles_fitted": 194,
 "tles_skipped": 106,
 "max_tle_position_error_km": 0.1291728444212451,
 "gabbard": [
  {
   "index": 0,
   "period_min": 95.17165618291621,
   "apogee_km": 691.7513105412136,
   "perigee_km": 362.0331215476608,
   "semi_major_axis_km": 6905.027216044437,
   "eccentricity": 0.023875227329113433,
   "bound": true
  },
  {
   "index": 1,
   "period_min": 90.16682679914476,
   "apogee_km": 390.08236424117786,
   "perigee_km": 175.19979905531636,
   "semi_major_axis_km": 6660.776081648248,
   "eccentricity": 0.01613044505263481,
   "bound": true
  }
 ]
}
"""
)


BREAKUP_TLE_TEXT = """FRAG-00000
1 90000U 26001A   26081.50012731  .00000000  00000-0  00000-0 0  9994
2 90000  21.1585 130.4490 0228398 175.0151  28.8946 15.15006337    15
FRAG-00001
1 90001U 26001A   26081.50012731  .00000000  00000-0  00000-0 0  9995
2 90001  20.1068 128.9967 0171146  45.3742 160.5532 15.99241685    19
FRAG-00002
1 90002U 26001A   26081.50012731  .00000000  00000-0  00000-0 0  9996
2 90002  20.4971 129.5545 0012273 351.0168 215.1077 15.63612376    10
"""

# Kept out of the JSON literal above only so no source line is longer than 99 columns.
CHAN_MESSAGE = (
    "encounter plane is too elongated for the Chan equivalent-area reduction; "
    "report the Foster or Alfano value instead"
)
for _w in COMPARE_STRESS["usage"]["warnings"]:
    if _w["message"] == "@CHAN_MESSAGE@":
        _w["message"] = CHAN_MESSAGE


# Just enough DOM for the panel: elements with children, attributes, class names,
# listeners and a disabled flag (a disabled button gets no click, as in a browser), plus
# a document with a head, a body and document-level events delivered to the target only.
# Assigning innerHTML throws, which turns "never innerHTML" into a test. The rest of the
# prelude is the page around the module: fake timers that only run when a scenario says
# so, a fetch that answers from a route table or waits to be answered, an
# AbortController, and just enough Cesium and satellite.js to record what the cloud does.
FAKE_DOM = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = [];
  this.attrs = {};
  this.listeners = {};
  this.style = {};
  this.className = '';
  this.id = '';
  this.disabled = false;
  this.title = '';
  this.parentNode = null;
  this._text = '';
}
El.prototype.appendChild = function (c) {
  if (c.parentNode) c.parentNode.removeChild(c);
  c.parentNode = this;
  this.children.push(c);
  return c;
};
El.prototype.removeChild = function (c) {
  var at = this.children.indexOf(c);
  if (at < 0) throw new Error('removeChild: not a child');
  this.children.splice(at, 1);
  c.parentNode = null;
  return c;
};
El.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.removeEventListener = function (t, fn) {
  this.listeners[t] = (this.listeners[t] || []).filter(function (f) { return f !== fn; });
};
Object.defineProperty(El.prototype, 'textContent', {
  get: function () {
    return this._text + this.children.map(function (c) { return c.textContent; }).join('');
  },
  set: function (v) { this.children = []; this._text = String(v); }
});
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { throw new Error('innerHTML read'); },
  set: function () { throw new Error('innerHTML used'); }
});
Object.defineProperty(El.prototype, 'classList', {
  get: function () {
    var el = this;
    function list() { return el.className.split(/\s+/).filter(Boolean); }
    function set(names) { el.className = names.join(' '); }
    return {
      contains: function (c) { return list().indexOf(c) >= 0; },
      add: function (c) { if (list().indexOf(c) < 0) set(list().concat(c)); },
      remove: function (c) { set(list().filter(function (x) { return x !== c; })); },
      toggle: function (c) { if (this.contains(c)) this.remove(c); else this.add(c); }
    };
  }
});
El.prototype.has = function (cls) { return this.classList.contains(cls); };
El.prototype.fire = function (type, ev) {
  ev = ev || {};
  ev.type = type;
  ev.preventDefault = function () {};
  ev.stopPropagation = function () {};
  var fns = (this.listeners[type] || []).slice();
  for (var i = 0; i < fns.length; i++) fns[i].call(this, ev);
  return ev;
};
El.prototype.click = function () { return this.disabled ? null : this.fire('click', {}); };
function all(el, out) {
  out = out || [];
  out.push(el);
  el.children.forEach(function (c) { all(c, out); });
  return out;
}
function byClass(el, cls) { return all(el).filter(function (x) { return x.has(cls); }); }

var document = {
  head: new El('head'),
  body: new El('body'),
  listeners: {},
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    var hit = all(this.head).concat(all(this.body)).filter(function (x) { return x.id === id; });
    return hit[0] || null;
  },
  addEventListener: function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
  removeEventListener: function (t, fn) {
    this.listeners[t] = (this.listeners[t] || []).filter(function (f) { return f !== fn; });
  },
  listenerCount: function () {
    var n = 0, l = this.listeners;
    Object.keys(l).forEach(function (k) { n += l[k].length; });
    return n;
  },
  dispatch: function (type, detail) {
    var ev = { type: type, detail: detail };
    (this.listeners[type] || []).slice().forEach(function (fn) { fn(ev); });
    return ev;
  }
};

// Timers never run by themselves; a scenario fires them, so "a timer was left behind"
// is a count rather than a race.
var timers = { nextId: 1, live: {} };
function setTimeout(fn, ms) {
  var id = timers.nextId++;
  timers.live[id] = { fn: fn, ms: ms, kind: 'timeout' };
  return id;
}
function setInterval(fn, ms) {
  var id = timers.nextId++;
  timers.live[id] = { fn: fn, ms: ms, kind: 'interval' };
  return id;
}
function clearTimeout(id) { delete timers.live[id]; }
function clearInterval(id) { delete timers.live[id]; }
timers.of = function (kind) {
  var live = this.live;
  return Object.keys(live).filter(function (id) { return live[id].kind === kind; })
    .map(function (id) { return live[id]; });
};
timers.fireTimeouts = function () {
  var live = this.live;
  Object.keys(live).filter(function (id) { return live[id].kind === 'timeout'; })
    .forEach(function (id) { var t = live[id]; delete live[id]; t.fn(); });
};
timers.tick = function () {
  var live = this.live;
  Object.keys(live).filter(function (id) { return live[id].kind === 'interval'; })
    .forEach(function (id) { if (live[id]) live[id].fn(); });
};

function AbortController() {
  var fns = [];
  var signal = { aborted: false, addEventListener: function (t, fn) { fns.push(fn); } };
  this.signal = signal;
  this.abort = function () {
    if (signal.aborted) return;
    signal.aborted = true;
    fns.forEach(function (fn) { fn(); });
  };
}

// net.routes[path] is { status, body } to answer at once; with net.hold every call waits
// and the scenario answers it through call.answer(status, body) or call.fail().
var net = { calls: [], hold: false, routes: {} };
function makeResponse(status, body, badJson) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: function () {
      return badJson ? Promise.reject(new SyntaxError('Unexpected token')) : Promise.resolve(body);
    }
  };
}
function fetch(url, opts) {
  var call = {
    url: url, method: opts.method, headers: opts.headers, body: JSON.parse(opts.body),
    signal: opts.signal || null
  };
  net.calls.push(call);
  return new Promise(function (resolve, reject) {
    call.answer = function (status, body, badJson) {
      resolve(makeResponse(status, body, badJson));
    };
    call.fail = function () { reject(new TypeError('Failed to fetch')); };
    if (call.signal) {
      call.signal.addEventListener('abort', function () {
        var e = new Error('The operation was aborted');
        e.name = 'AbortError';
        reject(e);
      });
    }
    if (net.hold) return;
    var path = url.replace(/^[a-z]+:\/\/[^/]+/, '');
    var route = net.routes[path];
    if (!route) { call.fail(); return; }
    call.answer(route.status, route.body, route.badJson);
  });
}
function lastCall() { return net.calls[net.calls.length - 1]; }

// Cesium and satellite.js, reduced to what the fragment cloud touches. Every point keeps
// the position it was last given, so a scenario can compare it with the conversion the
// page's own updateSatPosition does.
var trace = { fromRadians: [], gstime: [], propagate: [], eci: [], twoline: [] };
function PointPrimitiveCollection() { this.points = []; this.removed = false; }
PointPrimitiveCollection.prototype.add = function (o) {
  var p = { position: o.position, pixelSize: o.pixelSize, color: o.color, show: true };
  this.points.push(p);
  return p;
};
var Cesium = {
  PointPrimitiveCollection: PointPrimitiveCollection,
  Cartesian3: {
    fromRadians: function (lon, lat, h) {
      trace.fromRadians.push([lon, lat, h]);
      return { lon: lon, lat: lat, h: h };
    }
  },
  Color: { fromCssColorString: function (s) { return { css: s }; } }
};
var satellite = {
  twoline2satrec: function (l1, l2) {
    trace.twoline.push(l1);
    if (l1.indexOf('1 ') !== 0 || l2.indexOf('2 ') !== 0) throw new Error('bad element set');
    return { id: parseInt(l1.slice(2, 7), 10) };
  },
  gstime: function (when) { trace.gstime.push(when.getTime()); return 1.5; },
  propagate: function (rec, when) {
    trace.propagate.push([rec.id, when.getTime()]);
    if (rec.id % 2 === 1 && satellite.oddDecayed) return { position: false, velocity: false };
    return { position: { x: 7000 + rec.id % 100, y: 10, z: 20 }, velocity: { x: 0, y: 7, z: 0 } };
  },
  eciToGeodetic: function (pos, gmst) {
    trace.eci.push([pos.x, gmst]);
    return { longitude: pos.x / 1e4, latitude: 0.5, height: 400 + pos.x % 100 };
  }
};

function makePrimitives() {
  var p = { items: [], gone: [] };
  p.add = function (c) { p.items.push(c); return c; };
  p.remove = function (c) {
    var at = p.items.indexOf(c);
    if (at < 0) return false;
    p.items.splice(at, 1);
    p.gone.push(c);
    c.removed = true;
    return true;
  };
  p.clouds = function () { return p.items.filter(function (c) { return !c.removed; }); };
  return p;
}

var SIM_NOW = new Date(Date.UTC(2026, 2, 22, 12, 30, 0));

// The page flattens each event before it reaches the panel (worldview.html, where
// conjEvents is built); this copies that step so the fixtures arrive in the same shape.
function flatten(e) {
  var f = JSON.parse(JSON.stringify(e));
  f.primary = e.primary || e.primary_norad_id;
  f.secondary = e.secondary || e.secondary_norad_id;
  if (e.bplane) {
    f.bplane_x_km = e.bplane.miss_x_km;
    f.bplane_y_km = e.bplane.miss_y_km;
    f.sigma_combined_km = e.bplane.sigma_combined_km;
  }
  return f;
}
var EVENT = flatten(FIX.event);
var PLAIN = JSON.parse(JSON.stringify(FIX.plain));
function another(e) {
  var f = JSON.parse(JSON.stringify(e));
  f.secondary = 99999;
  f.secondary_name = 'OTHER OBJECT';
  f.tca = '2026-03-22T18:00:00Z';
  return f;
}

function setup(opts) {
  opts = opts || {};
  var h = { state: { current: ('event' in opts) ? opts.event : EVENT }, intel: [], simCalls: 0 };
  h.primitives = makePrimitives();
  h.ctx = {
    mount: new El('div'),
    api: ('api' in opts) ? opts.api : '',
    isStatic: !!opts.isStatic,
    getConjunction: function () { return h.state.current; },
    viewer: { scene: { primitives: h.primitives, requestRender: function () {} } },
    simNow: function () { h.simCalls += 1; return SIM_NOW; },
    satEngine: { byNoradId: {} },
    addIntelItem: function (t) { h.intel.push(t); }
  };
  if (opts.timeoutMs) h.ctx.timeoutMs = opts.timeoutMs;
  h.handle = window.SDAConjActions.init(h.ctx);
  return h;
}
function openEvent(h, e) {
  h.state.current = e;
  document.dispatch('sda:conjunction-open', e);
}
function closeEvent(h) {
  h.state.current = null;
  document.dispatch('sda:conjunction-close', {});
}
function btn(h, action) {
  return all(h.ctx.mount).filter(function (x) {
    return x.tagName === 'BUTTON' && x.getAttribute('data-action') === action;
  })[0] || null;
}
function block(h, action) {
  return all(h.ctx.mount).filter(function (x) {
    return x.getAttribute('data-block') === action;
  })[0] || null;
}
function statusOf(h, action) { return byClass(block(h, action), 'ca-status')[0]; }
function texts(els) { return els.map(function (e) { return e.textContent; }); }
function rowsOf(h, action, cls) { return texts(byClass(block(h, action), cls)); }
function numbers(text) { return (text.match(/-?\d+\.?\d*(?:e[+-]?\d+)?/gi) || []).map(Number); }
async function settle() { for (var i = 0; i < 60; i++) await Promise.resolve(); }
function route(path, status, body, badJson) {
  net.routes[path] = { status: status, body: body, badJson: !!badJson };
}
function okRoutes() {
  route('/pc/compare', 200, FIX.compare);
  route('/maneuver', 200, FIX.maneuver);
  route('/breakup/simulate', 200, FIX.breakup);
}
function clouds(h) { return h.primitives.clouds(); }
function allRemovedCount(h) { return h.primitives.gone.length; }
function noResult(h, action) {
  var blk = block(h, action);
  return ['ca-row', 'ca-opt', 'ca-warn', 'ca-assume', 'ca-note'].every(function (c) {
    return byClass(blk, c).length === 0;
  });
}
"""


# One scenario per behaviour group. Each body is the inside of an async function that
# returns a JSON-able value; it runs in a fresh vm context where `window.SDAConjActions`
# is loaded and FIX holds the fixtures.
SCENARIOS: dict[str, str] = {}


def scenario(name: str, body: str) -> None:
    assert name not in SCENARIOS
    SCENARIOS[name] = body


DRIVER = r"""
var vm = require('vm');
var fs = require('fs');
var prelude = fs.readFileSync(process.argv[2], 'utf8');
var modulePath = process.argv[3];
var scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

function load() {
  var box = {};
  box.window = box;
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'fake-dom' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'conjactions.js' });
  return box;
}

function withDeadline(promise, name) {
  return new Promise(function (resolve, reject) {
    var t = setTimeout(function () { reject(new Error(name + ' never finished')); }, 8000);
    promise.then(function (v) { clearTimeout(t); resolve(v); },
                 function (e) { clearTimeout(t); reject(e); });
  });
}

async function main() {
  var results = {};
  var names = Object.keys(scenarios);
  for (var i = 0; i < names.length; i++) {
    var name = names[i];
    try {
      var box = load();
      var text = await withDeadline(vm.runInContext(
        '(async function () {\n' + scenarios[name] + '\n})().then(function (v) {' +
        ' return JSON.stringify(v); })', box, { filename: name }), name);
      results[name] = { ok: true, value: JSON.parse(text) };
    } catch (e) {
      results[name] = { ok: false, error: String((e && e.stack) || e) };
    }
  }
  process.stdout.write(JSON.stringify(results));
}
main();
"""


class Ran:
    """Scenario results; reading one that threw fails the reading test with the stack."""

    def __init__(self, raw: dict):
        self._raw = raw

    def __getitem__(self, name: str):
        result = self._raw[name]
        assert result["ok"], result["error"]
        return result["value"]


def _fixtures() -> dict:
    breakup = dict(BREAKUP_BASE)
    breakup["tle_text"] = BREAKUP_TLE_TEXT
    return {
        "event": EVENT_RAW,
        "plain": PLAIN_EVENT,
        "compare": COMPARE_OK,
        "stress": COMPARE_STRESS,
        "maneuver": MANEUVER_TARGET_10,
        "noManeuver": MANEUVER_NONE,
        "breakup": breakup,
    }


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> Ran:
    work = tmp_path_factory.mktemp("conjactions")
    driver = work / "driver.js"
    spec = work / "scenarios.json"
    driver.write_text(DRIVER, encoding="utf-8")
    spec.write_text(json.dumps(SCENARIOS), encoding="utf-8")
    dom = work / "fake_dom.js"
    dom.write_text("var FIX = " + json.dumps(_fixtures()) + ";\n" + FAKE_DOM, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), str(dom), MODULE.as_posix(), str(spec)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- scenarios ---

scenario(
    "exports",
    r"""
var A = window.SDAConjActions;
return {
  keys: Object.keys(A).sort(),
  max: A.MAX_FRAGMENTS,
  interval: A.CLOUD_INTERVAL_MS,
  timeout: A.REQUEST_TIMEOUT_MS,
  initIsFunction: typeof A.init === 'function'
};
""",
)

scenario(
    "helpers",
    r"""
var A = window.SDAConjActions;
var shuffled = FIX.maneuver.options.slice().reverse();
var long = new Array(400).join('x ');
return {
  pc: [1.2341e-5, 0, null, undefined, NaN, 3e-4, 5e-7].map(A.formatPc),
  band: [2e-4, 1e-4, 5e-5, 1e-6, 9e-7, 0, 0.2, null].map(A.pcBand),
  input: shuffled.map(function (o) { return o.delta_v_m_s; }),
  sorted: A.sortOptions(shuffled).map(function (o) { return o.delta_v_m_s; }),
  inputUntouched: shuffled.map(function (o) { return o.delta_v_m_s; }),
  ties: A.sortOptions([
    { direction: 'b', delta_v_m_s: 1 }, { direction: 'a', delta_v_m_s: 1 },
    { direction: 'c', delta_v_m_s: 0.5 }]).map(function (o) { return o.direction; }),
  err422List: A.describeError(422, { detail: [
    { loc: ['body', 'rel_velocity_km_s'], msg: 'Field required', type: 'missing' }] }),
  err422Text: A.describeError(422, { detail: 'relative velocity is zero; no plane' }),
  err404: A.describeError(404, { detail: 'One or both satellites not found' }),
  err500: A.describeError(500, null),
  err429: A.describeError(429, { retry_after_seconds: 3.2 }),
  errNetwork: A.describeError(0, null),
  errLong: A.describeError(422, { detail: long }),
  timeout: A.describeTimeout(45000)
};
""",
)

scenario(
    "bodies",
    r"""
var A = window.SDAConjActions;
var noSpeed = JSON.parse(JSON.stringify(PLAIN));
noSpeed.relative_velocity_km_s = null;
return {
  compare: A.buildCompareBody(EVENT),
  maneuver: A.buildManeuverBody(EVENT),
  breakup: A.buildBreakupBody(EVENT),
  plainCompare: A.buildCompareBody(PLAIN),
  plainManeuver: A.buildManeuverBody(PLAIN),
  plainBreakup: A.buildBreakupBody(PLAIN),
  noSpeed: A.buildCompareBody(noSpeed),
  noIds: A.buildManeuverBody({ tca: '2026-03-22T12:00:11Z' }),
  noTca: A.buildManeuverBody({ primary: 1, secondary: 2 }),
  nothing: A.buildBreakupBody(null)
};
""",
)

scenario(
    "compare_flow",
    r"""
okRoutes();
net.hold = true;
var h = setup();
var b = btn(h, 'compare');
var before = [b.disabled, btn(h, 'maneuver').disabled, btn(h, 'breakup').disabled];
b.click();
var during = {
  own: b.disabled,
  others: [btn(h, 'maneuver').disabled, btn(h, 'breakup').disabled],
  status: statusOf(h, 'compare').textContent,
  calls: net.calls.length,
  timeouts: timers.of('timeout').map(function (t) { return t.ms; })
};
var call = lastCall();
call.answer(200, FIX.compare);
await settle();
return {
  before: before,
  during: during,
  request: { url: call.url, method: call.method, headers: call.headers, body: call.body },
  after: {
    own: b.disabled,
    status: statusOf(h, 'compare').textContent,
    statusIsError: statusOf(h, 'compare').has('ca-error'),
    methods: rowsOf(h, 'compare', 'ca-k'),
    pcs: rowsOf(h, 'compare', 'ca-v'),
    bands: rowsOf(h, 'compare', 'ca-band'),
    assume: rowsOf(h, 'compare', 'ca-assume'),
    warns: rowsOf(h, 'compare', 'ca-warn'),
    notes: rowsOf(h, 'compare', 'ca-note'),
    timeouts: timers.of('timeout').length,
    otherBlocks: [noResult(h, 'maneuver'), noResult(h, 'breakup')]
  }
};
""",
)

scenario(
    "compare_stress",
    r"""
route('/pc/compare', 200, FIX.stress);
var h = setup();
btn(h, 'compare').click();
await settle();
return {
  warns: rowsOf(h, 'compare', 'ca-warn'),
  pcs: rowsOf(h, 'compare', 'ca-v'),
  notes: rowsOf(h, 'compare', 'ca-note'),
  assume: rowsOf(h, 'compare', 'ca-assume')
};
""",
)

scenario(
    "compare_defaults",
    r"""
var resp = JSON.parse(JSON.stringify(FIX.compare));
resp.usage = { ok: true, codes: [], warnings: [], encounter_duration_s: 0.2, condition_number: 1 };
resp.assumptions = [
  'primary covariance missing; isotropic sigma=0.05 km assumed',
  'secondary covariance missing; isotropic sigma=0.05 km assumed'
];
resp.miss_distance_km = 5.7923;
route('/pc/compare', 200, resp);
var h = setup({ event: PLAIN });
btn(h, 'compare').click();
await settle();
return {
  body: net.calls[0].body,
  assume: rowsOf(h, 'compare', 'ca-assume'),
  warns: rowsOf(h, 'compare', 'ca-warn'),
  notes: rowsOf(h, 'compare', 'ca-note')
};
""",
)

scenario(
    "maneuver_flow",
    r"""
var resp = JSON.parse(JSON.stringify(FIX.maneuver));
resp.options.reverse();
route('/maneuver', 200, resp);
net.hold = true;
var h = setup();
var b = btn(h, 'maneuver');
b.click();
var during = {
  own: b.disabled,
  others: [btn(h, 'compare').disabled, btn(h, 'breakup').disabled],
  status: statusOf(h, 'maneuver').textContent
};
var call = lastCall();
call.answer(200, resp);
await settle();
return {
  during: during,
  request: { url: call.url, method: call.method, body: call.body },
  after: {
    own: b.disabled,
    status: statusOf(h, 'maneuver').textContent,
    dirs: rowsOf(h, 'maneuver', 'ca-opt-dir'),
    dvs: rowsOf(h, 'maneuver', 'ca-opt-dv'),
    leads: rowsOf(h, 'maneuver', 'ca-opt-lead'),
    misses: rowsOf(h, 'maneuver', 'ca-opt-miss'),
    assume: rowsOf(h, 'maneuver', 'ca-assume'),
    notes: rowsOf(h, 'maneuver', 'ca-note'),
    timeouts: timers.of('timeout').length
  }
};
""",
)

scenario(
    "maneuver_none",
    r"""
route('/maneuver', 200, FIX.noManeuver);
var h = setup();
btn(h, 'maneuver').click();
await settle();
return {
  options: rowsOf(h, 'maneuver', 'ca-opt-dv'),
  notes: rowsOf(h, 'maneuver', 'ca-note'),
  status: statusOf(h, 'maneuver').textContent
};
""",
)

scenario(
    "breakup_flow",
    r"""
route('/breakup/simulate', 200, FIX.breakup);
net.hold = true;
var h = setup();
var b = btn(h, 'breakup');
b.click();
var during = {
  own: b.disabled,
  others: [btn(h, 'compare').disabled, btn(h, 'maneuver').disabled],
  status: statusOf(h, 'breakup').textContent,
  clouds: clouds(h).length
};
var call = lastCall();
call.answer(200, FIX.breakup);
await settle();
var cloud = clouds(h)[0];
var first = {
  clouds: clouds(h).length,
  points: cloud.points.length,
  shown: cloud.points.map(function (p) { return p.show; }),
  pixel: cloud.points[0].pixelSize,
  fromRadians: trace.fromRadians.slice(),
  gstime: trace.gstime.slice(),
  eci: trace.eci.slice(),
  propagate: trace.propagate.slice(),
  simCalls: h.simCalls,
  intervals: timers.of('interval').map(function (t) { return t.ms; }),
  clearDisabled: btn(h, 'clear').disabled
};
return {
  during: during,
  request: { url: call.url, method: call.method, body: call.body },
  first: first,
  after: {
    own: b.disabled,
    status: statusOf(h, 'breakup').textContent,
    assume: rowsOf(h, 'breakup', 'ca-assume'),
    notes: rowsOf(h, 'breakup', 'ca-note'),
    cloudline: rowsOf(h, 'breakup', 'ca-cloudline'),
    timeouts: timers.of('timeout').length,
    intel: h.intel
  }
};
""",
)

scenario(
    "cloud_tick",
    r"""
route('/breakup/simulate', 200, FIX.breakup);
satellite.oddDecayed = true;
var h = setup();
btn(h, 'breakup').click();
await settle();
var cloud = clouds(h)[0];
var drawn = trace.propagate.length;
var simBefore = h.simCalls;
timers.tick();
timers.tick();
return {
  drawn: drawn,
  afterTwoTicks: trace.propagate.length,
  simBefore: simBefore,
  simAfter: h.simCalls,
  shown: cloud.points.map(function (p) { return p.show; }),
  positions: cloud.points.map(function (p) { return p.position && p.position.h; })
};
""",
)

scenario(
    "breakup_cap",
    r"""
var base = FIX.breakup.tle_text.split('\n');
var out = ['JUNK-1', '1 not an element set', '2 nope'];
for (var i = 0; i < 450; i++) {
  var id = String(10000 + i);
  out.push('FRAG-' + id);
  out.push('1 ' + id + base[1].slice(7));
  out.push('2 ' + id + base[2].slice(7));
}
var big = JSON.parse(JSON.stringify(FIX.breakup));
big.tle_text = out.join('\n');
route('/breakup/simulate', 200, big);
var h = setup();
btn(h, 'breakup').click();
await settle();
return {
  clouds: clouds(h).length,
  points: clouds(h)[0].points.length,
  cloudline: rowsOf(h, 'breakup', 'ca-cloudline'),
  junkTried: trace.twoline.filter(function (l) { return l.indexOf('not an') >= 0; }).length,
  intervals: timers.of('interval').length
};
""",
)

scenario(
    "breakup_lifecycle",
    r"""
route('/breakup/simulate', 200, FIX.breakup);
var h = setup();
var s = {};
function snap() {
  return {
    clouds: clouds(h).length,
    removed: allRemovedCount(h),
    intervals: timers.of('interval').length,
    clearDisabled: btn(h, 'clear').disabled
  };
}
btn(h, 'breakup').click();
await settle();
s.firstRun = snap();

net.hold = true;
btn(h, 'breakup').click();
s.secondRunWaiting = snap();
lastCall().answer(200, FIX.breakup);
await settle();
net.hold = false;
s.secondRunDone = snap();

btn(h, 'clear').click();
s.cleared = snap();
s.clearedLine = rowsOf(h, 'breakup', 'ca-cloudline');

btn(h, 'breakup').click();
await settle();
s.beforeClose = snap();
closeEvent(h);
s.closed = snap();
s.closedEmpty = noResult(h, 'breakup');
s.closedButtons = [btn(h, 'breakup').disabled, btn(h, 'compare').disabled];

openEvent(h, EVENT);
btn(h, 'breakup').click();
await settle();
s.beforeOther = snap();
s.callsBefore = net.calls.length;
openEvent(h, another(EVENT));
s.otherOpened = snap();
s.otherEmpty = noResult(h, 'breakup');
s.callsAfter = net.calls.length;
return s;
""",
)

scenario(
    "no_satellite_library",
    r"""
route('/breakup/simulate', 200, FIX.breakup);
window.satellite = undefined;
var h = setup();
btn(h, 'breakup').click();
await settle();
return {
  clouds: clouds(h).length,
  intervals: timers.of('interval').length,
  status: statusOf(h, 'breakup').textContent,
  notes: rowsOf(h, 'breakup', 'ca-note'),
  assume: rowsOf(h, 'breakup', 'ca-assume')
};
""",
)

scenario(
    "errors",
    r"""
var kinds = {
  invalid: function (call) {
    call.answer(422, { detail: [
      { loc: ['body', 'rel_velocity_km_s'], msg: 'Field required', type: 'missing' }] });
  },
  unprocessable: function (call) {
    call.answer(422, { detail: 'relative velocity is zero; the encounter plane is undefined' });
  },
  server500: function (call) { call.answer(500, 'Internal Server Error', true); },
  server500json: function (call) { call.answer(500, { detail: 'boom\nTraceback...' }); },
  network: function (call) { call.fail(); },
  timeout: function (call) { timers.fireTimeouts(); },
  unreadable: function (call) { call.answer(200, null, true); },
  wrongShape: function (call) { call.answer(200, { unexpected: true }); }
};
var out = {};
var paths = { compare: '/pc/compare', maneuver: '/maneuver', breakup: '/breakup/simulate' };
var actions = Object.keys(paths);
for (var a = 0; a < actions.length; a++) {
  var names = Object.keys(kinds);
  for (var k = 0; k < names.length; k++) {
    net.calls = [];
    net.hold = true;
    var h = setup();
    var b = btn(h, actions[a]);
    b.click();
    kinds[names[k]](lastCall());
    await settle();
    out[actions[a] + ':' + names[k]] = {
      status: statusOf(h, actions[a]).textContent,
      isError: statusOf(h, actions[a]).has('ca-error'),
      enabled: !b.disabled,
      timeoutsLeft: timers.of('timeout').length,
      nothingDrawn: noResult(h, actions[a]) && clouds(h).length === 0
    };
    h.handle.destroy();
  }
}
return out;
""",
)

scenario(
    "timeout_budget",
    r"""
net.hold = true;
var h = setup({ timeoutMs: 2000 });
btn(h, 'compare').click();
var call = lastCall();
var armed = timers.of('timeout').map(function (t) { return t.ms; });
timers.fireTimeouts();
await settle();
var status = statusOf(h, 'compare').textContent;
call.answer(200, FIX.compare);
await settle();
return {
  armed: armed,
  status: status,
  aborted: call.signal.aborted,
  statusAfterLateAnswer: statusOf(h, 'compare').textContent,
  drawn: !noResult(h, 'compare')
};
""",
)

scenario(
    "stale",
    r"""
okRoutes();
var out = {};
var paths = { compare: '/pc/compare', maneuver: '/maneuver', breakup: '/breakup/simulate' };
var actions = Object.keys(paths);
for (var a = 0; a < actions.length; a++) {
  var act = actions[a];
  var res = {};
  net.hold = true;

  var h = setup();
  btn(h, act).click();
  var call = lastCall();
  closeEvent(h);
  call.answer(200, { compare: FIX.compare, maneuver: FIX.maneuver, breakup: FIX.breakup }[act]);
  await settle();
  res.closed = {
    empty: noResult(h, act), clouds: clouds(h).length,
    intervals: timers.of('interval').length, status: statusOf(h, act).textContent,
    enabled: !btn(h, act).disabled
  };

  openEvent(h, EVENT);
  btn(h, act).click();
  call = lastCall();
  openEvent(h, another(EVENT));
  call.answer(200, { compare: FIX.compare, maneuver: FIX.maneuver, breakup: FIX.breakup }[act]);
  await settle();
  res.swapped = {
    empty: noResult(h, act), clouds: clouds(h).length,
    intervals: timers.of('interval').length, status: statusOf(h, act).textContent,
    enabled: !btn(h, act).disabled
  };

  openEvent(h, EVENT);
  btn(h, act).click();
  call = lastCall();
  h.state.current = another(EVENT);
  call.answer(200, { compare: FIX.compare, maneuver: FIX.maneuver, breakup: FIX.breakup }[act]);
  await settle();
  res.silentSwap = {
    empty: noResult(h, act), clouds: clouds(h).length,
    intervals: timers.of('interval').length
  };

  net.hold = false;
  openEvent(h, EVENT);
  btn(h, act).click();
  await settle();
  res.fresh = { empty: noResult(h, act), clouds: clouds(h).length };
  h.handle.destroy();
  out[act] = res;
}
return out;
""",
)

scenario(
    "superseded",
    r"""
net.hold = true;
var h = setup();
btn(h, 'compare').click();
var first = lastCall();
closeEvent(h);
openEvent(h, EVENT);
btn(h, 'compare').click();
var second = lastCall();
await settle();
var mid = {
  disabled: btn(h, 'compare').disabled,
  status: statusOf(h, 'compare').textContent,
  isError: statusOf(h, 'compare').has('ca-error'),
  calls: net.calls.length
};
btn(h, 'compare').fire('click');
mid.callsAfterExtraClick = net.calls.length;
first.answer(200, FIX.compare);
await settle();
mid.rowsAfterFirst = rowsOf(h, 'compare', 'ca-row').length;
second.answer(200, FIX.compare);
await settle();
return {
  mid: mid,
  firstAborted: first.signal.aborted,
  secondAborted: second.signal.aborted,
  final: {
    rows: rowsOf(h, 'compare', 'ca-row').length,
    disabled: btn(h, 'compare').disabled,
    status: statusOf(h, 'compare').textContent
  }
};
""",
)

scenario(
    "static_mode",
    r"""
okRoutes();
var h = setup({ isStatic: true });
return {
  text: h.ctx.mount.textContent,
  buttons: all(h.ctx.mount).filter(function (x) { return x.tagName === 'BUTTON'; }).length,
  calls: net.calls.length,
  listeners: document.listenerCount(),
  hasDestroy: !!(h.handle && typeof h.handle.destroy === 'function'),
  lines: all(h.ctx.mount).filter(function (x) { return x.has('ca-static'); }).length
};
""",
)

scenario(
    "no_event",
    r"""
okRoutes();
var h = setup({ event: null });
btn(h, 'compare').click();
btn(h, 'maneuver').click();
btn(h, 'breakup').click();
await settle();
return {
  calls: net.calls.length,
  statuses: ['compare', 'maneuver', 'breakup'].map(function (a) {
    return statusOf(h, a).textContent;
  }),
  enabled: ['compare', 'maneuver', 'breakup'].map(function (a) { return !btn(h, a).disabled; })
};
""",
)

scenario(
    "double_fire",
    r"""
net.hold = true;
var h = setup();
var b = btn(h, 'compare');
b.fire('click');
b.fire('click');
b.fire('click');
return { calls: net.calls.length };
""",
)

scenario(
    "api_prefix",
    r"""
okRoutes();
var h = setup({ api: 'http://svc.test' });
btn(h, 'compare').click();
btn(h, 'maneuver').click();
btn(h, 'breakup').click();
await settle();
return net.calls.map(function (c) { return c.url; });
""",
)

scenario(
    "hostile",
    r"""
var evil = '<img src=x onerror=alert(1)>';
var evil2 = '</div><script>alert(2)</script>';
var event = JSON.parse(JSON.stringify(EVENT));
event.primary_name = evil;
event.secondary_name = evil2;
var compare = JSON.parse(JSON.stringify(FIX.compare));
compare.usage.warnings[0].message = '<script>alert(3)</script> dilution';
var maneuver = JSON.parse(JSON.stringify(FIX.maneuver));
maneuver.options[0].direction = '<b>along</b>';
maneuver.warning = '<i>careful</i>';
var breakup = JSON.parse(JSON.stringify(FIX.breakup));
breakup.event_type = '<u>collision</u>';
route('/pc/compare', 200, compare);
route('/maneuver', 200, maneuver);
route('/breakup/simulate', 200, breakup);
var h = setup({ event: event });
btn(h, 'compare').click();
btn(h, 'maneuver').click();
btn(h, 'breakup').click();
await settle();
var tags = {};
all(h.ctx.mount).forEach(function (x) { tags[x.tagName] = true; });
return {
  tags: Object.keys(tags).sort(),
  text: h.ctx.mount.textContent,
  intel: h.intel,
  calls: net.calls.length
};
""",
)

scenario(
    "lifecycle_listeners",
    r"""
okRoutes();
var h = setup();
var afterFirst = document.listenerCount();
var again = window.SDAConjActions.init(h.ctx);
var panels = byClass(h.ctx.mount, 'ca-root').length;
var afterSecond = document.listenerCount();
btn(h, 'breakup').click();
await settle();
var cloudsBefore = clouds(h).length;
again.destroy();
var out = {
  afterFirst: afterFirst,
  afterSecond: afterSecond,
  panels: panels,
  cloudsBefore: cloudsBefore,
  cloudsAfter: clouds(h).length,
  intervals: timers.of('interval').length,
  listeners: document.listenerCount(),
  mountChildren: h.ctx.mount.children.length
};
document.dispatch('sda:conjunction-open', EVENT);
document.dispatch('sda:conjunction-close', {});
out.survivedEvents = true;
again.destroy();
return out;
""",
)

scenario(
    "init_failures",
    r"""
var A = window.SDAConjActions;
var broken = new El('div');
broken.appendChild = function () { throw new Error('mount is gone'); };
return {
  empty: A.init({}),
  nothing: A.init(null),
  broken: A.init({ mount: broken, api: '', isStatic: false })
};
""",
)

scenario(
    "styles",
    r"""
var h = setup();
setup();
var styles = all(document.head).filter(function (x) { return x.tagName === 'STYLE'; });
return {
  count: styles.length,
  id: styles[0] && styles[0].id,
  css: styles[0] ? styles[0].textContent : ''
};
""",
)


# --- syntax, exports, source hygiene ---


def _source() -> str:
    return MODULE.read_text(encoding="utf-8")


def test_node_check_passes():
    proc = subprocess.run(
        [NODE, "--check", str(MODULE)], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_module_exports_init_and_the_pure_helpers(ran):
    got = ran["exports"]
    assert got["initIsFunction"] is True
    for name in (
        "init",
        "buildCompareBody",
        "buildManeuverBody",
        "buildBreakupBody",
        "sortOptions",
        "describeError",
        "describeTimeout",
        "formatPc",
        "pcBand",
    ):
        assert name in got["keys"], name
    assert got["max"] == MAX_FRAGMENTS
    assert got["interval"] == CLOUD_INTERVAL_MS
    assert got["timeout"] == REQUEST_TIMEOUT_MS


def test_source_stays_inside_the_house_rules():
    text = _source()
    text.encode("ascii")  # a stray curly quote or dash fails here with its position
    assert "\u2014" not in text and "\u2013" not in text
    # split so the repo hook that blocks these two words in source files has nothing to match
    frames = ("WGS" + "84", "EC" + "EF")
    for banned in (*frames, "eval(", "new Function", "document.write"):
        assert banned not in text, banned
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert sink not in text, sink
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27bf]", text)


def test_source_only_talks_to_the_three_same_origin_routes():
    text = _source()
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(("//", "*", "/*"))
    )
    assert not re.search(r"https?://", code)
    assert "celestrak" not in text.lower()
    assert code.count("fetch(") == 1
    for route in ("/pc/compare", "/maneuver", "/breakup/simulate"):
        assert route in code, route


def test_header_credits_the_donor_with_commit_and_license():
    head = "\n".join(_source().splitlines()[:45])
    for needle in (
        "satguard",
        "cesabici-bit",
        "07a86ce",
        "MIT",
        "Copyright (c) 2026 Genius Lab",
        "ConjunctionBrowser.tsx",
    ):
        assert needle in head, needle


def test_cloud_uses_the_conversion_of_the_pages_own_update_sat_position():
    page = PAGE.read_text(encoding="utf-8")
    start = page.index("function updateSatPosition(")
    page_fn = page[start : start + 1800]
    mine = _source()
    for call in (
        "satellite.eciToGeodetic(pv.position, gmst)",
        "Cesium.Cartesian3.fromRadians(gd.longitude, gd.latitude, gd.height * 1000)",
    ):
        assert call in page_fn, f"the page no longer contains {call}"
        assert call in mine, call
    assert "satellite.gstime(" in mine and "satellite.propagate(" in mine


# --- pure helpers ---


def test_pc_is_shown_in_scientific_notation_with_na_for_missing(ran):
    got = ran["helpers"]
    assert got["pc"] == ["1.23e-5", "0", "N/A", "N/A", "N/A", "3.00e-4", "5.00e-7"]


def test_pc_bands_follow_the_donor_cutoffs(ran):
    assert ran["helpers"]["band"] == ["HIGH", "MED", "MED", "LOW", "LOW", "LOW", "HIGH", ""]


def test_options_sort_by_delta_v_without_touching_the_input(ran):
    got = ran["helpers"]
    assert got["sorted"] == sorted(got["input"])
    assert got["sorted"][0] == 0.0325
    assert got["input"] == got["inputUntouched"]
    assert got["input"] != got["sorted"]


def test_equal_delta_v_keeps_the_servers_order(ran):
    assert ran["helpers"]["ties"] == ["c", "b", "a"]


def test_error_sentences_are_plain_and_one_line(ran):
    got = ran["helpers"]
    assert got["err422List"] == "The server rejected rel_velocity_km_s: Field required."
    assert got["err422Text"].startswith("relative velocity is zero")
    assert got["err404"] == "One or both satellites not found."
    assert got["err500"] == "The server failed to answer (HTTP 500)."
    assert got["err429"] == "Too many requests, try again in 4 s."
    assert got["errNetwork"] == "Could not reach the server."
    assert got["timeout"] == "The server did not answer within 45 s."
    assert len(got["errLong"]) <= 150 and got["errLong"].endswith(".")
    for key in ("err422List", "err422Text", "err404", "err500", "errLong", "timeout"):
        assert "\n" not in got[key]


# --- request bodies ---


def _diag(sigma: float) -> list[list[float]]:
    s2 = sigma * sigma
    return [[s2, 0, 0], [0, s2, 0], [0, 0, s2]]


def test_compare_body_rebuilds_the_encounter_from_the_event_bplane_and_covariance(ran):
    built = ran["bodies"]["compare"]
    assert built["ok"] is True
    cov = EVENT_RAW["covariance"]
    assert built["body"] == {
        "miss_vector_km": [5.497067, 1.603339, 0],
        "rel_velocity_km_s": [0, 0, 4.4948],
        "cov_primary_km2": _diag(cov["primary_sigma_km"]),
        "cov_secondary_km2": _diag(cov["secondary_sigma_km"]),
        "include_monte_carlo": True,
    }
    assert built["geometry"] == "bplane"
    assert built["covariance"] == "event"


def test_compare_body_never_invents_a_hard_body_radius_or_sample_count(ran):
    for key in ("compare", "plainCompare"):
        body = ran["bodies"][key]["body"]
        assert "hard_body_radius_km" not in body
        assert "monte_carlo_samples" not in body


def test_compare_body_without_bplane_or_covariance_leaves_both_to_the_server(ran):
    built = ran["bodies"]["plainCompare"]
    assert built["ok"] is True
    assert built["body"] == {
        "miss_vector_km": [5.7923, 0, 0],
        "rel_velocity_km_s": [0, 0, 4.4948],
        "include_monte_carlo": True,
    }
    assert built["geometry"] == "miss-distance"
    assert built["covariance"] == "server-default"


def test_maneuver_body_matches_the_server_model(ran):
    want = {
        "primary_norad_id": 28485,
        "secondary_norad_id": 67851,
        "tca": EVENT_RAW["tca"],
        "target_miss_km": MANEUVER_TARGET_KM,
    }
    assert ran["bodies"]["maneuver"]["body"] == want
    assert ran["bodies"]["plainManeuver"]["body"] == {**want, "tca": PLAIN_EVENT["tca"]}


def test_breakup_body_matches_the_server_model(ran):
    want = {
        "norad_id": 28485,
        "epoch": EVENT_RAW["tca"],
        "event_type": "collision",
        "parent_type": "spacecraft",
        "parent_mass_kg": 1000,
        "projectile_mass_kg": 10,
        "relative_speed_km_s": 4.4948,
        "include_tles": True,
        "max_tle_objects": MAX_FRAGMENTS,
    }
    assert ran["bodies"]["breakup"]["body"] == want
    assert ran["bodies"]["plainBreakup"]["body"] == {**want, "epoch": PLAIN_EVENT["tca"]}


@pytest.mark.parametrize("key", ["noSpeed", "noIds", "noTca", "nothing"])
def test_an_event_that_cannot_build_a_body_says_why_and_builds_nothing(ran, key):
    built = ran["bodies"][key]
    assert built["ok"] is False
    assert "body" not in built
    assert built["error"].endswith(".") and "\n" not in built["error"]


# --- the three actions ---


def test_each_action_has_its_own_button_and_only_that_one_is_disabled_while_it_runs(ran):
    for name in ("compare_flow", "maneuver_flow", "breakup_flow"):
        got = ran[name]
        assert got["during"]["own"] is True, name
        assert got["during"]["others"] == [False, False], name
        assert got["after"]["own"] is False, name
    assert ran["compare_flow"]["before"] == [False, False, False]


def test_compare_posts_to_the_route_with_json_and_arms_one_timeout(ran):
    got = ran["compare_flow"]
    assert got["during"]["calls"] == 1
    assert got["during"]["timeouts"] == [REQUEST_TIMEOUT_MS]
    assert got["during"]["status"].startswith("Comparing")
    assert got["request"]["url"] == "/pc/compare"
    assert got["request"]["method"] == "POST"
    assert got["request"]["headers"]["Content-Type"] == "application/json"
    assert got["request"]["body"] == ran["bodies"]["compare"]["body"]
    assert got["after"]["timeouts"] == 0


def test_compare_shows_all_four_methods_side_by_side(ran):
    after = ran["compare_flow"]["after"]
    assert after["methods"] == ["Foster", "Chan", "Alfano", "Monte Carlo"]
    assert after["pcs"] == ["1.09e-218", "1.09e-218", "1.09e-218", "0"]
    assert after["bands"] == ["LOW"] * 4
    assert after["status"] == "" and after["statusIsError"] is False
    assert any("spread" in n.lower() and "1.09e-218" in n for n in after["notes"])
    assert after["otherBlocks"] == [True, True]


def test_compare_shows_the_usage_check_warning_in_the_servers_words(ran):
    warns = ran["compare_flow"]["after"]["warns"]
    want = COMPARE_OK["usage"]["warnings"][0]["message"]
    assert len(warns) == 1
    assert want in warns[0] or want[0].upper() + want[1:] in warns[0]


def test_compare_states_its_assumptions_in_one_line(ran):
    lines = ran["compare_flow"]["after"]["assume"]
    assert len(lines) == 1
    line = lines[0]
    assert "\n" not in line
    for needle in (
        "Assumed",
        "TLE age",
        "primary 0.151 km",
        "secondary 0.100 km",
        "hard-body radius 0.02 km",
        "server default",
        "B-plane miss 5.726 km",
        "relative speed 4.495 km/s",
    ):
        assert needle in line, needle


def test_every_usage_warning_of_a_hard_case_is_listed(ran):
    got = ran["compare_stress"]
    assert len(got["warns"]) == len(COMPARE_STRESS["usage"]["warnings"]) == 6
    for shown, raw in zip(got["warns"], COMPARE_STRESS["usage"]["warnings"], strict=True):
        assert raw["message"].lower() in shown.lower()
    assert got["pcs"] == ["0", "0", "0", "0"]
    assert not any("all passed" in n for n in got["notes"])


def test_server_assumptions_are_repeated_when_the_event_has_no_covariance(ran):
    got = ran["compare_defaults"]
    assert "cov_primary_km2" not in got["body"] and "cov_secondary_km2" not in got["body"]
    assert len(got["assume"]) == 1
    line = got["assume"][0]
    assert "primary covariance missing; isotropic sigma=0.05 km assumed" in line
    assert "secondary covariance missing; isotropic sigma=0.05 km assumed" in line
    assert "server" in line and "TLE age" not in line
    assert "miss distance 5.792 km" in line
    assert got["warns"] == []
    assert any("all passed" in n.lower() for n in got["notes"])


def test_maneuver_lists_options_smallest_delta_v_first(ran):
    after = ran["maneuver_flow"]["after"]
    dvs = [float(re.match(r"[\d.]+", text).group()) for text in after["dvs"]]
    assert len(dvs) == len(MANEUVER_TARGET_10["options"]) == 12
    assert dvs == sorted(dvs)
    assert dvs[0] == 0.0325 and dvs[-1] == 9.5072
    assert after["dirs"][0] == "along-track" and after["dirs"][-1] == "cross-track"
    assert all("m/s" in text for text in after["dvs"])
    assert all(text.startswith("10") and "km" in text for text in after["misses"])
    assert after["leads"][0].startswith("24 h")
    assert after["status"] == ""


def test_maneuver_posts_the_target_and_states_it(ran):
    got = ran["maneuver_flow"]
    assert got["request"]["url"] == "/maneuver"
    assert got["request"]["method"] == "POST"
    assert got["request"]["body"] == ran["bodies"]["maneuver"]["body"]
    assert got["during"]["status"].startswith("Planning")
    line = " ".join(got["after"]["assume"])
    assert "target miss 10 km" in line
    assert "impulsive" in line and "SWIFT" in line
    assert "2 to 24 h" in line
    assert any("12 options" in n and "5.79 km" in n for n in got["after"]["notes"])
    assert got["after"]["timeouts"] == 0


def test_maneuver_reports_the_servers_no_maneuver_needed_warning(ran):
    got = ran["maneuver_none"]
    assert got["options"] == []
    assert any(MANEUVER_NONE["warning"] in n for n in got["notes"])
    assert got["status"] == ""


def test_breakup_posts_the_pair_and_labels_the_preview_as_hypothetical(ran):
    got = ran["breakup_flow"]
    assert got["request"]["url"] == "/breakup/simulate"
    assert got["request"]["method"] == "POST"
    assert got["request"]["body"] == ran["bodies"]["breakup"]["body"]
    assert got["during"]["status"].startswith("Simulating")
    assert got["during"]["clouds"] == 0
    line = " ".join(got["after"]["assume"]).lower()
    for needle in ("hypothetical", "not an observed event", "1000 kg", "10 kg", "4.495 km/s"):
        assert needle in line, needle
    assert "SWIFT" in " ".join(got["after"]["assume"])
    assert "STARLINK-36348" in " ".join(got["after"]["assume"])


def test_breakup_summarises_the_consequences(ran):
    notes = " ".join(ran["breakup_flow"]["after"]["notes"])
    base = BREAKUP_BASE
    assert "Catastrophic: yes" in notes
    assert f"{base['specific_energy_j_per_g']:.0f} J/g" in notes
    assert f"{base['total_fragments_modelled']:.0f} fragments above 1 cm" in notes
    assert f"{base['n_above_10cm']:.0f} above 10 cm" in notes
    assert f"{base['summary']['median_delta_v_m_s']:.0f} m/s" in notes
    assert ran["breakup_flow"]["after"]["status"] == ""


def test_breakup_draws_one_point_per_fragment_in_its_own_collection(ran):
    first = ran["breakup_flow"]["first"]
    assert first["clouds"] == 1
    assert first["points"] == 3
    assert first["pixel"] > 0
    line = " ".join(ran["breakup_flow"]["after"]["cloudline"])
    assert "Drawing 3 fragments" in line and f"limit {MAX_FRAGMENTS}" in line
    assert f"{BREAKUP_BASE['tles_skipped']} could not be fitted" in line
    assert first["clearDisabled"] is False
    assert any("hypothetical" in t.lower() for t in ran["breakup_flow"]["after"]["intel"])


def test_cloud_positions_use_the_simulation_clock_and_the_pages_conversion(ran):
    first = ran["breakup_flow"]["first"]
    sim_ms = int(datetime(2026, 3, 22, 12, 30, tzinfo=UTC).timestamp() * 1000)  # SIM_NOW
    assert set(first["gstime"]) == {sim_ms}
    assert {p[1] for p in first["propagate"]} == {sim_ms}
    assert [p[0] for p in first["propagate"]] == [90000, 90001, 90002]
    assert first["simCalls"] >= 1
    assert all(g == 1.5 for _, g in first["eci"])
    # height is kilometres from eciToGeodetic and metres to Cesium, as in updateSatPosition
    assert first["fromRadians"][0] == [0.7, 0.5, 400000]
    assert first["fromRadians"][1] == [0.7001, 0.5, 401000]
    assert first["intervals"] == [CLOUD_INTERVAL_MS]


def test_cloud_refreshes_on_the_timer_and_hides_a_fragment_that_no_longer_propagates(ran):
    got = ran["cloud_tick"]
    assert got["drawn"] == 3
    assert got["afterTwoTicks"] == 3 + 2 * 3
    assert got["simAfter"] == got["simBefore"] + 2
    assert got["shown"] == [True, False, True]


def test_cloud_is_capped_at_the_fragment_limit_and_skips_bad_element_sets(ran):
    got = ran["breakup_cap"]
    assert got["clouds"] == 1
    assert got["points"] == MAX_FRAGMENTS
    assert got["intervals"] == 1
    assert got["junkTried"] == 1
    line = " ".join(got["cloudline"])
    assert f"Drawing {MAX_FRAGMENTS} of 451 fragments" in line


def test_cloud_is_removed_and_its_timer_stopped_on_close_clear_rerun_and_new_event(ran):
    got = ran["breakup_lifecycle"]
    assert got["firstRun"] == {"clouds": 1, "removed": 0, "intervals": 1, "clearDisabled": False}
    # the old cloud goes the moment a second run starts, before its answer arrives
    assert got["secondRunWaiting"]["clouds"] == 0
    assert got["secondRunWaiting"]["intervals"] == 0
    assert got["secondRunWaiting"]["removed"] == 1
    assert got["secondRunDone"] == {
        "clouds": 1,
        "removed": 1,
        "intervals": 1,
        "clearDisabled": False,
    }
    assert got["cleared"]["clouds"] == 0 and got["cleared"]["intervals"] == 0
    assert got["cleared"]["clearDisabled"] is True
    assert any("cleared" in line.lower() for line in got["clearedLine"])
    assert got["beforeClose"]["clouds"] == 1
    assert got["closed"]["clouds"] == 0 and got["closed"]["intervals"] == 0
    assert got["closedEmpty"] is True
    assert got["closedButtons"] == [False, False]
    assert got["beforeOther"]["clouds"] == 1
    assert got["otherOpened"]["clouds"] == 0 and got["otherOpened"]["intervals"] == 0
    assert got["otherEmpty"] is True
    assert got["callsAfter"] == got["callsBefore"]  # opening an event sends nothing


def test_without_satellite_js_the_summary_still_shows_and_nothing_is_drawn(ran):
    got = ran["no_satellite_library"]
    assert got["clouds"] == 0 and got["intervals"] == 0
    assert any("cannot be drawn" in n for n in got["notes"])
    assert any("Catastrophic" in n for n in got["notes"])
    assert got["status"] == ""


# --- failures, late answers, static mode ---

ACTIONS = ("compare", "maneuver", "breakup")


@pytest.mark.parametrize("action", ACTIONS)
def test_http_422_gets_one_plain_line(ran, action):
    listed = ran["errors"][f"{action}:invalid"]
    assert listed["status"].endswith("The server rejected rel_velocity_km_s: Field required.")
    text = ran["errors"][f"{action}:unprocessable"]["status"]
    assert "relative velocity is zero; the encounter plane is undefined." in text
    for case in (listed, ran["errors"][f"{action}:unprocessable"]):
        assert case["isError"] is True and "\n" not in case["status"]


@pytest.mark.parametrize("action", ACTIONS)
def test_http_500_gets_one_plain_line_whatever_the_body(ran, action):
    for kind in ("server500", "server500json"):
        case = ran["errors"][f"{action}:{kind}"]
        assert case["status"].endswith("The server failed to answer (HTTP 500).")
        assert case["isError"] is True
        assert "Traceback" not in case["status"] and "\n" not in case["status"]


@pytest.mark.parametrize("action", ACTIONS)
def test_a_rejected_fetch_gets_one_plain_line(ran, action):
    case = ran["errors"][f"{action}:network"]
    assert case["status"].endswith("Could not reach the server.")
    assert case["isError"] is True


@pytest.mark.parametrize("action", ACTIONS)
def test_a_timeout_gets_one_plain_line(ran, action):
    case = ran["errors"][f"{action}:timeout"]
    assert case["status"].endswith("The server did not answer within 45 s.")
    assert case["isError"] is True


@pytest.mark.parametrize("action", ACTIONS)
def test_an_unreadable_or_oddly_shaped_answer_gets_one_plain_line(ran, action):
    for kind in ("unreadable", "wrongShape"):
        case = ran["errors"][f"{action}:{kind}"]
        assert case["isError"] is True and "\n" not in case["status"]
        assert case["status"].endswith("not in the form the panel expected.")


def test_every_failure_leaves_the_button_enabled_the_timer_gone_and_nothing_drawn(ran):
    assert len(ran["errors"]) == len(ACTIONS) * 8
    for name, case in ran["errors"].items():
        assert case["enabled"] is True, name
        assert case["timeoutsLeft"] == 0, name
        assert case["nothingDrawn"] is True, name


def test_every_failure_names_the_analysis_that_failed(ran):
    lead = {"compare": "Pc comparison", "maneuver": "Avoidance options", "breakup": "Breakup"}
    for name, case in ran["errors"].items():
        action = name.split(":")[0]
        assert case["status"].startswith(lead[action]), name


def test_the_timeout_follows_the_page_override_and_a_late_answer_draws_nothing(ran):
    got = ran["timeout_budget"]
    assert got["armed"] == [2000]
    assert got["status"].endswith("The server did not answer within 2 s.")
    assert got["aborted"] is True
    assert got["statusAfterLateAnswer"] == got["status"]
    assert got["drawn"] is False


@pytest.mark.parametrize("action", ACTIONS)
def test_an_answer_after_close_or_after_another_event_opened_renders_nothing(ran, action):
    got = ran["stale"][action]
    for case in ("closed", "swapped", "silentSwap"):
        assert got[case]["empty"] is True, case
        assert got[case]["clouds"] == 0, case
        assert got[case]["intervals"] == 0, case
    assert got["closed"]["status"] == "" and got["swapped"]["status"] == ""
    assert got["closed"]["enabled"] is True and got["swapped"]["enabled"] is True


@pytest.mark.parametrize("action", ACTIONS)
def test_a_fresh_run_on_the_next_event_still_renders(ran, action):
    fresh = ran["stale"][action]["fresh"]
    assert fresh["empty"] is False
    assert fresh["clouds"] == (1 if action == "breakup" else 0)


def test_a_superseded_request_cannot_disturb_the_run_that_replaced_it(ran):
    got = ran["superseded"]
    assert got["firstAborted"] is True and got["secondAborted"] is False
    assert got["mid"]["disabled"] is True
    assert got["mid"]["status"].startswith("Comparing") and got["mid"]["isError"] is False
    assert got["mid"]["calls"] == 2 and got["mid"]["callsAfterExtraClick"] == 2
    assert got["mid"]["rowsAfterFirst"] == 0
    assert got["final"] == {"rows": 4, "disabled": False, "status": ""}


def test_static_mode_shows_one_plain_line_and_makes_no_request(ran):
    got = ran["static_mode"]
    assert got["calls"] == 0
    assert got["buttons"] == 0
    assert got["lines"] == 1
    assert got["listeners"] == 0
    assert got["hasDestroy"] is True
    assert "live server" in got["text"] and "\n" not in got["text"]
    assert len(got["text"]) < 160


def test_clicking_with_no_open_event_says_so_and_sends_nothing(ran):
    got = ran["no_event"]
    assert got["calls"] == 0
    assert all("Open a conjunction" in s for s in got["statuses"])
    assert got["enabled"] == [True, True, True]


def test_a_second_click_while_busy_sends_no_second_request(ran):
    assert ran["double_fire"]["calls"] == 1


def test_requests_go_to_the_api_prefix_the_page_gives(ran):
    assert ran["api_prefix"] == [
        "http://svc.test/pc/compare",
        "http://svc.test/maneuver",
        "http://svc.test/breakup/simulate",
    ]


# --- hostile text, lifecycle, styles ---


def test_hostile_names_and_server_text_are_rendered_as_text_never_html(ran):
    got = ran["hostile"]
    assert got["calls"] == 3
    # an innerHTML assignment would have thrown inside the scenario and failed the read
    assert set(got["tags"]) <= {"DIV", "SPAN", "BUTTON", "H4", "P", "UL", "LI"}
    text = got["text"]
    assert "<img src=x onerror=alert(1)>" in text
    assert "</div><script>alert(2)</script>" in text
    assert "<script>alert(3)</script> dilution" in text
    assert "<b>along</b>" in text


def test_a_second_init_replaces_the_first_and_destroy_leaves_nothing_behind(ran):
    got = ran["lifecycle_listeners"]
    assert got["panels"] == 1
    assert got["afterSecond"] == got["afterFirst"] > 0
    assert got["cloudsBefore"] == 1 and got["cloudsAfter"] == 0
    assert got["intervals"] == 0
    assert got["listeners"] == 0
    assert got["mountChildren"] == 0
    assert got["survivedEvents"] is True


def test_init_reports_failure_as_null_and_never_throws(ran):
    got = ran["init_failures"]
    assert got == {"empty": None, "nothing": None, "broken": None}


def test_one_style_block_however_many_inits(ran):
    got = ran["styles"]
    assert got["count"] == 1
    assert got["id"] == "conjactions-style"
    assert "var(--hud" in got["css"]


# --- the bodies the JS builds are accepted by the real routes ---


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real pc, analysis and history routers on a bare app over the demo catalog.

    The TestClient is deliberately not entered as a context manager: that would run the
    app lifespan, which loads the catalog from the network.
    """
    import sda.api
    from sda.routes.analysis import router as analysis_router
    from sda.routes.history import router as history_router
    from sda.routes.pc import router as pc_router

    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    monkeypatch.setattr(sda.api, "store", store)

    app = FastAPI()
    for router in (pc_router, analysis_router, history_router):
        app.include_router(router)
    return TestClient(app)


def _post(client: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(path, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("key", ["compare", "plainCompare"])
def test_the_real_pc_route_accepts_the_compare_body(client, ran, key):
    live = _post(client, "/pc/compare", ran["bodies"][key]["body"])
    assert set(live) == set(COMPARE_OK)
    assert set(live["probabilities"]) == {"foster", "chan", "alfano", "monte_carlo"}
    assert live["hard_body_radius_km"] == 0.02
    if key == "compare":
        assert live["assumptions"] == []
        for method in ("foster", "chan", "alfano"):
            assert live["probabilities"][method] == pytest.approx(
                COMPARE_OK["probabilities"][method], rel=1e-9
            )
    else:
        assert len(live["assumptions"]) == 2


@pytest.mark.parametrize("key", ["maneuver", "plainManeuver"])
def test_the_real_maneuver_route_accepts_the_maneuver_body(client, ran, key):
    live = _post(client, "/maneuver", ran["bodies"][key]["body"])
    assert set(live) == set(MANEUVER_TARGET_10)
    assert live["target_miss_km"] == MANEUVER_TARGET_KM
    assert live["warning"] is None
    deltas = sorted(o["delta_v_m_s"] for o in live["options"])
    assert deltas == sorted(o["delta_v_m_s"] for o in MANEUVER_TARGET_10["options"])


@pytest.mark.parametrize("key", ["breakup", "plainBreakup"])
def test_the_real_breakup_route_accepts_the_breakup_body(client, ran, key):
    live = _post(client, "/breakup/simulate", ran["bodies"][key]["body"])
    assert set(live) == set(BREAKUP_BASE) | {"tle_text"}
    assert live["catastrophic"] is True
    assert live["tles_fitted"] > 0
    sets = [ln for ln in live["tle_text"].splitlines() if ln.startswith("1 ")]
    assert 0 < len(sets) <= MAX_FRAGMENTS
