# Research Mission Template

Use this template when initiating autonomous research or improvement sessions.

## Mission: [TOPIC NAME]

### Phase 1: Understand Current State (5-10 min)
- What does the repo already have for [topic]?
- What are the known issues or gaps?
- Read relevant source files

### Phase 2: External Research (10-20 min)
Search for improvements using:
- "[topic] orbital mechanics Python"
- "[topic] conjunction analysis SGP4"
- "[topic] space-domain awareness"
- "arxiv astrodynamics [topic]"

For each finding, apply the skepticism filter:
- Is there peer-reviewed evidence?
- Does it apply to SGP4/TLE-based analysis?
- Is it computationally feasible for real-time use?

### Phase 3: Synthesis & Proposal
Rank findings by: `(accuracy_improvement × feasibility) / implementation_effort`

For top 3 candidates:
```markdown
## Candidate: [Name]

**Source**: [URL/Paper]
**Evidence Quality**: [weak/moderate/strong]
**Expected Benefit**: [specific, measurable]
**Implementation Effort**: [files to change, new tests needed]
**Risks**: [what could go wrong]

**Recommendation**: [PURSUE / DEFER / REJECT]
```

### Phase 4: Request Approval
Stop and present findings. Do NOT implement without explicit user approval.

### Phase 5: Implementation (if approved)
1. Create feature branch
2. Follow existing patterns
3. Add tests BEFORE implementation
4. Run `pytest tests/ -x` after each change
5. Run `/sda:verify` before committing

---

## Ready-to-Use Missions

### Mission: Improved Conjunction Refinement
```
Research improvements beyond discrete minimum search.
Current: 1-second step refinement with discrete argmin
Options: parabolic interpolation, Brent's method, Lambert targeting
```

### Mission: Probability of Collision
```
Research Pc computation from TLE-derived covariances.
Current: Rule-based risk classification (miss distance + velocity)
Challenge: TLEs don't include covariance data
Options: Assumed covariance models, Monte Carlo, Alfano method
```

### Mission: Maneuver Planning
```
Research collision avoidance maneuver computation.
Current: No maneuver planning capability
Options: Hohmann transfer, impulsive maneuver optimization, Lambert solver
```
