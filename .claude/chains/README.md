# Agent Chains

Predefined sequences of agents for common multi-step workflows.

## Available Chains

### safety-gauntlet.yaml
Full validation before changing safety-critical code. **Required before modifying risk thresholds.**

- Stage 1: code-auditor + test-validator (parallel)
- Stage 2: conjunction-predictor (scenario testing)
- Stage 3: safety-reviewer (adversarial - default REJECT)

### conjunction-analysis.yaml
End-to-end conjunction analysis and review.

- orbit-analyst → conjunction-predictor → viz-developer

### incident-response.yaml
Debugging workflow.

- debugger → code-auditor → test-validator

## Chain Schema

```yaml
name: chain-name
description: What this chain accomplishes
fail_fast: true
required_pass: all

stages:
  - name: step-1
    parallel:
      - agent: agent-a
      - agent: agent-b
    aggregate: all_pass

  - name: step-2
    depends_on: step-1
    agents:
      - name: final-check
        agent: agent-name
        pass_context: true
```

## Best Practices

1. **Order matters** - Critical checks first
2. **Fail fast** - Don't waste compute on doomed runs
3. **Parallelize** - Independent checks run together
4. **Timeout wisely** - Prevent infinite hangs
