"""Probability routes: method comparison, maximum Pc, dilution, CDM Pc.

Four POST endpoints over the collision-probability machinery in
sda.probability and sda.cdm. Everything here is a pure computation on the
request body: no catalog lookup, no propagation, no network.

The router is not registered by this module; sda.api owns app assembly.
"""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from sda.cdm import compute_pc_from_cdm, parse_cdm_detailed
from sda.constants import DEFAULT_COMBINED_RADIUS_KM, DEFAULT_POSITION_SIGMA_KM
from sda.probability import (
    PC_METHODS,
    PcUsageWarning,
    dilution_max_pc,
    frisbee_max_pc,
    pc_by_method,
    pc_monte_carlo_2d,
    pc_usage_checks,
    project_covariance_to_encounter_plane,
    project_miss_to_encounter_plane,
    remediate_covariance,
)

router = APIRouter(prefix="/pc", tags=["Probability"])

_MIN_REL_SPEED_KM_S = 1e-10


def _validate_3x3(value: list[list[float]] | None) -> list[list[float]] | None:
    if value is None:
        return None
    if len(value) != 3 or any(len(row) != 3 for row in value):
        raise ValueError("covariance must be a 3x3 matrix in km^2")
    return value


class EncounterBody(BaseModel):
    """Encounter geometry in the inertial frame, shared by every endpoint.

    A covariance left out is replaced by an isotropic default of
    DEFAULT_POSITION_SIGMA_KM, and the substitution is reported back in the
    response so it is never silent.
    """

    miss_vector_km: list[float] = Field(
        ..., min_length=3, max_length=3,
        description="Relative position at TCA (primary minus secondary), km",
    )
    rel_velocity_km_s: list[float] = Field(
        ..., min_length=3, max_length=3,
        description="Relative velocity at TCA, km/s",
    )
    cov_primary_km2: list[list[float]] | None = Field(
        None, description="3x3 primary position covariance, km^2"
    )
    cov_secondary_km2: list[list[float]] | None = Field(
        None, description="3x3 secondary position covariance, km^2"
    )
    hard_body_radius_km: float = Field(
        DEFAULT_COMBINED_RADIUS_KM, gt=0.0, le=100.0,
        description="Combined hard-body radius, km",
    )

    @field_validator("cov_primary_km2", "cov_secondary_km2")
    @classmethod
    def _check_shape(cls, value: list[list[float]] | None) -> list[list[float]] | None:
        return _validate_3x3(value)


class CompareBody(EncounterBody):
    """Encounter plus the options for the method comparison."""

    include_monte_carlo: bool = Field(
        False, description="Add a Monte Carlo estimate (slower, stochastic)"
    )
    monte_carlo_samples: int = Field(200_000, ge=1_000, le=5_000_000)
    monte_carlo_seed: int = Field(42, description="Seed, so the estimate is reproducible")
    orbital_period_s: float = Field(
        5400.0, gt=0.0, description="Period used by the extended-encounter check"
    )


class DilutionBody(EncounterBody):
    """Encounter plus the covariance-scale sweep settings."""

    scale_min: float = Field(1e-4, gt=0.0)
    scale_max: float = Field(1e4, gt=0.0)
    n_points: int = Field(121, ge=3, le=2001)
    method: str = Field("alfano", description="One of foster, chan, alfano")
    include_curve: bool = Field(False, description="Return the whole scale/Pc curve")


class MaxPcBody(BaseModel):
    """Encounter with one covariance known and the other missing."""

    miss_vector_km: list[float] = Field(..., min_length=3, max_length=3)
    rel_velocity_km_s: list[float] = Field(..., min_length=3, max_length=3)
    cov_known_km2: list[list[float]] = Field(
        ..., description="3x3 covariance of the object that has one, km^2"
    )
    hard_body_radius_km: float = Field(DEFAULT_COMBINED_RADIUS_KM, gt=0.0, le=100.0)
    method: str = Field("alfano", description="One of foster, chan, alfano")

    @field_validator("cov_known_km2")
    @classmethod
    def _check_shape(cls, value: list[list[float]]) -> list[list[float]]:
        checked = _validate_3x3(value)
        if checked is None:  # pragma: no cover - None is rejected by the type
            raise ValueError("covariance is required")
        return checked


class CdmBody(BaseModel):
    """A CDM to score, in either CCSDS wire format."""

    cdm_text: str = Field(..., min_length=1, description="CDM in KVN or XML form")
    hard_body_radius_km: float = Field(DEFAULT_COMBINED_RADIUS_KM, gt=0.0, le=100.0)


def _geometry(
    miss_vector_km: list[float],
    rel_velocity_km_s: list[float],
    cov_3d: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Project the encounter onto the 2D plane, or 422 if it has no plane."""
    rel_v = np.asarray(rel_velocity_km_s, dtype=float)
    if float(np.linalg.norm(rel_v)) < _MIN_REL_SPEED_KM_S:
        raise HTTPException(
            status_code=422,
            detail="relative velocity is zero; the encounter plane is undefined",
        )
    miss = np.asarray(miss_vector_km, dtype=float)
    return project_miss_to_encounter_plane(miss, rel_v), project_covariance_to_encounter_plane(
        cov_3d, rel_v
    )


def _combined_covariance(body: EncounterBody) -> tuple[np.ndarray, list[str]]:
    """Sum the two covariances, defaulting either one that is missing."""
    notes: list[str] = []
    default = np.eye(3) * DEFAULT_POSITION_SIGMA_KM**2
    if body.cov_primary_km2 is None:
        notes.append(
            f"primary covariance missing; isotropic sigma={DEFAULT_POSITION_SIGMA_KM} km assumed"
        )
        cov1 = default
    else:
        cov1 = np.asarray(body.cov_primary_km2, dtype=float)
    if body.cov_secondary_km2 is None:
        notes.append(
            f"secondary covariance missing; isotropic sigma={DEFAULT_POSITION_SIGMA_KM} km assumed"
        )
        cov2 = default
    else:
        cov2 = np.asarray(body.cov_secondary_km2, dtype=float)
    return cov1 + cov2, notes


def _check_method(method: str) -> str:
    if method not in PC_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown method {method}; expected one of {list(PC_METHODS)}",
        )
    return method


def _warnings_out(warnings: list[PcUsageWarning]) -> list[dict]:
    return [{"code": w.code, "message": w.message, "values": w.values} for w in warnings]


@router.post(
    "/compare",
    summary="Compare every Pc method on one encounter",
    response_description="Pc by method, encounter-plane geometry and usage violations",
)
def compare_methods(body: CompareBody) -> dict:
    """Run Foster, Chan and Alfano on the same encounter and check the assumptions.

    The three methods are computationally independent, so a disagreement
    between them is itself a warning sign. Chan is an equivalent-area
    approximation and drifts on elongated covariances (the usage report
    raises CHAN_ASPECT_RATIO there); Foster and Alfano both track the CARA
    Pc2D reference values to 5e-5 relative.
    """
    cov_3d, notes = _combined_covariance(body)
    miss_2d, cov_2d = _geometry(body.miss_vector_km, body.rel_velocity_km_s, cov_3d)
    hbr = body.hard_body_radius_km

    cov_2d_fixed, remediated = remediate_covariance(cov_2d, hbr)
    probabilities = {
        method: pc_by_method(method, miss_2d, cov_2d_fixed, hbr) for method in PC_METHODS
    }
    if body.include_monte_carlo:
        probabilities["monte_carlo"] = pc_monte_carlo_2d(
            miss_2d,
            cov_2d_fixed,
            hbr,
            n_samples=body.monte_carlo_samples,
            seed=body.monte_carlo_seed,
        )

    report = pc_usage_checks(
        np.asarray(body.miss_vector_km, dtype=float),
        np.asarray(body.rel_velocity_km_s, dtype=float),
        cov_3d,
        hard_body_radius_km=hbr,
        orbital_period_s=body.orbital_period_s,
    )
    return {
        "probabilities": probabilities,
        "method_spread": max(probabilities.values()) - min(probabilities.values()),
        "miss_distance_km": float(np.linalg.norm(body.miss_vector_km)),
        "relative_speed_km_s": float(np.linalg.norm(body.rel_velocity_km_s)),
        "hard_body_radius_km": hbr,
        "encounter_plane": {
            "miss_2d_km": [float(x) for x in miss_2d],
            "covariance_km2": [[float(c) for c in row] for row in cov_2d_fixed],
            "aspect_ratio": report.aspect_ratio,
            "covariance_remediated": bool(remediated),
        },
        "usage": {
            "ok": report.ok,
            "codes": report.codes,
            "warnings": _warnings_out(report.warnings),
            "encounter_duration_s": report.encounter_duration_s,
            "condition_number": report.condition_number,
        },
        "assumptions": notes,
    }


@router.post(
    "/max-missing-covariance",
    summary="Upper bound on Pc when one covariance is missing",
    response_description="Frisbee maximum Pc and the maximizing covariance",
)
def max_missing_covariance(body: MaxPcBody) -> dict:
    """Largest Pc any unknown second covariance could produce.

    Follows the CARA FrisbeeMaxPc construction. Use it to screen an event out
    when a covariance is unavailable: if the bound is small the conjunction is
    safe whatever the missing uncertainty turns out to be. The value is a
    bound, never an estimate of Pc.
    """
    method = _check_method(body.method)
    cov_3d = np.asarray(body.cov_known_km2, dtype=float)
    miss_2d, cov_2d = _geometry(body.miss_vector_km, body.rel_velocity_km_s, cov_3d)
    result = frisbee_max_pc(miss_2d, cov_2d, body.hard_body_radius_km, method=method)
    return {
        "max_pc": result.max_pc,
        "pc_known_covariance_only": result.pc_known_only,
        "mahalanobis_distance": result.mahalanobis_distance,
        "added_variance_km2": result.added_variance_km2,
        "maximizing_covariance_km2": result.combined_covariance_km2,
        "bounded": result.bounded,
        "method": result.method,
        "miss_distance_km": float(np.linalg.norm(body.miss_vector_km)),
        "hard_body_radius_km": body.hard_body_radius_km,
        "interpretation": (
            "upper bound over every covariance the second object could have; "
            "not an estimate of Pc"
        ),
    }


@router.post(
    "/dilution",
    summary="Scan covariance scale for the maximum achievable Pc",
    response_description="Actual Pc, maximum Pc, the scale that reaches it, and the curve",
)
def dilution_scan(body: DilutionBody) -> dict:
    """Detect probability dilution by sweeping a covariance scale factor.

    Pc rises with growing uncertainty until the hard-body disk stops gaining
    probability mass, then falls. A maximum at a scale above 1 means the
    reported Pc is low because the covariance is large, not because the
    encounter is safe.
    """
    method = _check_method(body.method)
    if body.scale_min >= body.scale_max:
        raise HTTPException(status_code=422, detail="require scale_min < scale_max")
    cov_3d, notes = _combined_covariance(body)
    miss_2d, cov_2d = _geometry(body.miss_vector_km, body.rel_velocity_km_s, cov_3d)
    scan = dilution_max_pc(
        miss_2d,
        cov_2d,
        body.hard_body_radius_km,
        scale_min=body.scale_min,
        scale_max=body.scale_max,
        n_points=body.n_points,
        method=method,
    )
    out: dict = {
        "pc_actual": scan.pc_actual,
        "max_pc": scan.max_pc,
        "scale_at_max": scan.scale_at_max,
        "diluted": scan.diluted,
        "dilution_ratio": (
            scan.max_pc / scan.pc_actual if scan.pc_actual > 0.0 else float("inf")
        ),
        "method": scan.method,
        "miss_distance_km": float(np.linalg.norm(body.miss_vector_km)),
        "hard_body_radius_km": body.hard_body_radius_km,
        "assumptions": notes,
    }
    if body.include_curve:
        out["curve"] = {"scales": scan.scales, "pc_values": scan.pc_values}
    return out


@router.post(
    "/cdm",
    summary="Compute Pc from a CDM in KVN or XML form",
    response_description="Parsed CDM summary with Pc by method and the stated value",
)
def pc_from_cdm(body: CdmBody) -> dict:
    """Parse a CCSDS 508.0-B-1 CDM and recompute its collision probability.

    The wire format is detected from the text, so the same endpoint takes a
    KVN message and an XML one. The stated Pc the message carries, when it
    has one, is returned next to the recomputed values for comparison.
    """
    try:
        detail = parse_cdm_detailed(body.cdm_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        result = compute_pc_from_cdm(
            detail.parsed, hard_body_radius_km=body.hard_body_radius_km
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    parsed = detail.parsed
    return {
        "source_format": detail.source_format,
        "message_id": parsed.message_id,
        "originator": parsed.originator,
        "tca": parsed.tca,
        "object1": parsed.object1.designator,
        "object2": parsed.object2.designator,
        "probabilities": {
            "foster": result.probability_foster,
            "chan": result.probability_chan,
        },
        "stated_collision_probability": result.stated_collision_probability,
        "stated_collision_probability_method": detail.collision_probability_method,
        "miss_distance_km": result.miss_distance_km,
        "mahalanobis_distance": result.mahalanobis_distance,
        "hard_body_radius_km": result.hard_body_radius_km,
        "frame": result.frame,
        "assumptions": result.assumptions,
        "comments": detail.comments,
    }
