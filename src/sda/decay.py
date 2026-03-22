"""Atmospheric drag decay and orbital lifetime estimation.

Estimates remaining orbital lifetime for LEO satellites based on
altitude, ballistic coefficient (BSTAR), and solar activity proxy.

Reference: King-Hele, "Satellite Orbits in an Atmosphere" (1987).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from dataclasses import dataclass

from sgp4.api import Satrec, WGS72

from sda.models import TLERecord


EARTH_RADIUS_KM = 6371.0
EARTH_MU_KM3_S2 = 398600.4418


@dataclass
class DecayEstimate:
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
    estimated_lifetime_category: str  # "days", "weeks", "months", "years", "decades", "stable"
    reentry_risk: str  # "IMMINENT", "HIGH", "MODERATE", "LOW", "MINIMAL"
    solar_activity_note: str


# Atmospheric density model (simplified exponential, Harris-Priester reference)
# (base_altitude_km, scale_height_km, density_kg_m3_at_base)
ATMOSPHERE_LAYERS = [
    (100, 5.9, 5.3e-7),
    (150, 18.0, 2.1e-9),
    (200, 28.0, 2.5e-10),
    (250, 35.0, 6.1e-11),
    (300, 40.0, 1.8e-11),
    (350, 45.0, 6.0e-12),
    (400, 53.0, 2.2e-12),
    (450, 62.0, 8.2e-13),
    (500, 70.0, 3.2e-13),
    (600, 88.0, 5.2e-14),
    (700, 120.0, 8.5e-15),
    (800, 170.0, 1.4e-15),
    (900, 220.0, 2.5e-16),
    (1000, 300.0, 4.8e-17),
]


def _atmospheric_density(altitude_km: float) -> float:
    """Estimate atmospheric density at given altitude using exponential model."""
    if altitude_km < 100:
        return 1.2  # sea level-ish
    if altitude_km > 1200:
        return 0.0  # negligible drag

    # Find bracketing layer
    for i in range(len(ATMOSPHERE_LAYERS) - 1):
        h0, H0, rho0 = ATMOSPHERE_LAYERS[i]
        h1, _, _ = ATMOSPHERE_LAYERS[i + 1]
        if h0 <= altitude_km < h1:
            return rho0 * math.exp(-(altitude_km - h0) / H0)

    # Above last defined layer
    h0, H0, rho0 = ATMOSPHERE_LAYERS[-1]
    return rho0 * math.exp(-(altitude_km - h0) / H0)


def _solar_activity_factor(f107: float = 150.0) -> float:
    """Scale factor for atmospheric density based on F10.7 solar flux.

    F10.7 ranges: ~70 (solar min) to ~250 (solar max).
    Density roughly scales 5-10x between min and max at 400 km.
    """
    # Normalize to moderate activity (F10.7 = 150)
    return (f107 / 150.0) ** 1.5


def estimate_decay(
    tle: TLERecord,
    f107_solar_flux: float = 150.0,
) -> DecayEstimate:
    """Estimate orbital decay rate and remaining lifetime.

    Parameters
    ----------
    tle : TLE record for the satellite
    f107_solar_flux : F10.7 solar radio flux in SFU (default 150 = moderate)

    Returns
    -------
    DecayEstimate with lifetime prediction
    """
    sat = Satrec.twoline2rv(tle.line1, tle.line2, WGS72)

    # Extract orbital elements
    eccentricity = sat.ecco
    mean_motion_rev_day = sat.no_kozai * (1440.0 / (2 * math.pi))  # rad/min → rev/day
    period_min = 1440.0 / mean_motion_rev_day if mean_motion_rev_day > 0 else 90.0

    # Semi-major axis from period
    period_s = period_min * 60.0
    a_km = (EARTH_MU_KM3_S2 * (period_s / (2 * math.pi)) ** 2) ** (1 / 3)

    # Perigee and apogee
    perigee_km = a_km * (1 - eccentricity) - EARTH_RADIUS_KM
    apogee_km = a_km * (1 + eccentricity) - EARTH_RADIUS_KM
    altitude_km = (perigee_km + apogee_km) / 2.0

    bstar = sat.bstar

    # Use mean motion derivative (ndot) from TLE for decay rate
    # ndot is in rev/day² (stored as rev/day²/2 in TLE line 1)
    # da/dt = -(2/3) * a * (ndot / n) where n = mean motion
    ndot = sat.ndot  # rad/min/min (already doubled in sgp4)
    n_rad_min = sat.no_kozai

    # Convert ndot to useful units: da/dt in km/day
    if abs(n_rad_min) > 1e-15:
        # da/dt = -(2/3) * a * (ndot/n)
        # ndot in rad/min², convert to per-day²: * (1440²)
        # n in rad/min, convert to per-day: * 1440
        # ratio is same: ndot/n in 1/min, convert to 1/day: * 1440
        ratio = ndot / n_rad_min  # 1/min
        da_dt_km_per_day = abs((2.0 / 3.0) * a_km * ratio * 1440.0)
    else:
        da_dt_km_per_day = 0.0

    # Apply solar activity scaling
    solar_scale = _solar_activity_factor(f107_solar_flux)
    decay_rate = da_dt_km_per_day * solar_scale

    # Clamp to reasonable values
    decay_rate = max(decay_rate, 1e-8)

    # Lifetime estimate
    if perigee_km < 180:
        lifetime_days = max(1.0, perigee_km / max(decay_rate, 0.1))
    elif decay_rate > 0:
        # Simple linear extrapolation (conservative for circular orbits)
        lifetime_days = max(1.0, (perigee_km - 150.0) / decay_rate)
    else:
        lifetime_days = 1e6  # essentially stable

    # Categorize
    if lifetime_days < 7:
        category = "days"
        risk = "IMMINENT"
    elif lifetime_days < 30:
        category = "weeks"
        risk = "HIGH"
    elif lifetime_days < 365:
        category = "months"
        risk = "MODERATE"
    elif lifetime_days < 3650:
        category = "years"
        risk = "LOW"
    elif lifetime_days < 36500:
        category = "decades"
        risk = "MINIMAL"
    else:
        category = "stable"
        risk = "MINIMAL"

    # Solar activity note
    if f107_solar_flux < 100:
        solar_note = "Solar minimum — reduced atmospheric drag, longer lifetime"
    elif f107_solar_flux < 180:
        solar_note = "Moderate solar activity — nominal drag conditions"
    else:
        solar_note = "Solar maximum — elevated atmospheric drag, shorter lifetime"

    return DecayEstimate(
        norad_id=tle.norad_id,
        name=tle.name,
        altitude_km=round(altitude_km, 1),
        perigee_km=round(perigee_km, 1),
        apogee_km=round(apogee_km, 1),
        eccentricity=round(eccentricity, 6),
        period_min=round(period_min, 2),
        bstar=bstar,
        decay_rate_km_per_day=round(decay_rate, 6),
        estimated_lifetime_days=round(lifetime_days, 1),
        estimated_lifetime_category=category,
        reentry_risk=risk,
        solar_activity_note=solar_note,
    )
