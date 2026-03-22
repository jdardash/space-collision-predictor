"""Pre-tool hook: detect coordinate frame and unit errors in written code."""

import sys
import json
import re

data = json.load(sys.stdin)
content = data.get("tool_input", {}).get("content", "") or data.get("tool_input", {}).get("new_string", "") or ""
path = data.get("tool_input", {}).get("file_path", "")

# Only check Python source files in src/
if not path.endswith(".py") or "src/" not in path:
    sys.exit(0)

errors = []

# Check for WGS84 usage (should be WGS72)
if re.search(r'\bWGS84\b', content):
    errors.append("WGS84 detected — this project uses WGS72 for SGP4 compatibility")

# Check for ECEF usage (should be ECI)
if re.search(r'\bECEF\b', content, re.IGNORECASE) and "comment" not in content.lower():
    errors.append("ECEF reference detected — this project uses ECI frame only")

# Check for single-argument sgp4 calls (loses precision)
if re.search(r'\.sgp4\([^,)]+\)', content):
    errors.append("Single-argument sgp4() call — must use sgp4(jd, fr) split form")

# Check for naive datetime.now() (missing UTC)
if re.search(r'datetime\.now\(\)', content):
    errors.append("datetime.now() without timezone — use datetime.now(timezone.utc)")

if errors:
    msg = "FRAME/UNIT CHECK: " + "; ".join(errors)
    print(json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": msg
        }
    }))
else:
    sys.exit(0)
