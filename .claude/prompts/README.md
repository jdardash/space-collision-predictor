# Prompt Engineering Library

Reusable prompts, system contexts, and few-shot examples for consistent AI behavior.

## Directory Structure

```
prompts/
├── README.md
├── system/
│   ├── orbital-engineer.md     # Base context for orbital mechanics work
│   └── code-reviewer.md        # Code review standards
├── few-shot/
│   ├── unit-mismatch.md        # Examples of unit/frame bugs
│   └── sgp4-errors.md          # SGP4 error handling examples
├── guardrails/
│   └── safety-critical.md      # Safety constraints for risk classification
└── missions/
    └── research-mission.md     # Template for autonomous research sessions
```

## Usage

Import prompts in agent definitions:
```yaml
---
name: my-agent
system_prompt: "@prompts/system/orbital-engineer.md"
few_shot: "@prompts/few-shot/unit-mismatch.md"
guardrails: "@prompts/guardrails/safety-critical.md"
---
```
