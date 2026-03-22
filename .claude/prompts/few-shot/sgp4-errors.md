# SGP4 Error Handling Examples

## Example 1: Silently Ignoring Errors

**Code Under Review:**
```python
error, pos, vel = sat.sgp4(jd, fr)
return StateVector(position_km=pos, velocity_km_s=vel, epoch=dt)
```

**Analysis:**
SGP4 error code is computed but never checked. If `error != 0`, the position/velocity values are garbage but get returned as valid data.

**Fix:**
```python
error, pos, vel = sat.sgp4(jd, fr)
if error != 0:
    raise RuntimeError(f"SGP4 propagation error code {error}")
return StateVector(position_km=pos, velocity_km_s=vel, epoch=dt)
```

---

## Example 2: Error Codes Reference

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Continue |
| 1 | Mean elements: ecc >= 1.0 or < -0.001 | Reject TLE |
| 2 | Mean motion < 0.0 | Reject TLE |
| 3 | Pert elements: ecc < 0.0 or > 1.0 | Skip timestep |
| 4 | Semi-latus rectum < 0.0 | Skip timestep |
| 5 | Epoch elements sub-orbital | Warn: satellite decaying |
| 6 | Satellite has decayed | Remove from catalog |

---

## Example 3: Batch Propagation Error Handling

**Code Under Review:**
```python
for i in range(n_steps):
    error, pos, vel = sat.sgp4(jds[i], frs[i])
    positions[i] = pos
    velocities[i] = vel
```

**Analysis:**
No error checking in the loop. Invalid positions get stored and used in distance calculations.

**Fix:**
```python
valid = np.ones(n_steps, dtype=bool)
for i in range(n_steps):
    error, pos, vel = sat.sgp4(jds[i], frs[i])
    if error != 0:
        valid[i] = False
        continue
    positions[i] = pos
    velocities[i] = vel
# Return only valid entries
return positions[valid], velocities[valid], [t for t, v in zip(times, valid) if v]
```
