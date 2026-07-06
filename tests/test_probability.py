"""Tests for collision probability computation."""

import math

import numpy as np

from sda.probability import (
    _bessel_i0,
    compute_collision_probability,
    compute_pc_for_conjunction,
)


def test_pc_zero_miss_distance():
    """Pc should be highest when miss distance is zero."""
    result = compute_collision_probability(
        miss_vector_km=np.array([0.0, 0.0, 0.0]),
        rel_velocity_km_s=np.array([10.0, 0.0, 0.0]),
        sigma_primary_km=0.050,
        sigma_secondary_km=0.050,
        combined_radius_km=0.020,
    )
    assert result["probability"] > 0
    assert result["miss_distance_km"] < 1e-10


def test_pc_large_miss_distance():
    """Pc should be negligible for large miss distances."""
    result = compute_collision_probability(
        miss_vector_km=np.array([100.0, 0.0, 0.0]),
        rel_velocity_km_s=np.array([0.0, 10.0, 0.0]),
        sigma_primary_km=0.050,
        sigma_secondary_km=0.050,
        combined_radius_km=0.020,
    )
    assert result["probability"] < 1e-10
    assert result["miss_distance_km"] > 99.0


def test_pc_increases_with_larger_radius():
    """Pc should increase with larger hard-body radius."""
    small = compute_collision_probability(
        miss_vector_km=np.array([0.01, 0.0, 0.0]),
        rel_velocity_km_s=np.array([10.0, 0.0, 0.0]),
        combined_radius_km=0.005,
    )
    large = compute_collision_probability(
        miss_vector_km=np.array([0.01, 0.0, 0.0]),
        rel_velocity_km_s=np.array([10.0, 0.0, 0.0]),
        combined_radius_km=0.050,
    )
    assert large["probability"] > small["probability"]


def test_pc_bounded_zero_to_one():
    """Pc must always be in [0, 1]."""
    result = compute_collision_probability(
        miss_vector_km=np.array([0.001, 0.0, 0.0]),
        rel_velocity_km_s=np.array([10.0, 0.0, 0.0]),
        combined_radius_km=1.0,
        sigma_primary_km=0.001,
    )
    assert 0.0 <= result["probability"] <= 1.0


def test_pc_zero_velocity():
    """Zero relative velocity should return Pc = 0 (degenerate case)."""
    result = compute_collision_probability(
        miss_vector_km=np.array([1.0, 0.0, 0.0]),
        rel_velocity_km_s=np.array([0.0, 0.0, 0.0]),
    )
    assert result["probability"] == 0.0


def test_bessel_i0_small():
    """Bessel I0(0) = 1."""
    assert abs(_bessel_i0(0.0) - 1.0) < 1e-10


def test_bessel_i0_large():
    """Bessel I0 for large argument should be positive and finite."""
    val = _bessel_i0(5.0)
    assert val > 1.0
    assert math.isfinite(val)


def test_compute_pc_for_conjunction_wrapper():
    """Test the convenience wrapper with state vector tuples."""
    result = compute_pc_for_conjunction(
        pos_primary_km=(6771.0, 0.0, 0.0),
        vel_primary_km_s=(0.0, 7.5, 0.0),
        pos_secondary_km=(6771.5, 0.0, 0.0),
        vel_secondary_km_s=(0.0, 7.4, 0.0),
    )
    assert "probability" in result
    assert "mahalanobis_distance" in result
    assert result["miss_distance_km"] > 0
