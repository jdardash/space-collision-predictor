# Unit Mismatch Detection Examples

## Example 1: km vs meters

**Code Under Review:**
```python
def check_altitude(position_km):
    altitude = np.linalg.norm(position_km) - 6371000  # BUG!
    return altitude > 200
```

**Analysis:**
CRITICAL unit mismatch. `position_km` is in km, but 6371000 is Earth's radius in meters. The subtraction produces nonsensical results.

**Fix:**
```python
def check_altitude(position_km):
    altitude = np.linalg.norm(position_km) - 6371  # km
    return altitude > 200  # 200 km
```

---

## Example 2: Gravity Model Mismatch

**Code Under Review:**
```python
from sgp4.api import Satrec, WGS84
sat = Satrec.twoline2rv(line1, line2, WGS84)
```

**Analysis:**
Wrong gravity model. SGP4 TLEs are generated with WGS72. Using WGS84 introduces systematic errors in propagation.

**Fix:**
```python
from sgp4.api import Satrec, WGS72
sat = Satrec.twoline2rv(line1, line2, WGS72)
```

---

## Example 3: Julian Date Precision Loss

**Code Under Review:**
```python
jd, fr = datetime_to_jd(dt)
error, pos, vel = sat.sgp4(jd + fr, 0.0)  # BUG!
```

**Analysis:**
Precision loss. Adding `fr` (small number ~0.5) to `jd` (large number ~2460000) loses significant digits. The SGP4 API takes split (jd, fr) specifically to avoid this.

**Fix:**
```python
jd, fr = datetime_to_jd(dt)
error, pos, vel = sat.sgp4(jd, fr)  # Keep split
```

---

## Example 4: Timezone Missing

**Code Under Review:**
```python
now = datetime.now()
sv = propagate_at(satrec, now)
```

**Analysis:**
`datetime.now()` returns local time without timezone info. SGP4 expects UTC. This could produce position errors of thousands of km.

**Fix:**
```python
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
sv = propagate_at(satrec, now)
```

---

## Detection Patterns

```python
# Dangerous patterns to grep for:
r'WGS84'                         # Wrong gravity model
r'datetime\.now\(\)'             # Missing UTC timezone
r'sgp4\([^,]+\)'                 # Single-arg sgp4 call
r'6371\d{3}'                     # Earth radius in meters (should be km)
r'ECEF|ecef'                     # Wrong coordinate frame
r'np\.sin\([^r]'                 # Sin without radians conversion
```
