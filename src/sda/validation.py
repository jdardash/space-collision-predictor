"""Cross-method validation suite for collision probability.

Validates the Pc implementations against each other and against exact
mathematics, using three computationally independent approaches:

1. Closed form            — for zero miss and isotropic covariance,
                            Pc = 1 - exp(-R^2 / (2 sigma^2)) exactly.
2. Chan analytic series   — exact for any isotropic encounter covariance
                            (equivalent-area transform introduces no error
                            when sigma_x == sigma_y).
3. Foster 2D quadrature   — direct numerical integration; validated against
                            the series where the series is exact, and against
                            a self-convergence (high-resolution) reference in
                            the anisotropic regime.
4. Monte Carlo sampling   — method-independent stochastic ground truth,
                            checked to within 5 standard errors.

The legacy isotropic implementation (compute_collision_probability) and the
full-covariance pipeline (compute_collision_probability_full) are validated
against the same references, so every Pc path shipped by this package is
covered.

Run:  python -m sda.validation [--mc-samples N] [--json]
Exit status is non-zero if any check fails, so this doubles as a CI gate.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

import numpy as np

from sda.probability import (
    compute_collision_probability,
    compute_collision_probability_full,
    pc_chan,
    pc_foster,
    pc_monte_carlo_2d,
)

# Pass thresholds (relative error unless stated otherwise)
FOSTER_VS_REFERENCE_RTOL = 2e-3
LEGACY_VS_REFERENCE_RTOL = 5e-3
PIPELINE_VS_REFERENCE_RTOL = 2e-3
MC_SIGMA_BOUND = 5.0  # pass when |mc - ref| < 5 standard errors
MC_MIN_PC = 1e-5  # below this, sampling error dominates; MC check skipped


def _rotated_cov(sigma_x_km: float, sigma_y_km: float, angle_deg: float) -> np.ndarray:
    """2x2 covariance with principal sigmas rotated by angle_deg."""
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    rot = np.array([[c, -s], [s, c]])
    return np.asarray(rot @ np.diag([sigma_x_km**2, sigma_y_km**2]) @ rot.T)


def _isotropic_cases() -> list[dict]:
    """Isotropic scenarios where the Chan series is mathematically exact."""
    sigma = 0.100  # km
    hbr = 0.020  # km
    cases = []
    for d_over_sigma in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0):
        cases.append(
            {
                "name": f"isotropic d={d_over_sigma:g}sigma",
                "miss_2d": np.array([d_over_sigma * sigma, 0.0]),
                "cov_2d": np.eye(2) * sigma**2,
                "hbr": hbr,
                "isotropic": True,
                "sigma": sigma,
            }
        )
    # Larger hard body relative to sigma (stresses the series convergence)
    cases.append(
        {
            "name": "isotropic large-HBR (R=sigma)",
            "miss_2d": np.array([0.15, 0.0]),
            "cov_2d": np.eye(2) * 0.100**2,
            "hbr": 0.100,
            "isotropic": True,
            "sigma": 0.100,
        }
    )
    return cases


def _anisotropic_cases() -> list[dict]:
    """Anisotropic scenarios; reference is high-resolution Foster quadrature."""
    return [
        {
            "name": "aniso 3:1 head-on",
            "miss_2d": np.array([0.0, 0.0]),
            "cov_2d": _rotated_cov(0.300, 0.100, 0.0),
            "hbr": 0.020,
            "isotropic": False,
        },
        {
            "name": "aniso 3:1 rotated 30deg offset",
            "miss_2d": np.array([0.200, 0.100]),
            "cov_2d": _rotated_cov(0.300, 0.100, 30.0),
            "hbr": 0.020,
            "isotropic": False,
        },
        {
            "name": "aniso 10:1 along major axis",
            "miss_2d": np.array([0.300, 0.0]),
            "cov_2d": _rotated_cov(0.300, 0.030, 0.0),
            "hbr": 0.020,
            "isotropic": False,
        },
        {
            "name": "aniso 10:1 along minor axis",
            "miss_2d": np.array([0.0, 0.060]),
            "cov_2d": _rotated_cov(0.300, 0.030, 0.0),
            "hbr": 0.020,
            "isotropic": False,
        },
    ]


def _rel_err(value: float, reference: float) -> float:
    if reference == 0.0:
        return abs(value)
    return abs(value - reference) / reference


def _evaluate_case(case: dict, mc_samples: int) -> dict:
    """Run all applicable methods on one case; return a result row."""
    miss, cov, hbr = case["miss_2d"], case["cov_2d"], case["hbr"]

    foster = pc_foster(miss, cov, hbr)
    chan = pc_chan(miss, cov, hbr)

    if case["isotropic"]:
        reference = chan  # series is exact here
        ref_label = "Chan series (exact)"
    else:
        reference = pc_foster(miss, cov, hbr, n_radial=1000, n_theta=2000)
        ref_label = "Foster hi-res quadrature"

    checks: dict[str, dict] = {}
    checks["foster"] = {
        "value": foster,
        "rel_err": _rel_err(foster, reference),
        "passed": _rel_err(foster, reference) < FOSTER_VS_REFERENCE_RTOL,
    }
    # Chan is exact for isotropic; informational (no pass gate) for anisotropic
    chan_err = _rel_err(chan, reference)
    checks["chan"] = {
        "value": chan,
        "rel_err": chan_err,
        "passed": chan_err < FOSTER_VS_REFERENCE_RTOL if case["isotropic"] else True,
        "informational": not case["isotropic"],
    }

    if case["isotropic"]:
        # Closed form at zero miss: Pc = 1 - exp(-R^2 / (2 sigma^2))
        if float(np.linalg.norm(miss)) == 0.0:
            closed = 1.0 - math.exp(-(hbr**2) / (2.0 * case["sigma"] ** 2))
            checks["closed_form"] = {
                "value": closed,
                "rel_err": _rel_err(foster, closed),
                "passed": _rel_err(foster, closed) < FOSTER_VS_REFERENCE_RTOL,
            }
        # Legacy isotropic implementation (the conjunction-pipeline path)
        legacy = compute_collision_probability(
            miss_vector_km=np.array([miss[0], miss[1], 0.0]),
            rel_velocity_km_s=np.array([0.0, 0.0, 10.0]),
            sigma_primary_km=case["sigma"] / math.sqrt(2.0),
            sigma_secondary_km=case["sigma"] / math.sqrt(2.0),
            combined_radius_km=hbr,
        )["probability"]
        checks["legacy"] = {
            "value": legacy,
            "rel_err": _rel_err(legacy, reference),
            "passed": _rel_err(legacy, reference) < LEGACY_VS_REFERENCE_RTOL,
        }
        # Full-covariance pipeline wiring (3D in, projected internally)
        pipeline = compute_collision_probability_full(
            miss_vector_km=np.array([miss[0], miss[1], 0.0]),
            rel_velocity_km_s=np.array([0.0, 0.0, 10.0]),
            cov_primary_eci_km2=np.eye(3) * case["sigma"] ** 2 / 2.0,
            cov_secondary_eci_km2=np.eye(3) * case["sigma"] ** 2 / 2.0,
            combined_radius_km=hbr,
        )["probability"]
        checks["pipeline"] = {
            "value": pipeline,
            "rel_err": _rel_err(pipeline, reference),
            "passed": _rel_err(pipeline, reference) < PIPELINE_VS_REFERENCE_RTOL,
        }

    if reference >= MC_MIN_PC:
        mc = pc_monte_carlo_2d(miss, cov, hbr, n_samples=mc_samples)
        std_err = math.sqrt(reference * (1.0 - reference) / mc_samples)
        checks["monte_carlo"] = {
            "value": mc,
            "rel_err": _rel_err(mc, reference),
            "passed": abs(mc - reference) < MC_SIGMA_BOUND * std_err + 1e-12,
            "std_err": std_err,
        }

    return {
        "case": case["name"],
        "reference": reference,
        "reference_method": ref_label,
        "checks": checks,
        "passed": all(c["passed"] for c in checks.values()),
    }


def run_validation(mc_samples: int = 4_000_000) -> dict:
    """Run the full cross-method validation suite.

    Returns {"rows": [...], "all_passed": bool, "mc_samples": int}.
    """
    rows = [
        _evaluate_case(case, mc_samples)
        for case in _isotropic_cases() + _anisotropic_cases()
    ]
    return {
        "rows": rows,
        "all_passed": all(r["passed"] for r in rows),
        "mc_samples": mc_samples,
    }


def render_table(report: dict) -> str:
    """Render the validation report as a fixed-width text table."""
    lines = [
        "Collision probability cross-method validation",
        f"Monte Carlo samples per case: {report['mc_samples']:,}",
        "",
        f"{'Case':<34} {'Method':<12} {'Pc':>13} {'RelErr':>10}  Status",
        "-" * 82,
    ]
    for row in report["rows"]:
        lines.append(
            f"{row['case']:<34} {'reference':<12} {row['reference']:>13.6e} "
            f"{'':>10}  ({row['reference_method']})"
        )
        for method, check in row["checks"].items():
            if check.get("informational"):
                status = f"info (approx, err {check['rel_err']:.1%})"
            else:
                status = "PASS" if check["passed"] else "FAIL"
            lines.append(
                f"{'':<34} {method:<12} {check['value']:>13.6e} "
                f"{check['rel_err']:>10.2e}  {status}"
            )
        lines.append("-" * 82)
    verdict = "ALL CHECKS PASSED" if report["all_passed"] else "FAILURES DETECTED"
    lines.append(verdict)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-validate Pc implementations")
    parser.add_argument(
        "--mc-samples", type=int, default=4_000_000,
        help="Monte Carlo samples per case (default 4e6)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = parser.parse_args(argv)

    report = run_validation(mc_samples=args.mc_samples)
    if args.json:
        print(json.dumps(report, indent=2, default=float))
    else:
        print(render_table(report))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
