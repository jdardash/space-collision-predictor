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
       4th Ed., Microcosm Press, 2013. Ch. 9 — TLE accuracy vs. age.
.. [4] M. Abramowitz & I. Stegun, *Handbook of Mathematical Functions*,
       Dover, 1965. §9.8.1-2: Polynomial approximations for I₀(x).
"""

from __future__ import annotations

import math

import numpy as np

from sda.constants import DEFAULT_COMBINED_RADIUS_KM, DEFAULT_POSITION_SIGMA_KM


def _rotation_matrix_to_conjunction_plane(
    rel_velocity: np.ndarray,
) -> np.ndarray:
    """Build rotation matrix whose Z-axis aligns with relative velocity.

    Returns a 3×3 matrix R such that R @ rel_velocity = [0, 0, |v|].
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
    v = miss_2d**2 / (2.0 * sigma**2)

    if v > 500:
        return 0.0  # negligible — miss distance >> sigma

    # Direct numerical radial-angular integration
    # For isotropic Gaussian with circular HBR:
    # Pc = 1 - Q_1(sqrt(2*v), sqrt(2*u)) where Q_1 is Marcum Q-function
    # Approximate via: Pc ≈ exp(-v) * (1 - exp(-u)) for u << 1
    # For better accuracy, use the full integral:
    pc_direct = 0.0
    dr = radius / max(n_steps, 1)
    for i in range(n_steps):
        r = (i + 0.5) * dr
        # Integrate in polar:
        # ∫₀²π ∫₀ʳ_hb (1/(2πσ²)) exp(-((r cosθ - d)² + (r sinθ)²)/(2σ²)) r dr dθ
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


# ---------------------------------------------------------------------------
# Full-covariance Pc methods (Foster quadrature, Chan series, Monte Carlo)
#
# These accept real 3x3 position covariances (e.g. from ingested CDMs) instead
# of the isotropic sigma used by compute_collision_probability(). Three
# mathematically independent computations are provided so results can be
# cross-validated (see sda.validation):
#
#   pc_foster()      — 2D numerical quadrature in the encounter plane
#                      (Foster & Estes 1992, as used by NASA CARA)
#   pc_chan()        — analytic series via equivalent-area transformation
#                      (Chan 2008); exact for isotropic covariance
#   pc_monte_carlo_2d() — direct sampling of the encounter-plane Gaussian
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
    v = (mx/sx)^2 + (my/sy)^2. Exact for isotropic covariance; an
    approximation for anisotropic covariance (error grows with aspect
    ratio — see sda.validation for measured bounds).

    Evaluated as Pc = sum_k P_u(k) * CDF_v(k-1) with P_x the Poisson(x/2)
    pmf — the summation order that keeps every term a positive product.
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
    miss_distance_km, mahalanobis_distance, hard_body_radius_km,
    encounter_covariance_km2 (2x2 nested list).

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
            "miss_distance_km": miss_distance,
            "mahalanobis_distance": float("inf"),
            "hard_body_radius_km": combined_radius_km,
            "encounter_covariance_km2": [[0.0, 0.0], [0.0, 0.0]],
        }

    cov_combined = np.asarray(cov_primary_eci_km2, dtype=float) + np.asarray(
        cov_secondary_eci_km2, dtype=float
    )
    rot = _rotation_matrix_to_conjunction_plane(rel_v)
    miss_2d = (rot @ miss)[:2]
    cov_2d = project_covariance_to_encounter_plane(cov_combined, rel_v)

    mx, my, sx, sy = _principal_frame(miss_2d, cov_2d)
    mahal = math.sqrt((mx / sx) ** 2 + (my / sy) ** 2)

    return {
        "probability": pc_foster(miss_2d, cov_2d, combined_radius_km),
        "probability_chan": pc_chan(miss_2d, cov_2d, combined_radius_km),
        "miss_distance_km": miss_distance,
        "mahalanobis_distance": mahal,
        "hard_body_radius_km": combined_radius_km,
        "encounter_covariance_km2": [[float(c) for c in row] for row in cov_2d],
    }


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
    if age_days <= 3.0:  # noqa: SIM108 — branch comments explain the growth regimes
        # sqrt growth: σ ≈ σ₀ * (1 + age_days)
        scale = 1.0 + age_days * 3.0
    else:
        # Linear growth beyond 3 days
        scale = 10.0 + (age_days - 3.0) * 5.0

    return base_sigma_km * scale
