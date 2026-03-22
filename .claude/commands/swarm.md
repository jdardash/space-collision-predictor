---
description: Launch multiple named background agents on parallel tasks
argument-hint: "<task-spec>"
---

# /swarm — Parallel Agent Orchestration

Launch multiple specialized agents in parallel from a single conversation.

## Argument Format

Free-form task description. Examples:
- `/swarm validate: code audit + test validation + safety review`
- `/swarm fix: test_propagator, test_conjunction, test_api (3 independent test failures)`
- `/swarm analyze: orbit validation + conjunction detection + visualization`

## Process

### 1. Parse Tasks
Break the argument into independent work items. Each needs:
- A **name** (short, for agent addressing)
- A **subagent_type** (match to one of the 10 agents)
- A **prompt** (clear, self-contained)
- Whether it needs **worktree isolation** (only if writing same files)

### 2. Launch Agents in Parallel
Use Agent tool with `run_in_background: true` for each task.
CRITICAL: launch ALL agents in a single message.

### 3. Monitor Progress
As agents complete, you'll be notified. Do NOT poll or sleep.

### 4. Synthesize Results
```
## Swarm Results

| Agent | Status | Key Finding |
|-------|--------|-------------|
| code-audit | Done | No unit mismatches found |
| test-check | Done | 2 missing edge case tests |
| safety-review | Done | CONDITIONAL PASS |

### Action Items
1. Add edge case tests identified by test-check
```

## Agent Type Quick Reference

| Task | Agent Type |
|------|-----------|
| Orbit analysis | orbit-analyst |
| Conjunction review | conjunction-predictor |
| TLE data quality | tle-researcher |
| Code bugs | code-auditor |
| Safety review | safety-reviewer |
| Test validation | test-validator |
| Debugging | debugger |
| API development | api-developer |
| Data issues | data-engineer |
| Visualization | viz-developer |
| Codebase search | Explore |
