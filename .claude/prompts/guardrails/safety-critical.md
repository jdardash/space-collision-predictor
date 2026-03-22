# Safety-Critical Code Guardrails

## Absolute Rules

### 1. Risk Classification is Immutable Without Review
- NEVER change risk thresholds (0.5, 1.0, 5.0, 10.0 km) without safety-reviewer approval
- NEVER change the relationship between miss distance and risk level
- NEVER add new risk levels or modify RiskLevel enum without review
- Any threshold change requires updating ALL tests

### 2. Unit Consistency is Non-Negotiable
- ALL positions in kilometers (km)
- ALL velocities in km/s
- ALL times in UTC
- ALL angles in degrees (TLE format) or radians (computation) with explicit conversion
- Earth radius = 6371 km

### 3. Coordinate Frame Purity
- ALL computations in ECI (Earth-Centered Inertial) frame
- NO ECEF (Earth-Centered Earth-Fixed) without explicit conversion function
- NO geodetic coordinates in distance calculations
- WGS72 gravity model only (SGP4 standard)

### 4. Error Propagation
- SGP4 error codes MUST be checked (never silently ignored)
- Propagation failures MUST raise exceptions, not return zeros
- TLE validation MUST use Satrec.twoline2rv() (catches invalid TLEs)
- Division by zero MUST be protected in velocity calculations

### 5. Data Integrity
- NEVER modify test reference data (ISS TLE used as canonical test)
- NEVER skip Phase 2 refinement in conjunction pipeline
- NEVER change coarse margin multiplier (10x) without analysis
- NEVER return unsorted conjunction results

## When These Rules May Be Bent

Never. These rules exist because violations have real-world consequences:
- Incorrect risk classification → missed collision → debris cascade
- Unit mismatch → prediction off by orders of magnitude
- Frame error → position wrong by thousands of km
- Silent SGP4 error → garbage predictions presented as valid
