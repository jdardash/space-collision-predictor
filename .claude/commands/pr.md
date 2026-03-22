# /pr [--draft]

Creates a GitHub pull request with a properly formatted conventional commit title.

## Prerequisites
- Current branch has commits not on main
- `gh` CLI authenticated
- Changes committed

## Steps

1. **Check State**: `git status` and `git diff --stat`
2. **Review Commits**: `git log origin/main..HEAD --oneline`
3. **Determine PR Title**:
   - **Type**: feat|fix|perf|test|docs|refactor|chore
   - **Scope**: propagator|conjunction|api|viz|store|models (optional)
   - **Summary**: Imperative, capitalized, no period
4. **Push Branch**: `git push -u origin HEAD`
5. **Create PR**:
```bash
gh pr create --title "<type>(<scope>): <summary>" --body "$(cat <<'EOF'
## Summary
<What this PR does>

## Changes
- <Change 1>
- <Change 2>

## Testing
- [ ] `pytest tests/ -x --tb=short`
- [ ] No WGS84/ECEF usage
- [ ] Risk thresholds unchanged (or safety-reviewed)

## Checklist
- [ ] PR title follows conventional commits
- [ ] Tests included
- [ ] Units consistent (km, km/s)
EOF
)"
```

## Title Examples
```
feat(conjunction): Add coplanar orbit handling
fix(propagator): Correct JD split precision loss
perf(conjunction): Vectorize pairwise distance computation
test(api): Add TLE ingestion edge case tests
docs: Update risk classification table
```

## Related Commands
- `/sda:verify`: Run before creating PR
- `/sda:audit`: Run for safety-critical changes
