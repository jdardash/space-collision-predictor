"""Hooks the globe page gives its static/js modules, run for real in Node.

A module can hide a set of satellites by setting ``hidden`` on the engine's
records; the page promises that every propagation path keeps a hidden dot
hidden, and that an object with no position is never drawn. The page also
announces a selection, the end of one, and the opening and closing of the
conjunction detail panel as DOM events.

The code under test is lifted out of worldview.html and executed against stubs:
functions by name, the detail panel as its whole section, and the satellite
record as the literal the catalog builder writes, so a field the page starts at
zero starts at zero here too.

They skip cleanly when Node is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "src" / "sda" / "templates" / "worldview.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

LIFTED = (
    "applyWorkerPositions",
    "updateSatPosition",
    "refreshSatVisibility",
    "applyServerTrack",
    "releaseServerTracked",
)

PANEL_SECTION = ("// ===== CONJUNCTION DETAIL PANEL =====", "// ===== TIMELINE STRIP =====")
RECORD_OPENS = "var s = {\n      noradId: rec.norad_id"

HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const rejections = [];
process.on('unhandledRejection', function (e) { rejections.push(String(e && e.name)); });

function makeWorld() {
  const box = {
    Float32Array: Float32Array,
    Cesium: {
      Math: { toDegrees: function (r) { return r * 180 / Math.PI; } },
      Cartesian3: {
        fromRadians: function (lon, lat, h) { return { lon: lon, lat: lat, h: h }; },
        fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h }; },
      },
    },
    satellite: {
      propagate: function (satrec) {
        return satrec.fails ? null : { position: { x: 7000, y: 0, z: 0 } };
      },
      eciToGeodetic: function () { return { longitude: 0.5, latitude: 0.25, height: 600 }; },
      degreesLat: function (r) { return r * 180 / Math.PI; },
      degreesLong: function (r) { return r * 180 / Math.PI; },
    },
    satrecFor: function (s) { return s.satrec || null; },
    satEngine: {
      sats: [], byNoradId: {}, workerPending: true, workerBuffer: null, lastLabelRebuild: 99,
    },
    layerState: { satellites: true },
    serverTrack: { tracked: {}, lastCount: 0, lastMessage: 0, state: 'live' },
    setServerTrackState: function () {},
    performance: { now: function () { return 0; } },
  };
  vm.createContext(box);
  vm.runInContext(input.source, box);
  return box;
}

// One record, built from the page's own literal and given the point the page adds.
function sat(extra) {
  const s = vm.runInNewContext('(' + input.record + ')', {
    rec: { norad_id: 1, name: 'OBJECT', regime: 'LEO', line1: '', line2: '' },
    color: {},
    satrec: null,
  });
  s.point = { show: false, position: null };
  return Object.assign(s, extra || {});
}
function shows(sats) { return sats.map(function (s) { return s.point.show; }); }
function floats(values) { return new Float32Array(values).buffer; }

const out = {};

// Worker path: one plain dot, one hidden, one with no SGP4 solution.
(function () {
  const box = makeWorld();
  const sats = [sat(), sat({ hidden: true }), sat()];
  box.satEngine.sats = sats;
  box.applyWorkerPositions({
    buffer: floats([0.1, 0.2, 500, 7.6, 0.3, 0.4, 600, 7.5, NaN, 0, 0, 0]), count: 3,
  });
  out.worker = {
    shows: shows(sats),
    hiddenStillTracked: sats[1].lastAlt === 600 && sats[1].point.position !== null,
  };
  // The filter changes its mind; nothing has ticked yet.
  sats[0].hidden = true;
  sats[1].hidden = false;
  box.refreshSatVisibility();
  out.refresh = { shows: shows(sats), lastLabelRebuild: box.satEngine.lastLabelRebuild };
})();

// A catalog that has only just loaded: a module sets its flags before anything
// has been propagated, which is what sda:catalog-loaded invites it to do.
(function () {
  const box = makeWorld();
  const sats = [sat(), sat({ hidden: true }), sat()];
  box.satEngine.sats = sats;
  box.refreshSatVisibility();
  out.fresh = { shows: shows(sats) };
})();

// An object that had a position and then lost its solution, on both paths.
(function () {
  const box = makeWorld();
  const viaWorker = sat();
  box.satEngine.sats = [viaWorker];
  box.applyWorkerPositions({ buffer: floats([0.1, 0.2, 500, 7.6]), count: 1 });
  const workerFirst = viaWorker.point.show;
  box.applyWorkerPositions({ buffer: floats([NaN, 0, 0, 0]), count: 1 });

  const viaFallback = sat({ satrec: {} });
  box.updateSatPosition(viaFallback, new Date(0), 0);
  const fallbackFirst = viaFallback.point.show;
  viaFallback.satrec = { fails: true };
  box.updateSatPosition(viaFallback, new Date(0), 0);

  box.satEngine.sats = [viaWorker, viaFallback];
  box.refreshSatVisibility();
  out.lost = { before: [workerFirst, fallbackFirst], after: shows([viaWorker, viaFallback]) };
})();

// Main-thread fallback path.
(function () {
  const box = makeWorld();
  const plain = sat({ satrec: {} });
  const hidden = sat({ satrec: {}, hidden: true });
  const broken = sat({ satrec: { fails: true } });
  [plain, hidden, broken].forEach(function (s) { box.updateSatPosition(s, new Date(0), 0); });
  out.fallback = { shows: shows([plain, hidden, broken]), hiddenAlt: hidden.lastAlt };
  plain.hidden = true;
  hidden.hidden = false;
  box.satEngine.sats = [plain, hidden, broken];
  box.refreshSatVisibility();
  out.fallback.refreshed = shows([plain, hidden, broken]);
})();

// Server track feed: it owns the dots it is subscribed to, and nothing local
// ever propagates them, so the feed alone gives them a position.
(function () {
  const box = makeWorld();
  const a = sat({ noradId: 1 });
  const b = sat({ noradId: 2, hidden: true });
  box.satEngine.sats = [a, b];
  box.satEngine.byNoradId = { 1: a, 2: b };
  const payload = {
    subscribed: [1, 2],
    satellites: [
      { norad_id: 1, lat: 10, lon: 20, alt_km: 500 },
      { norad_id: 2, lat: -10, lon: 40, alt_km: 700 },
    ],
  };
  box.applyServerTrack(payload);
  const first = shows([a, b]);
  a.hidden = true;
  b.hidden = false;
  box.refreshSatVisibility();
  const afterRefresh = shows([a, b]);
  box.applyServerTrack(payload);
  const second = shows([a, b]);
  a.hidden = false;
  box.layerState.satellites = false;
  box.applyServerTrack(payload);
  out.server = {
    first: first,
    afterRefresh: afterRefresh,
    second: second,
    layerOff: shows([a, b]),
    owned: a.serverTracked && b.serverTracked,
    hiddenStillTracked: a.lastAlt === 500 && a.point.position !== null,
  };
})();

// The conjunction detail panel, the whole section of the page.
function makePanel() {
  const els = {};
  const seen = [];
  const plots = [];
  function el() {
    const classes = new Set();
    return {
      children: [], style: {}, textContent: '', className: '',
      classList: {
        add: function (c) { classes.add(c); },
        remove: function (c) { classes.delete(c); },
        contains: function (c) { return classes.has(c); },
      },
      replaceChildren: function () { this.children = []; },
      appendChild: function (c) { this.children.push(c); },
    };
  }
  const box = {
    RISK_COLORS: { HIGH: '#ff8800' },
    IS_STATIC: true,
    API: '',
    openConjunction: null,
    $: function (id) { return els[id] || (els[id] = el()); },
    CustomEvent: function (type, init) {
      this.type = type;
      this.detail = init ? init.detail : undefined;
    },
  };
  box.window = box;
  box.document = {
    createElement: el,
    // What a listener finds when it asks the page which event is open.
    dispatchEvent: function (e) {
      const open = vm.runInContext('openConjunction', box);
      seen.push({
        type: e.type,
        detail: e.detail ? e.detail.primary : null,
        open: open ? open.primary : null,
      });
    },
  };
  vm.createContext(box);
  vm.runInContext(input.panel, box);
  return { box: box, els: els, seen: seen, plots: plots };
}

function conjunction(primary) {
  return {
    risk: 'HIGH', miss_distance_km: 0.8, relative_velocity_km_s: 12.1, pc: 1e-4,
    primary: primary, secondary: 99999,
    pc_history: [{ hours_before_tca: 24, pc: 1e-6 }, { hours_before_tca: 1, pc: 1e-4 }],
  };
}

function says(panel, id) {
  const kids = panel.els[id] ? panel.els[id].children : [];
  return kids.length ? kids[0].textContent : '';
}

function openOne(panel, primary) {
  let threw = null;
  panel.plots.length = 0;
  try { panel.box.openConjunctionDetail(conjunction(primary)); } catch (e) { threw = e.name; }
  const open = vm.runInContext('openConjunction', panel.box);
  return {
    threw: threw,
    visible: panel.els['conj-detail'].classList.contains('visible'),
    open: open ? open.primary : null,
    seen: panel.seen.splice(0),
    plots: panel.plots.slice(),
    says: { bplane: says(panel, 'cd-bplane'), trend: says(panel, 'cd-pc-trend') },
  };
}

(function () {
  const panel = makePanel();
  const working = {
    newPlot: function (id) { panel.plots.push(id); },
    purge: function () {},
  };
  out.panel = {};

  panel.box.Plotly = working;
  out.panel.with_charts = openOne(panel, 11111);

  // The operator clicks another event; the chart library never arrived (it
  // comes from a CDN) or has gone.
  delete panel.box.Plotly;
  out.panel.library_missing = openOne(panel, 22222);

  panel.box.Plotly = {
    newPlot: function () { throw new Error('no canvas'); },
    purge: function () {},
  };
  out.panel.draw_throws = openOne(panel, 33333);

  panel.box.Plotly = working;
  out.panel.charts_back = openOne(panel, 44444);

  panel.box.closeConjunctionDetail();
  const open = vm.runInContext('openConjunction', panel.box);
  out.close = {
    seen: panel.seen.splice(0),
    open: open,
    visible: panel.els['conj-detail'].classList.contains('visible'),
  };
})();

// A chart drawn from an async function fails as a rejection, a turn later.
setImmediate(function () {
  out.rejections = rejections;
  process.stdout.write(JSON.stringify(out));
});
"""


def _braced(page: str, start: int) -> str:
    """The text from *start* to the brace that closes the first one opened after it."""
    depth = 0
    for at in range(page.index("{", start), len(page)):
        if page[at] == "{":
            depth += 1
        elif page[at] == "}":
            depth -= 1
            if depth == 0:
                return page[start : at + 1]
    raise AssertionError(f"nothing closes the brace opened after {page[start : start + 40]!r}")


def _function_source(page: str, name: str) -> str:
    """The text of one top-level ``function name(...) { ... }`` from the page."""
    return _braced(page, page.index(f"\nfunction {name}(") + 1)


def _record_literal(page: str) -> str:
    """The object the catalog builder makes for every satellite, as the page writes it."""
    return _braced(page, page.index("{", page.index(RECORD_OPENS)))


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run(page) -> dict:
    opens, closes = PANEL_SECTION
    payload = {
        "source": "\n".join(_function_source(page, name) for name in LIFTED),
        "record": _record_literal(page),
        "panel": page[page.index(opens) : page.index(closes)],
    }
    done = subprocess.run(
        ["node", "-e", HARNESS],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_worker_positions_keep_a_hidden_dot_hidden(run):
    assert run["worker"]["shows"] == [True, False, False]
    # Hidden is a display choice: search and selection still need its position.
    assert run["worker"]["hiddenStillTracked"] is True


def test_refresh_applies_new_flags_without_waiting_for_a_tick(run):
    # The third object never had a solution, so it stays off whatever its flag says.
    assert run["refresh"]["shows"] == [False, True, False]
    assert run["refresh"]["lastLabelRebuild"] == 0, "labels must be rebuilt for the new set"


def test_refresh_before_the_first_tick_draws_nothing(run):
    """A new record sits at 0N 0E until it is propagated. Showing it there puts the
    whole catalog in one dot off the coast of Ghana."""
    assert run["fresh"]["shows"] == [False, False, False]


def test_refresh_keeps_an_object_that_lost_its_solution_off(run):
    assert run["lost"]["before"] == [True, True], "both had a position to begin with"
    assert run["lost"]["after"] == [False, False]


def test_main_thread_fallback_honours_the_flag_too(run):
    assert run["fallback"]["shows"] == [True, False, False]
    assert run["fallback"]["hiddenAlt"] == 600
    assert run["fallback"]["refreshed"] == [False, True, False]


def test_server_track_feed_honours_the_flag(run):
    server = run["server"]
    assert server["owned"] is True
    assert server["first"] == [True, False]
    assert server["second"] == [False, True], "the flag is read on every message"
    assert server["hiddenStillTracked"] is True
    assert server["layerOff"] == [False, False]


def test_refresh_shows_a_dot_only_the_server_feed_has_placed(run):
    assert run["server"]["afterRefresh"] == [False, True]


def test_opening_the_detail_panel_sets_the_event_before_announcing_it(run):
    opened = run["panel"]["with_charts"]
    assert opened["threw"] is None
    assert opened["visible"] is True
    # A listener asks getConjunction() from inside the event, so it must already answer.
    assert opened["seen"] == [{"type": "sda:conjunction-open", "detail": 11111, "open": 11111}]
    assert opened["plots"] == ["cd-bplane", "cd-pc-trend"]
    assert opened["says"] == {"bplane": "", "trend": ""}


@pytest.mark.parametrize(("case", "asked"), [("library_missing", 22222), ("draw_throws", 33333)])
def test_a_chart_that_cannot_draw_does_not_leave_the_previous_event_open(run, case, asked):
    """The panel on screen and the event the modules act on must be the same one.
    Before this held, a blocked chart library left the avoidance actions aimed at
    the pair opened earlier."""
    opened = run["panel"][case]
    assert opened["threw"] is None
    assert opened["visible"] is True
    assert opened["open"] == asked
    assert opened["seen"] == [{"type": "sda:conjunction-open", "detail": asked, "open": asked}]
    assert opened["plots"] == []
    assert opened["says"]["bplane"], "the empty chart box has to say why it is empty"
    assert opened["says"]["trend"]


def test_charts_return_once_the_library_does(run):
    opened = run["panel"]["charts_back"]
    assert opened["open"] == 44444
    assert opened["plots"] == ["cd-bplane", "cd-pc-trend"]
    # The message a failed draw left behind must not sit beside the chart.
    assert opened["says"] == {"bplane": "", "trend": ""}


def test_no_chart_failure_is_left_as_an_unhandled_rejection(run):
    assert run["rejections"] == []


def test_closing_the_detail_panel_clears_and_announces(run):
    assert run["close"] == {
        "seen": [{"type": "sda:conjunction-close", "detail": None, "open": None}],
        "open": None,
        "visible": False,
    }


def test_a_selection_is_announced(page):
    select = _function_source(page, "selectSatellite")
    assert "new CustomEvent('sda:satellite-selected'" in select
    assert "detail: { noradId: noradId, name: s.name }" in select


def test_dropping_a_selection_is_announced_once_it_is_dropped(page):
    """A filter that showed a hidden satellite only because it was selected has to
    hear when that stops, and must find the selection already cleared when it does."""
    clear = _function_source(page, "clearSatSelection")
    announce = "new CustomEvent('sda:satellite-deselected'"
    assert clear.count(announce) == 1
    assert "detail: { noradId: prevId }" in clear
    assert clear.index("satEngine.selectedId = null;") < clear.index(announce)
    # No selection, no event: selectSatellite() calls this on every pick.
    assert clear.index("if (prevId != null) {") < clear.index(announce)
    select = _function_source(page, "selectSatellite")
    assert select.index("clearSatSelection();") < select.index("'sda:satellite-selected'")


def test_a_replaced_catalog_is_announced(page):
    status = _function_source(page, "updateCatalogStatus")
    assert "new CustomEvent('sda:catalog-loaded'" in status
    assert "detail: { count: satEngine.sats.length }" in status


def test_modules_get_the_hooks_and_the_mounts(page):
    init = _function_source(page, "initExtensions")
    for hook in ("simNow: simNow", "refreshSatVisibility: refreshSatVisibility",
                 "getConjunction:", "getConjunctions:"):
        assert hook in init, hook
    for mount in ("ext-categories", "ext-isscam", "ext-conjactions"):
        assert page.count(f'<div id="{mount}"></div>') == 1, mount
