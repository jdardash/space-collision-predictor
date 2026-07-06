"""WorldView — Geospatial Intelligence Dashboard.

CesiumJS-powered 3D globe with live satellite tracking, flight data,
military aircraft, seismic activity, street traffic simulation, CCTV feeds,
and military-style visual filters (CRT, NVG, FLIR).
Inspired by Bilawal Sidhu's WorldView project.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"

WORLDVIEW_HTML: str = (_TEMPLATE_DIR / "worldview.html").read_text(encoding="utf-8")
