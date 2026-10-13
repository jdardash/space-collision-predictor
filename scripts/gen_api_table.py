"""Regenerate the README API reference table from the live FastAPI route table.

Usage:
    python scripts/gen_api_table.py            # print the markdown table and counts
    python scripts/gen_api_table.py --write    # splice it into README.md between markers

The README table must never be edited by hand; this script is the single source.
Markers in README.md: ``<!-- api-table:start -->`` and ``<!-- api-table:end -->``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from fastapi.routing import APIWebSocketRoute

START = "<!-- api-table:start -->"
END = "<!-- api-table:end -->"

# Display order of the path prefixes; unknown prefixes sort after these.
GROUP_ORDER = [
    "/", "/health", "/metrics", "/space-weather", "/satellites", "/tle", "/conjunctions",
    "/maneuver", "/montecarlo", "/pc", "/passes", "/sensors", "/lookangles", "/dops",
    "/breakup", "/replay", "/reentries", "/fleet", "/report", "/alerts", "/spacetrack",
    "/doctor", "/agent", "/api", "/ws",
]


def _walk(routes) -> list:
    """Flatten FastAPI 0.139+ ``_IncludedRouter`` wrappers into plain route objects."""
    out: list = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            out.extend(_walk(inner.routes))
        else:
            out.append(route)
    return out


def _group_key(path: str) -> tuple[int, str]:
    for idx, prefix in enumerate(GROUP_ORDER):
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")):
            return (idx, path)
    return (len(GROUP_ORDER), path)


def collect_rows(app) -> tuple[list[tuple[str, str, str]], int, int]:
    """REST rows come from the OpenAPI document (complete paths, summaries);
    WebSocket rows from the flattened route table (not part of OpenAPI)."""
    rows: list[tuple[str, str, str]] = []
    rest = 0
    ws = 0
    for path, ops in app.openapi()["paths"].items():
        for method, spec in ops.items():
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            summary = (spec.get("summary") or spec.get("description") or "").strip()
            summary = summary.splitlines()[0] if summary else ""
            rest += 1
            rows.append((method.upper(), path, summary))
    for route in _walk(app.routes):
        if isinstance(route, APIWebSocketRoute):
            ws += 1
            rows.append(("WS", route.path, "WebSocket stream"))
    rows.sort(key=lambda r: (_group_key(r[1]), r[0]))
    return rows, rest, ws


def render(rows: list[tuple[str, str, str]], rest: int, ws: int) -> str:
    lines = [
        f"{rest} REST endpoints + {ws} WebSocket, with auto-generated docs at **/docs** "
        "(Swagger UI) and **/redoc**.",
        "",
        "| Method | Endpoint | Description |",
        "| ------ | -------- | ----------- |",
    ]
    for method, path, summary in rows:
        lines.append(f"| `{method}` | `{path}` | {summary} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="splice into README.md")
    parser.add_argument("--readme", default="README.md")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from sda.api import app

    rows, rest, ws = collect_rows(app)
    table = render(rows, rest, ws)
    if not args.write:
        print(table)
        return 0

    readme = Path(args.readme)
    text = readme.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if not pattern.search(text):
        print(f"markers {START} / {END} not found in {readme}", file=sys.stderr)
        return 1
    text = pattern.sub(f"{START}\n{table}\n{END}", text)
    readme.write_text(text, encoding="utf-8")
    print(f"wrote {rest} REST + {ws} WS rows to {readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
