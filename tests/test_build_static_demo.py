"""The static demo build must be self-contained and offline-safe.

These tests are the acceptance criteria for the published demo: if the baked
payloads are empty or the Ion terrain path survives, the page a screener opens
is broken and nobody finds out.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.build_static_demo import BAKED_ENDPOINTS, build_static_demo


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


def test_nojekyll_present_so_pages_serves_underscore_paths(tmp_path: Path):
    build_static_demo(tmp_path)
    assert (tmp_path / ".nojekyll").exists()
