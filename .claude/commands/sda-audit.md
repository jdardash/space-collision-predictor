---
description: Full safety audit of the SDA collision predictor
---

# /sda:audit — Full Safety Audit

Run comprehensive safety validation of the collision prediction system.

## Process

### 1. Run Verification Suite
Execute `/sda:verify` first. If any check fails, stop here.

### 2. Code Audit (code-auditor agent)
Launch code-auditor to review:
- Unit consistency (km, km/s throughout)
- Coordinate frame correctness (ECI only)
- SGP4 error handling
- Risk classification thresholds
- Numerical stability

### 3. Safety Review (safety-reviewer agent)
Launch safety-reviewer for adversarial review:
- Attack all unit assumptions
- Attack coordinate frame usage
- Attack risk classification boundaries
- Attack numerical edge cases
- Default verdict: REJECT

### 4. Report
Present unified audit report with verdict.

## Output Format

```markdown
# SDA Safety Audit — [Date]

## Verification Suite: [PASS/FAIL]
## Code Audit: [PASS/FAIL]
- [Findings summary]

## Safety Review: [REJECT / CONDITIONAL PASS / PASS]
- [Key attack results]

## Overall Verdict: [APPROVED / BLOCKED]
```

## When to Use
- Before changing risk classification thresholds
- Before modifying propagation engine
- Before deploying to production
- After major refactoring

## Related Commands
- `/sda:verify`: Quick verification
- `/sda:conjunction`: Run analysis after audit passes
