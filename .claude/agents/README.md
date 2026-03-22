---
name: _readme
description: Agent documentation index (not an executable agent)
tools: []
---

# Claude Code Agents (10)

Specialized subagents organized like a Space-Domain Awareness operations center. Each agent has a focused role with specific tools and responsibilities.

## Agent Organization

```
┌─────────────────────────────────────────────────────────────────────┐
│                      YOU (Mission Director)                         │
│              Final decisions, mission planning                      │
└─────────────────────────────────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  ANALYSIS (3)   │     │ VALIDATION (3)  │     │  SUPPORT (4)    │
├─────────────────┤     ├─────────────────┤     ├─────────────────┤
│ orbit-analyst   │     │ safety-reviewer │     │ debugger        │
│ conjunction-    │     │ code-auditor    │     │ api-developer   │
│   predictor     │     │ test-validator  │     │ data-engineer   │
│ tle-researcher  │     │                 │     │ viz-developer   │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

---

## Analysis Division (3 agents)

Generate orbital predictions and analyze conjunction scenarios.

| Agent | Description | Model | When to Use |
|-------|-------------|-------|-------------|
| [orbit-analyst](orbit-analyst.md) | Analyzes orbital mechanics, validates propagation accuracy | sonnet | Verifying orbit predictions |
| [conjunction-predictor](conjunction-predictor.md) | Runs conjunction scenarios and evaluates risk classifications | sonnet | Conjunction analysis review |
| [tle-researcher](tle-researcher.md) | Researches TLE sources, validates TLE freshness and accuracy | haiku | TLE data quality issues |

---

## Validation Division (3 agents)

Ensure safety-critical code is correct and well-tested.

| Agent | Description | Model | When to Use |
|-------|-------------|-------|-------------|
| [safety-reviewer](safety-reviewer.md) | Adversarial reviewer for safety-critical orbital code. Default: REJECT | opus | **MANDATORY** before changing risk thresholds |
| [code-auditor](code-auditor.md) | Find bugs in propagation, coordinate frame errors, unit mismatches | sonnet | After implementing changes |
| [test-validator](test-validator.md) | Verify test coverage and correctness for orbital mechanics code | haiku | Quick test validation |

**Critical**: `safety-reviewer` MUST review any changes to `classify_risk()` or propagation engine before merge.

---

## Support Division (4 agents)

Debugging, API development, data engineering, and visualization.

| Agent | Description | Model | When to Use |
|-------|-------------|-------|-------------|
| [debugger](debugger.md) | Fast debugging for test failures and runtime errors | haiku | Quick debugging |
| [api-developer](api-developer.md) | FastAPI endpoint development and testing | sonnet | API changes |
| [data-engineer](data-engineer.md) | TLE ingestion pipeline and data quality | haiku | Data issues |
| [viz-developer](viz-developer.md) | Plotly 3D visualization development | sonnet | Visualization changes |

---

## Model Distribution

| Model | Count | Use Case | Agents |
|-------|-------|----------|--------|
| **opus** | 1 | Adversarial safety review | safety-reviewer |
| **sonnet** | 5 | Analysis, code review, development | orbit-analyst, conjunction-predictor, code-auditor, api-developer, viz-developer |
| **haiku** | 4 | Fast checks, data quality | tle-researcher, test-validator, debugger, data-engineer |

---

## Chain Integration

Agents are orchestrated through chains. See [chains/](../chains/) for definitions.

| Chain | Key Agents | Purpose |
|-------|-----------|---------|
| `safety-gauntlet` | 3 agents, 3 stages | **Required** before changing risk thresholds |
| `conjunction-analysis` | orbit-analyst, conjunction-predictor | Full conjunction review |
| `incident-response` | debugger, code-auditor, test-validator | Debugging workflow |

---

## Command -> Agent Mapping

| Command | Agent(s) Used |
|---------|---------------|
| `/sda:verify` | code-auditor, test-validator |
| `/sda:status` | orbit-analyst |
| `/sda:audit` | safety-reviewer, code-auditor |
| `/sda:conjunction` | conjunction-predictor |
| `/swarm` | Any parallel combination |

---

## Creating New Agents

1. Create `agents/agent-name.md` with YAML frontmatter:

```yaml
---
name: agent-name
description: One-line description
tools:
  - Read
  - Grep
model: opus  # or sonnet/haiku
---
```

2. Write clear mission statement and output format
3. Define specific checklist or workflow
4. Update this README

---

## Best Practices

1. **Use the right model** - haiku for fast checks, sonnet for analysis, opus for adversarial/complex
2. **Chain agents for complex tasks** - Analysis -> Validation -> Review
3. **Always run safety-reviewer** - MANDATORY before changing risk classification
4. **Document agent decisions** - Output goes to review reports
5. **Trust but verify** - Agents recommend, you decide
