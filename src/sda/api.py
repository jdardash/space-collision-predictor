"""FastAPI service for the SDA collision predictor."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from sda import __version__
from sda.demo_tles import DEMO_TLE_TEXT
from sda.routes.analysis import router as analysis_router
from sda.routes.conjunctions import router as conjunctions_router
from sda.routes.satellites import router as satellites_router
from sda.routes.system import router as system_router
from sda.routes.worldview import router as worldview_router
from sda.tle_store import TLEStore

# Module-level store singleton
store = TLEStore()

# Performance metrics
_metrics: dict[str, Any] = {
    "propagations": 0,
    "conjunctions_run": 0,
    "conjunctions_found": 0,
    "api_requests": 0,
    "last_analysis_duration_ms": 0.0,
    "startup_time": None,
}

# CelesTrak TLE sources — live groups covering LEO, MEO, GEO, and special orbits
CELESTRAK_GROUPS = [
    "stations",          # ISS, CSS, crewed vehicles
    "active",            # All active satellites (large set)
    "visual",            # Bright/visible objects
    "starlink",          # Starlink constellation
    "gps-ops",           # GPS operational
    "galileo",           # Galileo GNSS
    "beidou",            # BeiDou GNSS
    "geo",               # Geostationary
    "weather",           # Weather satellites
    "science",           # Science missions
    "resource",          # Earth resources
    "last-30-days",      # Recently launched
]
CELESTRAK_BASE = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"

# NOAA Space Weather — live F10.7 solar flux
NOAA_F107_URL = "https://services.swpc.noaa.gov/json/solar-cycle/predicted-solar-cycle.json"
NOAA_F107_OBSERVED_URL = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"

# Live data state
_live_f107: float = 150.0  # updated from NOAA
_last_tle_refresh: datetime | None = None
_tle_refresh_task: asyncio.Task | None = None

# Response caches for external APIs
class _CacheEntry(TypedDict):
    data: dict[str, Any]
    ts: float


_cache: dict[str, _CacheEntry] = {}


def _get_cached(key: str, ttl_sec: float) -> dict[str, Any] | None:
    entry = _cache.get(key)
    if entry is not None and time.time() - entry["ts"] < ttl_sec:
        return entry["data"]
    return None


def _set_cached(key: str, data: dict[str, Any]) -> None:
    _cache[key] = {"data": data, "ts": time.time()}


# --- Background tasks ---

async def _fetch_celestrak_group(client: httpx.AsyncClient, group: str) -> int:
    """Fetch a single CelesTrak group. Returns count ingested."""
    try:
        url = CELESTRAK_BASE.format(group=group)
        resp = await client.get(url)
        if resp.status_code == 200 and resp.text.strip():
            return store.load_from_text(resp.text)
    except Exception:
        pass
    return 0


async def _seed_catalog() -> None:
    """Seed catalog from live CelesTrak data. Falls back to bundled TLEs if all groups fail."""
    global _last_tle_refresh
    live_count = 0
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            import asyncio as _aio
            tasks = [_fetch_celestrak_group(client, g) for g in CELESTRAK_GROUPS]
            results = await _aio.gather(*tasks, return_exceptions=True)
            live_count = sum(r for r in results if isinstance(r, int))
    except Exception:
        pass

    if live_count == 0:
        store.load_from_text(DEMO_TLE_TEXT)

    _last_tle_refresh = datetime.now(UTC)


async def _fetch_live_f107() -> None:
    """Fetch current F10.7 solar flux from NOAA Space Weather Prediction Center."""
    global _live_f107
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(NOAA_F107_OBSERVED_URL)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list):
                    latest = data[-1]
                    flux = float(latest.get("flux", 150.0))
                    if 50.0 < flux < 400.0:
                        _live_f107 = flux
                        return
            resp = await client.get(NOAA_F107_URL)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list):
                    now_str = datetime.now(UTC).strftime("%Y-%m")
                    for entry in data:
                        if entry.get("time-tag", "").startswith(now_str):
                            flux = float(entry.get("predicted_ssn", 150.0))
                            _live_f107 = max(70.0, 67.0 + 0.572 * flux)
                            return
    except Exception:
        pass


async def _background_refresh_loop() -> None:
    """Background task: refresh TLEs every 2 hours and F10.7 every 6 hours."""
    f107_counter = 0
    while True:
        await asyncio.sleep(7200)
        await _seed_catalog()
        f107_counter += 1
        if f107_counter % 3 == 0:
            await _fetch_live_f107()


# --- App lifecycle ---

async def _initial_live_fetch() -> None:
    """Fetch live CelesTrak + F10.7 data (runs in background after instant startup)."""
    await _seed_catalog()
    await _fetch_live_f107()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tle_refresh_task
    _metrics["startup_time"] = datetime.now(UTC).isoformat()
    # Instant startup: load bundled demo TLEs synchronously
    store.load_from_text(DEMO_TLE_TEXT)
    # Fetch live data in background — app is already serving
    _tle_refresh_task = asyncio.create_task(_initial_live_fetch())
    _bg_refresh = asyncio.create_task(_background_refresh_loop())
    yield
    for task in (_tle_refresh_task, _bg_refresh):
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


# --- App setup ---

app = FastAPI(
    title="Space-Domain Awareness Collision Predictor",
    description=(
        "Orbital mechanics engine for satellite conjunction analysis. "
        "Uses SGP4 propagation to predict close approaches between tracked objects, "
        "classify collision risk, compute collision probability (Pc), and render "
        "interactive 3D visualizations.\n\n"
        "## Features\n"
        "- **SGP4 Propagation**: WGS72 gravity model, ECI frame, vectorized NumPy\n"
        "- **Two-Phase Conjunction Detection**: Coarse (60s) → Fine (1s) refinement\n"
        "- **Collision Probability**: 2D Pc via Chan/Alfano B-plane projection\n"
        "- **Risk Classification**: CRITICAL / HIGH / MODERATE / LOW / NEGLIGIBLE\n"
        "- **CCSDS CDM Generation**: Standard conjunction data messages\n"
        "- **Monte Carlo Analysis**: Gaussian position-noise miss distance distributions\n"
        "- **Maneuver Planning**: Along-track / cross-track / radial delta-V\n"
        "- **Orbital Decay Estimation**: Atmospheric drag lifetime prediction\n"
        "- **TLE Freshness Monitoring**: Staleness warnings and accuracy alerts\n"
        "- **WebSocket Live Tracking**: Real-time satellite position updates\n"
        "- **3D Visualization**: Plotly orbits, screening volumes, conjunction markers\n"
        "- **CesiumJS WorldView**: Interactive globe with CRT/NVG/FLIR modes\n"
    ),
    version=__version__,
    lifespan=lifespan,
)

# CORS for local development and embedding
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def count_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    _metrics["api_requests"] += 1
    return await call_next(request)


# Include routers
app.include_router(worldview_router)
app.include_router(system_router)
app.include_router(satellites_router)
app.include_router(conjunctions_router)
app.include_router(analysis_router)


def run() -> None:
    """Start the server. Entry point for `sda-server` and `python -m sda.api`."""
    import uvicorn

    uvicorn.run("sda.api:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    run()
