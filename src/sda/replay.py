"""Historical replay: rebuild the catalog as it was and re-run the pipeline.

Given an archive of element sets (sda.tle_history) and a target epoch, pick the
newest element set at or before that epoch for every object, rebuild a
temporary TLEStore from those lines, and report where everything was. The same
temporary store can be handed to the live conjunction screener, so a past day
is analysed by exactly the code that analyses today.

Attribution: the "rebuild a catalog snapshot from archived element sets, then
feed it to the normal pipeline" arrangement follows the replay design used by
the satguard project (MIT license). No satguard code is used here.

Notes
-----
* Element sets are always ingested through TLEStore.load_from_text, so replay
  lines pass the same validation as live ones.
* An element set is only used if its epoch is at or before the replay epoch.
  Objects with no such element set are reported in ``missing`` rather than
  silently dropped.
* ``element_age_days`` is the gap between the element set epoch and the replay
  epoch. Old element sets propagate badly; the field lets a caller weight or
  reject a result.
* The conjunction entry point is looked up by name at call time and given only
  the keyword arguments its current signature accepts, so a signature change in
  that module cannot break replay.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sda.models import ConjunctionEvent
from sda.propagator import build_satrec, propagate_at
from sda.tle_history import TLEHistoryStore, TLESnapshot
from sda.tle_store import TLEStore

SECONDS_PER_DAY = 86400.0


class ReplayObject(BaseModel):
    """One object as it was at the replay epoch."""

    norad_id: int
    name: str
    element_epoch: datetime
    element_age_days: float
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]


class ReplayResult(BaseModel):
    """Positions, and optionally conjunctions, at a past epoch."""

    epoch: datetime
    requested: int
    resolved: int
    objects: list[ReplayObject] = Field(default_factory=list)
    missing: list[int] = Field(default_factory=list)
    screened: bool = False
    conjunctions: list[ConjunctionEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def select_snapshots(
    history: TLEHistoryStore,
    epoch: datetime,
    norad_ids: Sequence[int] | None = None,
) -> tuple[list[TLESnapshot], list[int]]:
    """Newest element set at or before ``epoch`` for each requested object.

    Returns the selected snapshots and the ids that had no usable element set.
    """
    target = _utc(epoch)
    wanted = list(norad_ids) if norad_ids is not None else history.norad_ids()
    selected: list[TLESnapshot] = []
    missing: list[int] = []
    for norad_id in wanted:
        snapshot = history.latest_at(norad_id, target)
        if snapshot is None:
            missing.append(norad_id)
        else:
            selected.append(snapshot)
    return selected, missing


def build_replay_store(snapshots: Sequence[TLESnapshot]) -> TLEStore:
    """Build a temporary catalog from archived element sets."""
    store = TLEStore()
    if not snapshots:
        return store
    text = "\n".join(snapshot.tle_text() for snapshot in snapshots)
    store.load_from_text(text)
    return store


def _call_find_conjunctions(store: TLEStore, **kwargs: Any) -> list[ConjunctionEvent]:
    """Call the conjunction module's public entry point defensively.

    The entry point is resolved by name at call time and handed only the
    keyword arguments its signature currently accepts.
    """
    from sda import conjunction as conjunction_module

    entry = getattr(conjunction_module, "find_conjunctions", None)
    if entry is None or not callable(entry):
        raise RuntimeError("sda.conjunction.find_conjunctions is not available")
    accepted = inspect.signature(entry).parameters
    filtered = {key: value for key, value in kwargs.items() if key in accepted}
    result = entry(store, **filtered)
    return list(result)


def replay_at(
    history: TLEHistoryStore,
    epoch: datetime,
    norad_ids: Sequence[int] | None = None,
    screen: bool = False,
    hours: float = 1.0,
    threshold_km: float = 10.0,
    compute_probability: bool = True,
) -> ReplayResult:
    """Reconstruct positions at ``epoch``, optionally screening for conjunctions.

    ``hours`` and ``threshold_km`` are only used when ``screen`` is set; the
    screen starts at the replay epoch and runs forward.
    """
    target = _utc(epoch)
    requested = len(norad_ids) if norad_ids is not None else len(history.norad_ids())
    snapshots, missing = select_snapshots(history, target, norad_ids)
    warnings: list[str] = []

    store = build_replay_store(snapshots)
    objects: list[ReplayObject] = []
    for snapshot in snapshots:
        record = store.get(snapshot.norad_id)
        if record is None:
            warnings.append(f"{snapshot.norad_id}: element set rejected by the validator")
            missing.append(snapshot.norad_id)
            continue
        try:
            state = propagate_at(build_satrec(record), target)
        except Exception as exc:
            warnings.append(f"{snapshot.norad_id}: propagation failed ({exc})")
            missing.append(snapshot.norad_id)
            continue
        age_days = (target - _utc(snapshot.epoch)).total_seconds() / SECONDS_PER_DAY
        objects.append(
            ReplayObject(
                norad_id=snapshot.norad_id,
                name=snapshot.name,
                element_epoch=_utc(snapshot.epoch),
                element_age_days=age_days,
                position_km=state.position_km,
                velocity_km_s=state.velocity_km_s,
            )
        )

    conjunctions: list[ConjunctionEvent] = []
    screened = False
    if screen:
        try:
            conjunctions = _call_find_conjunctions(
                store,
                norad_ids=list(norad_ids) if norad_ids is not None else None,
                hours=hours,
                threshold_km=threshold_km,
                start=target,
                compute_probability=compute_probability,
            )
            screened = True
        except Exception as exc:
            warnings.append(f"conjunction screen failed ({exc})")

    return ReplayResult(
        epoch=target,
        requested=requested,
        resolved=len(objects),
        objects=objects,
        missing=sorted(set(missing)),
        screened=screened,
        conjunctions=conjunctions,
        warnings=warnings,
    )
