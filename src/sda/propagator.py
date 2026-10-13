"""SGP4 orbital propagation engine."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
from sgp4.api import WGS72, Satrec, SatrecArray, jday

from sda.models import StateVector, TLERecord


def build_satrec(tle: TLERecord) -> Satrec:
    """Construct an SGP4 satellite record from TLE lines."""
    return Satrec.twoline2rv(tle.line1, tle.line2, WGS72)


def datetime_to_jd(dt: datetime) -> tuple[float, float]:
    """Convert a datetime to Julian date pair (jd, fr)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    jd, fr = jday(
        dt.year, dt.month, dt.day,
        dt.hour, dt.minute,
        dt.second + dt.microsecond / 1e6,
    )
    return jd, fr


def jd_to_datetime(jd: float, fr: float = 0.0) -> datetime:
    """Convert Julian date pair back to UTC datetime."""
    total_jd = jd + fr
    # J2000.0 = 2451545.0 = 2000-01-01T12:00:00Z
    j2000_epoch = datetime(2000, 1, 1, 12, 0, 0, tzinfo=UTC)
    delta_days = total_jd - 2451545.0
    return j2000_epoch + timedelta(days=delta_days)


def propagate_at(satrec: Satrec, dt: datetime) -> StateVector:
    """Propagate to a single datetime, returning a StateVector."""
    jd, fr = datetime_to_jd(dt)
    error, position, velocity = satrec.sgp4(jd, fr)
    if error != 0:
        raise RuntimeError(f"SGP4 propagation error code {error}")
    return StateVector(
        position_km=(position[0], position[1], position[2]),
        velocity_km_s=(velocity[0], velocity[1], velocity[2]),
        epoch=dt,
    )


def propagate_window(
    tle: TLERecord,
    start: datetime,
    hours: float = 24.0,
    step_seconds: float = 60.0,
) -> list[StateVector]:
    """Propagate a satellite over a time window, returning state vectors."""
    satrec = build_satrec(tle)
    n_steps = int((hours * 3600) / step_seconds) + 1
    results: list[StateVector] = []

    for i in range(n_steps):
        dt = start + timedelta(seconds=i * step_seconds)
        try:
            sv = propagate_at(satrec, dt)
            results.append(sv)
        except RuntimeError:
            continue

    return results


def propagate_window_numpy(
    tle: TLERecord,
    start: datetime,
    hours: float = 24.0,
    step_seconds: float = 60.0,
) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    """Vectorized propagation returning (positions, velocities, times) as numpy arrays.

    positions: shape (N, 3) in km
    velocities: shape (N, 3) in km/s
    times: list of datetimes
    """
    satrec = build_satrec(tle)
    n_steps = int((hours * 3600) / step_seconds) + 1

    times = [start + timedelta(seconds=i * step_seconds) for i in range(n_steps)]
    jds = np.zeros(n_steps)
    frs = np.zeros(n_steps)

    for i, dt in enumerate(times):
        jd, fr = datetime_to_jd(dt)
        jds[i] = jd
        frs[i] = fr

    # Vectorized SGP4 propagation
    errors, positions, velocities = satrec.sgp4_array(jds, frs)
    valid = errors == 0

    # times and valid both have n_steps entries by construction
    valid_times = [t for t, v in zip(times, valid, strict=True) if v]
    return positions[valid], velocities[valid], valid_times


def time_grid(
    start: datetime,
    hours: float = 24.0,
    step_seconds: float = 60.0,
) -> tuple[list[datetime], np.ndarray, np.ndarray]:
    """Shared coarse time grid: (times, jd array, fr array).

    Julian dates stay split into the (jd, fr) pair SGP4 expects; they are never
    combined into a single float.
    """
    n_steps = int((hours * 3600) / step_seconds) + 1
    times = [start + timedelta(seconds=i * step_seconds) for i in range(n_steps)]
    jds = np.zeros(n_steps)
    frs = np.zeros(n_steps)
    for i, dt in enumerate(times):
        jd, fr = datetime_to_jd(dt)
        jds[i] = jd
        frs[i] = fr
    return times, jds, frs


def propagate_catalog_numpy(
    tles: list[TLERecord],
    start: datetime,
    hours: float = 24.0,
    step_seconds: float = 60.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[datetime]]:
    """Propagate a whole catalog on one shared time grid, in one SGP4 call.

    Uses ``sgp4.api.SatrecArray`` so every object is propagated at C level over
    the same (jd, fr) grid. Unlike ``propagate_window_numpy`` the returned arrays
    are *aligned*: index ``t`` means the same instant for every object, and
    per-object per-step failures are reported through the validity mask instead
    of shortening the arrays.

    Returns (positions, velocities, valid, times) with
    positions/velocities shaped (n_objects, n_steps, 3) in km and km/s (ECI,
    TEME as produced by SGP4 under WGS72), valid shaped (n_objects, n_steps).
    """
    times, jds, frs = time_grid(start, hours, step_seconds)
    n_steps = len(times)
    if not tles:
        empty = np.zeros((0, n_steps, 3))
        return empty, empty.copy(), np.zeros((0, n_steps), dtype=bool), times

    satrecs = [build_satrec(tle) for tle in tles]
    errors, positions, velocities = SatrecArray(satrecs).sgp4(jds, frs)
    valid = (errors == 0) & np.all(np.isfinite(positions), axis=2)
    return positions, velocities, valid, times


def compute_distance(pos1: tuple[float, ...], pos2: tuple[float, ...]) -> float:
    """Euclidean distance in km between two position vectors."""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(pos1, pos2, strict=True)))


def compute_relative_velocity(vel1: tuple[float, ...], vel2: tuple[float, ...]) -> float:
    """Magnitude of velocity difference in km/s."""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(vel1, vel2, strict=True)))
