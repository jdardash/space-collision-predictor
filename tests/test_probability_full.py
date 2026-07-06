"""Tests for full-covariance Pc methods (Foster, Chan, Monte Carlo)."""

import math

import numpy as np
import pytest

from sda.probability import (
    compute_collision_probability_full,
    pc_chan,
    pc_foster,
    pc_monte_carlo_2d,
    project_covariance_to_encounter_plane,
    rtn_covariance_to_eci,
    rtn_to_eci_matrix,
)

SIGMA = 0.100  # km
HBR = 0.020  # km
ISO_COV = np.eye(2) * SIGMA**2


def _rotated_cov(sx: float, sy: float, angle_deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    rot = np.array([[c, -s], [s, c]])
    return rot @ np.diag([sx**2, sy**2]) @ rot.T


class TestFosterVsClosedForm:
    def test_zero_miss_isotropic_matches_closed_form(self):
        exact = 1.0 - math.exp(-(HBR**2) / (2.0 * SIGMA**2))
        pc = pc_foster(np.zeros(2), ISO_COV, HBR)
        assert pc == pytest.approx(exact, rel=1e-4)

    def test_zero_radius_gives_zero(self):
        assert pc_foster(np.zeros(2), ISO_COV, 0.0) == 0.0

    def test_huge_miss_gives_zero(self):
        assert pc_foster(np.array([100.0, 0.0]), ISO_COV, HBR) == 0.0


class TestChanSeries:
    def test_zero_miss_isotropic_matches_closed_form(self):
        exact = 1.0 - math.exp(-(HBR**2) / (2.0 * SIGMA**2))
        assert pc_chan(np.zeros(2), ISO_COV, HBR) == pytest.approx(exact, rel=1e-12)

    @pytest.mark.parametrize("d_over_sigma", [0.5, 1.0, 2.0, 4.0, 8.0])
    def test_matches_foster_for_isotropic_offsets(self, d_over_sigma):
        miss = np.array([d_over_sigma * SIGMA, 0.0])
        chan = pc_chan(miss, ISO_COV, HBR)
        foster = pc_foster(miss, ISO_COV, HBR)
        assert foster == pytest.approx(chan, rel=2e-3)

    def test_huge_mahalanobis_returns_zero(self):
        assert pc_chan(np.array([10.0, 0.0]), ISO_COV, HBR) == 0.0

    def test_disk_engulfing_covariance_returns_one(self):
        tiny_cov = np.eye(2) * (1e-4) ** 2
        assert pc_chan(np.zeros(2), tiny_cov, 1.0) == pytest.approx(1.0)
        assert pc_foster(np.zeros(2), tiny_cov, 1.0) == pytest.approx(1.0, rel=1e-4)


class TestMonteCarloAgreement:
    @pytest.mark.parametrize(
        "miss,cov",
        [
            (np.zeros(2), ISO_COV),
            (np.array([0.1, 0.05]), ISO_COV),
            (np.zeros(2), _rotated_cov(0.3, 0.1, 0.0)),
            (np.array([0.2, 0.1]), _rotated_cov(0.3, 0.1, 30.0)),
            (np.array([0.0, 0.06]), _rotated_cov(0.3, 0.03, 0.0)),
        ],
    )
    def test_foster_within_mc_error_bars(self, miss, cov):
        n = 400_000
        foster = pc_foster(miss, cov, HBR)
        mc = pc_monte_carlo_2d(miss, cov, HBR, n_samples=n, seed=7)
        std_err = math.sqrt(max(foster, 1e-12) * (1.0 - foster) / n)
        assert abs(mc - foster) < 5.0 * std_err + 1e-12

    def test_deterministic_with_seed(self):
        a = pc_monte_carlo_2d(np.zeros(2), ISO_COV, HBR, n_samples=100_000, seed=3)
        b = pc_monte_carlo_2d(np.zeros(2), ISO_COV, HBR, n_samples=100_000, seed=3)
        assert a == b


class TestFrameUtilities:
    def test_rtn_matrix_is_orthonormal(self):
        m = rtn_to_eci_matrix(np.array([7000.0, 100.0, -50.0]), np.array([0.1, 7.4, 0.5]))
        assert np.allclose(m @ m.T, np.eye(3), atol=1e-12)
        assert np.linalg.det(m) == pytest.approx(1.0)

    def test_rtn_radial_axis_points_along_position(self):
        pos = np.array([7000.0, 0.0, 0.0])
        vel = np.array([0.0, 7.5, 0.0])
        m = rtn_to_eci_matrix(pos, vel)
        assert np.allclose(m[:, 0], [1.0, 0.0, 0.0])
        assert np.allclose(m[:, 1], [0.0, 1.0, 0.0])  # transverse
        assert np.allclose(m[:, 2], [0.0, 0.0, 1.0])  # normal

    def test_covariance_rotation_preserves_trace_and_eigenvalues(self):
        cov_rtn = np.diag([1e-4, 4e-2, 4e-4])
        pos = np.array([6800.0, 1200.0, 300.0])
        vel = np.array([-1.2, 7.1, 0.9])
        cov_eci = rtn_covariance_to_eci(cov_rtn, pos, vel)
        assert np.trace(cov_eci) == pytest.approx(np.trace(cov_rtn))
        assert np.allclose(
            np.sort(np.linalg.eigvalsh(cov_eci)), np.sort(np.diag(cov_rtn))
        )
        assert np.allclose(cov_eci, cov_eci.T)

    def test_isotropic_projection_stays_isotropic(self):
        cov3 = np.eye(3) * SIGMA**2
        cov2 = project_covariance_to_encounter_plane(cov3, np.array([1.0, 2.0, 3.0]))
        assert np.allclose(cov2, np.eye(2) * SIGMA**2)


class TestFullPipeline:
    def test_matches_isotropic_series(self):
        # 3D geometry: miss in the encounter plane, isotropic combined sigma
        result = compute_collision_probability_full(
            miss_vector_km=np.array([0.1, 0.0, 0.0]),
            rel_velocity_km_s=np.array([0.0, 0.0, 12.0]),
            cov_primary_eci_km2=np.eye(3) * SIGMA**2 / 2.0,
            cov_secondary_eci_km2=np.eye(3) * SIGMA**2 / 2.0,
            combined_radius_km=HBR,
        )
        expected = pc_chan(np.array([0.1, 0.0]), ISO_COV, HBR)
        assert result["probability"] == pytest.approx(expected, rel=2e-3)
        assert result["probability_chan"] == pytest.approx(expected, rel=1e-9)
        assert result["miss_distance_km"] == pytest.approx(0.1)
        assert result["mahalanobis_distance"] == pytest.approx(1.0, rel=1e-9)

    def test_zero_relative_velocity_returns_zero_probability(self):
        result = compute_collision_probability_full(
            miss_vector_km=np.array([0.1, 0.0, 0.0]),
            rel_velocity_km_s=np.zeros(3),
            cov_primary_eci_km2=np.eye(3) * 1e-4,
            cov_secondary_eci_km2=np.eye(3) * 1e-4,
        )
        assert result["probability"] == 0.0
        assert result["mahalanobis_distance"] == float("inf")

    def test_encounter_covariance_reported_symmetric(self):
        result = compute_collision_probability_full(
            miss_vector_km=np.array([0.1, 0.05, 0.02]),
            rel_velocity_km_s=np.array([1.0, -2.0, 7.0]),
            cov_primary_eci_km2=np.diag([1e-4, 4e-2, 4e-4]),
            cov_secondary_eci_km2=np.diag([2e-4, 1e-2, 9e-4]),
        )
        cov = np.array(result["encounter_covariance_km2"])
        assert cov.shape == (2, 2)
        assert cov[0, 1] == pytest.approx(cov[1, 0])
        assert 0.0 <= result["probability"] <= 1.0

    def test_degenerate_covariance_does_not_crash(self):
        result = compute_collision_probability_full(
            miss_vector_km=np.array([0.001, 0.0, 0.0]),
            rel_velocity_km_s=np.array([0.0, 0.0, 10.0]),
            cov_primary_eci_km2=np.zeros((3, 3)),
            cov_secondary_eci_km2=np.zeros((3, 3)),
        )
        assert 0.0 <= result["probability"] <= 1.0
