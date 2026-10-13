"""Conjunction reports: a self-contained HTML page and an optional PDF.

The HTML report is one file with inline styles and no external requests, so it
can be attached to an email or archived next to a CDM without losing its
formatting. Content is ASCII only, which keeps it readable in a Windows console
pipeline and in mail clients that mangle anything else.

The PDF variant needs fpdf2, which is not a required dependency. When it is not
importable, `render_pdf_report` raises `PdfUnavailableError` and the route turns
that into HTTP 501 with the pip hint rather than pretending to produce a file.

Distances are km, velocities km/s, epochs UTC ISO 8601, matching the rest of
the service.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sda import __version__
from sda.models import ConjunctionEvent, RiskLevel

PDF_PIP_HINT = "PDF output needs fpdf2. Install it with: pip install fpdf2"

RISK_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.CRITICAL,
    RiskLevel.HIGH,
    RiskLevel.MODERATE,
    RiskLevel.LOW,
    RiskLevel.NEGLIGIBLE,
)

_RISK_HEX: dict[str, str] = {
    RiskLevel.CRITICAL: "#c0202c",
    RiskLevel.HIGH: "#e05a00",
    RiskLevel.MODERATE: "#b58900",
    RiskLevel.LOW: "#3aa655",
    RiskLevel.NEGLIGIBLE: "#6b7280",
}


class PdfUnavailableError(RuntimeError):
    """fpdf2 is not installed, so the PDF variant cannot be produced."""


class ReportRequest(BaseModel):
    """Payload for POST /report."""

    norad_ids: list[int] | None = None
    hours: float = Field(default=24.0, gt=0, le=168.0)
    threshold_km: float = Field(default=10.0, gt=0, le=200.0)
    format: str = Field(default="html", pattern="^(html|pdf)$")
    title: str = "Conjunction Assessment Report"


# --- shared formatting helpers ---


def _pc_value(event: ConjunctionEvent) -> float | None:
    pc = event.collision_probability
    return None if pc is None else float(pc.probability)


def _pc_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3e}"


def pc_methods_for(
    event: ConjunctionEvent,
    extra: Mapping[tuple[int, int], Mapping[str, float]] | None = None,
) -> dict[str, float]:
    """Per-method Pc values for one event, when any are available.

    Three sources are merged, cheapest first: the scalar Pc on the event, a
    `methods` mapping if the probability model carries one, and an explicit
    per-pair mapping supplied by the caller. The function stays tolerant
    because the set of Pc methods is still growing.
    """
    methods: dict[str, float] = {}
    pc = event.collision_probability
    if pc is not None:
        methods["2D (Chan/Alfano)"] = float(pc.probability)
        carried = getattr(pc, "methods", None)
        if isinstance(carried, Mapping):
            for name, value in carried.items():
                try:
                    methods[str(name)] = float(value)
                except (TypeError, ValueError):
                    continue
    if extra:
        for key in ((event.primary, event.secondary), (event.secondary, event.primary)):
            supplied = extra.get(key)
            if supplied:
                for name, value in supplied.items():
                    try:
                        methods[str(name)] = float(value)
                    except (TypeError, ValueError):
                        continue
                break
    return methods


def summarize(events: Sequence[ConjunctionEvent]) -> dict[str, Any]:
    """Counts and extrema for the summary table."""
    counts = {str(level): 0 for level in RISK_ORDER}
    for event in events:
        counts[str(event.risk)] = counts.get(str(event.risk), 0) + 1

    closest = min((e.miss_distance_km for e in events), default=None)
    pcs = [pc for pc in (_pc_value(e) for e in events) if pc is not None]
    return {
        "total": len(events),
        "counts": counts,
        "closest_km": closest,
        "max_pc": max(pcs) if pcs else None,
        "events_with_pc": len(pcs),
    }


def _sorted_events(events: Sequence[ConjunctionEvent]) -> list[ConjunctionEvent]:
    rank = {str(level): i for i, level in enumerate(RISK_ORDER)}
    return sorted(events, key=lambda e: (rank.get(str(e.risk), 99), e.miss_distance_km))


# --- HTML ---

_CSS = """
body { font-family: "Segoe UI", Arial, sans-serif; margin: 0; padding: 24px;
       background: #f5f6f8; color: #1c1f26; }
h1 { font-size: 22px; margin: 0 0 4px 0; }
h2 { font-size: 16px; margin: 24px 0 8px 0; border-bottom: 1px solid #d4d7dd;
     padding-bottom: 4px; }
.meta { color: #5b6270; font-size: 12px; margin-bottom: 16px; }
table { border-collapse: collapse; width: 100%; background: #ffffff;
        font-size: 13px; margin-bottom: 12px; }
th, td { border: 1px solid #d4d7dd; padding: 6px 8px; text-align: left; }
th { background: #eceef2; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.risk { font-weight: 700; }
.event { background: #ffffff; border: 1px solid #d4d7dd; padding: 12px 16px;
         margin-bottom: 12px; }
.event h3 { margin: 0 0 6px 0; font-size: 14px; }
dl { display: grid; grid-template-columns: 210px 1fr; gap: 2px 12px;
     margin: 0; font-size: 13px; }
dt { color: #5b6270; }
dd { margin: 0; font-variant-numeric: tabular-nums; }
.empty { background: #ffffff; border: 1px solid #d4d7dd; padding: 16px;
         font-size: 13px; }
footer { color: #5b6270; font-size: 11px; margin-top: 24px; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _risk_span(risk: RiskLevel | str) -> str:
    color = _RISK_HEX.get(str(risk), "#1c1f26")
    return f'<span class="risk" style="color:{color}">{_esc(risk)}</span>'


def _summary_table(summary: dict[str, Any]) -> str:
    rows = [
        f"<tr><td>{_esc(level)}</td>"
        f'<td class="num">{summary["counts"].get(str(level), 0)}</td></tr>'
        for level in RISK_ORDER
    ]
    closest = summary["closest_km"]
    closest_text = "n/a" if closest is None else f"{closest:.3f} km"
    return (
        "<table>"
        "<tr><th>Metric</th><th>Value</th></tr>"
        f'<tr><td>Events detected</td><td class="num">{summary["total"]}</td></tr>'
        f'<tr><td>Closest approach</td><td class="num">{_esc(closest_text)}</td></tr>'
        f'<tr><td>Highest Pc</td><td class="num">{_esc(_pc_text(summary["max_pc"]))}</td></tr>'
        f'<tr><td>Events with Pc</td><td class="num">{summary["events_with_pc"]}</td></tr>'
        "</table>"
        "<table>"
        "<tr><th>Risk level</th><th>Count</th></tr>" + "".join(rows) + "</table>"
    )


def _event_block(
    event: ConjunctionEvent,
    extra: Mapping[tuple[int, int], Mapping[str, float]] | None,
) -> str:
    methods = pc_methods_for(event, extra)
    method_rows = "".join(
        f"<dt>Pc, {_esc(name)}</dt><dd>{_esc(_pc_text(value))}</dd>"
        for name, value in methods.items()
    )
    pc = event.collision_probability
    covariance_rows = ""
    if pc is not None:
        covariance_rows = (
            f"<dt>Combined sigma</dt><dd>{pc.combined_sigma_km:.4f} km</dd>"
            f"<dt>Mahalanobis distance</dt><dd>{pc.mahalanobis_distance:.3f}</dd>"
            f"<dt>Hard-body radius</dt><dd>{pc.hard_body_radius_km:.5f} km</dd>"
        )
    primary = f"{event.primary} {event.primary_name}".strip()
    secondary = f"{event.secondary} {event.secondary_name}".strip()
    return (
        '<div class="event">'
        f"<h3>{_esc(primary)} vs {_esc(secondary)} - {_risk_span(event.risk)}</h3>"
        "<dl>"
        f"<dt>TCA (UTC)</dt><dd>{_esc(event.tca.isoformat())}</dd>"
        f"<dt>Miss distance</dt><dd>{event.miss_distance_km:.4f} km</dd>"
        f"<dt>Relative velocity</dt><dd>{event.relative_velocity_km_s:.4f} km/s</dd>"
        f"{covariance_rows}{method_rows}"
        "</dl></div>"
    )


def render_html_report(
    events: Sequence[ConjunctionEvent],
    title: str = "Conjunction Assessment Report",
    hours: float | None = None,
    threshold_km: float | None = None,
    generated_at: datetime | None = None,
    pc_methods: Mapping[tuple[int, int], Mapping[str, float]] | None = None,
) -> str:
    """Render a standalone HTML report. No external requests, ASCII only."""
    moment = datetime.now(UTC) if generated_at is None else generated_at
    ordered = _sorted_events(events)
    summary = summarize(ordered)

    scope_bits = []
    if hours is not None:
        scope_bits.append(f"window {hours:g} h")
    if threshold_km is not None:
        scope_bits.append(f"screening threshold {threshold_km:g} km")
    scope = ", ".join(scope_bits) if scope_bits else "scope not specified"

    if ordered:
        body = "".join(_event_block(event, pc_methods) for event in ordered)
    else:
        body = (
            '<div class="empty">No conjunctions crossed the screening threshold '
            "in this window.</div>"
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_esc(title)}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{_esc(title)}</h1>"
        f'<p class="meta">Generated {_esc(moment.isoformat())} by '
        f"space-collision-predictor {_esc(__version__)} - {_esc(scope)}. "
        "Positions are ECI (TEME) in km and km/s; SGP4 with the WGS72 gravity model.</p>"
        "<h2>Summary</h2>"
        f"{_summary_table(summary)}"
        "<h2>Events</h2>"
        f"{body}"
        "<footer>Pc values are estimates. TLE-derived covariance grows with element-set "
        "age, so treat a single number as an order of magnitude, not a decision.</footer>"
        "</body></html>"
    )


# --- PDF ---


def pdf_available() -> bool:
    """True when fpdf2 can be imported."""
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return False
    return True


def render_pdf_report(
    events: Sequence[ConjunctionEvent],
    title: str = "Conjunction Assessment Report",
    hours: float | None = None,
    threshold_km: float | None = None,
    generated_at: datetime | None = None,
    pc_methods: Mapping[tuple[int, int], Mapping[str, float]] | None = None,
) -> bytes:
    """Render the same content as a PDF. Requires fpdf2."""
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise PdfUnavailableError(PDF_PIP_HINT) from exc

    moment = datetime.now(UTC) if generated_at is None else generated_at
    ordered = _sorted_events(events)
    summary = summarize(ordered)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    scope_bits = []
    if hours is not None:
        scope_bits.append(f"window {hours:g} h")
    if threshold_km is not None:
        scope_bits.append(f"threshold {threshold_km:g} km")
    scope = ", ".join(scope_bits) if scope_bits else "scope not specified"
    pdf.multi_cell(
        0,
        5,
        f"Generated {moment.isoformat()} by space-collision-predictor {__version__}\n"
        f"{scope}. Positions are ECI (TEME) in km and km/s; SGP4 with WGS72.",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    closest = summary["closest_km"]
    closest_text = "n/a" if closest is None else f"{closest:.3f} km"
    summary_lines = [
        f"Events detected: {summary['total']}",
        f"Closest approach: {closest_text}",
        f"Highest Pc: {_pc_text(summary['max_pc'])}",
        "Risk mix: "
        + ", ".join(f"{level} {summary['counts'].get(str(level), 0)}" for level in RISK_ORDER),
    ]
    for line in summary_lines:
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Events", new_x="LMARGIN", new_y="NEXT")

    if not ordered:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, "No conjunctions crossed the screening threshold.", new_x="LMARGIN",
                 new_y="NEXT")
    for event in ordered:
        pdf.set_font("Helvetica", "B", 10)
        primary = f"{event.primary} {event.primary_name}".strip()
        secondary = f"{event.secondary} {event.secondary_name}".strip()
        pdf.multi_cell(
            0, 5, f"{primary} vs {secondary} [{event.risk}]", new_x="LMARGIN", new_y="NEXT"
        )
        pdf.set_font("Helvetica", "", 9)
        lines = [
            f"TCA (UTC): {event.tca.isoformat()}",
            f"Miss distance: {event.miss_distance_km:.4f} km",
            f"Relative velocity: {event.relative_velocity_km_s:.4f} km/s",
        ]
        for name, value in pc_methods_for(event, pc_methods).items():
            lines.append(f"Pc, {name}: {_pc_text(value)}")
        for line in lines:
            pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    output = pdf.output()
    return bytes(output)
