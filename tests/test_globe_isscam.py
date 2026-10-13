"""Tests for the globe ISS live-video module (static/js/isscam.js).

The module is plain browser JavaScript with no bundler, so these tests run it under
Node's `vm` against a hand-written DOM stub (see FAKE_DOM), because jsdom is not a
dependency of this repo. They are offline and deterministic, and skip cleanly when
Node is not installed. Nothing here touches the network: the iframe is only a stub
element, and the module makes no requests of its own.

All scenarios run in one node process (one fresh `vm` context each, so no state leaks
between them); the module-scoped `ran` fixture holds their results and each test reads
the ones it needs. A scenario that throws fails only the tests that read it.
"""

import json
import re
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
    / "isscam.js"
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The fixed page chrome the floating frame has to stay clear of, in CSS pixels. These
# are copied from worldview.html (#controls, #header, the time bar, #info-panel and
# #intel-feed), so a change to the page layout has to be made here on purpose.
LEFT_COLUMN_RIGHT_EDGE = 10 + 240
HEADER_HEIGHT = 52
TIME_BAR_HEIGHT = 64
RIGHT_COLUMN_WIDTH = 300 + 10

PANEL_TITLE = "Live from the ISS"


# Just enough DOM for the panel and the frame: elements with children, attributes, class
# names and listeners, plus a document with a head, a body and document-level events.
# Events are delivered to the target only (no bubbling). Removing a node that is not a
# child throws, as the real DOM does, and assigning innerHTML throws, which turns
# "never innerHTML" into a test. URL comes from the outer Node, not from the browser.
FAKE_DOM = r"""
function El(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = [];
  this.attrs = {};
  this.listeners = {};
  this.style = {};
  this.className = '';
  this.id = '';
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
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.focus = function () {};
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
function byTag(el, tag) { return all(el).filter(function (x) { return x.tagName === tag; }); }

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
  dispatch: function (type, detail) {
    var ev = { type: type, detail: detail };
    (this.listeners[type] || []).forEach(function (fn) { fn(ev); });
    return ev;
  }
};

var ISS = { noradId: 25544, name: 'ISS (ZARYA)', regime: 'LEO', lastLat: 12.5, lastLon: -45.25,
            lastAlt: 420.4 };

function makeCtx(opts) {
  opts = opts || {};
  var calls = { select: [], feed: [] };
  var byId = ('byNoradId' in opts) ? opts.byNoradId : { 25544: ISS };
  var ctx = {
    viewer: {},
    satEngine: { sats: [ISS], byNoradId: byId, selectedId: null },
    selectSatellite: function (id) { calls.select.push(id); },
    addIntelItem: function (t) { calls.feed.push(t); },
    api: '',
    isStatic: false,
    simNow: function () { return new Date(0); },
    mount: new El('div')
  };
  return { ctx: ctx, calls: calls };
}
function tree(ctx) { return all(document.body).concat(all(ctx.mount)); }
function rows(ctx) { return byClass(ctx.mount, 'isscam-row'); }
function openBtns(ctx) { return byClass(ctx.mount, 'isscam-open'); }
function iframes(ctx) { return tree(ctx).filter(function (x) { return x.tagName === 'IFRAME'; }); }
function frames() { return byClass(document.body, 'isscam-frame'); }
function click(el) { return el.fire('click', {}); }
function pressed(ctx) {
  return openBtns(ctx).map(function (b) { return b.getAttribute('aria-pressed'); });
}
function cls(el) { return el.className; }
"""


# One scenario per behaviour group. Each body is the inside of a function that returns a
# JSON-able value; it runs in a fresh vm context where `window.SDAIssCam` is loaded.
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
  var box = { URL: URL };
  box.window = box;
  vm.createContext(box);
  vm.runInContext(prelude, box, { filename: 'fake-dom' });
  vm.runInContext(fs.readFileSync(modulePath, 'utf8'), box, { filename: 'isscam.js' });
  return box;
}

var results = {};
Object.keys(scenarios).forEach(function (name) {
  try {
    var box = load();
    var text = vm.runInContext(
      'JSON.stringify((function () {\n' + scenarios[name] + '\n})())', box, { filename: name });
    results[name] = { ok: true, value: JSON.parse(text) };
  } catch (e) {
    results[name] = { ok: false, error: String((e && e.stack) || e) };
  }
});
process.stdout.write(JSON.stringify(results));
"""


class Ran:
    """Scenario results; reading one that threw fails the reading test with the stack."""

    def __init__(self, raw: dict):
        self._raw = raw

    def __getitem__(self, name: str):
        result = self._raw[name]
        assert result["ok"], result["error"]
        return result["value"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory) -> Ran:
    work = tmp_path_factory.mktemp("isscam")
    driver = work / "driver.js"
    spec = work / "scenarios.json"
    driver.write_text(DRIVER, encoding="utf-8")
    spec.write_text(json.dumps(SCENARIOS), encoding="utf-8")
    dom = work / "fake_dom.js"
    dom.write_text(FAKE_DOM, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), str(dom), MODULE.as_posix(), str(spec)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return Ran(json.loads(proc.stdout))


# --- syntax, exports, source hygiene ---


def test_node_check_passes():
    proc = subprocess.run(
        [NODE, "--check", str(MODULE)], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr


scenario("exports", "return Object.keys(window.SDAIssCam).sort();")


def test_module_exports_init_and_the_pure_helpers(ran):
    assert ran["exports"] == [
        "FEEDS",
        "ISS_NORAD_ID",
        "LAYOUT",
        "embedUrl",
        "init",
        "isValidVideoId",
        "watchUrl",
    ]


def test_source_stays_inside_the_house_rules():
    source = MODULE.read_text(encoding="utf-8")
    # Feed text is data; it must never go through an HTML parser.
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in source, sink
    # The module makes no request of its own: the only network traffic is the iframe the
    # user asks for, so none of the usual request APIs may appear.
    for api in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "new Image", "EventSource"):
        assert api not in source, api
    assert source.isascii()
    assert "celestrak" not in source.lower()
    # Free and keyless: no key path of any kind.
    for word in ("apikey", "api_key", "api-key", "token="):
        assert word not in source.lower(), word


def test_only_the_two_youtube_hosts_appear_in_the_source():
    source = MODULE.read_text(encoding="utf-8")
    hosts = set(re.findall(r"https?://([^/'\"\s?#]+)", source))
    hosts.discard("")
    assert hosts <= {"www.youtube-nocookie.com", "www.youtube.com"}, hosts
    assert "www.youtube-nocookie.com" in hosts


def test_header_credits_the_donor_with_commit_and_license():
    head = "\n".join(MODULE.read_text(encoding="utf-8").splitlines()[:25])
    for part in (
        "simplifaisoul/osiris",
        "src/components/SpaceCam.tsx",
        "8edb376",
        "MIT",
        "Copyright (c) 2026 simplifaisoul",
    ):
        assert part in head, part


# --- feed list and validation ---

scenario(
    "feed_table",
    """
    var S = window.SDAIssCam;
    return S.FEEDS.map(function (f) {
      return { id: f.id, label: f.label, detail: f.detail, source: f.source,
               videoId: f.videoId, valid: S.isValidVideoId(f.videoId) };
    });
    """,
)


def test_bundled_feeds_are_complete_unique_and_valid(ran):
    feeds = ran["feed_table"]
    assert len(feeds) == 3
    assert all(f["valid"] for f in feeds)
    assert len({f["id"] for f in feeds}) == len(feeds)
    assert len({f["videoId"] for f in feeds}) == len(feeds)
    for f in feeds:
        assert f["label"].strip() and f["detail"].strip() and f["source"].strip()
        assert re.fullmatch(r"[A-Za-z0-9_-]{11}", f["videoId"])


scenario(
    "id_table",
    r"""
    var S = window.SDAIssCam;
    var valid = ['fO9e9jnhYK8', 'tj4knR4r1UU', 'OKQEMp2555A', 'a-_B0123456', '-----------'];
    var invalid = [
      '', 'short', 'toolongtoolong', 'fO9e9jnhYK', 'fO9e9jnhYK8x',
      'fO9e9jnhYK8\n', '\nfO9e9jnhYK8', ' fO9e9jnhYK8', 'fO9e9jnhYK8 ',
      'fO9e9jnhYK!', 'fO9e9jnhYK\u00e9', 'abc/../xyz?', '"><script>', 'a.b.c.d.e.f',
      null, undefined, 12345678901, true, {}, ['fO9e9jnhYK8'],
      { toString: function () { return 'fO9e9jnhYK8'; } }
    ];
    function row(id) { return [S.isValidVideoId(id), S.embedUrl(id), S.watchUrl(id)]; }
    return { valid: valid.map(row), invalid: invalid.map(row) };
    """,
)


def test_valid_ids_build_urls_on_the_expected_hosts(ran):
    ids = ["fO9e9jnhYK8", "tj4knR4r1UU", "OKQEMp2555A", "a-_B0123456", "-----------"]
    for vid, (ok, embed, watch) in zip(ids, ran["id_table"]["valid"], strict=True):
        assert ok is True
        assert embed.startswith("https://www.youtube-nocookie.com/embed/" + vid + "?")
        assert watch == "https://www.youtube.com/watch?v=" + vid


def test_invalid_ids_never_reach_a_url(ran):
    table = ran["id_table"]["invalid"]
    assert len(table) == 21
    for ok, embed, watch in table:
        assert ok is False
        assert embed is None
        assert watch is None


scenario(
    "bad_ids_in_the_panel",
    r"""
    var S = window.SDAIssCam;
    var bad = [
      { id: 'bad1', label: 'BAD ONE', detail: 'd', source: 's', videoId: 'x" onload="alert(1)' },
      { id: 'bad2', label: 'BAD TWO', detail: 'd', source: 's', videoId: '../../evil?a=1&b=2' },
      { id: 'bad3', label: 'BAD THREE', detail: 'd', source: 's', videoId: 'short' },
      { id: 'bad4', label: 'BAD FOUR', detail: 'd', source: 's' },
      { id: 'bad5', label: 'BAD FIVE', detail: 'd', source: 's', videoId: ['fO9e9jnhYK8'] }
    ];
    bad.forEach(function (f) { S.FEEDS.push(f); });
    S.FEEDS.push(null, 7, 'text');
    var k = makeCtx();
    S.init(k.ctx);
    openBtns(k.ctx).forEach(click);
    var seen = [];
    tree(k.ctx).forEach(function (el) {
      seen.push(el.textContent);
      Object.keys(el.attrs).forEach(function (a) { seen.push(el.attrs[a]); });
    });
    var blob = seen.join('\n');
    return {
      rows: rows(k.ctx).length,
      iframes: iframes(k.ctx).length,
      leaked: ['onload', 'evil', 'BAD ', 'short'].filter(function (s) {
        return blob.indexOf(s) >= 0; })
    };
    """,
)


def test_a_feed_with_a_bad_id_is_dropped_and_leaks_nowhere(ran):
    info = ran["bad_ids_in_the_panel"]
    assert info["rows"] == 3  # only the bundled feeds; junk entries add nothing
    assert info["iframes"] == 1  # the last click left one frame, from a valid id
    assert info["leaked"] == []


# --- panel rendering ---

scenario(
    "panel_shape",
    """
    var S = window.SDAIssCam;
    var a = makeCtx(), b = makeCtx();
    var first = S.init(a.ctx);
    var panel = a.ctx.mount.children[0];
    S.init(b.ctx);
    return {
      first: first,
      panels: a.ctx.mount.children.length,
      cls: panel.className,
      h3: panel.children[0].tagName,
      title: panel.children[0].textContent,
      body: panel.children[1].className,
      rows: rows(a.ctx).length,
      feeds: S.FEEDS.length,
      buttons: openBtns(a.ctx).map(cls),
      styles: document.head.children.length,
      find: byClass(a.ctx.mount, 'isscam-find').map(cls)
    };
    """,
)


def test_panel_uses_the_page_markup_and_one_style_block(ran):
    info = ran["panel_shape"]
    assert info["first"] is True
    assert info["panels"] == 1
    assert info["cls"].split() == ["panel"]
    assert info["h3"] == "H3"
    assert info["title"] == PANEL_TITLE
    assert info["body"] == "panel-body"
    assert info["styles"] == 1  # a second init must not add a second style block
    assert all("mode-btn" in c.split() for c in info["buttons"])
    assert all("mode-btn" in c.split() for c in info["find"])


def test_one_row_per_feed(ran):
    info = ran["panel_shape"]
    assert info["rows"] == info["feeds"] == 3
    assert len(info["buttons"]) == 3


scenario(
    "row_text_and_links",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    return rows(k.ctx).map(function (row, i) {
      var link = byTag(row, 'A')[0];
      return {
        text: row.textContent,
        label: S.FEEDS[i].label,
        detail: S.FEEDS[i].detail,
        videoId: S.FEEDS[i].videoId,
        href: link.getAttribute('href'),
        target: link.getAttribute('target'),
        rel: link.getAttribute('rel')
      };
    });
    """,
)


def test_rows_show_label_and_detail_and_link_to_the_youtube_page(ran):
    for row in ran["row_text_and_links"]:
        assert row["label"] in row["text"]
        assert row["detail"] in row["text"]
        assert row["href"] == "https://www.youtube.com/watch?v=" + row["videoId"]
        assert row["target"] == "_blank"
        assert set(row["rel"].split()) == {"noopener", "noreferrer"}


# --- nothing loads until a click ---

scenario(
    "before_any_click",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    document.dispatch('sda:satellite-selected', { noradId: 25544, name: 'ISS' });
    var withSrc = tree(k.ctx).filter(function (el) { return 'src' in el.attrs; });
    return {
      iframes: iframes(k.ctx).length,
      frames: frames().length,
      withSrc: withSrc.length,
      bodyKids: document.body.children.length,
      pressed: pressed(k.ctx)
    };
    """,
)


def test_no_iframe_and_no_source_attribute_before_a_click(ran):
    info = ran["before_any_click"]
    assert info["iframes"] == 0
    assert info["frames"] == 0
    assert info["withSrc"] == 0
    assert info["bodyKids"] == 0
    assert info["pressed"] == ["false", "false", "false"]


# --- opening, switching, closing ---

scenario(
    "open_first",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    click(openBtns(k.ctx)[0]);
    var frame = frames()[0];
    var frame_iframes = byTag(frame, 'IFRAME');
    var f = iframes(k.ctx)[0];
    var url = new URL(f.getAttribute('src'));
    var foot = byTag(frame, 'A')[0];
    return {
      frames: frames().length,
      iframes: iframes(k.ctx).length,
      inFrame: frame_iframes.length,
      framePlacedOnBody: frame.parentNode === document.body,
      src: f.getAttribute('src'),
      protocol: url.protocol,
      host: url.hostname,
      path: url.pathname,
      allow: f.getAttribute('allow'),
      policy: f.getAttribute('referrerpolicy'),
      title: f.getAttribute('title'),
      label: S.FEEDS[0].label,
      videoId: S.FEEDS[0].videoId,
      source: S.FEEDS[0].source,
      frameText: frame.textContent,
      closeButtons: byClass(frame, 'isscam-close').length,
      closeText: byClass(frame, 'isscam-close')[0].textContent,
      sourceText: byClass(frame, 'isscam-source')[0].textContent,
      href: foot.getAttribute('href'),
      rel: foot.getAttribute('rel'),
      target: foot.getAttribute('target'),
      pressed: pressed(k.ctx)
    };
    """,
)


def test_a_click_creates_exactly_one_iframe_on_the_nocookie_host(ran):
    info = ran["open_first"]
    assert info["frames"] == 1
    assert info["iframes"] == 1
    assert info["inFrame"] == 1
    assert info["framePlacedOnBody"] is True  # not inside the sidebar column
    assert info["protocol"] == "https:"
    assert info["host"] == "www.youtube-nocookie.com"
    assert info["path"] == "/embed/" + info["videoId"]
    assert info["src"].startswith("https://www.youtube-nocookie.com/embed/")


def test_iframe_carries_the_required_attributes(ran):
    info = ran["open_first"]
    assert info["allow"] == "autoplay; encrypted-media; picture-in-picture; fullscreen"
    assert info["policy"] == "strict-origin-when-cross-origin"
    assert info["label"] in info["title"]


def test_frame_has_a_close_button_a_source_label_and_a_fallback_link(ran):
    info = ran["open_first"]
    assert info["closeButtons"] == 1
    assert info["closeText"].strip()
    assert info["source"] in info["sourceText"]
    assert info["label"] in info["frameText"]
    assert info["href"] == "https://www.youtube.com/watch?v=" + info["videoId"]
    assert info["target"] == "_blank"
    assert set(info["rel"].split()) == {"noopener", "noreferrer"}
    assert info["pressed"] == ["true", "false", "false"]


scenario(
    "open_second",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    click(openBtns(k.ctx)[0]);
    var firstFrame = frames()[0];
    var firstIframe = iframes(k.ctx)[0];
    click(openBtns(k.ctx)[1]);
    var now = iframes(k.ctx);
    return {
      frames: frames().length,
      sameFrame: frames()[0] === firstFrame,
      iframes: now.length,
      firstDetached: firstIframe.parentNode === null,
      src: now[0].getAttribute('src'),
      want: S.FEEDS[1].videoId,
      label: byClass(frames()[0], 'isscam-label')[0].textContent,
      wantLabel: S.FEEDS[1].label,
      title: now[0].getAttribute('title'),
      pressed: pressed(k.ctx)
    };
    """,
)


def test_opening_a_second_feed_replaces_the_first(ran):
    info = ran["open_second"]
    assert info["frames"] == 1 and info["sameFrame"] is True
    assert info["iframes"] == 1
    assert info["firstDetached"] is True  # the old player is gone, so it stops playing
    assert info["want"] in info["src"]
    assert info["label"] == info["wantLabel"]
    assert info["wantLabel"] in info["title"]
    assert info["pressed"] == ["false", "true", "false"]


scenario(
    "close_paths",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    var out = {};

    click(openBtns(k.ctx)[0]);
    var f1 = iframes(k.ctx)[0];
    click(byClass(frames()[0], 'isscam-close')[0]);
    out.afterClose = { iframes: iframes(k.ctx).length, frames: frames().length,
                       detached: f1.parentNode === null, pressed: pressed(k.ctx),
                       rows: rows(k.ctx).length };

    click(openBtns(k.ctx)[2]);
    var f2 = iframes(k.ctx)[0];
    var frame = frames()[0];
    var ignored = frame.fire('keydown', { key: 'a' });
    out.otherKey = iframes(k.ctx).length;
    frame.fire('keydown', { key: 'Escape' });
    out.afterEscape = { iframes: iframes(k.ctx).length, detached: f2.parentNode === null };

    click(openBtns(k.ctx)[1]);
    click(openBtns(k.ctx)[1]);
    out.afterToggle = { iframes: iframes(k.ctx).length, frames: frames().length,
                        pressed: pressed(k.ctx) };

    click(openBtns(k.ctx)[0]);
    out.reopened = iframes(k.ctx).length;
    return out;
    """,
)


def test_close_button_removes_the_iframe_from_the_dom(ran):
    after = ran["close_paths"]["afterClose"]
    assert after["iframes"] == 0
    assert after["frames"] == 0
    assert after["detached"] is True
    assert after["pressed"] == ["false", "false", "false"]
    assert after["rows"] == 3  # the sidebar stays; only the player goes


def test_escape_closes_the_frame_and_other_keys_do_not(ran):
    info = ran["close_paths"]
    assert info["otherKey"] == 1
    assert info["afterEscape"] == {"iframes": 0, "detached": True}


def test_pressing_the_open_feed_again_closes_it_and_a_closed_frame_reopens(ran):
    info = ran["close_paths"]
    assert info["afterToggle"] == {"iframes": 0, "frames": 0, "pressed": ["false"] * 3}
    assert info["reopened"] == 1


scenario(
    "enlarge",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    click(openBtns(k.ctx)[0]);
    var frame = frames()[0];
    var player = iframes(k.ctx)[0];
    var btn = byClass(frame, 'isscam-size')[0];
    var before = { big: frame.has('isscam-big'), text: btn.textContent };
    click(btn);
    var mid = { big: frame.has('isscam-big'), text: btn.textContent,
                same: iframes(k.ctx)[0] === player, count: iframes(k.ctx).length };
    click(btn);
    return { before: before, mid: mid, after: frame.has('isscam-big') };
    """,
)


def test_enlarge_toggles_a_class_without_reloading_the_player(ran):
    info = ran["enlarge"]
    assert info["before"]["big"] is False
    assert info["mid"]["big"] is True
    assert info["mid"]["same"] is True and info["mid"]["count"] == 1
    assert info["mid"]["text"] != info["before"]["text"]
    assert info["after"] is False


# --- feed text is data ---

scenario(
    "hostile_text",
    """
    var S = window.SDAIssCam;
    var hostile = '<img src=x onerror=alert(1)>';
    S.FEEDS.push({ id: 'evil', label: hostile, detail: '<b>bold</b>',
                   source: '<script>alert(2)</script>', videoId: 'aaaaaaaaaaa' });
    var k = makeCtx();
    S.init(k.ctx);
    var evilIndex = S.FEEDS.length - 1;
    var rowText = rows(k.ctx)[evilIndex].textContent;
    click(openBtns(k.ctx)[evilIndex]);
    var frame = frames()[0];
    var strange = tree(k.ctx).filter(function (el) {
      return ['IMG', 'SCRIPT', 'B'].indexOf(el.tagName) >= 0; });
    return {
      hostile: hostile,
      rowText: rowText,
      label: byClass(frame, 'isscam-label')[0].textContent,
      source: byClass(frame, 'isscam-source')[0].textContent,
      title: iframes(k.ctx)[0].getAttribute('title'),
      strange: strange.length,
      rows: rows(k.ctx).length
    };
    """,
)


def test_hostile_feed_text_is_rendered_as_text_never_html(ran):
    info = ran["hostile_text"]
    assert info["rows"] == 4
    assert info["hostile"] in info["rowText"]
    assert "<b>bold</b>" in info["rowText"]
    assert info["label"] == info["hostile"]
    assert "<script>alert(2)</script>" in info["source"]
    assert info["hostile"] in info["title"]
    assert info["strange"] == 0


# --- selecting the ISS on the globe ---

scenario(
    "selection_event",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    var title = k.ctx.mount.children[0].children[0].textContent;
    document.dispatch('sda:satellite-selected', { noradId: 25544, name: 'ISS (ZARYA)' });
    var afterIss = k.calls.feed.slice();
    var openedByEvent = iframes(k.ctx).length + frames().length;
    document.dispatch('sda:satellite-selected', { noradId: 25545, name: 'OTHER' });
    document.dispatch('sda:satellite-selected', { noradId: 44713, name: 'STARLINK-1007' });
    document.dispatch('sda:satellite-selected', { noradId: null });
    document.dispatch('sda:satellite-selected', {});
    document.dispatch('sda:satellite-selected', undefined);
    var afterOthers = k.calls.feed.length;
    document.dispatch('sda:satellite-selected', { noradId: '25544' });
    return { title: title, afterIss: afterIss, openedByEvent: openedByEvent,
             afterOthers: afterOthers, afterString: k.calls.feed.length, select: k.calls.select };
    """,
)


def test_iss_selection_writes_one_feed_line_pointing_at_the_panel(ran):
    info = ran["selection_event"]
    assert len(info["afterIss"]) == 1
    assert info["title"] in info["afterIss"][0]
    assert info["openedByEvent"] == 0  # never auto-plays
    assert info["select"] == []


def test_other_satellites_and_malformed_events_write_nothing(ran):
    info = ran["selection_event"]
    assert info["afterOthers"] == 1  # still only the ISS line
    assert info["afterString"] == 2  # a digit-string id for the ISS is still the ISS


scenario(
    "selection_after_reinit",
    """
    var S = window.SDAIssCam;
    var a = makeCtx(), b = makeCtx();
    S.init(a.ctx);
    S.init(b.ctx);
    document.dispatch('sda:satellite-selected', { noradId: 25544 });
    return { a: a.calls.feed.length, b: b.calls.feed.length,
             listeners: document.listeners['sda:satellite-selected'].length };
    """,
)


def test_a_second_init_does_not_double_the_feed_line(ran):
    info = ran["selection_after_reinit"]
    assert info == {"a": 0, "b": 1, "listeners": 1}


# --- "Find the ISS" ---

scenario(
    "find_present",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    var btn = byClass(k.ctx.mount, 'isscam-find')[0];
    click(btn);
    return { text: btn.textContent, select: k.calls.select, feed: k.calls.feed,
             status: byClass(k.ctx.mount, 'isscam-status')[0].textContent,
             iframes: iframes(k.ctx).length };
    """,
)


def test_find_the_iss_selects_it_when_it_is_in_the_catalog(ran):
    info = ran["find_present"]
    assert "ISS" in info["text"]
    assert info["select"] == [25544]
    assert info["feed"] == []  # the page's own selection event writes the line
    assert info["iframes"] == 0
    assert "ISS" in info["status"]


scenario(
    "find_absent",
    """
    var S = window.SDAIssCam;
    function attempt(opts, tweak) {
      var k = makeCtx(opts);
      if (tweak) tweak(k.ctx);
      S.init(k.ctx);
      click(byClass(k.ctx.mount, 'isscam-find')[0]);
      return { select: k.calls.select,
               status: byClass(k.ctx.mount, 'isscam-status')[0].textContent };
    }
    return {
      empty: attempt({ byNoradId: {} }),
      other: attempt({ byNoradId: { 44713: { noradId: 44713, name: 'STARLINK-1007' } } }),
      undef: attempt({ byNoradId: undefined }),
      noEngine: attempt({}, function (ctx) { delete ctx.satEngine; }),
      broken: attempt({}, function (ctx) {
        Object.defineProperty(ctx.satEngine, 'byNoradId', {
          get: function () { throw new Error('engine gone'); } });
      }),
      selectThrows: attempt({}, function (ctx) {
        ctx.selectSatellite = function () { throw new Error('select failed'); };
      })
    };
    """,
)


@pytest.mark.parametrize("case", ["empty", "other", "undef", "noEngine", "broken"])
def test_find_the_iss_says_so_plainly_when_it_is_not_in_the_catalog(ran, case):
    info = ran["find_absent"][case]
    assert info["select"] == []
    assert "25544" in info["status"]
    assert "not" in info["status"].lower()


def test_find_the_iss_survives_a_page_that_throws(ran):
    info = ran["find_absent"]["selectThrows"]
    assert info["status"].strip()


scenario(
    "find_logs_once",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    k.ctx.selectSatellite = function (id) {
      k.calls.select.push(id);
      document.dispatch('sda:satellite-selected', { noradId: id, name: 'ISS (ZARYA)' });
    };
    S.init(k.ctx);
    click(byClass(k.ctx.mount, 'isscam-find')[0]);
    return { feed: k.calls.feed.length, select: k.calls.select };
    """,
)


def test_find_the_iss_adds_one_feed_line_in_total(ran):
    info = ran["find_logs_once"]
    assert info == {"feed": 1, "select": [25544]}


# --- caption, layout, robustness ---

scenario(
    "caption",
    """
    var S = window.SDAIssCam;
    function captionFor(opts) {
      var k = makeCtx(opts);
      S.init(k.ctx);
      click(openBtns(k.ctx)[0]);
      return byClass(frames()[0], 'isscam-caption')[0].textContent;
    }
    var live = captionFor({});
    var none = captionFor({ byNoradId: {} });
    return { live: live, none: none };
    """,
)


def test_caption_prefers_the_live_altitude_and_falls_back_to_the_donor_constant(ran):
    info = ran["caption"]
    assert "420" in info["live"] and "~" not in info["live"].split("|")[0]
    assert "ORBIT" in info["live"]
    assert "~408" in info["none"] and "ORBIT" in info["none"]


scenario(
    "layout",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    S.init(k.ctx);
    var css = document.head.children[0].textContent;
    return { layout: S.LAYOUT, css: css, styleId: document.head.children[0].id };
    """,
)


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


def test_frame_layout_clears_the_fixed_page_chrome(ran):
    info = ran["layout"]
    lay = info["layout"]
    assert lay["left"] >= LEFT_COLUMN_RIGHT_EDGE
    assert lay["top"] >= HEADER_HEIGHT
    assert lay["right"] >= RIGHT_COLUMN_WIDTH
    assert lay["bottom"] >= TIME_BAR_HEIGHT
    rule = _rule(info["css"], ".isscam-frame")
    assert "position: fixed" in rule
    assert f"left: {lay['left']}px" in rule
    assert f"top: {lay['top']}px" in rule
    # The width never lets the right edge pass the right column, and the 16:9 player
    # plus its top and bottom bars never reaches the time bar.
    width = re.search(r"(?<![\w-])width:\s*([^;]*);", rule).group(1)
    assert f"100vw - {lay['left'] + lay['right']}px" in width
    reserved = lay["top"] + lay["bottom"] + lay["chrome"]
    assert f"100vh - {reserved}px" in width
    z = int(re.search(r"z-index:\s*(\d+)", rule).group(1))
    assert z < 100  # the page's keyboard help and search overlays stay on top


scenario(
    "robust_init",
    """
    var S = window.SDAIssCam;
    var k = makeCtx();
    var throwing = makeCtx();
    throwing.ctx.mount.appendChild = function () { throw new Error('no mount'); };
    var results = [S.init(null), S.init(undefined), S.init({}), S.init({ mount: {} }),
                   S.init(throwing.ctx)];
    var good = S.init(k.ctx);

    var hostile = makeCtx();
    hostile.ctx.addIntelItem = function () { throw new Error('feed failed'); };
    S.init(hostile.ctx);
    document.dispatch('sda:satellite-selected', { noradId: 25544 });
    click(openBtns(hostile.ctx)[0]);
    return { results: results, good: good, survived: true };
    """,
)


def test_init_reports_failure_and_nothing_the_page_supplies_breaks_it(ran):
    info = ran["robust_init"]
    assert info["results"] == [False] * 5
    assert info["good"] is True
    assert info["survived"] is True
