# Space-Domain Awareness Collision Predictor

Orbital mechanics engine for satellite conjunction analysis. SGP4 propagation, risk classification, 3D visualization, FastAPI service.

## Session Continuity (Auto-Execute)

On every new conversation, automatically:
1. Read `docs/handoffs/LATEST.md` if it exists
2. Check `git branch --show-current` and `git status --short`
3. Present a 5-line session context block, then proceed

At session end ("done", "wrapping up", "session-end"), run `/session-end`.

## Quick Reference

```bash
pytest tests/ -x --tb=short                        # Run tests
.venv/Scripts/python -m sda.api                     # Start server (port 8000)
.venv/Scripts/python -m pytest tests/ --cov=src/sda # Coverage
```

## Critical Rules (Non-Negotiable)

1. **ECI FRAME ONLY**: All positions/velocities are Earth-Centered Inertial (km, km/s). Never mix coordinate frames.
2. **WGS72 ONLY**: SGP4 uses WGS72 gravity model. Never use WGS84. Hooks block it.
3. **JULIAN DATE PAIRS**: SGP4 uses `(jd, fr)` split — always use `datetime_to_jd()` helper, never combine into single float.
4. **TLE VALIDATION**: All TLE ingestion goes through `TLEStore.load_from_text()` which validates via `Satrec.twoline2rv()`.
5. **RISK THRESHOLDS**: Miss distance classification is safety-critical — changes to `classify_risk()` require safety-reviewer agent approval.
6. **safety-reviewer MANDATORY** before changing risk thresholds. Default verdict: REJECT.

## Directory Structure

```text
src/sda/
  models.py        — Pydantic models (TLERecord, StateVector, ConjunctionEvent, RiskLevel)
  propagator.py    — SGP4 wrapper, ECI propagation, vectorized NumPy mode
  conjunction.py   — Two-phase pipeline: coarse screen → fine refinement + risk classification
  tle_store.py     — In-memory TLE catalog, 2-line/3-line parser
  visualization.py — Plotly 3D Earth + orbit traces + conjunction markers
  api.py           — FastAPI service, CelesTrak seed, 7 endpoints
tests/
  test_propagator.py  — SGP4 engine tests (ISS LEO bounds, JD roundtrip)
  test_conjunction.py — Risk classification + pipeline smoke tests
  test_api.py         — FastAPI endpoint integration tests
.claude/              — AI orchestration (10 agents, 11 commands, 3 chains, prompts, hooks)
docs/handoffs/        — Session handoff files
```

Path-scoped rules in `.claude/rules/` auto-load when editing matching files.

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Health check + satellite count |
| `GET` | `/satellites` | List tracked satellites |
| `GET` | `/satellites/{norad_id}` | Detail + current state vector |
| `POST` | `/tle` | Ingest TLE text (body: `{"tle_text": "..."}`) |
| `DELETE` | `/satellites/{norad_id}` | Remove from tracking |
| `POST` | `/conjunctions` | Run analysis (body: `ConjunctionRequest`) |
| `GET` | `/conjunctions/visualize` | Interactive 3D Plotly HTML |

## Risk Classification (conjunction.py)

| Miss Distance | Relative Velocity | Risk |
|---------------|-------------------|------|
| < 0.5 km | any | CRITICAL |
| < 1.0 km | any | HIGH |
| < 5.0 km | > 10 km/s | HIGH |
| < 5.0 km | ≤ 10 km/s | MODERATE |
| < 10.0 km | any | LOW |
| ≥ 10.0 km | any | NEGLIGIBLE |

## Hooks (Safety)

**BLOCKED**: writes to `.env`, credentials; force push main; SQL DROP/TRUNCATE; WGS84/ECEF in source code
**LOGGED**: SubagentStart/SubagentStop → `logs/agent_activity.log`; test runs → `logs/tool_results.log`

## Agent Divisions (10 agents)

- **Analysis** (3): orbit-analyst, conjunction-predictor, tle-researcher
- **Validation** (3): safety-reviewer, code-auditor, test-validator
- **Support** (4): debugger, api-developer, data-engineer, viz-developer

## Parallel Agent Workflows

| Command / Chain | Purpose |
|----------------|---------|
| `/swarm <tasks>` | Launch N agents in parallel on different tasks |
| `safety-gauntlet` chain | Code audit → scenario testing → adversarial safety review |
| `conjunction-analysis` chain | Orbit validation → conjunction detection → visualization |
| `incident-response` chain | Debugger → code-audit + test-check → resolution |

## Key Commands

| Command | Purpose |
|---------|---------|
| `/sda:verify` | Pre-commit verification suite |
| `/sda:status` | Current system status |
| `/sda:conjunction` | Run conjunction analysis |
| `/sda:audit` | Full safety audit |
| `/swarm <tasks>` | Parallel agent orchestration |
| `/session-start` | Resume from last session |
| `/session-end` | Commit, push, and hand off |
| `/pr` | Create GitHub pull request |
| `/start-server` | Launch FastAPI dev server |
| `/verify` | Run tests + lint + checks |

## Testing & Performance

| Module | Coverage Target | Constraint |
|--------|----------------|------------|
| propagator | 90% | Unit < 100ms |
| conjunction | 85% | Integration < 5s |
| api | 80% | Uses TestClient |
| tle_store | 90% | Unit < 100ms |
