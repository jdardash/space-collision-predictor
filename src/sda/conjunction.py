"""Conjunction detection and risk classification pipeline.

Two phases, unchanged in shape since the first release: a coarse screen on a
60 s grid followed by a 1 s fine refinement around every candidate close
approach. What is new is *how* the coarse screen finds candidate pairs. The
"allpairs" strategy compares every surviving pair directly (blocked NumPy); the
"spatial" strategy builds a per-step spatial index (scipy cKDTree when scipy is
importable, otherwise a NumPy uniform-grid hash) so only near neighbours are
ever compared. Both are preceded by an apogee/perigee shell prefilter over the
TLE mean elements. All three produce the same candidate set; see
tests/test_screening.py for the equivalence lock.

Frames and units follow the project rule: ECI (TEME from SGP4) positions in km
and velocities in km/s, WGS72 gravity model, Julian dates as (jd, fr) pairs.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime, timedelta

import numpy as np

from sda.models import (
    CollisionProbability,
    ConjunctionEvent,
    RegimeSurveyResult,
    RiskLevel,
    TLERecord,
)
from sda.probability import compute_pc_for_conjunction, sigma_from_tle_age
from sda.propagator import (
    propagate_catalog_numpy,
    propagate_window_numpy,
)
from sda.screening import (
    SPATIAL_AUTO_MIN_OBJECTS,
    FormationRule,
    classify_regime,
    default_threshold_for_regime,
    formation_rules_with_extras,
    match_formation,
    pair_regime,
    query_pairs_within,
    risk_score,
    shell_bands,
    shell_prefilter_margin_km,
    shells_overlap,
)
from sda.tle_store import TLEStore

logger = logging.getLogger(__name__)

# Memory ceiling for one row block of the blocked all-pairs distance tensor.
_ALLPAIRS_BLOCK_BYTES = 64 * 1024 * 1024

# (index_a, index_b, min coarse distance km, coarse step index)
CoarseCandidate = tuple[int, int, float, int]


# Historical conjunction event storage (bounded deque)
_conjunction_history: deque[ConjunctionEvent] = deque(maxlen=1000)


def get_conjunction_history(
    norad_id: int | None = None,
    limit: int = 100,
) -> list[ConjunctionEvent]:
    """Retrieve historical conjunction events, optionally filtered by NORAD ID."""
    events = list(_conjunction_history)
    if norad_id is not None:
        events = [e for e in events if e.primary == norad_id or e.secondary == norad_id]
    events.sort(key=lambda e: e.tca, reverse=True)
    return events[:limit]


def clear_conjunction_history() -> int:
    """Clear all historical conjunction events. Returns count cleared."""
    count = len(_conjunction_history)
    _conjunction_history.clear()
    return count


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


def _tle_age_hours(tle: TLERecord, at: datetime) -> float:
    """Age of a TLE (hours) at a given time; clamps negative ages to zero."""
    epoch = tle.epoch if tle.epoch.tzinfo else tle.epoch.replace(tzinfo=UTC)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (at - epoch).total_seconds() / 3600.0)


def _refine_conjunction(
    tle_a: TLERecord,
    tle_b: TLERecord,
    coarse_tca: datetime,
    window_minutes: float = 5.0,
    step_seconds: float = 1.0,
) -> tuple[datetime, float, float, tuple, tuple, tuple, tuple]:
    """Refine the closest approach around a coarse TCA estimate.

    Returns (refined_tca, miss_distance_km, relative_velocity_km_s,
             pos_primary, vel_primary, pos_secondary, vel_secondary).
    """
    start = coarse_tca - timedelta(minutes=window_minutes)
    hours = (2 * window_minutes) / 60.0

    pos_a, vel_a, times_a = propagate_window_numpy(tle_a, start, hours, step_seconds)
    pos_b, vel_b, times_b = propagate_window_numpy(tle_b, start, hours, step_seconds)

    n = min(len(times_a), len(times_b), len(pos_a), len(pos_b))
    if n == 0:
        return coarse_tca, float("inf"), 0.0, (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)

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

    pos_p = tuple(pos_a[min_idx])
    vel_p = tuple(vel_a[min_idx])
    pos_s = tuple(pos_b[min_idx])
    vel_s = tuple(vel_b[min_idx])

    return tca, miss_dist, rel_vel, pos_p, vel_p, pos_s, vel_s


# --------------------------------------------------------------------------- #
# Phase 1: coarse screening strategies
# --------------------------------------------------------------------------- #


def resolve_screening_method(method: str, n_objects: int) -> str:
    """Turn the caller's ``method`` into a concrete strategy name.

    "auto" picks "spatial" once the catalog reaches SPATIAL_AUTO_MIN_OBJECTS
    objects (64), below which the blocked all-pairs tensor is faster than
    building an index at every coarse step.
    """
    if method == "auto":
        return "spatial" if n_objects >= SPATIAL_AUTO_MIN_OBJECTS else "allpairs"
    if method in {"allpairs", "spatial"}:
        return method
    raise ValueError(f"unknown screening method {method!r}")


def _allpairs_block_rows(n_objects: int, n_steps: int) -> int:
    """Rows per block so one difference tensor stays under the memory ceiling."""
    per_row = max(1, n_objects * n_steps * 3 * 8)
    return max(1, _ALLPAIRS_BLOCK_BYTES // per_row)


def _coarse_candidates_allpairs(
    positions: np.ndarray,
    valid: np.ndarray,
    perigee_km: np.ndarray,
    apogee_km: np.ndarray,
    coarse_margin_km: float,
    shell_margin_km: float,
    shell_prefilter: bool,
) -> list[CoarseCandidate]:
    """Every surviving pair compared directly, in row blocks of the distance tensor."""
    n_objects, n_steps = int(positions.shape[0]), int(positions.shape[1])
    if n_objects < 2 or n_steps == 0:
        return []

    out: list[CoarseCandidate] = []
    block = _allpairs_block_rows(n_objects, n_steps)
    for lo in range(0, n_objects, block):
        hi = min(lo + block, n_objects)
        diffs = positions[lo:hi, None, :, :] - positions[None, :, :, :]
        dist_sq = np.einsum("bntc,bntc->bnt", diffs, diffs)
        both_valid = valid[lo:hi, None, :] & valid[None, :, :]
        dist_sq = np.where(both_valid, dist_sq, np.inf)

        step_idx = np.argmin(dist_sq, axis=2)
        min_sq = np.take_along_axis(dist_sq, step_idx[:, :, None], axis=2)[:, :, 0]

        rows = np.arange(lo, hi)[:, None]
        cols = np.arange(n_objects)[None, :]
        keep = (cols > rows) & (min_sq < coarse_margin_km * coarse_margin_km)
        if shell_prefilter:
            keep &= np.asarray(
                shells_overlap(
                    perigee_km[lo:hi, None],
                    apogee_km[lo:hi, None],
                    perigee_km[None, :],
                    apogee_km[None, :],
                    shell_margin_km,
                )
            )

        bi, bj = np.nonzero(keep)
        for local_i, j in zip(bi.tolist(), bj.tolist(), strict=True):
            i = lo + local_i
            out.append((i, j, float(np.sqrt(min_sq[local_i, j])), int(step_idx[local_i, j])))
    return out


def _coarse_candidates_spatial(
    positions: np.ndarray,
    valid: np.ndarray,
    perigee_km: np.ndarray,
    apogee_km: np.ndarray,
    coarse_margin_km: float,
    shell_margin_km: float,
    shell_prefilter: bool,
    spatial_backend: str = "auto",
) -> list[CoarseCandidate]:
    """Per-step neighbour query instead of an all-pairs comparison.

    Adapted from the satguard vectorized screen (MIT). A pair whose global
    minimum separation is below ``coarse_margin_km`` is necessarily within the
    query radius at the step where that minimum occurs, so the running minimum
    kept here equals the all-pairs minimum for every pair either path reports.
    """
    n_objects, n_steps = int(positions.shape[0]), int(positions.shape[1])
    if n_objects < 2 or n_steps == 0:
        return []

    best: dict[tuple[int, int], tuple[float, int]] = {}
    for step in range(n_steps):
        live = np.nonzero(valid[:, step])[0]
        if live.size < 2:
            continue
        points = positions[live, step, :]
        pairs = query_pairs_within(points, coarse_margin_km, backend=spatial_backend)
        if pairs.shape[0] == 0:
            continue

        ia = live[pairs[:, 0]]
        ib = live[pairs[:, 1]]
        if shell_prefilter:
            overlap = np.asarray(
                shells_overlap(
                    perigee_km[ia], apogee_km[ia], perigee_km[ib], apogee_km[ib], shell_margin_km
                )
            )
            ia, ib = ia[overlap], ib[overlap]
            if ia.size == 0:
                continue

        deltas = positions[ia, step, :] - positions[ib, step, :]
        dists = np.sqrt(np.einsum("ij,ij->i", deltas, deltas))
        lo = np.minimum(ia, ib)
        hi = np.maximum(ia, ib)
        for a, b, d in zip(lo.tolist(), hi.tolist(), dists.tolist(), strict=True):
            prior = best.get((a, b))
            if prior is None or d < prior[0]:
                best[(a, b)] = (d, step)

    return [
        (a, b, dist, step)
        for (a, b), (dist, step) in best.items()
        if dist < coarse_margin_km
    ]


def screen_coarse(
    tles: list[TLERecord],
    start: datetime,
    hours: float = 24.0,
    coarse_margin_km: float = 100.0,
    method: str = "auto",
    shell_prefilter: bool = True,
    coarse_step_seconds: float = 60.0,
    spatial_backend: str = "auto",
) -> tuple[list[CoarseCandidate], list[datetime], str]:
    """Phase 1 only: candidate pairs on the coarse grid.

    Returns (candidates, times, resolved_method). Candidates carry indices into
    ``tles``, the minimum coarse separation in km, and the coarse step index of
    that minimum. Exposed separately so scripts/bench_screening.py can time the
    screen without paying for the 1 s refinement.
    """
    resolved = resolve_screening_method(method, len(tles))
    if len(tles) < 2:
        return [], [], resolved

    positions, _velocities, valid, times = propagate_catalog_numpy(
        tles, start, hours, coarse_step_seconds
    )
    if len(times) == 0:
        return [], [], resolved

    perigee_km, apogee_km = shell_bands(list(tles))
    shell_margin = shell_prefilter_margin_km(coarse_margin_km)

    if resolved == "spatial":
        candidates = _coarse_candidates_spatial(
            positions, valid, perigee_km, apogee_km,
            coarse_margin_km, shell_margin, shell_prefilter, spatial_backend,
        )
    else:
        candidates = _coarse_candidates_allpairs(
            positions, valid, perigee_km, apogee_km,
            coarse_margin_km, shell_margin, shell_prefilter,
        )
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates, times, resolved


# --------------------------------------------------------------------------- #
# Full pipeline
# --------------------------------------------------------------------------- #


def find_conjunctions(
    store: TLEStore,
    norad_ids: list[int] | None = None,
    hours: float = 24.0,
    threshold_km: float = 10.0,
    start: datetime | None = None,
    compute_probability: bool = True,
    method: str = "auto",
    shell_prefilter: bool = True,
    formation_rules: list[FormationRule] | tuple[FormationRule, ...] | None = None,
    spatial_backend: str = "auto",
) -> list[ConjunctionEvent]:
    """Run the full conjunction detection pipeline.

    Phase 1: Coarse screen at 60s steps, either all-pairs or spatially indexed.
    Phase 2: Fine refinement at 1s steps around candidate close approaches.

    ``method`` is "auto" (spatial above SPATIAL_AUTO_MIN_OBJECTS objects),
    "allpairs" or "spatial"; all three yield the same events. Events matching a
    known co-orbiting formation are flagged, never dropped.
    """
    if start is None:
        start = datetime.now(UTC)

    # Get satellites to analyze
    tles: list[TLERecord]
    if norad_ids:
        unique_ids = list(dict.fromkeys(norad_ids))
        tles = [t for nid in unique_ids if (t := store.get(nid)) is not None]
    else:
        tles = store.get_all()

    if len(tles) < 2:
        return []

    # Phase 1: coarse screen (60 s grid, wider net than the reporting threshold)
    coarse_margin = threshold_km * 10
    candidates, times, resolved_method = screen_coarse(
        tles,
        start,
        hours=hours,
        coarse_margin_km=coarse_margin,
        method=method,
        shell_prefilter=shell_prefilter,
        spatial_backend=spatial_backend,
    )

    rules = formation_rules_with_extras(formation_rules)
    regimes = [classify_regime(t) for t in tles]

    # Phase 2: Fine refinement
    events: list[ConjunctionEvent] = []
    for index_a, index_b, _coarse_dist, step in candidates:
        tle_a, tle_b = tles[index_a], tles[index_b]
        coarse_tca = times[step]
        tca, miss_dist, rel_vel, pos_p, vel_p, pos_s, vel_s = _refine_conjunction(
            tle_a, tle_b, coarse_tca
        )

        if miss_dist <= threshold_km:
            risk = classify_risk(miss_dist, rel_vel)

            # Compute collision probability with TLE-age-scaled covariance:
            # stale TLEs get a larger position sigma (Vallado 2013, Ch. 9)
            pc_result = None
            if compute_probability:
                try:
                    pc_data = compute_pc_for_conjunction(
                        pos_primary_km=pos_p,
                        vel_primary_km_s=vel_p,
                        pos_secondary_km=pos_s,
                        vel_secondary_km_s=vel_s,
                        sigma_primary_km=sigma_from_tle_age(_tle_age_hours(tle_a, tca)),
                        sigma_secondary_km=sigma_from_tle_age(_tle_age_hours(tle_b, tca)),
                    )
                    pc_result = CollisionProbability(**pc_data)
                except Exception as exc:
                    logger.warning(
                        "Pc computation failed for %d/%d: %s",
                        tle_a.norad_id, tle_b.norad_id, exc,
                    )

            formation = match_formation(
                tle_a.norad_id, tle_a.name, tle_a.line1,
                tle_b.norad_id, tle_b.name, tle_b.line1,
                rules=rules,
            )

            event = ConjunctionEvent(
                primary=tle_a.norad_id,
                secondary=tle_b.norad_id,
                primary_name=tle_a.name,
                secondary_name=tle_b.name,
                tca=tca,
                miss_distance_km=round(miss_dist, 4),
                relative_velocity_km_s=round(rel_vel, 4),
                risk=risk,
                collision_probability=pc_result,
                formation_excluded=formation is not None,
                formation_label=None if formation is None else formation.label,
                screening_method=resolved_method,
                regime=pair_regime(regimes[index_a], regimes[index_b]),
            )
            event.risk_score = risk_score(event, now=start)
            events.append(event)
            _conjunction_history.append(event)

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


def screen_regime(
    store: TLEStore,
    regime: str,
    hours: float = 24.0,
    threshold_km: float | None = None,
    start: datetime | None = None,
    compute_probability: bool = True,
    method: str = "auto",
    include_formations: bool = True,
    limit: int | None = None,
    formation_rules: list[FormationRule] | tuple[FormationRule, ...] | None = None,
) -> RegimeSurveyResult:
    """All-vs-all survey restricted to one orbit regime, ranked by miss distance.

    Design follows orbveil's regime survey (Apache-2.0), implemented here from
    the TLE mean elements. The coarse threshold defaults per regime (GEO is the
    widest) because station-keeping boxes and along-track uncertainty grow with
    altitude. Formation pairs are flagged in the events; ``include_formations``
    controls whether they are listed at all.
    """
    label = regime.upper()
    effective_threshold = (
        default_threshold_for_regime(label) if threshold_km is None else float(threshold_km)
    )
    members = [t for t in store.get_all() if classify_regime(t) == label]

    events: list[ConjunctionEvent] = []
    resolved = resolve_screening_method(method, len(members))
    if len(members) >= 2:
        events = find_conjunctions(
            store,
            norad_ids=[t.norad_id for t in members],
            hours=hours,
            threshold_km=effective_threshold,
            start=start,
            compute_probability=compute_probability,
            method=method,
            formation_rules=formation_rules,
        )
    if not include_formations:
        events = [e for e in events if not e.formation_excluded]
    events.sort(key=lambda e: e.miss_distance_km)
    if limit is not None:
        events = events[:limit]

    for event in events:
        event.regime = label

    return RegimeSurveyResult(
        regime=label,
        threshold_km=effective_threshold,
        hours=hours,
        objects_screened=len(members),
        screening_method=resolved,
        events=events,
    )
