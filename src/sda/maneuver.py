"""Collision avoidance maneuver planning.

Computes the minimum delta-V impulse to increase miss distance to a
safe threshold. Supports along-track, cross-track, and radial maneuvers.

Reference: Vallado, "Fundamentals of Astrodynamics and Applications," 4th Ed.
"""

from __future__ import annotations

import math
from datetime import timedelta

import numpy as np

from sda.constants import EARTH_MU_KM3_S2
from sda.models import ConjunctionEvent, ManeuverOption, ManeuverPlan, TLERecord
from sda.propagator import build_satrec, datetime_to_jd


def _compute_orbital_elements(pos_km: np.ndarray, vel_km_s: np.ndarray) -> dict:
    """Compute basic orbital elements from state vector."""
    r = np.linalg.norm(pos_km)
    v = np.linalg.norm(vel_km_s)
    h_vec = np.cross(pos_km, vel_km_s)
    h = np.linalg.norm(h_vec)

    if r < 1e-10 or h < 1e-10:
        raise ValueError("Degenerate orbit: near-zero radius or angular momentum")

    # Semi-major axis (vis-viva)
    energy = v**2 / 2 - EARTH_MU_KM3_S2 / r
    a = -EARTH_MU_KM3_S2 / (2 * energy) if abs(energy) > 1e-10 else r

    # Orbital period
    period_s = 2 * math.pi * math.sqrt(abs(a)**3 / EARTH_MU_KM3_S2)

    # Unit vectors: radial, along-track, cross-track (RSW frame)
    r_hat = pos_km / r
    w_hat = h_vec / h
    s_hat = np.cross(w_hat, r_hat)

    return {
        "a_km": a,
        "r_km": r,
        "v_km_s": v,
        "period_s": period_s,
        "r_hat": r_hat,
        "s_hat": s_hat,
        "w_hat": w_hat,
    }


def compute_along_track_delta_v(
    miss_distance_km: float,
    target_miss_km: float,
    lead_time_hours: float,
    orbital_period_s: float,
    semi_major_axis_km: float,
    radius_km: float | None = None,
) -> float:
    """Compute along-track delta-V to achieve target miss distance.

    An along-track impulse changes the semi-major axis, causing the
    satellite to drift ahead/behind its nominal position. The drift
    grows linearly with time (secular effect).

    delta_x ≈ (3/2) * (n * Δt) * (Δv / v) * a

    where n = mean motion, Δt = time to TCA, v = orbital velocity.
    Uses vis-viva equation for eccentric orbits when radius_km is provided.
    """
    deficit_km = target_miss_km - miss_distance_km
    if deficit_km <= 0:
        return 0.0

    n = 2 * math.pi / orbital_period_s  # mean motion (rad/s)
    dt = lead_time_hours * 3600.0  # seconds to TCA

    # Vis-viva velocity: v = sqrt(mu * (2/r - 1/a))
    r = radius_km if radius_km is not None else semi_major_axis_km
    v = math.sqrt(EARTH_MU_KM3_S2 * (2.0 / r - 1.0 / semi_major_axis_km))

    # delta_x = 1.5 * n * dt * (dv/v) * a → dv = delta_x * v / (1.5 * n * dt * a)
    denominator = 1.5 * n * dt * semi_major_axis_km
    if abs(denominator) < 1e-10:
        return float("inf")

    dv_km_s = deficit_km * v / denominator
    return abs(dv_km_s) * 1000.0  # convert to m/s


def plan_maneuver(
    event: ConjunctionEvent,
    tle_primary: TLERecord,
    tle_secondary: TLERecord,
    target_miss_km: float = 5.0,
    lead_times_hours: list[float] | None = None,
) -> ManeuverPlan:
    """Plan avoidance maneuvers for a conjunction event.

    Evaluates along-track, cross-track, and radial impulses at multiple
    lead times, recommending the minimum-fuel option.

    Parameters
    ----------
    event : the conjunction to avoid
    tle_primary : TLE of the maneuvering satellite (assumed primary)
    tle_secondary : TLE of the non-maneuvering satellite
    target_miss_km : desired minimum miss distance
    lead_times_hours : list of burn-to-TCA intervals to evaluate
    """
    if lead_times_hours is None:
        lead_times_hours = [2.0, 6.0, 12.0, 24.0]

    if event.miss_distance_km >= target_miss_km:
        return ManeuverPlan(
            conjunction=event,
            target_miss_km=target_miss_km,
            options=[],
            recommended=None,
            warning="Miss distance already exceeds target; no maneuver needed.",
        )

    # Get primary state at TCA
    satrec = build_satrec(tle_primary)
    jd, fr = datetime_to_jd(event.tca)
    err, pos, vel = satrec.sgp4(jd, fr)
    if err != 0:
        return ManeuverPlan(
            conjunction=event,
            target_miss_km=target_miss_km,
            options=[],
            recommended=None,
            warning="SGP4 propagation failed for primary object.",
        )

    pos_km = np.array(pos)
    vel_km_s = np.array(vel)
    orb = _compute_orbital_elements(pos_km, vel_km_s)

    options: list[ManeuverOption] = []

    for lead_hours in lead_times_hours:
        burn_time = event.tca - timedelta(hours=lead_hours)

        # Along-track maneuver (most fuel-efficient for LEO)
        dv_along = compute_along_track_delta_v(
            miss_distance_km=event.miss_distance_km,
            target_miss_km=target_miss_km,
            lead_time_hours=lead_hours,
            orbital_period_s=orb["period_s"],
            semi_major_axis_km=orb["a_km"],
            radius_km=orb["r_km"],
        )

        # Estimate new miss distance
        new_miss = target_miss_km if dv_along < 100.0 else event.miss_distance_km

        options.append(ManeuverOption(
            direction="along-track",
            delta_v_m_s=round(dv_along, 4),
            burn_time=burn_time,
            lead_time_hours=lead_hours,
            new_miss_distance_km=round(new_miss, 4),
        ))

        # Cross-track maneuver (less efficient, changes orbital plane)
        # Out-of-plane displacement after Δt from an impulse Δv_ct:
        # d ≈ (Δv_ct / n) * sin(n*Δt)  →  Δv_ct = deficit * n / sin(n*Δt)
        n = 2 * math.pi / orb["period_s"]
        dt = lead_hours * 3600.0
        sin_term = abs(math.sin(n * dt))
        if sin_term > 0.01:
            deficit_km = target_miss_km - event.miss_distance_km
            dv_cross = (deficit_km * n / sin_term) * 1000.0  # km/s -> m/s
        else:
            dv_cross = float("inf")

        if math.isfinite(dv_cross):
            options.append(ManeuverOption(
                direction="cross-track",
                delta_v_m_s=round(abs(dv_cross), 4),
                burn_time=burn_time,
                lead_time_hours=lead_hours,
                new_miss_distance_km=round(target_miss_km, 4),
            ))

        # Radial maneuver (least efficient in general)
        # Δv_r ≈ 2 * deficit / Δt (impulse approximation)
        dv_radial = (
            (2 * (target_miss_km - event.miss_distance_km) / dt) * 1000.0
            if dt > 0
            else float("inf")
        )
        if math.isfinite(dv_radial):
            options.append(ManeuverOption(
                direction="radial",
                delta_v_m_s=round(abs(dv_radial), 4),
                burn_time=burn_time,
                lead_time_hours=lead_hours,
                new_miss_distance_km=round(target_miss_km, 4),
            ))

    # Filter out unreasonable options (> 100 m/s is rarely practical)
    feasible = [o for o in options if o.delta_v_m_s < 100.0]
    if not feasible:
        feasible = options  # show all if none are feasible

    # Recommend minimum delta-V
    feasible.sort(key=lambda o: o.delta_v_m_s)
    recommended = feasible[0] if feasible else None

    return ManeuverPlan(
        conjunction=event,
        target_miss_km=target_miss_km,
        options=feasible,
        recommended=recommended,
    )
