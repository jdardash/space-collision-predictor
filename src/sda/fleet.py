"""Operator fleet definitions and batch screening against the loaded catalog.

An operator flying more than one spacecraft cares about "my birds against
everything else", not about a single pair. A fleet file names the NORAD ids,
the hard-body radius to assume for those vehicles, the thresholds that count as
actionable, and optionally a webhook to notify. Screening runs the normal
conjunction pipeline over the catalog and keeps the events that touch the
fleet.

Definitions are YAML when pyyaml is importable and JSON otherwise; the JSON
path is a strict subset, so a fleet file written as JSON parses either way.

Frames and units are the service defaults: ECI (TEME), km, km/s, UTC epochs.
The conjunction pipeline is called by name at call time and only with the
arguments its current signature accepts, so this module keeps working while
sda.conjunction grows new options.
"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from sda.constants import DEFAULT_COMBINED_RADIUS_KM
from sda.models import ConjunctionEvent, RiskLevel

_RISK_RANK: dict[str, int] = {
    RiskLevel.NEGLIGIBLE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MODERATE: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}

EXAMPLE_FLEET_YAML = """# Operator fleet definition for POST /fleet/screen
name: Example LEO Fleet
notes: Three well-known objects, used to show the file format.
norad_ids:
  - 25544   # ISS (ZARYA)
  - 48274   # CSS (TIANHE)
  - 20580   # HST
hard_body_radius_m: 15.0
webhook_url: null
thresholds:
  pc_threshold: 1.0e-4
  miss_distance_km: 5.0
  risk_at_or_above: HIGH
"""


class FleetThresholds(BaseModel):
    """What counts as actionable for this operator."""

    pc_threshold: float = Field(default=1e-4, ge=0.0, le=1.0)
    miss_distance_km: float = Field(default=5.0, gt=0.0)
    risk_at_or_above: RiskLevel = RiskLevel.HIGH


class FleetDefinition(BaseModel):
    """A named set of operator-owned objects plus its screening policy."""

    name: str
    norad_ids: list[int] = Field(default_factory=list)
    hard_body_radius_m: float = Field(default=10.0, gt=0.0)
    thresholds: FleetThresholds = Field(default_factory=FleetThresholds)
    webhook_url: str | None = None
    notes: str = ""

    @property
    def hard_body_radius_km(self) -> float:
        return self.hard_body_radius_m / 1000.0


class FleetEventSummary(BaseModel):
    """One conjunction that involves a fleet object."""

    fleet_norad_id: int
    other_norad_id: int
    other_name: str = ""
    tca: Any
    miss_distance_km: float
    relative_velocity_km_s: float
    risk: RiskLevel
    probability: float | None = None
    pc_scaled_to_fleet_hbr: float | None = None
    flagged: bool = False
    reasons: list[str] = Field(default_factory=list)


class FleetScreenResult(BaseModel):
    """Outcome of one fleet screening run."""

    fleet_name: str
    fleet_size: int
    catalog_size: int
    hours: float
    threshold_km: float
    events_total: int
    events_flagged: int
    events: list[FleetEventSummary] = Field(default_factory=list)
    missing_norad_ids: list[int] = Field(default_factory=list)


class FleetScreenRequest(BaseModel):
    """Payload for POST /fleet/screen: a fleet plus the screening window."""

    fleet: FleetDefinition | None = None
    fleet_text: str | None = None
    hours: float = Field(default=24.0, gt=0, le=168.0)
    threshold_km: float = Field(default=10.0, gt=0, le=200.0)
    against: str = Field(default="catalog", pattern="^(catalog|fleet)$")
    max_events: int = Field(default=200, ge=1, le=2000)


def parse_fleet(text: str) -> FleetDefinition:
    """Parse a fleet definition from YAML or JSON text."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty fleet definition")

    data: Any = None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        yaml = None  # type: ignore[assignment]

    if yaml is not None:
        try:
            data = yaml.safe_load(stripped)
        except Exception as exc:
            raise ValueError(f"fleet definition is not valid YAML or JSON: {exc}") from exc
    else:
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"fleet definition is not valid JSON (pyyaml not installed): {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise ValueError("fleet definition must be a mapping with at least name and norad_ids")
    return FleetDefinition.model_validate(data)


def example_fleet() -> FleetDefinition:
    """The bundled example, parsed."""
    return parse_fleet(EXAMPLE_FLEET_YAML)


def call_find_conjunctions(
    store: Any,
    norad_ids: Sequence[int] | None,
    hours: float,
    threshold_km: float,
) -> list[ConjunctionEvent]:
    """Call sda.conjunction.find_conjunctions with the args it accepts today.

    Resolved at call time and filtered against the live signature so a wider
    pipeline signature (new keyword-only options) cannot break fleet screening,
    and a narrower one degrades to fewer arguments instead of a TypeError.
    """
    module = importlib.import_module("sda.conjunction")
    func = getattr(module, "find_conjunctions", None)
    if func is None:
        raise RuntimeError("sda.conjunction.find_conjunctions is unavailable")

    parameters = inspect.signature(func).parameters
    kwargs: dict[str, Any] = {}
    candidates: list[tuple[str, Any]] = [
        ("store", store),
        ("norad_ids", list(norad_ids) if norad_ids else None),
        ("hours", hours),
        ("threshold_km", threshold_km),
    ]
    for name, value in candidates:
        if name in parameters and value is not None:
            kwargs[name] = value

    if "store" not in kwargs:
        return list(func(store, **kwargs))
    return list(func(**kwargs))


def _scale_pc_to_hbr(pc: float | None, fleet_hbr_km: float) -> float | None:
    """Rescale a Pc computed at the default hard-body radius to the fleet's.

    In the small-hard-body limit the encounter-plane probability density is
    nearly constant across the circle, so Pc scales with its area, that is with
    the square of the radius. This is an indication for triage only: it is not
    valid once the radius approaches the combined position sigma, and it never
    replaces a Pc computed from real covariances.
    """
    if pc is None or DEFAULT_COMBINED_RADIUS_KM <= 0.0:
        return None
    ratio = fleet_hbr_km / DEFAULT_COMBINED_RADIUS_KM
    return min(1.0, pc * ratio * ratio)


def _summarize_event(
    event: ConjunctionEvent, fleet: FleetDefinition, fleet_ids: set[int]
) -> FleetEventSummary:
    if event.primary in fleet_ids:
        mine, other, other_name = event.primary, event.secondary, event.secondary_name
    else:
        mine, other, other_name = event.secondary, event.primary, event.primary_name

    pc = None if event.collision_probability is None else event.collision_probability.probability
    scaled = _scale_pc_to_hbr(pc, fleet.hard_body_radius_km)

    reasons: list[str] = []
    thresholds = fleet.thresholds
    if event.miss_distance_km <= thresholds.miss_distance_km:
        reasons.append(
            f"miss {event.miss_distance_km:.3f} km within {thresholds.miss_distance_km:g} km"
        )
    if pc is not None and pc >= thresholds.pc_threshold:
        reasons.append(f"Pc {pc:.3e} at or above {thresholds.pc_threshold:.3e}")
    if _RISK_RANK.get(str(event.risk), 0) >= _RISK_RANK.get(str(thresholds.risk_at_or_above), 0):
        reasons.append(f"risk {event.risk} at or above {thresholds.risk_at_or_above}")

    return FleetEventSummary(
        fleet_norad_id=mine,
        other_norad_id=other,
        other_name=other_name,
        tca=event.tca,
        miss_distance_km=event.miss_distance_km,
        relative_velocity_km_s=event.relative_velocity_km_s,
        risk=event.risk,
        probability=pc,
        pc_scaled_to_fleet_hbr=scaled,
        flagged=bool(reasons),
        reasons=reasons,
    )


def screen_fleet(
    fleet: FleetDefinition,
    store: Any,
    hours: float = 24.0,
    threshold_km: float = 10.0,
    against: str = "catalog",
    max_events: int = 200,
) -> FleetScreenResult:
    """Screen a fleet and return only the events that involve its objects.

    `against="catalog"` screens the fleet against every tracked object, which
    is the operational case. `against="fleet"` restricts the run to the fleet
    itself, which is cheaper and useful for a constellation with tight
    intra-plane spacing.
    """
    fleet_ids = {int(n) for n in fleet.norad_ids}
    known = {tle.norad_id for tle in store.get_all()}
    missing = sorted(fleet_ids - known)

    if against == "fleet":
        events = call_find_conjunctions(store, sorted(fleet_ids), hours, threshold_km)
    else:
        events = call_find_conjunctions(store, None, hours, threshold_km)

    involved = [e for e in events if e.primary in fleet_ids or e.secondary in fleet_ids]
    summaries = [_summarize_event(e, fleet, fleet_ids) for e in involved]
    summaries.sort(
        key=lambda s: (
            -_RISK_RANK.get(str(s.risk), 0),
            s.miss_distance_km,
        )
    )

    return FleetScreenResult(
        fleet_name=fleet.name,
        fleet_size=len(fleet_ids),
        catalog_size=len(known),
        hours=hours,
        threshold_km=threshold_km,
        events_total=len(summaries),
        events_flagged=sum(1 for s in summaries if s.flagged),
        events=summaries[:max_events],
        missing_norad_ids=missing,
    )
