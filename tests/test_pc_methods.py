"""Pc methods: Alfano series, usage checks, maximum Pc, remediation, realism.

Every case is offline, deterministic and small. Where a closed form exists it
is the oracle; otherwise the oracle is a computationally independent method
(Chan's series on isotropic covariances, a high-resolution Foster quadrature
on anisotropic ones).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.constants import DEFAULT_COMBINED_RADIUS_KM
from sda.probability import (
    CHAN_ASPECT_RATIO_LIMIT,
    PC_METHODS,
    chan_reduction_error_estimate,
    chi_square_cdf,
    classify_orbit_regime,
    compute_collision_probability_full,
    compute_pc_for_conjunction,
    covariance_condition_number,
    covariance_realism_scale,
    dilution_max_pc,
    frisbee_max_pc,
    pc_alfano,
    pc_alfano_quadrature,
    pc_by_method,
    pc_chan,
    pc_foster,
    pc_usage_checks,
    regime_default_covariance_rtn,
    remediate_covariance,
)
from sda.routes.pc import router as pc_router

HBR = 0.020  # km, the default combined hard-body radius


def _rotated_cov(sigma_x_km: float, sigma_y_km: float, angle_deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    rot = np.array([[c, -s], [s, c]])
    return np.asarray(rot @ np.diag([sigma_x_km**2, sigma_y_km**2]) @ rot.T)


def _hi_res_foster(miss, cov, hbr):
    return pc_foster(miss, cov, hbr, n_radial=1000, n_theta=2000)


# ---------------------------------------------------------------------------
# Alfano series
# ---------------------------------------------------------------------------


class TestAlfanoSeries:
    def test_matches_closed_form_at_zero_miss(self):
        # Isotropic, zero miss: Pc = 1 - exp(-R^2 / (2 sigma^2)) exactly.
        sigma = 0.100
        closed = 1.0 - math.exp(-(HBR**2) / (2.0 * sigma**2))
        value = pc_alfano(np.zeros(2), np.eye(2) * sigma**2, HBR)
        assert value == pytest.approx(closed, rel=1e-9)

    @pytest.mark.parametrize("d_over_sigma", [0.0, 0.5, 1.0, 2.0, 4.0, 8.0])
    def test_matches_the_exact_chan_series_when_isotropic(self, d_over_sigma):
        # Chan's equivalent-area reduction is exact for sigma_x == sigma_y, so
        # it is an independent oracle for the strip series here.
        sigma = 0.100
        miss = np.array([d_over_sigma * sigma, 0.0])
        cov = np.eye(2) * sigma**2
        assert pc_alfano(miss, cov, HBR) == pytest.approx(pc_chan(miss, cov, HBR), rel=1e-6)

    @pytest.mark.parametrize(
        "miss,cov",
        [
            (np.zeros(2), _rotated_cov(0.300, 0.100, 0.0)),
            (np.array([0.200, 0.100]), _rotated_cov(0.300, 0.100, 30.0)),
            (np.array([0.300, 0.0]), _rotated_cov(0.300, 0.030, 0.0)),
            (np.array([0.0, 0.060]), _rotated_cov(0.300, 0.030, 0.0)),
        ],
    )
    def test_matches_high_resolution_foster_when_anisotropic(self, miss, cov):
        reference = _hi_res_foster(miss, cov, HBR)
        assert pc_alfano(miss, cov, HBR) == pytest.approx(reference, rel=2e-3)

    def test_large_hard_body_relative_to_sigma(self):
        # R = sigma stresses the strip count; Chan is still exact (isotropic).
        miss = np.array([0.150, 0.0])
        cov = np.eye(2) * 0.100**2
        assert pc_alfano(miss, cov, 0.100) == pytest.approx(pc_chan(miss, cov, 0.100), rel=1e-6)

    def test_convergence_metadata_is_reported(self):
        result = pc_alfano_quadrature(np.array([0.150, 0.050]), _rotated_cov(0.2, 0.05, 20.0), HBR)
        assert result.converged is True
        assert result.intervals >= 32
        assert result.intervals <= 16384
        assert result.intervals % 2 == 0  # Simpson needs an even interval count
        assert result.relative_change < 1e-12
        assert result.probability == pytest.approx(pc_alfano(
            np.array([0.150, 0.050]), _rotated_cov(0.2, 0.05, 20.0), HBR
        ))

    def test_term_count_grows_with_a_harder_integrand(self):
        cov = np.eye(2) * 0.100**2
        easy = pc_alfano_quadrature(np.zeros(2), cov, 0.100)
        hard = pc_alfano_quadrature(np.array([0.4, 0.0]), cov, 0.0005)
        assert hard.intervals >= easy.intervals

    def test_tightening_the_tolerance_does_not_move_the_answer(self):
        miss, cov = np.array([0.100, 0.030]), _rotated_cov(0.150, 0.060, 15.0)
        loose = pc_alfano_quadrature(miss, cov, HBR, tol=1e-9).probability
        tight = pc_alfano_quadrature(miss, cov, HBR, tol=1e-14).probability
        assert loose == pytest.approx(tight, rel=1e-8)

    def test_degenerate_inputs(self):
        cov = np.eye(2) * 0.100**2
        assert pc_alfano(np.zeros(2), cov, 0.0) == 0.0
        assert pc_alfano(np.array([100.0, 0.0]), cov, HBR) == pytest.approx(0.0, abs=1e-30)

    def test_probability_is_bounded(self):
        # A hard body far larger than the covariance must saturate at 1.
        value = pc_alfano(np.zeros(2), np.eye(2) * 1e-8, 1.0)
        assert 0.0 <= value <= 1.0
        assert value == pytest.approx(1.0, abs=1e-9)


class TestMethodDispatch:
    @pytest.mark.parametrize("method", PC_METHODS)
    def test_every_named_method_runs(self, method):
        value = pc_by_method(method, np.array([0.05, 0.0]), np.eye(2) * 0.01, HBR)
        assert 0.0 <= value <= 1.0

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError, match="unknown Pc method"):
            pc_by_method("kessler", np.zeros(2), np.eye(2), HBR)


# ---------------------------------------------------------------------------
# Chan validity limit
# ---------------------------------------------------------------------------


class TestChanReductionError:
    def test_zero_for_an_isotropic_covariance(self):
        # sigma_x == sigma_y makes the reduction exact, so the estimate is 0.
        est = chan_reduction_error_estimate(np.array([0.1, 0.05]), np.eye(2) * 0.01, HBR)
        assert est == pytest.approx(0.0, abs=1e-15)

    def test_zero_for_a_zero_hard_body_radius(self):
        cov = _rotated_cov(0.2, 0.05, 0.0)
        assert chan_reduction_error_estimate(np.array([0.1, 0.0]), cov, 0.0) == 0.0

    def test_grows_with_elongation(self):
        miss = np.array([0.05, 0.05])
        mild = chan_reduction_error_estimate(miss, _rotated_cov(0.10, 0.08, 0.0), 0.050)
        harsh = chan_reduction_error_estimate(miss, _rotated_cov(0.10, 0.005, 0.0), 0.050)
        assert harsh > mild >= 0.0

    def test_a_large_estimate_accompanies_a_large_actual_error(self):
        # Elongated encounter plane: Chan's equivalent-area value is far from
        # the Foster reference, and the estimator flags it.
        miss = np.array([0.020, 0.010])
        cov = _rotated_cov(0.300, 0.004, 0.0)
        actual = abs(pc_chan(miss, cov, 0.050) - _hi_res_foster(miss, cov, 0.050))
        actual /= _hi_res_foster(miss, cov, 0.050)
        assert actual > 0.10
        assert chan_reduction_error_estimate(miss, cov, 0.050) > 0.05


# ---------------------------------------------------------------------------
# Covariance remediation
# ---------------------------------------------------------------------------


class TestRemediation:
    def test_a_positive_definite_matrix_is_left_alone(self):
        cov = _rotated_cov(0.2, 0.1, 25.0)
        fixed, changed = remediate_covariance(cov, HBR)
        assert changed is False
        assert np.allclose(fixed, cov)

    def test_a_negative_eigenvalue_is_clipped(self):
        cov = np.array([[1e-4, 0.0], [0.0, -1e-6]])
        fixed, changed = remediate_covariance(cov, HBR)
        assert changed is True
        assert np.linalg.eigvalsh(fixed).min() > 0.0
        np.linalg.cholesky(fixed)  # must not raise

    def test_a_singular_matrix_becomes_usable(self):
        cov = np.outer([1.0, 2.0], [1.0, 2.0]) * 1e-4  # rank 1
        fixed, changed = remediate_covariance(cov, HBR)
        assert changed is True
        assert covariance_condition_number(fixed) < float("inf")

    def test_asymmetry_is_averaged_away(self):
        cov = np.array([[1e-4, 2e-5], [0.0, 4e-4]])
        fixed, _ = remediate_covariance(cov, HBR)
        assert np.allclose(fixed, fixed.T)
        assert fixed[0][1] == pytest.approx(1e-5)

    def test_a_3x3_input_is_handled(self):
        cov = np.diag([1e-4, 1e-4, -1e-8])
        fixed, changed = remediate_covariance(cov, HBR)
        assert changed is True
        assert fixed.shape == (3, 3)
        assert np.linalg.eigvalsh(fixed).min() > 0.0

    def test_non_finite_entries_are_rejected(self):
        with pytest.raises(ValueError, match="non-finite"):
            remediate_covariance(np.array([[float("nan"), 0.0], [0.0, 1e-4]]), HBR)

    def test_clip_floor_is_tied_to_the_hard_body_radius(self):
        cov = np.diag([1e-4, -1e-9])
        small = remediate_covariance(cov, 0.001)[0]
        large = remediate_covariance(cov, 1.0)[0]
        assert np.linalg.eigvalsh(large).min() > np.linalg.eigvalsh(small).min()


class TestFullCovariancePath:
    def _kwargs(self, cov_secondary):
        return {
            "miss_vector_km": np.array([0.150, 0.0, 0.0]),
            "rel_velocity_km_s": np.array([0.0, 0.0, 12.0]),
            "cov_primary_eci_km2": np.diag([1e-2, 1e-2, 1e-2]),
            "cov_secondary_eci_km2": cov_secondary,
            "combined_radius_km": HBR,
        }

    def test_a_non_positive_definite_input_no_longer_produces_garbage(self):
        bad = np.array([[1e-4, 0.0, 0.0], [0.0, -2e-2, 0.0], [0.0, 0.0, 1e-4]])
        result = compute_collision_probability_full(**self._kwargs(bad))
        assert result["covariance_remediated"] is True
        for key in ("probability", "probability_chan", "probability_alfano"):
            assert math.isfinite(result[key])
            assert 0.0 <= result[key] <= 1.0
        assert math.isfinite(result["mahalanobis_distance"])

    def test_a_clean_input_is_not_flagged_as_remediated(self):
        good = np.diag([1e-4, 1e-4, 1e-4])
        result = compute_collision_probability_full(**self._kwargs(good))
        assert result["covariance_remediated"] is False

    def test_alfano_is_reported_alongside_foster_and_chan(self):
        good = np.diag([1e-4, 1e-4, 1e-4])
        result = compute_collision_probability_full(**self._kwargs(good))
        assert result["probability_alfano"] == pytest.approx(result["probability"], rel=2e-3)
        assert set(result["encounter_covariance_km2"][0]) is not None
        assert len(result["encounter_covariance_km2"]) == 2

    def test_zero_relative_velocity_is_a_defined_zero(self):
        result = compute_collision_probability_full(
            miss_vector_km=np.array([0.1, 0.0, 0.0]),
            rel_velocity_km_s=np.zeros(3),
            cov_primary_eci_km2=np.eye(3) * 1e-4,
            cov_secondary_eci_km2=np.eye(3) * 1e-4,
            combined_radius_km=HBR,
        )
        assert result["probability"] == 0.0
        assert result["probability_alfano"] == 0.0


class TestConjunctionWrapper:
    STATES = {
        "pos_primary_km": (6800.0, 0.0, 0.0),
        "vel_primary_km_s": (0.0, 7.5, 0.0),
        "pos_secondary_km": (6800.0, 0.1, 0.05),
        "vel_secondary_km_s": (0.0, -7.5, 0.1),
    }

    def test_the_default_is_still_the_legacy_path(self):
        result = compute_pc_for_conjunction(**self.STATES)
        assert result["method"] == "legacy"
        assert {"probability", "miss_distance_km", "combined_sigma_km"} <= set(result)

    @pytest.mark.parametrize("method", PC_METHODS)
    def test_every_method_keeps_the_legacy_dict_shape(self, method):
        legacy = compute_pc_for_conjunction(**self.STATES)
        result = compute_pc_for_conjunction(**self.STATES, method=method)
        assert set(legacy) <= set(result)
        assert result["method"] == method
        assert result["miss_distance_km"] == pytest.approx(legacy["miss_distance_km"])

    def test_the_methods_agree_with_the_legacy_quadrature(self):
        legacy = compute_pc_for_conjunction(**self.STATES)["probability"]
        for method in PC_METHODS:
            value = compute_pc_for_conjunction(**self.STATES, method=method)["probability"]
            assert value == pytest.approx(legacy, rel=5e-3)

    def test_an_unknown_method_raises(self):
        with pytest.raises(ValueError, match="unknown Pc method"):
            compute_pc_for_conjunction(**self.STATES, method="chan2")


# ---------------------------------------------------------------------------
# Maximum Pc
# ---------------------------------------------------------------------------


class TestFrisbeeMaxPc:
    def test_bound_is_above_the_known_covariance_value(self):
        miss = np.array([0.500, 0.0])
        cov = np.diag([0.010**2, 0.010**2])
        result = frisbee_max_pc(miss, cov, HBR)
        assert result.bounded is True
        assert result.max_pc > result.pc_known_only
        assert result.mahalanobis_distance > 1.0

    def test_the_maximizing_covariance_is_the_documented_rank_one_sum(self):
        miss = np.array([0.300, 0.400])  # |r| = 0.5
        cov = np.diag([0.010**2, 0.010**2])
        result = frisbee_max_pc(miss, cov, HBR)
        ka_sq = float(miss @ np.linalg.inv(cov) @ miss)
        expected_added = 0.25 * (ka_sq - 1.0) / ka_sq
        assert result.added_variance_km2 == pytest.approx(expected_added, rel=1e-9)
        unit = miss / np.linalg.norm(miss)
        expected = cov + expected_added * np.outer(unit, unit)
        assert np.allclose(np.asarray(result.combined_covariance_km2), expected)

    def test_a_covariance_wider_than_the_miss_distance_is_already_maximal(self):
        miss = np.array([0.005, 0.0])
        cov = np.diag([0.100**2, 0.100**2])
        result = frisbee_max_pc(miss, cov, HBR)
        assert result.bounded is False
        assert result.max_pc == pytest.approx(result.pc_known_only)
        assert result.added_variance_km2 == 0.0

    def test_the_bound_holds_against_a_sweep_of_added_covariances(self):
        miss = np.array([0.400, 0.0])
        cov = np.diag([0.020**2, 0.008**2])
        bound = frisbee_max_pc(miss, cov, HBR).max_pc
        rng = np.random.default_rng(7)
        for _ in range(60):
            factor = rng.uniform(0.0, 1.0, size=(2, 2))
            added = factor @ factor.T * rng.uniform(1e-6, 1e-1)
            assert pc_alfano(miss, cov + added, HBR) <= bound * (1.0 + 1e-9)

    @pytest.mark.parametrize("method", PC_METHODS)
    def test_each_method_can_evaluate_the_bound(self, method):
        result = frisbee_max_pc(np.array([0.3, 0.0]), np.diag([1e-4, 1e-4]), HBR, method=method)
        assert result.method == method
        assert 0.0 <= result.max_pc <= 1.0

    def test_a_non_positive_definite_known_covariance_is_remediated(self):
        result = frisbee_max_pc(np.array([0.2, 0.0]), np.diag([1e-4, -1e-8]), HBR)
        assert math.isfinite(result.max_pc)
        assert 0.0 <= result.max_pc <= 1.0


class TestDilutionMaxPc:
    def test_reproduces_the_closed_form_dilution_maximum(self):
        # Isotropic, d >> R: max Pc = (R/d)^2 / e at s sigma^2 = d^2 / 2.
        d, sigma, radius = 1.0, 0.050, 0.005
        scan = dilution_max_pc(np.array([d, 0.0]), np.eye(2) * sigma**2, radius)
        oracle = (radius / d) ** 2 / math.e
        assert scan.max_pc == pytest.approx(oracle, rel=1e-3)
        assert scan.scale_at_max == pytest.approx(d**2 / (2.0 * sigma**2), rel=1e-3)

    def test_a_diluted_event_is_flagged(self):
        scan = dilution_max_pc(np.array([1.0, 0.0]), np.eye(2) * 0.050**2, 0.005)
        assert scan.diluted is True
        assert scan.max_pc > scan.pc_actual

    def test_a_covariance_already_past_the_peak_is_not_diluted(self):
        # sigma much larger than the miss distance: growing it only lowers Pc.
        scan = dilution_max_pc(np.array([0.010, 0.0]), np.eye(2) * 5.0**2, HBR)
        assert scan.diluted is False
        assert scan.scale_at_max <= 1.0 + 1e-6

    def test_the_curve_is_returned_and_brackets_the_maximum(self):
        scan = dilution_max_pc(np.array([1.0, 0.0]), np.eye(2) * 0.050**2, 0.005, n_points=41)
        assert len(scan.scales) == 41
        assert len(scan.pc_values) == 41
        assert scan.max_pc >= max(scan.pc_values) - 1e-15
        assert scan.scales[0] < scan.scale_at_max < scan.scales[-1]

    def test_bad_scan_bounds_are_rejected(self):
        with pytest.raises(ValueError, match="n_points"):
            dilution_max_pc(np.zeros(2), np.eye(2) * 1e-4, HBR, n_points=2)
        with pytest.raises(ValueError, match="scale_min"):
            dilution_max_pc(np.zeros(2), np.eye(2) * 1e-4, HBR, scale_min=10.0, scale_max=1.0)


# ---------------------------------------------------------------------------
# Usage checks
# ---------------------------------------------------------------------------


def _report(miss, rel_v, cov, **kwargs):
    return pc_usage_checks(
        np.asarray(miss, float),
        np.asarray(rel_v, float),
        np.asarray(cov, float),
        **kwargs,
    )


class TestUsageChecks:
    NOMINAL_MISS = [3.0, 0.0, 0.0]
    NOMINAL_V = [0.0, 0.0, 12.0]
    NOMINAL_COV = np.diag([9.0, 10.0, 9.0])

    def test_a_clean_hypervelocity_encounter_raises_nothing(self):
        report = _report(self.NOMINAL_MISS, self.NOMINAL_V, self.NOMINAL_COV)
        assert report.ok is True
        assert report.codes == []
        assert report.positive_definite is True
        assert 0.0 <= report.probability <= 1.0

    def test_the_report_carries_the_measured_geometry(self):
        report = _report(self.NOMINAL_MISS, self.NOMINAL_V, self.NOMINAL_COV)
        assert report.miss_distance_km == pytest.approx(3.0)
        assert report.relative_speed_km_s == pytest.approx(12.0)
        assert report.aspect_ratio >= 1.0
        assert report.encounter_duration_s > 0.0
        assert report.condition_number >= 1.0
        assert report.hbr_over_miss_distance == pytest.approx(DEFAULT_COMBINED_RADIUS_KM / 3.0)

    def test_low_relative_velocity(self):
        report = _report(self.NOMINAL_MISS, [0.0, 0.0, 0.01], self.NOMINAL_COV)
        assert "LOW_RELATIVE_VELOCITY" in report.codes
        warning = next(w for w in report.warnings if w.code == "LOW_RELATIVE_VELOCITY")
        assert warning.values["relative_speed_km_s"] == pytest.approx(0.01)
        assert warning.message

    def test_extended_encounter(self):
        # A slow encounter through a wide covariance lasts a large fraction of
        # the orbit, which breaks the rectilinear assumption.
        report = _report(self.NOMINAL_MISS, [0.0, 0.0, 0.5], np.diag([1e-4, 1e-4, 100.0]))
        assert "EXTENDED_ENCOUNTER" in report.codes
        assert report.duration_fraction_of_period > 2e-2

    def test_non_positive_definite_covariance(self):
        report = _report(self.NOMINAL_MISS, self.NOMINAL_V, np.diag([1e-4, 1e-4, -1e-6]))
        assert "NPD_COVARIANCE" in report.codes
        assert report.positive_definite is False
        warning = next(w for w in report.warnings if w.code == "NPD_COVARIANCE")
        assert warning.values["min_eigenvalue_km2"] < 0.0

    def test_ill_conditioned_covariance(self):
        report = _report(self.NOMINAL_MISS, self.NOMINAL_V, np.diag([1e-2, 1e-2, 1e-14]))
        assert "ILL_CONDITIONED_COVARIANCE" in report.codes
        assert report.condition_number > 1e8

    def test_extreme_aspect_ratio(self):
        cov = np.diag([1.0, 1e-10, 1.0])
        report = _report(self.NOMINAL_MISS, self.NOMINAL_V, cov, condition_number_limit=1e30)
        assert "EXTREME_ASPECT_RATIO" in report.codes
        assert report.aspect_ratio > 1000.0

    def test_chan_aspect_ratio(self):
        # 20:1 in the encounter plane, well past the measured Chan limit.
        cov = np.diag([4e-4, 1e-6, 4e-4])
        report = _report(self.NOMINAL_MISS, self.NOMINAL_V, cov)
        assert "CHAN_ASPECT_RATIO" in report.codes
        warning = next(w for w in report.warnings if w.code == "CHAN_ASPECT_RATIO")
        assert warning.values["aspect_ratio"] > CHAN_ASPECT_RATIO_LIMIT
        assert warning.values["aspect_ratio_limit"] == CHAN_ASPECT_RATIO_LIMIT
        assert "chan_error_estimate" in warning.values

    def test_chan_aspect_ratio_fires_on_the_error_estimate_alone(self):
        # Aspect ratio under the limit, but the second-order error estimate is
        # over its own limit: the warning must still be raised.
        report = _report(
            [0.020, 0.010, 0.0],
            self.NOMINAL_V,
            np.diag([2.5e-3, 1e-4, 1e-4]),
            hard_body_radius_km=0.050,
        )
        assert "CHAN_ASPECT_RATIO" in report.codes
        assert report.aspect_ratio < CHAN_ASPECT_RATIO_LIMIT
        assert report.chan_error_estimate > 0.05

    def test_hard_body_comparable_to_miss_distance(self):
        report = _report([0.030, 0.0, 0.0], self.NOMINAL_V, self.NOMINAL_COV)
        assert "HBR_COMPARABLE_TO_MISS" in report.codes
        assert report.hbr_over_miss_distance > 1e-2

    def test_pc_dilution(self):
        report = _report([2.0, 0.0, 0.0], self.NOMINAL_V, np.diag([1e-2, 1e-2, 1e-2]))
        assert "PC_DILUTION" in report.codes
        assert report.max_pc_dilution > report.probability
        assert report.dilution_scale > 1.0

    def test_every_warning_carries_a_code_a_message_and_numbers(self):
        report = _report([0.030, 0.0, 0.0], [0.0, 0.0, 0.01], np.diag([1e-4, 1e-4, -1e-6]))
        assert len(report.warnings) >= 3
        for warning in report.warnings:
            assert warning.code == warning.code.upper()
            assert len(warning.message) > 10
            assert warning.values
            assert all(isinstance(v, float) for v in warning.values.values())

    def test_limits_are_caller_tunable(self):
        strict = _report(
            self.NOMINAL_MISS, self.NOMINAL_V, self.NOMINAL_COV, aspect_ratio_limit=1.0
        )
        assert "EXTREME_ASPECT_RATIO" in strict.codes
        relaxed = _report(
            self.NOMINAL_MISS, [0.0, 0.0, 0.01], self.NOMINAL_COV, low_relative_speed_km_s=0.001
        )
        assert "LOW_RELATIVE_VELOCITY" not in relaxed.codes


# ---------------------------------------------------------------------------
# Covariance realism
# ---------------------------------------------------------------------------


class TestChiSquare:
    # Independent closed forms for the CDF, none of which touch the
    # incomplete-gamma routine under test.
    ORACLES = {
        1: lambda x: math.erf(math.sqrt(0.5 * x)),
        2: lambda x: 1.0 - math.exp(-0.5 * x),
        3: lambda x: (
            math.erf(math.sqrt(0.5 * x)) - math.sqrt(2.0 * x / math.pi) * math.exp(-0.5 * x)
        ),
        4: lambda x: 1.0 - math.exp(-0.5 * x) * (1.0 + 0.5 * x),
    }

    @pytest.mark.parametrize("dof", [1, 2, 3, 4])
    @pytest.mark.parametrize("x", [0.0, 0.25, 1.0, 3.0, 6.0, 12.0, 30.0])
    def test_matches_the_closed_form(self, dof, x):
        assert chi_square_cdf(x, dof) == pytest.approx(self.ORACLES[dof](x), abs=1e-12)

    def test_monotone_and_bounded(self):
        values = [chi_square_cdf(x, 3) for x in np.linspace(0.0, 40.0, 50)]
        assert all(b >= a for a, b in zip(values[:-1], values[1:], strict=True))
        assert values[0] == 0.0
        assert values[-1] == pytest.approx(1.0, abs=1e-6)

    def test_a_negative_argument_reads_as_zero(self):
        assert chi_square_cdf(-5.0, 3) == 0.0

    def test_zero_degrees_of_freedom_is_rejected(self):
        with pytest.raises(ValueError, match="dof"):
            chi_square_cdf(1.0, 0)


class TestCovarianceRealism:
    def test_a_truthful_covariance_scores_near_one(self):
        rng = np.random.default_rng(11)
        distances = np.linalg.norm(rng.normal(size=(20000, 3)), axis=1)
        realism = covariance_realism_scale(distances, dof=3)
        assert realism.scale_factor == pytest.approx(1.0, abs=0.05)
        assert realism.robust_scale_factor == pytest.approx(1.0, abs=0.05)
        assert realism.realistic is True
        assert realism.fraction_within_2_sigma == pytest.approx(
            realism.expected_within_2_sigma, abs=0.02
        )

    def test_an_optimistic_covariance_is_detected(self):
        rng = np.random.default_rng(12)
        # Residuals three sigma too large: variance scale should read ~9.
        distances = 3.0 * np.linalg.norm(rng.normal(size=(20000, 3)), axis=1)
        realism = covariance_realism_scale(distances, dof=3)
        assert realism.scale_factor == pytest.approx(9.0, rel=0.05)
        assert realism.sigma_scale_factor == pytest.approx(3.0, rel=0.03)
        assert realism.realistic is False

    def test_the_median_estimate_resists_one_outlier(self):
        rng = np.random.default_rng(13)
        distances = np.linalg.norm(rng.normal(size=(2000, 3)), axis=1)
        distances[0] = 500.0
        realism = covariance_realism_scale(distances, dof=3)
        assert realism.scale_factor > 20.0
        assert realism.robust_scale_factor == pytest.approx(1.0, abs=0.15)

    def test_bad_input_is_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            covariance_realism_scale([])
        with pytest.raises(ValueError, match="non-negative"):
            covariance_realism_scale([1.0, -1.0])


class TestRegimeDefaults:
    @pytest.mark.parametrize(
        "sma_km,ecc,expected",
        [
            (6778.0, 0.001, "LEO"),
            (26560.0, 0.001, "MEO"),
            (42164.0, 0.0001, "GEO"),
            (26000.0, 0.72, "HEO"),
        ],
    )
    def test_regime_classification(self, sma_km, ecc, expected):
        assert classify_orbit_regime(sma_km, ecc) == expected

    def test_defaults_are_diagonal_and_along_track_dominant(self):
        cov = regime_default_covariance_rtn("LEO")
        assert cov.shape == (3, 3)
        assert np.allclose(cov, np.diag(np.diag(cov)))
        assert cov[1][1] > cov[0][0]
        assert np.linalg.eigvalsh(cov).min() > 0.0

    def test_defaults_grow_with_tle_age(self):
        fresh = regime_default_covariance_rtn("LEO", tle_age_hours=0.0)
        old = regime_default_covariance_rtn("LEO", tle_age_hours=72.0)
        assert np.trace(old) > np.trace(fresh)

    def test_regimes_are_ordered_by_size(self):
        traces = [np.trace(regime_default_covariance_rtn(r)) for r in ("LEO", "MEO", "GEO")]
        assert traces[0] < traces[1] < traces[2]

    def test_an_unknown_regime_is_rejected(self):
        with pytest.raises(ValueError, match="unknown regime"):
            regime_default_covariance_rtn("CISLUNAR")


# ---------------------------------------------------------------------------
# Router (mounted on a bare FastAPI app, not on the service app)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(pc_router)
    return TestClient(app)


ENCOUNTER = {
    "miss_vector_km": [0.150, 0.0, 0.0],
    "rel_velocity_km_s": [0.0, 0.0, 12.0],
    "cov_primary_km2": [[1e-4, 0.0, 0.0], [0.0, 1e-4, 0.0], [0.0, 0.0, 1e-4]],
    "cov_secondary_km2": [[4e-4, 0.0, 0.0], [0.0, 1e-4, 0.0], [0.0, 0.0, 1e-4]],
    "hard_body_radius_km": 0.020,
}


class TestRouterShape:
    def test_the_router_is_mounted_under_its_own_prefix(self):
        paths = {route.path for route in pc_router.routes}
        assert paths == {"/pc/compare", "/pc/max-missing-covariance", "/pc/dilution", "/pc/cdm"}
        assert pc_router.prefix == "/pc"
        assert pc_router.tags == ["Probability"]


class TestCompareEndpoint:
    def test_all_three_methods_are_returned_and_agree(self, client):
        body = client.post("/pc/compare", json=ENCOUNTER).json()
        probabilities = body["probabilities"]
        assert set(probabilities) == set(PC_METHODS)
        assert probabilities["alfano"] == pytest.approx(probabilities["foster"], rel=1e-3)
        assert body["method_spread"] >= 0.0
        assert body["miss_distance_km"] == pytest.approx(0.150)
        assert body["encounter_plane"]["aspect_ratio"] >= 1.0

    def test_monte_carlo_is_optional_and_reproducible(self, client):
        payload = dict(ENCOUNTER, include_monte_carlo=True, monte_carlo_samples=50_000)
        first = client.post("/pc/compare", json=payload).json()["probabilities"]
        second = client.post("/pc/compare", json=payload).json()["probabilities"]
        assert first["monte_carlo"] == second["monte_carlo"]
        assert 0.0 <= first["monte_carlo"] <= 1.0

    def test_usage_warnings_are_surfaced(self, client):
        payload = dict(ENCOUNTER, miss_vector_km=[0.030, 0.0, 0.0])
        usage = client.post("/pc/compare", json=payload).json()["usage"]
        assert "HBR_COMPARABLE_TO_MISS" in usage["codes"]
        assert usage["ok"] is False
        for warning in usage["warnings"]:
            assert {"code", "message", "values"} == set(warning)

    def test_a_missing_covariance_is_defaulted_and_declared(self, client):
        payload = {k: v for k, v in ENCOUNTER.items() if k != "cov_secondary_km2"}
        body = client.post("/pc/compare", json=payload).json()
        assert any("secondary covariance missing" in note for note in body["assumptions"])
        assert body["probabilities"]["foster"] > 0.0

    def test_zero_relative_velocity_is_a_422(self, client):
        payload = dict(ENCOUNTER, rel_velocity_km_s=[0.0, 0.0, 0.0])
        response = client.post("/pc/compare", json=payload)
        assert response.status_code == 422
        assert "encounter plane is undefined" in str(response.json()["detail"])

    def test_a_malformed_covariance_is_rejected(self, client):
        payload = dict(ENCOUNTER, cov_primary_km2=[[1.0, 0.0], [0.0, 1.0]])
        assert client.post("/pc/compare", json=payload).status_code == 422


class TestMaxPcEndpoint:
    PAYLOAD = {
        "miss_vector_km": [0.500, 0.0, 0.0],
        "rel_velocity_km_s": [0.0, 0.0, 12.0],
        "cov_known_km2": [[1e-4, 0.0, 0.0], [0.0, 1e-4, 0.0], [0.0, 0.0, 1e-4]],
        "hard_body_radius_km": 0.020,
    }

    def test_the_bound_exceeds_the_known_covariance_value(self, client):
        body = client.post("/pc/max-missing-covariance", json=self.PAYLOAD).json()
        assert body["bounded"] is True
        assert body["max_pc"] > body["pc_known_covariance_only"]
        assert body["mahalanobis_distance"] > 1.0
        assert len(body["maximizing_covariance_km2"]) == 2
        assert "bound" in body["interpretation"]

    def test_an_unknown_method_is_a_422(self, client):
        payload = dict(self.PAYLOAD, method="kessler")
        assert client.post("/pc/max-missing-covariance", json=payload).status_code == 422


class TestDilutionEndpoint:
    PAYLOAD = {
        "miss_vector_km": [1.0, 0.0, 0.0],
        "rel_velocity_km_s": [0.0, 0.0, 12.0],
        "cov_primary_km2": [[1.25e-3, 0.0, 0.0], [0.0, 1.25e-3, 0.0], [0.0, 0.0, 1.25e-3]],
        "cov_secondary_km2": [[1.25e-3, 0.0, 0.0], [0.0, 1.25e-3, 0.0], [0.0, 0.0, 1.25e-3]],
        "hard_body_radius_km": 0.005,
    }

    def test_the_scan_matches_the_closed_form_maximum(self, client):
        body = client.post("/pc/dilution", json=self.PAYLOAD).json()
        assert body["diluted"] is True
        assert body["max_pc"] == pytest.approx((0.005 / 1.0) ** 2 / math.e, rel=2e-3)
        assert body["dilution_ratio"] > 1.0
        assert "curve" not in body

    def test_the_curve_is_returned_on_request(self, client):
        payload = dict(self.PAYLOAD, include_curve=True, n_points=31)
        curve = client.post("/pc/dilution", json=payload).json()["curve"]
        assert len(curve["scales"]) == 31
        assert len(curve["pc_values"]) == 31

    def test_inverted_scan_bounds_are_a_422(self, client):
        payload = dict(self.PAYLOAD, scale_min=100.0, scale_max=1.0)
        response = client.post("/pc/dilution", json=payload)
        assert response.status_code == 422
