"""History routes: breakup simulation, TLE archive, maneuver log, replay.

Five endpoints:

* POST /breakup/simulate      NASA standard breakup model on a catalog object
                              or an explicit state vector
* POST /tle/history/snapshot  archive the live catalog (or supplied TLE text)
* GET  /tle/history/{id}      archived element sets for one object
* GET  /tle/history/{id}/maneuvers  detected burns, decay anomalies, change log
* POST /replay                positions (and optionally conjunctions) at a past
                              epoch, rebuilt from the archive

The live catalog is reached the same way the other routers reach it: an import
of sda.api inside the handler, so importing this module never pulls in the app.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from sda.breakup import GabbardPoint, cloud_summary, gabbard_data, simulate_breakup
from sda.models import ConjunctionEvent
from sda.propagator import build_satrec, propagate_at
from sda.replay import ReplayObject, replay_at
from sda.rv2tle import fragments_to_tles
from sda.tle_history import (
    DecayAnomaly,
    ManeuverEvent,
    OrbitChangeEntry,
    detect_decay_anomalies,
    detect_maneuvers,
    get_history_store,
    orbit_change_log,
)

router = APIRouter(tags=["History"])


# --------------------------------------------------------------------------
# Request and response models
# --------------------------------------------------------------------------


class BreakupRequest(BaseModel):
    """Either a catalog object or an explicit state vector must be supplied."""

    norad_id: int | None = None
    position_km: tuple[float, float, float] | None = None
    velocity_km_s: tuple[float, float, float] | None = None
    epoch: datetime | None = None
    event_type: Literal["collision", "explosion"] = "collision"
    parent_type: Literal["spacecraft", "rocket_body"] = "spacecraft"
    parent_mass_kg: float = Field(default=1000.0, gt=0.0)
    projectile_mass_kg: float | None = Field(default=None, gt=0.0)
    relative_speed_km_s: float = Field(default=10.0, gt=0.0)
    min_characteristic_length_m: float = Field(default=0.01, gt=0.0)
    max_characteristic_length_m: float = Field(default=1.0, gt=0.0)
    explosion_scaling_factor: float = Field(default=1.0, gt=0.0)
    max_fragments: int = Field(default=500, ge=1, le=5000)
    seed: int = 0
    include_tles: bool = False
    max_tle_objects: int = Field(default=50, ge=1, le=500)


class BreakupResponse(BaseModel):
    event_type: str
    parent_type: str
    epoch: datetime
    catastrophic: bool
    specific_energy_j_per_g: float
    effective_mass_kg: float
    total_fragments_modelled: float
    n_above_1cm: float
    n_above_10cm: float
    fragments_sampled: int
    representative_count: float
    seed: int
    summary: dict[str, float]
    gabbard: list[GabbardPoint]
    tle_text: str | None = None
    tles_fitted: int = 0
    tles_skipped: int = 0
    max_tle_position_error_km: float = 0.0


class SnapshotRequest(BaseModel):
    """Archive the live catalog, or the TLE text supplied here."""

    tle_text: str | None = None


class SnapshotResponse(BaseModel):
    recorded: int
    objects_tracked: int
    total_snapshots: int
    persistent: bool
    directory: str | None = None


class HistoryEntry(BaseModel):
    norad_id: int
    name: str
    epoch: datetime
    recorded_at: datetime
    line1: str
    line2: str


class HistoryResponse(BaseModel):
    norad_id: int
    count: int
    entries: list[HistoryEntry]


class ManeuverResponse(BaseModel):
    norad_id: int
    snapshots: int
    maneuvers: list[ManeuverEvent]
    decay_anomalies: list[DecayAnomaly]
    change_log: list[OrbitChangeEntry]


class ReplayRequest(BaseModel):
    epoch: datetime
    norad_ids: list[int] | None = None
    screen: bool = False
    hours: float = Field(default=1.0, gt=0.0, le=168.0)
    threshold_km: float = Field(default=10.0, gt=0.0)
    compute_probability: bool = True


class ReplayResponse(BaseModel):
    epoch: datetime
    requested: int
    resolved: int
    objects: list[ReplayObject]
    missing: list[int]
    screened: bool
    conjunctions: list[ConjunctionEvent]
    warnings: list[str]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.post(
    "/breakup/simulate",
    summary="Simulate a breakup with the NASA standard breakup model",
    response_description="Fragment counts, Gabbard diagram data and cloud statistics",
)
def simulate_breakup_event(request: BreakupRequest) -> BreakupResponse:
    """Run a collision or explosion breakup on a catalog object or a raw state."""
    from sda.api import store

    epoch = request.epoch or datetime.now(UTC)
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=UTC)

    if request.norad_id is not None:
        tle = store.get(request.norad_id)
        if tle is None:
            raise HTTPException(status_code=404, detail="Satellite not found")
        try:
            state = propagate_at(build_satrec(tle), epoch)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=f"Propagation failed: {exc}") from exc
        position = state.position_km
        velocity = state.velocity_km_s
    elif request.position_km is not None and request.velocity_km_s is not None:
        position = request.position_km
        velocity = request.velocity_km_s
    else:
        raise HTTPException(
            status_code=422,
            detail="Supply either norad_id or both position_km and velocity_km_s",
        )

    try:
        cloud = simulate_breakup(
            position_km=position,
            velocity_km_s=velocity,
            epoch=epoch,
            event_type=request.event_type,
            parent_mass_kg=request.parent_mass_kg,
            projectile_mass_kg=request.projectile_mass_kg,
            relative_speed_km_s=request.relative_speed_km_s,
            parent_type=request.parent_type,
            min_characteristic_length_m=request.min_characteristic_length_m,
            max_characteristic_length_m=request.max_characteristic_length_m,
            max_fragments=request.max_fragments,
            explosion_scaling_factor=request.explosion_scaling_factor,
            seed=request.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response = BreakupResponse(
        event_type=cloud.event_type,
        parent_type=cloud.parent_type,
        epoch=cloud.epoch,
        catastrophic=cloud.catastrophic,
        specific_energy_j_per_g=cloud.specific_energy_j_per_g,
        effective_mass_kg=cloud.effective_mass_kg,
        total_fragments_modelled=cloud.total_fragments_modelled,
        n_above_1cm=cloud.n_above_1cm,
        n_above_10cm=cloud.n_above_10cm,
        fragments_sampled=cloud.fragments_sampled,
        representative_count=cloud.representative_count,
        seed=cloud.seed,
        summary=cloud_summary(cloud),
        gabbard=gabbard_data(cloud),
    )

    if request.include_tles:
        fitted = fragments_to_tles(cloud, max_objects=request.max_tle_objects)
        response.tle_text = fitted.tle_text
        response.tles_fitted = fitted.fitted
        response.tles_skipped = fitted.skipped
        response.max_tle_position_error_km = fitted.max_position_error_km

    return response


@router.post(
    "/tle/history/snapshot",
    summary="Archive element sets for later replay and maneuver detection",
    response_description="How many new snapshots were archived",
)
def record_tle_snapshot(request: SnapshotRequest | None = None) -> SnapshotResponse:
    """Archive the live catalog, or the TLE text in the request body."""
    from sda.api import store

    history = get_history_store()
    if request is not None and request.tle_text:
        try:
            recorded = history.add_from_text(request.tle_text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        recorded = history.record_snapshot(store)

    return SnapshotResponse(
        recorded=recorded,
        objects_tracked=history.object_count(),
        total_snapshots=history.count(),
        persistent=history.persistent,
        directory=str(history.directory) if history.directory else None,
    )


@router.get(
    "/tle/history/{norad_id}",
    summary="Archived element sets for one object",
    response_description="Element sets in epoch order, oldest first",
)
def get_tle_history(
    norad_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
) -> HistoryResponse:
    """Return the archived element sets for one object."""
    history = get_history_store()
    snapshots = history.history(norad_id)
    if not snapshots:
        raise HTTPException(status_code=404, detail="No archived element sets for this object")
    entries = [
        HistoryEntry(
            norad_id=snapshot.norad_id,
            name=snapshot.name,
            epoch=snapshot.epoch,
            recorded_at=snapshot.recorded_at,
            line1=snapshot.line1,
            line2=snapshot.line2,
        )
        for snapshot in snapshots[-limit:]
    ]
    return HistoryResponse(norad_id=norad_id, count=len(snapshots), entries=entries)


@router.get(
    "/tle/history/{norad_id}/maneuvers",
    summary="Detected maneuvers, decay anomalies and the orbit change log",
    response_description="Burns inferred from consecutive element sets",
)
def get_maneuver_history(
    norad_id: int,
    sigma_threshold: float = Query(default=3.0, gt=0.0, le=20.0),
    f107_solar_flux: float = Query(default=150.0, gt=0.0),
) -> ManeuverResponse:
    """Compare consecutive element sets and report unexplained orbit changes."""
    history = get_history_store()
    snapshots = history.history(norad_id)
    if not snapshots:
        raise HTTPException(status_code=404, detail="No archived element sets for this object")
    return ManeuverResponse(
        norad_id=norad_id,
        snapshots=len(snapshots),
        maneuvers=detect_maneuvers(
            snapshots, sigma_threshold=sigma_threshold, f107_solar_flux=f107_solar_flux
        ),
        decay_anomalies=detect_decay_anomalies(snapshots, f107_solar_flux=f107_solar_flux),
        change_log=orbit_change_log(
            snapshots, sigma_threshold=sigma_threshold, f107_solar_flux=f107_solar_flux
        ),
    )


@router.post(
    "/replay",
    summary="Replay the catalog at a past epoch",
    response_description="Positions at the epoch, optionally with a conjunction screen",
)
def replay_catalog(request: ReplayRequest) -> ReplayResponse:
    """Rebuild the catalog from archived element sets and report where it was."""
    history = get_history_store()
    if history.count() == 0:
        raise HTTPException(
            status_code=404,
            detail="No archived element sets. POST /tle/history/snapshot first.",
        )
    result = replay_at(
        history,
        epoch=request.epoch,
        norad_ids=request.norad_ids,
        screen=request.screen,
        hours=request.hours,
        threshold_km=request.threshold_km,
        compute_probability=request.compute_probability,
    )
    return ReplayResponse(
        epoch=result.epoch,
        requested=result.requested,
        resolved=result.resolved,
        objects=result.objects,
        missing=result.missing,
        screened=result.screened,
        conjunctions=result.conjunctions,
        warnings=result.warnings,
    )
