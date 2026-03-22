# Space-Domain Awareness Collision Predictor - AI Analysis Instructions

This document provides context for AI/LLM systems analyzing this codebase.

## Repository Purpose

This is a **space-domain awareness collision prediction system** implementing:
- SGP4 orbital propagation from Two-Line Element sets
- Conjunction detection with two-phase screening pipeline
- Risk classification based on miss distance and relative velocity
- 3D interactive orbit visualization
- RESTful API for satellite tracking and conjunction analysis

## Critical Invariants

### 1. ECI FRAME ONLY
All positions and velocities are in Earth-Centered Inertial (ECI) frame:
```python
# Units: km for position, km/s for velocity
position = (x_km, y_km, z_km)
velocity = (vx_km_s, vy_km_s, vz_km_s)
```

### 2. WGS72 GRAVITY MODEL
SGP4 TLEs are generated with WGS72. Never use WGS84:
```python
sat = Satrec.twoline2rv(line1, line2, WGS72)  # CORRECT
```

### 3. JULIAN DATE SPLIT
Maintain precision by keeping JD as (jd, fr) pair:
```python
error, pos, vel = sat.sgp4(jd, fr)  # CORRECT - keeps precision
```

### 4. RISK CLASSIFICATION THRESHOLDS
Safety-critical constants — changes require review:
| Miss Distance | Velocity | Risk |
|--------------|----------|------|
| < 0.5 km | any | CRITICAL |
| < 1.0 km | any | HIGH |
| < 5.0 km | > 10 km/s | HIGH |
| < 5.0 km | any | MODERATE |
| < 10 km | any | LOW |
| >= 10 km | any | NEGLIGIBLE |

## Directory Structure

```
src/sda/
├── models.py        → Pydantic models (TLERecord, StateVector, ConjunctionEvent, RiskLevel)
├── propagator.py    → SGP4 wrapper, ECI propagation
├── conjunction.py   → Two-phase pipeline, risk classification
├── tle_store.py     → In-memory TLE catalog
├── visualization.py → Plotly 3D rendering
└── api.py           → FastAPI service
tests/               → pytest test suite
```

## Key Patterns to Look For

1. **Unit mismatches**: Mixing km and meters, or radians and degrees
2. **Frame errors**: Using ECEF or geodetic coordinates
3. **Gravity model**: WGS84 usage (should be WGS72)
4. **Time errors**: Naive datetimes (should be UTC), single-arg sgp4 calls
5. **Silent errors**: SGP4 error codes not checked
