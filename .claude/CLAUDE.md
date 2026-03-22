# .claude/ Directory

AI orchestration layer for the Space-Domain Awareness Collision Predictor: 10 agents, 11 commands, 3 chains, prompts, safety hooks, path-scoped rules.

All details (agent divisions, commands, chains, hooks) are in the root [CLAUDE.md](../CLAUDE.md). This file documents the `.claude/` directory structure.

## Structure

- `agents/` — 10 specialist agents (.md with YAML frontmatter: name, description, tools, model)
- `commands/` — 11 slash commands (.md templates, `/namespace:action` format). Key: `/swarm` for parallel orchestration
- `chains/` — 3 multi-agent pipelines (.yaml). Key: `safety-gauntlet`, `conjunction-analysis`, `incident-response`
- `rules/` — 4 path-scoped rules (.md with `paths:` frontmatter, auto-loaded when editing matching files)
- `hooks.json` — Safety hooks (block dangerous commands, detect frame/unit errors, log agent activity)
- `hooks/` — Hook scripts (detect_frame_errors.py)
- `settings.json` — Permissions and auto-allow rules
- `settings.local.json` — Local overrides (Agent Teams enabled)
- `prompts/` — Prompt library (system contexts, few-shot examples, guardrails, mission templates)
- `workflows/` — Operational workflows (conjunction-pipeline, daily-scan)
- `repomix-instructions.md` — Context for external AI analysis tools
- `ide-hints.json` — VSCode file annotations, quick actions, diagnostics

## Adding New Components

- **Agent**: Copy existing `.md` in `agents/`, update frontmatter
- **Command**: Copy `commands/_TEMPLATE.md`
- **Chain**: Create YAML in `chains/`
- **Rule**: Create `.md` in `rules/` with `paths:` frontmatter
- **Hooks/permissions**: Edit `hooks.json` / `settings.json`
