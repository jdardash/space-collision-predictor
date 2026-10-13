"""The globe's basemap switch, run for real instead of read as text.

The basemap block of worldview.html is lifted out and executed in Node against a
stub Cesium, a stub viewer and four stub buttons, so these tests see what the
page would actually hand to Cesium and what it does to the layer stack.

Two failures put this file here. The NASA layers were first wired to GIBS's
geographic (EPSG:4326) tiles with Cesium's stock GeographicTilingScheme. That
GIBS grid is 2x1, 3x2, 5x3, 10x5 tiles, not a doubling pyramid, so the whole
world was drawn at 62.5 percent scale, anchored at the north-west corner, with
black over the rest of the globe and every satellite sitting over the wrong
country. And a share link carrying bm=constructor found Object.prototype's
constructor in the registry, removed the layers and threw during page start.

They skip cleanly when Node is not installed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "src" / "sda" / "templates" / "worldview.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

GIBS_MERCATOR = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
NASA_BASEMAPS = ("earth", "daynight", "night")
HOSTILE_NAMES = ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf", "nope", ""]

# stdin is {"block": <the basemap section of the page>}. Every scenario gets a
# fresh world so one cannot leak layers or button state into the next.
HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));

function makeWorld() {
  const layers = [];
  const log = { intel: [], destroyed: [] };
  const buttons = ['earth', 'daynight', 'night', 'hires'].map(function (name) {
    const classes = new Set(name === 'earth' ? ['active'] : []);
    return {
      dataset: { basemap: name },
      disabled: false,
      classList: {
        toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
        add(c) { classes.add(c); },
        remove(c) { classes.delete(c); },
        contains(c) { return classes.has(c); },
      },
    };
  });
  const credit = { textContent: '' };
  const box = {
    Cesium: {
      UrlTemplateImageryProvider: function (opts) { this.opts = opts; },
      Credit: function (text) { this.text = text; },
      GeographicTilingScheme: function () { this.kind = 'geographic'; },
      WebMercatorTilingScheme: function () { this.kind = 'mercator'; },
    },
    viewer: {
      imageryLayers: {
        addImageryProvider(provider, index) {
          const layer = { provider: provider };
          layers.splice(index === undefined ? layers.length : index, 0, layer);
          return layer;
        },
        remove(layer, destroy) {
          log.destroyed.push(destroy);
          const at = layers.indexOf(layer);
          if (at >= 0) layers.splice(at, 1);
          return at >= 0;
        },
      },
      scene: { globe: { enableLighting: null } },
    },
    document: {
      querySelectorAll(selector) {
        return selector === '.mode-btn[data-basemap]' ? buttons : [];
      },
    },
    $: function (id) { return id === 'basemap-credit' ? credit : null; },
    addIntelItem: function (msg) { log.intel.push(msg); },
  };
  vm.createContext(box);
  vm.runInContext(input.block, box);

  function snapshot() {
    return {
      current: vm.runInContext('currentBasemap', box),
      lighting: box.viewer.scene.globe.enableLighting,
      layers: layers.map(function (layer) {
        const o = layer.provider.opts;
        return {
          url: o.url,
          maximumLevel: o.maximumLevel === undefined ? null : o.maximumLevel,
          tilingScheme: o.tilingScheme ? o.tilingScheme.kind : null,
          tileWidth: o.tileWidth === undefined ? null : o.tileWidth,
          credit: o.credit ? o.credit.text : null,
          dayAlpha: layer.dayAlpha === undefined ? null : layer.dayAlpha,
        };
      }),
      active: buttons.filter(function (b) { return b.classList.contains('active'); })
        .map(function (b) { return b.dataset.basemap; }),
      disabled: buttons.filter(function (b) { return b.disabled; })
        .map(function (b) { return b.dataset.basemap; }),
      credit: credit.textContent,
      intel: log.intel.slice(),
      destroyed: log.destroyed.slice(),
    };
  }
  return { box: box, snapshot: snapshot };
}

const out = {};
const names = Object.keys(makeWorld().box.BASEMAPS);
out.names = names;
out.defaultName = makeWorld().box.DEFAULT_BASEMAP;
out.initial = makeWorld().snapshot().current;

out.each = {};
names.forEach(function (name) {
  const w = makeWorld();
  w.box.setBasemap(name);
  out.each[name] = w.snapshot();
});

(function () {
  const w = makeWorld();
  w.box.setBasemap('daynight');
  const before = w.snapshot();
  w.box.setBasemap('earth', true);
  out.swap = { before: before, after: w.snapshot() };
})();

(function () {
  const w = makeWorld();
  w.box.setBasemap('night');
  const before = w.snapshot();
  const thrown = {};
  input.hostile.concat([null, undefined, 7]).forEach(function (name) {
    try { w.box.setBasemap(name); thrown[String(name)] = null; }
    catch (e) { thrown[String(name)] = String(e && e.message); }
  });
  out.hostile = { before: before, after: w.snapshot(), thrown: thrown };
})();

(function () {
  const w = makeWorld();
  const seen = [];
  for (let i = 0; i < names.length + 1; i++) {
    w.box.cycleBasemap();
    seen.push(w.snapshot().current);
  }
  out.cycle = seen;
})();

(function () {
  const w = makeWorld();
  w.box.setBasemap('hires');
  w.box.lockBasemap('Google Photorealistic 3D Tiles');
  const locked = w.snapshot();
  w.box.setBasemap('earth');
  w.box.cycleBasemap();
  out.lock = { locked: locked, after: w.snapshot() };
})();

process.stdout.write(JSON.stringify(out));
"""


def _basemap_block() -> str:
    page = PAGE.read_text(encoding="utf-8")
    start = page.index("// ===== BASEMAP =====")
    return page[start:page.index("// ===== EXTENSION MODULES =====", start)]


@pytest.fixture(scope="module")
def run() -> dict:
    """One Node process for the whole file; each test reads its own scenario."""
    done = subprocess.run(
        ["node", "-e", HARNESS],
        input=json.dumps({"block": _basemap_block(), "hostile": HOSTILE_NAMES}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_registry_and_default(run):
    assert run["names"] == ["earth", "daynight", "night", "hires"]
    assert run["defaultName"] == "earth"
    assert run["initial"] == "earth"


@pytest.mark.parametrize("name", NASA_BASEMAPS)
def test_nasa_layers_use_the_grid_cesium_expects(run, name):
    """GIBS Web Mercator tiles on Cesium's default scheme: the two grids match.

    Naming a tiling scheme here, or going back to the epsg4326 endpoint, is what
    shrank the world and slid it into the north-west corner of the globe.
    """
    layers = run["each"][name]["layers"]
    assert layers, f"{name} put no imagery on the globe"
    for layer in layers:
        assert layer["url"].startswith(GIBS_MERCATOR), layer["url"]
        assert "/GoogleMapsCompatible_Level8/{z}/{y}/{x}." in layer["url"]
        assert "epsg4326" not in layer["url"]
        assert layer["tilingScheme"] is None, "GIBS imagery must ride the default scheme"
        assert layer["tileWidth"] is None, "GoogleMapsCompatible tiles are the default 256 px"
        # The set is named Level8 because level 8 is its last; level 9 answers HTTP 400.
        assert layer["maximumLevel"] == 8


def test_no_basemap_names_a_tiling_scheme_or_a_credential(run):
    for name, shot in run["each"].items():
        for layer in shot["layers"]:
            assert layer["tilingScheme"] is None, name
            assert not re.search(r"key=|token|apikey", layer["url"], re.IGNORECASE), name
            assert layer["url"].startswith("https://"), name


def test_each_basemap_stacks_the_layers_it_promises(run):
    each = run["each"]
    assert [len(each[name]["layers"]) for name in run["names"]] == [1, 2, 1, 1]

    assert "BlueMarble_ShadedRelief_Bathymetry" in each["earth"]["layers"][0]["url"]
    assert each["earth"]["layers"][0]["url"].endswith(".jpeg")
    assert "VIIRS_Black_Marble" in each["night"]["layers"][0]["url"]
    assert each["night"]["layers"][0]["url"].endswith(".png")
    assert "services.arcgisonline.com" in each["hires"]["layers"][0]["url"]

    # Daylight underneath, city lights on top and switched off on the sunlit side.
    day, lights = each["daynight"]["layers"]
    assert "BlueMarble_ShadedRelief_Bathymetry" in day["url"]
    assert "VIIRS_Black_Marble" in lights["url"]
    assert day["dayAlpha"] is None
    assert lights["dayAlpha"] == 0.0


def test_only_the_day_night_basemap_lights_the_globe(run):
    """dayAlpha does nothing on an unlit globe, and a lit globe turns the
    plain basemaps half black, so lighting has to follow the choice."""
    assert {name: shot["lighting"] for name, shot in run["each"].items()} == {
        "earth": False,
        "daynight": True,
        "night": False,
        "hires": False,
    }


def test_choosing_a_basemap_updates_the_button_the_credit_and_the_feed(run):
    for name, shot in run["each"].items():
        assert shot["current"] == name
        assert shot["active"] == [name]
        assert shot["credit"].startswith("Imagery: ")
        assert len(shot["intel"]) == 1 and shot["intel"][0].startswith("Basemap: ")
    assert "NASA" in run["each"]["earth"]["credit"]
    assert "Esri" in run["each"]["hires"]["credit"]


def test_switching_replaces_the_old_layers_and_can_be_quiet(run):
    before, after = run["swap"]["before"], run["swap"]["after"]
    assert len(before["layers"]) == 2
    assert len(after["layers"]) == 1
    assert "BlueMarble" in after["layers"][0]["url"]
    # Both day / night layers were removed, and destroyed so their tiles are freed.
    assert after["destroyed"] == [True, True]
    assert after["lighting"] is False
    assert after["intel"] == before["intel"], "a quiet switch must not write to the feed"


def test_a_name_from_a_share_link_cannot_break_the_page(run):
    """bm= is read straight from the URL. Object.prototype members are not basemaps."""
    hostile = run["hostile"]
    assert all(error is None for error in hostile["thrown"].values()), hostile["thrown"]
    assert hostile["after"] == hostile["before"]
    assert hostile["after"]["current"] == "night"
    assert len(hostile["after"]["layers"]) == 1


def test_the_i_key_walks_every_basemap_and_wraps(run):
    assert run["cycle"] == ["daynight", "night", "hires", "earth", "daynight"]


def test_google_tiles_take_the_basemap_switch_out_of_play(run):
    """Google's photorealistic tiles replace the globe surface. The buttons
    must not go on claiming a NASA basemap that is no longer on screen."""
    locked, after = run["lock"]["locked"], run["lock"]["after"]
    assert locked["active"] == []
    assert locked["disabled"] == ["earth", "daynight", "night", "hires"]
    assert locked["credit"] == "Imagery: Google Photorealistic 3D Tiles"
    assert after == locked, "a locked switch ignores both the buttons and the I key"


def test_browser_shortcuts_do_not_trigger_globe_keys():
    """Ctrl+Shift+I opens DevTools; it must not also cycle the basemap."""
    page = PAGE.read_text(encoding="utf-8")
    handler = page[page.index("// ===== KEYBOARD SHORTCUTS ====="):]
    head = handler[: handler.index("switch(e.key)")]
    assert "e.ctrlKey || e.metaKey || e.altKey" in head
