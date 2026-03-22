---
paths:
  - "tests/**"
---

# Test Rules

- Unit tests must complete in < 100ms each
- Integration tests (conjunction pipeline, API) must complete in < 5s
- Use ISS TLE (NORAD 25544) as the canonical test case for propagation
- Propagation tests should verify LEO bounds: altitude ~400km, velocity ~7.7 km/s
- API tests use FastAPI `TestClient` (synchronous)
- Conjunction tests: always test `classify_risk()` for all risk levels
