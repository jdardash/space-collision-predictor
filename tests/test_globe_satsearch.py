"""Tests for the globe satellite search module (static/js/satsearch.js).

The module is plain browser JavaScript with no bundler, so these tests run it
under Node: `global.window = global; require(<path>)`, then print JSON. They are
offline and deterministic, and skip cleanly when Node is not installed.

`rank` is the pure function behind the search box and is covered directly. The
`init` panel is covered through a small hand-written DOM stub (see FAKE_DOM),
because jsdom is not a dependency of this repo.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "sda"
    / "templates"
    / "static"
    / "js"
    / "satsearch.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_node(body: str, tmp_path: Path, prelude: str = ""):
    """Load the module under Node, run `body`, and return its JSON stdout.

    `body` sees `S` (window.SDASatSearch), `N` (the module's `module.exports`, which
    carries the alpha-5 helpers without widening the window surface) and must end by
    calling `out(value)`.
    """
    script = (
        "global.window = global;\n"
        + prelude
        + "\nvar N = require("
        + json.dumps(MODULE.as_posix())
        + ");\n"
        + "var S = window.SDASatSearch;\n"
        + "function out(v) { process.stdout.write(JSON.stringify(v)); }\n"
        + body
    )
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def ids(result):
    return [r["noradId"] for r in result["results"]]


# --- syntax and exports ---


def test_node_check_passes():
    proc = subprocess.run(
        [NODE, "--check", str(MODULE)], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_module_exports_init_rank_focus(tmp_path):
    info = run_node(
        "out(Object.keys(S).sort().map(function (k) { return [k, typeof S[k]]; }));", tmp_path
    )
    assert info == [["focus", "function"], ["init", "function"], ["rank", "function"]]


# --- rank: id matching ---


def test_exact_norad_id_beats_prefix(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 25545, name: 'AAA' },
          { noradId: 2554, name: 'ZZZ' },
          { noradId: 25544, name: 'MMM' },
          { noradId: 99, name: 'NO MATCH' }
        ];
        out(S.rank('2554', sats, 12));
        """,
        tmp_path,
    )
    # the exact id wins despite the alphabetically last name; the two prefix
    # hits then follow the within-tier rule (name first, so AAA before MMM)
    assert ids(result) == [2554, 25545, 25544]
    assert result["total"] == 3


def test_id_prefix_tier_orders_by_name_then_id(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 4002, name: 'BETA' },
          { noradId: 4003, name: 'ALPHA' },
          { noradId: 4001, name: 'ALPHA' }
        ];
        out(S.rank('400', sats, 12));
        """,
        tmp_path,
    )
    assert ids(result) == [4001, 4003, 4002]


def test_zero_padded_id_matches_exact(tmp_path):
    # TLE files print ids as five digits, so people type "00005" for id 5.
    result = run_node(
        "out(S.rank('00005', [{ noradId: 50, name: 'A' }, { noradId: 5, name: 'B' }], 12));",
        tmp_path,
    )
    assert ids(result)[0] == 5


def test_digit_query_also_finds_names_after_id_matches(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 28654, name: 'NOAA 19' },
          { noradId: 19, name: 'EARLY' },
          { noradId: 190, name: 'EARLY TWO' },
          { noradId: 77777, name: 'COSMOS 2119' }
        ];
        out(S.rank('19', sats, 12));
        """,
        tmp_path,
    )
    # exact id, id prefix, then the name hits (word-prefix before substring)
    assert ids(result) == [19, 190, 28654, 77777]


# --- rank: name matching ---


def test_name_tiers_in_order(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 1, name: 'MASTARX 9' },
          { noradId: 2, name: 'FOO STAR-2' },
          { noradId: 3, name: 'STARLINK-1' },
          { noradId: 4, name: 'STAR' },
          { noradId: 5, name: 'UNRELATED' }
        ];
        out(S.rank('star', sats, 12));
        """,
        tmp_path,
    )
    # exact, prefix, word-prefix, substring
    assert ids(result) == [4, 3, 2, 1]
    assert result["total"] == 4


def test_case_insensitive_both_ways(tmp_path):
    result = run_node(
        """
        var sats = [{ noradId: 25544, name: 'ISS (ZARYA)' }, { noradId: 7, name: 'iss two' }];
        out([S.rank('iss', sats, 12), S.rank('IsS', sats, 12), S.rank('zArYa', sats, 12)]);
        """,
        tmp_path,
    )
    lower, mixed, word = result
    assert ids(lower) == ids(mixed) == [7, 25544]
    assert ids(word) == [25544]


def test_name_order_within_a_tier_is_name_then_id(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 30, name: 'DELTA 2' },
          { noradId: 20, name: 'DELTA 1' },
          { noradId: 10, name: 'DELTA 2' }
        ];
        out(S.rank('delta', sats, 12));
        """,
        tmp_path,
    )
    assert ids(result) == [20, 10, 30]


def test_punctuation_and_spacing_are_interchangeable(tmp_path):
    result = run_node(
        """
        var sats = [{ noradId: 44713, name: 'STARLINK-1007' }];
        out([S.rank('starlink 1007', sats, 12), S.rank('starlink-1007', sats, 12),
             S.rank('1007', sats, 12)]);
        """,
        tmp_path,
    )
    assert [ids(r) for r in result] == [[44713]] * 3


# --- rank: limit, total, empties, bad data ---


def test_limit_and_total(tmp_path):
    result = run_node(
        """
        var sats = [];
        for (var i = 0; i < 40; i++) sats.push({ noradId: 1000 + i, name: 'SAT-' + i });
        out([S.rank('sat', sats, 5), S.rank('sat', sats), S.rank('sat', sats, 100)]);
        """,
        tmp_path,
    )
    five, default, wide = result
    assert len(five["results"]) == 5 and five["total"] == 40
    assert len(default["results"]) == 12 and default["total"] == 40
    assert len(wide["results"]) == 40 and wide["total"] == 40


def test_results_are_the_original_records(tmp_path):
    result = run_node(
        """
        var rec = { noradId: 25544, name: 'ISS', regime: 'LEO' };
        var got = S.rank('iss', [rec], 12).results[0];
        out({ same: got === rec });
        """,
        tmp_path,
    )
    assert result == {"same": True}


@pytest.mark.parametrize("query", ["''", "'   '", "null", "undefined", "'()'", "'--'"])
def test_empty_query_returns_nothing(tmp_path, query):
    result = run_node(
        "out(S.rank(" + query + ", [{ noradId: 1, name: 'ISS' }], 12));", tmp_path
    )
    assert result == {"results": [], "total": 0}


def test_bad_records_do_not_throw(tmp_path):
    result = run_node(
        """
        var sats = [
          null, undefined, 7, 'text',
          { noradId: 111 },
          { noradId: 112, name: null },
          { noradId: 113, name: 42 },
          { noradId: 114, name: { toString: function () { throw new Error('boom'); } } },
          { noradId: 'x', name: 'ISS' },
          { name: 'ISS ALSO' },
          { noradId: 25544, name: 'ISS (ZARYA)' }
        ];
        out({
          text: S.rank('iss', sats, 12),
          digits: S.rank('11', sats, 12),
          none: S.rank('iss', undefined, 12),
          notArray: S.rank('iss', { length: 'x' }, 12)
        });
        """,
        tmp_path,
    )
    # exact name first, then the two prefix hits ordered by name; a record with
    # no id (JSON drops the undefined key) still matches on its name
    assert [r.get("noradId") for r in result["text"]["results"]] == ["x", None, 25544]
    assert result["text"]["total"] == 3
    assert ids(result["digits"]) == [111, 112, 113, 114]
    assert result["none"] == {"results": [], "total": 0}
    assert result["notArray"] == {"results": [], "total": 0}


# --- init: the sidebar panel, against a hand-written DOM stub ---

# Just enough DOM for the panel: elements with children, attributes, class
# names, listeners and focus. Events are delivered to the target element only
# (no bubbling); the event objects record stopPropagation so the tests can
# check that keys typed in the box never reach the page's global handler.
# Assigning innerHTML throws, which turns "never innerHTML" into a test.
FAKE_DOM = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = [];
  this.attrs = {};
  this.listeners = {};
  this.style = {};
  this.className = '';
  this.value = '';
  this.parent = null;
  this._text = '';
  this.focused = false;
}
El.prototype.appendChild = function (c) { c.parent = this; this.children.push(c); return c; };
El.prototype.removeChild = function (c) {
  this.children = this.children.filter(function (x) { return x !== c; });
  return c;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.focus = function () { this.focused = true; };
El.prototype.blur = function () { this.focused = false; };
El.prototype.select = function () {};
El.prototype.scrollIntoView = function () {};
Object.defineProperty(El.prototype, 'firstChild', {
  get: function () { return this.children[0] || null; }
});
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
  ev.stopped = false;
  ev.prevented = false;
  ev.stopPropagation = function () { ev.stopped = true; };
  ev.stopImmediatePropagation = function () { ev.stopped = true; ev.immediate = true; };
  ev.preventDefault = function () { ev.prevented = true; };
  var fns = this.listeners[type] || [];
  for (var i = 0; i < fns.length; i++) { fns[i].call(this, ev); if (ev.immediate) break; }
  return ev;
};
function all(el, out) {
  out = out || [];
  out.push(el);
  el.children.forEach(function (c) { all(c, out); });
  return out;
}
function byClass(el, cls) { return all(el).filter(function (x) { return x.has(cls); }); }
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function main(fn) {
  fn().catch(function (e) { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
}

global.document = {
  head: new El('head'),
  createElement: function (tag) { return new El(tag); },
  getElementById: function (id) {
    return this.head.children.filter(function (c) { return c.id === id; })[0] || null;
  }
};
global.Cesium = {
  Cartesian3: { fromDegrees: function (lon, lat, h) { return { lon: lon, lat: lat, h: h }; } }
};

var ISS = { noradId: 25544, name: 'ISS (ZARYA)', regime: 'LEO', lastLat: 12.5, lastLon: -45.25,
            lastAlt: 420.4, lastVel: 7.66 };
var HOSTILE = { noradId: 31337, name: '<img src=x onerror=alert(1)>', regime: 'MEO',
                lastLat: 1, lastLon: 2, lastAlt: 20200, lastVel: 3.9 };
var NOPOS = { noradId: 90001, name: 'FRESH OBJECT', regime: 'LEO', lastLat: 0, lastLon: 0,
              lastAlt: 0, lastVel: 0 };

function makeCtx(sats) {
  var calls = { select: [], fly: [], feed: [] };
  var ctx = {
    viewer: { camera: { flyTo: function (o) { calls.fly.push(o); } } },
    satEngine: { sats: sats || [ISS, HOSTILE, NOPOS], byNoradId: {} },
    selectSatellite: function (id) { calls.select.push(id); },
    addIntelItem: function (t) { calls.feed.push(t); },
    api: '',
    isStatic: false,
    mount: new El('div')
  };
  return { ctx: ctx, calls: calls };
}
function input(ctx) { return byClass(ctx.mount, 'satsearch-input')[0]; }
function rows(ctx) { return byClass(ctx.mount, 'satsearch-row'); }
function status(ctx) { return byClass(ctx.mount, 'satsearch-status')[0].textContent; }
function key(ctx, k) { return input(ctx).fire('keydown', { key: k }); }
async function type(ctx, text) {
  var box = input(ctx);
  box.value = text;
  box.fire('input', {});
  await sleep(250);
}
"""


def run_panel(body: str, tmp_path: Path):
    return run_node("main(async function () {\n" + body + "\n});", tmp_path, prelude=FAKE_DOM)


def test_init_builds_one_panel_and_one_style(tmp_path):
    info = run_panel(
        """
        var a = makeCtx(), b = makeCtx();
        S.init(a.ctx);
        S.init(b.ctx);
        var panel = a.ctx.mount.children[0];
        out({
          panels: a.ctx.mount.children.length,
          cls: panel.className,
          h3: panel.children[0].textContent,
          body: panel.children[1].className,
          inputs: byClass(a.ctx.mount, 'satsearch-input').length,
          styles: document.head.children.length
        });
        """,
        tmp_path,
    )
    assert info["panels"] == 1
    assert info["cls"].split() == ["panel"]
    assert info["h3"].strip()
    assert info["body"] == "panel-body"
    assert info["inputs"] == 1
    assert info["styles"] == 1


def test_typing_renders_rows_after_the_debounce(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        S.init(k.ctx);
        var box = input(k.ctx);
        box.value = 'iss';
        box.fire('input', {});
        var before = rows(k.ctx).length;   // nothing yet: the query is debounced
        await sleep(250);
        out({ before: before, rows: rows(k.ctx).map(function (r) { return r.textContent; }) });
        """,
        tmp_path,
    )
    assert info["before"] == 0
    assert len(info["rows"]) == 1
    row = info["rows"][0]
    for part in ("ISS (ZARYA)", "25544", "LEO", "420 km"):
        assert part in row


def test_names_are_rendered_as_text_never_html(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        S.init(k.ctx);
        await type(k.ctx, 'img');
        var names = byClass(k.ctx.mount, 'satsearch-name');
        out({ names: names.map(function (n) { return n.textContent; }),
              kids: names.map(function (n) { return n.children.length; }) });
        """,
        tmp_path,
    )
    assert info["names"] == ["<img src=x onerror=alert(1)>"]
    assert info["kids"] == [0]


def test_enter_selects_flies_and_logs_once(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        S.init(k.ctx);
        await type(k.ctx, 'iss');
        var ev = key(k.ctx, 'Enter');
        out({ calls: k.calls, stopped: ev.stopped });
        """,
        tmp_path,
    )
    calls = info["calls"]
    assert calls["select"] == [25544]
    assert len(calls["fly"]) == 1
    dest = calls["fly"][0]["destination"]
    assert dest["lon"] == -45.25 and dest["lat"] == 12.5
    # metres: the satellite's own height plus a standoff of at least 1000 km
    assert dest["h"] >= (420.4 + 1000) * 1000
    assert isinstance(calls["fly"][0]["duration"], (int, float))
    assert len(calls["feed"]) == 1
    assert "ISS (ZARYA)" in calls["feed"][0] and "25544" in calls["feed"][0]
    assert info["stopped"] is True


def test_enter_runs_a_pending_query_without_waiting(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        S.init(k.ctx);
        var box = input(k.ctx);
        box.value = '25544';
        box.fire('input', {});
        key(k.ctx, 'Enter');      // before the debounce timer has fired
        out(k.calls.select);
        """,
        tmp_path,
    )
    assert info == [25544]


def test_satellite_without_a_position_is_selected_but_not_flown_to(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        S.init(k.ctx);
        await type(k.ctx, 'fresh');
        key(k.ctx, 'Enter');
        out(k.calls);
        """,
        tmp_path,
    )
    assert info["select"] == [90001]
    assert info["fly"] == []
    assert len(info["feed"]) == 1


def test_arrow_keys_move_the_highlight_and_wrap(tmp_path):
    info = run_panel(
        """
        var k = makeCtx([
          { noradId: 1, name: 'STAR A', lastLat: 1, lastLon: 1, lastAlt: 500 },
          { noradId: 2, name: 'STAR B', lastLat: 1, lastLon: 1, lastAlt: 500 },
          { noradId: 3, name: 'STAR C', lastLat: 1, lastLon: 1, lastAlt: 500 }
        ]);
        S.init(k.ctx);
        await type(k.ctx, 'star');
        function active() {
          return rows(k.ctx).map(function (r) { return r.has('satsearch-active'); })
            .indexOf(true);
        }
        var start = active();
        key(k.ctx, 'ArrowDown');
        var down = active();
        key(k.ctx, 'ArrowUp');
        key(k.ctx, 'ArrowUp');           // wraps from the top to the last row
        var wrapped = active();
        key(k.ctx, 'Enter');
        out({ start: start, down: down, wrapped: wrapped, selected: k.calls.select });
        """,
        tmp_path,
    )
    assert info == {"start": 0, "down": 1, "wrapped": 2, "selected": [3]}


def test_clicking_a_row_selects_that_row(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        S.init(k.ctx);
        await type(k.ctx, 'o');           // HOSTILE (onerror) and FRESH OBJECT
        var found = rows(k.ctx).length;
        rows(k.ctx)[1].fire('click', {});
        out({ found: found, select: k.calls.select });
        """,
        tmp_path,
    )
    assert info["found"] == 2
    assert len(info["select"]) == 1


def test_escape_clears_then_blurs_and_every_key_stays_in_the_box(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        S.init(k.ctx);
        var box = input(k.ctx);
        box.focus();
        await type(k.ctx, 'iss');
        var kept = ['a', '1', '/', 's', 'ArrowDown', 'ArrowUp', 'Enter'].map(function (name) {
          return key(k.ctx, name).stopped;
        });
        var first = key(k.ctx, 'Escape');
        var afterFirst = { value: box.value, rows: rows(k.ctx).length, focused: box.focused };
        var second = key(k.ctx, 'Escape');
        out({ kept: kept, first: first.stopped, second: second.stopped,
              afterFirst: afterFirst, focusedAfter: box.focused });
        """,
        tmp_path,
    )
    assert all(info["kept"])
    assert info["first"] and info["second"]
    assert info["afterFirst"] == {"value": "", "rows": 0, "focused": True}
    assert info["focusedAfter"] is False


def test_status_line_for_no_match_overflow_and_empty_catalog(tmp_path):
    info = run_panel(
        """
        var many = [];
        for (var i = 0; i < 40; i++) {
          many.push({ noradId: 5000 + i, name: 'SAT-' + i, lastLat: 1, lastLon: 1, lastAlt: 600 });
        }
        var k = makeCtx(many);
        S.init(k.ctx);
        await type(k.ctx, 'sat');
        var overflow = { shown: rows(k.ctx).length, status: status(k.ctx) };
        await type(k.ctx, 'zzzz');
        var none = { shown: rows(k.ctx).length, status: status(k.ctx) };
        var e = makeCtx([]);
        S.init(e.ctx);
        await type(e.ctx, 'iss');
        out({ overflow: overflow, none: none, empty: status(e.ctx) });
        """,
        tmp_path,
    )
    assert info["overflow"]["shown"] == 12
    assert "28 more" in info["overflow"]["status"]
    assert info["none"]["shown"] == 0
    assert "No satellite" in info["none"]["status"] and "zzzz" in info["none"]["status"]
    assert "loading" in info["empty"].lower()


def test_catalog_is_read_again_on_every_query(tmp_path):
    info = run_panel(
        """
        var k = makeCtx([{ noradId: 1, name: 'OLD ONE' }]);
        S.init(k.ctx);
        await type(k.ctx, 'new');
        var before = rows(k.ctx).length;
        k.ctx.satEngine.sats = [
          { noradId: 2, name: 'NEW TWO', lastLat: 1, lastLon: 1, lastAlt: 9 }
        ];
        await type(k.ctx, 'new');
        out({ before: before, after: rows(k.ctx).length });
        """,
        tmp_path,
    )
    assert info == {"before": 0, "after": 1}


def test_nothing_the_page_supplies_can_break_the_handler(tmp_path):
    info = run_panel(
        """
        var k = makeCtx();
        k.ctx.selectSatellite = function () { throw new Error('select failed'); };
        k.ctx.addIntelItem = function () { throw new Error('feed failed'); };
        k.ctx.viewer.camera.flyTo = function () { throw new Error('fly failed'); };
        S.init(k.ctx);
        await type(k.ctx, 'iss');
        key(k.ctx, 'Enter');                       // must not throw

        var bad = makeCtx();
        Object.defineProperty(bad.ctx.satEngine, 'sats', {
          get: function () { throw new Error('engine gone'); }
        });
        S.init(bad.ctx);
        await type(bad.ctx, 'iss');                 // must not throw

        var noMount = makeCtx();
        noMount.ctx.mount = null;
        S.init(null);
        S.init({});
        S.init(noMount.ctx);
        out({ survived: true, rowsAfterBad: rows(bad.ctx).length });
        """,
        tmp_path,
    )
    assert info["survived"] is True
    assert info["rowsAfterBad"] == 0


def test_focus_expands_the_panel_and_focuses_the_input(tmp_path):
    info = run_panel(
        """
        var before = S.focus();                    // not initialised yet: no throw
        var k = makeCtx();
        S.init(k.ctx);
        var panel = k.ctx.mount.children[0];
        var head = panel.children[0];
        var click = head.fire('click', { target: head });
        var collapsed = panel.has('collapsed');
        var focused = S.focus();
        out({ before: before, collapsed: collapsed, stoppedClick: click.stopped,
              afterCollapsed: panel.has('collapsed'), focused: focused,
              inputFocused: input(k.ctx).focused });
        """,
        tmp_path,
    )
    assert info["before"] is False
    assert info["collapsed"] is True and info["stoppedClick"] is True
    assert info["afterCollapsed"] is False
    assert info["focused"] is True and info["inputFocused"] is True


# --- alpha-5 NORAD ids ---
#
# Space-Track writes ids above 99,999 as a letter and four digits (A0001 = 100001),
# and the letters I and O are skipped. The decode is ported from satlas
# (apps/web/src/lib/norad.ts, MIT); the helpers are reached through module.exports so
# the window surface stays { init, rank, focus }.

ALPHA5_VALID = [
    ("A0000", 100000),
    ("A0001", 100001),
    ("B0000", 110000),
    ("H9999", 179999),
    ("J0000", 180000),  # the alphabet skips I
    ("N9999", 229999),
    ("P0000", 230000),  # and skips O
    ("T0000", 270000),
    ("Z9999", 339999),
]
PLAIN_IDS = [
    ("25544", 25544),
    ("06707", 6707),
    ("00000", 0),
    ("0", 0),
    ("99999", 99999),
    ("100001", 100001),
    (" 25544 ", 25544),
]
ALPHA5_INVALID = [
    "I0001",  # I and O are not alpha-5 letters
    "O0001",
    "a0001",  # lower case
    "i0001",
    "A001",  # too short
    "A00001",  # too long
    "A1",
    "AA001",
    "A00O1",
    "A-001",
    "1A234",
    "ABCDE",
    "A 001",
    "",
    "   ",
    "Z",
]


@pytest.mark.parametrize(("raw", "expected"), ALPHA5_VALID + PLAIN_IDS)
def test_norad_to_int_decodes_alpha5_and_plain_ids(tmp_path, raw, expected):
    got = run_node("out(N.noradToInt(" + json.dumps(raw) + "));", tmp_path)
    assert got == expected


def test_norad_to_int_rejects_what_is_not_an_id(tmp_path):
    got = run_node(
        "var bad = " + json.dumps(ALPHA5_INVALID) + ";\n"
        "bad = bad.concat([null, undefined, -1, 1.5, NaN, {}, true]);\n"
        "out(bad.map(function (v) { return Number.isNaN(N.noradToInt(v)); }));",
        tmp_path,
    )
    assert got == [True] * (len(ALPHA5_INVALID) + 7)


def test_norad_to_int_accepts_a_whole_number(tmp_path):
    assert run_node("out(N.noradToInt(25544));", tmp_path) == 25544


def test_norad_key_collapses_every_spelling_of_one_id(tmp_path):
    got = run_node(
        """
        out(['A0001', '100001', ' 100001 ', 100001, '06707', '6707', 6707, ' 25544 ']
            .map(N.noradKey));
        """,
        tmp_path,
    )
    assert got == ["100001", "100001", "100001", "100001", "6707", "6707", "6707", "25544"]


def test_norad_key_keeps_an_unparseable_id_instead_of_dropping_it(tmp_path):
    got = run_node(
        "out(['I0001', ' i0001 ', 'abc', null, undefined, ''].map(N.noradKey));", tmp_path
    )
    assert got == ["I0001", "i0001", "abc", "", "", ""]


def test_window_surface_is_unchanged_by_the_alpha5_helpers(tmp_path):
    info = run_node(
        "out({ keys: Object.keys(S).sort(), helpers: Object.keys(N).sort() });", tmp_path
    )
    assert info == {"keys": ["focus", "init", "rank"], "helpers": ["noradKey", "noradToInt"]}


def test_alpha5_query_finds_the_numeric_id(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 100002, name: 'NEXT' },
          { noradId: 100001, name: 'NEW SAT' },
          { noradId: 1, name: 'OLD' }
        ];
        out([S.rank('A0001', sats, 12), S.rank(' A0001 ', sats, 12)]);
        """,
        tmp_path,
    )
    assert [ids(r) for r in result] == [[100001], [100001]]
    assert result[0]["total"] == 1


def test_numeric_query_finds_a_record_whose_id_arrived_as_alpha5(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 'A0002', name: 'NEXT' },
          { noradId: 'A0001', name: 'NEW SAT' },
          { noradId: 25544, name: 'ISS' }
        ];
        out([S.rank('100001', sats, 12), S.rank('A0001', sats, 12), S.rank('a0001', sats, 12)]);
        """,
        tmp_path,
    )
    assert [ids(r) for r in result] == [["A0001"]] * 3


def test_alpha5_id_matches_by_prefix_of_the_decoded_number(tmp_path):
    result = run_node(
        """
        var sats = [{ noradId: 'A0001', name: 'NEW SAT' }, { noradId: 1000, name: 'KILO' }];
        out(S.rank('1000', sats, 12));
        """,
        tmp_path,
    )
    # the exact id first, then the alpha-5 record because 100001 starts with 1000
    assert ids(result) == [1000, "A0001"]


def test_alpha5_exact_id_beats_a_name_hit(tmp_path):
    result = run_node(
        """
        var sats = [
          { noradId: 1, name: 'A0001 RELAY' },
          { noradId: 100001, name: 'ZZ LAST ALPHABETICALLY' }
        ];
        out(S.rank('A0001', sats, 12));
        """,
        tmp_path,
    )
    assert ids(result) == [100001, 1]


@pytest.mark.parametrize("query", ["I0001", "O0001", "A001", "A00001", "B0001", "A0002"])
def test_a_query_that_is_not_this_alpha5_id_does_not_find_it(tmp_path, query):
    result = run_node(
        "out(S.rank(" + json.dumps(query) + ", [{ noradId: 100001, name: 'NEW SAT' }], 12));",
        tmp_path,
    )
    assert result == {"results": [], "total": 0}


def test_ordinary_digit_ranking_is_unchanged_by_alpha5_records(tmp_path):
    result = run_node(
        """
        var plain = [
          { noradId: 25545, name: 'AAA' }, { noradId: 2554, name: 'ZZZ' },
          { noradId: 25544, name: 'MMM' }
        ];
        var mixed = plain.concat([{ noradId: 'A0001', name: 'NEW SAT' }]);
        out([S.rank('2554', plain, 12), S.rank('2554', mixed, 12)]);
        """,
        tmp_path,
    )
    assert ids(result[0]) == ids(result[1]) == [2554, 25545, 25544]


def test_a_bare_letter_is_still_a_name_search(tmp_path):
    result = run_node(
        """
        var sats = [{ noradId: 100001, name: 'A STAR' }, { noradId: 7, name: 'BEE' }];
        out(S.rank('a', sats, 12));
        """,
        tmp_path,
    )
    assert ids(result) == [100001]


def test_alpha5_record_shows_the_decoded_id_and_flies_with_its_live_position(tmp_path):
    info = run_panel(
        """
        var k = makeCtx([{ noradId: 'A0001', name: 'NEW SAT', regime: 'LEO' }]);
        k.ctx.satEngine.byNoradId = {
          100001: { noradId: 100001, name: 'NEW SAT', regime: 'LEO',
                    lastLat: 5, lastLon: 6, lastAlt: 700 }
        };
        S.init(k.ctx);
        await type(k.ctx, 'a0001');
        var meta = byClass(k.ctx.mount, 'satsearch-meta')
          .map(function (n) { return n.textContent; });
        key(k.ctx, 'Enter');
        out({ meta: meta, select: k.calls.select, fly: k.calls.fly, feed: k.calls.feed });
        """,
        tmp_path,
    )
    assert info["meta"] == ["NORAD 100001 | LEO"]
    # the page is told the id its own index uses (the live record's), not the alpha-5 text
    assert info["select"] == [100001]
    assert len(info["fly"]) == 1
    dest = info["fly"][0]["destination"]
    assert dest["lat"] == 5 and dest["lon"] == 6
    assert len(info["feed"]) == 1 and "NORAD 100001" in info["feed"][0]
