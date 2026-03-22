# /sda:verify

Run the complete pre-commit verification suite for the SDA collision predictor.

## Prerequisites
- Python venv active (.venv)
- All dependencies installed

## Usage
```
/sda:verify
```

## Steps
1. **Unit Tests**:
   ```bash
   pytest tests/ -x --tb=short
   ```
2. **Frame Check**: Search for WGS84 or ECEF usage (should be WGS72/ECI):
   ```bash
   grep -rn "WGS84\|ECEF\|ecef\|wgs84" src/
   ```
3. **SGP4 API Check**: Search for legacy sgp4 imports:
   ```bash
   grep -rn "from sgp4.earth_gravity" src/
   ```
4. **Risk Threshold Check**: Verify classify_risk() thresholds match CLAUDE.md table.
5. **Secrets Check**:
   ```bash
   git grep -E "KEY|SECRET|TOKEN" -- ':!*.md'
   ```
6. **Report Summary**.

## Expected Output
```
=== SDA Verification Suite ===

[1/5] Unit Tests
  pytest: PASS (21 passed in 1.5s)

[2/5] Coordinate Frame Check
  No WGS84/ECEF usage: PASS

[3/5] SGP4 API Check
  Using sgp4.api (v2+): PASS

[4/5] Risk Thresholds
  classify_risk() matches spec: PASS

[5/5] Secrets Check
  No secrets found: PASS

=== ALL CHECKS PASSED ===
Ready for commit.
```

## Pass/Fail Criteria
- **PASS**: All 5 checks pass
- **FAIL**: Any check fails (blocks commit)

## Related Commands
- `/sda:audit`: Full safety audit with adversarial review
- `/sda:status`: Quick system status
