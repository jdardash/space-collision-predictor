"""Collision probability computation using the 2D Pc method.

Implements the Chan (2003) / Alfano (2005) approach: project the
miss vector and combined covariance onto the conjunction plane
(perpendicular to relative velocity), then integrate a 2D Gaussian
over a circular hard-body radius.

Reference: S. Alfano, "A Numerical Implementation of Spherical Object
Collision Probability," AMOS Technical Conference, 2005.
"""

from __future__ import annotations

import math

import numpy as np


# Default hard-body radii (meters → km)
DEFAULT_COMBINED_RADIUS_KM = 0.020  # ~20 m combined (two ~10 m objects)

# Default position uncertainty (1-sigma) in km per axis
DEFAULT_POSITION_SIGMA_KM = 0.050  # 50 m — typical for LEO with fresh TLEs


def _rotation_matrix_to_conjunction_plane(
    rel_velocity: np.ndarray,
) -> np.ndarray:
    """Build rotation matrix whose Z-axis aligns with relative velocity.

    Returns a 3×3 matrix R such that R @ rel_velocity = [0, 0, |v|].
    The X-Y plane of the rotated frame is the conjunction (B-plane).
    """
    v_hat = rel_velocity / np.linalg.norm(rel_velocity)

    # Choose an arbitrary vector not parallel to v_hat
    if abs(v_hat[0]) < 0.9:
        arbitrary = np.array([1.0, 0.0, 0.0])
    else:
        arbitrary = np.array([0.0, 1.0, 0.0])

    # Gram-Schmidt to build orthonormal basis
    x_hat = np.cross(v_hat, arbitrary)
    x_hat /= np.linalg.norm(x_hat)
    y_hat = np.cross(v_hat, x_hat)
    y_hat /= np.linalg.norm(y_hat)

    return np.array([x_hat, y_hat, v_hat])


def compute_collision_probability(
    miss_vector_km: np.ndarray,
    rel_velocity_km_s: np.ndarray,
    sigma_primary_km: float = DEFAULT_POSITION_SIGMA_KM,
    sigma_secondary_km: float = DEFAULT_POSITION_SIGMA_KM,
    combined_radius_km: float = DEFAULT_COMBINED_RADIUS_KM,
    n_integration_steps: int = 1000,
) -> dict:
    """Compute 2D probability of collision at closest approach.

    Parameters
    ----------
    miss_vector_km : (3,) array — relative position at TCA (primary - secondary) in ECI, km
    rel_velocity_km_s : (3,) array — relative velocity at TCA in ECI, km/s
    sigma_primary_km : 1-sigma position uncertainty for primary (isotropic), km
    sigma_secondary_km : 1-sigma position uncertainty for secondary (isotropic), km
    combined_radius_km : combined hard-body radius, km
    n_integration_steps : radial integration resolution

    Returns
    -------
    dict with keys: probability, miss_distance_km, combined_sigma_km,
                    mahalanobis_distance, hard_body_radius_km
    """
    rel_speed = float(np.linalg.norm(rel_velocity_km_s))
    miss_distance = float(np.linalg.norm(miss_vector_km))

    if rel_speed < 1e-10:
        return {
            "probability": 0.0,
            "miss_distance_km": miss_distance,
            "combined_sigma_km": 0.0,
            "mahalanobis_distance": float("inf"),
            "hard_body_radius_km": combined_radius_km,
        }

    # Rotate into conjunction plane
    R = _rotation_matrix_to_conjunction_plane(rel_velocity_km_s)
    miss_rotated = R @ miss_vector_km

    # 2D miss vector in the B-plane (x, y components)
    xm = miss_rotated[0]
    ym = miss_rotated[1]

    # Combined covariance in B-plane (isotropic assumption → diagonal)
    sigma_combined = math.sqrt(sigma_primary_km**2 + sigma_secondary_km**2)
    sx = sigma_combined
    sy = sigma_combined

    # Mahalanobis distance
    mahal = math.sqrt((xm / sx) ** 2 + (ym / sy) ** 2)

    # 2D Gaussian integration over circular hard-body region
    # Using polar coordinates centered on miss vector
    # Pc = ∫∫ (1/(2π σx σy)) exp(-0.5*((x/σx)² + (y/σy)²)) dA
    # For isotropic case, use radial integration with offset
    r_hb = combined_radius_km
    miss_2d = math.sqrt(xm**2 + ym**2)

    # Numerical integration using trapezoidal rule in polar coords
    pc = _integrate_2d_gaussian(miss_2d, sx, sy, r_hb, n_integration_steps)

    # Clamp to [0, 1]
    pc = max(0.0, min(1.0, pc))

    return {
        "probability": pc,
        "miss_distance_km": miss_distance,
        "combined_sigma_km": sigma_combined,
        "mahalanobis_distance": mahal,
        "hard_body_radius_km": combined_radius_km,
    }


def _integrate_2d_gaussian(
    miss_2d: float,
    sigma_x: float,
    sigma_y: float,
    radius: float,
    n_steps: int,
) -> float:
    """Numerically integrate 2D Gaussian over a circular region.

    Uses the series expansion approach for the offset circular integral.
    For isotropic case (σx ≈ σy = σ), Pc = 1 - exp(-r²/(2σ²)) when
    miss distance = 0. For offset miss, uses numerical radial-angular integration.
    """
    sigma = (sigma_x + sigma_y) / 2.0  # average for near-isotropic

    if sigma < 1e-15:
        return 1.0 if miss_2d < radius else 0.0

    # Use the Foster-Estes analytical approximation for circular cross-section
    # Pc ≈ (r²/(2σ²)) * exp(-(d²)/(2σ²)) for small r/σ
    # More precisely, use Rice distribution integral
    u = radius**2 / (2.0 * sigma**2)
    v = miss_2d**2 / (2.0 * sigma**2)

    if v > 500:
        return 0.0  # negligible — miss distance >> sigma

    # Series expansion of the non-central chi-squared CDF
    # Pc = exp(-v) * Σ (v^k / k!) * (1 - exp(-u) * Σ (u^j / j!))
    pc = 0.0
    term_v = math.exp(-v)
    for k in range(100):
        if k > 0:
            term_v *= v / k
        # Inner sum: CDF of Poisson
        inner = 0.0
        term_u = math.exp(-u)
        for j in range(k + 2):
            inner += term_u
            term_u *= u / (j + 1)
        pc += term_v * (1.0 - inner + term_u)  # correction term

    # Simpler and more numerically stable: direct formula
    # For isotropic Gaussian with circular HBR:
    # Pc = 1 - Q_1(sqrt(2*v), sqrt(2*u)) where Q_1 is Marcum Q-function
    # Approximate via: Pc ≈ exp(-v) * (1 - exp(-u)) for u << 1
    # For better accuracy, use the full integral:
    pc_direct = 0.0
    dr = radius / max(n_steps, 1)
    for i in range(n_steps):
        r = (i + 0.5) * dr
        # Integrate in polar: ∫₀²π ∫₀ʳ_hb (1/(2πσ²)) exp(-((r cosθ - d)² + (r sinθ)²)/(2σ²)) r dr dθ
        # = ∫₀ʳ_hb (r/(σ²)) exp(-(r² + d²)/(2σ²)) I₀(r*d/σ²) dr
        exponent = -(r**2 + miss_2d**2) / (2.0 * sigma**2)
        if exponent < -500:
            continue
        bessel_arg = r * miss_2d / (sigma**2)
        i0_val = _bessel_i0(bessel_arg)
        pc_direct += (r / sigma**2) * math.exp(exponent) * i0_val * dr

    return pc_direct


def _bessel_i0(x: float) -> float:
    """Modified Bessel function of the first kind, order 0.

    Uses polynomial approximation (Abramowitz & Stegun 9.8.1-2).
    """
    ax = abs(x)
    if ax < 3.75:
        t = (x / 3.75) ** 2
        return 1.0 + t * (
            3.5156229
            + t * (
                3.0899424
                + t * (
                    1.2067492
                    + t * (0.2659732 + t * (0.0360768 + t * 0.0045813))
                )
            )
        )
    else:
        t = 3.75 / ax
        return (
            math.exp(ax)
            / math.sqrt(ax)
            * (
                0.39894228
                + t * (
                    0.01328592
                    + t * (
                        0.00225319
                        + t * (
                            -0.00157565
                            + t * (
                                0.00916281
                                + t * (
                                    -0.02057706
                                    + t * (
                                        0.02635537
                                        + t * (-0.01647633 + t * 0.00392377)
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )


def compute_pc_for_conjunction(
    pos_primary_km: tuple[float, ...],
    vel_primary_km_s: tuple[float, ...],
    pos_secondary_km: tuple[float, ...],
    vel_secondary_km_s: tuple[float, ...],
    combined_radius_km: float = DEFAULT_COMBINED_RADIUS_KM,
    sigma_primary_km: float = DEFAULT_POSITION_SIGMA_KM,
    sigma_secondary_km: float = DEFAULT_POSITION_SIGMA_KM,
) -> dict:
    """Convenience wrapper: compute Pc from state vectors at TCA."""
    miss_vector = np.array(pos_primary_km) - np.array(pos_secondary_km)
    rel_velocity = np.array(vel_primary_km_s) - np.array(vel_secondary_km_s)

    return compute_collision_probability(
        miss_vector_km=miss_vector,
        rel_velocity_km_s=rel_velocity,
        sigma_primary_km=sigma_primary_km,
        sigma_secondary_km=sigma_secondary_km,
        combined_radius_km=combined_radius_km,
    )
