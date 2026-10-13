"""Collision probability computation using the 2D Pc method.

Implements the Chan (2003) / Alfano (2005) approach: project the
miss vector and combined covariance onto the conjunction plane
(perpendicular to relative velocity), then integrate a 2D Gaussian
over a circular hard-body radius.

Supports TLE-age-scaled covariance: position uncertainty grows with
TLE age following Vallado's empirical model, reflecting real-world
accuracy degradation as orbital perturbations accumulate.

References
----------
.. [1] S. Alfano, "A Numerical Implementation of Spherical Object
       Collision Probability," AMOS Technical Conference, 2005.
.. [2] F.K. Chan, *Spacecraft Collision Probability*, The Aerospace
       Press, 2008.
.. [3] D. Vallado, *Fundamentals of Astrodynamics and Applications*,
       4th Ed., Microcosm Press, 2013. Ch. 9, TLE accuracy vs. age.
.. [4] M. Abramowitz & I. Stegun, *Handbook of Mathematical Functions*,
       Dover, 1965. Sections 9.8.1-2: Polynomial approximations for I0(x).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from sda.constants import (
    DEFAULT_COMBINED_RADIUS_KM,
    DEFAULT_POSITION_SIGMA_KM,
    EARTH_RADIUS_KM,
)


def _rotation_matrix_to_conjunction_plane(
    rel_velocity: np.ndarray,
) -> np.ndarray:
    """Build rotation matrix whose Z-axis aligns with relative velocity.

    Returns a 3x3 matrix R such that R @ rel_velocity = [0, 0, |v|].
    The X-Y plane of the rotated frame is the conjunction (B-plane).
    """
    v_hat = rel_velocity / np.linalg.norm(rel_velocity)

    # Choose an arbitrary vector not parallel to v_hat
    arbitrary = np.array([1.0, 0.0, 0.0]) if abs(v_hat[0]) < 0.9 else np.array([0.0, 1.0, 0.0])

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
    miss_vector_km : (3,) array, relative position at TCA (primary - secondary) in ECI, km
    rel_velocity_km_s : (3,) array, relative velocity at TCA in ECI, km/s
    sigma_primary_km : 1-sigma position uncertainty for primary (isotropic), km
    sigma_secondary_km : 1-sigma position uncertainty for secondary (isotropic), km
    combined_radius_km : combined hard-body radius, km
    n_integration_steps : radial integration resolution

    Returns
    -------
    dict with keys: probability, miss_distance_km, combined_sigma_km,
                    mahalanobis_distance, hard_body_radius_km

    Notes
    -----
    2D Pc exhibits *probability dilution*: when the miss distance is small
    relative to sigma, Pc scales as ~r_hb^2 / (2 sigma^2), so LARGER
    uncertainty yields a LOWER reported Pc. A stale TLE can therefore make
    a genuinely close approach look improbable. Treat Pc as advisory; the
    miss-distance-based risk level (classify_risk) is the authoritative
    safety signal and never depends on Pc.
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

    # Combined covariance in B-plane (isotropic assumption -> diagonal)
    sigma_combined = math.sqrt(sigma_primary_km**2 + sigma_secondary_km**2)
    sx = sigma_combined
    sy = sigma_combined

    # Mahalanobis distance
    mahal = math.sqrt((xm / sx) ** 2 + (ym / sy) ** 2)

    # 2D Gaussian integration over circular hard-body region
    # Using polar coordinates centered on miss vector
    # Pc = integral integral  (1/(2pi sigmax sigmay)) exp(-0.5*((x/sigmax)^2 + (y/sigmay)^2)) dA
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
    For isotropic case (sigmax ~= sigmay = sigma), Pc = 1 - exp(-r^2/(2sigma^2)) when
    miss distance = 0. For offset miss, uses numerical radial-angular integration.
    """
    sigma = (sigma_x + sigma_y) / 2.0  # average for near-isotropic

    if sigma < 1e-15:
        return 1.0 if miss_2d < radius else 0.0

    # Use the Foster-Estes analytical approximation for circular cross-section
    # Pc ~= (r^2/(2sigma^2)) * exp(-(d^2)/(2sigma^2)) for small r/sigma
    # More precisely, use Rice distribution integral
    v = miss_2d**2 / (2.0 * sigma**2)

    if v > 500:
        return 0.0  # negligible, miss distance >> sigma

    # Direct numerical radial-angular integration
    # For isotropic Gaussian with circular HBR:
    # Pc = 1 - Q_1(sqrt(2*v), sqrt(2*u)) where Q_1 is Marcum Q-function
    # Approximate via: Pc ~= exp(-v) * (1 - exp(-u)) for u << 1
    # For better accuracy, use the full integral:
    pc_direct = 0.0
    dr = radius / max(n_steps, 1)
    for i in range(n_steps):
        r = (i + 0.5) * dr
        # Integrate in polar:
        # Integral over theta in [0, 2 pi] and r in [0, r_hb] of
        #   (1 / (2 pi sigma^2)) exp(-((r cos theta - d)^2 + (r sin theta)^2) / (2 sigma^2)) r
        # which reduces to the integral over r in [0, r_hb] of
        #   (r / sigma^2) exp(-(r^2 + d^2) / (2 sigma^2)) I0(r d / sigma^2)
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


# ---------------------------------------------------------------------------
# Full-covariance Pc methods (Foster quadrature, Chan series, Monte Carlo)
#
# These accept real 3x3 position covariances (e.g. from ingested CDMs) instead
# of the isotropic sigma used by compute_collision_probability(). Three
# mathematically independent computations are provided so results can be
# cross-validated (see sda.validation):
#
#   pc_foster()     , 2D numerical quadrature in the encounter plane
#                      (Foster & Estes 1992, as used by NASA CARA)
#   pc_chan()       , analytic series via equivalent-area transformation
#                      (Chan 2008); exact for isotropic covariance
#   pc_monte_carlo_2d(), direct sampling of the encounter-plane Gaussian
#
# References: Foster, J.L. and Estes, H.S., "A Parametric Analysis of Orbital
# Debris Collision Probability and Maneuver Rate for Space Vehicles",
# NASA/JSC-25898, 1992; Chan, F.K., "Spacecraft Collision Probability", 2008.
# ---------------------------------------------------------------------------

_MIN_VARIANCE_KM2 = 1e-24  # floor for degenerate covariance eigenvalues


def rtn_to_eci_matrix(pos_eci_km: np.ndarray, vel_eci_km_s: np.ndarray) -> np.ndarray:
    """Rotation matrix from the RTN (radial, transverse, normal) frame to ECI.

    Columns are the RTN basis vectors expressed in ECI, so for a covariance
    C_rtn the ECI covariance is  M @ C_rtn @ M.T.
    """
    r = np.asarray(pos_eci_km, dtype=float)
    v = np.asarray(vel_eci_km_s, dtype=float)
    r_hat = r / np.linalg.norm(r)
    n = np.cross(r, v)
    n_hat = n / np.linalg.norm(n)
    t_hat = np.cross(n_hat, r_hat)
    return np.column_stack([r_hat, t_hat, n_hat])


def rtn_covariance_to_eci(
    cov_rtn_km2: np.ndarray,
    pos_eci_km: np.ndarray,
    vel_eci_km_s: np.ndarray,
) -> np.ndarray:
    """Rotate a 3x3 RTN position covariance into the ECI frame."""
    m = rtn_to_eci_matrix(pos_eci_km, vel_eci_km_s)
    cov = np.asarray(cov_rtn_km2, dtype=float)
    return np.asarray(m @ cov @ m.T)


def project_covariance_to_encounter_plane(
    cov_eci_km2: np.ndarray,
    rel_velocity_km_s: np.ndarray,
) -> np.ndarray:
    """Project a 3x3 ECI covariance onto the 2D encounter (B-) plane.

    The encounter plane is perpendicular to the relative velocity; the same
    basis as _rotation_matrix_to_conjunction_plane() is used so projected
    miss vectors and covariances share coordinates.
    """
    rot = _rotation_matrix_to_conjunction_plane(np.asarray(rel_velocity_km_s, dtype=float))
    b = rot[:2, :]  # 2x3: encounter-plane basis rows
    return np.asarray(b @ np.asarray(cov_eci_km2, dtype=float) @ b.T)


def project_miss_to_encounter_plane(
    miss_vector_km: np.ndarray,
    rel_velocity_km_s: np.ndarray,
) -> np.ndarray:
    """Project a 3D miss vector onto the 2D encounter (B-) plane.

    Companion to project_covariance_to_encounter_plane(): both use the same
    basis, so the returned 2D miss vector and the projected covariance are
    expressed in one coordinate system and can be handed straight to any of
    the pc_* functions.
    """
    rot = _rotation_matrix_to_conjunction_plane(np.asarray(rel_velocity_km_s, dtype=float))
    return np.asarray((rot @ np.asarray(miss_vector_km, dtype=float))[:2])


def _principal_frame(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
) -> tuple[float, float, float, float]:
    """Diagonalize a 2x2 covariance; return (mx, my, sigma_x, sigma_y).

    The miss vector is rotated into the covariance principal axes so the
    Gaussian is axis-aligned. Eigenvalues are floored to avoid division by
    zero for degenerate covariances.
    """
    cov = np.asarray(cov_2d_km2, dtype=float)
    cov = 0.5 * (cov + cov.T)  # enforce symmetry
    eigvals, eigvecs = np.linalg.eigh(cov)
    eigvals = np.maximum(eigvals, _MIN_VARIANCE_KM2)
    m = eigvecs.T @ np.asarray(miss_2d_km, dtype=float)
    return float(m[0]), float(m[1]), float(math.sqrt(eigvals[0])), float(math.sqrt(eigvals[1]))


def pc_foster(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
    n_radial: int = 200,
    n_theta: int = 360,
) -> float:
    """Foster-method Pc: 2D quadrature of the encounter-plane Gaussian.

    Integrates the (possibly anisotropic) Gaussian PDF centered at the miss
    vector over the combined hard-body disk at the origin, using midpoint
    quadrature in polar coordinates on the covariance principal axes.
    """
    mx, my, sx, sy = _principal_frame(miss_2d_km, cov_2d_km2)
    r_hb = float(hard_body_radius_km)
    if r_hb <= 0.0:
        return 0.0

    miss_norm = math.hypot(mx, my)
    sigma_max = max(sx, sy)
    sigma_min = min(sx, sy)

    if r_hb >= miss_norm + 40.0 * sigma_max:
        # Disk contains all probability mass beyond 40 sigma: Pc = 1 to
        # double precision, and a fixed grid could not resolve the peak.
        return 1.0

    # Integrate only where the Gaussian has support, and refine the grid
    # when the covariance is much smaller than the integration domain.
    r_upper = min(r_hb, miss_norm + 40.0 * sigma_max)
    n_r = max(n_radial, min(6000, int(4.0 * r_upper / max(sigma_min, r_upper / 6000.0)) + 1))
    angular_scale = sigma_min / (miss_norm + sigma_min)
    n_t = max(n_theta, min(6000, int(8.0 * math.pi / angular_scale) + 1))

    r = (np.arange(n_r) + 0.5) * (r_upper / n_r)
    theta = (np.arange(n_t) + 0.5) * (2.0 * math.pi / n_t)
    cos_t = np.cos(theta)
    sin_t = np.sin(theta)
    dr = r_upper / n_r
    dtheta = 2.0 * math.pi / n_t

    # Chunk the radial axis so adaptive grids stay memory-bounded.
    pc = 0.0
    chunk = max(1, 8_000_000 // n_t)
    for start in range(0, n_r, chunk):
        r_blk = r[start:start + chunk, None]
        px = r_blk * cos_t[None, :]
        py = r_blk * sin_t[None, :]
        exponent = -0.5 * (((px - mx) / sx) ** 2 + ((py - my) / sy) ** 2)
        density = np.exp(np.maximum(exponent, -745.0))
        pc += float(np.sum(density * r_blk))
    pc *= dr * dtheta / (2.0 * math.pi * sx * sy)
    return max(0.0, min(1.0, pc))


def pc_chan(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
    max_terms: int = 1000,
    tol: float = 1e-16,
) -> float:
    """Chan-method Pc: convergent analytic series (Chan 2008, Eq. 4-4).

    Uses the equivalent-area transformation u = R^2/(sx*sy),
    v = (mx/sx)^2 + (my/sy)^2, which replaces the scaled hard-body ellipse
    with a circle of the same area and evaluates the resulting isotropic
    problem. Exact when sx == sy, an approximation otherwise.

    Validity limit (measured against the 53 operational CARA reference
    conjunctions carried by sda.cara; worst relative error vs CARA Pc2D):

        aspect ratio  1 to 10     3.2e-2
        aspect ratio 10 to 20     1.2e-1
        aspect ratio 20 to 50     3.5e-2
        aspect ratio 50 to 100    8.6e-1
        aspect ratio above 100    9.9e-1 (worst case, aspect ratio 8.6e3)

    The residual is the equivalent-area reduction, not this series: the sum
    is bit-identical when forced to 500000 terms with tol=0, and it matches
    an independent quadrature of the reduced isotropic problem to 1.2e-9
    relative. Prefer pc_alfano or pc_foster above CHAN_ASPECT_RATIO_LIMIT
    (both match CARA Pc2D to 5.4e-5 over those same cases). The aspect ratio
    alone does not bound the error, because a deep tail amplifies it at any
    elongation: call chan_reduction_error_estimate() for the per-geometry
    bound, which pc_usage_checks applies when it raises CHAN_ASPECT_RATIO.

    Evaluated as Pc = sum_k P_u(k) * CDF_v(k-1) with P_x the Poisson(x/2)
    pmf, the summation order that keeps every term a positive product.
    (The textbook order needs the complement 1 - CDF_u(m), which loses all
    precision to cancellation once the true complement is below 1 ulp and
    inflates deep-tail results by tens of percent.)
    """
    mx, my, sx, sy = _principal_frame(miss_2d_km, cov_2d_km2)
    r_hb = float(hard_body_radius_km)
    if r_hb <= 0.0:
        return 0.0

    half_u = 0.5 * r_hb * r_hb / (sx * sy)
    half_v = 0.5 * ((mx / sx) ** 2 + (my / sy) ** 2)

    if half_u > 700.0 or half_v > 700.0:
        # exp() underflow territory. Pc = P(K > M) for independent
        # K ~ Poisson(half_u), M ~ Poisson(half_v); with either mean this
        # large the normal approximation of K - M is exact to double
        # precision except in the operationally absurd transition zone
        # where the disk radius and a > 37-sigma miss are comparable.
        z = (half_u - half_v) / math.sqrt(half_u + half_v)
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    pc = 0.0
    v_pow = math.exp(-half_v)  # P_v(m) at m = 0
    cdf_v = v_pow  # sum of P_v(m) for m <= k-1, starting at k = 1
    u_pow = math.exp(-half_u) * half_u  # P_u(k) at k = 1
    for k in range(1, max_terms):
        pc += u_pow * cdf_v
        if k > half_u and u_pow < tol * max(pc, 1e-300):
            break
        u_pow *= half_u / (k + 1)
        v_pow *= half_v / k
        cdf_v += v_pow
    return max(0.0, min(1.0, pc))


def chan_reduction_error_estimate(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
) -> float:
    """Estimated relative error of the Chan equivalent-area reduction.

    Expanding both the exact encounter-plane integral and the Chan reduced
    integral to second order in the hard-body radius R gives, in the
    covariance principal frame with sigmas sx, sy and miss (mx, my),

        exact  ~ (R^2 / (2 sx sy)) exp(-v/2) [1 + (R^2/8) (mx^2/sx^4
                 + my^2/sy^4 - 1/sx^2 - 1/sy^2)]
        Chan   ~ (R^2 / (2 sx sy)) exp(-v/2) [1 + (R^2/8) (v - 2)/(sx sy)]

    with v = (mx/sx)^2 + (my/sy)^2. Their difference is returned:

        (R^2/8) |k (mx^2/sx^3 - my^2/sy^3) - k^2|,   k = 1/sx - 1/sy

    It vanishes for an isotropic covariance, where Chan is exact, and grows
    with both the elongation and the depth of the tail. Checked against the
    53 CARA reference conjunctions plus a synthetic elongated case: no case
    with a true error above 10 percent estimates below 5 percent, and no case
    estimating above 5 percent has a true error below 5 percent. The estimate
    tracks the true error to about 20 percent while it is small and saturates
    (over-predicts) once the true error approaches 1.

    A pure aspect-ratio test is not enough on its own: the synthetic case has
    an aspect ratio of 4.8 and a true Chan error of 51 percent, because the
    miss sits deep in the tail.
    """
    mx, my, sx, sy = _principal_frame(miss_2d_km, cov_2d_km2)
    r_hb = float(hard_body_radius_km)
    if r_hb <= 0.0:
        return 0.0
    k = 1.0 / sx - 1.0 / sy
    return abs(r_hb * r_hb / 8.0 * (k * (mx * mx / sx**3 - my * my / sy**3) - k * k))


def pc_monte_carlo_2d(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
    n_samples: int = 1_000_000,
    seed: int = 42,
) -> float:
    """Monte Carlo Pc: sample the encounter-plane Gaussian directly.

    Method-independent ground truth for validating pc_foster/pc_chan.
    Statistical 1-sigma relative error is roughly 1/sqrt(n*Pc).
    """
    mx, my, sx, sy = _principal_frame(miss_2d_km, cov_2d_km2)
    rng = np.random.default_rng(seed)
    hits = 0
    remaining = int(n_samples)
    chunk = 4_000_000
    r2 = float(hard_body_radius_km) ** 2
    while remaining > 0:
        n = min(remaining, chunk)
        x = rng.normal(mx, sx, n)
        y = rng.normal(my, sy, n)
        hits += int(np.count_nonzero(x * x + y * y <= r2))
        remaining -= n
    return hits / float(n_samples)


def compute_collision_probability_full(
    miss_vector_km: np.ndarray,
    rel_velocity_km_s: np.ndarray,
    cov_primary_eci_km2: np.ndarray,
    cov_secondary_eci_km2: np.ndarray,
    combined_radius_km: float = DEFAULT_COMBINED_RADIUS_KM,
) -> dict:
    """Compute 2D Pc from full 3x3 ECI position covariances.

    Combines both objects' covariances, projects onto the encounter plane,
    and evaluates both the Foster quadrature and the Chan series.

    Returns a dict with keys: probability (Foster), probability_chan,
    probability_alfano, miss_distance_km, mahalanobis_distance,
    hard_body_radius_km, encounter_covariance_km2 (2x2 nested list) and
    covariance_remediated (bool).

    Non-positive-definite input is not an error: the combined 3x3 covariance
    and the projected 2x2 both go through remediate_covariance(), which clips
    the eigenvalues onto a positive floor of (1e-4 * HBR)^2 and symmetrizes.
    covariance_remediated says whether that clipping changed anything, so a
    caller can flag the Pc as computed from a repaired covariance rather than
    receiving a NaN or a silently wrong number.

    The probability-dilution caveat of compute_collision_probability()
    applies equally here: risk classification never depends on Pc.
    """
    miss = np.asarray(miss_vector_km, dtype=float)
    rel_v = np.asarray(rel_velocity_km_s, dtype=float)
    miss_distance = float(np.linalg.norm(miss))

    if float(np.linalg.norm(rel_v)) < 1e-10:
        return {
            "probability": 0.0,
            "probability_chan": 0.0,
            "probability_alfano": 0.0,
            "miss_distance_km": miss_distance,
            "mahalanobis_distance": float("inf"),
            "hard_body_radius_km": combined_radius_km,
            "encounter_covariance_km2": [[0.0, 0.0], [0.0, 0.0]],
            "covariance_remediated": False,
        }

    cov_combined = np.asarray(cov_primary_eci_km2, dtype=float) + np.asarray(
        cov_secondary_eci_km2, dtype=float
    )
    cov_combined, remediated_3d = remediate_covariance(cov_combined, combined_radius_km)
    rot = _rotation_matrix_to_conjunction_plane(rel_v)
    miss_2d = (rot @ miss)[:2]
    cov_2d = project_covariance_to_encounter_plane(cov_combined, rel_v)
    cov_2d, remediated_2d = remediate_covariance(cov_2d, combined_radius_km)

    mx, my, sx, sy = _principal_frame(miss_2d, cov_2d)
    mahal = math.sqrt((mx / sx) ** 2 + (my / sy) ** 2)

    return {
        "probability": pc_foster(miss_2d, cov_2d, combined_radius_km),
        "probability_chan": pc_chan(miss_2d, cov_2d, combined_radius_km),
        "probability_alfano": pc_alfano(miss_2d, cov_2d, combined_radius_km),
        "miss_distance_km": miss_distance,
        "mahalanobis_distance": mahal,
        "hard_body_radius_km": combined_radius_km,
        "encounter_covariance_km2": [[float(c) for c in row] for row in cov_2d],
        "covariance_remediated": bool(remediated_3d or remediated_2d),
    }


def compute_pc_for_conjunction(
    pos_primary_km: tuple[float, ...],
    vel_primary_km_s: tuple[float, ...],
    pos_secondary_km: tuple[float, ...],
    vel_secondary_km_s: tuple[float, ...],
    combined_radius_km: float = DEFAULT_COMBINED_RADIUS_KM,
    sigma_primary_km: float = DEFAULT_POSITION_SIGMA_KM,
    sigma_secondary_km: float = DEFAULT_POSITION_SIGMA_KM,
    method: str = "legacy",
) -> dict:
    """Convenience wrapper: compute Pc from state vectors at TCA.

    `method` selects how the probability is evaluated from the isotropic
    combined covariance sigma_primary^2 + sigma_secondary^2:

    legacy
        The polar quadrature of compute_collision_probability(); the default,
        so existing callers keep their exact numbers.
    foster, chan, alfano
        The encounter-plane methods, evaluated on the same isotropic
        covariance. The returned dict gains a "method" key and keeps every key
        the legacy path returns, so callers can switch without changing shape.
    """
    miss_vector = np.array(pos_primary_km) - np.array(pos_secondary_km)
    rel_velocity = np.array(vel_primary_km_s) - np.array(vel_secondary_km_s)

    if method.lower() == "legacy":
        result = compute_collision_probability(
            miss_vector_km=miss_vector,
            rel_velocity_km_s=rel_velocity,
            sigma_primary_km=sigma_primary_km,
            sigma_secondary_km=sigma_secondary_km,
            combined_radius_km=combined_radius_km,
        )
        result["method"] = "legacy"
        return result

    combined_sigma = math.sqrt(sigma_primary_km**2 + sigma_secondary_km**2)
    cov_2d = np.eye(2) * max(combined_sigma**2, _MIN_VARIANCE_KM2)
    speed = float(np.linalg.norm(rel_velocity))
    miss_distance = float(np.linalg.norm(miss_vector))
    if speed < 1e-10:
        miss_2d = miss_vector[:2]
    else:
        miss_2d = (_rotation_matrix_to_conjunction_plane(rel_velocity) @ miss_vector)[:2]
    mahal = float(np.linalg.norm(miss_2d)) / max(combined_sigma, math.sqrt(_MIN_VARIANCE_KM2))
    return {
        "probability": pc_by_method(method, miss_2d, cov_2d, combined_radius_km),
        "miss_distance_km": miss_distance,
        "combined_sigma_km": combined_sigma,
        "mahalanobis_distance": mahal,
        "hard_body_radius_km": combined_radius_km,
        "method": method.lower(),
    }


def sigma_from_tle_age(
    tle_age_hours: float,
    base_sigma_km: float = DEFAULT_POSITION_SIGMA_KM,
) -> float:
    """Scale position uncertainty based on TLE epoch age.

    Empirical model following Vallado (2013, Ch. 9): TLE position
    error grows roughly as the square root of epoch age for the first
    few days, then linearly. Fresh TLEs (<6h) have ~50m uncertainty;
    a 3-day-old TLE degrades to ~500m.

    Parameters
    ----------
    tle_age_hours : hours since TLE epoch
    base_sigma_km : 1-sigma uncertainty for a fresh TLE (default 50m)

    Returns
    -------
    Scaled 1-sigma position uncertainty in km.

    Notes
    -----
    Feeding this into the 2D Pc computation triggers probability dilution
    for stale TLEs (see compute_collision_probability): the growth here is
    conservative for uncertainty, but it can DECREASE the reported Pc of a
    close approach. Risk classification intentionally ignores Pc.
    """
    if tle_age_hours <= 0:
        return base_sigma_km

    # Quadratic growth for first 72h, then linear
    age_days = tle_age_hours / 24.0
    if age_days <= 3.0:  # noqa: SIM108, branch comments explain the growth regimes
        # sqrt growth: sigma ~= sigma0 * (1 + age_days)
        scale = 1.0 + age_days * 3.0
    else:
        # Linear growth beyond 3 days
        scale = 10.0 + (age_days - 3.0) * 5.0

    return base_sigma_km * scale


# ---------------------------------------------------------------------------
# Alfano series Pc, CARA-style usage checks, maximum-Pc utilities and
# covariance realism / remediation.
#
# The strip series below follows S. Alfano, "A Numerical Implementation of
# Spherical Object Collision Probability" (2005). The satguard implementation
# (reference/satguard, MIT) was read for its interface; the quadrature here is
# our own and needs no scipy. Regime default covariances are adapted from
# satguard covariance/realism.py (MIT). The usage-violation, maximum-Pc and
# covariance-remediation logic follows NASA CARA_Analysis_Tools (NASA Open
# Source Agreement 1.3): PcMultiStep.m, Utils/UsageViolationPc2D.m,
# Utils/RemediateCovariance2x2.m, Maximum2DPc/FrisbeeMaxPc.m and
# Maximum2DPc/DilutionMaxPc.m were read for method only; the Python is new.
#
# Further references
# ------------------
# .. [5] M. Hejduk, "Satellite Conjunction Assessment Risk Analysis for
#        'Dilution Region' Events", Space Traffic Management Conf., 2019.
# .. [6] R. Frisbee, maximum Pc with a missing covariance, as implemented in
#        CARA Maximum2DPc/FrisbeeMaxPc.m.
# ---------------------------------------------------------------------------

_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)

# Adaptive-Simpson control for pc_alfano (see its docstring for the argument).
_ALFANO_MIN_INTERVALS = 32
_ALFANO_MAX_INTERVALS = 16384
_ALFANO_TOL = 1e-12
# Gaussian support half-width used to trim the strip range: truncating at
# 12 sigma drops at most exp(-72) ~ 5e-32 of the integral, relative.
_ALFANO_SIGMA_SPAN = 12.0

# CARA eigenvalue-clipping ladder from Utils/RemediateCovariance2x2.m: the
# clipping floor is (Fclip * HBR)^2, tried smallest first.
# Encounter-plane sigma ratio above which the Chan equivalent-area reduction
# stops being trustworthy. Measured on the 53 CARA reference conjunctions:
# worst relative error vs CARA Pc2D is 3.2e-2 below this ratio and rises to
# 9.9e-1 above it. See the pc_chan docstring for the full table.
CHAN_ASPECT_RATIO_LIMIT = 10.0
# Estimated relative error of the Chan reduction (see chan_reduction_error_estimate)
# above which the Chan value should not be reported. 5 percent separates the
# CARA cases cleanly: nothing above it has a true error below 5 percent and
# nothing below it has a true error above 10 percent.
CHAN_ERROR_ESTIMATE_LIMIT = 0.05
_CLIP_FACTORS = (1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2)

# CARA PcMultiStep.m usage-violation defaults.
_EXTENDED_DURATION_FRACTION = 2e-2
_OFFSET_CUTOFF = 1e-2
_NOMINAL_PERIOD_S = 5400.0  # nominal LEO period, used when none is supplied


def _erf_diff(lo: float, hi: float) -> float:
    """erf(hi) - erf(lo) evaluated without tail cancellation (hi >= lo).

    Both erf values approach +/-1 in the tails, so the naive difference loses
    every significant digit exactly in the small-Pc regime. Using erfc on the
    side where both arguments share a sign keeps full relative accuracy.
    """
    if lo >= 0.0:
        return math.erfc(lo) - math.erfc(hi)
    if hi <= 0.0:
        return math.erfc(-hi) - math.erfc(-lo)
    return math.erf(hi) - math.erf(lo)


@dataclass(frozen=True)
class AlfanoQuadrature:
    """Alfano-series Pc together with its convergence diagnostics."""

    probability: float
    intervals: int
    relative_change: float
    converged: bool


def _alfano_strip(
    t: float,
    mx: float,
    my: float,
    sx: float,
    sy: float,
    r_hb: float,
    e_shift: float,
) -> float:
    """One Alfano strip: chord length times the along-chord Gaussian mass."""
    h = r_hb * math.cos(t)
    x = mx + r_hb * math.sin(t)
    exponent = 0.5 * (x / sx) ** 2 - e_shift
    if exponent > 700.0:
        return 0.0
    scale = _SQRT_2 * sy
    return math.exp(-exponent) * _erf_diff((my - h) / scale, (my + h) / scale) * h


def pc_alfano_quadrature(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
    tol: float = _ALFANO_TOL,
    max_intervals: int = _ALFANO_MAX_INTERVALS,
) -> AlfanoQuadrature:
    """Alfano-series Pc with the interval count and convergence flag.

    Method
    ------
    Work in the covariance principal axes, where the Gaussian is axis aligned
    at (mx, my) and the hard-body disk of radius R sits at the origin. Slicing
    the disk into vertical strips lets the y integral be done in closed form,
    so the double integral collapses to one dimension::

        Pc = int_{mx-R}^{mx+R} N(x; mx, sx) * 0.5 *
             [erf((my+h)/(sqrt(2) sy)) - erf((my-h)/(sqrt(2) sy))] dx

    with the half-chord h(x) = sqrt(R^2 - (x - mx)^2). That is Alfano's
    series: a sum of error-function strips. The substitution x = mx + R sin(t)
    gives h = R cos(t) and dx = R cos(t) dt, which removes the square-root
    endpoint singularity; without it the integrand has infinite slope at the
    disk edge and Simpson's rule degrades from O(n^-4) to about O(n^-1.5).

    Term count and convergence
    --------------------------
    Composite Simpson is applied on the t interval, doubling the number of
    strips until two successive estimates agree to `tol` relatively, starting
    at 32 and stopping at `max_intervals` (16384). Because the substituted
    integrand is analytic on the closed interval and vanishes quadratically at
    both ends, the O(h^4) rate holds and the doubling terminates after two or
    three refinements for every case in sda.validation. The t range is first
    trimmed to the part of the disk within 12 sigma of the point of closest
    approach in x, which bounds the discarded mass by exp(-72) relative and
    keeps the strip count small when the covariance is far smaller than the
    hard-body radius.

    Numerical stability
    -------------------
    The Gaussian exponent is shifted by its maximum over the disk, so the
    exponential is O(1) inside the integral and the whole dynamic range is
    carried by one final multiplication; small Pc values stay accurate down to
    the double-precision underflow limit near 1e-308 instead of being lost at
    1e-16. The erf difference is evaluated through erfc on the matching side
    (see _erf_diff) so no cancellation occurs in the tails.
    """
    mx, my, sx, sy = _principal_frame(miss_2d_km, cov_2d_km2)
    r_hb = float(hard_body_radius_km)
    if r_hb <= 0.0:
        return AlfanoQuadrature(0.0, 0, 0.0, True)

    miss_norm = math.hypot(mx, my)
    if r_hb >= miss_norm + 40.0 * max(sx, sy):
        # Disk swallows the whole distribution; matches the pc_foster early-out.
        return AlfanoQuadrature(1.0, 0, 0.0, True)

    # Closest approach of the strip range [mx - R, mx + R] to the Gaussian mean.
    x_near = 0.0 if abs(mx) <= r_hb else abs(mx) - r_hb
    e_shift = 0.5 * (x_near / sx) ** 2
    if e_shift > 745.0:
        return AlfanoQuadrature(0.0, 0, 0.0, True)  # exp(-e_shift) underflows

    span = _ALFANO_SIGMA_SPAN * sx
    x_centre = 0.0 if abs(mx) <= r_hb else math.copysign(x_near, mx)
    x_lo = max(mx - r_hb, x_centre - span)
    x_hi = min(mx + r_hb, x_centre + span)
    if x_hi <= x_lo:
        return AlfanoQuadrature(0.0, 0, 0.0, True)
    t_lo = math.asin(max(-1.0, min(1.0, (x_lo - mx) / r_hb)))
    t_hi = math.asin(max(-1.0, min(1.0, (x_hi - mx) / r_hb)))
    if t_hi <= t_lo:
        return AlfanoQuadrature(0.0, 0, 0.0, True)

    def simpson(n: int) -> float:
        step = (t_hi - t_lo) / n
        total = _alfano_strip(t_lo, mx, my, sx, sy, r_hb, e_shift)
        total += _alfano_strip(t_hi, mx, my, sx, sy, r_hb, e_shift)
        for i in range(1, n):
            weight = 4.0 if i % 2 else 2.0
            total += weight * _alfano_strip(t_lo + i * step, mx, my, sx, sy, r_hb, e_shift)
        return total * step / 3.0

    intervals = _ALFANO_MIN_INTERVALS
    estimate = simpson(intervals)
    relative_change = float("inf")
    converged = False
    while intervals < max_intervals:
        intervals *= 2
        refined = simpson(intervals)
        relative_change = abs(refined - estimate) / max(abs(refined), 1e-300)
        estimate = refined
        if relative_change < tol:
            converged = True
            break

    pc = estimate * math.exp(-e_shift) / (2.0 * _SQRT_2PI * sx)
    return AlfanoQuadrature(max(0.0, min(1.0, pc)), intervals, relative_change, converged)


def pc_alfano(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
) -> float:
    """Alfano-series Pc for an encounter-plane 2x2 covariance.

    Same input conventions as pc_foster and pc_chan: the 2D miss vector and
    covariance are in the encounter plane (km, km^2) and the hard-body radius
    is the combined radius of both objects. See pc_alfano_quadrature for the
    method, the term-count rule and the stability argument.
    """
    return pc_alfano_quadrature(miss_2d_km, cov_2d_km2, hard_body_radius_km).probability


PC_METHODS: tuple[str, ...] = ("foster", "chan", "alfano")


def pc_by_method(
    method: str,
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
) -> float:
    """Dispatch to one encounter-plane Pc method by name."""
    key = method.lower()
    if key == "foster":
        return pc_foster(miss_2d_km, cov_2d_km2, hard_body_radius_km)
    if key == "chan":
        return pc_chan(miss_2d_km, cov_2d_km2, hard_body_radius_km)
    if key == "alfano":
        return pc_alfano(miss_2d_km, cov_2d_km2, hard_body_radius_km)
    raise ValueError(f"unknown Pc method '{method}'; expected one of {PC_METHODS}")


# ---------------------------------------------------------------------------
# Covariance remediation
# ---------------------------------------------------------------------------


def _is_positive_definite(matrix: np.ndarray) -> bool:
    """True when a Cholesky factorization of the symmetric matrix succeeds."""
    try:
        np.linalg.cholesky(matrix)
    except np.linalg.LinAlgError:
        return False
    return True


def remediate_covariance(
    cov_km2: np.ndarray,
    hard_body_radius_km: float = DEFAULT_COMBINED_RADIUS_KM,
) -> tuple[np.ndarray, bool]:
    """Clip a covariance onto the positive-definite cone (CARA style).

    Follows CARA Utils/RemediateCovariance2x2.m: symmetrize, and if the matrix
    is not numerically positive definite, raise every eigenvalue to a floor of
    (Fclip * HBR)^2, trying CARA's ladder of clipping factors
    (1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2) smallest first and rebuilding the
    matrix from the clipped spectrum. Works for 2x2 and 3x3 inputs.

    One deliberate deviation: where CARA returns NaN if no factor in the
    ladder works, this floors the spectrum at 4 eps times the largest
    eigenvalue instead, so the caller always receives a usable matrix rather
    than a NaN that would poison the Pc downstream.

    Returns (remediated_covariance, was_remediated).
    """
    cov = np.asarray(cov_km2, dtype=float)
    cov = 0.5 * (cov + cov.T)
    if not np.all(np.isfinite(cov)):
        raise ValueError("covariance contains non-finite entries")

    eigvals, eigvecs = np.linalg.eigh(cov)
    if float(eigvals.min()) > 0.0 and _is_positive_definite(cov):
        return cov, False

    hbr = abs(float(hard_body_radius_km))
    if hbr <= 0.0:
        hbr = DEFAULT_COMBINED_RADIUS_KM
    floors = [(factor * hbr) ** 2 for factor in _CLIP_FACTORS]
    floors.append(4.0 * float(np.finfo(float).eps) * max(float(eigvals.max()), 0.0))
    for floor in floors:
        if floor <= 0.0:
            continue
        fixed = eigvecs @ np.diag(np.maximum(eigvals, floor)) @ eigvecs.T
        fixed = 0.5 * (fixed + fixed.T)
        if _is_positive_definite(fixed):
            return fixed, True

    # Last resort: an isotropic floor, which is positive definite by build.
    floor = max(floors[0], 4.0 * float(np.finfo(float).eps) * max(float(eigvals.max()), 1.0))
    return np.eye(cov.shape[0]) * floor, True


def covariance_condition_number(cov_km2: np.ndarray) -> float:
    """Ratio of largest to smallest eigenvalue; inf for a singular matrix."""
    cov = np.asarray(cov_km2, dtype=float)
    eigvals = np.linalg.eigvalsh(0.5 * (cov + cov.T))
    smallest = float(eigvals.min())
    if smallest <= 0.0:
        return float("inf")
    return float(eigvals.max()) / smallest


# ---------------------------------------------------------------------------
# Maximum Pc utilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrisbeeMaxPcResult:
    """Maximum Pc over all covariances the missing object could have."""

    max_pc: float
    pc_known_only: float
    mahalanobis_distance: float
    added_variance_km2: float
    combined_covariance_km2: list[list[float]]
    bounded: bool
    method: str


def frisbee_max_pc(
    miss_2d_km: np.ndarray,
    cov_known_2d_km2: np.ndarray,
    hard_body_radius_km: float,
    method: str = "alfano",
) -> FrisbeeMaxPcResult:
    """Maximum Pc when only one object's covariance is known.

    Method follows CARA Maximum2DPc/FrisbeeMaxPc.m. With the known encounter
    plane covariance A and miss vector r, form Ka = sqrt(r' inv(A) r). If
    Ka <= 1 the known covariance already spreads further than the miss
    distance and no rank-one addition along r can raise Pc, so the maximum is
    the Pc of A itself. Otherwise the maximizing unknown covariance is the
    rank-one matrix Vc * u u' along the miss direction u = r / |r| with
    Vc = |r|^2 (Ka^2 - 1) / Ka^2, and the maximum Pc is evaluated with the sum
    A + Vc u u'.

    The result is an upper bound on Pc, never an estimate of it: use it to
    decide whether a conjunction can be screened out when a covariance is
    missing, not to report a probability.
    """
    miss = np.asarray(miss_2d_km, dtype=float).reshape(2)
    cov, _ = remediate_covariance(np.asarray(cov_known_2d_km2, dtype=float), hard_body_radius_km)
    pc_known = pc_by_method(method, miss, cov, hard_body_radius_km)

    miss_norm = float(np.linalg.norm(miss))
    inv_cov = np.linalg.inv(cov)
    ka_sq = float(miss @ inv_cov @ miss)
    ka = math.sqrt(max(ka_sq, 0.0))
    if ka <= 1.0 or miss_norm <= 0.0:
        return FrisbeeMaxPcResult(
            max_pc=pc_known,
            pc_known_only=pc_known,
            mahalanobis_distance=ka,
            added_variance_km2=0.0,
            combined_covariance_km2=[[float(c) for c in row] for row in cov],
            bounded=False,
            method=method,
        )

    unit = miss / miss_norm
    added = miss_norm**2 * (ka_sq - 1.0) / ka_sq
    combined = cov + added * np.outer(unit, unit)
    return FrisbeeMaxPcResult(
        max_pc=pc_by_method(method, miss, combined, hard_body_radius_km),
        pc_known_only=pc_known,
        mahalanobis_distance=ka,
        added_variance_km2=float(added),
        combined_covariance_km2=[[float(c) for c in row] for row in combined],
        bounded=True,
        method=method,
    )


@dataclass(frozen=True)
class DilutionScanResult:
    """Maximum Pc over a scan of covariance scale factors."""

    pc_actual: float
    max_pc: float
    scale_at_max: float
    diluted: bool
    scales: list[float] = field(default_factory=list)
    pc_values: list[float] = field(default_factory=list)
    method: str = "alfano"


def dilution_max_pc(
    miss_2d_km: np.ndarray,
    cov_2d_km2: np.ndarray,
    hard_body_radius_km: float,
    scale_min: float = 1e-4,
    scale_max: float = 1e4,
    n_points: int = 121,
    method: str = "alfano",
) -> DilutionScanResult:
    """Scan a covariance scale factor for the maximum achievable Pc.

    Method follows CARA Maximum2DPc/DilutionMaxPc.m and the dilution analysis
    of Hejduk (2019). The encounter-plane covariance is multiplied by a scale
    factor s swept logarithmically over [scale_min, scale_max]; Pc rises with
    s while the hard-body disk is still gathering probability mass and falls
    once the mass spreads past it. The grid maximum is refined by a golden
    section search in log(s) between its neighbours.

    An event is reported as diluted when the maximum sits at s > 1, that is
    when growing the covariance would raise Pc: the reported Pc is then low
    because the uncertainty is large, not because the encounter is safe.

    For an isotropic covariance with miss distance d much larger than the
    hard-body radius R, Pc(s) ~ (R^2 / (2 s sigma^2)) exp(-d^2 / (2 s sigma^2))
    peaks at s sigma^2 = d^2 / 2, giving max_pc = (R/d)^2 / e; the scan
    reproduces that closed form, which is how it is checked in the tests.
    """
    miss = np.asarray(miss_2d_km, dtype=float).reshape(2)
    if n_points < 3:
        raise ValueError("n_points must be at least 3")
    if not 0.0 < scale_min < scale_max:
        raise ValueError("require 0 < scale_min < scale_max")
    cov, _ = remediate_covariance(np.asarray(cov_2d_km2, dtype=float), hard_body_radius_km)

    def pc_at(log_scale: float) -> float:
        return pc_by_method(method, miss, math.exp(log_scale) * cov, hard_body_radius_km)

    log_lo = math.log(scale_min)
    log_hi = math.log(scale_max)
    step = (log_hi - log_lo) / (n_points - 1)
    logs = [log_lo + i * step for i in range(n_points)]
    values = [pc_at(x) for x in logs]

    best = max(range(n_points), key=lambda i: values[i])
    best_log = logs[best]
    best_pc = values[best]

    # Golden section refinement of the bracketed maximum in log space.
    inv_phi = (math.sqrt(5.0) - 1.0) / 2.0
    a = logs[max(best - 1, 0)]
    b = logs[min(best + 1, n_points - 1)]
    c = b - inv_phi * (b - a)
    d = a + inv_phi * (b - a)
    fc, fd = pc_at(c), pc_at(d)
    for _ in range(60):
        if b - a < 1e-8:
            break
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - inv_phi * (b - a)
            fc = pc_at(c)
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = pc_at(d)
    if fc > best_pc:
        best_log, best_pc = c, fc
    if fd > best_pc:
        best_log, best_pc = d, fd

    pc_actual = pc_by_method(method, miss, cov, hard_body_radius_km)
    scale_at_max = math.exp(best_log)
    diluted = scale_at_max > 1.0 and best_pc > pc_actual * (1.0 + 1e-9)
    return DilutionScanResult(
        pc_actual=pc_actual,
        max_pc=best_pc,
        scale_at_max=scale_at_max,
        diluted=diluted,
        scales=[math.exp(x) for x in logs],
        pc_values=values,
        method=method,
    )


# ---------------------------------------------------------------------------
# Usage-violation checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PcUsageWarning:
    """One named usage violation with the numbers that triggered it."""

    code: str
    message: str
    values: dict[str, float]


@dataclass(frozen=True)
class PcUsageReport:
    """Structured result of pc_usage_checks."""

    probability: float
    miss_distance_km: float
    relative_speed_km_s: float
    encounter_duration_s: float
    duration_fraction_of_period: float
    condition_number: float
    aspect_ratio: float
    chan_error_estimate: float
    positive_definite: bool
    hbr_over_miss_distance: float
    max_pc_dilution: float
    dilution_scale: float
    warnings: list[PcUsageWarning] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        """Warning codes in the order they were raised."""
        return [w.code for w in self.warnings]

    @property
    def ok(self) -> bool:
        """True when the 2D Pc assumptions hold well enough to report Pc."""
        return not self.warnings


def pc_usage_checks(
    miss_vector_km: np.ndarray,
    rel_velocity_km_s: np.ndarray,
    cov_combined_eci_km2: np.ndarray,
    hard_body_radius_km: float = DEFAULT_COMBINED_RADIUS_KM,
    orbital_period_s: float = _NOMINAL_PERIOD_S,
    low_relative_speed_km_s: float = 0.1,
    duration_fraction_limit: float = _EXTENDED_DURATION_FRACTION,
    aspect_ratio_limit: float = 1000.0,
    chan_aspect_ratio_limit: float = CHAN_ASPECT_RATIO_LIMIT,
    chan_error_estimate_limit: float = CHAN_ERROR_ESTIMATE_LIMIT,
    condition_number_limit: float = 1e8,
    hbr_miss_ratio_limit: float = _OFFSET_CUTOFF,
    method: str = "alfano",
) -> PcUsageReport:
    """Check the 2D Pc assumptions for one encounter and name every violation.

    Modelled on CARA PcMultiStep.m and Utils/UsageViolationPc2D.m, which run
    the 2D Foster Pc, test the assumptions behind it, and escalate to a 3D
    method when they fail. This function does the testing part: it returns
    every violated assumption with a code, a one-line message and the numbers
    that triggered it, so a caller can decide whether the 2D Pc is reportable.

    Codes raised
    ------------
    LOW_RELATIVE_VELOCITY
        Relative speed below `low_relative_speed_km_s`. The rectilinear
        relative motion the 2D method assumes stops holding here first.
    EXTENDED_ENCOUNTER
        The covariance takes longer than `duration_fraction_limit` of an orbit
        to traverse at the relative speed (Pc2DExtendedCutoff in CARA, 2e-2).
        The encounter is no longer short compared with the orbital dynamics.
    NPD_COVARIANCE
        The combined covariance is not positive definite; Pc computed from it
        went through eigenvalue remediation.
    ILL_CONDITIONED_COVARIANCE
        Eigenvalue ratio of the 3D covariance above `condition_number_limit`.
    EXTREME_ASPECT_RATIO
        Encounter-plane sigma ratio above `aspect_ratio_limit`; quadrature and
        series methods both lose accuracy on a needle-shaped distribution.
    CHAN_ASPECT_RATIO
        The Chan equivalent-area reduction is not trustworthy for this
        geometry: either the encounter-plane sigma ratio is above
        `chan_aspect_ratio_limit` (measured worst error against CARA Pc2D is
        3.2e-2 below a ratio of 10 and 9.9e-1 above it) or the second-order
        error estimate of the reduction is above `chan_error_estimate_limit`.
        Only probability_chan is affected; Foster and Alfano stay good.
    HBR_COMPARABLE_TO_MISS
        Hard-body radius above `hbr_miss_ratio_limit` times the miss distance
        (Pc2DOffsetCutoff in CARA, 1e-2), where the miss vector no longer sits
        well outside the combined object.
    PC_DILUTION
        Scaling the covariance up raises Pc (see dilution_max_pc). The
        reported Pc is small because the uncertainty is large.
    """
    miss = np.asarray(miss_vector_km, dtype=float).reshape(3)
    rel_v = np.asarray(rel_velocity_km_s, dtype=float).reshape(3)
    cov_3d = np.asarray(cov_combined_eci_km2, dtype=float).reshape(3, 3)
    hbr = float(hard_body_radius_km)
    warnings: list[PcUsageWarning] = []

    miss_distance = float(np.linalg.norm(miss))
    rel_speed = float(np.linalg.norm(rel_v))

    remediated_3d, was_remediated_3d = remediate_covariance(cov_3d, hbr)
    condition = covariance_condition_number(cov_3d)
    positive_definite = not was_remediated_3d

    if not positive_definite:
        eigvals = np.linalg.eigvalsh(0.5 * (cov_3d + cov_3d.T))
        warnings.append(
            PcUsageWarning(
                code="NPD_COVARIANCE",
                message="combined covariance is not positive definite; eigenvalues were clipped",
                values={"min_eigenvalue_km2": float(eigvals.min())},
            )
        )
    elif condition > condition_number_limit:
        warnings.append(
            PcUsageWarning(
                code="ILL_CONDITIONED_COVARIANCE",
                message="combined covariance is badly conditioned for a 2D projection",
                values={"condition_number": condition, "limit": condition_number_limit},
            )
        )

    if rel_speed < low_relative_speed_km_s:
        warnings.append(
            PcUsageWarning(
                code="LOW_RELATIVE_VELOCITY",
                message="relative speed is too low for the rectilinear encounter assumption",
                values={"relative_speed_km_s": rel_speed, "limit_km_s": low_relative_speed_km_s},
            )
        )

    # Encounter duration: time to cross +/-3 sigma of the combined covariance
    # measured along the relative-velocity direction, at the relative speed.
    if rel_speed > 0.0:
        unit_v = rel_v / rel_speed
        sigma_along = math.sqrt(max(float(unit_v @ remediated_3d @ unit_v), 0.0))
        duration = 6.0 * sigma_along / rel_speed
    else:
        duration = float("inf")
    period = orbital_period_s if orbital_period_s > 0.0 else _NOMINAL_PERIOD_S
    duration_fraction = duration / period
    if duration_fraction > duration_fraction_limit:
        warnings.append(
            PcUsageWarning(
                code="EXTENDED_ENCOUNTER",
                message="encounter duration is long compared with the orbital period",
                values={
                    "encounter_duration_s": duration,
                    "duration_fraction_of_period": duration_fraction,
                    "limit": duration_fraction_limit,
                },
            )
        )

    if rel_speed > 0.0:
        rot = _rotation_matrix_to_conjunction_plane(rel_v)
        miss_2d = (rot @ miss)[:2]
        cov_2d = project_covariance_to_encounter_plane(remediated_3d, rel_v)
    else:
        miss_2d = miss[:2]
        cov_2d = remediated_3d[:2, :2]
    cov_2d, was_remediated_2d = remediate_covariance(cov_2d, hbr)
    _, _, sx, sy = _principal_frame(miss_2d, cov_2d)
    aspect_ratio = max(sx, sy) / min(sx, sy)
    if aspect_ratio > aspect_ratio_limit:
        warnings.append(
            PcUsageWarning(
                code="EXTREME_ASPECT_RATIO",
                message="encounter-plane covariance is extremely elongated",
                values={"aspect_ratio": aspect_ratio, "limit": aspect_ratio_limit},
            )
        )
    chan_error = chan_reduction_error_estimate(miss_2d, cov_2d, hbr)
    if aspect_ratio > chan_aspect_ratio_limit or chan_error > chan_error_estimate_limit:
        warnings.append(
            PcUsageWarning(
                code="CHAN_ASPECT_RATIO",
                message=(
                    "encounter plane is too elongated for the Chan equivalent-area "
                    "reduction; report the Foster or Alfano value instead"
                ),
                values={
                    "aspect_ratio": aspect_ratio,
                    "aspect_ratio_limit": chan_aspect_ratio_limit,
                    "chan_error_estimate": chan_error,
                    "chan_error_estimate_limit": chan_error_estimate_limit,
                },
            )
        )

    hbr_ratio = float("inf") if miss_distance <= 0.0 else hbr / miss_distance
    if hbr_ratio > hbr_miss_ratio_limit:
        warnings.append(
            PcUsageWarning(
                code="HBR_COMPARABLE_TO_MISS",
                message="hard-body radius is not small compared with the miss distance",
                values={"hbr_over_miss_distance": hbr_ratio, "limit": hbr_miss_ratio_limit},
            )
        )

    scan = dilution_max_pc(miss_2d, cov_2d, hbr, method=method)
    if scan.diluted:
        warnings.append(
            PcUsageWarning(
                code="PC_DILUTION",
                message="Pc would rise if the covariance grew; this is the dilution region",
                values={
                    "pc": scan.pc_actual,
                    "max_pc": scan.max_pc,
                    "scale_at_max": scan.scale_at_max,
                },
            )
        )

    return PcUsageReport(
        probability=scan.pc_actual,
        miss_distance_km=miss_distance,
        relative_speed_km_s=rel_speed,
        encounter_duration_s=duration,
        duration_fraction_of_period=duration_fraction,
        condition_number=condition,
        aspect_ratio=aspect_ratio,
        chan_error_estimate=chan_error,
        positive_definite=positive_definite and not was_remediated_2d,
        hbr_over_miss_distance=hbr_ratio,
        max_pc_dilution=scan.max_pc,
        dilution_scale=scan.scale_at_max,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Covariance realism: regime defaults and a Hejduk-style scale factor
# ---------------------------------------------------------------------------

# Base 1-sigma RTN position uncertainty (km) for a fresh TLE-derived state, by
# orbit regime. Structure adapted from satguard covariance/realism.py (MIT);
# the values here are anisotropic (along-track dominant, as TLE error is) and
# are scaled with TLE age by the same law as sigma_from_tle_age.
REGIME_BASE_SIGMA_RTN_KM: dict[str, tuple[float, float, float]] = {
    "LEO": (0.05, 0.15, 0.05),
    "MEO": (0.10, 0.30, 0.10),
    "GEO": (0.20, 0.60, 0.20),
    "HEO": (0.25, 0.75, 0.25),
}

_GEO_ALTITUDE_KM = 35786.0
_GEO_ALTITUDE_TOLERANCE_KM = 500.0
_LEO_ALTITUDE_KM = 2000.0
_HEO_ECCENTRICITY = 0.25


def classify_orbit_regime(semi_major_axis_km: float, eccentricity: float = 0.0) -> str:
    """Classify an orbit as LEO, MEO, GEO or HEO from its size and shape."""
    if eccentricity >= _HEO_ECCENTRICITY:
        return "HEO"
    altitude = float(semi_major_axis_km) - EARTH_RADIUS_KM
    if abs(altitude - _GEO_ALTITUDE_KM) <= _GEO_ALTITUDE_TOLERANCE_KM:
        return "GEO"
    if altitude < _LEO_ALTITUDE_KM:
        return "LEO"
    return "MEO"


def regime_default_covariance_rtn(regime: str, tle_age_hours: float = 0.0) -> np.ndarray:
    """Default 3x3 RTN position covariance (km^2) for an orbit regime.

    Adapted from satguard covariance/realism.py (MIT), extended with a HEO
    regime and with TLE-age growth: the base sigmas are multiplied by the same
    age factor sigma_from_tle_age() applies, so a covariance defaulted here
    ages consistently with the isotropic path. Use only when no real
    covariance is available, and say so in whatever you report.
    """
    key = regime.upper()
    if key not in REGIME_BASE_SIGMA_RTN_KM:
        known = sorted(REGIME_BASE_SIGMA_RTN_KM)
        raise ValueError(f"unknown regime '{regime}'; expected one of {known}")
    growth = sigma_from_tle_age(tle_age_hours) / DEFAULT_POSITION_SIGMA_KM
    sigmas = np.array(REGIME_BASE_SIGMA_RTN_KM[key], dtype=float) * growth
    return np.diag(sigmas**2)


def _regularized_gamma_p(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x), series / continued fraction."""
    if a <= 0.0:
        raise ValueError("a must be positive")
    if x <= 0.0:
        return 0.0
    log_prefactor = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1.0:
        term = 1.0 / a
        total = term
        ap = a
        for _ in range(1000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return min(1.0, total * math.exp(log_prefactor))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return max(0.0, 1.0 - math.exp(log_prefactor) * h)


def chi_square_cdf(x: float, dof: int) -> float:
    """Chi-square CDF for integer degrees of freedom."""
    if dof < 1:
        raise ValueError("dof must be at least 1")
    return _regularized_gamma_p(0.5 * dof, 0.5 * max(x, 0.0))


def _chi_square_median(dof: int) -> float:
    """Median of the chi-square distribution, by bisection on the CDF."""
    lo, hi = 0.0, 10.0 * dof + 20.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if chi_square_cdf(mid, dof) < 0.5:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


@dataclass(frozen=True)
class CovarianceRealism:
    """Hejduk-style realism assessment of a set of Mahalanobis distances."""

    n_samples: int
    dof: int
    mean_mahalanobis_sq: float
    expected_mahalanobis_sq: float
    scale_factor: float
    sigma_scale_factor: float
    robust_scale_factor: float
    fraction_within_2_sigma: float
    expected_within_2_sigma: float
    realistic: bool


def covariance_realism_scale(
    mahalanobis_distances: list[float] | np.ndarray,
    dof: int = 3,
    realism_band: tuple[float, float] = (0.5, 2.0),
) -> CovarianceRealism:
    """Estimate the covariance scale factor implied by observed residuals.

    The Hejduk covariance realism test compares the observed distribution of
    squared Mahalanobis distances against the chi-square distribution it
    should follow when the covariance is truthful. Matching the first moment
    gives the scale factor to apply to the covariance: for k degrees of
    freedom E[d^2] = k, so scale = mean(d^2) / k, and a scale above one means
    the covariance is optimistic (too small) by that factor in variance,
    sqrt(scale) in sigma. The median-based estimate is reported alongside
    because a single outlier moves the mean and not the median.

    `realism_band` is the variance-scale band inside which the covariance is
    declared realistic; the default (0.5, 2.0) is one factor of sqrt(2) either
    side in sigma.
    """
    values = np.asarray(list(mahalanobis_distances), dtype=float)
    if values.size == 0:
        raise ValueError("need at least one Mahalanobis distance")
    if np.any(values < 0.0):
        raise ValueError("Mahalanobis distances must be non-negative")
    squared = values**2
    mean_sq = float(np.mean(squared))
    scale = mean_sq / dof
    median_sq = float(np.median(squared))
    robust = median_sq / _chi_square_median(dof)
    # Fraction inside the 2-sigma ellipsoid, against the chi-square prediction.
    within = float(np.mean(squared <= 4.0))
    expected_within = chi_square_cdf(4.0, dof)
    return CovarianceRealism(
        n_samples=int(values.size),
        dof=dof,
        mean_mahalanobis_sq=mean_sq,
        expected_mahalanobis_sq=float(dof),
        scale_factor=scale,
        sigma_scale_factor=math.sqrt(max(scale, 0.0)),
        robust_scale_factor=robust,
        fraction_within_2_sigma=within,
        expected_within_2_sigma=expected_within,
        realistic=realism_band[0] <= scale <= realism_band[1],
    )
