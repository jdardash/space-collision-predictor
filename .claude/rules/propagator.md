---
paths:
  - "src/sda/propagator.py"
---

# Propagator Rules

- Always use `WGS72` gravity model (SGP4 standard), never WGS84
- Use `sgp4.api` module (v2+), not legacy `sgp4.earth_gravity` imports
- Julian dates must be split as `(jd, fr)` pair — use `datetime_to_jd()` helper
- All datetimes must be UTC-aware (`timezone.utc`)
- Propagation errors (non-zero error code) must raise `RuntimeError`, never silently return zeros
- Position units: km. Velocity units: km/s. No exceptions.
