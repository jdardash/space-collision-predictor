"""FastAPI service for the SDA collision predictor."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from sda.models import (
    ConjunctionEvent,
    ConjunctionRequest,
    SatelliteDetail,
    SatelliteSummary,
    TLERecord,
)
from sda.tle_store import TLEStore
from sda.propagator import build_satrec, propagate_at
from sda.conjunction import find_conjunctions
from sda.visualization import render_html


# Module-level store singleton
store = TLEStore()

CELESTRAK_ACTIVE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle"
CELESTRAK_STATIONS_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle"


async def _seed_catalog() -> None:
    """Attempt to load a small set of TLEs from CelesTrak on startup."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(CELESTRAK_STATIONS_URL)
            if resp.status_code == 200:
                store.load_from_text(resp.text)
    except Exception:
        pass  # Non-critical; user can ingest TLEs manually


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _seed_catalog()
    yield


app = FastAPI(
    title="Space-Domain Awareness Collision Predictor",
    version="0.1.0",
    lifespan=lifespan,
)


# --- Health ---

@app.get("/health")
def health():
    return {"status": "ok", "satellites_tracked": store.count()}


# --- Satellites ---

@app.get("/satellites", response_model=list[SatelliteSummary])
def list_satellites():
    return [
        SatelliteSummary(norad_id=t.norad_id, name=t.name, epoch=t.epoch)
        for t in store.get_all()
    ]


@app.get("/satellites/{norad_id}", response_model=SatelliteDetail)
def get_satellite(norad_id: int):
    tle = store.get(norad_id)
    if tle is None:
        raise HTTPException(status_code=404, detail="Satellite not found")

    current_state = None
    try:
        satrec = build_satrec(tle)
        current_state = propagate_at(satrec, datetime.now(timezone.utc))
    except RuntimeError:
        pass

    return SatelliteDetail(
        norad_id=tle.norad_id,
        name=tle.name,
        line1=tle.line1,
        line2=tle.line2,
        epoch=tle.epoch,
        current_state=current_state,
    )


@app.delete("/satellites/{norad_id}")
def delete_satellite(norad_id: int):
    if not store.remove(norad_id):
        raise HTTPException(status_code=404, detail="Satellite not found")
    return {"status": "removed", "norad_id": norad_id}


# --- TLE Ingestion ---

class TLEIngestBody(BaseModel):
    tle_text: str


@app.post("/tle")
def ingest_tle(body: TLEIngestBody):
    count = store.load_from_text(body.tle_text)
    return {"status": "ok", "ingested": count, "total_tracked": store.count()}


# --- Conjunction Analysis ---

@app.post("/conjunctions", response_model=list[ConjunctionEvent])
def run_conjunctions(request: ConjunctionRequest):
    events = find_conjunctions(
        store=store,
        norad_ids=request.norad_ids,
        hours=request.hours,
        threshold_km=request.threshold_km,
    )
    return events


@app.get("/conjunctions/visualize", response_class=HTMLResponse)
def visualize_conjunctions(
    hours: float = 24.0,
    threshold_km: float = 10.0,
):
    events = find_conjunctions(
        store=store,
        hours=hours,
        threshold_km=threshold_km,
    )
    html = render_html(events, store, hours)
    return HTMLResponse(content=html)


def run():
    """Entry point for the sda-server script."""
    import uvicorn
    uvicorn.run("sda.api:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    run()
