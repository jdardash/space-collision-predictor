---
description: Run conjunction analysis on tracked satellites
argument-hint: "[hours] [threshold_km]"
---

# /sda:conjunction — Run Conjunction Analysis

## Usage
```
/sda:conjunction                  # Default: 24h window, 10km threshold
/sda:conjunction 48 5.0           # 48h window, 5km threshold
```

## Process

### 1. Check Catalog
Verify at least 2 satellites are tracked.

### 2. Run Analysis
```python
from sda.conjunction import find_conjunctions
from sda.tle_store import TLEStore

store = TLEStore()
# Load TLEs if needed
events = find_conjunctions(store, hours=24.0, threshold_km=10.0)
```

### 3. Report Results

```markdown
## Conjunction Report — [Date]

| # | Primary | Secondary | TCA | Miss (km) | Rel Vel (km/s) | Risk |
|---|---------|-----------|-----|-----------|----------------|------|
| 1 | ISS | CSS | 2024-02-14 15:30 | 3.42 | 12.1 | HIGH |

**Summary**: X conjunctions detected, Y critical, Z high risk
```

### 4. Visualize (Optional)
If conjunctions found, offer to open the 3D visualization:
```
http://localhost:8000/conjunctions/visualize?hours=24&threshold_km=10
```

## Related Commands
- `/sda:verify`: Verify system before analysis
- `/start-server`: Start API server for visualization
