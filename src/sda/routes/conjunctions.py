"""Conjunction analysis, history, visualization, and CDM routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from sda.cdm import generate_cdm_batch
from sda.conjunction import clear_conjunction_history, find_conjunctions, get_conjunction_history
from sda.models import ConjunctionEvent, ConjunctionRequest
from sda.visualization import render_html

router = APIRouter()


@router.post(
    "/conjunctions",
    response_model=list[ConjunctionEvent],
    tags=["Conjunctions"],
    summary="Run conjunction analysis with collision probability",
    response_description="Conjunction events sorted by risk, each with Pc estimate",
)
def run_conjunctions(request: ConjunctionRequest):
    """Execute the two-phase conjunction detection pipeline."""
    from sda.api import _metrics, store

    t0 = time.monotonic()
    events = find_conjunctions(
        store=store,
        norad_ids=request.norad_ids,
        hours=request.hours,
        threshold_km=request.threshold_km,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000
    _metrics["conjunctions_run"] += 1
    _metrics["conjunctions_found"] += len(events)
    _metrics["last_analysis_duration_ms"] = round(elapsed_ms, 2)
    return events


@router.get(
    "/conjunctions/history",
    response_model=list[ConjunctionEvent],
    tags=["Conjunctions"],
    summary="Historical conjunction events",
)
def conjunction_history(
    norad_id: int | None = Query(default=None, description="Filter by NORAD ID"),
    limit: int = Query(default=100, le=1000, description="Max events to return"),
):
    """Returns previously detected conjunction events from the in-memory history."""
    return get_conjunction_history(norad_id=norad_id, limit=limit)


@router.delete("/conjunctions/history", tags=["Conjunctions"], summary="Clear conjunction history")
def clear_history():
    """Clears all stored conjunction history."""
    count = clear_conjunction_history()
    return {"status": "cleared", "events_removed": count}


@router.get(
    "/conjunctions/visualize",
    response_class=HTMLResponse,
    tags=["Conjunctions"],
    summary="Interactive 3D conjunction visualization with screening volumes",
)
def visualize_conjunctions(
    hours: float = Query(default=24.0, description="Analysis window in hours"),
    threshold_km: float = Query(default=10.0, description="Screening threshold in km"),
):
    """Render an interactive 3D Plotly visualization."""
    from sda.api import store

    events = find_conjunctions(store=store, hours=hours, threshold_km=threshold_km)
    html = render_html(events, store, hours)
    return HTMLResponse(content=html)


@router.post(
    "/conjunctions/cdm",
    tags=["Conjunctions"],
    summary="Generate CCSDS Conjunction Data Messages",
    response_description="Array of CDM text blocks in CCSDS 508.0-B-1 format",
)
def generate_cdms(request: ConjunctionRequest):
    """Run conjunction analysis and generate CCSDS CDM for each detected event."""
    from sda.api import store

    events = find_conjunctions(
        store=store,
        norad_ids=request.norad_ids,
        hours=request.hours,
        threshold_km=request.threshold_km,
    )
    tle_lookup = {t.norad_id: t for t in store.get_all()}
    pc_lookup = {}
    for e in events:
        if e.collision_probability is not None:
            pc_lookup[(e.primary, e.secondary)] = e.collision_probability.probability

    return generate_cdm_batch(events, tle_lookup=tle_lookup, pc_lookup=pc_lookup)
