"""Extended probability tests — covariance scaling and edge cases."""

import numpy as np

from sda.probability import (
    _rotation_matrix_to_conjunction_plane,
    compute_collision_probability,
    sigma_from_tle_age,
)


def test_sigma_fresh_tle():
    """Fresh TLE (0h) should return base sigma."""
    sigma = sigma_from_tle_age(0.0)
    assert sigma == 0.050  # 50m default


def test_sigma_grows_with_age():
    """Older TLEs should have larger uncertainty."""
    s_fresh = sigma_from_tle_age(1.0)
    s_day = sigma_from_tle_age(24.0)
    s_week = sigma_from_tle_age(168.0)
    assert s_fresh < s_day < s_week


def test_sigma_3day_roughly_10x():
    """3-day-old TLE should have ~10x the uncertainty of a fresh one."""
    s_fresh = sigma_from_tle_age(0.0)
    s_3day = sigma_from_tle_age(72.0)
    ratio = s_3day / s_fresh
    assert 8 < ratio < 12


def test_sigma_negative_age():
    """Negative age (future epoch) should return base sigma."""
    sigma = sigma_from_tle_age(-5.0)
    assert sigma == 0.050


def test_sigma_custom_base():
    """Custom base sigma should scale proportionally."""
    s1 = sigma_from_tle_age(24.0, base_sigma_km=0.050)
    s2 = sigma_from_tle_age(24.0, base_sigma_km=0.100)
    assert abs(s2 / s1 - 2.0) < 1e-10


def test_rotation_matrix_orthogonal():
    """Rotation matrix should be orthogonal (R @ R.T = I)."""
    vel = np.array([3.0, 4.0, 5.0])
    R = _rotation_matrix_to_conjunction_plane(vel)
    identity = R @ R.T
    np.testing.assert_allclose(identity, np.eye(3), atol=1e-10)


def test_rotation_matrix_z_aligned():
    """Z-axis of rotated frame should align with velocity direction."""
    vel = np.array([1.0, 2.0, 3.0])
    R = _rotation_matrix_to_conjunction_plane(vel)
    v_rotated = R @ vel
    # X and Y should be ~0, Z should be |v|
    assert abs(v_rotated[0]) < 1e-10
    assert abs(v_rotated[1]) < 1e-10
    assert abs(v_rotated[2] - np.linalg.norm(vel)) < 1e-10


def test_pc_symmetry():
    """Pc should be the same regardless of miss vector direction."""
    pc_x = compute_collision_probability(
        miss_vector_km=np.array([0.01, 0.0, 0.0]),
        rel_velocity_km_s=np.array([0.0, 0.0, 10.0]),
    )
    pc_y = compute_collision_probability(
        miss_vector_km=np.array([0.0, 0.01, 0.0]),
        rel_velocity_km_s=np.array([0.0, 0.0, 10.0]),
    )
    # Should be very close (isotropic covariance)
    assert abs(pc_x["probability"] - pc_y["probability"]) < 1e-6


def test_pc_monotonic_with_miss_distance():
    """Pc should decrease as miss distance increases."""
    pcs = []
    for d in [0.001, 0.01, 0.05, 0.1, 0.5]:
        result = compute_collision_probability(
            miss_vector_km=np.array([d, 0.0, 0.0]),
            rel_velocity_km_s=np.array([0.0, 10.0, 0.0]),
        )
        pcs.append(result["probability"])
    for i in range(len(pcs) - 1):
        assert pcs[i] >= pcs[i + 1]
