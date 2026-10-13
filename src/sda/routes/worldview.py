"""WorldView globe, WebSocket live tracking, and proxy routes.

Static assets for the globe (the SGP4 web worker, the PWA manifest and the
service worker) live in ``src/sda/templates/static`` and are served by
``GET /static/{asset_path}`` below. The static demo build copies that tree
next to its ``index.html`` so the same relative paths resolve offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import numpy as np
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.dashboard import DASHBOARD_HTML
from sda.models import ConjunctionRequest, TLERecord
from sda.propagator import build_satrec, datetime_to_jd, propagate_at
from sda.screening import GEO_BAND_HALF_WIDTH_KM
from sda.tle_store import TLEStore
from sda.worldview import WORLDVIEW_HTML

router = APIRouter()


# ---------------------------------------------------------------------------
# Orbit regime classification
# ---------------------------------------------------------------------------

_GEO_ALTITUDE_KM = 35786.0

REGIME_COLORS: dict[str, str] = {
    "LEO": "#00d4ff",
    "MEO": "#00ff88",
    "GEO": "#ffd700",
    "HEO": "#ff4466",
}


def _classify_regime(alt_km: float, eccentricity: float) -> str:
    """Classify orbit regime from altitude and TLE eccentricity.

    Rules
    -----
    HEO  eccentricity > 0.25, or above the GEO band
    GEO  within 300 km of 35786 km on either side (the band sda.screening uses)
    MEO  from 2000 km up to the GEO band
    LEO  below 2000 km

    The band used to be 35786 to 35800 km, which drew every geostationary
    satellite sitting a few km under the nominal altitude as MEO.
    """
    if eccentricity > 0.25 or alt_km > _GEO_ALTITUDE_KM + GEO_BAND_HALF_WIDTH_KM:
        return "HEO"
    if alt_km >= _GEO_ALTITUDE_KM - GEO_BAND_HALF_WIDTH_KM:
        return "GEO"
    if alt_km >= 2000.0:
        return "MEO"
    return "LEO"


# --- Dashboard ---

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


# --- WorldView Dashboard ---

@router.get("/worldview", response_class=HTMLResponse, include_in_schema=False)
def worldview():
    return HTMLResponse(content=WORLDVIEW_HTML)


# --- Static assets for the globe (worker, PWA manifest, service worker) ---

STATIC_DIR: Path = Path(__file__).resolve().parent.parent / "templates" / "static"

#: Extension to Content-Type. Anything not listed here is refused rather than
#: served as an unknown type, which keeps the route from becoming a generic
#: file server for whatever ends up in the directory.
STATIC_CONTENT_TYPES: dict[str, str] = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/vnd.microsoft.icon",
    ".txt": "text/plain; charset=utf-8",
}


def resolve_static_asset(asset_path: str) -> Path:
    """Resolve *asset_path* inside STATIC_DIR, or raise HTTPException.

    Guards against directory traversal: the request path may not be absolute,
    may not contain a ``..`` segment, and the fully resolved candidate must
    still sit under the resolved static root. Symlinks are resolved before the
    containment check, so a link pointing out of the tree is refused too.
    """
    cleaned = asset_path.replace("\\", "/").strip("/")
    if not cleaned:
        raise HTTPException(status_code=404, detail="Not found")

    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=404, detail="Not found")

    root = STATIC_DIR.resolve()
    candidate = (root / "/".join(parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=404, detail="Not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if candidate.suffix.lower() not in STATIC_CONTENT_TYPES:
        raise HTTPException(status_code=404, detail="Not found")
    return candidate


@router.get("/static/{asset_path:path}", include_in_schema=False)
def worldview_static(asset_path: str) -> FileResponse:
    """Serve one bundled WorldView asset by relative path."""
    target = resolve_static_asset(asset_path)
    return FileResponse(
        target,
        media_type=STATIC_CONTENT_TYPES[target.suffix.lower()],
        headers={
            # The service worker must be allowed to control the whole origin,
            # not just /static/.
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


# --- Coordinate conversion ---

def _teme_to_geographic(x_km: float, y_km: float, z_km: float, jd: float, fr: float):
    """Convert TEME position to geodetic lat/lon/alt for display.

    Uses GMST rotation (Vallado formulation) to transform from the
    True Equator Mean Equinox frame to a geographic reference, then
    Bowring's iterative method for geodetic latitude on WGS84 ellipsoid.
    """
    # Greenwich Mean Sidereal Time (radians)
    t_ut1 = ((jd - 2451545.0) + fr) / 36525.0
    gmst_sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t_ut1
        + 0.093104 * t_ut1 ** 2
        - 6.2e-6 * t_ut1 ** 3
    )
    gmst_rad = math.fmod(gmst_sec * (2.0 * math.pi / 86400.0), 2.0 * math.pi)

    # Rotate to geographic frame
    cos_g = math.cos(gmst_rad)
    sin_g = math.sin(gmst_rad)
    x_geo = x_km * cos_g + y_km * sin_g
    y_geo = -x_km * sin_g + y_km * cos_g
    z_geo = z_km

    # Geodetic coordinates via Bowring's iterative method (WGS84 ellipsoid)
    a_wgs = 6378.137  # semi-major axis, km
    f = 1.0 / 298.257223563  # flattening
    e2 = 2 * f - f * f  # first eccentricity squared

    lon_deg = math.degrees(math.atan2(y_geo, x_geo))

    p = math.sqrt(x_geo ** 2 + y_geo ** 2)
    lat_rad = math.atan2(z_geo, p * (1.0 - e2))

    for _ in range(5):
        sin_lat = math.sin(lat_rad)
        N = a_wgs / math.sqrt(1.0 - e2 * sin_lat ** 2)
        lat_rad = math.atan2(z_geo + e2 * N * sin_lat, p)

    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    N = a_wgs / math.sqrt(1.0 - e2 * sin_lat ** 2)

    alt_km = p / cos_lat - N if abs(cos_lat) > 1e-10 else abs(z_geo) - N * (1.0 - e2)

    lat_deg = math.degrees(lat_rad)

    return lat_deg, lon_deg, alt_km


def _compute_geographic_positions(
    store,
    epoch_dt: datetime | None = None,
    norad_ids: set[int] | None = None,
):
    """Compute geographic positions for all tracked satellites.

    Parameters
    ----------
    store
        TLEStore singleton.
    epoch_dt
        UTC datetime to propagate to.  Defaults to the current time when None.
    norad_ids
        Restrict the result to these NORAD ids.  None means every object in
        the store.  The WebSocket uses this so a client watching a handful of
        tracked objects does not pay for the whole catalog every tick.

    Returns
    -------
    (satellites, timestamp)
        *satellites* is a list of dicts each including regime and
        regime_color fields.  *timestamp* is the UTC propagation epoch.
    """
    now = epoch_dt if epoch_dt is not None else datetime.now(UTC)
    jd, fr = datetime_to_jd(now)
    satellites = []

    for tle in store.get_all():
        if norad_ids is not None and tle.norad_id not in norad_ids:
            continue
        try:
            satrec = build_satrec(tle)
            sv = propagate_at(satrec, now)
            lat, lon, alt = _teme_to_geographic(
                sv.position_km[0], sv.position_km[1], sv.position_km[2], jd, fr
            )
            vel = math.sqrt(sum(v ** 2 for v in sv.velocity_km_s))
            eccentricity = satrec.ecco
            regime = _classify_regime(alt, eccentricity)
            satellites.append({
                "norad_id": tle.norad_id,
                "name": tle.name,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "alt_km": round(alt, 1),
                "velocity_km_s": round(vel, 3),
                "regime": regime,
                "regime_color": REGIME_COLORS[regime],
            })
        except RuntimeError:
            continue

    return satellites, now


# --- WebSocket Live Tracking ---

#: Cap on how many NORAD ids one WebSocket client may subscribe to. The globe
#: only ever watches the selected object plus its tracked roster, so this is
#: generous while still bounding the per-tick work a single client can ask for.
WS_MAX_SUBSCRIPTION = 200


def parse_ws_subscription(raw: object) -> set[int] | None:
    """Turn a client ``norad_ids`` message field into a filter set.

    Returns None for "no filter" (null, absent, not a list, or nothing usable in
    it), so a malformed message degrades to the full-catalog stream rather than
    silently muting it.
    """
    if not isinstance(raw, list):
        return None
    ids: set[int] = set()
    for item in raw[:WS_MAX_SUBSCRIPTION]:
        try:
            ids.add(int(item))
        except (TypeError, ValueError):
            continue
    return ids or None


@router.websocket("/ws/positions")
async def websocket_positions(websocket: WebSocket):
    """Stream server-propagated satellite positions to the globe.

    The client may send ``{"interval": seconds}`` to retune the cadence and
    ``{"norad_ids": [...]}`` to follow a subset of the catalog (null or an empty
    list restores the full catalog). Positions are geodetic lat/lon/alt derived
    from the server's own SGP4 run, which is what makes this stream a
    cross-check on the browser's propagation rather than a duplicate of it.
    """
    from sda.api import store

    await websocket.accept()
    interval = 2.0
    norad_ids: set[int] | None = None

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=0.01)
            except WebSocketDisconnect:
                break
            except Exception:
                data = None
            if isinstance(data, dict):
                if "interval" in data:
                    with contextlib.suppress(TypeError, ValueError):
                        interval = max(1.0, min(30.0, float(data["interval"])))
                if "norad_ids" in data:
                    norad_ids = parse_ws_subscription(data["norad_ids"])

            satellites, now = _compute_geographic_positions(store, norad_ids=norad_ids)

            await websocket.send_json({
                "satellites": satellites,
                "count": len(satellites),
                "timestamp": now.isoformat(),
                "source": "server-sgp4",
                "interval_s": interval,
                "subscribed": sorted(norad_ids) if norad_ids else None,
            })

            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass


# --- WorldView Data APIs ---

@router.get(
    "/api/satellite-positions",
    tags=["WorldView"],
    summary="All satellite positions as lat/lon/alt",
)
def satellite_positions(
    epoch: str | None = Query(
        default=None,
        description=(
            "ISO 8601 epoch for propagation (e.g. 2026-03-22T12:00:00Z). "
            "Defaults to the current UTC time."
        ),
    ),
):
    """Return geographic positions of all tracked satellites for globe display.

    Pass an optional epoch query parameter (ISO 8601) to propagate to a
    specific time instead of now. This enables the time-slider feature in the
    WorldView frontend.
    """
    from sda.api import store

    epoch_dt: datetime | None = None
    if epoch is not None:
        try:
            epoch_dt = datetime.fromisoformat(epoch)
            if epoch_dt.tzinfo is None:
                epoch_dt = epoch_dt.replace(tzinfo=UTC)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid epoch format {epoch!r}. "
                    "Expected ISO 8601, e.g. 2026-03-22T12:00:00Z"
                ),
            ) from exc

    satellites, now = _compute_geographic_positions(store, epoch_dt)

    return {
        "satellites": satellites,
        "count": len(satellites),
        "timestamp": now.isoformat(),
    }


def _catalog_entry(tle) -> dict | None:
    """Build one /api/tles row, or None when the TLE will not build a satrec."""
    try:
        satrec = build_satrec(tle)
    except (RuntimeError, ValueError):
        return None
    n_rad_s = satrec.no_kozai / 60.0
    if n_rad_s <= 0:
        return None
    a_km = (EARTH_MU_KM3_S2 / (n_rad_s ** 2)) ** (1.0 / 3.0)
    regime = _classify_regime(a_km - EARTH_RADIUS_KM, satrec.ecco)
    return {
        "norad_id": tle.norad_id,
        "name": tle.name,
        "line1": tle.line1,
        "line2": tle.line2,
        "regime": regime,
        "regime_color": REGIME_COLORS[regime],
    }


def _catalog_from_store(store) -> list[dict]:
    """Map every usable TLE in *store* to a /api/tles row."""
    rows = []
    for tle in store.get_all():
        entry = _catalog_entry(tle)
        if entry is not None:
            rows.append(entry)
    return rows


#: How long past the refresh interval the globe waits before it asks CelesTrak for
#: a group itself. A group may be asked for again from minute 120 and the
#: background refresh comes round at minute 125; if the globe took the group in
#: between, the refresh would find it resting and the screening catalog would go a
#: cycle without new elements. So the globe asks only when the refresh is overdue.
GROUP_SELF_FETCH_GRACE_S = 600.0

#: group name -> (time the download was taken, catalog rows built from it). Rows
#: for "active" are ~11k satrecs, so they are built once per download.
_GROUP_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _allowed_groups() -> tuple[str, ...]:
    """CelesTrak groups the globe may request (mirrors the background seeder)."""
    from sda.api import CELESTRAK_GROUPS

    return tuple(CELESTRAK_GROUPS)


async def _fetch_group_catalog(group: str) -> list[dict]:
    """One CelesTrak group as /api/tles rows, from the last good download of it.

    The background refresh downloads every group the globe may ask for, so this
    normally sends nothing: it serves the text the refresh last got, from memory
    or from the cache directory after a restart. It used to download the group
    again itself, behind a 15-minute cache that a restart emptied, and a second
    download inside CelesTrak's two-hour window is refused and counted against
    the address. It asks only when no reply for the group is on record, or the
    refresh is overdue, and then through the same request and repeat window as
    the refresh.

    The text is parsed through a scratch :class:`TLEStore` so ingestion still goes
    through ``load_from_text`` (and its ``twoline2rv`` validation) and a request
    from the page never changes the process-wide catalog the conjunction engine
    screens.
    """
    import sda.api as api

    fetched: TLEStore | None = None
    age_s = api._asked_age_s(group)
    if age_s is None or age_s >= api.TLE_REFRESH_INTERVAL_S + GROUP_SELF_FETCH_GRACE_S:
        scratch = TLEStore()
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            if await api._fetch_celestrak_group(client, group, into=scratch) > 0:
                fetched = scratch

    held = api.last_group_download(group)
    if held is None:
        return []
    taken_at, text = held
    cached = _GROUP_CACHE.get(group)
    if cached is not None and cached[0] == taken_at:
        return cached[1]
    if fetched is None:
        fetched = TLEStore()
        fetched.load_from_text(text)
    rows = _catalog_from_store(fetched)
    if rows:
        _GROUP_CACHE[group] = (taken_at, rows)
    return rows


@router.get(
    "/api/tles",
    tags=["WorldView"],
    summary="Raw TLE catalog for client-side propagation",
)
async def tle_catalog(
    group: str | None = Query(
        default=None,
        description=(
            "Optional CelesTrak group to serve instead of the process catalog, "
            "for example 'active' for the full active list. Served from the last "
            "download the background refresh made; falls back to the process "
            "catalog when there is none."
        ),
    ),
):
    """Return raw TLE lines plus orbit regime for client-side propagation.

    The WorldView frontend propagates these in a web worker with satellite.js
    (WGS72 SGP4, matching the server) so satellite motion animates smoothly
    without polling the server for positions. Regime is classified from mean
    elements: semi-major-axis altitude derived from the Kozai mean motion, plus
    TLE eccentricity.
    """
    from sda.api import store

    if group:
        requested = group.strip().lower()
        allowed = _allowed_groups()
        if requested not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown group '{requested}'. Allowed: {', '.join(allowed)}",
            )
        try:
            rows = await _fetch_group_catalog(requested)
        except Exception:
            rows = []
        if rows:
            return {
                "satellites": rows,
                "count": len(rows),
                "group": requested,
                "source": "celestrak",
            }
        rows = _catalog_from_store(store)
        return {
            "satellites": rows,
            "count": len(rows),
            "group": requested,
            "source": "fallback-catalog",
        }

    rows = _catalog_from_store(store)
    return {"satellites": rows, "count": len(rows), "group": None, "source": "catalog"}


@router.get(
    "/api/satellite-orbits",
    tags=["WorldView"],
    summary="Orbit paths for all tracked satellites",
)
def satellite_orbits():
    """Return one-orbit path for each tracked satellite as lat/lon/alt arrays."""
    from sda.api import store

    now = datetime.now(UTC)
    orbits = []

    for tle in store.get_all():
        try:
            satrec = build_satrec(tle)
            n_rad_min = satrec.no_kozai
            period_min = (2 * math.pi / n_rad_min) if n_rad_min > 0 else 90.0
            period_min = min(period_min, 200.0)
            max_points = 100  # cap to prevent browser OOM on large catalogs
            step_sec = max(20.0, period_min * 60.0 / max_points)

            path = []
            n_steps = min(int(period_min * 60.0 / step_sec) + 1, max_points)
            for i in range(n_steps):
                dt = now + timedelta(seconds=i * step_sec)
                jd, fr = datetime_to_jd(dt)
                try:
                    sv = propagate_at(satrec, dt)
                    lat, lon, alt = _teme_to_geographic(
                        sv.position_km[0], sv.position_km[1], sv.position_km[2], jd, fr
                    )
                    path.append(
                        {"lat": round(lat, 3), "lon": round(lon, 3), "alt_km": round(alt, 1)}
                    )
                except RuntimeError:
                    continue

            orbits.append({
                "norad_id": tle.norad_id,
                "name": tle.name,
                "period_min": round(period_min, 1),
                "path": path,
            })
        except (RuntimeError, ValueError):
            continue

    return {"orbits": orbits, "count": len(orbits)}


#: OpenSky's OAuth2 client-credentials endpoint. Authenticated clients get a
#: far higher request allowance than the anonymous feed; the credentials are
#: read from the environment in this process and never reach the browser.
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"

#: (token, monotonic expiry). Refreshed a minute before the stated expiry.
_OPENSKY_TOKEN: tuple[str, float] | None = None
_OPENSKY_TOKEN_SKEW_S = 60.0


async def _opensky_token() -> str | None:
    """Return a cached bearer token, or None when unauthenticated.

    Missing credentials, a refused grant and a network failure all return None,
    which drops the caller back to the anonymous feed instead of failing the
    layer.
    """
    global _OPENSKY_TOKEN

    client_id = (os.environ.get("OPENSKY_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("OPENSKY_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        return None

    now = time.monotonic()
    if _OPENSKY_TOKEN is not None and now < _OPENSKY_TOKEN[1]:
        return _OPENSKY_TOKEN[0]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                OPENSKY_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
    except Exception:
        return None

    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        return None
    lifetime = float(payload.get("expires_in") or 1800.0)
    _OPENSKY_TOKEN = (str(token), now + max(60.0, lifetime - _OPENSKY_TOKEN_SKEW_S))
    return _OPENSKY_TOKEN[0]


def reset_opensky_token() -> None:
    """Forget the cached OpenSky token. Used by the tests."""
    global _OPENSKY_TOKEN
    _OPENSKY_TOKEN = None


@router.get("/api/flights", tags=["WorldView"], summary="Live flight data from OpenSky Network")
async def proxy_flights():
    """Proxy live aircraft positions from OpenSky Network for globe display.

    Authenticated when OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET are set,
    anonymous otherwise. The anonymous path stays the default so a fresh
    checkout still shows aircraft.
    """
    token = await _opensky_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(OPENSKY_STATES_URL)
            if resp.status_code != 200:
                return {
                    "flights": [],
                    "count": 0,
                    "authenticated": bool(token),
                    "error": "OpenSky unavailable",
                }
            data = resp.json()
    except Exception:
        return {
            "flights": [],
            "count": 0,
            "authenticated": bool(token),
            "error": "OpenSky request failed",
        }

    flights = []
    states = data.get("states") or []
    for s in states[:2000]:
        if s[5] is None or s[6] is None:
            continue
        flights.append({
            "icao24": s[0],
            "callsign": (s[1] or "").strip(),
            "origin_country": s[2] or "",
            "lon": s[5],
            "lat": s[6],
            "alt_m": s[7] or s[13] or 10000,
            "on_ground": s[8],
            "velocity": s[9],
            "heading": s[10],
            "is_military": False,
        })

    return {
        "flights": flights,
        "count": len(flights),
        "authenticated": bool(token),
        "timestamp": data.get("time"),
    }


@router.get("/api/earthquakes", tags=["WorldView"], summary="Recent seismic activity from USGS")
async def proxy_earthquakes():
    """Proxy recent earthquake data from USGS for globe display."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
            )
            if resp.status_code != 200:
                return {"earthquakes": [], "count": 0, "error": "USGS unavailable"}
            data = resp.json()
    except Exception:
        return {"earthquakes": [], "count": 0, "error": "USGS request failed"}

    earthquakes = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [0, 0, 0])
        earthquakes.append({
            "id": feature.get("id", ""),
            "magnitude": props.get("mag", 0),
            "place": props.get("place", ""),
            "time": datetime.fromtimestamp(
                props.get("time", 0) / 1000, tz=UTC
            ).strftime("%Y-%m-%d %H:%M UTC"),
            "title": props.get("title", ""),
            "lon": coords[0],
            "lat": coords[1],
            "depth_km": coords[2],
        })

    return {"earthquakes": earthquakes, "count": len(earthquakes)}


# ---------------------------------------------------------------------------
# WorldView proxy endpoints: config, military ADS-B, traffic, CCTV, geocode
# ---------------------------------------------------------------------------

_KNOTS_TO_M_S = 0.514444
_FEET_TO_M = 0.3048

#: Road classes requested from Overpass, most important first.
_TRAFFIC_HIGHWAY_CLASSES = "motorway|trunk|primary|secondary"
_TRAFFIC_MAX_WAYS = 300
_TRAFFIC_MAX_SPAN_DEG = 0.5

#: Public open-data CCTV catalogs (image snapshots, no credentials).
_CCTV_CITIES = ("austin", "nyc", "london")
_AUSTIN_CAMERAS_URL = (
    "https://data.austintexas.gov/resource/b4k4-adkb.json?$limit=250"
)
_AUSTIN_IMAGE_URL = "https://cctv.austinmobility.io/image/{camera_id}.jpg"
_NYC_CAMERAS_URL = "https://webcams.nyctmc.org/api/cameras"
_LONDON_CAMERAS_URL = "https://api.tfl.gov.uk/Place/Type/JamCam"


@router.get(
    "/api/config",
    tags=["WorldView"],
    summary="Optional map-provider tokens for the WorldView frontend",
)
def worldview_config():
    """Expose optional map tokens read from environment variables.

    CESIUM_ION_TOKEN enables Cesium World Terrain; GOOGLE_MAPS_API_KEY
    enables Google Photorealistic 3D Tiles. Both are optional; the globe
    works without them.
    """
    return {
        "cesium_ion_token": os.environ.get("CESIUM_ION_TOKEN") or None,
        "google_maps_api_key": os.environ.get("GOOGLE_MAPS_API_KEY") or None,
    }


@router.get(
    "/api/military-flights",
    tags=["WorldView"],
    summary="Live military aircraft from crowdsourced ADS-B (adsb.lol)",
)
async def proxy_military_flights():
    """Proxy military aircraft positions from the adsb.lol /v2/mil feed."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://api.adsb.lol/v2/mil")
            if resp.status_code != 200:
                return {"flights": [], "count": 0, "error": "adsb.lol unavailable"}
            data = resp.json()
    except Exception:
        return {"flights": [], "count": 0, "error": "adsb.lol request failed"}

    flights = []
    for ac in (data.get("ac") or [])[:500]:
        lat = ac.get("lat")
        lon = ac.get("lon")
        if lat is None or lon is None:
            continue
        alt_baro = ac.get("alt_baro")
        alt_m = (
            round(float(alt_baro) * _FEET_TO_M, 1)
            if isinstance(alt_baro, (int, float))
            else 0.0  # "ground" or missing
        )
        gs_kt = ac.get("gs")
        flights.append({
            "icao24": ac.get("hex", ""),
            "callsign": (ac.get("flight") or "").strip(),
            "lat": lat,
            "lon": lon,
            "alt_m": alt_m,
            "type": ac.get("t") or "MIL",
            "squawk": ac.get("squawk") or "",
            "velocity": (
                round(float(gs_kt) * _KNOTS_TO_M_S, 1)
                if isinstance(gs_kt, (int, float))
                else None
            ),
            "heading": ac.get("track"),
            "is_military": True,
        })

    return {"flights": flights, "count": len(flights)}


@router.get(
    "/api/traffic",
    tags=["WorldView"],
    summary="Road geometry in a bounding box from OpenStreetMap Overpass",
)
async def proxy_traffic(
    south: float = Query(..., ge=-90.0, le=90.0),
    west: float = Query(..., ge=-180.0, le=180.0),
    north: float = Query(..., ge=-90.0, le=90.0),
    east: float = Query(..., ge=-180.0, le=180.0),
):
    """Return major-road polylines inside the bbox for the traffic layer.

    The frontend spawns animated particles along these segments to emulate
    street traffic. Bbox spans are clamped server-side to keep Overpass
    queries cheap.
    """
    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="Empty bounding box")
    if (north - south) > _TRAFFIC_MAX_SPAN_DEG or (east - west) > _TRAFFIC_MAX_SPAN_DEG:
        raise HTTPException(
            status_code=422,
            detail=f"Bounding box span exceeds {_TRAFFIC_MAX_SPAN_DEG} degrees",
        )

    overpass_query = (
        f"[out:json][timeout:10];"
        f'way["highway"~"^({_TRAFFIC_HIGHWAY_CLASSES})$"]'
        f"({south},{west},{north},{east});"
        f"out geom {_TRAFFIC_MAX_WAYS};"
    )
    url = "https://overpass-api.de/api/interpreter?data=" + quote(overpass_query)
    # Overpass rejects generic library User-Agents with HTTP 406
    headers = {"User-Agent": "sda-collision-predictor/0.2 (worldview traffic layer)"}

    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"roads": [], "count": 0, "error": "Overpass unavailable"}
            data = resp.json()
    except Exception:
        return {"roads": [], "count": 0, "error": "Overpass request failed"}

    roads = []
    for element in data.get("elements", []):
        if element.get("type") != "way":
            continue
        geometry = element.get("geometry") or []
        if len(geometry) < 2:
            continue
        roads.append({
            "type": (element.get("tags") or {}).get("highway", "secondary"),
            "coords": [{"lat": g["lat"], "lon": g["lon"]} for g in geometry],
        })

    return {"roads": roads, "count": len(roads)}


def _parse_austin_cameras(rows: list) -> list[dict]:
    """Map Austin open-data camera rows to the WorldView camera schema.

    Handles both Socrata location shapes: a GeoJSON-style point in
    ``location`` and flat ``location_latitude``/``location_longitude``.
    """
    cameras = []
    for row in rows:
        camera_id = row.get("camera_id")
        if not camera_id:
            continue
        lat = lon = None
        location = row.get("location")
        if isinstance(location, dict):
            coords = location.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                lon, lat = float(coords[0]), float(coords[1])
            elif location.get("latitude") and location.get("longitude"):
                lat = float(location["latitude"])
                lon = float(location["longitude"])
        if lat is None and row.get("location_latitude"):
            lat = float(row["location_latitude"])
            lon = float(row["location_longitude"])
        if lat is None or lon is None:
            continue
        cameras.append({
            "id": str(camera_id),
            "name": row.get("location_name", "").strip() or f"CAM {camera_id}",
            "lat": lat,
            "lon": lon,
            "image_url": _AUSTIN_IMAGE_URL.format(camera_id=camera_id),
            "city": "austin",
        })
    return cameras


def _parse_nyc_cameras(rows: list) -> list[dict]:
    """Map NYC TMC camera rows to the WorldView camera schema."""
    cameras = []
    for row in rows:
        cam_id = row.get("id")
        lat = row.get("latitude")
        lon = row.get("longitude")
        if not cam_id or lat is None or lon is None:
            continue
        if str(row.get("isOnline", "true")).lower() == "false":
            continue
        cameras.append({
            "id": str(cam_id),
            "name": row.get("name", "").strip() or f"CAM {cam_id}",
            "lat": float(lat),
            "lon": float(lon),
            "image_url": row.get("imageUrl", ""),
            "city": "nyc",
        })
    return cameras


def _parse_london_cameras(rows: list) -> list[dict]:
    """Map TfL JamCam places to the WorldView camera schema."""
    cameras = []
    for row in rows:
        cam_id = row.get("id")
        lat = row.get("lat")
        lon = row.get("lon")
        if not cam_id or lat is None or lon is None:
            continue
        image_url = ""
        available = True
        for prop in row.get("additionalProperties") or []:
            key = prop.get("key")
            if key == "imageUrl":
                image_url = prop.get("value") or ""
            elif key == "available" and str(prop.get("value")).lower() == "false":
                available = False
        if not available or not image_url:
            continue
        cameras.append({
            "id": str(cam_id),
            "name": (row.get("commonName") or "").strip() or f"CAM {cam_id}",
            "lat": float(lat),
            "lon": float(lon),
            "image_url": image_url,
            "city": "london",
        })
    return cameras


_CCTV_SOURCES = {
    "austin": (_AUSTIN_CAMERAS_URL, _parse_austin_cameras),
    "nyc": (_NYC_CAMERAS_URL, _parse_nyc_cameras),
    "london": (_LONDON_CAMERAS_URL, _parse_london_cameras),
}


@router.get(
    "/api/cctv",
    tags=["WorldView"],
    summary="Public traffic-camera locations and snapshot URLs",
)
async def proxy_cctv(
    city: str = Query(default="austin", description="One of: austin, nyc, london"),
):
    """Proxy public open-data traffic cameras for the CCTV layer.

    Sources: Austin Transportation open data (snapshot images served by
    cctv.austinmobility.io), NYC TMC webcams, and TfL JamCams. All are
    public feeds published by the respective city governments.
    """
    city = city.lower().strip()
    if city not in _CCTV_SOURCES:
        return {
            "cameras": [],
            "count": 0,
            "error": f"Unknown city {city!r}; supported: {', '.join(_CCTV_CITIES)}",
        }

    url, parser = _CCTV_SOURCES[city]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"cameras": [], "count": 0, "error": "CCTV source unavailable"}
            data = resp.json()
    except Exception:
        return {"cameras": [], "count": 0, "error": "CCTV request failed"}

    rows = data if isinstance(data, list) else []
    cameras = parser(rows)

    return {"cameras": cameras, "count": len(cameras)}


@router.get(
    "/api/geocode",
    tags=["WorldView"],
    summary="Place search via OpenStreetMap Nominatim",
)
async def proxy_geocode(
    q: str = Query(..., min_length=2, max_length=200, description="Free-text place query"),
):
    """Proxy place-name search to Nominatim for the WorldView search box."""
    url = (
        "https://nominatim.openstreetmap.org/search"
        "?format=jsonv2&limit=8&q=" + quote(q)
    )
    headers = {"User-Agent": "sda-collision-predictor/0.2 (worldview geocoder)"}

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"results": [], "error": "Nominatim unavailable"}
            data = resp.json()
    except Exception:
        return {"results": [], "error": "Nominatim request failed"}

    results = []
    for row in data if isinstance(data, list) else []:
        try:
            results.append({
                "name": row.get("display_name", ""),
                "type": row.get("type") or row.get("category") or "place",
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Conjunction globe data
# ---------------------------------------------------------------------------

#: Risk-level colour palette for CesiumJS rendering.
_RISK_COLORS: dict[str, str] = {
    "CRITICAL":   "#ff0000",
    "HIGH":       "#ff6600",
    "MODERATE":   "#ffcc00",
    "LOW":        "#66ff66",
    "NEGLIGIBLE": "#aaaaaa",
}


def _tle_age_hours(tle: TLERecord, at_dt: datetime) -> float:
    """Hours between a TLE epoch and *at_dt*, never negative.

    Used to size the position-uncertainty estimate drawn on the globe. A TLE
    whose epoch is after the requested time is treated as fresh (0 h) rather
    than as negatively aged.
    """
    epoch = tle.epoch
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)
    return max(0.0, (at_dt - epoch).total_seconds() / 3600.0)


@router.post(
    "/api/conjunction-globe-data",
    tags=["WorldView"],
    summary="Conjunction events formatted for CesiumJS globe rendering",
)
def conjunction_globe_data(request: ConjunctionRequest):
    """Run conjunction analysis, enriching each event with geographic
    positions, B-plane projection data and a position-uncertainty estimate
    for CesiumJS rendering.

    Response fields per conjunction: primary_position, secondary_position
    (lat/lon/alt_km at TCA), risk, risk_color, miss_distance_km,
    relative_velocity_km_s, tca (ISO 8601), bplane (miss_x_km, miss_y_km,
    sigma_combined_km), covariance, pc (collision probability).

    The ``covariance`` block is an isotropic 1-sigma position estimate per
    object derived from TLE epoch age (``sda.probability.sigma_from_tle_age``),
    not an operator-supplied covariance matrix. The globe labels the ellipsoids
    it draws from it as TLE-age estimates for exactly that reason.
    """
    from sda.api import store
    from sda.conjunction import find_conjunctions
    from sda.probability import _rotation_matrix_to_conjunction_plane, sigma_from_tle_age

    events = find_conjunctions(
        store,
        norad_ids=request.norad_ids,
        hours=request.hours,
        threshold_km=request.threshold_km,
        compute_probability=True,
    )

    results = []
    for event in events:
        tca_dt = event.tca
        if tca_dt.tzinfo is None:
            tca_dt = tca_dt.replace(tzinfo=UTC)

        jd, fr = datetime_to_jd(tca_dt)

        primary_tle = store.get(event.primary)
        secondary_tle = store.get(event.secondary)
        if primary_tle is None or secondary_tle is None:
            continue

        try:
            satrec_p = build_satrec(primary_tle)
            satrec_s = build_satrec(secondary_tle)
            sv_p = propagate_at(satrec_p, tca_dt)
            sv_s = propagate_at(satrec_s, tca_dt)
        except RuntimeError:
            continue

        lat_p, lon_p, alt_p = _teme_to_geographic(
            sv_p.position_km[0], sv_p.position_km[1], sv_p.position_km[2], jd, fr
        )
        lat_s, lon_s, alt_s = _teme_to_geographic(
            sv_s.position_km[0], sv_s.position_km[1], sv_s.position_km[2], jd, fr
        )

        miss_vec = np.array(sv_p.position_km) - np.array(sv_s.position_km)
        rel_vel_vec = np.array(sv_p.velocity_km_s) - np.array(sv_s.velocity_km_s)
        rel_speed = float(np.linalg.norm(rel_vel_vec))
        miss_x_km = 0.0
        miss_y_km = 0.0
        if rel_speed > 1e-10:
            R = _rotation_matrix_to_conjunction_plane(rel_vel_vec)
            miss_rotated = R @ miss_vec
            miss_x_km = float(miss_rotated[0])
            miss_y_km = float(miss_rotated[1])

        pc_model = event.collision_probability
        sigma_combined_km = pc_model.combined_sigma_km if pc_model else 0.0
        pc_value = pc_model.probability if pc_model else 0.0

        age_p_hours = _tle_age_hours(primary_tle, tca_dt)
        age_s_hours = _tle_age_hours(secondary_tle, tca_dt)
        sigma_p_km = sigma_from_tle_age(age_p_hours)
        sigma_s_km = sigma_from_tle_age(age_s_hours)
        if sigma_combined_km <= 0.0:
            sigma_combined_km = float(math.sqrt(sigma_p_km ** 2 + sigma_s_km ** 2))

        risk_str = event.risk.value
        results.append({
            "primary_norad_id": event.primary,
            "primary_name": event.primary_name,
            "secondary_norad_id": event.secondary,
            "secondary_name": event.secondary_name,
            "tca": tca_dt.isoformat(),
            "risk": risk_str,
            "risk_color": _RISK_COLORS.get(risk_str, "#aaaaaa"),
            "miss_distance_km": round(event.miss_distance_km, 4),
            "relative_velocity_km_s": round(event.relative_velocity_km_s, 4),
            "primary_position": {
                "lat": round(lat_p, 4),
                "lon": round(lon_p, 4),
                "alt_km": round(alt_p, 1),
            },
            "secondary_position": {
                "lat": round(lat_s, 4),
                "lon": round(lon_s, 4),
                "alt_km": round(alt_s, 1),
            },
            "bplane": {
                "miss_x_km": round(miss_x_km, 6),
                "miss_y_km": round(miss_y_km, 6),
                "sigma_combined_km": round(sigma_combined_km, 6),
            },
            "covariance": {
                "source": "tle-age-estimate",
                "model": "sda.probability.sigma_from_tle_age (Vallado 2013 ch. 9)",
                "isotropic": True,
                "primary_sigma_km": round(sigma_p_km, 6),
                "secondary_sigma_km": round(sigma_s_km, 6),
                "combined_sigma_km": round(sigma_combined_km, 6),
                "primary_tle_age_hours": round(age_p_hours, 3),
                "secondary_tle_age_hours": round(age_s_hours, 3),
            },
            "pc": pc_value,
        })

    return {"conjunctions": results, "count": len(results)}

# ---------------------------------------------------------------------------
# Pc history endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/api/conjunction-pc-history",
    tags=["WorldView"],
    summary="Pc evolution over lead times before TCA",
)
def conjunction_pc_history(
    primary_norad_id: int = Query(..., description="NORAD ID of the primary satellite"),
    secondary_norad_id: int = Query(
        ..., description="NORAD ID of the secondary satellite"
    ),
    tca: str = Query(..., description="Time of closest approach as ISO 8601 string"),
    n_points: int = Query(
        default=12,
        ge=2,
        le=48,
        description=(
            "Number of time samples evenly distributed over the 24 h window "
            "before TCA (includes TCA itself at 0 h lead time)"
        ),
    ),
):
    """Simulate Pc evolution by recomputing collision probability at multiple
    lead times before TCA. Returns data points for Pc trending plots.

    Samples are evenly spaced from 24 h before TCA through TCA itself (0 h).
    Each sample re-propagates both satellites and recomputes Pc, capturing
    the changing geometry over the approach window.

    Response items: hours_before_tca, pc, miss_distance_km,
    relative_velocity_km_s, timestamp.
    """
    from sda.api import store
    from sda.probability import compute_pc_for_conjunction

    try:
        tca_dt = datetime.fromisoformat(tca)
        if tca_dt.tzinfo is None:
            tca_dt = tca_dt.replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid tca format. Expected ISO 8601.",
        ) from exc

    primary_tle = store.get(primary_norad_id)
    secondary_tle = store.get(secondary_norad_id)

    if primary_tle is None:
        raise HTTPException(
            status_code=404,
            detail="Primary satellite " + str(primary_norad_id) + " not found",
        )
    if secondary_tle is None:
        raise HTTPException(
            status_code=404,
            detail="Secondary satellite " + str(secondary_norad_id) + " not found",
        )

    satrec_p = build_satrec(primary_tle)
    satrec_s = build_satrec(secondary_tle)

    max_lead_hours = 24.0
    step_hours = max_lead_hours / (n_points - 1)
    lead_times = [round(max_lead_hours - i * step_hours, 4) for i in range(n_points)]

    history = []
    for lead_h in lead_times:
        sample_dt = tca_dt - timedelta(hours=lead_h)
        try:
            sv_p = propagate_at(satrec_p, sample_dt)
            sv_s = propagate_at(satrec_s, sample_dt)
        except RuntimeError:
            continue

        try:
            pc_data = compute_pc_for_conjunction(
                pos_primary_km=sv_p.position_km,
                vel_primary_km_s=sv_p.velocity_km_s,
                pos_secondary_km=sv_s.position_km,
                vel_secondary_km_s=sv_s.velocity_km_s,
            )
        except Exception:
            continue

        rel_vel_mag = math.sqrt(
            sum(
                (a - b) ** 2
                for a, b in zip(sv_p.velocity_km_s, sv_s.velocity_km_s, strict=True)
            )
        )
        history.append({
            "hours_before_tca": lead_h,
            "timestamp": sample_dt.isoformat(),
            "pc": pc_data["probability"],
            "miss_distance_km": round(pc_data["miss_distance_km"], 4),
            "relative_velocity_km_s": round(rel_vel_mag, 4),
        })

    return {
        "primary_norad_id": primary_norad_id,
        "secondary_norad_id": secondary_norad_id,
        "tca": tca_dt.isoformat(),
        "n_points": len(history),
        "history": history,
    }


# ---------------------------------------------------------------------------
# C3 layer proxies: shared TTL cache
# ---------------------------------------------------------------------------
#
# Every layer below follows one contract, borrowed from the proxies above and
# from gods-eye-view (https://github.com/bilawalsidhu/gods-eye-view, MIT), whose
# server keeps the third-party keys and hands the browser only the parsed rows:
#
#   * the response is always 200 with the layer's list key present,
#   * an upstream failure, a refusal or a missing API key sets ``error`` and
#     leaves the list empty, so the client renders "unavailable" instead of
#     throwing,
#   * successful payloads are cached for a per-layer TTL, because these feeds
#     are polled by every open tab and several of them ask for polite clients.
#
# API keys are read from the environment inside this process only. No handler
# below ever copies a key into a response body.

#: cache key -> (monotonic store time, payload).
_PROXY_CACHE: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str, ttl_s: float) -> dict | None:
    """Return the cached payload for *key* while it is younger than *ttl_s*."""
    entry = _PROXY_CACHE.get(key)
    if entry is None:
        return None
    if time.monotonic() - entry[0] >= ttl_s:
        return None
    return entry[1]


def _cache_put(key: str, payload: dict) -> dict:
    """Store *payload* under *key* and return it unchanged."""
    _PROXY_CACHE[key] = (time.monotonic(), payload)
    return payload


def clear_proxy_caches() -> None:
    """Drop every cached proxy payload. Used by the tests and by /doctor."""
    _PROXY_CACHE.clear()


def _as_float(value: object) -> float:
    """``float()`` for values typed as ``Any | None``.

    Feed rows arrive as untyped JSON, so every coordinate has to be narrowed
    before it is used. Anything that is not a number or a numeric string raises
    TypeError, which every caller already catches alongside ValueError.
    """
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"not a number: {value!r}")


def _as_int(value: object) -> int:
    """``int()`` for a counter held in an untyped state dict."""
    if isinstance(value, (int, float, str)):
        return int(value)
    return 0


def _browser_headers(purpose: str) -> dict[str, str]:
    """User-Agent several of these public APIs require of automated clients."""
    return {
        "User-Agent": f"sda-collision-predictor/0.2 (worldview {purpose})",
        "Accept": "application/json",
    }


async def _get_json(url: str, timeout_s: float = 10.0, purpose: str = "layer"):
    """GET *url* and return parsed JSON, or None on any failure at all.

    Returning None rather than raising keeps every caller's error branch to one
    shape, which is what makes the "empty list plus an error string" contract
    hold for feeds as different as a GeoJSON alert store and a CSV fire feed.
    """
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True, headers=_browser_headers(purpose)
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None


async def _get_text(url: str, timeout_s: float = 10.0, purpose: str = "layer") -> str | None:
    """GET *url* and return the body text, or None on any failure."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True, headers=_browser_headers(purpose)
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.text
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Keyless layer: upcoming launches (Launch Library 2)
# ---------------------------------------------------------------------------

_LAUNCH_URL = (
    "https://ll.thespacedevs.com/2.2.0/launch/upcoming/"
    "?limit=20&hide_recent_previous=true"
)
LAUNCH_CACHE_TTL_S = 900.0


def _parse_launches(data: dict) -> list[dict]:
    """Map Launch Library 2 results to pad-located rows for the globe."""
    rows: list[dict] = []
    for item in (data.get("results") or [])[:40]:
        pad = item.get("pad") or {}
        try:
            lat = _as_float(pad.get("latitude"))
            lon = _as_float(pad.get("longitude"))
        except (TypeError, ValueError):
            continue
        status = item.get("status") or {}
        provider = item.get("launch_service_provider") or {}
        mission = item.get("mission") or {}
        location = pad.get("location") or {}
        rows.append({
            "id": str(item.get("id") or ""),
            "name": item.get("name") or "",
            "net": item.get("net") or "",
            "window_start": item.get("window_start") or "",
            "status": status.get("abbrev") or status.get("name") or "",
            "provider": provider.get("name") or "",
            "mission": mission.get("name") or "",
            "orbit": ((mission.get("orbit") or {}) or {}).get("abbrev") or "",
            "pad": pad.get("name") or "",
            "site": location.get("name") or "",
            "lat": lat,
            "lon": lon,
        })
    return rows


@router.get(
    "/api/launches",
    tags=["WorldView"],
    summary="Upcoming orbital launches from Launch Library 2",
)
async def proxy_launches() -> dict:
    """Upcoming launches with pad coordinates for the launch layer.

    Source: Launch Library 2 (ll.thespacedevs.com), free and keyless. The
    client renders a countdown from ``net``; the server does not compute one so
    the value stays correct however long the response is cached.
    """
    cached = _cache_get("launches", LAUNCH_CACHE_TTL_S)
    if cached is not None:
        return cached

    data = await _get_json(_LAUNCH_URL, timeout_s=12.0, purpose="launches")
    if not isinstance(data, dict):
        return {"launches": [], "count": 0, "error": "Launch Library unavailable"}

    rows = _parse_launches(data)
    return _cache_put("launches", {
        "launches": rows,
        "count": len(rows),
        "source": "Launch Library 2 (thespacedevs)",
    })


# ---------------------------------------------------------------------------
# Keyless layer: NOAA SWPC space weather HUD strip
# ---------------------------------------------------------------------------

_SWPC_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
# The products/solar-wind/plasma-*.json files were retired (404). Their successor
# is a one-minute list of objects, about 2.8 MB for a day, with several
# spacecraft interleaved in it.
_SWPC_WIND_URL = "https://services.swpc.noaa.gov/json/rtsw/rtsw_wind_1m.json"
_SWPC_WIND_TIMEOUT_S = 20.0
_SWPC_ALERTS_URL = "https://services.swpc.noaa.gov/products/alerts.json"
SPACE_WEATHER_CACHE_TTL_S = 300.0

#: Kp -> NOAA G-scale label. Kp is a quasi-logarithmic 0-9 index; the G scale
#: starts at Kp 5 (G1) and each further step is one more storm level.
_G_SCALE = ((9.0, "G5"), (8.0, "G4"), (7.0, "G3"), (6.0, "G2"), (5.0, "G1"))


def _g_scale(kp: float | None) -> str:
    if kp is None:
        return ""
    for threshold, label in _G_SCALE:
        if kp >= threshold:
            return label
    return "G0"


def _swpc_records(data: object) -> list[dict]:
    """SWPC product rows as dicts, from either shape the products have used.

    Current products are a JSON list of objects. Older ones were a header row
    followed by value rows. Anything else yields no rows.
    """
    if not isinstance(data, list):
        return []
    header = data[0] if data else None
    if isinstance(header, list) and all(isinstance(name, str) for name in header):
        return [dict(zip(header, row, strict=False)) for row in data[1:] if isinstance(row, list)]
    return [row for row in data if isinstance(row, dict)]


#: What a reading can physically be. SWPC marks a missing one with a fill value
#: (-9999.9, -99999 and the like), which is a finite number and reached the strip
#: as a reading until it was held to these.
_KP_RANGE = (0.0, 9.0)
_WIND_SPEED_RANGE_KM_S = (50.0, 5000.0)
_WIND_DENSITY_RANGE_P_CM3 = (0.0, 2000.0)

#: A standby spacecraft takes over once the active one's newest reading is this
#: much older than its own. Below that the two simply report a minute apart.
_SWPC_STANDBY_AFTER_S = 600.0


def _swpc_number(
    record: dict, *keys: str, within: tuple[float, float] = (-math.inf, math.inf)
) -> float | None:
    """First number inside *within* found under *keys* (JSON numbers or numeric strings)."""
    low, high = within
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            continue  # float(True) is 1.0, and a flag is not a reading
        try:
            number = _as_float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and low <= number <= high:
            return number
    return None


def _swpc_time(tag: str) -> datetime | None:
    """A product time tag as a datetime; both the T and the space forms are in use."""
    try:
        return datetime.fromisoformat(tag.strip().replace(" ", "T").removesuffix("Z"))
    except ValueError:
        return None


def _swpc_kp(data: object) -> tuple[float | None, str]:
    """Newest Kp and its time tag, whatever order the rows arrive in."""
    # Key fallback adapted from simplifaisoul/osiris (MIT),
    # src/app/api/space-weather/route.ts: the 1-minute product names it kp_index.
    rows = [
        (str(rec.get("time_tag") or ""), kp)
        for rec in _swpc_records(data)
        if (kp := _swpc_number(rec, "Kp", "kp_index", within=_KP_RANGE)) is not None
    ]
    if not rows:
        return None, ""
    stamp, kp = max(rows, key=lambda row: row[0])
    return kp, stamp


def _swpc_wind(data: object) -> tuple[float | None, float | None, str]:
    """Newest solar wind speed (km/s), density (p/cm3) and time tag.

    The one-minute product carries several spacecraft at once. Rows from the
    one flagged ``active`` are the ones SWPC publishes, so they win. A standby
    source is used when no active row has a speed, or when the active one has
    gone quiet and its newest reading is more than ten minutes behind the
    standby's. The older product had no ``active`` flag, so a row without one
    counts as active.
    """
    active: list[tuple[str, float, float | None]] = []
    standby: list[tuple[str, float, float | None]] = []
    for rec in _swpc_records(data):
        speed = _swpc_number(rec, "proton_speed", "speed", within=_WIND_SPEED_RANGE_KM_S)
        if speed is None:
            continue
        row = (
            str(rec.get("time_tag") or ""),
            speed,
            _swpc_number(rec, "proton_density", "density", within=_WIND_DENSITY_RANGE_P_CM3),
        )
        (standby if rec.get("active") is False else active).append(row)
    if not active and not standby:
        return None, None, ""
    newest = max(active or standby, key=lambda row: row[0])
    if active and standby:
        backup = max(standby, key=lambda row: row[0])
        primary_at, backup_at = _swpc_time(newest[0]), _swpc_time(backup[0])
        if primary_at is not None and backup_at is not None:
            with contextlib.suppress(TypeError):  # one tag with a zone, one without
                if (backup_at - primary_at).total_seconds() > _SWPC_STANDBY_AFTER_S:
                    newest = backup
    stamp, speed, density = newest
    return speed, density, stamp


@router.get(
    "/api/space-weather",
    tags=["WorldView"],
    summary="NOAA SWPC planetary K index, solar wind and active alerts",
)
async def proxy_space_weather() -> dict:
    """Space-weather HUD strip: Kp, solar wind speed and density, alerts.

    Source: NOAA Space Weather Prediction Center products, public domain and
    keyless. Each of the three products degrades on its own, so a failure of
    the alert feed still leaves Kp and the solar wind on the strip.
    """
    cached = _cache_get("space-weather", SPACE_WEATHER_CACHE_TTL_S)
    if cached is not None:
        return cached

    kp_data, wind_data, alert_data = await asyncio.gather(
        _get_json(_SWPC_KP_URL, purpose="space weather"),
        _get_json(_SWPC_WIND_URL, timeout_s=_SWPC_WIND_TIMEOUT_S, purpose="space weather"),
        _get_json(_SWPC_ALERTS_URL, purpose="space weather"),
    )

    kp, kp_time = _swpc_kp(kp_data)
    speed_km_s, density_p_cm3, wind_time = _swpc_wind(wind_data)

    alerts: list[dict] = []
    if isinstance(alert_data, list):
        for entry in alert_data[:8]:
            if not isinstance(entry, dict):
                continue
            message = str(entry.get("message") or "")
            alerts.append({
                "product_id": entry.get("product_id") or "",
                "issued": entry.get("issue_datetime") or "",
                # The full bulletin is several hundred characters of fixed-width
                # text; the strip shows the first line and links out for the rest.
                "headline": message.strip().splitlines()[0] if message.strip() else "",
            })

    degraded = kp is None and speed_km_s is None and not alerts
    payload = {
        "kp": kp,
        "kp_time": kp_time,
        "g_scale": _g_scale(kp),
        "solar_wind_speed_km_s": speed_km_s,
        "solar_wind_density_p_cm3": density_p_cm3,
        "solar_wind_time": wind_time,
        "alerts": alerts,
        "count": len(alerts),
        "source": "NOAA SWPC",
    }
    if degraded:
        payload["error"] = "SWPC unavailable"
        return payload
    return _cache_put("space-weather", payload)


# ---------------------------------------------------------------------------
# Keyless layer: Holocene volcanoes (Smithsonian GVP)
# ---------------------------------------------------------------------------

_VOLCANO_URL = (
    "https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes"
    "&outputFormat=application/json&maxFeatures=1500"
)
VOLCANO_CACHE_TTL_S = 86400.0


def _parse_volcanoes(data: dict) -> list[dict]:
    """Map the Smithsonian GVP WFS GeoJSON to globe rows."""
    rows: list[dict] = []
    for feature in (data.get("features") or [])[:1500]:
        geometry = (feature or {}).get("geometry") or {}
        coords = geometry.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            continue
        props = feature.get("properties") or {}
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        rows.append({
            "name": props.get("Volcano_Name") or props.get("name") or "",
            "country": props.get("Country") or "",
            "type": props.get("Primary_Volcano_Type") or "",
            "elevation_m": props.get("Elevation") or 0,
            "last_eruption": str(props.get("Last_Eruption_Year") or ""),
            "lat": lat,
            "lon": lon,
        })
    return rows


@router.get(
    "/api/volcanoes",
    tags=["WorldView"],
    summary="Holocene volcanoes from the Smithsonian Global Volcanism Program",
)
async def proxy_volcanoes() -> dict:
    """Volcano locations for the geology layer.

    Source: Smithsonian Institution Global Volcanism Program WFS, keyless. The
    catalog changes on the order of once a year, so it is cached for a day.
    """
    cached = _cache_get("volcanoes", VOLCANO_CACHE_TTL_S)
    if cached is not None:
        return cached

    data = await _get_json(_VOLCANO_URL, timeout_s=20.0, purpose="volcanoes")
    if not isinstance(data, dict):
        return {"volcanoes": [], "count": 0, "error": "Smithsonian GVP unavailable"}

    rows = _parse_volcanoes(data)
    if not rows:
        return {"volcanoes": [], "count": 0, "error": "Smithsonian GVP returned no features"}
    return _cache_put("volcanoes", {
        "volcanoes": rows,
        "count": len(rows),
        "source": "Smithsonian Institution Global Volcanism Program",
    })


# ---------------------------------------------------------------------------
# Keyless layer: active US weather alerts (NWS)
# ---------------------------------------------------------------------------

_NWS_ALERTS_URL = "https://api.weather.gov/alerts/active?status=actual&limit=200"
WEATHER_ALERT_CACHE_TTL_S = 180.0

#: NWS severity -> a colour the client can use without a second lookup.
_NWS_SEVERITY_COLORS = {
    "Extreme": "#ff2d55",
    "Severe": "#ff8800",
    "Moderate": "#ffd24e",
    "Minor": "#4ea3ff",
}


def _alert_rings(geometry: object) -> list[list[float]]:
    """Flatten a GeoJSON Polygon or MultiPolygon into lon/lat ring arrays.

    Only outer rings are kept: an alert polygon's holes are rare and a hole
    drawn as a filled ring would read as a second alert.
    """
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    polygons: list = []
    if kind == "Polygon" and isinstance(coords, list):
        polygons = [coords]
    elif kind == "MultiPolygon" and isinstance(coords, list):
        polygons = list(coords)
    else:
        return []

    rings: list[list[float]] = []
    for polygon in polygons[:6]:
        if not isinstance(polygon, list) or not polygon:
            continue
        outer = polygon[0]
        if not isinstance(outer, list) or len(outer) < 3:
            continue
        flat: list[float] = []
        for point in outer[:400]:
            if not isinstance(point, list) or len(point) < 2:
                continue
            with contextlib.suppress(TypeError, ValueError):
                flat.extend([float(point[0]), float(point[1])])
        if len(flat) >= 6:
            rings.append(flat)
    return rings


@router.get(
    "/api/weather-alerts",
    tags=["WorldView"],
    summary="Active National Weather Service alerts (United States)",
)
async def proxy_weather_alerts() -> dict:
    """Active NWS alert polygons.

    Source: api.weather.gov, keyless, US coverage only. Alerts without a
    geometry (county-coded only) are dropped rather than placed at a guessed
    centroid.
    """
    cached = _cache_get("weather-alerts", WEATHER_ALERT_CACHE_TTL_S)
    if cached is not None:
        return cached

    data = await _get_json(_NWS_ALERTS_URL, timeout_s=15.0, purpose="weather alerts")
    if not isinstance(data, dict):
        return {"alerts": [], "count": 0, "error": "api.weather.gov unavailable"}

    rows: list[dict] = []
    for feature in (data.get("features") or [])[:300]:
        rings = _alert_rings((feature or {}).get("geometry"))
        if not rings:
            continue
        props = feature.get("properties") or {}
        severity = str(props.get("severity") or "")
        rows.append({
            "id": str(feature.get("id") or ""),
            "event": props.get("event") or "",
            "headline": props.get("headline") or "",
            "severity": severity,
            "urgency": props.get("urgency") or "",
            "area": props.get("areaDesc") or "",
            "expires": props.get("expires") or "",
            "color": _NWS_SEVERITY_COLORS.get(severity, "#8899aa"),
            "rings": rings,
        })

    return _cache_put("weather-alerts", {
        "alerts": rows,
        "count": len(rows),
        "source": "NOAA National Weather Service (US only)",
    })


# ---------------------------------------------------------------------------
# Keyless layer: GDELT GEO 2.1 recent events
# ---------------------------------------------------------------------------

_GDELT_URL = (
    "https://api.gdeltproject.org/api/v2/geo/geo"
    "?query={query}&format=GeoJSON&mode=PointData&timespan={timespan}"
)
GDELT_CACHE_TTL_S = 600.0
_GDELT_MAX_QUERY = 120


@router.get(
    "/api/gdelt",
    tags=["WorldView"],
    summary="Geolocated news events from the GDELT GEO 2.1 API",
)
async def proxy_gdelt(
    q: str = Query(default="satellite", min_length=2, max_length=_GDELT_MAX_QUERY),
    timespan: str = Query(default="24H", pattern=r"^\d{1,3}[HD]$"),
) -> dict:
    """Recent geolocated news mentions as globe points.

    Source: GDELT Project GEO 2.1 API, keyless. ``count`` on each point is how
    many articles GDELT clustered at that place, not an event severity.
    """
    query = q.strip()
    cache_key = f"gdelt:{query.lower()}:{timespan.upper()}"
    cached = _cache_get(cache_key, GDELT_CACHE_TTL_S)
    if cached is not None:
        return cached

    url = _GDELT_URL.format(query=quote(query), timespan=timespan.upper())
    data = await _get_json(url, timeout_s=15.0, purpose="gdelt")
    if not isinstance(data, dict):
        return {"events": [], "count": 0, "query": query, "error": "GDELT unavailable"}

    rows: list[dict] = []
    for feature in (data.get("features") or [])[:400]:
        geometry = (feature or {}).get("geometry") or {}
        coords = geometry.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            continue
        props = feature.get("properties") or {}
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        rows.append({
            "name": props.get("name") or "",
            "count": props.get("count") or 0,
            "lat": lat,
            "lon": lon,
        })

    return _cache_put(cache_key, {
        "events": rows,
        "count": len(rows),
        "query": query,
        "timespan": timespan.upper(),
        "source": "GDELT Project GEO 2.1",
    })


# ---------------------------------------------------------------------------
# Keyless layer: Radio Browser stations near a point
# ---------------------------------------------------------------------------

_RADIO_URL = (
    "https://de1.api.radio-browser.info/json/stations/search"
    "?limit={limit}&hidebroken=true&has_geo_info=true&order=clickcount&reverse=true"
    "&geo_lat={lat}&geo_long={lon}&geo_distance={distance_m}"
)
RADIO_CACHE_TTL_S = 900.0


@router.get(
    "/api/radio",
    tags=["WorldView"],
    summary="Internet radio stations near a point (Radio Browser)",
)
async def proxy_radio(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
    radius_km: float = Query(default=400.0, gt=0.0, le=5000.0),
    limit: int = Query(default=60, ge=1, le=200),
) -> dict:
    """Geolocated radio stations around the camera target.

    Source: Radio Browser community API, keyless. Stream URLs are passed
    straight through; the browser plays them, the server never proxies audio.
    """
    cache_key = f"radio:{round(lat, 1)}:{round(lon, 1)}:{int(radius_km)}:{limit}"
    cached = _cache_get(cache_key, RADIO_CACHE_TTL_S)
    if cached is not None:
        return cached

    url = _RADIO_URL.format(
        limit=limit, lat=lat, lon=lon, distance_m=int(radius_km * 1000.0)
    )
    data = await _get_json(url, timeout_s=12.0, purpose="radio")
    if not isinstance(data, list):
        return {"stations": [], "count": 0, "error": "Radio Browser unavailable"}

    rows: list[dict] = []
    for row in data[:limit]:
        if not isinstance(row, dict):
            continue
        try:
            station_lat = _as_float(row.get("geo_lat"))
            station_lon = _as_float(row.get("geo_long"))
        except (TypeError, ValueError):
            continue
        rows.append({
            "id": str(row.get("stationuuid") or ""),
            "name": (row.get("name") or "").strip(),
            "country": row.get("country") or "",
            "tags": row.get("tags") or "",
            "bitrate": row.get("bitrate") or 0,
            "url": row.get("url_resolved") or row.get("url") or "",
            "lat": station_lat,
            "lon": station_lon,
        })

    return _cache_put(cache_key, {
        "stations": rows,
        "count": len(rows),
        "source": "Radio Browser",
    })


# ---------------------------------------------------------------------------
# Keyless layer: GBFS bikeshare stations
# ---------------------------------------------------------------------------

#: city -> GBFS feed root. Every one of these publishes the General Bikeshare
#: Feed Specification openly; the list is configurable only in the sense that a
#: new city is one entry here plus one option in the client selector.
_GBFS_CITIES: dict[str, tuple[str, str]] = {
    "nyc": ("New York (Citi Bike)", "https://gbfs.citibikenyc.com/gbfs/en/"),
    "dc": ("Washington DC (Capital Bikeshare)", "https://gbfs.capitalbikeshare.com/gbfs/en/"),
    "chicago": ("Chicago (Divvy)", "https://gbfs.divvybikes.com/gbfs/en/"),
    "boston": ("Boston (Bluebikes)", "https://gbfs.bluebikes.com/gbfs/en/"),
    "sf": ("San Francisco (Bay Wheels)", "https://gbfs.baywheels.com/gbfs/en/"),
}
GBFS_CACHE_TTL_S = 120.0


def _merge_gbfs(info: dict, status: dict) -> list[dict]:
    """Join GBFS station_information with station_status on station_id."""
    by_id: dict[str, dict] = {}
    for station in ((info.get("data") or {}).get("stations") or [])[:2000]:
        if not isinstance(station, dict):
            continue
        station_id = str(station.get("station_id") or "")
        try:
            lat = _as_float(station.get("lat"))
            lon = _as_float(station.get("lon"))
        except (TypeError, ValueError):
            continue
        if not station_id:
            continue
        by_id[station_id] = {
            "id": station_id,
            "name": station.get("name") or "",
            "capacity": station.get("capacity") or 0,
            "lat": lat,
            "lon": lon,
            "bikes_available": None,
            "docks_available": None,
        }

    for row in ((status.get("data") or {}).get("stations") or [])[:2000]:
        if not isinstance(row, dict):
            continue
        entry = by_id.get(str(row.get("station_id") or ""))
        if entry is None:
            continue
        entry["bikes_available"] = row.get("num_bikes_available")
        entry["docks_available"] = row.get("num_docks_available")

    return list(by_id.values())


@router.get(
    "/api/bikeshare",
    tags=["WorldView"],
    summary="GBFS bikeshare stations and live availability",
)
async def proxy_bikeshare(
    city: str = Query(default="nyc", description="One of: " + ", ".join(_GBFS_CITIES)),
) -> dict:
    """Bikeshare docks with live bike and dock counts.

    Source: the operator's own GBFS feed, keyless and openly licensed. Both
    halves of the feed are fetched together; either one failing gives the
    empty-list answer rather than a half-populated map.
    """
    key = city.lower().strip()
    if key not in _GBFS_CITIES:
        return {
            "stations": [],
            "count": 0,
            "cities": sorted(_GBFS_CITIES),
            "error": f"Unknown city {city!r}",
        }

    cache_key = f"bikeshare:{key}"
    cached = _cache_get(cache_key, GBFS_CACHE_TTL_S)
    if cached is not None:
        return cached

    label, root = _GBFS_CITIES[key]
    info, status = await asyncio.gather(
        _get_json(root + "station_information.json", purpose="bikeshare"),
        _get_json(root + "station_status.json", purpose="bikeshare"),
    )
    if not isinstance(info, dict) or not isinstance(status, dict):
        return {
            "stations": [],
            "count": 0,
            "city": key,
            "cities": sorted(_GBFS_CITIES),
            "error": "GBFS feed unavailable",
        }

    rows = _merge_gbfs(info, status)
    return _cache_put(cache_key, {
        "stations": rows,
        "count": len(rows),
        "city": key,
        "city_label": label,
        "cities": sorted(_GBFS_CITIES),
        "source": "General Bikeshare Feed Specification",
    })


# ---------------------------------------------------------------------------
# Keyless layer: OSM military installations (Overpass)
# ---------------------------------------------------------------------------

_MILITARY_MAX_SPAN_DEG = 6.0
_MILITARY_MAX_FEATURES = 300


@router.get(
    "/api/military-sites",
    tags=["WorldView"],
    summary="Military installations in a bounding box from OpenStreetMap",
)
async def proxy_military_sites(
    south: float = Query(..., ge=-90.0, le=90.0),
    west: float = Query(..., ge=-180.0, le=180.0),
    north: float = Query(..., ge=-90.0, le=90.0),
    east: float = Query(..., ge=-180.0, le=180.0),
) -> dict:
    """Mapped military land use and installations inside the bbox.

    Source: OpenStreetMap via Overpass, ODbL, same proxy pattern and polite
    User-Agent as the traffic layer. ``out center`` collapses ways and
    relations to one point each, which is what the globe draws.
    """
    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="Empty bounding box")
    if (north - south) > _MILITARY_MAX_SPAN_DEG or (east - west) > _MILITARY_MAX_SPAN_DEG:
        raise HTTPException(
            status_code=422,
            detail=f"Bounding box span exceeds {_MILITARY_MAX_SPAN_DEG} degrees",
        )

    bbox = f"({south},{west},{north},{east})"
    overpass_query = (
        "[out:json][timeout:20];("
        f'node["military"]{bbox};'
        f'way["military"]{bbox};'
        f'way["landuse"="military"]{bbox};'
        f'relation["landuse"="military"]{bbox};'
        f");out center {_MILITARY_MAX_FEATURES};"
    )
    url = "https://overpass-api.de/api/interpreter?data=" + quote(overpass_query)
    headers = {"User-Agent": "sda-collision-predictor/0.2 (worldview military sites)"}

    try:
        async with httpx.AsyncClient(timeout=25.0, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {"sites": [], "count": 0, "error": "Overpass unavailable"}
            data = resp.json()
    except Exception:
        return {"sites": [], "count": 0, "error": "Overpass request failed"}

    rows: list[dict] = []
    for element in (data.get("elements") or [])[:_MILITARY_MAX_FEATURES]:
        if not isinstance(element, dict):
            continue
        center = element.get("center") or {}
        lat = element.get("lat", center.get("lat"))
        lon = element.get("lon", center.get("lon"))
        if lat is None or lon is None:
            continue
        tags = element.get("tags") or {}
        rows.append({
            "id": f"{element.get('type', 'node')}/{element.get('id', '')}",
            "name": tags.get("name") or tags.get("operator") or "UNNAMED",
            "kind": tags.get("military") or tags.get("landuse") or "military",
            "operator": tags.get("operator") or "",
            "lat": float(lat),
            "lon": float(lon),
        })

    return {
        "sites": rows,
        "count": len(rows),
        "source": "OpenStreetMap via Overpass (ODbL)",
    }


# ---------------------------------------------------------------------------
# Keyed layer: NASA FIRMS active fires (FIRMS_MAP_KEY)
# ---------------------------------------------------------------------------

_FIRMS_URL = (
    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
    "{key}/{source}/{area}/{days}"
)
FIRMS_CACHE_TTL_S = 600.0
_FIRMS_SOURCES = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "MODIS_NRT")
_FIRMS_MAX_ROWS = 3000


def _parse_firms_csv(text: str, max_rows: int = _FIRMS_MAX_ROWS) -> list[dict]:
    """Parse the FIRMS area CSV into fire rows.

    The header names differ between the VIIRS and MODIS products (bright_ti4
    against brightness), so columns are looked up by name and the brightness
    field is whichever of the known names is present.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    header = [name.strip().lower() for name in lines[0].split(",")]
    index = {name: i for i, name in enumerate(header)}
    if "latitude" not in index or "longitude" not in index:
        return []

    def field(cells: list[str], name: str) -> str:
        position = index.get(name)
        if position is None or position >= len(cells):
            return ""
        return cells[position].strip()

    rows: list[dict] = []
    for line in lines[1:max_rows + 1]:
        cells = line.split(",")
        try:
            lat = float(field(cells, "latitude"))
            lon = float(field(cells, "longitude"))
        except ValueError:
            continue
        brightness = field(cells, "bright_ti4") or field(cells, "brightness")
        frp = field(cells, "frp")
        rows.append({
            "lat": lat,
            "lon": lon,
            "brightness_k": float(brightness) if brightness else 0.0,
            "frp_mw": float(frp) if frp else 0.0,
            "confidence": field(cells, "confidence"),
            "acq_date": field(cells, "acq_date"),
            "acq_time": field(cells, "acq_time"),
            "daynight": field(cells, "daynight"),
            "satellite": field(cells, "satellite"),
        })
    return rows


@router.get(
    "/api/fires",
    tags=["WorldView"],
    summary="NASA FIRMS active fire detections (needs FIRMS_MAP_KEY)",
)
async def proxy_fires(
    days: int = Query(default=1, ge=1, le=7),
    source: str = Query(default="VIIRS_SNPP_NRT"),
    south: float = Query(default=-90.0, ge=-90.0, le=90.0),
    west: float = Query(default=-180.0, ge=-180.0, le=180.0),
    north: float = Query(default=90.0, ge=-90.0, le=90.0),
    east: float = Query(default=180.0, ge=-180.0, le=180.0),
) -> dict:
    """Active fire detections for the fire layer.

    The FIRMS map key is read from ``FIRMS_MAP_KEY`` in this process and is
    never included in the response, so the browser never sees it. Without the
    key the endpoint answers 200 with an empty list and a setup hint, which is
    what makes the layer toggle degrade instead of erroring.
    """
    key = (os.environ.get("FIRMS_MAP_KEY") or "").strip()
    if not key:
        return {
            "fires": [],
            "count": 0,
            "key_configured": False,
            "error": "FIRMS_MAP_KEY is not set on the server",
        }
    if source not in _FIRMS_SOURCES:
        return {
            "fires": [],
            "count": 0,
            "key_configured": True,
            "error": f"Unknown source {source!r}; one of {', '.join(_FIRMS_SOURCES)}",
        }
    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="Empty bounding box")

    area = "world" if (south, west, north, east) == (-90.0, -180.0, 90.0, 180.0) else (
        f"{west},{south},{east},{north}"
    )
    cache_key = f"fires:{source}:{days}:{area}"
    cached = _cache_get(cache_key, FIRMS_CACHE_TTL_S)
    if cached is not None:
        return cached

    url = _FIRMS_URL.format(key=key, source=source, area=area, days=days)
    text = await _get_text(url, timeout_s=25.0, purpose="fires")
    if text is None:
        return {
            "fires": [],
            "count": 0,
            "key_configured": True,
            "error": "FIRMS unavailable",
        }
    if "Invalid" in text[:200] or text.lstrip().startswith("<"):
        # FIRMS answers a bad or exhausted key with prose, not an HTTP error.
        return {
            "fires": [],
            "count": 0,
            "key_configured": True,
            "error": "FIRMS refused the request (key invalid or transaction limit)",
        }

    rows = _parse_firms_csv(text)
    return _cache_put(cache_key, {
        "fires": rows,
        "count": len(rows),
        "key_configured": True,
        "source": f"NASA FIRMS {source}",
    })


# ---------------------------------------------------------------------------
# Keyed layer: AIS vessel positions over the AISStream WebSocket
# ---------------------------------------------------------------------------
#
# AISStream pushes vessel reports over a single authenticated WebSocket; there
# is no request/response endpoint to poll. So the server holds one subscription
# open, keeps the latest report per MMSI, and serves that cache over a plain
# GET with a bounding box. The API key stays in this process: it is sent to
# AISStream in the subscribe frame and never appears in a response.
#
# The reader stops itself once nobody has asked for AIS in AIS_IDLE_STOP_S, so
# a socket is only open while the layer is actually on, and no shutdown hook in
# api.py (which this package does not own) is needed.

AIS_WS_URL = "wss://stream.aisstream.io/v0/stream"
AIS_CACHE_LIMIT = 4000
AIS_STALE_S = 900.0
AIS_IDLE_STOP_S = 300.0
AIS_RECONNECT_MIN_S = 2.0
AIS_RECONNECT_MAX_S = 60.0

_AIS_STATE: dict[str, object] = {
    "vessels": {},          # mmsi -> row
    "task": None,           # asyncio.Task or None
    "connected": False,
    "error": None,
    "messages": 0,
    "last_request": 0.0,
}


def _ais_vessels() -> dict[str, dict]:
    """The per-MMSI cache, typed for mypy."""
    vessels = _AIS_STATE["vessels"]
    if not isinstance(vessels, dict):
        vessels = {}
        _AIS_STATE["vessels"] = vessels
    return vessels


def ais_subscribe_frame(api_key: str) -> str:
    """The AISStream subscribe frame: whole globe, position reports only."""
    return json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": [[[-90.0, -180.0], [90.0, 180.0]]],
        "FilterMessageTypes": ["PositionReport"],
    })


def ais_ingest(payload: object) -> bool:
    """Fold one AISStream frame into the per-MMSI cache.

    Returns True when a vessel row was written. Frames of other message types,
    frames without a position and malformed frames are ignored rather than
    raising, because one bad frame must not tear down the subscription.
    """
    if not isinstance(payload, dict):
        return False
    meta = payload.get("MetaData")
    if not isinstance(meta, dict):
        return False
    mmsi = meta.get("MMSI") or meta.get("MMSI_String")
    if mmsi is None:
        return False

    message = payload.get("Message")
    report = (message or {}).get("PositionReport") or {} if isinstance(message, dict) else {}
    lat = report.get("Latitude", meta.get("latitude"))
    lon = report.get("Longitude", meta.get("longitude"))
    try:
        lat_f = _as_float(lat)
        lon_f = _as_float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return False

    vessels = _ais_vessels()
    vessels[str(mmsi)] = {
        "mmsi": str(mmsi),
        "name": str(meta.get("ShipName") or "").strip(),
        "lat": lat_f,
        "lon": lon_f,
        "sog_kn": report.get("Sog"),
        "cog_deg": report.get("Cog"),
        "heading_deg": report.get("TrueHeading"),
        "nav_status": report.get("NavigationalStatus"),
        "time_utc": str(meta.get("time_utc") or ""),
        "received": time.time(),
    }
    _AIS_STATE["messages"] = _as_int(_AIS_STATE.get("messages")) + 1

    # Oldest-first eviction keeps the cache bounded without a sweep on every
    # frame; dict order is insertion order and each write re-inserts.
    while len(vessels) > AIS_CACHE_LIMIT:
        vessels.pop(next(iter(vessels)), None)
    return True


async def _open_ais_socket(url: str) -> Any:
    """Open the AISStream WebSocket. Split out so tests can substitute it."""
    from websockets.asyncio.client import connect

    return connect(url, open_timeout=10, close_timeout=5)


def _ais_idle() -> bool:
    """True once nothing has asked for AIS data for AIS_IDLE_STOP_S."""
    last = float(_AIS_STATE.get("last_request") or 0.0)  # type: ignore[arg-type]
    return (time.monotonic() - last) >= AIS_IDLE_STOP_S


async def _ais_reader(api_key: str) -> None:
    """Hold the AISStream subscription open until the layer goes idle."""
    backoff = AIS_RECONNECT_MIN_S
    try:
        while not _ais_idle():
            try:
                socket_cm = await _open_ais_socket(AIS_WS_URL)
                async with socket_cm as socket:
                    await socket.send(ais_subscribe_frame(api_key))
                    _AIS_STATE["connected"] = True
                    _AIS_STATE["error"] = None
                    backoff = AIS_RECONNECT_MIN_S
                    async for raw in socket:
                        with contextlib.suppress(ValueError, TypeError):
                            ais_ingest(json.loads(raw))
                        if _ais_idle():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network, auth, protocol: all retryable
                _AIS_STATE["error"] = f"{type(exc).__name__}: {exc}"
            _AIS_STATE["connected"] = False
            if _ais_idle():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, AIS_RECONNECT_MAX_S)
    finally:
        _AIS_STATE["connected"] = False
        _AIS_STATE["task"] = None


def _ais_ensure_stream(api_key: str) -> None:
    """Start the reader task if one is not already running."""
    task = _AIS_STATE.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        return
    with contextlib.suppress(RuntimeError):
        _AIS_STATE["task"] = asyncio.get_running_loop().create_task(_ais_reader(api_key))


def stop_ais_stream() -> None:
    """Cancel the reader and drop the cache. Used by the tests."""
    task = _AIS_STATE.get("task")
    if isinstance(task, asyncio.Task):
        task.cancel()
    _AIS_STATE["task"] = None
    _AIS_STATE["connected"] = False
    _AIS_STATE["error"] = None
    _AIS_STATE["messages"] = 0
    _AIS_STATE["last_request"] = 0.0
    _ais_vessels().clear()


@router.get(
    "/api/ais",
    tags=["WorldView"],
    summary="AIS vessel positions in a bounding box (needs AISSTREAM_API_KEY)",
)
async def proxy_ais(
    south: float = Query(default=-90.0, ge=-90.0, le=90.0),
    west: float = Query(default=-180.0, ge=-180.0, le=180.0),
    north: float = Query(default=90.0, ge=-90.0, le=90.0),
    east: float = Query(default=180.0, ge=-180.0, le=180.0),
    limit: int = Query(default=2000, ge=1, le=5000),
) -> dict:
    """Latest cached vessel position per MMSI inside the bounding box.

    The first call starts the subscription, so it usually answers with a small
    or empty list and ``connected`` false; the layer polls and fills in. The
    key is read from ``AISSTREAM_API_KEY`` server-side and never returned.
    """
    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="Empty bounding box")

    key = (os.environ.get("AISSTREAM_API_KEY") or "").strip()
    if not key:
        return {
            "vessels": [],
            "count": 0,
            "key_configured": False,
            "connected": False,
            "error": "AISSTREAM_API_KEY is not set on the server",
        }

    _AIS_STATE["last_request"] = time.monotonic()
    _ais_ensure_stream(key)

    cutoff = time.time() - AIS_STALE_S
    rows: list[dict] = []
    for vessel in list(_ais_vessels().values()):
        if float(vessel["received"]) < cutoff:
            continue
        if not (south <= vessel["lat"] <= north):
            continue
        if not (west <= vessel["lon"] <= east):
            continue
        rows.append({k: v for k, v in vessel.items() if k != "received"})
        if len(rows) >= limit:
            break

    return {
        "vessels": rows,
        "count": len(rows),
        "key_configured": True,
        "connected": bool(_AIS_STATE.get("connected")),
        "cached_vessels": len(_ais_vessels()),
        "messages": _AIS_STATE.get("messages", 0),
        "error": _AIS_STATE.get("error"),
        "source": "AISStream",
    }
