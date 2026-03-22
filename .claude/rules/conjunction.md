---
paths:
  - "src/sda/conjunction.py"
---

# Conjunction Pipeline Rules

- Two-phase approach is mandatory: coarse screen (60s) then fine refinement (1s)
- Coarse margin must be `threshold_km * 10` to avoid missing approaches
- Risk classification thresholds are safety-critical — any change requires updating tests
- Results must be sorted: CRITICAL first, then by ascending miss distance
- All distance computations use Euclidean norm in ECI frame
- Never skip Phase 2 refinement — coarse TCA can be off by up to 30 seconds
