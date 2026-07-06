"""Safety-invariant regression tests.

Locks the risk-classification thresholds and the known probability-dilution
property of the 2D Pc method so neither can drift silently. Changes to these
tests require safety-reviewer approval (see CLAUDE.md).
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np

from sda.conjunction import classify_risk
from sda.models import RiskLevel
from sda.probability import compute_collision_probability, sigma_from_tle_age


class TestClassifyRiskBoundaries:
    """Exact boundary behavior of the safety-critical thresholds."""

    def test_below_half_km_is_critical(self):
        assert classify_risk(0.4999, 7.5) == RiskLevel.CRITICAL

    def test_exactly_half_km_is_high(self):
        # Boundary is exclusive: < 0.5 km
        assert classify_risk(0.5, 7.5) == RiskLevel.HIGH

    def test_below_one_km_is_high(self):
        assert classify_risk(0.9999, 7.5) == RiskLevel.HIGH

    def test_exactly_one_km_slow_is_moderate(self):
        assert classify_risk(1.0, 7.5) == RiskLevel.MODERATE

    def test_under_five_km_fast_is_high(self):
        assert classify_risk(4.9999, 10.0001) == RiskLevel.HIGH

    def test_under_five_km_at_ten_km_s_is_moderate(self):
        # Velocity boundary is exclusive: > 10 km/s
        assert classify_risk(4.9999, 10.0) == RiskLevel.MODERATE

    def test_exactly_five_km_is_low(self):
        assert classify_risk(5.0, 15.0) == RiskLevel.LOW

    def test_below_ten_km_is_low(self):
        assert classify_risk(9.9999, 7.5) == RiskLevel.LOW

    def test_exactly_ten_km_is_negligible(self):
        assert classify_risk(10.0, 7.5) == RiskLevel.NEGLIGIBLE

    def test_zero_miss_is_critical(self):
        assert classify_risk(0.0, 0.0) == RiskLevel.CRITICAL


class TestProbabilityDilution:
    """The 2D Pc dilution property is intentional and must stay documented.

    For a miss distance much smaller than the combined sigma, Pc scales as
    ~r_hb^2 / (2 sigma^2): growing sigma (stale TLEs) DECREASES reported Pc.
    Risk level is therefore keyed to miss distance, never to Pc.
    """

    @staticmethod
    def _pc(sigma_km: float) -> float:
        result = compute_collision_probability(
            miss_vector_km=np.array([0.02, 0.0, 0.0]),  # 20 m miss
            rel_velocity_km_s=np.array([0.0, 10.0, 0.0]),
            sigma_primary_km=sigma_km,
            sigma_secondary_km=sigma_km,
        )
        return result["probability"]

    def test_pc_decreases_as_sigma_grows_for_close_miss(self):
        pc_fresh = self._pc(0.05)
        pc_stale = self._pc(1.0)
        assert pc_fresh > pc_stale > 0.0

    def test_dilution_is_monotonic_across_decades(self):
        pcs = [self._pc(s) for s in (0.05, 0.2, 0.5, 1.0, 2.0)]
        assert all(a > b for a, b in pairwise(pcs))


class TestSigmaFromTleAge:
    """Age-scaled covariance must grow monotonically with TLE age."""

    def test_fresh_tle_uses_base_sigma(self):
        assert sigma_from_tle_age(0.0) == 0.050

    def test_negative_age_clamps_to_base(self):
        assert sigma_from_tle_age(-5.0) == 0.050

    def test_monotonic_growth(self):
        ages = [0.0, 6.0, 24.0, 72.0, 168.0, 336.0]
        sigmas = [sigma_from_tle_age(a) for a in ages]
        assert all(a <= b for a, b in pairwise(sigmas))

    def test_three_day_tle_roughly_half_km(self):
        # Vallado-style empirical model: ~10x base at 3 days
        assert 0.4 <= sigma_from_tle_age(72.0) <= 0.6
