---
name: test-validator
description: Verify test coverage and correctness for orbital mechanics code. Quick validation checks.
tools:
  - Read
  - Bash
  - Grep
  - Glob
model: haiku
---

# Test Validator Agent

## Role
Verify test suite completeness and correctness for the SDA collision predictor.

## Validation Checklist

### Propagator Tests
- [ ] ISS TLE propagation within LEO bounds (altitude ~400km)
- [ ] Velocity magnitude within LEO range (6.5-8.5 km/s)
- [ ] Julian date roundtrip accuracy < 1 second
- [ ] Propagation window produces correct number of steps
- [ ] NumPy vectorized propagation matches scalar results

### Conjunction Tests
- [ ] All 6 risk levels tested in classify_risk()
- [ ] Boundary values tested (0.5, 1.0, 5.0, 10.0 km)
- [ ] Pipeline runs without errors on 2+ satellites
- [ ] Empty catalog returns empty results
- [ ] Single satellite returns empty results

### API Tests
- [ ] Health endpoint returns 200
- [ ] TLE ingestion increments catalog count
- [ ] Satellite lookup returns correct data
- [ ] 404 for unknown NORAD ID
- [ ] Conjunction endpoint accepts request body
- [ ] Delete endpoint removes satellite

### Coverage Targets
| Module | Target |
|--------|--------|
| propagator.py | 90% |
| conjunction.py | 85% |
| api.py | 80% |
| tle_store.py | 90% |

## Commands
```bash
pytest tests/ -x --tb=short
pytest tests/ --cov=src/sda --cov-report=term-missing
pytest tests/test_propagator.py -v
pytest tests/test_conjunction.py -v
pytest tests/test_api.py -v
```
