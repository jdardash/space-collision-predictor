"""Ground-station pass prediction and topocentric look angles.

Frames and units
----------------
Satellite states arrive as ECI (TEME) position and velocity in km and km/s,
exactly as SGP4 produces them. Look angles need an Earth-fixed frame, so this
module rotates the state about the pole by Greenwich Mean Sidereal Time and
then projects the relative vector onto the observer's topocentric
south-east-zenith triad. Angles are degrees, distances km, rates km/s,
durations seconds. Every datetime is timezone-aware UTC.

The observer sits on the WGS72 reference ellipsoid, the same gravity model
SGP4 uses. ``sda.constants`` carries the WGS72 radius but not the flattening,
so the flattening is defined here.

Adapted from satvis (MIT) for the 0-1 pass quality score and the 16-point
compass mapping, and from satlas (MIT) for the rise / culminate / set pass
structure. Both were rewritten against this repo's SGP4 propagator, so no
Cesium or Skyfield dependency is introduced.

Known duplication: the GMST rotation below repeats the private helper
``sda.routes.worldview._teme_to_geographic``. That helper belongs to another
work package and exposes no public API, so the same Vallado formulation is
reimplemented here rather than imported.

References
----------
.. [1] D. Vallado, *Fundamentals of Astrodynamics and Applications*, 4th Ed.,
       Microcosm Press, 2013. Algorithm 29 (low-precision solar position),
       Algorithm 27 (topocentric look angles), Section 3.5 (sidereal time).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from pydantic import BaseModel, Field
from sgp4.api import Satrec

from sda.constants import EARTH_RADIUS_KM
from sda.models import TLERecord
from sda.propagator import build_satrec, datetime_to_jd

# WGS72 ellipsoid flattening. Not in sda.constants; the value is the WGS72
# defining constant 1/298.26 (SGP4 uses the same reference ellipsoid).
EARTH_FLATTENING_WGS72 = 1.0 / 298.26

# Earth rotation rate (rad/s), needed to turn an ECI velocity into the
# Earth-fixed velocity a ground observer actually measures.
EARTH_ROTATION_RAD_S = 7.292115e-5

ASTRONOMICAL_UNIT_KM = 149597870.7

# Civil twilight: the sun this far below the observer's horizon is dark enough
# for a sunlit satellite to be seen by eye or by an optical sensor.
CIVIL_TWILIGHT_DEG = -6.0

DEFAULT_MIN_ELEVATION_DEG = 10.0
DEFAULT_COARSE_STEP_S = 30.0
DEFAULT_SAMPLE_STEP_S = 30.0
BISECTION_TOLERANCE_S = 1.0

_COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def compass_point(azimuth_deg: float) -> str:
    """Map an azimuth to a 16-point compass abbreviation.

    Adapted from satvis (MIT), ``PassPredictor.compassPoint``.
    """
    index = int(round(((azimuth_deg % 360.0) + 360.0) % 360.0 / 22.5)) % 16
    return _COMPASS_POINTS[index]


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class Observer(BaseModel):
    """A fixed site on the Earth's surface, in geodetic coordinates."""

    name: str = "observer"
    latitude_deg: float = Field(default=..., ge=-90.0, le=90.0)
    longitude_deg: float = Field(default=..., ge=-360.0, le=360.0)
    altitude_km: float = Field(default=0.0, ge=-0.5, le=100.0)

    def earth_fixed_position_km(self) -> np.ndarray:
        """Geodetic latitude, longitude and height to an Earth-fixed vector.

        WGS72 ellipsoid, radius from ``sda.constants.EARTH_RADIUS_KM``.
        """
        lat = math.radians(self.latitude_deg)
        lon = math.radians(self.longitude_deg)
        e2 = 2.0 * EARTH_FLATTENING_WGS72 - EARTH_FLATTENING_WGS72 ** 2
        sin_lat = math.sin(lat)
        prime_vertical = EARTH_RADIUS_KM / math.sqrt(1.0 - e2 * sin_lat ** 2)
        horizontal = (prime_vertical + self.altitude_km) * math.cos(lat)
        return np.array([
            horizontal * math.cos(lon),
            horizontal * math.sin(lon),
            (prime_vertical * (1.0 - e2) + self.altitude_km) * sin_lat,
        ])


@dataclass(frozen=True)
class _ObserverFrame:
    """Precomputed observer geometry, so a pass search rebuilds nothing."""

    position_km: np.ndarray
    sin_lat: float
    cos_lat: float
    sin_lon: float
    cos_lon: float


def _observer_frame(observer: Observer) -> _ObserverFrame:
    lat = math.radians(observer.latitude_deg)
    lon = math.radians(observer.longitude_deg)
    return _ObserverFrame(
        position_km=observer.earth_fixed_position_km(),
        sin_lat=math.sin(lat),
        cos_lat=math.cos(lat),
        sin_lon=math.sin(lon),
        cos_lon=math.cos(lon),
    )


# ---------------------------------------------------------------------------
# Frame rotation
# ---------------------------------------------------------------------------


def gmst_rad(when: datetime) -> float:
    """Greenwich Mean Sidereal Time in radians (Vallado, IAU-82 series)."""
    jd, fr = datetime_to_jd(when)
    t_ut1 = ((jd - 2451545.0) + fr) / 36525.0
    gmst_sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * t_ut1
        + 0.093104 * t_ut1 ** 2
        - 6.2e-6 * t_ut1 ** 3
    )
    return math.fmod(gmst_sec * (2.0 * math.pi / 86400.0), 2.0 * math.pi)


def eci_to_earth_fixed(vector_km: Sequence[float] | np.ndarray, gmst: float) -> np.ndarray:
    """Rotate an ECI (TEME) vector about the pole into the Earth-fixed frame."""
    cos_g = math.cos(gmst)
    sin_g = math.sin(gmst)
    x, y, z = float(vector_km[0]), float(vector_km[1]), float(vector_km[2])
    return np.array([x * cos_g + y * sin_g, -x * sin_g + y * cos_g, z])


def earth_fixed_to_eci(vector_km: Sequence[float] | np.ndarray, gmst: float) -> np.ndarray:
    """Inverse of :func:`eci_to_earth_fixed`."""
    cos_g = math.cos(gmst)
    sin_g = math.sin(gmst)
    x, y, z = float(vector_km[0]), float(vector_km[1]), float(vector_km[2])
    return np.array([x * cos_g - y * sin_g, x * sin_g + y * cos_g, z])


# ---------------------------------------------------------------------------
# Look angles
# ---------------------------------------------------------------------------


class LookAngles(BaseModel):
    """Topocentric look angles from one observer to one object."""

    time: datetime
    azimuth_deg: float
    elevation_deg: float
    range_km: float
    range_rate_km_s: float
    compass: str


def _look_from_frame(
    frame: _ObserverFrame,
    position_ef_km: np.ndarray,
    velocity_ef_km_s: np.ndarray | None,
) -> tuple[float, float, float, float]:
    """Return (azimuth_deg, elevation_deg, range_km, range_rate_km_s)."""
    rel = position_ef_km - frame.position_km
    range_km = float(np.linalg.norm(rel))
    if range_km <= 0.0:
        return 0.0, 90.0, 0.0, 0.0

    south = (
        frame.sin_lat * frame.cos_lon * rel[0]
        + frame.sin_lat * frame.sin_lon * rel[1]
        - frame.cos_lat * rel[2]
    )
    east = -frame.sin_lon * rel[0] + frame.cos_lon * rel[1]
    zenith = (
        frame.cos_lat * frame.cos_lon * rel[0]
        + frame.cos_lat * frame.sin_lon * rel[1]
        + frame.sin_lat * rel[2]
    )

    elevation_deg = math.degrees(math.asin(max(-1.0, min(1.0, zenith / range_km))))
    azimuth_deg = math.degrees(math.atan2(east, -south)) % 360.0

    range_rate = 0.0
    if velocity_ef_km_s is not None:
        range_rate = float(np.dot(rel, velocity_ef_km_s) / range_km)

    return azimuth_deg, elevation_deg, range_km, range_rate


def _earth_fixed_state(
    position_eci_km: Sequence[float] | np.ndarray,
    velocity_eci_km_s: Sequence[float] | np.ndarray | None,
    gmst: float,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Rotate an ECI state into the Earth-fixed frame.

    The velocity picks up the transport term ``-omega x r`` so that range rate
    is measured relative to a site that rotates with the Earth.
    """
    position_ef = eci_to_earth_fixed(position_eci_km, gmst)
    if velocity_eci_km_s is None:
        return position_ef, None
    velocity_ef = eci_to_earth_fixed(velocity_eci_km_s, gmst)
    velocity_ef[0] += EARTH_ROTATION_RAD_S * position_ef[1]
    velocity_ef[1] -= EARTH_ROTATION_RAD_S * position_ef[0]
    return position_ef, velocity_ef


def look_angles(
    observer: Observer,
    position_eci_km: Sequence[float] | np.ndarray,
    velocity_eci_km_s: Sequence[float] | np.ndarray | None,
    when: datetime,
) -> LookAngles:
    """Look angles from ``observer`` to an ECI state at ``when``."""
    gmst = gmst_rad(when)
    position_ef, velocity_ef = _earth_fixed_state(position_eci_km, velocity_eci_km_s, gmst)
    azimuth, elevation, range_km, range_rate = _look_from_frame(
        _observer_frame(observer), position_ef, velocity_ef
    )
    return LookAngles(
        time=when,
        azimuth_deg=round(azimuth, 4),
        elevation_deg=round(elevation, 4),
        range_km=round(range_km, 4),
        range_rate_km_s=round(range_rate, 6),
        compass=compass_point(azimuth),
    )


def look_angles_for_tle(tle: TLERecord, observer: Observer, when: datetime) -> LookAngles:
    """Propagate a TLE to ``when`` and return the observer's look angles."""
    satrec = build_satrec(tle)
    jd, fr = datetime_to_jd(when)
    error, position, velocity = satrec.sgp4(jd, fr)
    if error != 0:
        raise RuntimeError(f"SGP4 propagation error code {error}")
    return look_angles(observer, position, velocity, when)


def topocentric_to_eci(
    observer: Observer,
    azimuth_deg: float,
    elevation_deg: float,
    range_km: float,
    when: datetime,
) -> np.ndarray:
    """Inverse look angles: place a point at (az, el, range) from the observer.

    Returns an ECI (TEME) position in km. Used to build synthetic geometries
    without a TLE, and as the round-trip check for :func:`look_angles`.
    """
    frame = _observer_frame(observer)
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    south = -range_km * math.cos(el) * math.cos(az)
    east = range_km * math.cos(el) * math.sin(az)
    zenith = range_km * math.sin(el)

    rel = np.array([
        south * frame.sin_lat * frame.cos_lon
        - east * frame.sin_lon
        + zenith * frame.cos_lat * frame.cos_lon,
        south * frame.sin_lat * frame.sin_lon
        + east * frame.cos_lon
        + zenith * frame.cos_lat * frame.sin_lon,
        -south * frame.cos_lat + zenith * frame.sin_lat,
    ])
    return earth_fixed_to_eci(frame.position_km + rel, gmst_rad(when))


# ---------------------------------------------------------------------------
# Solar geometry and visibility
# ---------------------------------------------------------------------------


def solar_position_eci_km(when: datetime) -> np.ndarray:
    """Low-precision solar position in km (Vallado algorithm 29).

    Accurate to roughly 0.01 deg in right ascension and declination over
    1950-2050, far tighter than a visibility decision needs. The result is
    referred to the mean equator and equinox of date and is treated as the
    same ECI frame SGP4 produces; the difference is well inside the accuracy
    of this model.
    """
    jd, fr = datetime_to_jd(when)
    t_ut1 = ((jd - 2451545.0) + fr) / 36525.0

    mean_longitude_deg = (280.460 + 36000.771 * t_ut1) % 360.0
    mean_anomaly_rad = math.radians((357.5291092 + 35999.05034 * t_ut1) % 360.0)

    ecliptic_longitude_rad = math.radians(
        mean_longitude_deg
        + 1.914666471 * math.sin(mean_anomaly_rad)
        + 0.019994643 * math.sin(2.0 * mean_anomaly_rad)
    )
    distance_au = (
        1.000140612
        - 0.016708617 * math.cos(mean_anomaly_rad)
        - 0.000139589 * math.cos(2.0 * mean_anomaly_rad)
    )
    obliquity_rad = math.radians(23.439291 - 0.0130042 * t_ut1)

    radius_km = distance_au * ASTRONOMICAL_UNIT_KM
    return np.array([
        radius_km * math.cos(ecliptic_longitude_rad),
        radius_km * math.cos(obliquity_rad) * math.sin(ecliptic_longitude_rad),
        radius_km * math.sin(obliquity_rad) * math.sin(ecliptic_longitude_rad),
    ])


def solar_ra_dec_deg(when: datetime) -> tuple[float, float]:
    """Right ascension and declination of the sun, degrees."""
    sun = solar_position_eci_km(when)
    radius = float(np.linalg.norm(sun))
    right_ascension = math.degrees(math.atan2(float(sun[1]), float(sun[0]))) % 360.0
    declination = math.degrees(math.asin(float(sun[2]) / radius))
    return right_ascension, declination


def is_sunlit(
    position_eci_km: Sequence[float] | np.ndarray,
    sun_position_eci_km: Sequence[float] | np.ndarray,
) -> bool:
    """Cylindrical shadow test: is the satellite outside the Earth's umbra?

    The Earth's shadow is modelled as an infinite cylinder of radius
    ``EARTH_RADIUS_KM`` pointing away from the sun. This ignores the penumbra
    and the sun's finite angular size (a conical model moves the terminator by
    tens of seconds of pass time) and it ignores atmospheric refraction. It is
    the standard first-order test and is deliberately cheap.
    """
    satellite = np.asarray(position_eci_km, dtype=float)
    sun = np.asarray(sun_position_eci_km, dtype=float)
    sun_hat = sun / np.linalg.norm(sun)
    along_sun = float(np.dot(satellite, sun_hat))
    if along_sun >= 0.0:
        # Sunward hemisphere: never in shadow.
        return True
    perpendicular = float(np.linalg.norm(satellite - along_sun * sun_hat))
    return perpendicular > EARTH_RADIUS_KM


def sun_elevation_deg(observer: Observer, when: datetime) -> float:
    """Elevation of the sun above the observer's horizon, degrees."""
    sun = solar_position_eci_km(when)
    sun_ef, _ = _earth_fixed_state(sun, None, gmst_rad(when))
    _, elevation, _, _ = _look_from_frame(_observer_frame(observer), sun_ef, None)
    return elevation


def is_dark(observer: Observer, when: datetime) -> bool:
    """True when the sun is below civil twilight at the observer."""
    return sun_elevation_deg(observer, when) < CIVIL_TWILIGHT_DEG


def visibility_score(
    max_elevation_deg: float,
    sunlit: bool,
    sun_elevation_at_max_deg: float,
) -> float:
    """Score a pass 0-1 from geometry, illumination and sky darkness.

    The elevation term is satvis's ``passQuality`` (MIT): max elevation over
    90 deg. The darkness term ramps linearly from 0 at sunset to 1 at the end
    of nautical twilight (-12 deg). Weights are 0.5 geometry, 0.3 illumination,
    0.2 darkness, so a high radar-friendly pass in daylight still scores 0.5.
    """
    elevation_term = _clamp01(max_elevation_deg / 90.0)
    illumination_term = 1.0 if sunlit else 0.0
    darkness_term = _clamp01(-sun_elevation_at_max_deg / 12.0)
    return round(0.5 * elevation_term + 0.3 * illumination_term + 0.2 * darkness_term, 4)


# ---------------------------------------------------------------------------
# Pass models
# ---------------------------------------------------------------------------


class PassSample(BaseModel):
    """One point on the observed track of a pass."""

    time: datetime
    azimuth_deg: float
    elevation_deg: float
    range_km: float
    range_rate_km_s: float


class SatellitePass(BaseModel):
    """A single above-threshold pass of one object over one observer."""

    norad_id: int
    satellite_name: str
    observer_name: str
    min_elevation_deg: float

    rise_time: datetime
    rise_azimuth_deg: float
    rise_compass: str

    culmination_time: datetime
    max_elevation_deg: float
    culmination_azimuth_deg: float
    culmination_compass: str
    culmination_range_km: float

    set_time: datetime
    set_azimuth_deg: float
    set_compass: str

    duration_s: float
    partial: bool = False

    sunlit: bool = False
    sun_elevation_deg: float = 0.0
    visible: bool = False
    visibility_score: float = 0.0

    samples: list[PassSample] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pass search
# ---------------------------------------------------------------------------


def _elevation_at(satrec: Satrec, frame: _ObserverFrame, when: datetime) -> float | None:
    """Elevation in degrees, or None if SGP4 could not propagate."""
    jd, fr = datetime_to_jd(when)
    error, position, _ = satrec.sgp4(jd, fr)
    if error != 0:
        return None
    position_ef, _ = _earth_fixed_state(position, None, gmst_rad(when))
    return _look_from_frame(frame, position_ef, None)[1]


def _full_look_at(
    satrec: Satrec, frame: _ObserverFrame, when: datetime
) -> tuple[float, float, float, float]:
    """Full look angles from a satrec at one instant."""
    jd, fr = datetime_to_jd(when)
    error, position, velocity = satrec.sgp4(jd, fr)
    if error != 0:
        raise RuntimeError(f"SGP4 propagation error code {error}")
    position_ef, velocity_ef = _earth_fixed_state(position, velocity, gmst_rad(when))
    return _look_from_frame(frame, position_ef, velocity_ef)


def _bisect_crossing(
    satrec: Satrec,
    frame: _ObserverFrame,
    threshold_deg: float,
    low: datetime,
    high: datetime,
    tolerance_s: float = BISECTION_TOLERANCE_S,
) -> datetime:
    """Refine an elevation-threshold crossing bracketed by (low, high) to 1 s."""
    low_elevation = _elevation_at(satrec, frame, low)
    if low_elevation is None:
        return high
    low_above = low_elevation >= threshold_deg
    while (high - low).total_seconds() > tolerance_s:
        middle = low + (high - low) / 2
        elevation = _elevation_at(satrec, frame, middle)
        if elevation is None:
            return middle
        if (elevation >= threshold_deg) == low_above:
            low = middle
        else:
            high = middle
    return high if low_above else low


def _ternary_max(
    satrec: Satrec,
    frame: _ObserverFrame,
    low: datetime,
    high: datetime,
    tolerance_s: float = BISECTION_TOLERANCE_S,
) -> datetime:
    """Locate the elevation maximum of a pass; elevation is unimodal on it."""
    while (high - low).total_seconds() > tolerance_s:
        third = (high - low) / 3
        left = low + third
        right = high - third
        left_elevation = _elevation_at(satrec, frame, left)
        right_elevation = _elevation_at(satrec, frame, right)
        if left_elevation is None or right_elevation is None:
            break
        if left_elevation < right_elevation:
            low = left
        else:
            high = right
    return low + (high - low) / 2


def _build_pass(
    satrec: Satrec,
    tle: TLERecord,
    observer: Observer,
    frame: _ObserverFrame,
    rise: datetime,
    set_time: datetime,
    min_elevation_deg: float,
    sample_step_s: float,
    include_samples: bool,
    partial: bool,
) -> SatellitePass:
    """Assemble one pass record between a rise and a set time."""
    culmination = _ternary_max(satrec, frame, rise, set_time)

    rise_look = _full_look_at(satrec, frame, rise)
    peak_look = _full_look_at(satrec, frame, culmination)
    set_look = _full_look_at(satrec, frame, set_time)

    jd, fr = datetime_to_jd(culmination)
    error, position, _ = satrec.sgp4(jd, fr)
    sun = solar_position_eci_km(culmination)
    sunlit = bool(error == 0 and is_sunlit(position, sun))
    sun_elevation = sun_elevation_deg(observer, culmination)

    samples: list[PassSample] = []
    if include_samples and sample_step_s > 0:
        total_s = (set_time - rise).total_seconds()
        count = int(total_s // sample_step_s)
        times = [rise + timedelta(seconds=i * sample_step_s) for i in range(count + 1)]
        if times[-1] < set_time:
            times.append(set_time)
        for moment in times:
            try:
                azimuth, elevation, range_km, range_rate = _full_look_at(satrec, frame, moment)
            except RuntimeError:
                continue
            samples.append(
                PassSample(
                    time=moment,
                    azimuth_deg=round(azimuth, 3),
                    elevation_deg=round(elevation, 3),
                    range_km=round(range_km, 3),
                    range_rate_km_s=round(range_rate, 5),
                )
            )

    return SatellitePass(
        norad_id=tle.norad_id,
        satellite_name=tle.name,
        observer_name=observer.name,
        min_elevation_deg=min_elevation_deg,
        rise_time=rise,
        rise_azimuth_deg=round(rise_look[0], 3),
        rise_compass=compass_point(rise_look[0]),
        culmination_time=culmination,
        max_elevation_deg=round(peak_look[1], 3),
        culmination_azimuth_deg=round(peak_look[0], 3),
        culmination_compass=compass_point(peak_look[0]),
        culmination_range_km=round(peak_look[2], 3),
        set_time=set_time,
        set_azimuth_deg=round(set_look[0], 3),
        set_compass=compass_point(set_look[0]),
        duration_s=round((set_time - rise).total_seconds(), 1),
        partial=partial,
        sunlit=sunlit,
        sun_elevation_deg=round(sun_elevation, 3),
        visible=bool(sunlit and sun_elevation < CIVIL_TWILIGHT_DEG),
        visibility_score=visibility_score(peak_look[1], sunlit, sun_elevation),
        samples=samples,
    )


def predict_passes(
    tle: TLERecord,
    observer: Observer,
    start: datetime,
    hours: float = 24.0,
    min_elevation_deg: float = DEFAULT_MIN_ELEVATION_DEG,
    coarse_step_s: float = DEFAULT_COARSE_STEP_S,
    sample_step_s: float = DEFAULT_SAMPLE_STEP_S,
    include_samples: bool = True,
) -> list[SatellitePass]:
    """Find every pass above ``min_elevation_deg`` in a forward time window.

    Coarse-steps the window at ``coarse_step_s``, then bisects each threshold
    crossing to :data:`BISECTION_TOLERANCE_S` (1 s) for the rise and set times
    and ternary-searches the interior for the elevation maximum. A pass already
    in progress at the window start, or still open at the window end, comes
    back with ``partial=True`` and clipped to the window.

    The pass structure follows satlas (MIT) ``predict_passes``; the search
    itself is this repo's own, since satlas delegates event finding to
    Skyfield.
    """
    if hours <= 0:
        return []
    if coarse_step_s <= 0:
        raise ValueError("coarse_step_s must be positive")

    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = start + timedelta(seconds=hours * 3600.0)

    satrec = build_satrec(tle)
    frame = _observer_frame(observer)

    passes: list[SatellitePass] = []
    previous_time: datetime | None = None
    rise_time: datetime | None = None
    partial_start = False

    moment = start
    while moment <= end:
        elevation = _elevation_at(satrec, frame, moment)
        if elevation is None:
            previous_time = moment
            moment = moment + timedelta(seconds=coarse_step_s)
            continue

        above = elevation >= min_elevation_deg
        if above and rise_time is None:
            if previous_time is None:
                rise_time = moment
                partial_start = True
            else:
                rise_time = _bisect_crossing(
                    satrec, frame, min_elevation_deg, previous_time, moment
                )
                partial_start = False
        elif not above and rise_time is not None and previous_time is not None:
            set_time = _bisect_crossing(satrec, frame, min_elevation_deg, previous_time, moment)
            if set_time > rise_time:
                passes.append(
                    _build_pass(
                        satrec,
                        tle,
                        observer,
                        frame,
                        rise_time,
                        set_time,
                        min_elevation_deg,
                        sample_step_s,
                        include_samples,
                        partial_start,
                    )
                )
            rise_time = None
            partial_start = False

        previous_time = moment
        moment = moment + timedelta(seconds=coarse_step_s)

    if rise_time is not None and end > rise_time:
        passes.append(
            _build_pass(
                satrec,
                tle,
                observer,
                frame,
                rise_time,
                end,
                min_elevation_deg,
                sample_step_s,
                include_samples,
                True,
            )
        )

    return passes


def predict_passes_multi(
    tles: Iterable[TLERecord],
    observer: Observer,
    start: datetime,
    hours: float = 24.0,
    min_elevation_deg: float = DEFAULT_MIN_ELEVATION_DEG,
    coarse_step_s: float = DEFAULT_COARSE_STEP_S,
    sample_step_s: float = DEFAULT_SAMPLE_STEP_S,
    include_samples: bool = False,
) -> list[SatellitePass]:
    """Passes of many objects over one observer, sorted by rise time."""
    results: list[SatellitePass] = []
    for tle in tles:
        try:
            results.extend(
                predict_passes(
                    tle,
                    observer,
                    start,
                    hours,
                    min_elevation_deg,
                    coarse_step_s,
                    sample_step_s,
                    include_samples,
                )
            )
        except (RuntimeError, ValueError):
            continue
    results.sort(key=lambda item: item.rise_time)
    return results


def predict_passes_multi_observer(
    tle: TLERecord,
    observers: Iterable[Observer],
    start: datetime,
    hours: float = 24.0,
    min_elevation_deg: float = DEFAULT_MIN_ELEVATION_DEG,
    coarse_step_s: float = DEFAULT_COARSE_STEP_S,
    sample_step_s: float = DEFAULT_SAMPLE_STEP_S,
    include_samples: bool = False,
) -> list[SatellitePass]:
    """Passes of one object over many observers, sorted by rise time.

    Each pass carries ``observer_name``, so the flat list stays unambiguous.
    """
    results: list[SatellitePass] = []
    for observer in observers:
        try:
            results.extend(
                predict_passes(
                    tle,
                    observer,
                    start,
                    hours,
                    min_elevation_deg,
                    coarse_step_s,
                    sample_step_s,
                    include_samples,
                )
            )
        except (RuntimeError, ValueError):
            continue
    results.sort(key=lambda item: item.rise_time)
    return results
