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

The bake runs the app with SDA_OFFLINE set and no catalog cache, so it starts
on the bundled demo TLEs and asks CelesTrak and NOAA for nothing. Every build
bakes the same catalog, on a laptop and on a CI runner alike.

Run: python scripts/build_static_demo.py dist
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "src" / "sda" / "templates" / "worldview.html"
STATIC_SRC = REPO_ROOT / "src" / "sda" / "templates" / "static"

# Copied to the demo root rather than left under static/. A service worker can
# only control pages at or below its own URL, and widening the scope needs a
# Service-Worker-Allowed response header that GitHub Pages will not send.
ROOT_LEVEL_STATIC = ("sw.js",)

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
    # The sensor catalog is static reference data, so it bakes cleanly. Its
    # companion POST /sensors/coverage needs a live propagation and is stubbed
    # to an empty list by the page's fetch shim instead.
    "sensors": ("GET", "/sensors", None),
}

# /api/config reports the map keys held by whoever runs the build, and the
# result is baked into a file meant for GitHub Pages. Null every field whose
# name ends in key, token or secret (case-insensitive) rather than a fixed
# pair, so a token field added to the route later is scrubbed without anyone
# remembering to update this list. The shape is kept so the page reads it as is.
SECRET_FIELD_SUFFIXES = ("key", "token", "secret")

STATIC_FLAG_LIVE = "window.STATIC_DEMO = false;"
STATIC_FLAG_BAKED = "window.STATIC_DEMO = true;"


def _scrub_secrets(payload: dict) -> dict:
    """Return the payload with every credential-named field set to None."""
    return {
        field: None if field.lower().endswith(SECRET_FIELD_SUFFIXES) else value
        for field, value in payload.items()
    }


# What the app is started under for the bake (None removes the variable).
# Without SDA_OFFLINE its startup fetch runs in the background and whichever
# CelesTrak replies arrive before the last payload is written get baked in;
# with a catalog cache set, the build machine's last live download would be.
BAKE_ENVIRONMENT: dict[str, str | None] = {"SDA_OFFLINE": "1", "SDA_TLE_CACHE_DIR": None}


@contextmanager
def _bake_environment() -> Iterator[None]:
    """Apply BAKE_ENVIRONMENT for the bake and put the caller's values back after."""
    before = {name: os.environ.get(name) for name in BAKE_ENVIRONMENT}

    def apply(values: dict[str, str | None]) -> None:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    apply(BAKE_ENVIRONMENT)
    try:
        yield
    finally:
        apply(before)


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


def _copy_static_tree(out_dir: Path) -> list[Path]:
    """Copy the served static tree next to index.html.

    The page loads the SGP4 web worker, the manifest and the icons from these
    paths, and the service worker precaches them, so a demo without this tree
    would silently fall back to main-thread propagation.
    """
    written: list[Path] = []
    if not STATIC_SRC.is_dir():
        raise RuntimeError(f"static asset tree missing: {STATIC_SRC}")

    dest_root = out_dir / "static"
    shutil.copytree(STATIC_SRC, dest_root)
    written.extend(sorted(path for path in dest_root.rglob("*") if path.is_file()))

    for name in ROOT_LEVEL_STATIC:
        source = STATIC_SRC / name
        if not source.is_file():
            raise RuntimeError(f"expected {name} in {STATIC_SRC}")
        target = out_dir / name
        shutil.copyfile(source, target)
        written.append(target)

    return written


def build_static_demo(out_dir: Path) -> list[Path]:
    from fastapi.testclient import TestClient

    from sda.api import app

    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "data").mkdir(parents=True)

    written: list[Path] = []
    with _bake_environment(), TestClient(app) as client:
        for name, (method, route, body) in BAKED_ENDPOINTS.items():
            response = (
                client.get(route)
                if method == "GET"
                else client.post(route, json=body)
            )
            response.raise_for_status()
            target = out_dir / "data" / f"{name}.json"
            payload = response.json()
            if name == "config":
                payload = _scrub_secrets(payload)
            target.write_text(json.dumps(payload), encoding="utf-8")
            written.append(target)

    written.extend(_copy_static_tree(out_dir))

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
