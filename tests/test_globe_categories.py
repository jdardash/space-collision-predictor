"""Tests for the globe constellation filter (static/js/categories.js).

The module is plain browser JavaScript with no bundler, so one Node process runs
every scenario inside `vm` contexts (a fresh context per scenario, so module state
never leaks), prints a single JSON document, and the tests below assert on slices
of it. They are offline and deterministic, and skip cleanly when Node is missing.

The panel is exercised against a hand-written DOM stub (see HARNESS), because jsdom
is not a dependency of this repo. Assigning innerHTML throws in the stub, which turns
"names are text, never markup" into a test.
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
    / "categories.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Pill order as the panel draws it (after "All"). Order is part of the contract: the
# row must not reshuffle when the catalog reloads.
KEYS = [
    "STARLINK",
    "ONEWEB",
    "KUIPER",
    "CHINA_MEGA",
    "GPS",
    "GLONASS",
    "GALILEO",
    "BEIDOU",
    "IRIDIUM",
    "STATIONS",
    "WEATHER",
    "GEO",
    "DEBRIS",
    "OTHER",
]

# Every name quoted in the task brief, plus the cases where rule order matters.
NAME_TABLE = [
    ("STARLINK-1234", "STARLINK"),
    ("STARLINK-1234 [DTC]", "STARLINK"),
    ("starlink-1007", "STARLINK"),
    ("ONEWEB-0012", "ONEWEB"),
    ("KUIPER-12", "KUIPER"),
    ("QIANFAN-3", "CHINA_MEGA"),
    ("HULIANWANG DIGUI-12", "CHINA_MEGA"),
    ("HULIANWANG JISHU SHIYAN*", "CHINA_MEGA"),
    ("GPS BIIR-2  (PRN 13)", "GPS"),
    ("NAVSTAR 43 (USA 132)", "GPS"),
    ("BIIF-12", "GPS"),
    ("GSAT0101 (GALILEO-PFM)", "GALILEO"),
    ("GALILEO 25", "GALILEO"),
    ("BEIDOU-3 M1", "BEIDOU"),
    ("BEIDOU-2 G4", "BEIDOU"),
    ("COSMOS 2545 (GLONASS-M)", "GLONASS"),
    ("COSMOS 2433 [GLONASS-M]", "GLONASS"),
    ("IRIDIUM 106", "IRIDIUM"),
    ("ISS (ZARYA)", "STATIONS"),
    ("ISS (NAUKA)", "STATIONS"),
    ("CSS (TIANHE)", "STATIONS"),
    ("CREW DRAGON 12", "STATIONS"),
    ("PROGRESS-MS 32", "STATIONS"),
    ("NOAA 19", "WEATHER"),
    ("NOAA 20 (JPSS-1)", "WEATHER"),
    ("GOES 16", "WEATHER"),
    ("EWS-G2 (GOES 15)", "WEATHER"),
    ("FENGYUN 3D", "WEATHER"),
    ("METOP-B", "WEATHER"),
    ("METEOSAT-11 (MSG-4)", "WEATHER"),
    ("SUOMI NPP", "WEATHER"),
    ("COSMOS 2251 DEB", "DEBRIS"),
    ("FENGYUN 1C DEB", "DEBRIS"),
    ("IRIDIUM 33 DEB", "DEBRIS"),
    ("CZ-4B R/B", "DEBRIS"),
    ("SL-16 R/B", "DEBRIS"),
    ("SL-16 R/B(2)", "DEBRIS"),
    ("IDEFIX & ARIANE 42P R/B", "DEBRIS"),
    ("FREGAT DEB", "DEBRIS"),
    ("ISS OBJECT YM", "DEBRIS"),
    ("YAOGAN-30 A", "OTHER"),
    ("FLOCK 4P 1", "OTHER"),
    ("GSAT-15", "OTHER"),
    ("SUOMI-100", "OTHER"),
    ("MIMIR 1", "OTHER"),
    ("DEBUT 1", "OTHER"),
    ("SPACE DEBUTANT", "OTHER"),
    ("", "OTHER"),
]

# Rows of the mixed catalog used by most panel scenarios. Counts per category, in
# KEYS order, are MIXED_COUNTS; two categories (KUIPER, CHINA_MEGA) are empty on purpose.
MIXED = [
    (1, "STARLINK-1001", "LEO"),
    (2, "STARLINK-1002", "LEO"),
    (3, "STARLINK-1003", "LEO"),
    (4, "ONEWEB-0012", "LEO"),
    (5, "ONEWEB-0013", "LEO"),
    (6, "GPS BIIR-2  (PRN 13)", "MEO"),
    (7, "NAVSTAR 43 (USA 132)", "MEO"),
    (8, "COSMOS 2545 (GLONASS-M)", "MEO"),
    (9, "GSAT0101 (GALILEO-PFM)", "MEO"),
    (10, "BEIDOU-3 M1", "MEO"),
    (11, "IRIDIUM 106", "LEO"),
    (12, "ISS (ZARYA)", "LEO"),
    (13, "CSS (TIANHE)", "LEO"),
    (14, "NOAA 19", "LEO"),
    (15, "COSMOS 2251 DEB", "LEO"),
    (16, "FENGYUN 1C DEB", "LEO"),
    (17, "CZ-4B R/B", "LEO"),
    (18, "SL-16 R/B", "LEO"),
    (19, "INTELSAT 39", "GEO"),
    (20, "YAOGAN-30 A", "LEO"),
    (21, "LEMUR-2-FOO", "LEO"),
]
MIXED_COUNTS = dict(
    zip(KEYS, [3, 2, 0, 0, 2, 1, 1, 1, 1, 2, 1, 1, 4, 2], strict=True)
)

HARNESS = r"""
var vm = require('vm');
var fs = require('fs');
var SRC = fs.readFileSync(CFG.module, 'utf8');

function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = [];
  this.attrs = {};
  this.listeners = {};
  this.style = {};
  this.className = '';
  this.parent = null;
  this._text = '';
  this.disabled = false;
}
El.prototype.appendChild = function (c) { c.parent = this; this.children.push(c); return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
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
      toggle: function (c, force) {
        var on = force === undefined ? !this.contains(c) : !!force;
        if (on) this.add(c); else this.remove(c);
        return on;
      }
    };
  }
});
El.prototype.has = function (cls) { return this.classList.contains(cls); };
El.prototype.fire = function (type, ev) {
  ev = ev || {};
  ev.type = type;
  ev.stopPropagation = function () {};
  ev.stopImmediatePropagation = function () {};
  ev.preventDefault = function () {};
  var fns = this.listeners[type] || [];
  for (var i = 0; i < fns.length; i++) fns[i].call(this, ev);
  return ev;
};
function all(el, acc) {
  acc = acc || [];
  acc.push(el);
  el.children.forEach(function (c) { all(c, acc); });
  return acc;
}
function byClass(el, cls) { return all(el).filter(function (x) { return x.has(cls); }); }

function makeDocument() {
  var doc = {
    head: new El('head'),
    handlers: {},
    createElement: function (tag) { return new El(tag); },
    getElementById: function (id) {
      return doc.head.children.filter(function (c) { return c.id === id; })[0] || null;
    },
    addEventListener: function (type, fn) {
      (doc.handlers[type] = doc.handlers[type] || []).push(fn);
    },
    removeEventListener: function (type, fn) {
      doc.handlers[type] = (doc.handlers[type] || []).filter(function (f) { return f !== fn; });
    },
    emit: function (type, detail) {
      (doc.handlers[type] || []).slice().forEach(function (fn) {
        fn({ type: type, detail: detail });
      });
    }
  };
  return doc;
}

function loadModule() {
  var doc = makeDocument();
  var sandbox = { document: doc };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: 'categories.js' });
  return { C: sandbox.window.SDACategories, doc: doc, win: sandbox.window };
}

function mkSat(row) {
  return { noradId: row[0], name: row[1], regime: row[2], color: null, point: { show: true },
           label: null, lastLat: 0, lastLon: 0, lastAlt: 0, lastVel: 0, hidden: false };
}
function mkCtx(sats) {
  var calls = { refresh: 0, feed: [], hiddenAtRefresh: [] };
  var engine = { sats: sats, byNoradId: {}, selectedId: null };
  sats.forEach(function (s) { if (s && s.noradId != null) engine.byNoradId[s.noradId] = s; });
  var ctx = {
    satEngine: engine,
    refreshSatVisibility: function () {
      calls.refresh++;
      calls.hiddenAtRefresh.push(
        engine.sats.filter(function (s) { return s && s.hidden; }).length);
    },
    addIntelItem: function (t) { calls.feed.push(t); },
    mount: new El('div')
  };
  return { ctx: ctx, calls: calls };
}
function start(rows) {
  var m = loadModule();
  var sats = rows.map(mkSat);
  var k = mkCtx(sats);
  m.ok = m.C.init(k.ctx);
  m.k = k;
  m.sats = sats;
  return m;
}
function pills(m) { return byClass(m.k.ctx.mount, 'catf-pill'); }
function pill(m, key) {
  return pills(m).filter(function (p) { return p.attrs['data-cat'] === key; })[0];
}
function pillInfo(p) {
  return {
    key: p.attrs['data-cat'],
    label: byClass(p, 'catf-name')[0].textContent,
    count: Number(byClass(p, 'catf-count')[0].textContent.replace(/,/g, '')),
    active: p.has('active'),
    disabled: p.disabled === true,
    pressed: p.attrs['aria-pressed']
  };
}
function allInfo(m) { return pills(m).map(pillInfo); }
function activeKeys(m) {
  return allInfo(m).filter(function (p) { return p.active; }).map(function (p) { return p.key; });
}
function hiddenIds(sats) { return ids(sats, function (s) { return s.hidden; }); }
function countOf(btn) {
  return Number(byClass(btn, 'catf-count')[0].textContent.replace(/,/g, ''));
}
function clickKey(pl, key) {
  pl.filter(function (x) { return x.attrs['data-cat'] === key; })[0].fire('click', {});
}
function flags(sats) { return sats.map(function (s) { return s && s.hidden === true ? 1 : 0; }); }
function click(m, key) { pill(m, key).fire('click', {}); }
function note(m) { return byClass(m.k.ctx.mount, 'catf-note')[0].textContent; }
function ids(sats, pred) {
  return sats.filter(function (s) { return s && pred(s); })
    .map(function (s) { return s.noradId; });
}

var R = {};

// -- exports, names, regime --
(function () {
  var m = loadModule();
  R.exports = Object.keys(m.C).sort();
  R.categories = m.C.categories.map(function (c) { return [c.key, c.label]; });
  R.classes = {};
  CFG.names.forEach(function (n) { R.classes[n] = m.C.classify(n); });
  R.regime = {
    geoOther: m.C.classify('INTELSAT 39', 'GEO'),
    leoOther: m.C.classify('INTELSAT 39', 'LEO'),
    geoWeather: m.C.classify('GOES 16', 'GEO'),
    geoStarlink: m.C.classify('STARLINK-1', 'GEO'),
    geoDebris: m.C.classify('SL-8 R/B', 'GEO'),
    noRegime: m.C.classify('INTELSAT 39')
  };
  R.badNames = [null, undefined, 42, {}, [], true].map(function (v) { return m.C.classify(v); });
})();

// -- the panel as built --
(function () {
  var a = start(CFG.mixed);
  var b = start(CFG.mixed);
  var panel = a.k.ctx.mount.children[0];
  R.panel = {
    ok: a.ok,
    panels: a.k.ctx.mount.children.length,
    cls: panel.className,
    h3: panel.children[0].tagName + ':' + panel.children[0].textContent,
    body: panel.children[1].className,
    pills: allInfo(a),
    tags: pills(a).map(function (p) { return p.tagName; }),
    types: pills(a).map(function (p) { return p.attrs.type; }),
    styles: a.doc.head.children.length,
    stylesB: b.doc.head.children.length,
    note: note(a),
    refreshAtInit: a.k.calls.refresh,
    flagsAtInit: flags(a.sats).reduce(function (x, y) { return x + y; }, 0)
  };
})();

// -- one pill, then All --
(function () {
  var m = start(CFG.mixed);
  var k = m.k;
  click(m, 'STARLINK');
  var one = {
    hidden: ids(m.sats, function (s) { return s.hidden; }),
    refresh: k.calls.refresh,
    hiddenAtRefresh: k.calls.hiddenAtRefresh.slice(),
    active: activeKeys(m),
    pressed: allInfo(m).filter(function (p) { return p.pressed === 'true'; })
      .map(function (p) { return p.key; }),
    note: note(m),
    feed: k.calls.feed.slice()
  };
  click(m, 'ALL');
  R.one = one;
  R.all = {
    hidden: ids(m.sats, function (s) { return s.hidden; }),
    refresh: k.calls.refresh,
    active: activeKeys(m),
    feed: k.calls.feed.length
  };
  // "All" while already on All still clears flags that something else set.
  m.sats[3].hidden = true;
  click(m, 'ALL');
  R.allClearsStray = { hidden: hiddenIds(m.sats), refresh: k.calls.refresh };
})();

// -- several pills at once --
(function () {
  var m = start(CFG.mixed);
  var seq = {};
  function visibleIds() { return ids(m.sats, function (s) { return !s.hidden; }); }
  click(m, 'STARLINK');  click(m, 'ONEWEB');
  seq.two = visibleIds();
  click(m, 'STARLINK');
  seq.afterOff = visibleIds();
  click(m, 'ONEWEB');
  seq.afterEmpty = { hidden: hiddenIds(m.sats), active: activeKeys(m) };
  seq.refresh = m.k.calls.refresh;
  R.multi = seq;
})();

// -- catalog reload --
(function () {
  var m = start(CFG.mixed);
  var oldSats = m.sats;
  click(m, 'STARLINK');
  var before = m.k.calls.refresh;
  var fresh = CFG.reload.map(mkSat);
  m.k.ctx.satEngine.sats = fresh;
  m.k.ctx.satEngine.byNoradId = {};
  fresh.forEach(function (s) { m.k.ctx.satEngine.byNoradId[s.noradId] = s; });
  m.doc.emit('sda:catalog-loaded', { count: fresh.length });
  R.reload = {
    pills: allInfo(m),
    visible: ids(fresh, function (s) { return !s.hidden; }),
    hidden: ids(fresh, function (s) { return s.hidden; }),
    oldTouched: ids(oldSats, function (s) { return s.hidden; }).length,
    refreshDelta: m.k.calls.refresh - before,
    feedAfter: m.k.calls.feed.length
  };
  // The active category has no members in the next catalog: fall back to All
  // instead of hiding every dot.
  var m2 = start(CFG.mixed);
  click(m2, 'DEBRIS');
  var fresh2 = CFG.noDebris.map(mkSat);
  m2.k.ctx.satEngine.sats = fresh2;
  m2.doc.emit('sda:catalog-loaded', { count: fresh2.length });
  R.reloadEmptyActive = {
    hidden: ids(fresh2, function (s) { return s.hidden; }),
    active: activeKeys(m2),
    debris: pillInfo(pill(m2, 'DEBRIS'))
  };
  // Two catalog-loaded events in a row do not stack anything.
  m2.doc.emit('sda:catalog-loaded', { count: fresh2.length });
  R.reloadTwice = allInfo(m2).map(function (p) { return p.count; });
})();

// -- the selected satellite --
(function () {
  var m = start(CFG.mixed);
  var s = m.sats;
  var byId = m.k.ctx.satEngine.byNoradId;
  click(m, 'STARLINK');
  var base = m.k.calls.refresh;
  // select a GPS record (hidden by the filter)
  m.k.ctx.satEngine.selectedId = 6;
  m.doc.emit('sda:satellite-selected', { noradId: 6, name: byId[6].name });
  var first = {
    hidden6: byId[6].hidden,
    hiddenOthers: ids(s, function (x) { return x.hidden; }),
    refreshDelta: m.k.calls.refresh - base,
    note: note(m)
  };
  // select another hidden record: the first one is hidden again
  m.k.ctx.satEngine.selectedId = 14;
  m.doc.emit('sda:satellite-selected', { noradId: 14, name: byId[14].name });
  var second = { hidden6: byId[6].hidden, hidden14: byId[14].hidden };
  // select a record the filter already shows: nothing extra changes
  var hiddenBefore = ids(s, function (x) { return x.hidden; });
  m.k.ctx.satEngine.selectedId = 1;
  m.doc.emit('sda:satellite-selected', { noradId: 1, name: byId[1].name });
  var third = { hidden14: byId[14].hidden, hiddenBefore: hiddenBefore,
                hiddenAfter: ids(s, function (x) { return x.hidden; }), note: note(m) };
  // a pill click while a hidden satellite is selected keeps that one visible
  m.k.ctx.satEngine.selectedId = 11;
  click(m, 'ONEWEB');
  var fourth = { hidden11: byId[11].hidden,
                 visible: ids(s, function (x) { return !x.hidden; }) };
  // All, then a filter again with nothing selected: no exception
  m.k.ctx.satEngine.selectedId = null;
  click(m, 'ALL');
  click(m, 'GPS');
  var fifth = { hidden11: byId[11].hidden };
  // an event naming an id that is not in the catalog is ignored
  var refreshBefore = m.k.calls.refresh;
  m.doc.emit('sda:satellite-selected', { noradId: 999999, name: 'GHOST' });
  m.doc.emit('sda:satellite-selected', null);
  m.doc.emit('sda:satellite-selected', {});
  var sixth = { refreshDelta: m.k.calls.refresh - refreshBefore };
  R.selected = { first: first, second: second, third: third, fourth: fourth, fifth: fifth,
                 sixth: sixth };
})();

// -- the selected satellite, string id and event before the engine updates selectedId --
(function () {
  var m = start(CFG.mixed);
  var byId = m.k.ctx.satEngine.byNoradId;
  click(m, 'STARLINK');
  // selectedId still null when the event lands: the event is the authority
  m.doc.emit('sda:satellite-selected', { noradId: '12', name: 'ISS (ZARYA)' });
  R.selectedString = { hidden12: byId[12].hidden };
})();

// -- an empty category is disabled, not removed --
(function () {
  var m = start(CFG.mixed);
  var nodes = pills(m);
  var kuiperBefore = pillInfo(pill(m, 'KUIPER'));
  var refresh0 = m.k.calls.refresh;
  click(m, 'KUIPER');
  click(m, 'CHINA_MEGA');
  var afterClick = {
    refreshDelta: m.k.calls.refresh - refresh0,
    hidden: ids(m.sats, function (s) { return s.hidden; }),
    active: activeKeys(m)
  };
  var fresh = CFG.noDebris.map(mkSat);
  m.k.ctx.satEngine.sats = fresh;
  m.doc.emit('sda:catalog-loaded', { count: fresh.length });
  var nodesAfter = pills(m);
  R.disabled = {
    kuiperBefore: kuiperBefore,
    kuiperCls: pill(m, 'KUIPER').className,
    ariaDisabled: pill(m, 'KUIPER').attrs['aria-disabled'],
    afterClick: afterClick,
    sameCount: nodes.length === nodesAfter.length,
    sameNodes: nodes.every(function (n, i) { return n === nodesAfter[i]; }),
    keysAfter: allInfo(m).map(function (p) { return p.key; }),
    debrisAfter: pillInfo(pill(m, 'DEBRIS'))
  };
})();

// -- hostile text --
(function () {
  var hostile = '<img src=x onerror=alert(1)>';
  var m = start(CFG.mixed.concat([[22, hostile, 'LEO']]));
  click(m, 'STARLINK');
  m.k.ctx.satEngine.selectedId = 22;
  m.doc.emit('sda:satellite-selected', { noradId: 22, name: hostile });
  var n = byClass(m.k.ctx.mount, 'catf-note')[0];
  R.hostile = {
    note: n.textContent,
    kids: n.children.length,
    elements: all(m.k.ctx.mount).filter(function (x) { return x.tagName === 'IMG'; }).length,
    hidden22: m.sats[21].hidden
  };
  // a hostile name in the event detail alone (record unknown) is never rendered as markup
  m.doc.emit('sda:satellite-selected', { noradId: 1, name: hostile });
  R.hostileEventName = {
    imgs: all(m.k.ctx.mount).filter(function (x) { return x.tagName === 'IMG'; }).length
  };
})();

// -- reads: classify once per catalog load, never on a click --
(function () {
  function counted(n) {
    var reads = { n: 0 };
    var sats = [];
    for (var i = 0; i < n; i++) {
      var s = { noradId: i + 1, regime: 'LEO', hidden: false, point: { show: true } };
      (function (name) {
        Object.defineProperty(s, 'name', {
          get: function () { reads.n++; return name; }, enumerable: true
        });
      })(i % 3 === 0 ? 'STARLINK-' + i : 'NOAA ' + i);
      sats.push(s);
    }
    return { sats: sats, reads: reads };
  }
  var m = loadModule();
  var c = counted(3000);
  var k = mkCtx(c.sats);
  m.C.init(k.ctx);
  var atInit = c.reads.n;
  c.reads.n = 0;
  var pl = byClass(k.ctx.mount, 'catf-pill');
  var p = pl.filter(function (x) { return x.attrs['data-cat'] === 'STARLINK'; })[0];
  p.fire('click', {}); p.fire('click', {}); p.fire('click', {});
  var afterClicks = c.reads.n;
  var c2 = counted(3000);
  k.ctx.satEngine.sats = c2.sats;
  m.doc.emit('sda:catalog-loaded', { count: 3000 });
  var atReload = c2.reads.n;
  c2.reads.n = 0;
  p.fire('click', {});
  R.reads = { atInit: atInit, afterClicks: afterClicks, atReload: atReload,
              afterReloadClick: c2.reads.n };
})();

// -- 33,000 records --
(function () {
  var N = 33000;
  var patterns = [
    function (i) { return 'STARLINK-' + i; },
    function (i) { return 'ONEWEB-' + i; },
    function (i) { return 'COSMOS ' + i + ' DEB'; },
    function (i) { return 'NOAA ' + i; },
    function (i) { return 'SOMETHING ' + i; },
    function (i) { return 'GPS BIIR-' + i; },
    function (i) { return 'SL-16 R/B(' + i + ')'; }
  ];
  var expect = { STARLINK: 0, ONEWEB: 0, DEBRIS: 0, WEATHER: 0, OTHER: 0, GPS: 0, GEO: 0 };
  var keyOf = ['STARLINK', 'ONEWEB', 'DEBRIS', 'WEATHER', 'OTHER', 'GPS', 'DEBRIS'];
  var rows = [];
  for (var i = 0; i < N; i++) {
    var p = i % patterns.length;
    rows.push([i + 1, patterns[p](i), i % 11 === 0 ? 'GEO' : 'LEO']);
    // an unclaimed name in GEO falls to the regime rule
    expect[(p === 4 && i % 11 === 0) ? 'GEO' : keyOf[p]]++;
  }
  var sats = rows.map(mkSat);
  var t0 = process.hrtime.bigint();
  var m = loadModule();
  var k = mkCtx(sats);
  m.C.init(k.ctx);
  var t1 = process.hrtime.bigint();
  var panel = k.ctx.mount;
  var pl = byClass(panel, 'catf-pill');
  function pk(key) { return pl.filter(function (x) { return x.attrs['data-cat'] === key; })[0]; }
  pk('STARLINK').fire('click', {});
  var t2 = process.hrtime.bigint();
  pk('STARLINK').fire('click', {});
  pk('DEBRIS').fire('click', {});
  pk('ALL').fire('click', {});
  var t3 = process.hrtime.bigint();
  var hiddenAfterAll = sats.filter(function (s) { return s.hidden; }).length;
  pk('STARLINK').fire('click', {});
  var visible = sats.filter(function (s) { return !s.hidden; }).length;
  var counts = {};
  pl.forEach(function (x) {
    counts[x.attrs['data-cat']] = countOf(x);
  });
  R.big = {
    n: N,
    initMs: Number(t1 - t0) / 1e6,
    clickMs: Number(t2 - t1) / 1e6,
    threeClicksMs: Number(t3 - t2) / 1e6,
    counts: counts,
    expect: expect,
    hiddenAfterAll: hiddenAfterAll,
    visibleStarlink: visible
  };
})();

// -- bad data and bad pages --
(function () {
  var out = {};
  var m = loadModule();
  out.nullInit = m.C.init(null);
  out.emptyInit = m.C.init({});
  out.noMount = m.C.init({ satEngine: { sats: [] }, mount: null });

  // junk in the record array
  var junk = [null, undefined, 7, 'text', { noradId: 1 }, { noradId: 2, name: null },
              { noradId: 3, name: 42 }, { noradId: 4, name: 'STARLINK-1' }];
  var k = mkCtx(junk.slice());
  k.ctx.satEngine.sats = junk;
  var ok = m.C.init(k.ctx);
  var info = pills({ k: k }).map(pillInfo);
  var threw = false;
  try { clickKey(pills({ k: k }), 'STARLINK'); } catch (e) { threw = true; }
  out.junk = { ok: ok, threw: threw, all: info[0].count,
               starlink: info.filter(function (p) { return p.key === 'STARLINK'; })[0].count,
               other: info.filter(function (p) { return p.key === 'OTHER'; })[0].count };

  // refreshSatVisibility throws, or is missing: flags are still written, nothing throws
  var m2 = start(CFG.mixed);
  m2.k.ctx.refreshSatVisibility = function () { throw new Error('refresh failed'); };
  var t2 = false;
  try { click(m2, 'STARLINK'); } catch (e) { t2 = true; }
  out.refreshThrows = { threw: t2, hidden: hiddenIds(m2.sats).length };
  var m3 = start(CFG.mixed);
  delete m3.k.ctx.refreshSatVisibility;
  var t3 = false;
  try { click(m3, 'STARLINK'); } catch (e) { t3 = true; }
  out.noRefresh = { threw: t3, hidden: hiddenIds(m3.sats).length };

  // addIntelItem throws
  var m4 = start(CFG.mixed);
  m4.k.ctx.addIntelItem = function () { throw new Error('feed failed'); };
  var t4 = false;
  try { click(m4, 'STARLINK'); } catch (e) { t4 = true; }
  out.feedThrows = { threw: t4, hidden: hiddenIds(m4.sats).length };

  // satEngine getter throws at event time
  var m5 = start(CFG.mixed);
  Object.defineProperty(m5.k.ctx, 'satEngine', { get: function () { throw new Error('gone'); } });
  var t5 = false;
  try {
    m5.doc.emit('sda:catalog-loaded', { count: 1 });
    m5.doc.emit('sda:satellite-selected', { noradId: 1, name: 'X' });
  } catch (e) { t5 = true; }
  out.engineGone = { threw: t5 };

  // a second init on a new mount takes over; the old one stops listening
  var a = start(CFG.mixed);
  click(a, 'STARLINK');
  a.doc.emit('sda:satellite-selected', { noradId: 6, name: 'GPS BIIR-2  (PRN 13)' });
  var aRefresh = a.k.calls.refresh;
  var b = mkCtx(CFG.reload.map(mkSat));
  a.C.init(b.ctx);
  a.doc.emit('sda:satellite-deselected', { noradId: 6 });
  out.reinit = {
    oldRefreshDelta: a.k.calls.refresh - aRefresh,
    oldHidden6: a.sats[5].hidden,
    firstPanels: a.k.ctx.mount.children.length,
    secondPanels: b.ctx.mount.children.length,
    handlers: Object.keys(a.doc.handlers).map(function (t) {
      return [t, a.doc.handlers[t].length];
    })
  };
  R.robust = out;
})();

// -- deselecting the satellite that was shown only because it was selected --
(function () {
  var out = {};
  var m = start(CFG.mixed);
  var byId = m.k.ctx.satEngine.byNoradId;
  var engine = m.k.ctx.satEngine;
  click(m, 'STARLINK');
  engine.selectedId = 6;
  m.doc.emit('sda:satellite-selected', { noradId: 6, name: byId[6].name });
  var noteBefore = note(m);
  var r0 = m.k.calls.refresh;
  var feed0 = m.k.calls.feed.length;
  engine.selectedId = null;
  m.doc.emit('sda:satellite-deselected', { noradId: 6 });
  out.basic = {
    hidden6: byId[6].hidden,
    hidden: hiddenIds(m.sats),
    noteBefore: noteBefore,
    noteAfter: note(m),
    refreshDelta: m.k.calls.refresh - r0,
    hiddenAtRefresh: m.k.calls.hiddenAtRefresh[m.k.calls.hiddenAtRefresh.length - 1],
    feedDelta: m.k.calls.feed.length - feed0
  };
  // a second deselect straight after: nothing is left to hide
  var r1 = m.k.calls.refresh;
  m.doc.emit('sda:satellite-deselected', { noradId: 6 });
  out.again = { refreshDelta: m.k.calls.refresh - r1, hidden6: byId[6].hidden };
  // selecting the same satellite again shows it again
  engine.selectedId = 6;
  m.doc.emit('sda:satellite-selected', { noradId: 6, name: byId[6].name });
  out.reselect = {
    hidden6: byId[6].hidden,
    refreshDelta: m.k.calls.refresh - r1,
    noteNamesIt: note(m).indexOf(byId[6].name) >= 0
  };

  // no filter at all
  var n = start(CFG.mixed);
  n.doc.emit('sda:satellite-deselected', { noradId: 6 });
  out.noFilter = { refresh: n.k.calls.refresh, hidden: hiddenIds(n.sats) };

  // filter on, but the selected satellite was already visible: nothing forced
  var v = start(CFG.mixed);
  click(v, 'STARLINK');
  v.k.ctx.satEngine.selectedId = 1;
  v.doc.emit('sda:satellite-selected', { noradId: 1, name: 'STARLINK-1001' });
  var vRefresh = v.k.calls.refresh;
  var vFlags = flags(v.sats);
  v.k.ctx.satEngine.selectedId = null;
  v.doc.emit('sda:satellite-deselected', { noradId: 1 });
  out.nothingForced = {
    refreshDelta: v.k.calls.refresh - vRefresh,
    flagsSame: JSON.stringify(vFlags) === JSON.stringify(flags(v.sats))
  };

  // the page's order when another satellite is picked: deselected(old), selected(new)
  var p = start(CFG.mixed);
  var pById = p.k.ctx.satEngine.byNoradId;
  click(p, 'STARLINK');
  p.k.ctx.satEngine.selectedId = 6;
  p.doc.emit('sda:satellite-selected', { noradId: 6, name: pById[6].name });
  var pBase = p.k.calls.refresh;
  p.k.ctx.satEngine.selectedId = null;
  p.doc.emit('sda:satellite-deselected', { noradId: 6 });
  var mid = { hidden: hiddenIds(p.sats), refreshDelta: p.k.calls.refresh - pBase };
  p.k.ctx.satEngine.selectedId = 14;
  p.doc.emit('sda:satellite-selected', { noradId: 14, name: pById[14].name });
  out.swap = {
    mid: mid,
    hidden: hiddenIds(p.sats),
    refreshDelta: p.k.calls.refresh - pBase,
    note: note(p)
  };

  // the other order (selected(new) before deselected(old)) must not hide the new one
  var q = start(CFG.mixed);
  var qById = q.k.ctx.satEngine.byNoradId;
  click(q, 'STARLINK');
  q.doc.emit('sda:satellite-selected', { noradId: 6, name: qById[6].name });
  q.doc.emit('sda:satellite-selected', { noradId: 14, name: qById[14].name });
  var qBase = q.k.calls.refresh;
  q.doc.emit('sda:satellite-deselected', { noradId: 6 });
  out.lateOld = { hidden14: qById[14].hidden, refreshDelta: q.k.calls.refresh - qBase };
  q.doc.emit('sda:satellite-deselected', { noradId: 14 });
  out.lateNew = { hidden14: qById[14].hidden, refreshDelta: q.k.calls.refresh - qBase };

  // malformed events never throw; with nothing forced they do nothing at all
  var bad = start(CFG.mixed);
  click(bad, 'STARLINK');
  var badBase = bad.k.calls.refresh;
  var hostile = { toString: function () { throw new Error('boom'); } };
  var threw = false;
  [null, undefined, {}, { noradId: null }, { noradId: 'x' }, { noradId: 999999 },
   { noradId: hostile }].forEach(function (detail) {
    try { bad.doc.emit('sda:satellite-deselected', detail); } catch (e) { threw = true; }
  });
  out.malformedIdle = { threw: threw, refreshDelta: bad.k.calls.refresh - badBase };
  // and with a record forced: still no throw, and an event with no id releases it
  bad.doc.emit('sda:satellite-selected', { noradId: 6, name: 'x' });
  try { bad.doc.emit('sda:satellite-deselected', null); } catch (e) { threw = true; }
  try {
    bad.doc.emit('sda:satellite-deselected', { noradId: hostile });
  } catch (e) { threw = true; }
  out.malformedForced = { threw: threw, hidden6: bad.sats[5].hidden };
  R.deselect = out;
})();

process.stdout.write(JSON.stringify(R));
"""


def _config() -> dict:
    reload_rows = [
        (101, "STARLINK-2001", "LEO"),
        (102, "STARLINK-2002", "LEO"),
        (103, "GPS BIIF-5", "MEO"),
        (104, "SL-8 R/B", "LEO"),
        (105, "QIANFAN-7", "LEO"),
        (106, "SOME CUBESAT", "LEO"),
    ]
    no_debris = [r for r in MIXED if "DEB" not in r[1] and "R/B" not in r[1]]
    return {
        "module": MODULE.as_posix(),
        "names": [n for n, _ in NAME_TABLE],
        "mixed": [list(r) for r in MIXED],
        "reload": [list(r) for r in reload_rows],
        "noDebris": [list(r) for r in no_debris],
    }


@pytest.fixture(scope="module")
def R(tmp_path_factory):
    """One Node process runs every scenario; tests read slices of the result."""
    script = "var CFG = " + json.dumps(_config()) + ";\n" + HARNESS
    path = tmp_path_factory.mktemp("categories") / "harness.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(path)], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def pill_by_key(pills, key):
    return next(p for p in pills if p["key"] == key)


# --- syntax and surface ---


def test_node_check_passes():
    proc = subprocess.run(
        [NODE, "--check", str(MODULE)], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_module_source_is_ascii_without_em_dash():
    text = MODULE.read_bytes()
    assert all(b < 128 for b in text), "categories.js must be ASCII only"


def test_surface_and_category_order(R):
    assert R["exports"] == ["categories", "classify", "init", "tally"]
    assert [key for key, _ in R["categories"]] == KEYS
    assert all(label.strip() for _, label in R["categories"])


# --- classification ---


@pytest.mark.parametrize(("name", "expected"), NAME_TABLE)
def test_name_to_category(R, name, expected):
    assert R["classes"][name] == expected


def test_regime_only_decides_what_no_name_rule_claims(R):
    r = R["regime"]
    assert r["geoOther"] == "GEO"
    assert r["leoOther"] == "OTHER"
    assert r["noRegime"] == "OTHER"
    assert r["geoWeather"] == "WEATHER"
    assert r["geoStarlink"] == "STARLINK"
    assert r["geoDebris"] == "DEBRIS"


def test_non_string_names_are_other_and_never_throw(R):
    assert R["badNames"] == ["OTHER"] * 6


# --- the panel as built ---


def test_panel_structure_and_one_style(R):
    p = R["panel"]
    assert p["ok"] is True
    assert p["panels"] == 1
    assert p["cls"].split() == ["panel"]
    assert p["h3"].startswith("H3:") and p["h3"][3:].strip()
    assert p["body"] == "panel-body"
    assert p["styles"] == 1 and p["stylesB"] == 1
    assert set(p["tags"]) == {"BUTTON"}
    assert set(p["types"]) == {"button"}


def test_all_pill_first_then_categories_in_order(R):
    keys = [p["key"] for p in R["panel"]["pills"]]
    assert keys == ["ALL", *KEYS]
    assert R["panel"]["pills"][0]["label"] == "All"


def test_counts_per_pill(R):
    pills = R["panel"]["pills"]
    for key, expected in MIXED_COUNTS.items():
        assert pill_by_key(pills, key)["count"] == expected, key
    assert pill_by_key(pills, "ALL")["count"] == len(MIXED)
    assert sum(MIXED_COUNTS.values()) == len(MIXED)


def test_all_is_active_at_start_and_nothing_is_hidden(R):
    p = R["panel"]
    assert [x["key"] for x in p["pills"] if x["active"]] == ["ALL"]
    assert pill_by_key(p["pills"], "ALL")["pressed"] == "true"
    assert pill_by_key(p["pills"], "STARLINK")["pressed"] == "false"
    # building the panel is not a filter change
    assert p["refreshAtInit"] == 0 and p["flagsAtInit"] == 0
    assert "21" in p["note"]


# --- filtering ---


def test_one_pill_hides_exactly_the_records_outside_it(R):
    one = R["one"]
    assert one["hidden"] == [i for i in range(1, 22) if i not in (1, 2, 3)]
    assert one["active"] == ["STARLINK"]
    assert one["pressed"] == ["STARLINK"]


def test_one_click_calls_refresh_once_after_the_flags_are_written(R):
    one = R["one"]
    assert one["refresh"] == 1
    assert one["hiddenAtRefresh"] == [18]


def test_filter_change_is_logged_and_the_note_says_how_many_are_shown(R):
    one = R["one"]
    assert len(one["feed"]) == 1 and "Starlink" in one["feed"][0]
    assert "3" in one["note"] and "21" in one["note"]


def test_all_clears_every_flag(R):
    assert R["all"]["hidden"] == []
    assert R["all"]["refresh"] == 2
    assert R["all"]["active"] == ["ALL"]
    assert R["all"]["feed"] == 2


def test_all_also_clears_flags_it_did_not_set(R):
    assert R["allClearsStray"] == {"hidden": [], "refresh": 3}


def test_pills_combine_and_unwind_back_to_all(R):
    m = R["multi"]
    assert m["two"] == [1, 2, 3, 4, 5]
    assert m["afterOff"] == [4, 5]
    assert m["afterEmpty"] == {"hidden": [], "active": ["ALL"]}
    assert m["refresh"] == 4


# --- catalog reload ---


def test_catalog_loaded_recounts_and_reapplies_the_filter(R):
    r = R["reload"]
    pills = r["pills"]
    assert pill_by_key(pills, "STARLINK")["count"] == 2
    assert pill_by_key(pills, "GPS")["count"] == 1
    assert pill_by_key(pills, "CHINA_MEGA")["count"] == 1
    assert pill_by_key(pills, "DEBRIS")["count"] == 1
    assert pill_by_key(pills, "ONEWEB")["count"] == 0
    assert pill_by_key(pills, "ALL")["count"] == 6
    assert [p["key"] for p in pills if p["active"]] == ["STARLINK"]
    assert r["visible"] == [101, 102]
    assert r["hidden"] == [103, 104, 105, 106]
    assert r["oldTouched"] == 18  # the replaced array is left as it was
    assert r["refreshDelta"] == 1
    assert r["feedAfter"] == 1  # only the click was logged, not the reload


def test_active_category_that_vanishes_falls_back_to_all(R):
    r = R["reloadEmptyActive"]
    assert r["hidden"] == []
    assert r["active"] == ["ALL"]
    assert r["debris"]["disabled"] is True and r["debris"]["count"] == 0
    assert R["reloadTwice"][0] == len(MIXED) - 4


# --- the selected satellite ---


def test_selecting_a_hidden_satellite_shows_that_one_record(R):
    s = R["selected"]["first"]
    assert s["hidden6"] is False
    assert s["hiddenOthers"] == [i for i in range(4, 22) if i != 6]
    assert s["refreshDelta"] == 1
    assert "GPS" in s["note"] or "NAVSTAR" in s["note"]


def test_the_previous_forced_satellite_is_hidden_again(R):
    s = R["selected"]["second"]
    assert s == {"hidden6": True, "hidden14": False}


def test_selecting_a_visible_satellite_releases_the_previous_override_only(R):
    t = R["selected"]["third"]
    assert t["hidden14"] is True
    assert t["hiddenBefore"] == [i for i in range(4, 22) if i != 14]
    assert t["hiddenAfter"] == list(range(4, 22))


def test_a_filter_click_keeps_the_selected_satellite_visible(R):
    f = R["selected"]["fourth"]
    assert f["hidden11"] is False
    assert 11 in f["visible"] and {1, 2, 3, 4, 5}.issubset(set(f["visible"]))
    assert sorted(f["visible"]) == [1, 2, 3, 4, 5, 11]


def test_filter_after_deselect_hides_the_old_selection_again(R):
    assert R["selected"]["fifth"] == {"hidden11": True}


def test_selection_events_for_unknown_or_malformed_ids_are_ignored(R):
    assert R["selected"]["sixth"] == {"refreshDelta": 0}
    assert R["selectedString"] == {"hidden12": False}


# --- empty categories ---


def test_a_category_with_no_members_is_disabled_not_removed(R):
    d = R["disabled"]
    assert d["kuiperBefore"]["disabled"] is True
    assert d["kuiperBefore"]["count"] == 0
    assert "catf-disabled" in d["kuiperCls"].split()
    assert d["ariaDisabled"] == "true"


def test_clicking_a_disabled_pill_does_nothing(R):
    a = R["disabled"]["afterClick"]
    assert a == {"refreshDelta": 0, "hidden": [], "active": ["ALL"]}


def test_the_row_does_not_jump_when_the_catalog_reloads(R):
    d = R["disabled"]
    assert d["sameCount"] is True and d["sameNodes"] is True
    assert d["keysAfter"] == ["ALL", *KEYS]
    assert d["debrisAfter"]["disabled"] is True and d["debrisAfter"]["count"] == 0


# --- hostile text ---


def test_a_hostile_name_is_rendered_as_text(R):
    h = R["hostile"]
    assert "<img src=x onerror=alert(1)>" in h["note"]
    assert h["kids"] == 0 and h["elements"] == 0
    assert h["hidden22"] is False
    assert R["hostileEventName"] == {"imgs": 0}


# --- cost ---


def test_classification_runs_once_per_catalog_load_and_never_per_click(R):
    r = R["reads"]
    assert r["atInit"] == 3000
    assert r["afterClicks"] == 0
    assert r["atReload"] == 3000
    assert r["afterReloadClick"] == 0


def test_thirty_three_thousand_records(R):
    big = R["big"]
    assert big["n"] == 33000
    for key, expected in big["expect"].items():
        assert big["counts"][key] == expected, key
    assert big["counts"]["ALL"] == 33000
    assert big["hiddenAfterAll"] == 0
    assert big["visibleStarlink"] == big["expect"]["STARLINK"]
    # generous bounds: the point is "milliseconds, not seconds", on a slow CI box too
    assert big["initMs"] < 3000
    assert big["clickMs"] < 1000
    assert big["threeClicksMs"] < 2000


# --- bad data and bad pages ---


def test_init_without_a_usable_mount_returns_false(R):
    r = R["robust"]
    assert r["nullInit"] is False and r["emptyInit"] is False and r["noMount"] is False


def test_junk_records_are_counted_as_other_or_skipped(R):
    j = R["robust"]["junk"]
    assert j["ok"] is True and j["threw"] is False
    assert j["starlink"] == 1
    # null, undefined, 7 and 'text' are skipped; the four objects with a bad or
    # missing name land in OTHER except the one real Starlink
    assert j["other"] == 3
    assert j["all"] == 4


def test_page_callbacks_that_throw_do_not_stop_the_filter(R):
    r = R["robust"]
    assert r["refreshThrows"] == {"threw": False, "hidden": 18}
    assert r["noRefresh"] == {"threw": False, "hidden": 18}
    assert r["feedThrows"] == {"threw": False, "hidden": 18}
    assert r["engineGone"] == {"threw": False}


def test_a_second_init_replaces_the_first_instead_of_stacking_listeners(R):
    r = R["robust"]["reinit"]
    assert r["firstPanels"] == 1 and r["secondPanels"] == 1
    assert dict(r["handlers"]) == {
        "sda:catalog-loaded": 1,
        "sda:satellite-selected": 1,
        "sda:satellite-deselected": 1,
    }
    # the old panel no longer hears a deselect: its override stays and it asks for no refresh
    assert r["oldRefreshDelta"] == 0
    assert r["oldHidden6"] is False


# --- deselecting the satellite that was shown only because it was selected ---


def test_deselect_hides_the_forced_satellite_again_with_one_refresh(R):
    b = R["deselect"]["basic"]
    assert b["hidden6"] is True
    assert b["hidden"] == list(range(4, 22))
    assert b["refreshDelta"] == 1
    # the flag is written before the page is asked to apply it
    assert b["hiddenAtRefresh"] == 18
    assert b["feedDelta"] == 0


def test_deselect_removes_the_selected_stays_visible_note(R):
    b = R["deselect"]["basic"]
    assert "GPS BIIR-2" in b["noteBefore"]
    assert "GPS BIIR-2" not in b["noteAfter"]
    assert "Selected stays visible" not in b["noteAfter"]
    assert "3" in b["noteAfter"] and "21" in b["noteAfter"]


def test_a_second_deselect_finds_nothing_left_to_hide(R):
    assert R["deselect"]["again"] == {"refreshDelta": 0, "hidden6": True}


def test_selecting_the_same_satellite_after_a_deselect_shows_it_again(R):
    r = R["deselect"]["reselect"]
    assert r["hidden6"] is False
    assert r["refreshDelta"] == 1
    assert r["noteNamesIt"] is True


def test_deselect_with_no_filter_does_nothing(R):
    assert R["deselect"]["noFilter"] == {"refresh": 0, "hidden": []}


def test_deselect_when_nothing_was_forced_changes_no_flag_and_asks_for_no_refresh(R):
    assert R["deselect"]["nothingForced"] == {"refreshDelta": 0, "flagsSame": True}


def test_picking_another_satellite_hides_the_old_one_and_shows_only_the_new_one(R):
    s = R["deselect"]["swap"]
    # after the deselect alone nothing outside the filter is visible
    assert s["mid"] == {"hidden": list(range(4, 22)), "refreshDelta": 1}
    # then the select shows exactly one record
    assert s["hidden"] == [i for i in range(4, 22) if i != 14]
    assert s["refreshDelta"] == 2
    assert "NOAA 19" in s["note"] and "GPS BIIR-2" not in s["note"]


def test_a_late_deselect_for_the_old_satellite_does_not_hide_the_new_one(R):
    d = R["deselect"]
    assert d["lateOld"] == {"hidden14": False, "refreshDelta": 0}
    assert d["lateNew"] == {"hidden14": True, "refreshDelta": 1}


def test_malformed_deselect_events_never_throw_and_idle_ones_do_nothing(R):
    d = R["deselect"]
    assert d["malformedIdle"] == {"threw": False, "refreshDelta": 0}
    assert d["malformedForced"] == {"threw": False, "hidden6": True}

