"""Conjunction analysis, history, visualization, and CDM routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from sda.cdm import compute_pc_from_cdm, generate_cdm_batch, parse_cdm
from sda.conjunction import (
    clear_conjunction_history,
    find_conjunctions,
    get_conjunction_history,
    screen_regime,
)
from sda.models import (
    CDMIngestRequest,
    CDMIngestResult,
    ConjunctionEvent,
    ConjunctionRequest,
    RegimeSurveyRequest,
    RegimeSurveyResult,
)
from sda.screening import (
    BUNDLED_FORMATION_RULES,
    FORMATION_HEURISTIC_LIMITS,
    REGIME_DEFAULT_THRESHOLD_KM,
    formation_rule_to_dict,
)
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
    """Execute the two-phase conjunction detection pipeline.

    ``method`` selects the coarse screening strategy ("auto", "allpairs" or
    "spatial"); all three return the same events. ``include_formations`` set to
    false drops events flagged as known co-orbiting formations.
    """
    from sda.api import _metrics, store

    t0 = time.monotonic()
    try:
        events = find_conjunctions(
            store=store,
            norad_ids=request.norad_ids,
            hours=request.hours,
            threshold_km=request.threshold_km,
            method=request.method,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not request.include_formations:
        events = [e for e in events if not e.formation_excluded]
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


@router.post(
    "/conjunctions/survey",
    response_model=RegimeSurveyResult,
    tags=["Conjunctions"],
    summary="All-vs-all conjunction survey restricted to one orbit regime",
    response_description="Events ranked by closest approach, plus the parameters used",
)
def survey_regime(request: RegimeSurveyRequest):
    """Screen only the objects in one regime (LEO, MEO, GEO or HEO).

    The coarse threshold defaults per regime (GEO widest) unless overridden.
    Events are ranked by ascending miss distance and truncated to ``limit``.
    """
    from sda.api import _metrics, store

    t0 = time.monotonic()
    try:
        result = screen_regime(
            store=store,
            regime=request.regime,
            hours=request.hours,
            threshold_km=request.threshold_km,
            method=request.method,
            include_formations=request.include_formations,
            limit=request.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _metrics["conjunctions_run"] += 1
    _metrics["conjunctions_found"] += len(result.events)
    _metrics["last_analysis_duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
    return result


@router.get(
    "/conjunctions/exclusions",
    tags=["Conjunctions"],
    summary="Bundled formation and co-orbiting exclusion table",
    response_description="Formation rules, their heuristic limits, and per-regime thresholds",
)
def formation_exclusions():
    """The co-orbiting sets that flag a conjunction as an expected formation pair.

    Matching events are flagged ``formation_excluded`` and kept in the output;
    POST /conjunctions and POST /conjunctions/survey drop them only when
    ``include_formations`` is false.
    """
    return {
        "rules": [formation_rule_to_dict(r) for r in BUNDLED_FORMATION_RULES],
        "count": len(BUNDLED_FORMATION_RULES),
        "heuristic_limits": list(FORMATION_HEURISTIC_LIMITS),
        "regime_default_threshold_km": dict(REGIME_DEFAULT_THRESHOLD_KM),
    }


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
        method=request.method,
    )
    if not request.include_formations:
        events = [e for e in events if not e.formation_excluded]
    tle_lookup = {t.norad_id: t for t in store.get_all()}
    pc_lookup = {}
    for e in events:
        if e.collision_probability is not None:
            pc_lookup[(e.primary, e.secondary)] = e.collision_probability.probability

    return generate_cdm_batch(events, tle_lookup=tle_lookup, pc_lookup=pc_lookup)


@router.post(
    "/conjunctions/cdm/ingest",
    response_model=CDMIngestResult,
    tags=["Conjunctions"],
    summary="Ingest a CCSDS CDM and compute Pc from its real covariances",
    response_description=(
        "Parsed CDM fields plus collision probability computed from the "
        "message's per-object RTN covariances (Foster and Chan methods)"
    ),
)
def ingest_cdm(request: CDMIngestRequest):
    """Parse an inbound CCSDS 508.0-B-1 CDM (KVN format) and compute Pc.

    Unlike POST /conjunctions (which derives uncertainty from TLE age),
    this endpoint consumes the real covariances carried by operational
    CDMs, closing the loop between CDM generation and ingestion.
    """
    try:
        parsed = parse_cdm(request.cdm_text)
        pc = compute_pc_from_cdm(parsed, hard_body_radius_km=request.hard_body_radius_km)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CDMIngestResult(parsed=parsed, pc=pc)
