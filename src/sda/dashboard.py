"""Embedded web dashboard for the SDA collision predictor."""

from __future__ import annotations

from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"

DASHBOARD_HTML: str = (_TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")
