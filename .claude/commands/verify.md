# /verify

Run the full verification suite before committing.

1. Run `pytest tests/ -x --tb=short` — all tests must pass
2. Check for any imports of `WGS84` or legacy `sgp4.earth_gravity` — flag as errors
3. Verify `classify_risk()` thresholds in conjunction.py match the table in CLAUDE.md
4. Report results summary
