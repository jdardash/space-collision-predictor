"""Conjunction detection and risk classification pipeline."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta

import numpy as np

from sda.models import TLERecord, ConjunctionEvent, RiskLevel
from sda.propagator import (
    propagate_window_numpy,
    build_satrec,
    datetime_to_jd,
    compute_distance,
    compute_relative_velocity,
)
from sda.tle_store import TLEStore


def classify_risk(miss_distance_km: float, relative_velocity_km_s: float) -> RiskLevel:
    """Assign risk level based on miss distance and relative velocity."""
    if miss_distance_km < 0.5:
        return RiskLevel.CRITICAL
    if miss_distance_km < 1.0:
        return RiskLevel.HIGH
    if miss_distance_km < 5.0 and relative_velocity_km_s > 10.0:
        return RiskLevel.HIGH
    if miss_distance_km < 5.0:
        return RiskLevel.MODERATE
    if miss_distance_km < 10.0:
        return RiskLevel.LOW
    return RiskLevel.NEGLIGIBLE


def _refine_conjunction(
    tle_a: TLERecord,
    tle_b: TLERecord,
    coarse_tca: datetime,
    window_minutes: float = 5.0,
    step_seconds: float = 1.0,
) -> tuple[datetime, float, float]:
    """Refine the closest approach around a coarse TCA estimate.

    Returns (refined_tca, miss_distance_km, relative_velocity_km_s).
    """
    start = coarse_tca - timedelta(minutes=window_minutes)
    hours = (2 * window_minutes) / 60.0

    pos_a, vel_a, times_a = propagate_window_numpy(tle_a, start, hours, step_seconds)
    pos_b, vel_b, times_b = propagate_window_numpy(tle_b, start, hours, step_seconds)

    n = min(len(times_a), len(times_b))
    if n == 0:
        return coarse_tca, float("inf"), 0.0

    pos_a, vel_a = pos_a[:n], vel_a[:n]
    pos_b, vel_b = pos_b[:n], vel_b[:n]
    times = times_a[:n]

    diffs = pos_a - pos_b
    distances = np.linalg.norm(diffs, axis=1)
    min_idx = int(np.argmin(distances))

    tca = times[min_idx]
    miss_dist = float(distances[min_idx])

    vel_diff = vel_a[min_idx] - vel_b[min_idx]
    rel_vel = float(np.linalg.norm(vel_diff))

    return tca, miss_dist, rel_vel


def find_conjunctions(
    store: TLEStore,
    norad_ids: list[int] | None = None,
    hours: float = 24.0,
    threshold_km: float = 10.0,
    start: datetime | None = None,
) -> list[ConjunctionEvent]:
    """Run the full conjunction detection pipeline.

    Phase 1: Coarse screen at 60s steps for all satellite pairs.
    Phase 2: Fine refinement at 1s steps around candidate close approaches.
    """
    if start is None:
        start = datetime.now(timezone.utc)

    # Get satellites to analyze
    if norad_ids:
        tles = [store.get(nid) for nid in norad_ids]
        tles = [t for t in tles if t is not None]
    else:
        tles = store.get_all()

    if len(tles) < 2:
        return []

    # Phase 1: Coarse propagation
    coarse_step = 60.0
    propagated: dict[int, tuple[np.ndarray, np.ndarray, list[datetime]]] = {}

    for tle in tles:
        pos, vel, times = propagate_window_numpy(tle, start, hours, coarse_step)
        if len(times) > 0:
            propagated[tle.norad_id] = (pos, vel, times)

    # Screen all pairs
    coarse_margin = threshold_km * 10  # wider net for coarse screening
    candidates: list[tuple[TLERecord, TLERecord, datetime]] = []
    tle_map = {t.norad_id: t for t in tles}

    for id_a, id_b in itertools.combinations(propagated.keys(), 2):
        pos_a, _, times_a = propagated[id_a]
        pos_b, _, times_b = propagated[id_b]

        n = min(len(pos_a), len(pos_b))
        if n == 0:
            continue

        diffs = pos_a[:n] - pos_b[:n]
        distances = np.linalg.norm(diffs, axis=1)
        min_dist = float(np.min(distances))

        if min_dist < coarse_margin:
            min_idx = int(np.argmin(distances))
            coarse_tca = times_a[min_idx]
            candidates.append((tle_map[id_a], tle_map[id_b], coarse_tca))

    # Phase 2: Fine refinement
    events: list[ConjunctionEvent] = []
    for tle_a, tle_b, coarse_tca in candidates:
        tca, miss_dist, rel_vel = _refine_conjunction(tle_a, tle_b, coarse_tca)

        if miss_dist <= threshold_km:
            risk = classify_risk(miss_dist, rel_vel)
            events.append(ConjunctionEvent(
                primary=tle_a.norad_id,
                secondary=tle_b.norad_id,
                primary_name=tle_a.name,
                secondary_name=tle_b.name,
                tca=tca,
                miss_distance_km=round(miss_dist, 4),
                relative_velocity_km_s=round(rel_vel, 4),
                risk=risk,
            ))

    # Sort: CRITICAL first, then by miss distance
    risk_order = {
        RiskLevel.CRITICAL: 0,
        RiskLevel.HIGH: 1,
        RiskLevel.MODERATE: 2,
        RiskLevel.LOW: 3,
        RiskLevel.NEGLIGIBLE: 4,
    }
    events.sort(key=lambda e: (risk_order[e.risk], e.miss_distance_km))
    return events
