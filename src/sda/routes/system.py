"""System routes: health, metrics, space weather."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import APIRouter

from sda import __version__
from sda.conjunction import get_conjunction_history

router = APIRouter()


@router.get("/health", tags=["System"], summary="Health check and system status")
def health():
    """Returns system status, satellite count, TLE freshness summary, and uptime."""
    from sda.api import CELESTRAK_GROUPS, _last_tle_refresh, _live_f107, _metrics, store

    stale = store.get_stale_satellites()
    return {
        "status": "ok",
        "satellites_tracked": store.count(),
        "stale_tles": len(stale),
        "live_data": {
            "f107_solar_flux": _live_f107,
            "last_tle_refresh": _last_tle_refresh.isoformat() if _last_tle_refresh else None,
            "celestrak_groups": len(CELESTRAK_GROUPS),
            "auto_refresh_hours": 2,
        },
        "version": __version__,
        "timestamp": datetime.now(UTC).isoformat(),
        "startup_time": _metrics["startup_time"],
    }


@router.get(
    "/metrics",
    tags=["System"],
    summary="Performance and operational metrics",
    response_description="Current system performance counters and timing data",
)
def get_metrics():
    """Returns operational metrics."""
    from sda.api import _metrics, store

    freshness_list = store.get_all_freshness()
    fresh = sum(1 for f in freshness_list if f.freshness == "FRESH")
    aging = sum(1 for f in freshness_list if f.freshness == "AGING")
    stale = sum(1 for f in freshness_list if f.freshness == "STALE")
    expired = sum(1 for f in freshness_list if f.freshness == "EXPIRED")

    return {
        "satellites_tracked": store.count(),
        "catalog_freshness": {
            "fresh": fresh,
            "aging": aging,
            "stale": stale,
            "expired": expired,
        },
        "conjunction_history_size": len(get_conjunction_history(limit=10000)),
        "performance": {
            "total_api_requests": _metrics["api_requests"],
            "total_conjunctions_run": _metrics["conjunctions_run"],
            "total_conjunctions_found": _metrics["conjunctions_found"],
            "last_analysis_duration_ms": _metrics["last_analysis_duration_ms"],
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get(
    "/space-weather",
    tags=["System"],
    summary="Live space weather data from NOAA SWPC",
)
async def space_weather():
    """Returns current space weather conditions affecting orbital operations."""
    from sda.api import _live_f107

    result = {"f107_solar_flux": _live_f107, "source": "noaa_swpc"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            kp_resp = await client.get(
                "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
            )
            if kp_resp.status_code == 200:
                kp_data = kp_resp.json()
                if kp_data:
                    latest_kp = kp_data[-1]
                    result["kp_index"] = latest_kp.get("kp_index")
                    result["kp_timestamp"] = latest_kp.get("time_tag")

            sw_resp = await client.get(
                "https://services.swpc.noaa.gov/products/summary/solar-wind-speed.json"
            )
            if sw_resp.status_code == 200:
                sw_data = sw_resp.json()
                result["solar_wind_speed_km_s"] = sw_data.get("WindSpeed")
                result["solar_wind_timestamp"] = sw_data.get("TimeStamp")

            storm_resp = await client.get(
                "https://services.swpc.noaa.gov/products/noaa-scales.json"
            )
            if storm_resp.status_code == 200:
                storm_data = storm_resp.json()
                if isinstance(storm_data, dict) and "0" in storm_data:
                    current = storm_data["0"]
                    result["geomagnetic_storm"] = current.get("G", {}).get("Scale")
                    result["solar_radiation"] = current.get("S", {}).get("Scale")
                    result["radio_blackout"] = current.get("R", {}).get("Scale")

    except Exception:
        result["warning"] = "Some space weather sources unavailable"

    result["timestamp"] = datetime.now(UTC).isoformat()
    return result
