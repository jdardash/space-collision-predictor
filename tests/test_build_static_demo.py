"""The static demo build must be self-contained and offline-safe.

These tests are the acceptance criteria for the published demo: if the baked
payloads are empty or the Ion terrain path survives, the page a screener opens
is broken and nobody finds out.
"""

import json
import os
from pathlib import Path

from scripts.build_static_demo import BAKED_ENDPOINTS, build_static_demo
from tests import conftest
from tests.test_api_lifecycle import make_tle


def test_build_emits_index_and_every_baked_endpoint(tmp_path: Path):
    build_static_demo(tmp_path)

    assert (tmp_path / "index.html").exists()
    for name in BAKED_ENDPOINTS:
        payload = tmp_path / "data" / f"{name}.json"
        assert payload.exists(), f"missing baked payload: {name}"
        json.loads(payload.read_text(encoding="utf-8"))


def test_index_declares_static_mode_and_no_ion_token(tmp_path: Path):
    build_static_demo(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert "window.STATIC_DEMO = true;" in html
    assert "Ion.defaultAccessToken = undefined" in html
    assert "fromIonAssetId" not in html


def test_positions_payload_has_real_satellites(tmp_path: Path):
    build_static_demo(tmp_path)
    raw = json.loads(
        (tmp_path / "data" / "satellite-positions.json").read_text(encoding="utf-8")
    )
    records = raw if isinstance(raw, list) else (
        raw.get("satellites") or raw.get("positions") or []
    )
    assert len(records) >= 10, f"only {len(records)} satellites baked"


def test_the_bake_asks_the_network_for_nothing(tmp_path: Path):
    """The demo is the bundled catalog on every build. A bake that left the app's
    startup fetch running sent twelve requests to CelesTrak each time and baked in
    whichever replies arrived before the last payload was written."""
    before = len(conftest.REFUSED_HOSTS)
    build_static_demo(tmp_path)
    assert conftest.REFUSED_HOSTS[before:] == []


def test_the_bake_ignores_a_catalog_cache_on_the_build_machine(tmp_path: Path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "stations.tle").write_text(make_tle(99071, "ONLY IN THE CACHE"), encoding="utf-8")
    monkeypatch.setenv("SDA_TLE_CACHE_DIR", str(cache))

    build_static_demo(tmp_path / "site")

    baked = (tmp_path / "site" / "data" / "satellite-positions.json").read_text(encoding="utf-8")
    assert "99071" not in baked
    assert "ONLY IN THE CACHE" not in baked
    # The build borrows the environment and hands it back as it found it.
    assert os.environ["SDA_TLE_CACHE_DIR"] == str(cache)
    assert "SDA_OFFLINE" not in os.environ


def test_nojekyll_present_so_pages_serves_underscore_paths(tmp_path: Path):
    build_static_demo(tmp_path)
    assert (tmp_path / ".nojekyll").exists()


def test_static_asset_tree_is_copied_next_to_index(tmp_path: Path):
    """The worker, manifest and icons must ship, or the demo silently degrades.

    Without static/js/sgp4-worker.js the page falls back to main-thread
    propagation and the object cap drops back to what one thread can carry.
    """
    build_static_demo(tmp_path)

    for relative in (
        "static/js/sgp4-worker.js",
        "static/js/styles.js",
        "static/js/tactical.js",
        "static/js/satsearch.js",
        "static/js/layers.js",
        "static/js/passpanel.js",
        "static/js/agentpanel.js",
        "static/js/isscam.js",
        "static/js/categories.js",
        "static/js/conjactions.js",
        "static/manifest.webmanifest",
        "static/icon.svg",
        "static/icon-maskable.svg",
    ):
        assert (tmp_path / relative).exists(), f"missing static asset: {relative}"

    manifest = json.loads(
        (tmp_path / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
    )
    assert manifest["start_url"] == "../", "manifest must point at the demo root"
    assert manifest["icons"], "manifest declares no icons"


def test_service_worker_sits_at_the_demo_root(tmp_path: Path):
    """Scope: a worker under static/ could not control index.html.

    GitHub Pages cannot send Service-Worker-Allowed, so the only way to get
    root scope is to serve the file from the root.
    """
    build_static_demo(tmp_path)

    root_worker = tmp_path / "sw.js"
    assert root_worker.exists()
    source = root_worker.read_text(encoding="utf-8")
    assert "addEventListener('fetch'" in source
    # Cross-origin requests (Cesium, satellite.js, Plotly, basemap tiles) must
    # never be cached; the guard below is what keeps them out.
    assert "self.location.origin" in source


def test_index_registers_the_worker_and_manifest_only_in_static_mode(tmp_path: Path):
    build_static_demo(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert "window.STATIC_DEMO = true;" in html
    assert "navigator.serviceWorker.register('sw.js')" in html
    assert "static/manifest.webmanifest" in html
    assert "if (!IS_STATIC) return;" in html, "registration must be gated on static mode"


def test_baked_assets_stay_ascii(tmp_path: Path):
    """ASCII-only is a project rule and a cp1252-console safety net."""
    build_static_demo(tmp_path)

    for relative in (
        "index.html",
        "sw.js",
        "static/js/sgp4-worker.js",
        "static/js/styles.js",
        "static/js/tactical.js",
        "static/js/satsearch.js",
        "static/js/layers.js",
        "static/js/passpanel.js",
        "static/js/agentpanel.js",
        "static/js/isscam.js",
        "static/js/categories.js",
        "static/js/conjactions.js",
    ):
        text = (tmp_path / relative).read_text(encoding="utf-8")
        offenders = sorted({ch for ch in text if ord(ch) > 126})
        assert not offenders, f"{relative} has non-ASCII: {offenders!r}"


def test_style_and_tactical_bundles_are_loaded_by_the_page(tmp_path: Path):
    """The GLSL style registry and the tactical overlays ship as plain scripts.

    Relative src paths resolve under /worldview live and under the demo root
    when baked, so one tag serves both; if either goes missing the page falls
    back to the CSS filters and drops trails, declutter and the sensor layer.
    """
    build_static_demo(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert 'src="static/js/styles.js"' in html
    assert 'src="static/js/tactical.js"' in html

    worker = (tmp_path / "sw.js").read_text(encoding="utf-8")
    assert "./static/js/styles.js" in worker
    assert "./static/js/tactical.js" in worker
    assert "./data/sensors.json" in worker


# In page order. A module joins this list when its script tag joins the page.
PANEL_MODULES = (
    "satsearch", "layers", "passpanel", "agentpanel", "isscam", "categories", "conjactions",
)


def test_panel_modules_are_loaded_by_the_page_and_precached(tmp_path: Path):
    """Search, feed layers, passes, the agent and the rest each ship as one plain script.

    The page starts a module only when its global exists, so a missing tag
    fails with no message at all: the panel is simply not there.
    """
    build_static_demo(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    worker = (tmp_path / "sw.js").read_text(encoding="utf-8")

    for name in PANEL_MODULES:
        assert f'src="static/js/{name}.js"' in html
        assert f"./static/js/{name}.js" in worker

    # Each module must come after the page's own bundles and before the inline
    # script that calls initExtensions().
    assert html.index('src="static/js/tactical.js"') < html.index('src="static/js/satsearch.js"')
    last = f'src="static/js/{PANEL_MODULES[-1]}.js"'
    assert html.index(last) < html.index("function initExtensions()")


def test_sensor_catalog_is_baked_for_the_field_of_view_layer(tmp_path: Path):
    """GET /sensors is static reference data, so the cone layer works offline."""
    build_static_demo(tmp_path)

    payload = json.loads((tmp_path / "data" / "sensors.json").read_text(encoding="utf-8"))
    sensors = payload["sensors"] if isinstance(payload, dict) else payload
    assert len(sensors) >= 1, "no sensors baked"
    first = sensors[0]
    for field in ("name", "latitude_deg", "longitude_deg", "min_elevation_deg"):
        assert field in first, f"sensor record is missing {field}"


def test_static_shim_stubs_the_live_only_sensor_coverage_call(tmp_path: Path):
    """POST /sensors/coverage needs a live propagation, so it cannot be baked.

    An empty coverage list makes the page skip the highlight instead of
    inventing one, which is the same contract as the other unbaked proxies.
    """
    build_static_demo(tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert '"coverage": []' in html
    assert 'nativeFetch("data/sensors.json")' in html
