"""Analysis routes: maneuver planning, Monte Carlo, orbital decay."""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from sda.conjunction import classify_risk
from sda.decay import estimate_decay
from sda.maneuver import plan_maneuver
from sda.models import (
    ConjunctionEvent,
    ManeuverRequest,
    MonteCarloRequest,
    RiskLevel,
)
from sda.montecarlo import run_monte_carlo
from sda.propagator import build_satrec, compute_distance, compute_relative_velocity, propagate_at

router = APIRouter()


@router.post(
    "/maneuver",
    tags=["Maneuver Planning"],
    summary="Plan collision avoidance maneuver",
    response_description="Maneuver options with delta-V, burn time, and new miss distance",
)
def plan_avoidance_maneuver(request: ManeuverRequest):
    """Compute minimum delta-V impulse maneuvers to increase miss distance."""
    from sda.api import store

    tle_p = store.get(request.primary_norad_id)
    tle_s = store.get(request.secondary_norad_id)
    if tle_p is None or tle_s is None:
        raise HTTPException(status_code=404, detail="One or both satellites not found")

    event = ConjunctionEvent(
        primary=request.primary_norad_id,
        secondary=request.secondary_norad_id,
        primary_name=tle_p.name,
        secondary_name=tle_s.name,
        tca=request.tca,
        miss_distance_km=0.0,
        relative_velocity_km_s=0.0,
        risk=RiskLevel.CRITICAL,
    )

    try:
        sv_p = propagate_at(build_satrec(tle_p), request.tca)
        sv_s = propagate_at(build_satrec(tle_s), request.tca)
        miss = compute_distance(sv_p.position_km, sv_s.position_km)
        rel_v = compute_relative_velocity(sv_p.velocity_km_s, sv_s.velocity_km_s)
        event.miss_distance_km = round(miss, 4)
        event.relative_velocity_km_s = round(rel_v, 4)
        event.risk = classify_risk(miss, rel_v)
    except RuntimeError:
        pass

    result = plan_maneuver(
        event=event,
        tle_primary=tle_p,
        tle_secondary=tle_s,
        target_miss_km=request.target_miss_km,
        lead_times_hours=request.lead_times_hours,
    )

    return {
        "primary": result.conjunction.primary,
        "secondary": result.conjunction.secondary,
        "current_miss_km": result.conjunction.miss_distance_km,
        "target_miss_km": result.target_miss_km,
        "warning": result.warning,
        "recommended": result.recommended.model_dump(mode="json") if result.recommended else None,
        "options": [o.model_dump(mode="json") for o in result.options],
    }


@router.post(
    "/montecarlo",
    tags=["Monte Carlo"],
    summary="Monte Carlo miss distance uncertainty analysis",
    response_description="Statistical distribution of miss distances from BSTAR perturbations",
)
def run_monte_carlo_analysis(request: MonteCarloRequest):
    """Perturb BSTAR drag coefficient and propagate N samples to TCA."""
    from sda.api import store

    tle_p = store.get(request.primary_norad_id)
    tle_s = store.get(request.secondary_norad_id)
    if tle_p is None or tle_s is None:
        raise HTTPException(status_code=404, detail="One or both satellites not found")

    result = run_monte_carlo(
        tle_primary=tle_p,
        tle_secondary=tle_s,
        tca=request.tca,
        n_samples=min(request.n_samples, 5000),
        bstar_sigma_fraction=request.bstar_sigma_fraction,
    )

    return {
        "primary": request.primary_norad_id,
        "secondary": request.secondary_norad_id,
        "tca": request.tca.isoformat(),
        "n_samples": result.n_samples,
        "statistics": {
            "mean_miss_km": result.mean_miss_km,
            "std_miss_km": result.std_miss_km,
            "median_miss_km": result.median_miss_km,
            "percentile_5_km": result.percentile_5_km,
            "percentile_95_km": result.percentile_95_km,
            "min_miss_km": result.min_miss_km,
            "max_miss_km": result.max_miss_km,
        },
        "collision_probability_mc": result.collision_probability_mc,
        "histogram_bins": _make_histogram(result.miss_distances, n_bins=30),
    }


def _make_histogram(values: list[float], n_bins: int = 30) -> list[dict]:
    """Create histogram bin data for visualization."""
    if not values:
        return []
    arr = np.array(values)
    counts, edges = np.histogram(arr, bins=n_bins)
    return [
        {"bin_start": round(float(edges[i]), 4),
         "bin_end": round(float(edges[i + 1]), 4),
         "count": int(counts[i])}
        for i in range(len(counts))
    ]


@router.get(
    "/satellites/{norad_id}/decay",
    tags=["Orbital Decay"],
    summary="Orbital decay and lifetime estimate",
    response_description="Estimated remaining lifetime, decay rate, and reentry risk",
)
def get_decay_estimate(
    norad_id: int,
    f107: float | None = Query(
        default=None,
        description="F10.7 solar flux index (SFU). Omit to use live NOAA data.",
    ),
):
    """Estimate orbital decay rate and remaining lifetime for a satellite."""
    from sda.api import _live_f107, store

    tle = store.get(norad_id)
    if tle is None:
        raise HTTPException(status_code=404, detail="Satellite not found")

    effective_f107 = f107 if f107 is not None else _live_f107
    result = estimate_decay(tle, f107_solar_flux=effective_f107)
    out = result.model_dump(mode="json")
    out["f107_source"] = "user" if f107 is not None else "live_noaa"
    out["f107_value"] = effective_f107
    return out
