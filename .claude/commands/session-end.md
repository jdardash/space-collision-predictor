---
description: Commit, push, and create lean handoff for next session
argument-hint: "[topic/summary]"
---

# /session-end — Ship It and Hand Off

Commit all work, push to GitHub, and leave a lean handoff.

## Process

### 1. Commit All Work
```bash
git status --short
git log --oneline -10 --since="8 hours ago"
```
- Stage and commit with conventional commit messages
- Never commit `.env`, credentials, or secrets

### 2. Write LATEST.md
Overwrite `docs/handoffs/LATEST.md` (15 lines max):

```markdown
# Handoff — {YYYY-MM-DD}
Branch: {branch} | Commits: {count this session}

## Done
- {What got done, 2-5 bullets max}

## Broke / Blocked
- {What's broken or blocking, or "None"}

## Next
1. {Single most important next step}
2. {Second priority}

## Key Files
- `{path:line}` — {why to read it}
```

### 3. Push to GitHub
```bash
git add docs/handoffs/LATEST.md
git commit -m "docs: session handoff — {topic}"
git push origin {current-branch}
```

## Related Commands
- `/session-start`: Load LATEST.md at next session
