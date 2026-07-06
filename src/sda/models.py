"""Data models for the SDA collision predictor."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class RiskLevel(enum.StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MODERATE = "MODERATE"
    LOW = "LOW"
    NEGLIGIBLE = "NEGLIGIBLE"


class TLERecord(BaseModel):
    norad_id: int
    name: str
    line1: str
    line2: str
    epoch: datetime

    @field_validator("line1")
    @classmethod
    def validate_line1(cls, v: str) -> str:
        if not v.startswith("1 "):
            raise ValueError("TLE line 1 must start with '1 '")
        return v

    @field_validator("line2")
    @classmethod
    def validate_line2(cls, v: str) -> str:
        if not v.startswith("2 "):
            raise ValueError("TLE line 2 must start with '2 '")
        return v


class StateVector(BaseModel):
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]
    epoch: datetime


class CollisionProbability(BaseModel):
    probability: float
    miss_distance_km: float
    combined_sigma_km: float
    mahalanobis_distance: float
    hard_body_radius_km: float


class ConjunctionEvent(BaseModel):
    primary: int
    secondary: int
    primary_name: str = ""
    secondary_name: str = ""
    tca: datetime
    miss_distance_km: float
    relative_velocity_km_s: float
    risk: RiskLevel
    collision_probability: CollisionProbability | None = None


class ConjunctionRequest(BaseModel):
    norad_ids: list[int] | None = None
    hours: float = Field(default=24.0, gt=0, le=168.0)
    threshold_km: float = Field(default=10.0, gt=0, le=200.0)


class ManeuverRequest(BaseModel):
    primary_norad_id: int
    secondary_norad_id: int
    tca: datetime
    target_miss_km: float = Field(default=5.0, gt=0)
    lead_times_hours: list[float] | None = None


class MonteCarloRequest(BaseModel):
    primary_norad_id: int
    secondary_norad_id: int
    tca: datetime
    n_samples: int = Field(default=500, ge=1, le=5000)
    bstar_sigma_fraction: float = Field(default=0.1, ge=0.0, le=1.0)


class DecayEstimate(BaseModel):
    """Orbital decay and lifetime estimate."""
    norad_id: int
    name: str
    altitude_km: float
    perigee_km: float
    apogee_km: float
    eccentricity: float
    period_min: float
    bstar: float
    decay_rate_km_per_day: float
    estimated_lifetime_days: float
    estimated_lifetime_category: str
    reentry_risk: str
    solar_activity_note: str


class ManeuverOption(BaseModel):
    """A candidate avoidance maneuver."""
    direction: str
    delta_v_m_s: float
    burn_time: datetime
    lead_time_hours: float
    new_miss_distance_km: float
    fuel_mass_kg: float | None = None


class ManeuverPlan(BaseModel):
    """Complete maneuver plan for a conjunction event."""
    conjunction: ConjunctionEvent
    target_miss_km: float
    options: list[ManeuverOption]
    recommended: ManeuverOption | None
    warning: str | None = None


class MonteCarloResult(BaseModel):
    """Results from Monte Carlo miss distance analysis."""
    mean_miss_km: float
    std_miss_km: float
    median_miss_km: float
    percentile_5_km: float
    percentile_95_km: float
    min_miss_km: float
    max_miss_km: float
    n_samples: int
    miss_distances: list[float]
    collision_probability_mc: float


class SatelliteSummary(BaseModel):
    norad_id: int
    name: str
    epoch: datetime


class TLEFreshness(BaseModel):
    norad_id: int
    name: str
    epoch: datetime
    age_hours: float
    age_days: float
    freshness: str  # "FRESH", "AGING", "STALE", "EXPIRED"
    accuracy_warning: str | None = None


class SatelliteDetail(BaseModel):
    norad_id: int
    name: str
    line1: str
    line2: str
    epoch: datetime
    current_state: StateVector | None = None
    freshness: TLEFreshness | None = None
