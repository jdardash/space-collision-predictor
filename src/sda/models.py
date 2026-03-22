"""Data models for the SDA collision predictor."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, field_validator


class RiskLevel(str, enum.Enum):
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


class ConjunctionEvent(BaseModel):
    primary: int
    secondary: int
    primary_name: str = ""
    secondary_name: str = ""
    tca: datetime
    miss_distance_km: float
    relative_velocity_km_s: float
    risk: RiskLevel


class ConjunctionRequest(BaseModel):
    norad_ids: list[int] | None = None
    hours: float = 24.0
    threshold_km: float = 10.0


class SatelliteSummary(BaseModel):
    norad_id: int
    name: str
    epoch: datetime


class SatelliteDetail(BaseModel):
    norad_id: int
    name: str
    line1: str
    line2: str
    epoch: datetime
    current_state: StateVector | None = None
