"""Pass prediction, look angle, sensor coverage and DOP routes.

Request models live here rather than in ``sda.models``; the orbital mechanics
lives in :mod:`sda.passes` and :mod:`sda.sensors`. The app-level TLE catalog is
resolved lazily through :func:`_store`, the same late import
``sda.routes.analysis`` uses, so importing this module does not pull in the
application object.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from sda.models import TLERecord
from sda.passes import (
    DEFAULT_COARSE_STEP_S,
    DEFAULT_MIN_ELEVATION_DEG,
    DEFAULT_SAMPLE_STEP_S,
    Observer,
    look_angles_for_tle,
    predict_passes_multi,
)
from sda.propagator import build_satrec, datetime_to_jd
from sda.sensors import (
    DEFAULT_FENCE_STEP_S,
    SensorType,
    compute_dop,
    fence_crossings,
    list_sensors,
    resolve_sensors,
    sensors_covering_tle,
)
from sda.tle_store import TLEStore

router = APIRouter(tags=["Passes"])

MAX_WINDOW_HOURS = 168.0
MAX_OBJECTS_PER_REQUEST = 50


def _store() -> TLEStore:
    """The app-level TLE catalog (late import, as in routes/analysis.py)."""
    from sda.api import store

    return store


def _require_tle(norad_id: int) -> TLERecord:
    tle = _store().get(norad_id)
    if tle is None:
        raise HTTPException(status_code=404, detail=f"Satellite {norad_id} not found")
    return tle


def _resolve_start(start: datetime | None) -> datetime:
    if start is None:
        return datetime.now(UTC)
    return start if start.tzinfo is not None else start.replace(tzinfo=UTC)


def _clamp_hours(hours: float) -> float:
    return max(0.0, min(hours, MAX_WINDOW_HOURS))


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class PassRequest(BaseModel):
    """Pass prediction over one observer for one or many objects."""

    observer: Observer
    norad_id: int | None = None
    norad_ids: list[int] = Field(default_factory=list)
    start: datetime | None = None
    hours: float = Field(default=24.0, gt=0.0, le=MAX_WINDOW_HOURS)
    min_elevation_deg: float = Field(default=DEFAULT_MIN_ELEVATION_DEG, ge=0.0, le=89.0)
    coarse_step_s: float = Field(default=DEFAULT_COARSE_STEP_S, gt=0.0, le=600.0)
    sample_step_s: float = Field(default=DEFAULT_SAMPLE_STEP_S, gt=0.0, le=600.0)
    include_samples: bool = False

    def requested_ids(self) -> list[int]:
        ids = list(self.norad_ids)
        if self.norad_id is not None and self.norad_id not in ids:
            ids.insert(0, self.norad_id)
        return ids[:MAX_OBJECTS_PER_REQUEST]


class LookAngleRequest(BaseModel):
    """Instantaneous look angles from one observer to a set of objects."""

    observer: Observer
    norad_ids: list[int] = Field(default_factory=list)
    time: datetime | None = None


class CoverageRequest(BaseModel):
    """Which sensors can see one object at one instant."""

    norad_id: int
    time: datetime | None = None
    sensor_names: list[str] = Field(default_factory=list)


class FenceRequest(BaseModel):
    """Field-of-regard entries and exits over a window."""

    norad_id: int
    start: datetime | None = None
    hours: float = Field(default=24.0, gt=0.0, le=MAX_WINDOW_HOURS)
    step_s: float = Field(default=DEFAULT_FENCE_STEP_S, gt=0.0, le=600.0)
    sensor_names: list[str] = Field(default_factory=list)


class DopRequest(BaseModel):
    """Dilution of precision for an observer over a set of objects."""

    observer: Observer
    norad_ids: list[int] = Field(default_factory=list)
    time: datetime | None = None
    min_elevation_deg: float = Field(default=0.0, ge=-90.0, le=89.0)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/passes",
    summary="Predict ground-station passes",
    response_description="Rise, culmination and set for every pass in the window",
)
def predict_pass_window(request: PassRequest) -> dict[str, Any]:
    """Predict passes of one or more tracked objects over a ground observer."""
    norad_ids = request.requested_ids()
    if not norad_ids:
        raise HTTPException(status_code=400, detail="Provide norad_id or norad_ids")

    tles = [_require_tle(norad_id) for norad_id in norad_ids]
    start = _resolve_start(request.start)

    found = predict_passes_multi(
        tles,
        request.observer,
        start,
        hours=_clamp_hours(request.hours),
        min_elevation_deg=request.min_elevation_deg,
        coarse_step_s=request.coarse_step_s,
        sample_step_s=request.sample_step_s,
        include_samples=request.include_samples,
    )

    return {
        "observer": request.observer.model_dump(mode="json"),
        "start": start.isoformat(),
        "hours": request.hours,
        "min_elevation_deg": request.min_elevation_deg,
        "norad_ids": norad_ids,
        "count": len(found),
        "visible_count": sum(1 for item in found if item.visible),
        "passes": [item.model_dump(mode="json") for item in found],
    }


@router.post(
    "/lookangles",
    summary="Topocentric look angles at one instant",
    response_description="Azimuth, elevation, range and range rate per object",
)
def compute_look_angles(request: LookAngleRequest) -> dict[str, Any]:
    """Azimuth, elevation, slant range and range rate from an observer."""
    if not request.norad_ids:
        raise HTTPException(status_code=400, detail="Provide at least one norad_id")

    when = _resolve_start(request.time)
    results: list[dict[str, Any]] = []
    for norad_id in request.norad_ids[:MAX_OBJECTS_PER_REQUEST]:
        tle = _require_tle(norad_id)
        try:
            look = look_angles_for_tle(tle, request.observer, when)
        except RuntimeError as exc:
            results.append(
                {"norad_id": norad_id, "name": tle.name, "error": str(exc)}
            )
            continue
        entry: dict[str, Any] = {"norad_id": norad_id, "name": tle.name}
        entry.update(look.model_dump(mode="json"))
        entry["above_horizon"] = look.elevation_deg > 0.0
        results.append(entry)

    return {
        "observer": request.observer.model_dump(mode="json"),
        "time": when.isoformat(),
        "count": len(results),
        "look_angles": results,
    }


@router.get(
    "/sensors",
    summary="Bundled surveillance sensor network",
    response_description="Illustrative sensor sites and their fields of regard",
)
def get_sensors(
    sensor_type: Annotated[
        SensorType | None, Query(description="Filter by sensor type (radar or optical)")
    ] = None,
) -> dict[str, Any]:
    """List the bundled sensor sites. Coordinates are illustrative, not survey data."""
    sensors = list_sensors(sensor_type)
    return {
        "count": len(sensors),
        "disclaimer": (
            "Approximate coordinates written from public knowledge. Illustrative "
            "only, not operational sensor data."
        ),
        "sensors": [sensor.model_dump(mode="json") for sensor in sensors],
    }


@router.post(
    "/sensors/coverage",
    summary="Which sensors can see an object now",
    response_description="Sensors whose field of regard contains the object, with look angles",
)
def get_sensor_coverage(request: CoverageRequest) -> dict[str, Any]:
    """Report the sensors that can see one object at one instant."""
    tle = _require_tle(request.norad_id)
    when = _resolve_start(request.time)
    sensors = resolve_sensors(request.sensor_names)
    if not sensors:
        raise HTTPException(status_code=404, detail="No matching sensors")

    try:
        covering = sensors_covering_tle(tle, when, sensors)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "norad_id": request.norad_id,
        "name": tle.name,
        "time": when.isoformat(),
        "sensors_queried": len(sensors),
        "count": len(covering),
        "coverage": [entry.model_dump(mode="json") for entry in covering],
    }


@router.post(
    "/sensors/fences",
    summary="Sensor fence crossings over a window",
    response_description="Field-of-regard entry and exit times, refined to one second",
)
def get_fence_crossings(request: FenceRequest) -> dict[str, Any]:
    """Find where an object enters or leaves each sensor's field of regard."""
    tle = _require_tle(request.norad_id)
    start = _resolve_start(request.start)
    sensors = resolve_sensors(request.sensor_names)
    if not sensors:
        raise HTTPException(status_code=404, detail="No matching sensors")

    crossings = fence_crossings(
        tle,
        start,
        hours=_clamp_hours(request.hours),
        sensors=sensors,
        step_s=request.step_s,
    )

    return {
        "norad_id": request.norad_id,
        "name": tle.name,
        "start": start.isoformat(),
        "hours": request.hours,
        "sensors_queried": len(sensors),
        "count": len(crossings),
        "crossings": [crossing.model_dump(mode="json") for crossing in crossings],
    }


@router.post(
    "/dops",
    summary="Dilution of precision for an observer",
    response_description="GDOP, PDOP, HDOP, VDOP and TDOP from the visible geometry",
)
def get_dilution_of_precision(request: DopRequest) -> dict[str, Any]:
    """Compute DOP values for an observer given a set of tracked objects."""
    if not request.norad_ids:
        raise HTTPException(status_code=400, detail="Provide at least one norad_id")

    when = _resolve_start(request.time)
    positions: list[np.ndarray] = []
    used: list[int] = []
    for norad_id in request.norad_ids[:MAX_OBJECTS_PER_REQUEST]:
        tle = _require_tle(norad_id)
        satrec = build_satrec(tle)
        jd, fr = datetime_to_jd(when)
        error, position, _ = satrec.sgp4(jd, fr)
        if error != 0:
            continue
        positions.append(np.array(position, dtype=float))
        used.append(norad_id)

    try:
        dop = compute_dop(request.observer, positions, when, request.min_elevation_deg)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "observer": request.observer.model_dump(mode="json"),
        "time": when.isoformat(),
        "norad_ids": used,
        "min_elevation_deg": request.min_elevation_deg,
        "dop": dop.model_dump(mode="json"),
    }
