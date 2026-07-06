"""Tests for the Pc cross-method validation suite."""

import json

from sda.validation import main, render_table, run_validation

MC_SAMPLES = 300_000  # reduced for test speed; CLI default is 4e6


def test_all_checks_pass():
    report = run_validation(mc_samples=MC_SAMPLES)
    failures = [
        (row["case"], method)
        for row in report["rows"]
        for method, check in row["checks"].items()
        if not check["passed"]
    ]
    assert report["all_passed"], f"validation failures: {failures}"


def test_covers_isotropic_and_anisotropic_regimes():
    report = run_validation(mc_samples=MC_SAMPLES)
    names = [row["case"] for row in report["rows"]]
    assert any("isotropic" in n for n in names)
    assert any("aniso" in n for n in names)
    assert len(names) >= 10


def test_every_pc_path_is_validated():
    report = run_validation(mc_samples=MC_SAMPLES)
    methods = {m for row in report["rows"] for m in row["checks"]}
    assert {"foster", "chan", "legacy", "pipeline", "monte_carlo", "closed_form"} <= methods


def test_render_table_output():
    report = run_validation(mc_samples=MC_SAMPLES)
    table = render_table(report)
    assert "ALL CHECKS PASSED" in table
    assert "FAIL\n" not in table
    assert "reference" in table


def test_cli_json_output(capsys):
    exit_code = main(["--mc-samples", str(MC_SAMPLES), "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["all_passed"] is True


def test_cli_table_output(capsys):
    exit_code = main(["--mc-samples", str(MC_SAMPLES)])
    assert exit_code == 0
    assert "ALL CHECKS PASSED" in capsys.readouterr().out
