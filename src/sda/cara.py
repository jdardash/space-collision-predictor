"""External validation tier: our Pc against the NASA CARA reference values.

Adapted from CARA_Analysis_Tools (NASA Open Source Agreement v1.3); the
fixtures under tests/data/cara are verbatim copies with their NOTICE.md.

CARA publishes 53 real conjunction data messages together with the Pc that its
own operational chain computed for each of them under five methods (Pc2D, Nc2D,
Nc3D, Coppola 3D and a supervised direct Monte Carlo), plus 11 Alfano 2009
benchmark cases whose expected 2D-Pc are literals in a MATLAB unit test. This
module runs our probability code over those fixtures and reports, per case and
per method, how far we land from the reference.

Three things have to be right before the numbers mean anything:

1. Frame. CDM covariances are 6x6 in RTN; only the position 3x3 block matters
   for 2D-Pc, and it has to be rotated into the inertial frame using that
   object's own state before the two objects can be combined. Positions and
   velocities in these messages are EME2000, which we treat as the same
   inertial frame our propagator works in (km, km/s).
2. Epoch. CDM TCA is rounded to the millisecond and the states are given at
   that rounded time, so the objects are not actually at closest approach.
   CARA's own Pc2D_FromCDM.m calls FindNearbyCA before PcCircle. We do the
   same, but with two-body motion rather than the released implementation's
   straight lines (FindNearbyCA.m errors out on its TWOBODY branch). The two
   agree to well under a nanosecond here because the shift is under a
   millisecond; see tca_shift_s in the comparison record.
3. Hard body radius. The combined HBR lives in a CDM COMMENT, which the
   CCSDS keyword parser in sda.cdm skips, and in the HBR_m column of CARA's
   spreadsheet. The spreadsheet wins when both are present.

Run:  python -m sda.cara [--mc-samples N] [--json]
Exit status is non-zero if any Foster case falls outside CARA's tolerance
bands, so this doubles as an external-parity gate.

References
----------
.. [1] NASA CARA Analysis Tools, github.com/nasa/CARA_Analysis_Tools at 86ba28a.
       DataFiles/PcTestCaseCDMs/README.md lines 35-49 (TCA re-propagation),
       ComparePc2D_to_CARAValues.m lines 17-24 (tolerance bands),
       DistributedMatlab/ProbabilityOfCollision/UnitTests/Pc2D_Foster_UnitTest.m
       lines 31-45 (Alfano literals and their 1e-3 relative tolerance).
.. [2] S. Alfano, "Satellite Conjunction Monte Carlo Analysis," AAS 09-233, 2009.
.. [3] R. Foster and H. Estes, "A Parametric Analysis of Orbital Debris Collision
       Probability and Maneuver Rate for Space Vehicles," NASA/JSC-25898, 1992.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from sda.constants import EARTH_MU_KM3_S2
from sda.probability import pc_chan, pc_foster, pc_monte_carlo_2d, rtn_covariance_to_eci

__all__ = [
    "CaraCdm",
    "CaraComparison",
    "CaraReferenceRow",
    "alfano_comparisons",
    "cara_tolerance_band",
    "compare_all",
    "encounter_geometry",
    "find_true_tca",
    "fixture_dir",
    "load_alfano_literals",
    "load_reference_rows",
    "main",
    "monte_carlo_is_resolvable",
    "propagate_two_body",
    "read_cara_cdm",
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "data" / "cara"

#: Tolerance ladder from ComparePc2D_to_CARAValues.m lines 17-24, evaluated by
#: isclose.m as abs(a - b) <= max(tol, tol * max(abs(a), abs(b))).
CARA_TOLERANCE_BANDS: tuple[tuple[str, float], ...] = (
    ("VeryTight", 1e-15),
    ("Tight", 1e-12),
    ("Loose", 1e-8),
    ("VeryLoose", 1e-3),
)

#: Widest band CARA still calls acceptable; anything past it is "NoMatch".
CARA_ACCEPTANCE_TOLERANCE = 1e-3


def fixture_dir() -> Path:
    """Directory holding the vendored CARA fixtures.

    Override with SDA_CARA_FIXTURES; regenerate with
    scripts/import_cara_fixtures.py.
    """
    override = os.environ.get("SDA_CARA_FIXTURES")
    return Path(override) if override else _DEFAULT_FIXTURE_DIR


def cara_tolerance_band(actual: float, expected: float) -> str:
    """Name the tightest CARA tolerance band that holds for this pair.

    Mirrors isclose.m: a band with tolerance t holds when
    abs(a - b) <= max(t, t * max(abs(a), abs(b))). Returns "Exact" for a bit
    for bit match and "NoMatch" when even the 1e-3 band fails.
    """
    if actual == expected:
        return "Exact"
    diff = abs(actual - expected)
    scale = max(abs(actual), abs(expected))
    for name, tol in CARA_TOLERANCE_BANDS:
        if diff <= max(tol, tol * scale):
            return name
    return "NoMatch"


# ---------------------------------------------------------------------------
# Reference spreadsheet
# ---------------------------------------------------------------------------


def _to_float(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float("nan")


@dataclass(frozen=True)
class CaraReferenceRow:
    """One row of CARA_PcMethod_Test_Conjunctions.xlsx (as vendored CSV)."""

    conjunction_id: str
    comment: str
    primary_name: str
    secondary_name: str
    hbr_m: float
    miss_distance_m: float
    relative_speed_m_s: float
    velocity_angle_deg: float
    pc2d_no_adj: float
    pc2d: float
    nc2d: float
    nc3d: float
    pc_sdmc: float
    pc_sdmc_lo: float
    pc_sdmc_hi: float
    pc3d_coppola: float
    violations_pc2d: str
    violations_nc2d: str
    violations_nc3d: str
    violations_sdmc: str
    n_hit_sdmc: float
    n_tot_sdmc: float

    @property
    def pc2d_is_valid(self) -> bool:
        """True when CARA records no 2D-Pc method usage violation for this case.

        ViolationsPc2D is a digit string of per-criterion flags; "0" (or an
        empty cell) means the 2D linear-relative-motion assumptions hold.
        """
        return self.violations_pc2d.strip() in ("", "0")

    @property
    def violation_reason(self) -> str:
        """CARA's own words for why the 2D method is or is not usable here."""
        return self.comment.strip()


_REFERENCE_COLUMNS = {
    "conjunction_id": "Conjunction_ID",
    "comment": "Comment",
    "primary_name": "PrimaryName",
    "secondary_name": "SecondaryName",
    "hbr_m": "HBR_m",
    "miss_distance_m": "MissDist_m",
    "relative_speed_m_s": "Vrel_mps",
    "velocity_angle_deg": "Vang_deg",
    "pc2d_no_adj": "Pc2D_NoAdj",
    "pc2d": "Pc2D",
    "nc2d": "Nc2D",
    "nc3d": "Nc3D",
    "pc_sdmc": "PcSDMC",
    "pc_sdmc_lo": "PcSDMCLo",
    "pc_sdmc_hi": "PcSDMCHi",
    "pc3d_coppola": "Pc3DCoppola",
    "n_hit_sdmc": "NhitSDMC",
    "n_tot_sdmc": "NtotSDMC",
}
_REFERENCE_TEXT_COLUMNS = {
    "conjunction_id",
    "comment",
    "primary_name",
    "secondary_name",
    "violations_pc2d",
    "violations_nc2d",
    "violations_nc3d",
    "violations_sdmc",
}
_VIOLATION_COLUMNS = {
    "violations_pc2d": "ViolationsPc2D",
    "violations_nc2d": "ViolationsNc2D",
    "violations_nc3d": "ViolationsNc3D",
    "violations_sdmc": "ViolationsSDMC",
}


def load_reference_rows(directory: Path | None = None) -> list[CaraReferenceRow]:
    """Load the 53 reference conjunctions from the vendored CSV."""
    path = (directory or fixture_dir()) / "pc_method_test_conjunctions.csv"
    with path.open(newline="", encoding="ascii") as handle:
        raw_rows = list(csv.DictReader(handle))

    rows: list[CaraReferenceRow] = []
    for raw in raw_rows:
        kwargs: dict[str, Any] = {}
        for attr, column in _REFERENCE_COLUMNS.items():
            value = (raw.get(column) or "").strip()
            kwargs[attr] = value if attr in _REFERENCE_TEXT_COLUMNS else _to_float(value)
        for attr, column in _VIOLATION_COLUMNS.items():
            kwargs[attr] = (raw.get(column) or "").strip()
        rows.append(CaraReferenceRow(**kwargs))
    return rows


def load_alfano_literals(directory: Path | None = None) -> dict[str, Any]:
    """Load the 11 Alfano expected-Pc literals extracted from the MATLAB test."""
    path = (directory or fixture_dir()) / "alfano_expected_pc.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="ascii"))
    return data


# ---------------------------------------------------------------------------
# Tolerant KVN CDM reader
# ---------------------------------------------------------------------------

#: Lower-triangle position covariance keywords, RTN order, in (row, col).
_COV_INDEX: dict[str, tuple[int, int]] = {
    "CR_R": (0, 0),
    "CT_R": (1, 0),
    "CT_T": (1, 1),
    "CN_R": (2, 0),
    "CN_T": (2, 1),
    "CN_N": (2, 2),
}
_STATE_KEYS = ("X", "Y", "Z", "X_DOT", "Y_DOT", "Z_DOT")
_KM_PER_M = 1e-3
_KM2_PER_M2 = 1e-6


@dataclass
class CaraCdm:
    """A CDM reduced to what a 2D-Pc calculation needs. Distances in km."""

    path: Path
    reader: str
    message_id: str | None = None
    tca: datetime | None = None
    stated_pc: float | None = None
    stated_pc_method: str | None = None
    hbr_m: float | None = None
    miss_distance_km: float | None = None
    relative_speed_km_s: float | None = None
    position1_km: np.ndarray = field(default_factory=lambda: np.zeros(3))
    velocity1_km_s: np.ndarray = field(default_factory=lambda: np.zeros(3))
    cov1_rtn_km2: np.ndarray = field(default_factory=lambda: np.zeros((3, 3)))
    position2_km: np.ndarray = field(default_factory=lambda: np.zeros(3))
    velocity2_km_s: np.ndarray = field(default_factory=lambda: np.zeros(3))
    cov2_rtn_km2: np.ndarray = field(default_factory=lambda: np.zeros((3, 3)))

    def combined_covariance_eci_km2(self) -> np.ndarray:
        """Both RTN position covariances rotated to the inertial frame and summed."""
        first = rtn_covariance_to_eci(self.cov1_rtn_km2, self.position1_km, self.velocity1_km_s)
        second = rtn_covariance_to_eci(self.cov2_rtn_km2, self.position2_km, self.velocity2_km_s)
        combined: np.ndarray = np.asarray(first, dtype=float) + np.asarray(second, dtype=float)
        return combined


def _kvn_pairs(text: str) -> list[tuple[str, str, int]]:
    """Split KVN text into (key, value, object_section) triples.

    object_section is -1 for the relative-metadata block and 0/1 for the two
    object blocks. COMMENT lines are kept: the hard body radius only ever
    appears as one.
    """
    pairs: list[tuple[str, str, int]] = []
    section = -1
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if key.startswith("COMMENT"):
            key = key[len("COMMENT"):].strip()
            if not key:
                continue
            pairs.append((f"COMMENT {key}", value, section))
            continue
        if key == "OBJECT":
            section = 0 if value.upper() == "OBJECT1" else 1
        pairs.append((key, value, section))
    return pairs


def _numeric(raw: str) -> float | None:
    """Parse a KVN numeric value, dropping any bracketed unit. NaN reads as None."""
    head = raw.split("[", 1)[0].strip()
    if not head:
        return None
    try:
        value = float(head)
    except ValueError:
        return None
    return None if math.isnan(value) else value


def _unit_of(raw: str) -> str | None:
    parts = raw.split("[", 1)
    return parts[1].rstrip("]").strip().lower() if len(parts) == 2 else None


def _parse_cdm_tolerant(text: str, path: Path) -> CaraCdm:
    """Parse a CARA KVN CDM without rejecting the file over a bad unit tag.

    sda.cdm.parse_cdm is stricter, and rightly so for ingest: it raises on an
    unknown unit. The Alfano sample CDMs tag RELATIVE_VELOCITY_* as [m] rather
    than [m/s], so a validation harness that must read every fixture needs a
    reader that takes the declared unit as a hint and falls back to the CCSDS
    default for the keyword.
    """
    cdm = CaraCdm(path=path, reader="sda.cara tolerant reader")
    states: list[dict[str, float]] = [{}, {}]
    covs: list[dict[str, float]] = [{}, {}]

    for key, raw, section in _kvn_pairs(text):
        if key == "MESSAGE_ID":
            cdm.message_id = raw
        elif key == "TCA":
            cdm.tca = _parse_epoch(raw)
        elif key == "COLLISION_PROBABILITY":
            cdm.stated_pc = _numeric(raw)
        elif key == "COLLISION_PROBABILITY_METHOD":
            cdm.stated_pc_method = raw
        elif key == "COMMENT HBR":
            value = _numeric(raw)
            if value is not None:
                # CCSDS has no HBR keyword; CARA writes it as a comment in
                # metres, with or without an explicit [m] tag.
                cdm.hbr_m = value * (1e3 if _unit_of(raw) == "km" else 1.0)
        elif key == "MISS_DISTANCE":
            value = _numeric(raw)
            if value is not None:
                cdm.miss_distance_km = value * (1.0 if _unit_of(raw) == "km" else _KM_PER_M)
        elif key == "RELATIVE_SPEED":
            value = _numeric(raw)
            if value is not None:
                cdm.relative_speed_km_s = value * (1.0 if _unit_of(raw) == "km/s" else _KM_PER_M)
        elif section >= 0 and key in _STATE_KEYS:
            value = _numeric(raw)
            if value is not None:
                # CCSDS states are km / km s^-1 by default.
                unit = _unit_of(raw)
                scale = _KM_PER_M if unit in ("m", "m/s") else 1.0
                states[section][key] = value * scale
        elif section >= 0 and key in _COV_INDEX:
            value = _numeric(raw)
            if value is not None:
                scale = 1.0 if _unit_of(raw) in ("km**2", "km2") else _KM2_PER_M2
                covs[section][key] = value * scale

    for idx in (0, 1):
        missing = [k for k in _STATE_KEYS if k not in states[idx]]
        if missing:
            raise ValueError(f"{path.name}: OBJECT{idx + 1} is missing {', '.join(missing)}")
        position = np.array([states[idx][k] for k in ("X", "Y", "Z")], dtype=float)
        velocity = np.array([states[idx][k] for k in ("X_DOT", "Y_DOT", "Z_DOT")], dtype=float)
        cov = np.zeros((3, 3))
        for keyword, (i, j) in _COV_INDEX.items():
            cov[i, j] = cov[j, i] = covs[idx].get(keyword, 0.0)
        if idx == 0:
            cdm.position1_km, cdm.velocity1_km_s, cdm.cov1_rtn_km2 = position, velocity, cov
        else:
            cdm.position2_km, cdm.velocity2_km_s, cdm.cov2_rtn_km2 = position, velocity, cov
    return cdm


def _parse_epoch(raw: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _hbr_from_comment(text: str) -> float | None:
    for key, raw, _section in _kvn_pairs(text):
        if key == "COMMENT HBR":
            value = _numeric(raw)
            if value is not None:
                return value * (1e3 if _unit_of(raw) == "km" else 1.0)
    return None


def read_cara_cdm(path: Path) -> CaraCdm:
    """Read one CDM, preferring the project's own ingest path.

    sda.cdm.parse_cdm is tried first so this tier exercises the shipped
    reader end to end; the tolerant reader above takes over for fixtures it
    refuses. Either way the hard body radius comes from the COMMENT block,
    which the CCSDS keyword parser does not surface.
    """
    text = path.read_text(encoding="ascii", errors="replace")
    try:
        from sda.cdm import parse_cdm

        parsed = parse_cdm(text)
        obj1, obj2 = parsed.object1, parsed.object2
        if (
            obj1.position_km is None
            or obj1.velocity_km_s is None
            or obj1.cov_rtn_km2 is None
            or obj2.position_km is None
            or obj2.velocity_km_s is None
            or obj2.cov_rtn_km2 is None
        ):
            raise ValueError("incomplete state or covariance")
        return CaraCdm(
            path=path,
            reader="sda.cdm.parse_cdm",
            message_id=parsed.message_id,
            tca=parsed.tca,
            stated_pc=parsed.stated_collision_probability,
            stated_pc_method=None,
            hbr_m=_hbr_from_comment(text),
            miss_distance_km=parsed.miss_distance_km,
            relative_speed_km_s=parsed.relative_speed_km_s,
            position1_km=np.asarray(obj1.position_km, dtype=float),
            velocity1_km_s=np.asarray(obj1.velocity_km_s, dtype=float),
            cov1_rtn_km2=np.asarray(obj1.cov_rtn_km2, dtype=float),
            position2_km=np.asarray(obj2.position_km, dtype=float),
            velocity2_km_s=np.asarray(obj2.velocity_km_s, dtype=float),
            cov2_rtn_km2=np.asarray(obj2.cov_rtn_km2, dtype=float),
        )
    except (ValueError, KeyError, ImportError, AttributeError):
        return _parse_cdm_tolerant(text, path)


# ---------------------------------------------------------------------------
# Two-body propagation and the true time of closest approach
# ---------------------------------------------------------------------------


def _stumpff(psi: float) -> tuple[float, float]:
    """Stumpff functions c2(psi), c3(psi) with series near psi = 0."""
    if psi > 1e-6:
        root = math.sqrt(psi)
        return (1.0 - math.cos(root)) / psi, (root - math.sin(root)) / (root * psi)
    if psi < -1e-6:
        root = math.sqrt(-psi)
        return (math.cosh(root) - 1.0) / (-psi), (math.sinh(root) - root) / (root * (-psi))
    return (
        0.5 - psi / 24.0 + psi * psi / 720.0,
        1.0 / 6.0 - psi / 120.0 + psi * psi / 5040.0,
    )


def propagate_two_body(
    position_km: np.ndarray,
    velocity_km_s: np.ndarray,
    dt_s: float,
    mu_km3_s2: float = EARTH_MU_KM3_S2,
    tol: float = 1e-12,
    max_iter: int = 80,
) -> tuple[np.ndarray, np.ndarray]:
    """Advance an inertial state by dt_s under two-body motion.

    Universal-variable (Battin/Vallado) Kepler solver with Stumpff functions,
    so a single call is exact for any conic and any step size, unlike a fixed
    step RK4. The gravitational parameter defaults to the WGS72 value in
    sda.constants, which is what SGP4 uses everywhere else in this package.
    """
    r0 = np.asarray(position_km, dtype=float)
    v0 = np.asarray(velocity_km_s, dtype=float)
    if dt_s == 0.0:
        return r0.copy(), v0.copy()

    sqrt_mu = math.sqrt(mu_km3_s2)
    r0_mag = float(np.linalg.norm(r0))
    v0_mag = float(np.linalg.norm(v0))
    rv0 = float(r0 @ v0)
    alpha = 2.0 / r0_mag - v0_mag * v0_mag / mu_km3_s2  # 1 / semi-major axis

    if alpha > 1e-9:
        chi = sqrt_mu * dt_s * alpha
    elif alpha < -1e-9:
        sign = math.copysign(1.0, dt_s)
        a = 1.0 / alpha
        inner = -2.0 * mu_km3_s2 * alpha * dt_s / (
            rv0 + sign * math.sqrt(-mu_km3_s2 * a) * (1.0 - r0_mag * alpha)
        )
        chi = sign * math.sqrt(-a) * math.log(max(inner, 1e-300))
    else:
        chi = sqrt_mu * dt_s / r0_mag

    r_mag = r0_mag
    psi = 0.0
    c2 = 0.5
    c3 = 1.0 / 6.0
    for _ in range(max_iter):
        psi = chi * chi * alpha
        c2, c3 = _stumpff(psi)
        r_mag = (
            chi * chi * c2
            + rv0 / sqrt_mu * chi * (1.0 - psi * c3)
            + r0_mag * (1.0 - psi * c2)
        )
        time_now = (
            chi * chi * chi * c3
            + rv0 / sqrt_mu * chi * chi * c2
            + r0_mag * chi * (1.0 - psi * c3)
        ) / sqrt_mu
        delta = (dt_s - time_now) * sqrt_mu / max(r_mag, 1e-12)
        chi += delta
        if abs(delta) < tol:
            break

    f = 1.0 - chi * chi / r0_mag * c2
    g = dt_s - chi * chi * chi / sqrt_mu * c3
    g_dot = 1.0 - chi * chi / max(r_mag, 1e-12) * c2
    f_dot = sqrt_mu / (max(r_mag, 1e-12) * r0_mag) * chi * (psi * c3 - 1.0)
    return f * r0 + g * v0, f_dot * r0 + g_dot * v0


def _range_rate(
    state1: tuple[np.ndarray, np.ndarray],
    state2: tuple[np.ndarray, np.ndarray],
    dt_s: float,
) -> float:
    """d/dt of half the squared range at dt_s, i.e. (dr . dv). Zero at a CA."""
    r1, v1 = propagate_two_body(state1[0], state1[1], dt_s)
    r2, v2 = propagate_two_body(state2[0], state2[1], dt_s)
    return float((r1 - r2) @ (v1 - v2))


def _range_at(
    state1: tuple[np.ndarray, np.ndarray],
    state2: tuple[np.ndarray, np.ndarray],
    dt_s: float,
) -> float:
    r1, _ = propagate_two_body(state1[0], state1[1], dt_s)
    r2, _ = propagate_two_body(state2[0], state2[1], dt_s)
    return float(np.linalg.norm(r1 - r2))


def _golden_section(f: Callable[[float], float], lo: float, hi: float, tol: float) -> float:
    """Minimise a unimodal f on [lo, hi] by golden-section search."""
    inv_phi = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    c = b - inv_phi * (b - a)
    d = a + inv_phi * (b - a)
    fc, fd = f(c), f(d)
    while abs(b - a) > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - inv_phi * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = f(d)
    return 0.5 * (a + b)


def find_true_tca(
    position1_km: np.ndarray,
    velocity1_km_s: np.ndarray,
    position2_km: np.ndarray,
    velocity2_km_s: np.ndarray,
    window_s: float = 5.0,
    tol_s: float = 1e-12,
    max_iter: int = 40,
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Find the two-body time of closest approach near the CDM epoch.

    CDM states sit at a TCA rounded to the millisecond, so the objects are up
    to half a millisecond away from the actual minimum of the range. Newton on
    the range rate (dr . dv) converges in two or three steps at that scale;
    golden-section search on the range itself is the fallback when Newton
    walks outside the window or the derivative degenerates.

    Returns (dt_s, position1, velocity1, position2, velocity2) with the states
    propagated to the recovered TCA. dt_s is the shift from the CDM epoch.
    """
    s1 = (np.asarray(position1_km, dtype=float), np.asarray(velocity1_km_s, dtype=float))
    s2 = (np.asarray(position2_km, dtype=float), np.asarray(velocity2_km_s, dtype=float))

    dt = 0.0
    converged = False
    for _ in range(max_iter):
        g = _range_rate(s1, s2, dt)
        step = max(1e-4, abs(dt) * 1e-3)
        g_plus = _range_rate(s1, s2, dt + step)
        g_minus = _range_rate(s1, s2, dt - step)
        slope = (g_plus - g_minus) / (2.0 * step)
        if not math.isfinite(slope) or abs(slope) < 1e-30:
            break
        delta = -g / slope
        if not math.isfinite(delta) or abs(dt + delta) > window_s:
            break
        dt += delta
        if abs(delta) <= tol_s:
            converged = True
            break

    if not converged:
        dt = _golden_section(lambda t: _range_at(s1, s2, t), -window_s, window_s, 1e-9)

    r1, v1 = propagate_two_body(s1[0], s1[1], dt)
    r2, v2 = propagate_two_body(s2[0], s2[1], dt)
    return dt, r1, v1, r2, v2


# ---------------------------------------------------------------------------
# Encounter plane
# ---------------------------------------------------------------------------


def _encounter_basis(rel_velocity_km_s: np.ndarray) -> np.ndarray:
    """3x3 rotation whose third row is the relative-velocity direction.

    Same construction as sda/probability.py:33
    (_rotation_matrix_to_conjunction_plane), reproduced here rather than
    imported because that symbol is private to a module this package owns
    separately. The first two rows span the encounter (B-) plane; Pc is
    invariant to how those two axes are spun within the plane, but the miss
    vector and the covariance have to be projected with the same pair, which
    is why both projections happen here.
    """
    v_hat = np.asarray(rel_velocity_km_s, dtype=float)
    v_hat = v_hat / np.linalg.norm(v_hat)
    arbitrary = np.array([1.0, 0.0, 0.0]) if abs(v_hat[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    x_hat = np.cross(v_hat, arbitrary)
    x_hat /= np.linalg.norm(x_hat)
    y_hat = np.cross(v_hat, x_hat)
    y_hat /= np.linalg.norm(y_hat)
    return np.array([x_hat, y_hat, v_hat])


def encounter_geometry(
    position1_km: np.ndarray,
    velocity1_km_s: np.ndarray,
    position2_km: np.ndarray,
    velocity2_km_s: np.ndarray,
    cov_combined_eci_km2: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Project the miss vector and combined covariance onto the encounter plane.

    Returns (miss_2d_km, cov_2d_km2), the inputs every Pc method in
    sda.probability takes.
    """
    miss = np.asarray(position1_km, dtype=float) - np.asarray(position2_km, dtype=float)
    rel_v = np.asarray(velocity1_km_s, dtype=float) - np.asarray(velocity2_km_s, dtype=float)
    rot = _encounter_basis(rel_v)
    plane = rot[:2, :]
    return (rot @ miss)[:2], plane @ np.asarray(cov_combined_eci_km2, dtype=float) @ plane.T


def _optional_pc_alfano(
    miss_2d_km: np.ndarray, cov_2d_km2: np.ndarray, hbr_km: float
) -> float | None:
    """Call sda.probability.pc_alfano if a future version of it exists."""
    import sda.probability as probability

    func = getattr(probability, "pc_alfano", None)
    if func is None:
        return None
    try:
        return float(func(miss_2d_km, cov_2d_km2, hbr_km))
    except (TypeError, ValueError):  # pragma: no cover - signature drift guard
        return None


# ---------------------------------------------------------------------------
# Comparison records
# ---------------------------------------------------------------------------


@dataclass
class CaraComparison:
    """Our Pc next to CARA's, for one conjunction."""

    case_id: str
    label: str
    hbr_m: float
    tca: datetime | None
    tca_shift_s: float
    miss_distance_m: float
    relative_speed_m_s: float
    reader: str
    expected_pc2d: float
    ours: dict[str, float | None]
    reference: dict[str, float] = field(default_factory=dict)
    violation_flag: str = "0"
    violation_reason: str = ""
    monte_carlo_samples: int = 0

    @property
    def pc2d_is_valid(self) -> bool:
        return self.violation_flag.strip() in ("", "0")

    def absolute_error(self, method: str) -> float:
        value = self.ours.get(method)
        return float("nan") if value is None else abs(value - self.expected_pc2d)

    def relative_error(self, method: str) -> float:
        value = self.ours.get(method)
        if value is None:
            return float("nan")
        scale = max(abs(value), abs(self.expected_pc2d))
        return float("nan") if scale == 0.0 else abs(value - self.expected_pc2d) / scale

    def band(self, method: str) -> str:
        value = self.ours.get(method)
        return "n/a" if value is None else cara_tolerance_band(value, self.expected_pc2d)

    def band_passes(self, method: str) -> bool:
        return self.band(method) not in ("NoMatch", "n/a")

    def sdmc_interval_contains(self, method: str, sigma_multiple: float = 3.0) -> bool:
        """Is our Pc inside CARA's supervised Monte Carlo confidence interval?

        The interval is widened by sigma_multiple standard errors of our own
        sampling when the method is monte_carlo, since our estimate carries
        binomial noise the reference bounds know nothing about.
        """
        value = self.ours.get(method)
        lo = self.reference.get("pc_sdmc_lo", float("nan"))
        hi = self.reference.get("pc_sdmc_hi", float("nan"))
        if value is None or not (math.isfinite(lo) and math.isfinite(hi)):
            return False
        pad = 0.0
        if method == "monte_carlo" and self.monte_carlo_samples > 0:
            pad = sigma_multiple * math.sqrt(
                max(value * (1.0 - value), 0.0) / self.monte_carlo_samples
            )
        return lo - pad <= value <= hi + pad

    def to_dict(self) -> dict[str, Any]:
        methods = [m for m, v in self.ours.items() if v is not None]
        return {
            "case_id": self.case_id,
            "label": self.label,
            "hbr_m": self.hbr_m,
            "tca": self.tca.isoformat() if self.tca else None,
            "tca_shift_s": self.tca_shift_s,
            "miss_distance_m": self.miss_distance_m,
            "relative_speed_m_s": self.relative_speed_m_s,
            "reader": self.reader,
            "expected_pc2d": self.expected_pc2d,
            "ours": dict(self.ours),
            "reference": dict(self.reference),
            "violation_flag": self.violation_flag,
            "violation_reason": self.violation_reason,
            "pc2d_is_valid": self.pc2d_is_valid,
            "monte_carlo_samples": self.monte_carlo_samples,
            "absolute_error": {m: self.absolute_error(m) for m in methods},
            "relative_error": {m: self.relative_error(m) for m in methods},
            "tolerance_band": {m: self.band(m) for m in methods},
        }


def _evaluate(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hbr_km: float,
    mc_samples: int,
    mc_seed: int,
) -> dict[str, float | None]:
    ours: dict[str, float | None] = {
        "foster": pc_foster(miss_2d_km, cov_2d_km2, hbr_km),
        "chan": pc_chan(miss_2d_km, cov_2d_km2, hbr_km),
    }
    ours["monte_carlo"] = (
        pc_monte_carlo_2d(miss_2d_km, cov_2d_km2, hbr_km, n_samples=mc_samples, seed=mc_seed)
        if mc_samples > 0
        else None
    )
    ours["alfano"] = _optional_pc_alfano(miss_2d_km, cov_2d_km2, hbr_km)
    return ours


#: A binomial estimate is worthless until it expects a decent number of hits:
#: at 10 expected hits the relative standard error is already 32 percent, and
#: below about 3 the estimator returns zero more often than not. Cases whose
#: reference Pc is too small to clear this at the chosen sample count are
#: reported as "not sampled" rather than as a spurious zero.
MC_MIN_EXPECTED_HITS = 10.0
DEFAULT_MC_SAMPLES = 200_000
DEFAULT_MC_SEED = 20240101


def monte_carlo_is_resolvable(reference_pc: float, n_samples: int) -> bool:
    """Can a Monte Carlo of this size say anything useful about this Pc?"""
    return n_samples > 0 and reference_pc * n_samples >= MC_MIN_EXPECTED_HITS


def compare_case(
    row: CaraReferenceRow,
    directory: Path | None = None,
    mc_samples: int = DEFAULT_MC_SAMPLES,
    mc_seed: int = DEFAULT_MC_SEED,
) -> CaraComparison:
    """Run every available Pc method over one CARA reference conjunction."""
    base = directory or fixture_dir()
    cdm = read_cara_cdm(base / "cdms" / f"{row.conjunction_id}.cdm")
    hbr_m = row.hbr_m if math.isfinite(row.hbr_m) else (cdm.hbr_m or 0.0)

    shift, r1, v1, r2, v2 = find_true_tca(
        cdm.position1_km, cdm.velocity1_km_s, cdm.position2_km, cdm.velocity2_km_s
    )
    miss_2d, cov_2d = encounter_geometry(r1, v1, r2, v2, cdm.combined_covariance_eci_km2())
    samples = mc_samples if monte_carlo_is_resolvable(row.pc2d, mc_samples) else 0
    ours = _evaluate(miss_2d, cov_2d, hbr_m * 1e-3, samples, mc_seed)

    return CaraComparison(
        case_id=row.conjunction_id,
        label=f"{row.primary_name} vs {row.secondary_name}",
        hbr_m=hbr_m,
        tca=cdm.tca,
        tca_shift_s=shift,
        miss_distance_m=float(np.linalg.norm(r1 - r2)) * 1e3,
        relative_speed_m_s=float(np.linalg.norm(v1 - v2)) * 1e3,
        reader=cdm.reader,
        expected_pc2d=row.pc2d,
        ours=ours,
        reference={
            "pc2d": row.pc2d,
            "pc2d_no_adj": row.pc2d_no_adj,
            "nc2d": row.nc2d,
            "nc3d": row.nc3d,
            "pc3d_coppola": row.pc3d_coppola,
            "pc_sdmc": row.pc_sdmc,
            "pc_sdmc_lo": row.pc_sdmc_lo,
            "pc_sdmc_hi": row.pc_sdmc_hi,
            "cdm_stated_pc": cdm.stated_pc if cdm.stated_pc is not None else float("nan"),
        },
        violation_flag=row.violations_pc2d,
        violation_reason=row.violation_reason,
        monte_carlo_samples=samples,
    )


def compare_all(
    directory: Path | None = None,
    mc_samples: int = DEFAULT_MC_SAMPLES,
    mc_seed: int = DEFAULT_MC_SEED,
) -> list[CaraComparison]:
    """Run every one of the 53 CARA reference conjunctions."""
    base = directory or fixture_dir()
    return [
        compare_case(row, base, mc_samples=mc_samples, mc_seed=mc_seed)
        for row in load_reference_rows(base)
    ]


def alfano_comparisons(
    directory: Path | None = None,
    mc_samples: int = 0,
    mc_seed: int = DEFAULT_MC_SEED,
) -> list[CaraComparison]:
    """Run the 11 Alfano 2009 benchmark CDMs against the MATLAB literals.

    The expected values come from Pc2D_Foster_UnitTest.m lines 31-45, which
    verifies Pc2D_Foster to a relative tolerance of 1e-3 (AlfanoAccuracy).
    """
    base = directory or fixture_dir()
    literals = load_alfano_literals(base)
    out: list[CaraComparison] = []
    for case in literals["cases"]:
        cdm = read_cara_cdm(base / "sample_cdms" / str(case["cdm"]))
        hbr_m = float(case["hbr_m"])
        shift, r1, v1, r2, v2 = find_true_tca(
            cdm.position1_km, cdm.velocity1_km_s, cdm.position2_km, cdm.velocity2_km_s
        )
        miss_2d, cov_2d = encounter_geometry(r1, v1, r2, v2, cdm.combined_covariance_eci_km2())
        ours = _evaluate(miss_2d, cov_2d, hbr_m * 1e-3, mc_samples, mc_seed)
        out.append(
            CaraComparison(
                case_id=f"Alfano{case['case']}",
                label=f"A09 case {case['case']}",
                hbr_m=hbr_m,
                tca=cdm.tca,
                tca_shift_s=shift,
                miss_distance_m=float(np.linalg.norm(r1 - r2)) * 1e3,
                relative_speed_m_s=float(np.linalg.norm(v1 - v2)) * 1e3,
                reader=cdm.reader,
                expected_pc2d=float(case["expected_pc"]),
                ours=ours,
                reference={"rel_tolerance": float(case["rel_tolerance"])},
                monte_carlo_samples=mc_samples,
            )
        )
    return out


def worst_relative_errors(
    comparisons: list[CaraComparison], method: str, count: int = 5
) -> list[CaraComparison]:
    """The `count` cases where `method` is furthest from CARA, worst first."""
    scored = [c for c in comparisons if math.isfinite(c.relative_error(method))]
    scored.sort(key=lambda c: c.relative_error(method), reverse=True)
    return scored[:count]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_TABLE_METHODS = ("foster", "chan", "alfano", "monte_carlo")


def render_table(
    comparisons: list[CaraComparison],
    alfano: list[CaraComparison] | None = None,
) -> str:
    """ASCII report: one row per case, plus the divergence and summary blocks."""
    lines: list[str] = []
    header = (
        f"{'case':<44} {'HBR m':>6} {'CARA Pc2D':>12} {'foster':>12} "
        f"{'rel err':>9} {'band':>10} {'chan':>12} {'Nc3D':>12} {'PcSDMC':>12} {'2D ok':>6}"
    )
    lines.append("CARA external validation: 53 reference conjunctions")
    lines.append("=" * len(header))
    lines.append(header)
    lines.append("-" * len(header))
    for c in comparisons:
        foster = c.ours.get("foster")
        chan = c.ours.get("chan")
        lines.append(
            f"{c.case_id:<44} {c.hbr_m:>6.1f} {c.expected_pc2d:>12.5e} "
            f"{(foster if foster is not None else float('nan')):>12.5e} "
            f"{c.relative_error('foster'):>9.2e} {c.band('foster'):>10} "
            f"{(chan if chan is not None else float('nan')):>12.5e} "
            f"{c.reference.get('nc3d', float('nan')):>12.5e} "
            f"{c.reference.get('pc_sdmc', float('nan')):>12.5e} "
            f"{('yes' if c.pc2d_is_valid else 'NO'):>6}"
        )
    lines.append("-" * len(header))

    shifts = [abs(c.tca_shift_s) for c in comparisons]
    if shifts:
        lines.append(
            f"TCA shift from the millisecond-rounded CDM epoch: "
            f"max {max(shifts):.3e} s, mean {sum(shifts) / len(shifts):.3e} s"
        )

    for method in _TABLE_METHODS:
        worst = worst_relative_errors(comparisons, method)
        if not worst:
            continue
        lines.append("")
        lines.append(f"Worst 5 relative errors vs CARA Pc2D, method = {method}:")
        for c in worst:
            value = c.ours.get(method)
            lines.append(
                f"  {c.relative_error(method):>9.3e}  {c.case_id:<44} "
                f"ours {value:.5e}  CARA {c.expected_pc2d:.5e}  [{c.band(method)}]"
            )

    diverging = [
        c
        for c in comparisons
        if not c.pc2d_is_valid and math.isfinite(c.reference.get("nc3d", float("nan")))
    ]
    if diverging:
        lines.append("")
        lines.append(
            f"Cases CARA flags as outside the 2D method's validity ({len(diverging)} of "
            f"{len(comparisons)}); Nc3D is the 3D count that the 2D value misses:"
        )
        for c in sorted(
            diverging,
            key=lambda x: abs(x.reference.get("nc3d", 0.0) - x.expected_pc2d),
            reverse=True,
        )[:10]:
            nc3d = c.reference.get("nc3d", float("nan"))
            ratio = nc3d / c.expected_pc2d if c.expected_pc2d > 0 else float("nan")
            lines.append(
                f"  {c.case_id:<44} Pc2D {c.expected_pc2d:.4e}  Nc3D {nc3d:.4e}  "
                f"Nc3D/Pc2D {ratio:>9.2e}  {c.violation_reason}"
            )

    if alfano:
        lines.append("")
        lines.append("Alfano 2009 benchmark cases vs Pc2D_Foster_UnitTest.m literals")
        alf_header = (
            f"{'case':<12} {'HBR m':>6} {'expected':>13} {'foster':>13} "
            f"{'rel err':>9} {'tol':>8} {'result':>7}"
        )
        lines.append(alf_header)
        lines.append("-" * len(alf_header))
        for c in alfano:
            tol = c.reference.get("rel_tolerance", 1e-3)
            rel = c.relative_error("foster")
            lines.append(
                f"{c.case_id:<12} {c.hbr_m:>6.1f} {c.expected_pc2d:>13.6e} "
                f"{(c.ours.get('foster') or float('nan')):>13.6e} {rel:>9.2e} "
                f"{tol:>8.0e} {('PASS' if rel <= tol else 'FAIL'):>7}"
            )

    failures = [c for c in comparisons if not c.band_passes("foster")]
    alfano_failures = [
        c
        for c in (alfano or [])
        if c.relative_error("foster") > c.reference.get("rel_tolerance", 1e-3)
    ]
    lines.append("")
    lines.append(
        f"SUMMARY: {len(comparisons) - len(failures)}/{len(comparisons)} Foster cases inside "
        f"CARA's tolerance bands (widest accepted {CARA_ACCEPTANCE_TOLERANCE:.0e}); "
        f"{len(alfano or []) - len(alfano_failures)}/{len(alfano or [])} Alfano literals matched"
    )
    if failures:
        lines.append("FAILURES: " + ", ".join(c.case_id for c in failures))
    if alfano_failures:
        lines.append("ALFANO FAILURES: " + ", ".join(c.case_id for c in alfano_failures))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate our Pc implementations against the NASA CARA reference values"
    )
    parser.add_argument(
        "--mc-samples",
        type=int,
        default=DEFAULT_MC_SAMPLES,
        help=f"Monte Carlo samples per case, 0 to skip (default {DEFAULT_MC_SAMPLES})",
    )
    parser.add_argument(
        "--mc-seed", type=int, default=DEFAULT_MC_SEED, help="Monte Carlo seed (default 20240101)"
    )
    parser.add_argument(
        "--fixtures", type=Path, default=None, help="fixture directory (default tests/data/cara)"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    parser.add_argument("--no-alfano", action="store_true", help="skip the 11 Alfano cases")
    args = parser.parse_args(argv)

    base = args.fixtures or fixture_dir()
    if not (base / "pc_method_test_conjunctions.csv").exists():
        print(
            f"error: CARA fixtures not found under {base}. "
            "Run scripts/import_cara_fixtures.py first.",
            file=sys.stderr,
        )
        return 2

    comparisons = compare_all(base, mc_samples=args.mc_samples, mc_seed=args.mc_seed)
    alfano = [] if args.no_alfano else alfano_comparisons(base)

    failures = [c for c in comparisons if not c.band_passes("foster")]
    alfano_failures = [
        c for c in alfano if c.relative_error("foster") > c.reference.get("rel_tolerance", 1e-3)
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "cases": [c.to_dict() for c in comparisons],
                    "alfano": [c.to_dict() for c in alfano],
                    "foster_failures": [c.case_id for c in failures],
                    "alfano_failures": [c.case_id for c in alfano_failures],
                    "all_passed": not failures and not alfano_failures,
                },
                indent=2,
                default=float,
            )
        )
    else:
        print(render_table(comparisons, alfano))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
