---
description: Show current system status for the SDA collision predictor
---

# /sda:status

Quick overview of the system's current state.

## Process

### 1. Catalog Status
```bash
python -c "
from sda.tle_store import TLEStore
store = TLEStore()
print(f'Satellites tracked: {store.count()}')
" 2>/dev/null || echo "Store not available (server not running)"
```

### 2. Test Status
```bash
pytest tests/ -q --tb=no 2>&1 | tail -1
```

### 3. Git Status
```bash
git status --short
git log --oneline -3
```

### 4. Dependency Status
```bash
pip show sgp4 plotly fastapi numpy 2>/dev/null | grep -E "Name|Version"
```

## Output Format
```
## SDA Status — [Date]

**Tests**: 21 passed
**Branch**: `main` (0 uncommitted changes)
**Dependencies**: sgp4 2.22, plotly 5.18, fastapi 0.110
**Last commit**: [message]
```
