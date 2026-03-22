# Workflow Orchestration

YAML-based workflow definitions for multi-step processes.

## Available Workflows

### conjunction-pipeline.yaml
End-to-end conjunction analysis from TLE ingestion to visualization.

**Steps:**
1. TLE freshness check (tle-researcher)
2. Orbit validation (orbit-analyst)
3. Conjunction detection (conjunction-predictor)
4. Visualization (viz-developer)

### daily-scan.yaml
Daily automated conjunction scan for all tracked objects.

**Steps:**
1. Refresh TLE catalog from CelesTrak
2. Run 24-hour conjunction analysis
3. Generate risk report
4. Flag critical/high events

## Workflow Schema

```yaml
name: workflow-name
description: What this workflow accomplishes
triggers:
  - manual
  - schedule: "0 6 * * *"
steps:
  - name: step-name
    agent: agent-name
    prompt: "Task description"
    timeout: 300
    on_failure: abort | continue | retry
outputs:
  - name: report
    from: final-step.result
```
