"""Satellite tracking and TLE management routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from sda.models import SatelliteDetail, SatelliteSummary, TLEFreshness
from sda.propagator import build_satrec, propagate_at

router = APIRouter()


# --- Satellites ---

@router.get(
    "/satellites",
    response_model=list[SatelliteSummary],
    tags=["Satellites"],
    summary="List all tracked satellites",
    response_description="Array of satellite summaries with NORAD ID, name, and TLE epoch",
)
def list_satellites():
    """Returns a summary of every satellite in the tracking catalog."""
    from sda.api import store

    return [
        SatelliteSummary(norad_id=t.norad_id, name=t.name, epoch=t.epoch)
        for t in store.get_all()
    ]


@router.get(
    "/satellites/{norad_id}",
    response_model=SatelliteDetail,
    tags=["Satellites"],
    summary="Get satellite detail with current state vector",
    response_description="Full TLE data, current ECI state vector, and TLE freshness indicator",
)
def get_satellite(norad_id: int):
    """Returns TLE data, the current ECI state vector, and TLE freshness for a satellite."""
    from sda.api import store

    tle = store.get(norad_id)
    if tle is None:
        raise HTTPException(status_code=404, detail="Satellite not found")

    current_state = None
    try:
        satrec = build_satrec(tle)
        current_state = propagate_at(satrec, datetime.now(UTC))
    except RuntimeError:
        pass

    freshness = store.get_freshness(norad_id)

    return SatelliteDetail(
        norad_id=tle.norad_id,
        name=tle.name,
        line1=tle.line1,
        line2=tle.line2,
        epoch=tle.epoch,
        current_state=current_state,
        freshness=freshness,
    )


@router.delete(
    "/satellites/{norad_id}",
    tags=["Satellites"],
    summary="Remove a satellite from tracking",
)
def delete_satellite(norad_id: int):
    """Removes a satellite from the in-memory catalog."""
    from sda.api import store

    if not store.remove(norad_id):
        raise HTTPException(status_code=404, detail="Satellite not found")
    return {"status": "removed", "norad_id": norad_id}


# --- TLE Ingestion ---

class TLEIngestBody(BaseModel):
    tle_text: str

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "tle_text": (
                        "ISS (ZARYA)\n"
                        "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997\n"
                        "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"
                    )
                }
            ]
        }
    }


@router.post("/tle", tags=["TLE"], summary="Ingest TLE data")
def ingest_tle(body: TLEIngestBody):
    """Parse and ingest 2-line or 3-line TLE sets into the tracking catalog."""
    from sda.api import store

    count = store.load_from_text(body.tle_text)
    return {"status": "ok", "ingested": count, "total_tracked": store.count()}


# --- TLE Freshness ---

@router.get(
    "/tle/freshness",
    response_model=list[TLEFreshness],
    tags=["TLE"],
    summary="TLE freshness report for all satellites",
    response_description="Freshness status (FRESH/AGING/STALE/EXPIRED) with age and warnings",
)
def tle_freshness():
    """Returns TLE age and freshness status for every tracked satellite."""
    from sda.api import store

    return store.get_all_freshness()


@router.post(
    "/tle/refresh",
    tags=["TLE"],
    summary="Refresh TLEs from live CelesTrak sources",
)
async def refresh_tles():
    """Re-fetch TLE data from all CelesTrak groups and update the catalog."""
    from sda.api import _fetch_live_f107, _last_tle_refresh, _live_f107, _seed_catalog, store

    before = store.count()
    await _seed_catalog()
    await _fetch_live_f107()
    return {
        "status": "refreshed",
        "satellites_before": before,
        "satellites_after": store.count(),
        "f107_solar_flux": _live_f107,
        "timestamp": _last_tle_refresh.isoformat() if _last_tle_refresh else None,
    }


@router.get(
    "/tle/stale",
    response_model=list[TLEFreshness],
    tags=["TLE"],
    summary="List satellites with stale TLEs",
)
def stale_tles(
    threshold_hours: float = Query(default=168.0, description="Age threshold in hours"),
):
    """Returns satellites whose TLE epoch exceeds the staleness threshold."""
    from sda.api import store

    return store.get_stale_satellites(threshold_hours)
