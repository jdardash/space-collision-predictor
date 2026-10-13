"""Catalog-scale screening helpers: shell prefilter, spatial index, regimes, scoring.

Frame conventions match the rest of the package: every position is Earth-Centered
Inertial (TEME, as produced by SGP4) in km, every velocity in km/s. Orbital
elements read here are the TLE *mean* elements under the WGS72 gravity model
(sda.constants), never osculating elements.

This module is deliberately free of any runtime import from sda.models so that
sda.models can import ``classify_regime`` without an import cycle. Typing-only
imports are guarded by TYPE_CHECKING.

Adapted from satguard (MIT) for the apogee/perigee shell prefilter and the idea
of a per-step spatial index over vectorized SGP4 output, and from orbveil
(Apache-2.0) for the formation exclusion table and the composite risk score
design. Both were reimplemented against this project's models and conventions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import numpy as np

from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sda.models import ConjunctionEvent

try:  # pragma: no cover - exercised only where scipy is installed
    from scipy.spatial import cKDTree as _CKDTree

    SCIPY_AVAILABLE = True
except ImportError:  # pragma: no cover - the default environment has no scipy
    _CKDTree = None  # type: ignore[assignment, misc]
    SCIPY_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Tuning constants (all documented, all overridable by callers)
# --------------------------------------------------------------------------- #

# "auto" screening picks the spatial index at or above this object count. Below
# it the per-step index costs more Python overhead than the blocked all-pairs
# distance computation saves, measured on the bundled demo catalog.
SPATIAL_AUTO_MIN_OBJECTS = 64

# Extra altitude margin added to the coarse screening threshold before two
# altitude shells are declared incapable of meeting. Covers the difference
# between TLE mean elements and the SGP4 osculating radius (J2 short-period
# terms plus drift over a multi-day window). satguard uses the same 200 km.
SHELL_DRIFT_MARGIN_KM = 200.0

# Geostationary semi-major axis (WGS72 mu, sidereal day) and the half-width of
# the band that still counts as GEO.
GEO_SEMI_MAJOR_AXIS_KM = 42164.17
GEO_BAND_HALF_WIDTH_KM = 300.0
LEO_CEILING_ALT_KM = 2000.0

# Per-regime default coarse screening threshold. GEO is wider because station
# keeping boxes are tens of km across and GEO ephemeris error is larger.
REGIME_DEFAULT_THRESHOLD_KM: dict[str, float] = {
    "LEO": 10.0,
    "MEO": 25.0,
    "GEO": 50.0,
    "HEO": 25.0,
    "OTHER": 25.0,
}

REGIMES: tuple[str, ...] = ("LEO", "MEO", "GEO", "HEO", "OTHER")

# Memory ceiling for one expansion block of the uniform grid hash.
_MAX_GRID_EXPANSION = 4_000_000
# Largest cell-key space we are willing to address with a single int64 key.
_MAX_KEY_SPAN = 1 << 62

# Largest cell index magnitude the int64 grid encoding tolerates.
_MAX_CELL_INDEX = 1e15


class TleLike(Protocol):
    """Structural type for anything carrying the four TLE fields we need."""

    norad_id: int
    name: str
    line1: str
    line2: str


# --------------------------------------------------------------------------- #
# Mean elements and the apogee / perigee shell prefilter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MeanElements:
    """TLE mean elements plus the derived altitude shell (km)."""

    mean_motion_rev_day: float
    eccentricity: float
    inclination_deg: float
    semi_major_axis_km: float
    perigee_alt_km: float
    apogee_alt_km: float
    valid: bool


_INVALID_ELEMENTS = MeanElements(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False)


def mean_elements_from_line2(line2: str) -> MeanElements:
    """Parse inclination, eccentricity and mean motion from TLE line 2.

    Columns follow the standard two-line element format. Returns an element set
    with ``valid=False`` when the line is too short or unparseable, so callers
    can degrade to "screen this object against everything".
    """
    if len(line2) < 63:
        return _INVALID_ELEMENTS
    try:
        inclination = float(line2[8:16])
        eccentricity = float("0." + line2[26:33].strip())
        mean_motion = float(line2[52:63])
    except ValueError:
        return _INVALID_ELEMENTS
    if mean_motion <= 0.0 or not 0.0 <= eccentricity < 1.0:
        return _INVALID_ELEMENTS

    n_rad_s = 2.0 * math.pi * mean_motion / 86400.0
    semi_major_axis = (EARTH_MU_KM3_S2 / (n_rad_s**2)) ** (1.0 / 3.0)
    perigee_alt = semi_major_axis * (1.0 - eccentricity) - EARTH_RADIUS_KM
    apogee_alt = semi_major_axis * (1.0 + eccentricity) - EARTH_RADIUS_KM
    return MeanElements(
        mean_motion_rev_day=mean_motion,
        eccentricity=eccentricity,
        inclination_deg=inclination,
        semi_major_axis_km=semi_major_axis,
        perigee_alt_km=perigee_alt,
        apogee_alt_km=apogee_alt,
        valid=True,
    )


def mean_elements(tle: TleLike) -> MeanElements:
    """Mean elements of a TLE record."""
    return mean_elements_from_line2(tle.line2)


def altitude_band_km(tle: TleLike) -> tuple[float, float]:
    """(perigee altitude, apogee altitude) in km above the WGS72 equator.

    An unparseable TLE yields a band spanning every plausible orbit so that the
    prefilter can never drop it.
    """
    elements = mean_elements(tle)
    if not elements.valid:
        return (0.0, 1.0e6)
    return (elements.perigee_alt_km, elements.apogee_alt_km)


def shell_bands(tles: list[TleLike]) -> tuple[np.ndarray, np.ndarray]:
    """Perigee and apogee altitude arrays (km) for a catalog, in input order."""
    bands = [altitude_band_km(t) for t in tles]
    if not bands:
        return np.zeros(0), np.zeros(0)
    arr = np.asarray(bands, dtype=np.float64)
    return arr[:, 0], arr[:, 1]


def shells_overlap(
    perigee_a: np.ndarray | float,
    apogee_a: np.ndarray | float,
    perigee_b: np.ndarray | float,
    apogee_b: np.ndarray | float,
    margin_km: float,
) -> np.ndarray | bool:
    """True when two altitude shells, grown by ``margin_km``, can intersect.

    Broadcasting-friendly: pass scalars for a single pair or arrays for a block.
    Two objects whose radial bands stay further apart than the margin can never
    be within the screening threshold, so the pair is never screened.
    """
    return (np.asarray(apogee_a) + margin_km >= np.asarray(perigee_b)) & (
        np.asarray(apogee_b) + margin_km >= np.asarray(perigee_a)
    )


def shell_prefilter_margin_km(threshold_km: float) -> float:
    """Margin used by the shell prefilter for a given coarse threshold."""
    return float(threshold_km) + SHELL_DRIFT_MARGIN_KM


# --------------------------------------------------------------------------- #
# Spatial index over one coarse time step
# --------------------------------------------------------------------------- #

# 13 of the 26 neighbour offsets (one from each antipodal pair) plus the cell
# itself: enough to visit every unordered cell pair exactly once.
_HALF_OFFSETS: tuple[tuple[int, int, int], ...] = ((0, 0, 0),) + tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dy, dz) > (0, 0, 0)
)

_EMPTY_PAIRS = np.empty((0, 2), dtype=np.int64)

# Lexicographic cell key: numpy orders structured arrays field by field, so a
# record of three int64 axes sorts and searchsorts exactly like a tuple.
_CELL_DTYPE = np.dtype([("x", np.int64), ("y", np.int64), ("z", np.int64)])


def _cell_view(cells: np.ndarray) -> np.ndarray:
    """Lexicographically ordered 1-D view of an (N, 3) int64 cell array."""
    return np.ascontiguousarray(cells).view(_CELL_DTYPE).reshape(cells.shape[0])


def brute_force_pairs_within(points: np.ndarray, radius: float) -> np.ndarray:
    """Reference O(N^2) pair search. Returns an (K, 2) int64 array with i < j."""
    n = int(points.shape[0])
    if n < 2 or radius <= 0.0:
        return _EMPTY_PAIRS.copy()
    diffs = points[:, None, :] - points[None, :, :]
    dist2 = np.einsum("ijk,ijk->ij", diffs, diffs)
    iu, ju = np.triu_indices(n, k=1)
    keep = dist2[iu, ju] <= radius * radius
    return np.stack((iu[keep], ju[keep]), axis=1).astype(np.int64)


def grid_pairs_within(points: np.ndarray, radius: float) -> np.ndarray:
    """Uniform-grid hash pair search: cell size = radius, 27-neighbour check.

    A numpy-only stand-in for ``scipy.spatial.cKDTree.query_pairs`` so the
    package keeps zero required dependencies. Points are bucketed into cells of
    edge ``radius``; any pair closer than ``radius`` must lie in the same cell or
    in one of the 26 adjacent cells, so visiting the cell itself plus 13 of the
    26 neighbours covers every unordered pair exactly once.

    Returns an (K, 2) int64 array of index pairs with i < j and distance
    <= radius, in no particular order.
    """
    n = int(points.shape[0])
    if n < 2 or radius <= 0.0:
        return _EMPTY_PAIRS.copy()

    # NaN or a coordinate spread beyond int64 cell indices: fall back to the
    # exact reference search rather than risk a corrupt cell index.
    extent = float(np.max(np.abs(points)))
    if not math.isfinite(extent) or extent / radius > _MAX_CELL_INDEX:
        return brute_force_pairs_within(points, radius)

    cells = np.floor(points / radius).astype(np.int64)
    cells -= cells.min(axis=0)
    cells += 1  # one pad cell on the low side keeps neighbour keys in range
    dims = cells.max(axis=0) + 2  # and one on the high side
    span = int(dims[0]) * int(dims[1]) * int(dims[2])

    keys: np.ndarray
    if 0 < span <= _MAX_KEY_SPAN:
        strides = np.array([int(dims[1]) * int(dims[2]), int(dims[2]), 1], dtype=np.int64)
        keys = cells @ strides

        def neighbour_key(offset: tuple[int, int, int]) -> np.ndarray:
            return keys + int(np.dot(np.asarray(offset, dtype=np.int64), strides))
    else:
        # A handful of extreme outliers (a decaying TLE that SGP4 flings
        # millions of km out) would overflow the dense key space. The
        # lexicographic cell key has no span limit, so the search stays
        # sub-quadratic instead of degrading to the brute-force fallback.
        keys = _cell_view(cells)

        def neighbour_key(offset: tuple[int, int, int]) -> np.ndarray:
            return _cell_view(cells + np.asarray(offset, dtype=np.int64))

    order = np.argsort(keys, kind="stable")
    uniq, starts, counts = np.unique(keys[order], return_index=True, return_counts=True)

    radius2 = radius * radius
    found: list[np.ndarray] = []
    for offset in _HALF_OFFSETS:
        neighbour_keys = neighbour_key(offset)
        slot = np.searchsorted(uniq, neighbour_keys)
        np.clip(slot, 0, len(uniq) - 1, out=slot)
        hit = uniq[slot] == neighbour_keys
        left_ids = np.nonzero(hit)[0]
        if left_ids.size == 0:
            continue
        cell_slots = slot[left_ids]
        for chunk in _expansion_chunks(counts[cell_slots]):
            sub = left_ids[chunk]
            sub_counts = counts[cell_slots[chunk]]
            sub_starts = starts[cell_slots[chunk]]
            total = int(sub_counts.sum())
            if total == 0:
                continue
            left = np.repeat(sub, sub_counts)
            ramp = np.arange(total, dtype=np.int64) - np.repeat(
                np.cumsum(sub_counts) - sub_counts, sub_counts
            )
            right = order[np.repeat(sub_starts, sub_counts) + ramp]
            if offset == (0, 0, 0):
                keep = left < right
                left, right = left[keep], right[keep]
                if left.size == 0:
                    continue
            diffs = points[left] - points[right]
            close = np.einsum("ij,ij->i", diffs, diffs) <= radius2
            if not np.any(close):
                continue
            lo = np.minimum(left[close], right[close])
            hi = np.maximum(left[close], right[close])
            found.append(np.stack((lo, hi), axis=1))

    if not found:
        return _EMPTY_PAIRS.copy()
    return np.concatenate(found).astype(np.int64)


def _expansion_chunks(counts: np.ndarray) -> list[np.ndarray]:
    """Split an index range so each chunk expands to at most _MAX_GRID_EXPANSION."""
    total = int(counts.sum())
    if total <= _MAX_GRID_EXPANSION:
        return [np.arange(counts.size, dtype=np.int64)]
    edges = np.cumsum(counts)
    n_chunks = int(total // _MAX_GRID_EXPANSION) + 1
    bounds = np.searchsorted(
        edges, np.linspace(0, total, n_chunks + 1)[1:], side="left"
    )
    chunks: list[np.ndarray] = []
    prev = 0
    for bound in bounds:
        stop = min(int(bound) + 1, counts.size)
        if stop > prev:
            chunks.append(np.arange(prev, stop, dtype=np.int64))
            prev = stop
    if prev < counts.size:
        chunks.append(np.arange(prev, counts.size, dtype=np.int64))
    return chunks


def query_pairs_within(
    points: np.ndarray,
    radius: float,
    backend: str = "auto",
) -> np.ndarray:
    """All index pairs within ``radius`` of each other, as an (K, 2) int64 array.

    ``backend`` is "auto" (scipy cKDTree when importable, otherwise the numpy
    uniform-grid hash), "scipy", "grid" or "brute".
    """
    if backend not in {"auto", "scipy", "grid", "brute"}:
        raise ValueError(f"unknown spatial backend {backend!r}")
    if backend == "brute":
        return brute_force_pairs_within(points, radius)
    if backend == "grid" or (backend == "auto" and not SCIPY_AVAILABLE):
        return grid_pairs_within(points, radius)
    if not SCIPY_AVAILABLE:
        raise RuntimeError("scipy backend requested but scipy is not installed")
    if int(points.shape[0]) < 2 or radius <= 0.0:
        return _EMPTY_PAIRS.copy()
    tree = _CKDTree(points)
    pairs = tree.query_pairs(radius, output_type="ndarray")
    return np.asarray(pairs, dtype=np.int64).reshape(-1, 2)


def active_spatial_backend() -> str:
    """Name of the backend "auto" would pick in this environment."""
    return "scipy" if SCIPY_AVAILABLE else "grid"


# --------------------------------------------------------------------------- #
# Orbit regime classification
# --------------------------------------------------------------------------- #


def classify_regime_from_line2(line2: str) -> str:
    """Classify an orbit into LEO / MEO / GEO / HEO / OTHER from mean elements.

    Order of tests (first match wins):

    1. unparseable elements -> OTHER
    2. eccentricity >= 0.25 -> HEO (Molniya, GTO, tundra)
    3. semi-major axis within 300 km of GEO and e < 0.1 -> GEO
    4. apogee altitude <= 2000 km -> LEO
    5. semi-major axis above the GEO band -> OTHER (graveyard and beyond)
    6. perigee altitude >= 2000 km -> MEO
    7. anything left crosses the 2000 km LEO ceiling -> HEO
    """
    elements = mean_elements_from_line2(line2)
    if not elements.valid:
        return "OTHER"
    if elements.eccentricity >= 0.25:
        return "HEO"
    geo_offset = abs(elements.semi_major_axis_km - GEO_SEMI_MAJOR_AXIS_KM)
    if geo_offset <= GEO_BAND_HALF_WIDTH_KM and elements.eccentricity < 0.1:
        return "GEO"
    if elements.apogee_alt_km <= LEO_CEILING_ALT_KM:
        return "LEO"
    if elements.semi_major_axis_km > GEO_SEMI_MAJOR_AXIS_KM + GEO_BAND_HALF_WIDTH_KM:
        return "OTHER"
    if elements.perigee_alt_km >= LEO_CEILING_ALT_KM:
        return "MEO"
    return "HEO"


def classify_regime(tle: TleLike) -> str:
    """Orbit regime of a TLE record. See classify_regime_from_line2."""
    return classify_regime_from_line2(tle.line2)


def default_threshold_for_regime(regime: str) -> float:
    """Default coarse screening threshold (km) for a regime."""
    return REGIME_DEFAULT_THRESHOLD_KM.get(regime.upper(), 10.0)


def pair_regime(regime_a: str, regime_b: str) -> str:
    """Regime label carried on an event: the shared regime, else MIXED."""
    return regime_a if regime_a == regime_b else "MIXED"


# --------------------------------------------------------------------------- #
# Formation / co-orbiting exclusion table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormationRule:
    """One known co-orbiting set.

    A pair matches when *both* objects match the rule (by NORAD id, by NORAD id
    range or by name substring) and, when ``require_same_launch`` is set, when
    both carry the same international designator launch prefix (year plus launch
    number, e.g. "26034").
    """

    key: str
    label: str
    reason: str
    norad_ids: frozenset[int] = frozenset()
    norad_ranges: tuple[tuple[int, int], ...] = ()
    name_patterns: tuple[str, ...] = ()
    require_same_launch: bool = False

    def matches_object(self, norad_id: int, name: str) -> bool:
        """True when a single object belongs to this set."""
        if norad_id in self.norad_ids:
            return True
        if any(lo <= norad_id <= hi for lo, hi in self.norad_ranges):
            return True
        upper = name.upper()
        return any(pattern in upper for pattern in self.name_patterns)


# Bundled table. Heuristic limits are documented in the module README section of
# the project docs and repeated on GET /conjunctions/exclusions:
#   - name matching depends on the catalog naming convention of the TLE source;
#     a renamed or unnamed object (TLEStore synthesises "OBJECT <id>") will not
#     match a name pattern.
#   - the ISS and CSS rules flag visitors by name, not by docking state, so a
#     departed visitor stays flagged until its name leaves the catalog.
#   - the same-launch rule flags any two objects sharing a launch, including
#     ones that have long since dispersed into genuinely independent orbits.
#   - the table is static; it does not know about new formations or dockings.
BUNDLED_FORMATION_RULES: tuple[FormationRule, ...] = (
    FormationRule(
        key="iss_complex",
        label="ISS complex",
        reason="docked_or_co_orbiting_visitors",
        norad_ids=frozenset({25544, 49044}),
        name_patterns=(
            "ISS ",
            "ZARYA",
            "NAUKA",
            "PROGRESS-MS",
            "SOYUZ-MS",
            "CREW DRAGON",
            "DRAGON CRS",
            "CYGNUS",
            "HTV-X",
            "STARLINER",
        ),
    ),
    FormationRule(
        key="css_complex",
        label="CSS (Tiangong) complex",
        reason="docked_or_co_orbiting_visitors",
        name_patterns=("CSS (", "TIANHE", "WENTIAN", "MENGTIAN", "TIANZHOU", "SHENZHOU"),
    ),
    FormationRule(
        key="tandem_x",
        label="TerraSAR-X / TanDEM-X",
        reason="close_formation_flying",
        norad_ids=frozenset({31698, 36605}),
        name_patterns=("TERRASAR", "TANDEM-X"),
    ),
    FormationRule(
        key="grace_fo",
        label="GRACE-FO A/B",
        reason="close_formation_flying",
        norad_ids=frozenset({43476, 43477}),
        name_patterns=("GRACE-FO",),
    ),
    FormationRule(
        key="swarm",
        label="Swarm A/B/C",
        reason="close_formation_flying",
        norad_ids=frozenset({39451, 39452, 39453}),
        name_patterns=("SWARM A", "SWARM B", "SWARM C"),
    ),
    FormationRule(
        key="a_train",
        label="A-train afternoon constellation",
        reason="constellation_slots",
        name_patterns=("AQUA", "AURA", "CALIPSO", "CLOUDSAT", "OCO-2", "GCOM-W1", "PARASOL"),
    ),
    FormationRule(
        key="starlink_same_launch",
        label="Starlink same-launch batch",
        reason="rideshare_dispersing",
        name_patterns=("STARLINK",),
        require_same_launch=True,
    ),
    FormationRule(
        key="oneweb_same_launch",
        label="OneWeb same-launch batch",
        reason="rideshare_dispersing",
        name_patterns=("ONEWEB",),
        require_same_launch=True,
    ),
)

FORMATION_HEURISTIC_LIMITS: tuple[str, ...] = (
    "Name patterns depend on the catalog naming of the TLE source; an object "
    "ingested without a name line is matched only by NORAD id.",
    "Station rules flag visitors by name, not by docking state, so a departed "
    "visitor keeps the flag while its name stays in the catalog.",
    "Same-launch rules flag any two objects sharing an international designator "
    "launch prefix, including batches that have already dispersed.",
    "The table is static and knows nothing about formations created after this "
    "release; supply extra rules for those.",
    "Flagged pairs are kept in the output and never dropped: the flag is an "
    "operator hint, not a screening decision.",
)


def launch_prefix(line1: str) -> str:
    """International designator launch prefix (year + launch number) from line 1.

    Columns 10-14 of TLE line 1, e.g. "1 25544U 98067A ..." -> "98067".
    """
    if len(line1) < 14:
        return ""
    return line1[9:14].strip()


def match_formation(
    norad_a: int,
    name_a: str,
    line1_a: str,
    norad_b: int,
    name_b: str,
    line1_b: str,
    rules: tuple[FormationRule, ...] | None = None,
) -> FormationRule | None:
    """First rule flagging this pair as a known co-orbiting set, else None."""
    for rule in rules if rules is not None else BUNDLED_FORMATION_RULES:
        if not (rule.matches_object(norad_a, name_a) and rule.matches_object(norad_b, name_b)):
            continue
        if rule.require_same_launch:
            prefix_a = launch_prefix(line1_a)
            if not prefix_a or prefix_a != launch_prefix(line1_b):
                continue
        return rule
    return None


def formation_rules_with_extras(
    extra: list[FormationRule] | tuple[FormationRule, ...] | None,
) -> tuple[FormationRule, ...]:
    """Bundled table followed by any user-supplied rules."""
    if not extra:
        return BUNDLED_FORMATION_RULES
    return BUNDLED_FORMATION_RULES + tuple(extra)


def formation_rule_to_dict(rule: FormationRule) -> dict[str, object]:
    """JSON-serializable view of a rule, for the API exclusions endpoint."""
    return {
        "key": rule.key,
        "label": rule.label,
        "reason": rule.reason,
        "norad_ids": sorted(rule.norad_ids),
        "norad_ranges": [list(r) for r in rule.norad_ranges],
        "name_patterns": list(rule.name_patterns),
        "require_same_launch": rule.require_same_launch,
    }


# --------------------------------------------------------------------------- #
# Composite risk score (distinct from conjunction.classify_risk)
# --------------------------------------------------------------------------- #

# Component weights. They sum to 1.0 and each component is on 0-100, so the
# composite score is on 0-100 by construction.
RISK_WEIGHT_PROXIMITY = 0.55
RISK_WEIGHT_URGENCY = 0.20
RISK_WEIGHT_SIZE = 0.15
RISK_WEIGHT_MANEUVER = 0.10

# Proximity from Pc: 1e-3 and above saturates, 1e-9 and below scores zero,
# linear in log10 between (the decades operators actually act on).
PC_SCORE_MAX_EXPONENT = -3.0
PC_SCORE_MIN_EXPONENT = -9.0

# Proximity from miss distance when no Pc is available: halves every 2 km.
MISS_SCORE_HALF_LIFE_KM = 2.0

# Urgency: at or inside 2 h scores 100, at or beyond 72 h scores 0, linear
# between. A TCA already in the past scores 0 (nothing left to decide).
URGENCY_FULL_HOURS = 2.0
URGENCY_ZERO_HOURS = 72.0

SIZE_CLASS_SCORES: dict[str, float] = {
    "small": 30.0,
    "medium": 60.0,
    "large": 100.0,
    "unknown": 50.0,
}

# Maneuverability: an unmaneuverable pair cannot be resolved by anyone.
MANEUVER_SCORE_NEITHER = 100.0
MANEUVER_SCORE_ONE = 50.0
MANEUVER_SCORE_BOTH = 20.0

_DEBRIS_TOKENS = ("DEB", "DEBRIS", "FRAGMENT", "SHRAPNEL")
_ROCKET_BODY_TOKENS = ("R/B", "ROCKET BODY", "AKM", "PKM")


@dataclass(frozen=True)
class RiskScoreBreakdown:
    """Component scores behind a composite risk score."""

    score: float
    proximity: float
    urgency: float
    size: float
    maneuver: float
    hours_to_tca: float | None
    proximity_source: str
    primary_class: str
    secondary_class: str
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "proximity": RISK_WEIGHT_PROXIMITY,
            "urgency": RISK_WEIGHT_URGENCY,
            "size": RISK_WEIGHT_SIZE,
            "maneuver": RISK_WEIGHT_MANEUVER,
        }
    )


def object_class(name: str) -> str:
    """Coarse object class from the catalog name: DEBRIS, ROCKET_BODY or PAYLOAD.

    Purely a name heuristic. CelesTrak style names end debris with "DEB" and
    rocket bodies with "R/B"; anything else is assumed to be a payload.
    """
    upper = name.upper()
    if any(token in upper for token in _DEBRIS_TOKENS):
        return "DEBRIS"
    if any(token in upper for token in _ROCKET_BODY_TOKENS):
        return "ROCKET_BODY"
    return "PAYLOAD"


def is_maneuverable_name(name: str) -> bool:
    """Assume active payloads can maneuver and debris or rocket bodies cannot."""
    return object_class(name) == "PAYLOAD"


def proximity_score(
    probability: float | None,
    miss_distance_km: float,
) -> tuple[float, str]:
    """Proximity component on 0-100 plus the source used ("pc" or "miss_distance")."""
    if probability is not None and probability > 0.0:
        exponent = math.log10(probability)
        span = PC_SCORE_MAX_EXPONENT - PC_SCORE_MIN_EXPONENT
        raw = 100.0 * (exponent - PC_SCORE_MIN_EXPONENT) / span
        return (_clamp(raw, 0.0, 100.0), "pc")
    if probability is not None:
        # An explicit Pc of exactly zero is a real answer: no proximity risk.
        return (0.0, "pc")
    miss = max(0.0, float(miss_distance_km))
    raw = 100.0 * math.exp(-math.log(2.0) * miss / MISS_SCORE_HALF_LIFE_KM)
    return (_clamp(raw, 0.0, 100.0), "miss_distance")


def urgency_score(hours_to_tca: float | None) -> float:
    """Urgency component on 0-100 from the time remaining until TCA."""
    if hours_to_tca is None:
        return 0.0
    if hours_to_tca <= 0.0:
        return 0.0
    if hours_to_tca <= URGENCY_FULL_HOURS:
        return 100.0
    if hours_to_tca >= URGENCY_ZERO_HOURS:
        return 0.0
    span = URGENCY_ZERO_HOURS - URGENCY_FULL_HOURS
    return _clamp(100.0 * (URGENCY_ZERO_HOURS - hours_to_tca) / span, 0.0, 100.0)


def size_score(primary_size: str, secondary_size: str) -> float:
    """Size component on 0-100: the worse (larger) of the two RCS classes."""
    a = SIZE_CLASS_SCORES.get(primary_size.strip().lower(), SIZE_CLASS_SCORES["unknown"])
    b = SIZE_CLASS_SCORES.get(secondary_size.strip().lower(), SIZE_CLASS_SCORES["unknown"])
    return max(a, b)


def maneuver_score(primary_maneuverable: bool, secondary_maneuverable: bool) -> float:
    """Maneuverability component on 0-100: fewer maneuverable objects, higher risk."""
    if primary_maneuverable and secondary_maneuverable:
        return MANEUVER_SCORE_BOTH
    if primary_maneuverable or secondary_maneuverable:
        return MANEUVER_SCORE_ONE
    return MANEUVER_SCORE_NEITHER


def risk_score_breakdown(
    event: ConjunctionEvent,
    now: datetime | None = None,
    primary_size: str = "unknown",
    secondary_size: str = "unknown",
    primary_maneuverable: bool | None = None,
    secondary_maneuverable: bool | None = None,
) -> RiskScoreBreakdown:
    """Composite 0-100 risk score with its component breakdown.

    Pure function: nothing here reads the clock unless ``now`` is omitted, and
    nothing mutates the event. This score is advisory and is deliberately
    separate from ``conjunction.classify_risk``, whose thresholds are the
    safety-critical ones.

    Weights: proximity 0.55, urgency 0.20, size 0.15, maneuverability 0.10.
    Proximity uses collision probability when the event carries one and falls
    back to miss distance otherwise. Size classes are "small", "medium",
    "large" or "unknown" (RCS class). Maneuverability defaults to a name
    heuristic: "DEB" or "R/B" in the name means the object cannot maneuver.
    """
    pc = None
    if event.collision_probability is not None:
        pc = float(event.collision_probability.probability)
    proximity, source = proximity_score(pc, event.miss_distance_km)

    hours = _hours_to_tca(event.tca, now)
    urgency = urgency_score(hours)
    size = size_score(primary_size, secondary_size)

    primary_class = object_class(event.primary_name)
    secondary_class = object_class(event.secondary_name)
    can_a = (
        is_maneuverable_name(event.primary_name)
        if primary_maneuverable is None
        else primary_maneuverable
    )
    can_b = (
        is_maneuverable_name(event.secondary_name)
        if secondary_maneuverable is None
        else secondary_maneuverable
    )
    maneuver = maneuver_score(can_a, can_b)

    total = (
        RISK_WEIGHT_PROXIMITY * proximity
        + RISK_WEIGHT_URGENCY * urgency
        + RISK_WEIGHT_SIZE * size
        + RISK_WEIGHT_MANEUVER * maneuver
    )
    return RiskScoreBreakdown(
        score=round(_clamp(total, 0.0, 100.0), 2),
        proximity=round(proximity, 2),
        urgency=round(urgency, 2),
        size=round(size, 2),
        maneuver=round(maneuver, 2),
        hours_to_tca=None if hours is None else round(hours, 4),
        proximity_source=source,
        primary_class=primary_class,
        secondary_class=secondary_class,
    )


def risk_score(
    event: ConjunctionEvent,
    now: datetime | None = None,
    primary_size: str = "unknown",
    secondary_size: str = "unknown",
    primary_maneuverable: bool | None = None,
    secondary_maneuverable: bool | None = None,
) -> float:
    """Composite 0-100 risk score. See risk_score_breakdown for the weights."""
    return risk_score_breakdown(
        event,
        now=now,
        primary_size=primary_size,
        secondary_size=secondary_size,
        primary_maneuverable=primary_maneuverable,
        secondary_maneuverable=secondary_maneuverable,
    ).score


def _hours_to_tca(tca: datetime, now: datetime | None) -> float | None:
    """Hours from ``now`` (default: current UTC) to TCA."""
    reference = datetime.now(UTC) if now is None else now
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    moment = tca if tca.tzinfo else tca.replace(tzinfo=UTC)
    return (moment - reference).total_seconds() / 3600.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
