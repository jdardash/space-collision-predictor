"""Fit a valid TLE to an ECI (TEME) state vector.

Takes a position/velocity pair in the SGP4 output frame (TEME, km and km/s)
plus an epoch and produces two TLE lines whose SGP4 evaluation at that epoch
reproduces the input state. This is what lets a synthetic object (a breakup
fragment, a propagated hypothetical, a state read from a CDM) enter the
ordinary catalog path: everything downstream keeps going through
TLEStore.load_from_text() and the normal propagator.

Method
------
1. Osculating classical elements are computed from the state with the WGS72
   gravitational parameter used by SGP4.
2. Those elements seed a trial TLE. SGP4 is evaluated at the epoch through
   sda.propagator and the osculating elements of its output are differenced
   against the target in *equinoctial* variables (a, h, k, p, q, mean
   longitude), which stay well conditioned as eccentricity or inclination go to
   zero. The difference is added back to the trial mean elements.
3. The loop stops when the position residual at epoch falls below
   POSITION_TOLERANCE_KM, when the residual has stopped improving for
   STAGNATION_LIMIT passes (it has reached the floor set by the TLE text
   format), or after MAX_ITERATIONS passes. The best iterate is kept, not the
   last one.

The osculating-to-mean offset is a J2-order perturbation, so the fixed-point
iteration is strongly contractive and converges in a handful of passes for
orbits from LEO to GEO.

Accuracy floor: a TLE stores angles to 1e-4 degrees, so the best achievable
residual is about half an angular quantum times the orbit radius, roughly 6 m
in LEO and 40 m at geostationary altitude. POSITION_TOLERANCE_KM is set to
100 m so that "converged" means "at the format floor" rather than "hit the
iteration cap"; measured residuals on the bundled demo catalog are 1 to 8 m in
LEO and 20 to 90 m at GEO.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
from pydantic import BaseModel

from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.models import TLERecord
from sda.propagator import build_satrec, propagate_at
from sda.tle_store import TLEStore

MAX_ITERATIONS = 40
POSITION_TOLERANCE_KM = 0.1
STAGNATION_LIMIT = 8

# Bounds on the measured loop gain used by the diagonal secant correction.
MIN_GAIN = 0.05
MAX_GAIN = 20.0

TWO_PI = 2.0 * math.pi

# Lowest perigee radius for which a fitted TLE is worth producing. Below this
# the object is reentering and SGP4 gives up anyway.
MIN_PERIGEE_RADIUS_KM = EARTH_RADIUS_KM + 90.0
MAX_FIT_ECCENTRICITY = 0.99


class ClassicalElements(BaseModel):
    """Classical orbital elements. Angles in radians, lengths in km."""

    semi_major_axis_km: float
    eccentricity: float
    inclination_rad: float
    raan_rad: float
    arg_perigee_rad: float
    mean_anomaly_rad: float

    @property
    def mean_motion_rev_day(self) -> float:
        """Keplerian mean motion in revolutions per day."""
        return mean_motion_rev_day(self.semi_major_axis_km)


class TLEFitResult(BaseModel):
    """Outcome of a state-to-TLE fit."""

    tle: TLERecord | None
    converged: bool
    iterations: int
    position_error_km: float
    velocity_error_km_s: float
    reason: str = ""


class FragmentTLESet(BaseModel):
    """TLE text for a set of fitted objects."""

    tle_text: str
    fitted: int
    skipped: int
    norad_ids: list[int]
    max_position_error_km: float


# --------------------------------------------------------------------------
# Element conversions
# --------------------------------------------------------------------------


def mean_motion_rev_day(semi_major_axis_km: float) -> float:
    """Keplerian mean motion (rev/day) for a semi-major axis in km."""
    if semi_major_axis_km <= 0.0:
        raise ValueError("semi_major_axis_km must be positive")
    n_rad_s = math.sqrt(EARTH_MU_KM3_S2 / semi_major_axis_km**3)
    return n_rad_s * 86400.0 / TWO_PI


def semi_major_axis_km(mean_motion_rev_day_value: float) -> float:
    """Semi-major axis in km for a mean motion in rev/day."""
    if mean_motion_rev_day_value <= 0.0:
        raise ValueError("mean motion must be positive")
    n_rad_s = mean_motion_rev_day_value * TWO_PI / 86400.0
    return float((EARTH_MU_KM3_S2 / n_rad_s**2) ** (1.0 / 3.0))


def _wrap_two_pi(angle: float) -> float:
    """Wrap an angle into [0, 2*pi)."""
    return angle % TWO_PI


def _wrap_pi(angle: float) -> float:
    """Wrap an angle difference into [-pi, pi)."""
    return (angle + math.pi) % TWO_PI - math.pi


def osculating_elements(
    position_km: tuple[float, float, float] | np.ndarray,
    velocity_km_s: tuple[float, float, float] | np.ndarray,
) -> ClassicalElements:
    """Classical osculating elements from an ECI state, WGS72 mu.

    Uses atan2 forms throughout so the result stays finite for circular and
    equatorial orbits; the degenerate split between RAAN, argument of perigee
    and mean anomaly is then absorbed by the equinoctial variables used for the
    differential correction.
    """
    r_vec = np.asarray(position_km, dtype=float)
    v_vec = np.asarray(velocity_km_s, dtype=float)
    mu = EARTH_MU_KM3_S2

    r = float(np.linalg.norm(r_vec))
    v2 = float(np.dot(v_vec, v_vec))
    rv = float(np.dot(r_vec, v_vec))
    if r <= 0.0:
        raise ValueError("position vector must be non-zero")

    h_vec = np.cross(r_vec, v_vec)
    h = float(np.linalg.norm(h_vec))
    if h <= 0.0:
        raise ValueError("degenerate (radial) state has no orbit plane")

    a = 1.0 / (2.0 / r - v2 / mu)

    inclination = math.atan2(math.hypot(h_vec[0], h_vec[1]), h_vec[2])
    raan = math.atan2(h_vec[0], -h_vec[1])

    p = h * h / mu
    e_cos_nu = p / r - 1.0
    e_sin_nu = rv * h / (r * mu)
    ecc = math.hypot(e_cos_nu, e_sin_nu)
    nu = math.atan2(e_sin_nu, e_cos_nu)

    cos_raan, sin_raan = math.cos(raan), math.sin(raan)
    x_plane = r_vec[0] * cos_raan + r_vec[1] * sin_raan
    y_plane = (-r_vec[0] * sin_raan + r_vec[1] * cos_raan) * math.cos(inclination) + r_vec[
        2
    ] * math.sin(inclination)
    arg_latitude = math.atan2(y_plane, x_plane)
    arg_perigee = _wrap_two_pi(arg_latitude - nu)

    if ecc < 1.0:
        ecc_anomaly = math.atan2(math.sqrt(max(0.0, 1.0 - ecc * ecc)) * math.sin(nu),
                                 ecc + math.cos(nu))
        mean_anomaly = _wrap_two_pi(ecc_anomaly - ecc * math.sin(ecc_anomaly))
    else:
        mean_anomaly = _wrap_two_pi(nu)

    return ClassicalElements(
        semi_major_axis_km=a,
        eccentricity=ecc,
        inclination_rad=inclination,
        raan_rad=_wrap_two_pi(raan),
        arg_perigee_rad=arg_perigee,
        mean_anomaly_rad=mean_anomaly,
    )


def _to_equinoctial(el: ClassicalElements) -> np.ndarray:
    """Non-singular equinoctial set [a, h, k, p, q, mean longitude].

        h = e sin(argp + raan),  k = e cos(argp + raan)
        p = tan(i/2) sin(raan),  q = tan(i/2) cos(raan)
        L = M + argp + raan

    The combination is invariant to how a near-circular or near-equatorial
    orbit splits its angles, which is exactly the failure mode of a classical
    element correction.
    """
    lon_perigee = el.arg_perigee_rad + el.raan_rad
    tan_half_i = math.tan(el.inclination_rad / 2.0)
    return np.array(
        [
            el.semi_major_axis_km,
            el.eccentricity * math.sin(lon_perigee),
            el.eccentricity * math.cos(lon_perigee),
            tan_half_i * math.sin(el.raan_rad),
            tan_half_i * math.cos(el.raan_rad),
            el.mean_anomaly_rad + lon_perigee,
        ]
    )


def _from_equinoctial(eq: np.ndarray) -> ClassicalElements:
    """Inverse of _to_equinoctial()."""
    a, h, k, p, q, mean_longitude = (float(x) for x in eq)
    ecc = math.hypot(h, k)
    lon_perigee = math.atan2(h, k) if ecc > 0.0 else 0.0
    tan_half_i = math.hypot(p, q)
    inclination = 2.0 * math.atan(tan_half_i)
    raan = math.atan2(p, q) if tan_half_i > 0.0 else 0.0
    return ClassicalElements(
        semi_major_axis_km=a,
        eccentricity=ecc,
        inclination_rad=inclination,
        raan_rad=_wrap_two_pi(raan),
        arg_perigee_rad=_wrap_two_pi(lon_perigee - raan),
        mean_anomaly_rad=_wrap_two_pi(mean_longitude - lon_perigee),
    )


# --------------------------------------------------------------------------
# TLE text formatting
# --------------------------------------------------------------------------


def tle_checksum(line: str) -> int:
    """TLE modulo-10 checksum over the first 68 columns.

    Digits add their value, a minus sign adds 1, everything else adds 0.
    """
    total = 0
    for char in line[:68]:
        if char.isdigit():
            total += int(char)
        elif char == "-":
            total += 1
    return total % 10


def _epoch_field(epoch: datetime) -> str:
    """TLE epoch columns 19-32: two-digit year plus fractional day of year."""
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)
    epoch = epoch.astimezone(UTC)
    year_start = datetime(epoch.year, 1, 1, tzinfo=UTC)
    day_of_year = (epoch - year_start).total_seconds() / 86400.0 + 1.0
    return f"{epoch.year % 100:02d}{day_of_year:012.8f}"


def _ndot_field(ndot_rev_day2_over_2: float) -> str:
    """TLE columns 34-43: first derivative of mean motion over two."""
    value = max(-0.99999999, min(0.99999999, ndot_rev_day2_over_2))
    sign = "-" if value < 0 else " "
    return f"{sign}.{int(round(abs(value) * 1e8)):08d}"


def _exponent_field(value: float) -> str:
    """TLE 8-column field with an assumed decimal point (BSTAR, second derivative)."""
    if value == 0.0 or not math.isfinite(value):
        return " 00000-0"
    sign = "-" if value < 0 else " "
    magnitude = abs(value)
    exponent = int(math.floor(math.log10(magnitude))) + 1
    mantissa = int(round(magnitude / (10.0**exponent) * 1e5))
    if mantissa >= 100000:
        mantissa //= 10
        exponent += 1
    if exponent > 9 or exponent < -9:
        return " 00000-0"
    exp_sign = "+" if exponent >= 0 else "-"
    return f"{sign}{mantissa:05d}{exp_sign}{abs(exponent)}"


def format_tle_lines(
    norad_id: int,
    epoch: datetime,
    elements: ClassicalElements,
    bstar: float = 0.0,
    ndot_rev_day2_over_2: float = 0.0,
    classification: str = "U",
    intl_designator: str = "",
    element_number: int = 999,
    rev_number: int = 1,
) -> tuple[str, str]:
    """Render classical mean elements as the two TLE lines, checksums included.

    Column layout follows the standard NORAD two-line element set: line 1
    carries the epoch, drag terms and identifiers, line 2 the angles, the
    eccentricity with an assumed leading decimal point, and the mean motion in
    revolutions per day.
    """
    if not 0 <= norad_id <= 99999:
        raise ValueError("norad_id must fit in five digits")
    ecc = min(max(elements.eccentricity, 0.0), 0.9999999)
    designator = (intl_designator or f"{epoch.year % 100:02d}001A")[:8]

    line1 = (
        "1 "
        f"{norad_id:05d}{classification[0]} "
        f"{designator:<8s} "
        f"{_epoch_field(epoch)} "
        f"{_ndot_field(ndot_rev_day2_over_2)} "
        " 00000-0 "
        f"{_exponent_field(bstar)} "
        "0 "
        f"{element_number % 10000:4d}"
    )
    line2 = (
        "2 "
        f"{norad_id:05d} "
        f"{math.degrees(elements.inclination_rad) % 180.0:8.4f} "
        f"{math.degrees(elements.raan_rad) % 360.0:8.4f} "
        f"{int(round(ecc * 1e7)):07d} "
        f"{math.degrees(elements.arg_perigee_rad) % 360.0:8.4f} "
        f"{math.degrees(elements.mean_anomaly_rad) % 360.0:8.4f} "
        f"{elements.mean_motion_rev_day:11.8f}"
        f"{rev_number % 100000:5d}"
    )
    line1 = f"{line1[:68]:<68s}{tle_checksum(line1)}"
    line2 = f"{line2[:68]:<68s}{tle_checksum(line2)}"
    return line1, line2


# --------------------------------------------------------------------------
# Differential correction
# --------------------------------------------------------------------------


def _update_gain(
    gain: np.ndarray,
    trial_eq: np.ndarray,
    out_eq: np.ndarray,
    prev_eq: np.ndarray | None,
    prev_out: np.ndarray | None,
) -> np.ndarray:
    """Diagonal Broyden update of the loop gain d(output)/d(trial) per element.

    A plain fixed-point step assumes a unit gain. That holds for most orbits,
    but near zero inclination SGP4's low-inclination handling can invert the
    sign of the inclination-vector response, so an undamped step walks away
    from the solution and damping alone never recovers. Measuring the gain from
    the step just taken and dividing the next correction by it turns the loop
    into a diagonal secant method, which handles the inverted case directly.
    """
    if prev_eq is None or prev_out is None:
        return gain
    updated = gain.copy()
    delta_x = trial_eq - prev_eq
    delta_out = out_eq - prev_out
    delta_out[5] = _wrap_pi(float(delta_out[5]))
    for i in range(6):
        scale = max(abs(float(trial_eq[i])), 1e-9)
        if abs(float(delta_x[i])) <= max(1e-11 * scale, 1e-13):
            continue
        measured = float(delta_out[i]) / float(delta_x[i])
        if abs(measured) < MIN_GAIN:
            continue
        updated[i] = float(np.clip(measured, -MAX_GAIN, MAX_GAIN))
    return updated


def state_to_tle(
    position_km: tuple[float, float, float] | np.ndarray,
    velocity_km_s: tuple[float, float, float] | np.ndarray,
    epoch: datetime,
    norad_id: int,
    name: str = "SYNTHETIC",
    bstar: float = 0.0,
    intl_designator: str = "",
    max_iterations: int = MAX_ITERATIONS,
    tolerance_km: float = POSITION_TOLERANCE_KM,
) -> TLEFitResult:
    """Fit a TLE whose SGP4 state at `epoch` matches the given ECI state.

    Returns a TLEFitResult; `converged` says whether the position residual fell
    under `tolerance_km` inside `max_iterations`, and the fitted TLE is
    returned either way so a caller can judge the residual for itself. `tle` is
    None only when no TLE could be built at all (unbound orbit, perigee below
    the atmosphere, or SGP4 refusing every trial).
    """
    r_target = np.asarray(position_km, dtype=float)
    v_target = np.asarray(velocity_km_s, dtype=float)

    target = osculating_elements(r_target, v_target)
    if target.eccentricity >= MAX_FIT_ECCENTRICITY or target.semi_major_axis_km <= 0.0:
        return TLEFitResult(
            tle=None, converged=False, iterations=0,
            position_error_km=float("inf"), velocity_error_km_s=float("inf"),
            reason="unbound or hyperbolic state",
        )
    perigee_radius = target.semi_major_axis_km * (1.0 - target.eccentricity)
    if perigee_radius < MIN_PERIGEE_RADIUS_KM:
        return TLEFitResult(
            tle=None, converged=False, iterations=0,
            position_error_km=float("inf"), velocity_error_km_s=float("inf"),
            reason="perigee below the usable atmosphere",
        )

    target_eq = _to_equinoctial(target)
    trial_eq = target_eq.copy()

    best: TLERecord | None = None
    best_error = float("inf")
    best_velocity_error = float("inf")
    prev_eq: np.ndarray | None = None
    prev_out: np.ndarray | None = None
    gain = np.ones(6)
    iterations = 0
    stagnant = 0

    for step in range(1, max_iterations + 1):
        iterations = step
        trial_el = _from_equinoctial(trial_eq)
        if trial_el.semi_major_axis_km <= 0.0 or trial_el.eccentricity >= MAX_FIT_ECCENTRICITY:
            break
        line1, line2 = format_tle_lines(
            norad_id, epoch, trial_el, bstar=bstar, intl_designator=intl_designator
        )
        record = TLERecord(
            norad_id=norad_id, name=name, line1=line1, line2=line2, epoch=epoch
        )
        try:
            state = propagate_at(build_satrec(record), epoch)
        except (RuntimeError, ValueError):
            break

        r_trial = np.asarray(state.position_km, dtype=float)
        v_trial = np.asarray(state.velocity_km_s, dtype=float)
        error = float(np.linalg.norm(r_target - r_trial))
        out_eq = _to_equinoctial(osculating_elements(r_trial, v_trial))
        residual = target_eq - out_eq
        residual[5] = _wrap_pi(float(residual[5]))

        if error < best_error - 1e-12:
            best_error = error
            best_velocity_error = float(np.linalg.norm(v_target - v_trial))
            best = record
            stagnant = 0
        else:
            stagnant += 1

        if error < tolerance_km or stagnant >= STAGNATION_LIMIT:
            break

        gain = _update_gain(gain, trial_eq, out_eq, prev_eq, prev_out)
        prev_eq, prev_out = trial_eq.copy(), out_eq.copy()
        trial_eq = trial_eq + residual / gain

    if best is None:
        return TLEFitResult(
            tle=None, converged=False, iterations=iterations,
            position_error_km=float("inf"), velocity_error_km_s=float("inf"),
            reason="SGP4 rejected every trial element set",
        )
    return TLEFitResult(
        tle=best,
        converged=best_error < tolerance_km,
        iterations=iterations,
        position_error_km=best_error,
        velocity_error_km_s=best_velocity_error,
        reason="" if best_error < tolerance_km else "residual floor or iteration cap reached",
    )


def fragments_to_tles(
    cloud: object,
    base_norad_id: int = 90000,
    name_prefix: str = "FRAG",
    max_objects: int | None = None,
    tolerance_km: float = 0.5,
) -> FragmentTLESet:
    """Fit TLEs to every fragment of a breakup cloud.

    `cloud` is a sda.breakup.BreakupCloud (typed loosely so this module does not
    import the breakup model and create a cycle). Fragments whose orbit is
    unbound, whose perigee is already inside the atmosphere, or whose fit does
    not reach `tolerance_km` are skipped and counted.

    The returned text is ordinary three-line TLE text: pass it straight to
    TLEStore.load_from_text() and the fragments screen like any other object.
    """
    fragments = list(getattr(cloud, "fragments", []))
    epoch = getattr(cloud, "epoch", datetime.now(UTC))
    if max_objects is not None:
        fragments = fragments[:max_objects]

    lines: list[str] = []
    norad_ids: list[int] = []
    skipped = 0
    max_error = 0.0

    for offset, frag in enumerate(fragments):
        norad_id = base_norad_id + offset
        if norad_id > 99999:
            skipped += len(fragments) - offset
            break
        result = state_to_tle(
            frag.position_km,
            frag.velocity_km_s,
            epoch,
            norad_id,
            name=f"{name_prefix}-{offset:05d}",
            tolerance_km=tolerance_km,
        )
        if result.tle is None or result.position_error_km > tolerance_km:
            skipped += 1
            continue
        lines.extend([result.tle.name, result.tle.line1, result.tle.line2])
        norad_ids.append(norad_id)
        max_error = max(max_error, result.position_error_km)

    return FragmentTLESet(
        tle_text="\n".join(lines),
        fitted=len(norad_ids),
        skipped=skipped,
        norad_ids=norad_ids,
        max_position_error_km=max_error,
    )


def load_fragments_into_store(cloud: object, store: TLEStore, **kwargs: object) -> FragmentTLESet:
    """Fit a breakup cloud to TLEs and ingest them through TLEStore.load_from_text().

    Keyword arguments are forwarded to fragments_to_tles().
    """
    result = fragments_to_tles(cloud, **kwargs)  # type: ignore[arg-type]
    if result.tle_text:
        store.load_from_text(result.tle_text)
    return result
