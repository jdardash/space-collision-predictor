"""The static demo build must never publish a map key or token.

GET /api/config reports CESIUM_ION_TOKEN and GOOGLE_MAPS_API_KEY from the
environment of whoever runs the build, and the build bakes that payload into
data/config.json for GitHub Pages. These tests set made-up values for both
variables and then read every byte of the output directory, so a key that
leaks through any file (not only config.json) fails here.

One build serves every test in this file (a build takes a few seconds).
"""

import json

import pytest

from scripts.build_static_demo import build_static_demo

FAKE_GOOGLE = "FAKE-TEST-GOOGLE-KEY-0001"
FAKE_ION = "FAKE-TEST-ION-TOKEN-0002"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    """Build once with both keys in the environment; return (out_dir, live payload)."""
    from sda.routes.worldview import worldview_config

    out = tmp_path_factory.mktemp("demo-keys") / "dist"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GOOGLE_MAPS_API_KEY", FAKE_GOOGLE)
        mp.setenv("CESIUM_ION_TOKEN", FAKE_ION)
        mp.delenv("SDA_TLE_CACHE_DIR", raising=False)
        live = worldview_config()
        build_static_demo(out)
    return out, live


def test_live_route_carries_the_keys_so_the_scrub_is_meaningful(built):
    """If the route stopped reporting the keys this file would pass for no reason."""
    _, live = built
    assert FAKE_GOOGLE in live.values()
    assert FAKE_ION in live.values()


def test_no_output_file_contains_a_key_value(built):
    out, _ = built
    files = [p for p in out.rglob("*") if p.is_file()]
    assert files, "build wrote nothing"
    for path in files:
        data = path.read_bytes()
        assert FAKE_GOOGLE.encode() not in data, f"google key leaked into {path}"
        assert FAKE_ION.encode() not in data, f"ion token leaked into {path}"


def test_baked_config_keeps_its_shape_with_values_nulled(built):
    out, live = built
    config = json.loads((out / "data" / "config.json").read_text(encoding="utf-8"))
    assert set(config) == set(live)
    assert all(value is None for value in config.values())
