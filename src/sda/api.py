"""FastAPI service for the SDA collision predictor."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from sda import __version__
from sda.demo_tles import DEMO_TLE_TEXT
from sda.ratelimit import (
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    bind_host,
    bind_port,
    cors_origins,
)
from sda.routes.agent import router as agent_router
from sda.routes.analysis import router as analysis_router
from sda.routes.conjunctions import router as conjunctions_router
from sda.routes.history import router as history_router
from sda.routes.ops import router as ops_router
from sda.routes.passes import router as passes_router
from sda.routes.pc import router as pc_router
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

# CelesTrak TLE sources - live groups covering LEO, MEO, GEO, and special orbits
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
    # "last-30-days" is left out on purpose. The public five-digit catalog numbers ran
    # out in July 2026 and a TLE cannot carry the six-digit numbers issued since, so in
    # this format the group answers HTTP 404 "No GP data found" on every request.
]
CELESTRAK_BASE = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"

# Optional disk cache of the last good catalog, switched on by SDA_TLE_CACHE_DIR. CelesTrak
# answers 403 when a group is requested again before its data has updated (every 2 hours), so
# a restart soon after a start would otherwise lose most of the catalog. Unset or empty means
# no cache: nothing is created, read or written.
TLE_CACHE_ENV = "SDA_TLE_CACHE_DIR"
# Cached files older than this are not loaded: stale elements propagate to wrong positions
# and this catalog feeds conjunction screening.
TLE_CACHE_MAX_AGE_HOURS = 72.0
# CelesTrak asks for no more than one download of a group every two hours. The loop runs
# five minutes past that, so the pass after a startup fetch never finds the fetch too
# recent and skips it.
TLE_REFRESH_INTERVAL_S = 7500
# How long a group is left alone after CelesTrak answered for it, with element sets or
# without (a 403 inside its two-hour window, an error page, an empty body). Enough refused
# repeats get an address blocked, so a restart must not send them. Kept under the refresh
# interval so the running loop never finds its own last download too recent.
CELESTRAK_REPEAT_WINDOW_S = 7200.0

# Under uvicorn only the uvicorn loggers have handlers, so INFO lines from a module-named
# logger would never reach the console; uvicorn.error is configured at INFO.
logger = logging.getLogger("uvicorn.error")

# NOAA Space Weather - live F10.7 solar flux
NOAA_F107_URL = "https://services.swpc.noaa.gov/json/solar-cycle/predicted-solar-cycle.json"
NOAA_F107_OBSERVED_URL = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"

# Live data state
_live_f107: float = 150.0  # updated from NOAA
_last_tle_refresh: datetime | None = None
_tle_refresh_task: asyncio.Task | None = None

# What this process remembers about its own CelesTrak requests. The cache directory
# holds the same facts across a restart; without SDA_TLE_CACHE_DIR this is the only
# record, and it is what keeps a second refresh, or the globe asking for one group,
# inside CelesTrak's rule.
_celestrak_lock = threading.Lock()
_celestrak_in_flight: set[str] = set()  # groups with a request on the wire
_celestrak_answered_at: dict[str, float] = {}  # group -> time.time() of the last reply
_celestrak_text: dict[str, tuple[float, str]] = {}  # group -> (time taken, last good text)

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


# --- TLE disk cache ---

def _tle_cache_dir() -> Path | None:
    """Cache directory from SDA_TLE_CACHE_DIR, or None when the cache is off."""
    raw = os.environ.get(TLE_CACHE_ENV, "").strip()
    return Path(raw) if raw else None


def _write_tle_cache(group: str, text: str) -> None:
    """Save a group's raw response as <dir>/<group>.tle. A failed write is logged, never raised."""
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return
    # Temp file in the same directory so os.replace is atomic. The .tmp suffix keeps a
    # half-written file out of the *.tle glob that startup uses.
    tmp_path = cache_dir / f"{group}.{uuid.uuid4().hex}.tmp"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(text, encoding="utf-8", newline="")
        os.replace(tmp_path, cache_dir / f"{group}.tle")
    except Exception as exc:
        logger.warning("TLE cache: could not write %s.tle: %s", group, exc)
        with suppress(OSError):
            tmp_path.unlink()


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _load_newer_only(raw: str) -> tuple[int, int]:
    """Load TLE text through TLEStore.load_from_text without letting older elements win.

    TLEStore.upsert is last-write-wins per NORAD id whatever the epoch, so a plain load of
    older text would replace a newer element set. The text is parsed and validated into a
    scratch store, then only records newer than the live store's are applied.
    Returns (records parsed, records applied).
    """
    scratch = TLEStore()
    scratch.load_from_text(raw)
    applied = 0
    for record in scratch.get_all():
        current = store.get(record.norad_id)
        if current is None or _utc(record.epoch) > _utc(current.epoch):
            store.upsert(record)
            applied += 1
    return scratch.count(), applied


def _load_tle_cache() -> tuple[int, int]:
    """Load every recent *.tle file from the cache dir. Returns (files, objects) applied.

    Runs after the bundled demo catalog and before the network fetch. A file that is too
    old, unreadable or corrupt is skipped and never stops startup.
    """
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return 0, 0
    max_age_s = TLE_CACHE_MAX_AGE_HOURS * 3600.0
    now = time.time()
    ages_h: list[float] = []
    objects = 0
    try:
        paths = sorted(cache_dir.glob("*.tle"))
    except Exception as exc:
        logger.warning("TLE cache: could not list %s: %s", cache_dir, exc)
        return 0, 0
    for path in paths:
        try:
            age_s = now - path.stat().st_mtime
            if age_s > max_age_s:
                logger.info(
                    "TLE cache: skipping %s, %.1f h old (limit %.0f h)",
                    path.name, age_s / 3600.0, TLE_CACHE_MAX_AGE_HOURS,
                )
                continue
            parsed, applied = _load_newer_only(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except Exception as exc:
            logger.warning("TLE cache: could not load %s: %s", path.name, exc)
            continue
        if parsed == 0:
            logger.warning("TLE cache: %s holds no valid TLE records, skipped", path.name)
            continue
        ages_h.append(age_s / 3600.0)
        objects += applied
    if ages_h:
        logger.info(
            "TLE cache: applied %d objects from %d file%s in %s (%.1f to %.1f h old)",
            objects, len(ages_h), "" if len(ages_h) == 1 else "s", cache_dir,
            min(ages_h), max(ages_h),
        )
    return len(ages_h), objects


def _asked_age_s(group: str) -> float | None:
    """Seconds since CelesTrak last answered for this group, None when unknown.

    The newest of what this process saw and what the cache directory records. The
    directory is what survives a restart: <group>.tle is the last download that worked,
    <group>.refused the last reply that carried no element sets.
    """
    stamps: list[float] = []
    answered_at = _celestrak_answered_at.get(group)
    if answered_at is not None:
        stamps.append(answered_at)
    cache_dir = _tle_cache_dir()
    if cache_dir is not None:
        for suffix in (".tle", ".refused"):
            with suppress(OSError):
                stamps.append((cache_dir / f"{group}{suffix}").stat().st_mtime)
    return time.time() - max(stamps) if stamps else None


def _asked_recently(group: str) -> bool:
    age_s = _asked_age_s(group)
    return age_s is not None and 0.0 <= age_s < CELESTRAK_REPEAT_WINDOW_S


def _resting(group: str) -> bool:
    """Inside the repeat window, or being asked for right now."""
    return group in _celestrak_in_flight or _asked_recently(group)


def _claim_group(group: str) -> bool:
    """True when the caller may ask CelesTrak for this group now.

    Two refreshes that overlap (the startup fetch and a POST /tle/refresh, say) would
    otherwise both ask before either had an answer to remember. The caller that gets
    True takes the group out of _celestrak_in_flight when its request ends.
    """
    with _celestrak_lock:
        if _resting(group):
            return False
        _celestrak_in_flight.add(group)
        return True


def _forget_celestrak() -> None:
    """Drop what this process remembers about its CelesTrak requests.

    What a restart does. The tests call it between cases so one case's download does
    not rest the next one's groups.
    """
    with _celestrak_lock:
        _celestrak_in_flight.clear()
        _celestrak_answered_at.clear()
        _celestrak_text.clear()


def last_group_download(group: str) -> tuple[float, str] | None:
    """(time taken, raw text) of the last good download of a group, None when there is none.

    From this process when it made one, else from the cache directory if that copy is
    young enough to be loaded at startup. GET /api/tles?group= serves from here, so the
    globe never has to ask CelesTrak for a group the refresh already holds.
    """
    held = _celestrak_text.get(group)
    if held is not None:
        return held
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return None
    path = cache_dir / f"{group}.tle"
    try:
        taken_at = path.stat().st_mtime
        if time.time() - taken_at > TLE_CACHE_MAX_AGE_HOURS * 3600.0:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _celestrak_text[group] = (taken_at, text)
    return taken_at, text


def _last_download_at() -> datetime | None:
    """When the newest good download this process is running on was taken."""
    stamps = [taken_at for taken_at, _ in _celestrak_text.values()]
    cache_dir = _tle_cache_dir()
    if cache_dir is not None:
        oldest_loaded = time.time() - TLE_CACHE_MAX_AGE_HOURS * 3600.0
        for group in CELESTRAK_GROUPS:
            with suppress(OSError):
                taken_at = (cache_dir / f"{group}.tle").stat().st_mtime
                if taken_at >= oldest_loaded:
                    stamps.append(taken_at)
    return datetime.fromtimestamp(max(stamps), UTC) if stamps else None


def _mark_refused(group: str) -> None:
    cache_dir = _tle_cache_dir()
    if cache_dir is None:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{group}.refused").touch()
    except OSError as exc:
        logger.warning("TLE cache: could not record the refusal of %s: %s", group, exc)


def _first_refresh_delay_s() -> float:
    """Seconds until the first background refresh.

    Normally the full interval. After a restart that skipped groups still inside
    CelesTrak's window, it is the time until the last of them may be asked for again.
    """
    waits = [
        CELESTRAK_REPEAT_WINDOW_S - age_s
        for age_s in (_asked_age_s(group) for group in CELESTRAK_GROUPS)
        if age_s is not None and 0.0 <= age_s < CELESTRAK_REPEAT_WINDOW_S
    ]
    return max(waits) + 60.0 if waits else TLE_REFRESH_INTERVAL_S


# --- Background tasks ---

async def _fetch_celestrak_group(
    client: httpx.AsyncClient, group: str, into: TLEStore | None = None
) -> int:
    """Fetch a single CelesTrak group. Returns count ingested.

    The elements go into the process catalog, or into *into* when the caller wants the
    group on its own (the globe's single-group view). It is the same request either
    way, under the same repeat window.
    """
    if not _claim_group(group):
        return 0
    try:
        resp = await client.get(CELESTRAK_BASE.format(group=group))
    except Exception as exc:
        # No reply reached us, so nothing was counted against this address and the
        # next call may try again.
        logger.info("CelesTrak: %s did not answer (%s)", group, type(exc).__name__)
        return 0
    finally:
        _celestrak_in_flight.discard(group)
    answered_at = time.time()
    _celestrak_answered_at[group] = answered_at

    count = 0
    if resp.status_code == 200 and resp.text.strip():
        try:
            count = (store if into is None else into).load_from_text(resp.text)
        except Exception:
            count = 0
    if count > 0:
        _celestrak_text[group] = (answered_at, resp.text)
        _write_tle_cache(group, resp.text)
        return count

    # CelesTrak answered and there was nothing to load: a 403 inside its window, an
    # error page, an empty or unreadable body. Asking again before the window ends
    # gets the same answer, and a restart used to do exactly that for every reply
    # except the 403.
    _mark_refused(group)
    logger.info(
        "CelesTrak: %s gave no element sets (HTTP %d, %d characters)",
        group, resp.status_code, len(resp.text),
    )
    return 0


async def _seed_catalog() -> int:
    """Seed catalog from live CelesTrak data. Falls back to bundled TLEs if all groups fail.

    Returns how many groups CelesTrak was asked for, which is zero when every group is
    still inside the repeat window.
    """
    global _last_tle_refresh
    live_count = 0
    served = 0
    resting = [group for group in CELESTRAK_GROUPS if _resting(group)]
    if resting:
        logger.info(
            "CelesTrak: %d of %d groups were asked for in the last %.0f minutes, not asked again",
            len(resting), len(CELESTRAK_GROUPS), CELESTRAK_REPEAT_WINDOW_S / 60.0,
        )
    asked = len(CELESTRAK_GROUPS) - len(resting)
    if asked:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                import asyncio as _aio
                tasks = [_fetch_celestrak_group(client, g) for g in CELESTRAK_GROUPS]
                results = await _aio.gather(*tasks, return_exceptions=True)
                live_count = sum(r for r in results if isinstance(r, int))
                served = sum(1 for r in results if isinstance(r, int) and r > 0)
        except Exception:
            pass
        logger.info(
            "CelesTrak: %d objects from %d of %d groups asked", live_count, served, asked
        )

    if live_count == 0:
        # Fills in what is missing and nothing else. A plain load is last-write-wins,
        # and the bundled elements are older than anything a refresh or the cache put
        # in the catalog.
        _load_newer_only(DEMO_TLE_TEXT)

    if served:
        _last_tle_refresh = datetime.now(UTC)
    elif _last_tle_refresh is None:
        # Nothing new arrived: a restart inside the window asked nobody, or nobody
        # answered with elements. The catalog is as new as the last download it was
        # loaded from, and that is the time to report.
        _last_tle_refresh = _last_download_at()
    return asked


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
    """Background task: refresh TLEs every TLE_REFRESH_INTERVAL_S, F10.7 every third pass."""
    f107_counter = 0
    delay_s = _first_refresh_delay_s()
    while True:
        await asyncio.sleep(delay_s)
        delay_s = TLE_REFRESH_INTERVAL_S
        await _seed_catalog()
        f107_counter += 1
        if f107_counter % 3 == 0:
            await _fetch_live_f107()


# --- App lifecycle ---

async def _initial_live_fetch() -> None:
    """Fetch live CelesTrak + F10.7 data (runs in background after instant startup)."""
    await _seed_catalog()
    await _fetch_live_f107()


def _offline() -> bool:
    """True when SDA_OFFLINE asks for a start with no background fetch.

    The app then runs on the bundled catalog (and the disk cache, when one is
    configured) and asks CelesTrak and NOAA for nothing until told to. The static
    demo build bakes under it, so every build bakes the same catalog.
    """
    return os.environ.get("SDA_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tle_refresh_task
    _metrics["startup_time"] = datetime.now(UTC).isoformat()
    # Instant startup: load bundled demo TLEs synchronously
    store.load_from_text(DEMO_TLE_TEXT)
    # Then the last good catalog from disk (no-op unless SDA_TLE_CACHE_DIR is set)
    _load_tle_cache()
    background: list[asyncio.Task] = []
    _tle_refresh_task = None
    if not _offline():
        # Fetch live data in background - app is already serving
        _tle_refresh_task = asyncio.create_task(_initial_live_fetch())
        background = [_tle_refresh_task, asyncio.create_task(_background_refresh_loop())]
    yield
    for task in background:
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
        "- **Two-Phase Conjunction Detection**: Coarse (60s) -> Fine (1s) refinement\n"
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

# --- Network posture ---
#
# Middleware added later wraps middleware added earlier, so the order below puts the rate
# limiter innermost: a 429 it returns still passes back out through the security headers
# and the CORS layer, so browsers and operators see a well formed response.
#
# Per-IP token bucket over /api/ and /agent/ (configure with SDA_RATE_LIMIT).
app.add_middleware(RateLimitMiddleware)

# X-Content-Type-Options, Referrer-Policy, X-Frame-Options. Deliberately no
# Content-Security-Policy: the worldview page loads CesiumJS and the visualization page loads
# plotly.js from public CDNs and both use inline scripts and styles, so any CSP strict enough
# to be worth setting would break the globe. Revisit if those assets are ever vendored.
app.add_middleware(SecurityHeadersMiddleware)

# CORS allowlist from SDA_CORS_ORIGINS (comma separated). Defaults to the localhost and
# 127.0.0.1 origins for the bound port. Credentials are off: the API has no cookie session,
# so no browser origin needs them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
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

# --- feature routers registered by the integrator ---
app.include_router(ops_router)
app.include_router(passes_router)
app.include_router(agent_router)
app.include_router(history_router)
app.include_router(pc_router)


def run() -> None:
    """Start the server. Entry point for `sda-server` and `python -m sda.api`.

    Binds SDA_HOST (default 127.0.0.1, loopback only) and SDA_PORT (default 8000). The
    container images set SDA_HOST=0.0.0.0 so published ports keep working.
    """
    import uvicorn

    uvicorn.run("sda.api:app", host=bind_host(), port=bind_port())


if __name__ == "__main__":
    run()
