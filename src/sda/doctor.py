"""Environment diagnostics: `python -m sda.doctor` and GET /doctor.

Answers the question an operator asks before trusting a result: is this install
actually complete? It reports the interpreter, the required and optional
packages with their versions, which SDA_* variables are set (values masked when
they are credentials), and whether the bundled demo catalog parses through the
normal TLE ingestion path.

Network checks are opt-in (`--network`) and never run from the HTTP route, so
the default invocation is offline and deterministic. Each offline check runs
against a shared deadline: one that raises or does not answer in time is a FAIL
row, so a stuck check can delay the report by the deadline but never hold it. A check
whose earlier run is still alive is not started again, so repeated requests cannot
pile up threads behind it.
Only hard failures, a
missing required package or a catalog that will not parse, produce a non-zero
exit code; an absent optional package is a warning because the service degrades
cleanly without it.

Output is ASCII only, so it renders correctly in a Windows console.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field

MIN_PYTHON = (3, 11)

# Wall-clock budget, in seconds, shared by all offline checks in one run.
CHECK_TIMEOUT_SECONDS = 10.0

STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"

REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("sgp4", "SGP4 propagation"),
    ("numpy", "vector math"),
    ("fastapi", "HTTP service"),
    ("pydantic", "models"),
    ("httpx", "outbound HTTP"),
    ("uvicorn", "ASGI server"),
    ("plotly", "3D visualisation"),
)

OPTIONAL_PACKAGES: tuple[tuple[str, str], ...] = (
    ("yaml", "fleet definitions in YAML"),
    ("fpdf", "PDF conjunction reports (fpdf2)"),
    ("scipy", "extended probability methods"),
    ("websockets", "WebSocket live tracking"),
)

# Every variable the service reads, with whether its value is a secret.
ENV_KEYS: tuple[tuple[str, bool], ...] = (
    ("SDA_HOST", False),
    ("SDA_PORT", False),
    ("SDA_CORS_ORIGINS", False),
    ("SDA_RATE_LIMIT", False),
    ("SDA_RATE_LIMIT_PATHS", False),
    ("SDA_RATE_LIMIT_TRUST_FORWARDED", False),
    ("SDA_SPACETRACK_USER", True),
    ("SDA_SPACETRACK_PASS", True),
    ("SDA_WEBHOOK_URLS", True),
)

NETWORK_TARGETS: tuple[tuple[str, str], ...] = (
    ("CelesTrak", "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle"),
    ("OpenSky", "https://opensky-network.org/api/states/all"),
    ("USGS", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_day.geojson"),
    ("Space-Track", "https://www.space-track.org"),
)


class CheckResult(BaseModel):
    """One diagnostic line."""

    section: str
    name: str
    status: str
    detail: str = ""


class DoctorReport(BaseModel):
    """All checks plus the counts a caller needs to decide an exit code."""

    checks: list[CheckResult] = Field(default_factory=list)
    ok: int = 0
    warnings: int = 0
    failures: int = 0
    network_checked: bool = False

    @property
    def healthy(self) -> bool:
        return self.failures == 0


def mask(value: str) -> str:
    """Redact a secret while still showing that something is set."""
    if not value:
        return "(empty)"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * 6}{value[-2:]} (len {len(value)})"


def _module_version(module: Any) -> str:
    for attr in ("__version__", "version", "VERSION"):
        raw = getattr(module, attr, None)
        if isinstance(raw, str) and raw:
            return raw
    return "unknown version"


def check_python() -> CheckResult:
    version = platform.python_version()
    if sys.version_info < MIN_PYTHON:
        return CheckResult(
            section="runtime",
            name="python",
            status=STATUS_FAIL,
            detail=f"{version}; this project needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
        )
    return CheckResult(
        section="runtime",
        name="python",
        status=STATUS_OK,
        detail=f"{version} on {platform.system() or 'unknown OS'}",
    )


def check_packages() -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, purpose in REQUIRED_PACKAGES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            results.append(
                CheckResult(
                    section="required",
                    name=name,
                    status=STATUS_FAIL,
                    detail=f"not importable; needed for {purpose}",
                )
            )
            continue
        results.append(
            CheckResult(
                section="required",
                name=name,
                status=STATUS_OK,
                detail=_module_version(module),
            )
        )

    for name, purpose in OPTIONAL_PACKAGES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            results.append(
                CheckResult(
                    section="optional",
                    name=name,
                    status=STATUS_WARN,
                    detail=f"not installed; {purpose} unavailable",
                )
            )
            continue
        results.append(
            CheckResult(
                section="optional",
                name=name,
                status=STATUS_OK,
                detail=_module_version(module),
            )
        )
    return results


def check_environment(env: dict[str, str] | None = None) -> list[CheckResult]:
    source = dict(os.environ) if env is None else env
    results: list[CheckResult] = []
    for key, secret in ENV_KEYS:
        raw = source.get(key)
        if raw is None or raw == "":
            results.append(
                CheckResult(section="env", name=key, status=STATUS_SKIP, detail="not set")
            )
            continue
        detail = mask(raw) if secret else raw
        results.append(CheckResult(section="env", name=key, status=STATUS_OK, detail=detail))
    return results


def check_catalog() -> CheckResult:
    """Parse the bundled demo TLEs through the normal ingestion path."""
    try:
        from sda.demo_tles import DEMO_TLE_TEXT
        from sda.tle_store import TLEStore

        store = TLEStore()
        count = store.load_from_text(DEMO_TLE_TEXT)
    except Exception as exc:
        return CheckResult(
            section="catalog",
            name="demo TLEs",
            status=STATUS_FAIL,
            detail=f"load failed: {exc}",
        )
    if count <= 0:
        return CheckResult(
            section="catalog",
            name="demo TLEs",
            status=STATUS_FAIL,
            detail="no TLEs parsed from the bundled catalog",
        )
    return CheckResult(
        section="catalog",
        name="demo TLEs",
        status=STATUS_OK,
        detail=f"{count} objects parsed and validated (WGS72)",
    )


def check_network(timeout: float = 6.0) -> list[CheckResult]:
    """Short reachability probes. Never called from the HTTP route."""
    import httpx

    results: list[CheckResult] = []
    for name, url in NETWORK_TARGETS:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url)
            status = STATUS_OK if resp.status_code < 400 else STATUS_WARN
            results.append(
                CheckResult(
                    section="network",
                    name=name,
                    status=status,
                    detail=f"HTTP {resp.status_code}",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    section="network",
                    name=name,
                    status=STATUS_WARN,
                    detail=f"unreachable: {type(exc).__name__}",
                )
            )
    return results


_CheckFn = Callable[[], CheckResult | list[CheckResult]]


def _capture(fn: _CheckFn, box: dict[str, Any]) -> None:
    try:
        box["value"] = fn()
    except Exception as exc:  # a check that raises is a failed check, not a 500
        box["error"] = exc


class _Run:
    """One execution of a check on its own daemon thread.

    Every call that arrives while it is alive shares it instead of starting another.
    """

    def __init__(self, name: str, fn: _CheckFn, deadline: float) -> None:
        self.box: dict[str, Any] = {}
        self.started = time.monotonic()
        self.deadline = deadline
        self.thread = threading.Thread(
            target=_capture, args=(fn, self.box), name=f"doctor-{name}", daemon=True
        )
        self.thread.start()


# GET /doctor is unauthenticated and sits outside the rate limiter, so a check that
# never returns must cost one thread in total, not one per request. Runs are keyed by
# the check function, which is a stable module-level object in production.
_runs: dict[tuple[str, str, _CheckFn], _Run] = {}
_runs_lock = threading.Lock()


def _join_or_start(section: str, name: str, fn: _CheckFn, deadline: float) -> tuple[_Run, bool]:
    """Return the live run of this check and whether an earlier call started it."""
    with _runs_lock:
        for key in [key for key, run in _runs.items() if not run.thread.is_alive()]:
            del _runs[key]
        key = (section, name, fn)
        existing = _runs.get(key)
        if existing is not None:
            return existing, True
        run = _runs[key] = _Run(name, fn, deadline)
        return run, False


def _run_bounded(tasks: Sequence[tuple[str, str, _CheckFn]], timeout: float) -> list[CheckResult]:
    """Run each check on its own daemon thread and collect them against one deadline.

    A thread cannot be killed, so a check that misses the deadline is abandoned: it is
    reported as FAIL and its thread runs on until the call it is stuck in returns. The
    checks run side by side, so the total is bounded by `timeout` however many hang.

    A check whose earlier run is still alive is not started again. A call that arrives
    while that run is within its own deadline waits for it and shares its result; once
    the run is past its deadline the call reports it as still running and returns at
    once, so a stuck check holds no request for the full budget a second time.
    """
    deadline = time.monotonic() + timeout
    running = [
        (section, name, *_join_or_start(section, name, fn, deadline))
        for section, name, fn in tasks
    ]

    results: list[CheckResult] = []
    for section, name, run, shared in running:
        run.thread.join(max(0.0, min(deadline, run.deadline) - time.monotonic()))
        if run.thread.is_alive():
            if shared:
                age = time.monotonic() - run.started
                detail = (
                    f"still running from an earlier request ({age:.0f}s so far); "
                    "not started again"
                )
            else:
                detail = f"no answer within {timeout:g}s; check abandoned"
        elif "error" in run.box:
            detail = f"raised {type(run.box['error']).__name__}: {run.box['error']}"
        else:
            value = run.box["value"]
            results.extend(value if isinstance(value, list) else [value])
            continue
        results.append(CheckResult(section=section, name=name, status=STATUS_FAIL, detail=detail))
    return results


def run_checks(network: bool = False, timeout: float | None = None) -> DoctorReport:
    """Run every offline check, plus the network probes when asked.

    The offline checks share one deadline (`timeout`, default CHECK_TIMEOUT_SECONDS).
    The network probes keep their own per-request timeout and are CLI-only.
    """
    budget = CHECK_TIMEOUT_SECONDS if timeout is None else timeout
    checks = _run_bounded(
        [
            ("runtime", "python", check_python),
            ("packages", "imports", check_packages),
            ("env", "variables", check_environment),
            ("catalog", "demo TLEs", check_catalog),
        ],
        budget,
    )
    if network:
        checks.extend(check_network())

    report = DoctorReport(checks=checks, network_checked=network)
    report.ok = sum(1 for c in checks if c.status == STATUS_OK)
    report.warnings = sum(1 for c in checks if c.status == STATUS_WARN)
    report.failures = sum(1 for c in checks if c.status == STATUS_FAIL)
    return report


def format_table(report: DoctorReport, width: int = 58) -> str:
    """Render the report as an ASCII table."""
    rows = [(c.section, c.name, c.status, c.detail) for c in report.checks]
    section_w = max([len("SECTION")] + [len(r[0]) for r in rows])
    name_w = max([len("CHECK")] + [len(r[1]) for r in rows])
    status_w = max([len("STATUS")] + [len(r[2]) for r in rows])
    detail_w = max([len("DETAIL")] + [min(width, len(r[3])) for r in rows])

    border = (
        "+"
        + "-" * (section_w + 2)
        + "+"
        + "-" * (name_w + 2)
        + "+"
        + "-" * (status_w + 2)
        + "+"
        + "-" * (detail_w + 2)
        + "+"
    )

    def line(section: str, name: str, status: str, detail: str) -> str:
        clipped = detail if len(detail) <= width else detail[: width - 3] + "..."
        return (
            f"| {section.ljust(section_w)} | {name.ljust(name_w)} "
            f"| {status.ljust(status_w)} | {clipped.ljust(detail_w)} |"
        )

    out = [
        "space-collision-predictor doctor",
        border,
        line("SECTION", "CHECK", "STATUS", "DETAIL"),
        border,
    ]
    out.extend(line(*row) for row in rows)
    out.append(border)
    out.append(
        f"{report.ok} ok, {report.warnings} warning(s), {report.failures} failure(s); "
        f"network checks {'run' if report.network_checked else 'skipped'}"
    )
    if report.failures:
        out.append("Result: FAIL. Fix the rows marked FAIL before trusting any analysis.")
    else:
        out.append("Result: OK. Warnings are optional features, not defects.")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sda.doctor",
        description="Diagnose the space-collision-predictor install and configuration.",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="also probe CelesTrak, OpenSky, USGS and Space-Track (off by default)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    args = parser.parse_args(argv)

    report = run_checks(network=args.network)
    if args.json:
        print(json.dumps(report.model_dump(), indent=2))
    else:
        print(format_table(report))
    return 0 if report.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
