"""WorldView globe, WebSocket live tracking, and proxy routes."""

from __future__ import annotations

import asyncio
import math
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import numpy as np
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from sda.dashboard import DASHBOARD_HTML
from sda.models import ConjunctionRequest
from sda.propagator import build_satrec, datetime_to_jd, propagate_at
from sda.worldview import WORLDVIEW_HTML

router = APIRouter()


# ---------------------------------------------------------------------------
# Orbit regime classification
# ---------------------------------------------------------------------------

_GEO_ALTITUDE_KM = 35786.0
_GEO_BELT_HALF_WIDTH_KM = 14.0

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
    HEO  eccentricity > 0.25 OR alt >= 35800 km
    GEO  35786 <= alt < 35800 km  (within nominal GEO belt +/-14 km)
    MEO  2000 <= alt < 35786 km
    LEO  alt < 2000 km
    """
    if eccentricity > 0.25 or alt_km >= (_GEO_ALTITUDE_KM + _GEO_BELT_HALF_WIDTH_KM):
        return "HEO"
    if alt_km >= _GEO_ALTITUDE_KM:
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


def _compute_geographic_positions(store, epoch_dt: datetime | None = None):
    """Compute geographic positions for all tracked satellites.

    Parameters
    ----------
    store
        TLEStore singleton.
    epoch_dt
        UTC datetime to propagate to.  Defaults to the current time when None.

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

@router.websocket("/ws/positions")
async def websocket_positions(websocket: WebSocket):
    """WebSocket endpoint for real-time satellite position updates."""
    from sda.api import store

    await websocket.accept()
    interval = 2.0

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=0.01)
                if "interval" in data:
                    interval = max(1.0, min(30.0, float(data["interval"])))
            except (TimeoutError, Exception):
                pass

            satellites, now = _compute_geographic_positions(store)

            await websocket.send_json({
                "satellites": satellites,
                "count": len(satellites),
                "timestamp": now.isoformat(),
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


@router.get("/api/flights", tags=["WorldView"], summary="Live flight data from OpenSky Network")
async def proxy_flights():
    """Proxy live aircraft positions from OpenSky Network for globe display."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://opensky-network.org/api/states/all")
            if resp.status_code != 200:
                return {"flights": [], "count": 0, "error": "OpenSky unavailable"}
            data = resp.json()
    except Exception:
        return {"flights": [], "count": 0, "error": "OpenSky request failed"}

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

    return {"flights": flights, "count": len(flights), "timestamp": data.get("time")}


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
    enables Google Photorealistic 3D Tiles. Both are optional — the globe
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


@router.post(
    "/api/conjunction-globe-data",
    tags=["WorldView"],
    summary="Conjunction events formatted for CesiumJS globe rendering",
)
def conjunction_globe_data(request: ConjunctionRequest):
    """Run conjunction analysis, enriching each event with geographic
    positions and B-plane projection data for CesiumJS rendering.

    Response fields per conjunction: primary_position, secondary_position
    (lat/lon/alt_km at TCA), risk, risk_color, miss_distance_km,
    relative_velocity_km_s, tca (ISO 8601), bplane (miss_x_km, miss_y_km,
    sigma_combined_km), pc (collision probability).
    """
    from sda.api import store
    from sda.conjunction import find_conjunctions
    from sda.probability import _rotation_matrix_to_conjunction_plane

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
