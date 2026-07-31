"""Bake the WorldView globe into a self-contained static site.

Precomputes the API payloads the globe needs, flips the template into static
mode, and writes everything under an output directory suitable for GitHub
Pages.

Why static rather than a deployed app: a free-tier backend cold-starts for the
better part of a minute, and a screener who clicks a demo link reads that as
broken. This build cannot 500 and cannot cold-start.

The live-only proxies (flights, earthquakes, military flights, traffic,
cameras, geocode) are deliberately excluded. They need upstream services and
they dilute the space-domain story; the demo shows propagation and conjunction
screening, which is what the project is actually about. The full application
still runs with `make docker`.

No offline flag is needed: src/sda/api.py loads the bundled demo TLEs
synchronously in its lifespan hook and defers the CelesTrak fetch to a
background task, so the payloads below are populated without network access.

Run: python scripts/build_static_demo.py dist
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "src" / "sda" / "templates" / "worldview.html"

sys.path.insert(0, str(REPO_ROOT / "src"))

# name -> (method, path, json body). The globe requests conjunction data by
# POST with the analysis window in the body; the static build bakes the
# template's own defaults (ConjunctionRequest: hours=24, threshold_km=10) so
# the page shows the same result it would live. The fetch shim turns the
# page's POST back into a GET against the baked file.
BAKED_ENDPOINTS: dict[str, tuple[str, str, dict | None]] = {
    "tles": ("GET", "/api/tles", None),
    "satellite-positions": ("GET", "/api/satellite-positions?limit=500", None),
    "satellite-orbits": ("GET", "/api/satellite-orbits", None),
    "config": ("GET", "/api/config", None),
    "conjunction-globe-data": (
        "POST",
        "/api/conjunction-globe-data",
        {"hours": 24.0, "threshold_km": 10.0},
    ),
}

STATIC_FLAG_LIVE = "window.STATIC_DEMO = false;"
STATIC_FLAG_BAKED = "window.STATIC_DEMO = true;"


def _render_index() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    if STATIC_FLAG_LIVE not in html:
        raise RuntimeError(
            "static-mode shim missing from worldview.html; "
            f"expected the literal {STATIC_FLAG_LIVE!r}"
        )
    html = html.replace(STATIC_FLAG_LIVE, STATIC_FLAG_BAKED)
    # Ion terrain needs an access token and a round trip to api.cesium.com.
    # Neither belongs in a public static build, so remove the code path.
    html = html.replace("fromIonAssetId", "disabledIonAssetId")
    return html


def build_static_demo(out_dir: Path) -> list[Path]:
    from fastapi.testclient import TestClient

    from sda.api import app

    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "data").mkdir(parents=True)

    written: list[Path] = []
    with TestClient(app) as client:
        for name, (method, route, body) in BAKED_ENDPOINTS.items():
            response = (
                client.get(route)
                if method == "GET"
                else client.post(route, json=body)
            )
            response.raise_for_status()
            target = out_dir / "data" / f"{name}.json"
            target.write_text(json.dumps(response.json()), encoding="utf-8")
            written.append(target)

    index = out_dir / "index.html"
    index.write_text(_render_index(), encoding="utf-8")
    written.append(index)

    # GitHub Pages runs Jekyll by default, which drops paths beginning with an
    # underscore. Cesium's asset tree contains them.
    nojekyll = out_dir / ".nojekyll"
    nojekyll.write_text("", encoding="utf-8")
    written.append(nojekyll)

    return written


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "dist"
    for path in build_static_demo(out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
