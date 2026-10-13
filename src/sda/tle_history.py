"""Historical TLE snapshot store, maneuver detection and decay anomaly flags.

A TLE catalog only ever holds the newest element set per object. Keeping the
older ones lets the service answer questions the live catalog cannot: when did
this object change orbit, how large was the burn, and is it decaying faster
than the drag model says it should.

Storage
-------
Snapshots are keyed by NORAD id and element-set epoch. The store is in memory
by default. When a directory is supplied (constructor argument, else the
SDA_HISTORY_DIR environment variable) every snapshot is also appended to
``<directory>/<norad_id>.jsonl`` and existing files are read back on startup,
so history survives a restart without adding a database dependency.

Maneuver detection
------------------
Two consecutive element sets are compared in mean elements taken straight from
the TLE (Brouwer/Kozai mean motion, eccentricity, inclination):

* semi-major axis, after removing the decay the drag model expects over the
  same interval (sda.decay.estimate_decay). What is left is the unexplained
  in-plane change.
* inclination and eccentricity, which drag changes only very slowly.

Each residual series is scored against a robust sigma (median absolute
deviation, scaled by 1.4826) with an absolute floor, so a quiet series with a
near-zero spread cannot manufacture detections and a single burn cannot inflate
the spread that is meant to catch it.

Delta-V is estimated from vis-viva. For a small change of semi-major axis at
mean radius r = a,

    v = sqrt(mu / a),  dv_along = v * |da| / (2 a)

which is the standard tangential-burn sensitivity dv/da = mu / (2 a^2 v). A
plane change of di needs dv_cross = 2 v sin(di / 2), and a small eccentricity
change needs about dv_radial = v |de| / 2 for a single impulse at an apsis.
These three are lower bounds on separate components of the same maneuver, so
the reported total is their root sum of squares.

Decay anomaly
-------------
The observed mean-motion drift (n2 - n1) / dt is compared with the drift the
decay model predicts from the drag term, using n = sqrt(mu / a^3), so

    dn/dt = -1.5 * (n / a) * da/dt

with da/dt the modelled decay rate. A large ratio, a sign flip on a decaying
orbit, or a big jump in the drag term itself raises the flag.

References
----------
[1] D. A. Vallado, "Fundamentals of Astrodynamics and Applications", 4th ed.,
    Microcosm Press, 2013, sections 2.2 (vis-viva) and 9.6 (element
    differencing for maneuver reconstruction).
[2] F. R. Hoots and R. L. Roehrich, "Models for Propagation of NORAD Element
    Sets", Spacetrack Report No. 3, 1980 (definition of the TLE mean elements
    used here).
[3] T. Kelso, "Frequently Asked Questions: Two-Line Element Set Format",
    CelesTrak, on the meaning of the first derivative of mean motion field.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sgp4.api import WGS72, Satrec

from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.models import TLERecord
from sda.tle_store import TLEStore

MINUTES_PER_DAY = 1440.0
SECONDS_PER_DAY = 86400.0
TWO_PI = 2.0 * math.pi

#: Default number of robust sigmas a residual must exceed to be called a burn.
DEFAULT_SIGMA_THRESHOLD = 3.0
#: Absolute floors. Below these a change is treated as element-set noise even
#: when the series happens to be very quiet. Chosen from typical public TLE
#: element scatter for a well tracked object.
MIN_SMA_RESIDUAL_KM = 0.30
MIN_INCLINATION_CHANGE_DEG = 0.01
MIN_ECCENTRICITY_CHANGE = 5.0e-5
#: Ignore pairs closer together than this; consecutive element sets from the
#: same fit carry correlated noise rather than dynamics.
MIN_INTERVAL_DAYS = 0.02
#: Decay anomaly gates.
DECAY_RATIO_THRESHOLD = 5.0
MIN_MEAN_MOTION_RATE_REV_DAY2 = 1.0e-6
BSTAR_CHANGE_THRESHOLD = 0.5

HISTORY_DIR_ENV = "SDA_HISTORY_DIR"


class TLESnapshot(BaseModel):
    """One archived element set."""

    norad_id: int
    name: str
    line1: str
    line2: str
    epoch: datetime
    recorded_at: datetime

    def to_record(self) -> TLERecord:
        return TLERecord(
            norad_id=self.norad_id,
            name=self.name,
            line1=self.line1,
            line2=self.line2,
            epoch=self.epoch,
        )

    def tle_text(self) -> str:
        return f"{self.name}\n{self.line1}\n{self.line2}"


class MeanElements(BaseModel):
    """Mean elements read directly out of a TLE, plus derived quantities."""

    epoch: datetime
    mean_motion_rev_day: float
    semi_major_axis_km: float
    eccentricity: float
    inclination_deg: float
    raan_deg: float
    arg_perigee_deg: float
    perigee_km: float
    apogee_km: float
    bstar: float
    ndot_rev_day2: float


class ManeuverEvent(BaseModel):
    """An unexplained orbit change between two consecutive element sets."""

    norad_id: int
    start_epoch: datetime
    end_epoch: datetime
    interval_days: float
    delta_semi_major_axis_km: float
    expected_decay_km: float
    residual_semi_major_axis_km: float
    delta_inclination_deg: float
    delta_eccentricity: float
    sma_sigma_km: float
    inclination_sigma_deg: float
    eccentricity_sigma: float
    delta_v_along_track_m_s: float
    delta_v_cross_track_m_s: float
    delta_v_radial_m_s: float
    delta_v_total_m_s: float
    confidence: str
    score: float
    reasons: list[str] = Field(default_factory=list)


class DecayAnomaly(BaseModel):
    """A mean-motion drift or drag term that departs from the decay model."""

    norad_id: int
    start_epoch: datetime
    end_epoch: datetime
    interval_days: float
    observed_mean_motion_rate_rev_day2: float
    expected_mean_motion_rate_rev_day2: float
    ratio: float
    bstar_start: float
    bstar_end: float
    bstar_change_fraction: float
    ndot_start_rev_day2: float
    ndot_end_rev_day2: float
    ndot_change_fraction: float
    reasons: list[str] = Field(default_factory=list)


class OrbitChangeEntry(BaseModel):
    """One line of the orbit change log."""

    norad_id: int
    start_epoch: datetime
    end_epoch: datetime
    kind: str
    summary: str
    delta_v_m_s: float = 0.0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ndot_rev_day2(satrec: Satrec) -> float:
    """First derivative of mean motion in rev/day^2.

    The sgp4 Satrec stores the TLE field (which is dn/dt divided by two) in
    rad/min^2. Undo both conversions. Reference [3].
    """
    return 2.0 * float(satrec.ndot) * MINUTES_PER_DAY * MINUTES_PER_DAY / TWO_PI


def semi_major_axis_km(mean_motion_rev_day: float) -> float:
    """Mean semi-major axis from mean motion, a = (mu / n^2)^(1/3)."""
    if mean_motion_rev_day <= 0.0:
        raise ValueError("mean_motion_rev_day must be positive")
    n_rad_s = mean_motion_rev_day * TWO_PI / SECONDS_PER_DAY
    return float((EARTH_MU_KM3_S2 / (n_rad_s * n_rad_s)) ** (1.0 / 3.0))


def mean_elements(snapshot: TLESnapshot) -> MeanElements:
    """Extract mean elements from an archived element set."""
    satrec = Satrec.twoline2rv(snapshot.line1, snapshot.line2, WGS72)
    n_rev_day = float(satrec.no_kozai) * MINUTES_PER_DAY / TWO_PI
    sma = semi_major_axis_km(n_rev_day)
    ecc = float(satrec.ecco)
    return MeanElements(
        epoch=_utc(snapshot.epoch),
        mean_motion_rev_day=n_rev_day,
        semi_major_axis_km=sma,
        eccentricity=ecc,
        inclination_deg=math.degrees(float(satrec.inclo)),
        raan_deg=math.degrees(float(satrec.nodeo)),
        arg_perigee_deg=math.degrees(float(satrec.argpo)),
        perigee_km=sma * (1.0 - ecc) - EARTH_RADIUS_KM,
        apogee_km=sma * (1.0 + ecc) - EARTH_RADIUS_KM,
        bstar=float(satrec.bstar),
        ndot_rev_day2=ndot_rev_day2(satrec),
    )


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def robust_sigma(values: Sequence[float]) -> float:
    """Median absolute deviation scaled to a Gaussian sigma.

    Returns 0.0 for fewer than three samples, which pushes the caller onto its
    absolute floor instead of a meaningless spread.
    """
    if len(values) < 3:
        return 0.0
    med = _median(values)
    mad = _median([abs(value - med) for value in values])
    return 1.4826 * mad


def _expected_decay_km(snapshot: TLESnapshot, days: float, f107: float) -> float:
    """Semi-major axis loss the drag model expects over ``days``.

    Positive means the orbit is expected to shrink. Degrades to zero if the
    decay model cannot handle the element set.
    """
    try:
        from sda.decay import estimate_decay

        estimate = estimate_decay(snapshot.to_record(), f107_solar_flux=f107)
        rate = float(estimate.decay_rate_km_per_day)
    except Exception:
        return 0.0
    if not math.isfinite(rate):
        return 0.0
    return abs(rate) * days


def _circular_speed_m_s(sma_km: float) -> float:
    return 1000.0 * math.sqrt(EARTH_MU_KM3_S2 / sma_km)


def delta_v_from_sma_change(sma_km: float, delta_sma_km: float) -> float:
    """Tangential delta-V in m/s for a small semi-major axis change."""
    if sma_km <= 0.0:
        return 0.0
    return _circular_speed_m_s(sma_km) * abs(delta_sma_km) / (2.0 * sma_km)


def delta_v_from_inclination_change(sma_km: float, delta_inc_deg: float) -> float:
    """Cross-track delta-V in m/s for a small plane change."""
    if sma_km <= 0.0:
        return 0.0
    return 2.0 * _circular_speed_m_s(sma_km) * abs(math.sin(math.radians(delta_inc_deg) / 2.0))


def delta_v_from_eccentricity_change(sma_km: float, delta_ecc: float) -> float:
    """Single-impulse delta-V in m/s for a small eccentricity change."""
    if sma_km <= 0.0:
        return 0.0
    return 0.5 * _circular_speed_m_s(sma_km) * abs(delta_ecc)


def _pairs(history: Sequence[TLESnapshot]) -> list[tuple[TLESnapshot, TLESnapshot]]:
    ordered = sorted(history, key=lambda snap: _utc(snap.epoch))
    return [(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)]


def detect_maneuvers(
    history: Sequence[TLESnapshot],
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD,
    min_sma_residual_km: float = MIN_SMA_RESIDUAL_KM,
    min_inclination_deg: float = MIN_INCLINATION_CHANGE_DEG,
    min_eccentricity: float = MIN_ECCENTRICITY_CHANGE,
    f107_solar_flux: float = 150.0,
) -> list[ManeuverEvent]:
    """Flag orbit changes between consecutive element sets that drag cannot explain.

    The series is walked once to build residuals, once more to score them
    against the robust spread of the whole series. Returns the flagged
    intervals in epoch order.
    """
    pairs = _pairs(history)
    if not pairs:
        return []

    rows: list[dict[str, Any]] = []
    for earlier, later in pairs:
        start = _utc(earlier.epoch)
        end = _utc(later.epoch)
        days = (end - start).total_seconds() / SECONDS_PER_DAY
        if days < MIN_INTERVAL_DAYS:
            continue
        try:
            first = mean_elements(earlier)
            second = mean_elements(later)
        except Exception:
            continue
        expected = _expected_decay_km(earlier, days, f107_solar_flux)
        d_sma = second.semi_major_axis_km - first.semi_major_axis_km
        rows.append(
            {
                "start": start,
                "end": end,
                "days": days,
                "first": first,
                "second": second,
                "d_sma": d_sma,
                "expected": expected,
                # Expected change is negative (the orbit shrinks), so adding the
                # expected loss back removes the modelled trend.
                "residual": d_sma + expected,
                "d_inc": second.inclination_deg - first.inclination_deg,
                "d_ecc": second.eccentricity - first.eccentricity,
            }
        )

    if not rows:
        return []

    sma_sigma = robust_sigma([row["residual"] for row in rows])
    inc_sigma = robust_sigma([row["d_inc"] for row in rows])
    ecc_sigma = robust_sigma([row["d_ecc"] for row in rows])

    sma_limit = max(min_sma_residual_km, sigma_threshold * sma_sigma)
    inc_limit = max(min_inclination_deg, sigma_threshold * inc_sigma)
    ecc_limit = max(min_eccentricity, sigma_threshold * ecc_sigma)

    events: list[ManeuverEvent] = []
    for row in rows:
        base: MeanElements = row["first"]
        residual = float(row["residual"])
        d_inc = float(row["d_inc"])
        d_ecc = float(row["d_ecc"])
        reasons: list[str] = []
        scores: list[float] = []
        if abs(residual) > sma_limit:
            reasons.append(
                f"semi-major axis residual {residual:+.3f} km over limit {sma_limit:.3f} km"
            )
            scores.append(abs(residual) / sma_limit)
        if abs(d_inc) > inc_limit:
            reasons.append(
                f"inclination change {d_inc:+.4f} deg over limit {inc_limit:.4f} deg"
            )
            scores.append(abs(d_inc) / inc_limit)
        if abs(d_ecc) > ecc_limit:
            reasons.append(
                f"eccentricity change {d_ecc:+.3e} over limit {ecc_limit:.3e}"
            )
            scores.append(abs(d_ecc) / ecc_limit)
        if not reasons:
            continue

        sma = base.semi_major_axis_km
        dv_along = delta_v_from_sma_change(sma, residual)
        dv_cross = delta_v_from_inclination_change(sma, d_inc)
        dv_radial = delta_v_from_eccentricity_change(sma, d_ecc)
        total = math.sqrt(dv_along**2 + dv_cross**2 + dv_radial**2)
        score = max(scores)
        if score >= 5.0 and len(reasons) >= 2:
            confidence = "HIGH"
        elif score >= 5.0:
            confidence = "MODERATE"
        else:
            confidence = "LOW"

        events.append(
            ManeuverEvent(
                norad_id=history[0].norad_id,
                start_epoch=row["start"],
                end_epoch=row["end"],
                interval_days=float(row["days"]),
                delta_semi_major_axis_km=float(row["d_sma"]),
                expected_decay_km=float(row["expected"]),
                residual_semi_major_axis_km=residual,
                delta_inclination_deg=d_inc,
                delta_eccentricity=d_ecc,
                sma_sigma_km=sma_sigma,
                inclination_sigma_deg=inc_sigma,
                eccentricity_sigma=ecc_sigma,
                delta_v_along_track_m_s=dv_along,
                delta_v_cross_track_m_s=dv_cross,
                delta_v_radial_m_s=dv_radial,
                delta_v_total_m_s=total,
                confidence=confidence,
                score=score,
                reasons=reasons,
            )
        )
    return events


def _relative_change(start: float, end: float) -> float:
    """Fractional change of a drag term, zero when the baseline is negligible."""
    if abs(start) <= 1.0e-12:
        return 0.0
    return (end - start) / abs(start)


def expected_mean_motion_rate(
    elements: MeanElements, decay_rate_km_per_day: float
) -> float:
    """Convert a modelled semi-major axis decay rate to rev/day^2.

    n = sqrt(mu / a^3) gives dn/dt = -1.5 (n / a) da/dt, so a shrinking orbit
    (negative da/dt) produces a positive mean-motion drift.
    """
    if elements.semi_major_axis_km <= 0.0:
        return 0.0
    return (
        1.5
        * elements.mean_motion_rev_day
        / elements.semi_major_axis_km
        * abs(decay_rate_km_per_day)
    )


def detect_decay_anomalies(
    history: Sequence[TLESnapshot],
    ratio_threshold: float = DECAY_RATIO_THRESHOLD,
    min_rate_rev_day2: float = MIN_MEAN_MOTION_RATE_REV_DAY2,
    bstar_change_threshold: float = BSTAR_CHANGE_THRESHOLD,
    f107_solar_flux: float = 150.0,
) -> list[DecayAnomaly]:
    """Flag intervals where the drag term or the mean-motion drift misbehaves."""
    anomalies: list[DecayAnomaly] = []
    for earlier, later in _pairs(history):
        start = _utc(earlier.epoch)
        end = _utc(later.epoch)
        days = (end - start).total_seconds() / SECONDS_PER_DAY
        if days < MIN_INTERVAL_DAYS:
            continue
        try:
            first = mean_elements(earlier)
            second = mean_elements(later)
        except Exception:
            continue

        observed = (second.mean_motion_rev_day - first.mean_motion_rev_day) / days
        expected_km = _expected_decay_km(earlier, 1.0, f107_solar_flux)
        expected = expected_mean_motion_rate(first, expected_km)
        floor = max(min_rate_rev_day2, abs(expected))
        ratio = abs(observed) / floor if floor > 0.0 else 0.0

        bstar_fraction = _relative_change(first.bstar, second.bstar)
        ndot_fraction = _relative_change(first.ndot_rev_day2, second.ndot_rev_day2)

        reasons: list[str] = []
        if abs(observed) > min_rate_rev_day2 and ratio > ratio_threshold:
            reasons.append(
                f"mean motion drift {observed:+.3e} rev/day^2 is {ratio:.1f}x the "
                f"modelled {expected:.3e}"
            )
        if observed < -min_rate_rev_day2 and expected > min_rate_rev_day2:
            reasons.append(
                "mean motion is falling while the drag model expects it to rise"
            )
        if abs(bstar_fraction) > bstar_change_threshold:
            reasons.append(
                f"drag term changed by {100.0 * bstar_fraction:+.0f} percent "
                f"({first.bstar:.4e} to {second.bstar:.4e})"
            )
        if abs(ndot_fraction) > bstar_change_threshold:
            reasons.append(
                f"mean motion derivative changed by {100.0 * ndot_fraction:+.0f} percent "
                f"({first.ndot_rev_day2:.4e} to {second.ndot_rev_day2:.4e})"
            )
        if not reasons:
            continue

        anomalies.append(
            DecayAnomaly(
                norad_id=history[0].norad_id,
                start_epoch=start,
                end_epoch=end,
                interval_days=days,
                observed_mean_motion_rate_rev_day2=observed,
                expected_mean_motion_rate_rev_day2=expected,
                ratio=ratio,
                bstar_start=first.bstar,
                bstar_end=second.bstar,
                bstar_change_fraction=bstar_fraction,
                ndot_start_rev_day2=first.ndot_rev_day2,
                ndot_end_rev_day2=second.ndot_rev_day2,
                ndot_change_fraction=ndot_fraction,
                reasons=reasons,
            )
        )
    return anomalies


def orbit_change_log(
    history: Sequence[TLESnapshot],
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD,
    f107_solar_flux: float = 150.0,
) -> list[OrbitChangeEntry]:
    """Merge maneuvers and decay anomalies into one epoch-ordered summary.

    An interval already explained by a maneuver is not reported a second time
    as a decay anomaly: a burn changes the mean motion, so it would otherwise
    always show up twice.
    """
    entries: list[OrbitChangeEntry] = []
    maneuver_intervals: set[tuple[datetime, datetime]] = set()
    for event in detect_maneuvers(
        history, sigma_threshold=sigma_threshold, f107_solar_flux=f107_solar_flux
    ):
        maneuver_intervals.add((event.start_epoch, event.end_epoch))
        entries.append(
            OrbitChangeEntry(
                norad_id=event.norad_id,
                start_epoch=event.start_epoch,
                end_epoch=event.end_epoch,
                kind="maneuver",
                summary=(
                    f"{event.confidence} confidence maneuver, "
                    f"da {event.residual_semi_major_axis_km:+.3f} km, "
                    f"di {event.delta_inclination_deg:+.4f} deg, "
                    f"dv {event.delta_v_total_m_s:.3f} m/s"
                ),
                delta_v_m_s=event.delta_v_total_m_s,
            )
        )
    for anomaly in detect_decay_anomalies(
        history, f107_solar_flux=f107_solar_flux
    ):
        if (anomaly.start_epoch, anomaly.end_epoch) in maneuver_intervals:
            continue
        entries.append(
            OrbitChangeEntry(
                norad_id=anomaly.norad_id,
                start_epoch=anomaly.start_epoch,
                end_epoch=anomaly.end_epoch,
                kind="decay_anomaly",
                summary="decay anomaly: " + "; ".join(anomaly.reasons),
            )
        )
    entries.sort(key=lambda entry: (entry.start_epoch, entry.kind))
    return entries


def format_change_log(entries: Sequence[OrbitChangeEntry]) -> str:
    """Render the change log as plain ASCII lines."""
    lines = []
    for entry in entries:
        lines.append(
            f"{entry.start_epoch.isoformat()} to {entry.end_epoch.isoformat()} "
            f"[{entry.norad_id}] {entry.kind}: {entry.summary}"
        )
    return "\n".join(lines)


def split_tle_blocks(raw: str) -> list[str]:
    """Split raw TLE text into one block per element set.

    Accepts the two-line and three-line forms and tolerates blank lines. Each
    returned block is ready for TLEStore.load_from_text.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        text = line.rstrip()
        if not text.strip():
            continue
        if text.startswith("2 ") and current:
            current.append(text)
            blocks.append("\n".join(current))
            current = []
        elif text.startswith("1 "):
            if current and current[-1].startswith("1 "):
                current = []
            current.append(text)
        else:
            current = [text]
    return blocks


class TLEHistoryStore:
    """Archive of element sets keyed by NORAD id and epoch.

    In memory by default. Pass ``directory`` (or set SDA_HISTORY_DIR) to append
    every snapshot to a JSONL file per object and read those files back on
    construction.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        load_existing: bool = True,
    ) -> None:
        self._snapshots: dict[int, dict[str, TLESnapshot]] = {}
        resolved = directory if directory is not None else os.environ.get(HISTORY_DIR_ENV)
        self._directory: Path | None = Path(resolved) if resolved else None
        if self._directory is not None:
            self._directory.mkdir(parents=True, exist_ok=True)
            if load_existing:
                self.load_persisted()

    @property
    def directory(self) -> Path | None:
        return self._directory

    @property
    def persistent(self) -> bool:
        return self._directory is not None

    def _path_for(self, norad_id: int) -> Path | None:
        if self._directory is None:
            return None
        return self._directory / f"{norad_id}.jsonl"

    def load_persisted(self) -> int:
        """Read every JSONL file in the directory. Returns snapshots loaded."""
        if self._directory is None or not self._directory.is_dir():
            return 0
        loaded = 0
        for path in sorted(self._directory.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                        snapshot = TLESnapshot(**payload)
                    except Exception:
                        continue
                    if self._insert(snapshot):
                        loaded += 1
        return loaded

    def _insert(self, snapshot: TLESnapshot) -> bool:
        by_epoch = self._snapshots.setdefault(snapshot.norad_id, {})
        key = _utc(snapshot.epoch).isoformat()
        existing = by_epoch.get(key)
        if existing is not None and (
            existing.line1 == snapshot.line1 and existing.line2 == snapshot.line2
        ):
            return False
        by_epoch[key] = snapshot
        return True

    def _persist(self, snapshot: TLESnapshot) -> None:
        path = self._path_for(snapshot.norad_id)
        if path is None:
            return
        payload = {
            "norad_id": snapshot.norad_id,
            "name": snapshot.name,
            "line1": snapshot.line1,
            "line2": snapshot.line2,
            "epoch": _utc(snapshot.epoch).isoformat(),
            "recorded_at": _utc(snapshot.recorded_at).isoformat(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def add(self, tle: TLERecord, recorded_at: datetime | None = None) -> bool:
        """Archive one element set. Returns True when it was new."""
        snapshot = TLESnapshot(
            norad_id=tle.norad_id,
            name=tle.name,
            line1=tle.line1,
            line2=tle.line2,
            epoch=_utc(tle.epoch),
            recorded_at=_utc(recorded_at or datetime.now(UTC)),
        )
        if not self._insert(snapshot):
            return False
        self._persist(snapshot)
        return True

    def add_many(self, tles: Iterable[TLERecord]) -> int:
        return sum(1 for tle in tles if self.add(tle))

    def add_from_text(self, raw: str) -> int:
        """Archive element sets from raw TLE text.

        Ingestion goes through TLEStore.load_from_text so the lines are
        validated exactly the way the live catalog validates them. The text is
        split into individual element sets first: a live catalog keeps one
        record per object, so several epochs of the same object would otherwise
        overwrite each other before they could be archived.
        """
        added = 0
        for block in split_tle_blocks(raw):
            staging = TLEStore()
            try:
                staging.load_from_text(block)
            except ValueError:
                continue
            added += self.add_many(staging.get_all())
        return added

    def record_snapshot(self, store: TLEStore) -> int:
        """Archive the current contents of a live catalog. Returns new snapshots."""
        return self.add_many(store.get_all())

    def history(self, norad_id: int) -> list[TLESnapshot]:
        """Every archived element set for one object, oldest first."""
        by_epoch = self._snapshots.get(norad_id, {})
        return sorted(by_epoch.values(), key=lambda snap: _utc(snap.epoch))

    def latest_at(self, norad_id: int, epoch: datetime) -> TLESnapshot | None:
        """The newest element set at or before ``epoch``, if any."""
        target = _utc(epoch)
        best: TLESnapshot | None = None
        for snapshot in self.history(norad_id):
            if _utc(snapshot.epoch) <= target:
                best = snapshot
            else:
                break
        return best

    def norad_ids(self) -> list[int]:
        return sorted(self._snapshots)

    def count(self) -> int:
        return sum(len(by_epoch) for by_epoch in self._snapshots.values())

    def object_count(self) -> int:
        return len(self._snapshots)

    def clear(self) -> None:
        self._snapshots.clear()

    def maneuvers(self, norad_id: int, **kwargs: Any) -> list[ManeuverEvent]:
        return detect_maneuvers(self.history(norad_id), **kwargs)

    def decay_anomalies(self, norad_id: int, **kwargs: Any) -> list[DecayAnomaly]:
        return detect_decay_anomalies(self.history(norad_id), **kwargs)

    def change_log(self, norad_id: int, **kwargs: Any) -> list[OrbitChangeEntry]:
        return orbit_change_log(self.history(norad_id), **kwargs)


_default_store: TLEHistoryStore | None = None


def get_history_store() -> TLEHistoryStore:
    """Process-wide history store, created on first use."""
    global _default_store
    if _default_store is None:
        _default_store = TLEHistoryStore()
    return _default_store


def reset_history_store(store: TLEHistoryStore | None = None) -> TLEHistoryStore:
    """Replace the process-wide store. Intended for tests and for reloads."""
    global _default_store
    _default_store = store if store is not None else TLEHistoryStore(load_existing=False)
    return _default_store
