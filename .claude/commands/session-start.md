---
description: Load context from last session and current repo state
argument-hint: "[topic filter]"
---

# /session-start — Resume from Last Session

## Process

### 1. Read Handoff
```bash
cat docs/handoffs/LATEST.md 2>/dev/null || echo "No handoff found"
```

### 2. Check Repo State
```bash
git branch --show-current
git status --short
git log --oneline -5
```

### 3. Run Quick Health Check
```bash
pytest tests/ -q --tb=no
```

### 4. Present Briefing (6 lines)

```text
## Session Start — {date}
**Branch**: `{branch}` ({n} uncommitted changes)
**Last session**: {1-line summary from LATEST.md}
**Tests**: {X passed / Y failed}
**Next**: {from LATEST.md priorities}
**Stale?**: {flag if LATEST.md date is >3 days old}
```

### 5. Confirm
Ask: "Continue with these priorities, or redirect?"
