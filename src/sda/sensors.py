"""Surveillance sensor network: coverage, fences, pass ranking and DOP.

A sensor is an :class:`~sda.passes.Observer` with a field of regard: a minimum
elevation, an optional maximum elevation, a maximum range, and an optional
azimuth wedge. Everything geometric is delegated to :mod:`sda.passes`, so the
frame conventions are the same: ECI (TEME) states in km and km/s, rotated to
the Earth-fixed frame by GMST and projected into the site's topocentric
south-east-zenith triad.

The bundled site list is ILLUSTRATIVE. Coordinates were written from public
knowledge of where these well-known surveillance sites sit, rounded to about a
hundredth of a degree, and the field-of-regard numbers are plausible round
figures rather than published performance data. Nothing here comes from an
operational source, and nothing here should be used to plan real collection.
Replace the list with surveyed coordinates before using it for anything but a
demonstration.

Dilution of precision follows the standard GNSS geometry-matrix method: build
the line-of-sight design matrix in the local east-north-up triad, invert its
normal matrix, and read the DOP values off the diagonal.

References
----------
.. [1] B. Hofmann-Wellenhof et al., *GNSS: Global Navigation Satellite
       Systems*, Springer, 2008. Section 7.2 (dilution of precision).
.. [2] D. Vallado, *Fundamentals of Astrodynamics and Applications*, 4th Ed.,
       Microcosm Press, 2013. Section 4.4 (sensor observations).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import numpy as np
from pydantic import BaseModel, Field
from sgp4.api import Satrec

from sda.models import TLERecord
from sda.passes import (
    DEFAULT_COARSE_STEP_S,
    LookAngles,
    Observer,
    SatellitePass,
    _clamp01,
    _earth_fixed_state,
    _look_from_frame,
    _observer_frame,
    _ObserverFrame,
    gmst_rad,
    look_angles,
    predict_passes,
)
from sda.propagator import build_satrec, datetime_to_jd

BISECTION_TOLERANCE_S = 1.0
DEFAULT_FENCE_STEP_S = 60.0
MIN_DOP_SATELLITES = 4
# An optical site cannot work a pass it cannot see, so a pass that fails the
# sunlit-target plus dark-sky test keeps only a quarter of its score.
OPTICAL_UNOBSERVABLE_FACTOR = 0.25


class SensorType(StrEnum):
    """What the site measures with."""

    RADAR = "radar"
    OPTICAL = "optical"


class Sensor(BaseModel):
    """A ground sensor and its field of regard.

    The azimuth wedge runs clockwise from ``azimuth_min_deg`` to
    ``azimuth_max_deg``; a minimum greater than the maximum wraps through
    north. Leaving either bound unset means the site sweeps all azimuths.
    """

    name: str
    sensor_type: SensorType
    latitude_deg: float = Field(default=..., ge=-90.0, le=90.0)
    longitude_deg: float = Field(default=..., ge=-360.0, le=360.0)
    altitude_km: float = 0.0
    min_elevation_deg: float = 5.0
    max_elevation_deg: float = 90.0
    max_range_km: float = 5000.0
    azimuth_min_deg: float | None = None
    azimuth_max_deg: float | None = None
    country: str = ""
    note: str = ""

    def observer(self) -> Observer:
        """The site as a plain observer, for the pass and look-angle code."""
        return Observer(
            name=self.name,
            latitude_deg=self.latitude_deg,
            longitude_deg=self.longitude_deg,
            altitude_km=self.altitude_km,
        )

    def azimuth_in_fence(self, azimuth_deg: float) -> bool:
        """Is this azimuth inside the site's wedge?"""
        if self.azimuth_min_deg is None or self.azimuth_max_deg is None:
            return True
        azimuth = azimuth_deg % 360.0
        low = self.azimuth_min_deg % 360.0
        high = self.azimuth_max_deg % 360.0
        if low <= high:
            return low <= azimuth <= high
        return azimuth >= low or azimuth <= high

    def sees(self, look: LookAngles) -> bool:
        """Is an object at these look angles inside the field of regard?"""
        return (
            look.elevation_deg >= self.min_elevation_deg
            and look.elevation_deg <= self.max_elevation_deg
            and look.range_km <= self.max_range_km
            and self.azimuth_in_fence(look.azimuth_deg)
        )


# ---------------------------------------------------------------------------
# Bundled network
#
# ILLUSTRATIVE ONLY. Approximate coordinates written from public knowledge of
# well-known surveillance sites, with plausible round-figure field-of-regard
# limits. Not survey data, not published sensor performance.
# ---------------------------------------------------------------------------

SENSOR_NETWORK: tuple[Sensor, ...] = (
    Sensor(
        name="Eglin AN/FPS-85",
        sensor_type=SensorType.RADAR,
        latitude_deg=30.57,
        longitude_deg=-86.21,
        altitude_km=0.04,
        min_elevation_deg=3.0,
        max_range_km=22000.0,
        azimuth_min_deg=120.0,
        azimuth_max_deg=240.0,
        country="US",
        note="Phased array, southern sector, deep-space capable",
    ),
    Sensor(
        name="Cavalier PARCS",
        sensor_type=SensorType.RADAR,
        latitude_deg=48.72,
        longitude_deg=-97.90,
        altitude_km=0.35,
        min_elevation_deg=2.0,
        max_range_km=3300.0,
        azimuth_min_deg=298.0,
        azimuth_max_deg=78.0,
        country="US",
        note="Single-face array looking north, wedge wraps through north",
    ),
    Sensor(
        name="Fylingdales",
        sensor_type=SensorType.RADAR,
        latitude_deg=54.36,
        longitude_deg=-0.67,
        altitude_km=0.26,
        min_elevation_deg=3.0,
        max_range_km=4800.0,
        country="UK",
        note="Three-face array, full azimuth coverage",
    ),
    Sensor(
        name="Clear",
        sensor_type=SensorType.RADAR,
        latitude_deg=64.29,
        longitude_deg=-149.19,
        altitude_km=0.17,
        min_elevation_deg=3.0,
        max_range_km=4800.0,
        country="US",
        note="Upgraded early-warning radar, wide northern sector",
    ),
    Sensor(
        name="Cape Cod",
        sensor_type=SensorType.RADAR,
        latitude_deg=41.75,
        longitude_deg=-70.54,
        altitude_km=0.08,
        min_elevation_deg=3.0,
        max_range_km=5500.0,
        azimuth_min_deg=347.0,
        azimuth_max_deg=227.0,
        country="US",
        note="Two-face array, seaward wedge wraps through north",
    ),
    Sensor(
        name="Beale",
        sensor_type=SensorType.RADAR,
        latitude_deg=39.14,
        longitude_deg=-121.35,
        altitude_km=0.34,
        min_elevation_deg=3.0,
        max_range_km=5500.0,
        azimuth_min_deg=126.0,
        azimuth_max_deg=6.0,
        country="US",
        note="Two-face array, western wedge wraps through north",
    ),
    Sensor(
        name="Pituffik (Thule)",
        sensor_type=SensorType.RADAR,
        latitude_deg=76.57,
        longitude_deg=-68.30,
        altitude_km=0.08,
        min_elevation_deg=3.0,
        max_range_km=4800.0,
        country="Greenland (DK/US)",
        note="High-latitude early-warning radar",
    ),
    Sensor(
        name="ALTAIR Kwajalein",
        sensor_type=SensorType.RADAR,
        latitude_deg=9.40,
        longitude_deg=167.48,
        altitude_km=0.01,
        min_elevation_deg=1.0,
        max_range_km=40000.0,
        country="Marshall Islands (US)",
        note="Steerable dish on Roi-Namur, deep-space tracking",
    ),
    Sensor(
        name="Ascension",
        sensor_type=SensorType.RADAR,
        latitude_deg=-7.97,
        longitude_deg=-14.40,
        altitude_km=0.09,
        min_elevation_deg=3.0,
        max_range_km=5500.0,
        country="Ascension Island (UK/US)",
        note="Equatorial tracking radar, southern-hemisphere coverage",
    ),
    Sensor(
        name="GEODSS Socorro",
        sensor_type=SensorType.OPTICAL,
        latitude_deg=33.82,
        longitude_deg=-106.66,
        altitude_km=1.50,
        min_elevation_deg=20.0,
        max_range_km=45000.0,
        country="US",
        note="Optical telescopes, needs a dark sky and a sunlit target",
    ),
    Sensor(
        name="GEODSS Maui",
        sensor_type=SensorType.OPTICAL,
        latitude_deg=20.71,
        longitude_deg=-156.26,
        altitude_km=3.06,
        min_elevation_deg=20.0,
        max_range_km=45000.0,
        country="US",
        note="Haleakala summit site, needs a dark sky and a sunlit target",
    ),
    Sensor(
        name="GEODSS Diego Garcia",
        sensor_type=SensorType.OPTICAL,
        latitude_deg=-7.41,
        longitude_deg=72.45,
        altitude_km=0.01,
        min_elevation_deg=20.0,
        max_range_km=45000.0,
        country="BIOT (UK/US)",
        note="Indian Ocean optical site, closes the longitude gap",
    ),
    Sensor(
        name="Globus II Vardo",
        sensor_type=SensorType.RADAR,
        latitude_deg=70.37,
        longitude_deg=31.13,
        altitude_km=0.13,
        min_elevation_deg=3.0,
        max_range_km=40000.0,
        country="NO",
        note="Steerable dish, high-latitude tracking",
    ),
    Sensor(
        name="Amateur Munich",
        sensor_type=SensorType.RADAR,
        latitude_deg=48.15,
        longitude_deg=11.58,
        altitude_km=0.52,
        min_elevation_deg=10.0,
        max_range_km=3000.0,
        country="DE",
        note="Receive-only amateur RF station, typed as radar (no illumination need)",
    ),
    Sensor(
        name="Amateur Wellington",
        sensor_type=SensorType.RADAR,
        latitude_deg=-41.29,
        longitude_deg=174.78,
        altitude_km=0.03,
        min_elevation_deg=10.0,
        max_range_km=3000.0,
        country="NZ",
        note="Receive-only amateur RF station, southern-hemisphere fill-in",
    ),
)

SENSORS_BY_NAME: dict[str, Sensor] = {sensor.name.lower(): sensor for sensor in SENSOR_NETWORK}


def list_sensors(sensor_type: SensorType | None = None) -> list[Sensor]:
    """The bundled network, optionally filtered by sensor type."""
    if sensor_type is None:
        return list(SENSOR_NETWORK)
    return [sensor for sensor in SENSOR_NETWORK if sensor.sensor_type is sensor_type]


def get_sensor(name: str) -> Sensor | None:
    """Look a bundled sensor up by name, case-insensitively."""
    return SENSORS_BY_NAME.get(name.strip().lower())


def resolve_sensors(names: Sequence[str] | None) -> list[Sensor]:
    """Resolve a list of sensor names, or the whole network when empty."""
    if not names:
        return list(SENSOR_NETWORK)
    resolved: list[Sensor] = []
    for name in names:
        sensor = get_sensor(name)
        if sensor is not None:
            resolved.append(sensor)
    return resolved


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


class SensorLookAngles(BaseModel):
    """One sensor's view of one object at one instant."""

    sensor_name: str
    sensor_type: SensorType
    in_field_of_regard: bool
    look: LookAngles


def sensor_look_angles(
    position_eci_km: Sequence[float] | np.ndarray,
    velocity_eci_km_s: Sequence[float] | np.ndarray | None,
    when: datetime,
    sensors: Iterable[Sensor] | None = None,
) -> list[SensorLookAngles]:
    """Look angles from every sensor to one ECI state, in field of regard or not."""
    selected = list(SENSOR_NETWORK) if sensors is None else list(sensors)
    results: list[SensorLookAngles] = []
    for sensor in selected:
        look = look_angles(sensor.observer(), position_eci_km, velocity_eci_km_s, when)
        results.append(
            SensorLookAngles(
                sensor_name=sensor.name,
                sensor_type=sensor.sensor_type,
                in_field_of_regard=sensor.sees(look),
                look=look,
            )
        )
    return results


def sensors_covering(
    position_eci_km: Sequence[float] | np.ndarray,
    velocity_eci_km_s: Sequence[float] | np.ndarray | None,
    when: datetime,
    sensors: Iterable[Sensor] | None = None,
) -> list[SensorLookAngles]:
    """Only the sensors whose field of regard actually contains the object.

    Sorted by descending elevation, so the best-placed site comes first.
    """
    covering = [
        entry
        for entry in sensor_look_angles(position_eci_km, velocity_eci_km_s, when, sensors)
        if entry.in_field_of_regard
    ]
    covering.sort(key=lambda entry: entry.look.elevation_deg, reverse=True)
    return covering


def sensors_covering_tle(
    tle: TLERecord,
    when: datetime,
    sensors: Iterable[Sensor] | None = None,
) -> list[SensorLookAngles]:
    """Propagate a TLE to ``when`` and report which sensors can see it."""
    satrec = build_satrec(tle)
    jd, fr = datetime_to_jd(when)
    error, position, velocity = satrec.sgp4(jd, fr)
    if error != 0:
        raise RuntimeError(f"SGP4 propagation error code {error}")
    return sensors_covering(position, velocity, when, sensors)


# ---------------------------------------------------------------------------
# Fence crossings
# ---------------------------------------------------------------------------


class FenceCrossing(BaseModel):
    """The moment an object enters or leaves a sensor's field of regard."""

    sensor_name: str
    sensor_type: SensorType
    norad_id: int
    satellite_name: str
    event: str  # "enter" or "exit"
    time: datetime
    azimuth_deg: float
    elevation_deg: float
    range_km: float


def _visible_to_sensor(
    sensor: Sensor, satrec: Satrec, frame: _ObserverFrame, when: datetime
) -> bool:
    """Field-of-regard predicate evaluated straight off a satrec."""
    jd, fr = datetime_to_jd(when)
    error, position, velocity = satrec.sgp4(jd, fr)
    if error != 0:
        return False
    position_ef, velocity_ef = _earth_fixed_state(position, velocity, gmst_rad(when))
    azimuth, elevation, range_km, _ = _look_from_frame(frame, position_ef, velocity_ef)
    return (
        elevation >= sensor.min_elevation_deg
        and elevation <= sensor.max_elevation_deg
        and range_km <= sensor.max_range_km
        and sensor.azimuth_in_fence(azimuth)
    )


def _bisect_predicate(
    sensor: Sensor,
    satrec: Satrec,
    frame: _ObserverFrame,
    low: datetime,
    high: datetime,
    low_state: bool,
    tolerance_s: float = BISECTION_TOLERANCE_S,
) -> datetime:
    """Refine a field-of-regard state change bracketed by (low, high) to 1 s."""
    while (high - low).total_seconds() > tolerance_s:
        middle = low + (high - low) / 2
        if _visible_to_sensor(sensor, satrec, frame, middle) == low_state:
            low = middle
        else:
            high = middle
    return high


def fence_crossings(
    tle: TLERecord,
    start: datetime,
    hours: float = 24.0,
    sensors: Iterable[Sensor] | None = None,
    step_s: float = DEFAULT_FENCE_STEP_S,
) -> list[FenceCrossing]:
    """Every field-of-regard entry and exit in a window, refined to 1 s.

    An object already inside a fence at the window start produces an ``enter``
    at the start time; one still inside at the end produces no ``exit``.
    """
    if hours <= 0:
        return []
    if step_s <= 0:
        raise ValueError("step_s must be positive")
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)

    selected = list(SENSOR_NETWORK) if sensors is None else list(sensors)
    satrec = build_satrec(tle)
    end = start + timedelta(seconds=hours * 3600.0)
    crossings: list[FenceCrossing] = []

    for sensor in selected:
        frame = _observer_frame(sensor.observer())
        previous_time = start
        previous_state = _visible_to_sensor(sensor, satrec, frame, start)
        if previous_state:
            crossings.append(_crossing_at(sensor, tle, satrec, frame, start, "enter"))

        moment = start + timedelta(seconds=step_s)
        while moment <= end:
            state = _visible_to_sensor(sensor, satrec, frame, moment)
            if state != previous_state:
                crossing_time = _bisect_predicate(
                    sensor, satrec, frame, previous_time, moment, previous_state
                )
                crossings.append(
                    _crossing_at(
                        sensor,
                        tle,
                        satrec,
                        frame,
                        crossing_time,
                        "enter" if state else "exit",
                    )
                )
                previous_state = state
            previous_time = moment
            moment = moment + timedelta(seconds=step_s)

    crossings.sort(key=lambda crossing: crossing.time)
    return crossings


def _crossing_at(
    sensor: Sensor,
    tle: TLERecord,
    satrec: Satrec,
    frame: _ObserverFrame,
    when: datetime,
    event: str,
) -> FenceCrossing:
    """Build a crossing record with the look angles at the crossing instant."""
    jd, fr = datetime_to_jd(when)
    error, position, velocity = satrec.sgp4(jd, fr)
    if error != 0:
        raise RuntimeError(f"SGP4 propagation error code {error}")
    position_ef, velocity_ef = _earth_fixed_state(position, velocity, gmst_rad(when))
    azimuth, elevation, range_km, _ = _look_from_frame(frame, position_ef, velocity_ef)
    return FenceCrossing(
        sensor_name=sensor.name,
        sensor_type=sensor.sensor_type,
        norad_id=tle.norad_id,
        satellite_name=tle.name,
        event=event,
        time=when,
        azimuth_deg=round(azimuth, 3),
        elevation_deg=round(elevation, 3),
        range_km=round(range_km, 3),
    )


# ---------------------------------------------------------------------------
# Pass ranking
# ---------------------------------------------------------------------------


class RankedPass(BaseModel):
    """A pass over one sensor, with the score that ordered it."""

    sensor_name: str
    sensor_type: SensorType
    rank_score: float
    satellite_pass: SatellitePass


def pass_rank_score(sensor: Sensor, satellite_pass: SatellitePass) -> float:
    """Score a pass 0-1 for one sensor.

    An optical site needs a dark sky and a sunlit target, so it inherits the
    illumination-aware :func:`sda.passes.visibility_score` and then keeps only
    ``OPTICAL_UNOBSERVABLE_FACTOR`` of it when the pass is not observable, for
    example a bright target crossing a daylit sky. A radar does not care about
    lighting, so it is scored on geometry alone. Both then take 30 percent from
    track length, capped at ten minutes, because a longer track carries more
    observations.
    """
    duration_term = _clamp01(satellite_pass.duration_s / 600.0)
    if sensor.sensor_type is SensorType.OPTICAL:
        score = 0.7 * satellite_pass.visibility_score + 0.3 * duration_term
        if not satellite_pass.visible:
            score *= OPTICAL_UNOBSERVABLE_FACTOR
        return round(score, 4)
    geometry_term = _clamp01(satellite_pass.max_elevation_deg / 90.0)
    return round(0.7 * geometry_term + 0.3 * duration_term, 4)


def rank_passes(
    tle: TLERecord,
    start: datetime,
    hours: float = 24.0,
    sensors: Iterable[Sensor] | None = None,
    top_n: int = 3,
    coarse_step_s: float = DEFAULT_COARSE_STEP_S,
    include_samples: bool = False,
) -> list[RankedPass]:
    """Best passes of one object over each sensor, best first.

    Each sensor screens with its own minimum elevation. ``top_n`` limits the
    passes kept per sensor, not overall.
    """
    selected = list(SENSOR_NETWORK) if sensors is None else list(sensors)
    ranked: list[RankedPass] = []
    for sensor in selected:
        try:
            found = predict_passes(
                tle,
                sensor.observer(),
                start,
                hours=hours,
                min_elevation_deg=sensor.min_elevation_deg,
                coarse_step_s=coarse_step_s,
                include_samples=include_samples,
            )
        except (RuntimeError, ValueError):
            continue
        scored = [
            RankedPass(
                sensor_name=sensor.name,
                sensor_type=sensor.sensor_type,
                rank_score=pass_rank_score(sensor, item),
                satellite_pass=item,
            )
            for item in found
        ]
        scored.sort(key=lambda entry: entry.rank_score, reverse=True)
        ranked.extend(scored[: max(0, top_n)])
    ranked.sort(key=lambda entry: entry.rank_score, reverse=True)
    return ranked


def rank_passes_by_sensor(
    tle: TLERecord,
    start: datetime,
    hours: float = 24.0,
    sensors: Iterable[Sensor] | None = None,
    top_n: int = 3,
    coarse_step_s: float = DEFAULT_COARSE_STEP_S,
) -> dict[str, list[RankedPass]]:
    """Same ranking as :func:`rank_passes`, grouped by sensor name."""
    grouped: dict[str, list[RankedPass]] = {}
    for entry in rank_passes(tle, start, hours, sensors, top_n, coarse_step_s):
        grouped.setdefault(entry.sensor_name, []).append(entry)
    return grouped


# ---------------------------------------------------------------------------
# Dilution of precision
# ---------------------------------------------------------------------------


class DopValues(BaseModel):
    """Geometry quality for an observer given a set of tracked objects."""

    n_satellites: int
    gdop: float
    pdop: float
    hdop: float
    vdop: float
    tdop: float


def _line_of_sight_row(azimuth_deg: float, elevation_deg: float) -> list[float]:
    """One design-matrix row: the unit line of sight in east-north-up, plus clock."""
    azimuth = math.radians(azimuth_deg)
    elevation = math.radians(elevation_deg)
    east = math.cos(elevation) * math.sin(azimuth)
    north = math.cos(elevation) * math.cos(azimuth)
    up = math.sin(elevation)
    return [-east, -north, -up, 1.0]


def compute_dop(
    observer: Observer,
    positions_eci_km: Sequence[Sequence[float] | np.ndarray],
    when: datetime,
    min_elevation_deg: float = 0.0,
) -> DopValues:
    """Geometric, position, horizontal, vertical and time DOP for a site.

    Objects at or below ``min_elevation_deg`` are dropped before the geometry
    matrix is built. Fewer than four remaining objects, or a rank-deficient
    matrix, raises ``ValueError``: a DOP is undefined there, and returning a
    large number instead would hide the difference between bad geometry and no
    geometry at all.
    """
    frame = _observer_frame(observer)
    gmst = gmst_rad(when)

    rows: list[list[float]] = []
    for position in positions_eci_km:
        position_ef, _ = _earth_fixed_state(position, None, gmst)
        azimuth, elevation, _, _ = _look_from_frame(frame, position_ef, None)
        if elevation <= min_elevation_deg:
            continue
        rows.append(_line_of_sight_row(azimuth, elevation))

    if len(rows) < MIN_DOP_SATELLITES:
        raise ValueError(
            f"DOP needs at least {MIN_DOP_SATELLITES} objects above "
            f"{min_elevation_deg} deg elevation, got {len(rows)}"
        )

    design = np.array(rows, dtype=float)
    normal = design.T @ design
    try:
        cofactor = np.linalg.inv(normal)
    except np.linalg.LinAlgError as exc:
        raise ValueError("degenerate geometry: line-of-sight matrix is singular") from exc

    diagonal = np.diag(cofactor)
    if not np.all(np.isfinite(diagonal)) or np.any(diagonal < 0):
        raise ValueError("degenerate geometry: cofactor matrix is not positive")

    east_term, north_term, up_term, time_term = (float(value) for value in diagonal)
    return DopValues(
        n_satellites=len(rows),
        gdop=round(math.sqrt(east_term + north_term + up_term + time_term), 4),
        pdop=round(math.sqrt(east_term + north_term + up_term), 4),
        hdop=round(math.sqrt(east_term + north_term), 4),
        vdop=round(math.sqrt(up_term), 4),
        tdop=round(math.sqrt(time_term), 4),
    )


def compute_dop_for_tles(
    observer: Observer,
    tles: Iterable[TLERecord],
    when: datetime,
    min_elevation_deg: float = 0.0,
) -> DopValues:
    """DOP from a set of TLEs propagated to ``when``."""
    positions: list[np.ndarray] = []
    for tle in tles:
        satrec = build_satrec(tle)
        jd, fr = datetime_to_jd(when)
        error, position, _ = satrec.sgp4(jd, fr)
        if error != 0:
            continue
        positions.append(np.array(position, dtype=float))
    return compute_dop(observer, positions, when, min_elevation_deg)
