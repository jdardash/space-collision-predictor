"""NASA Standard Breakup Model (EVOLVE 4.0) for collisions and explosions.

Generates a debris cloud from a parent object: fragment counts, characteristic
length distribution, area-to-mass ratio, area, mass, and an isotropic delta-V
applied to the parent state vector. All states are Earth-Centered Inertial
(TEME from SGP4), km and km/s.

The model is the one published for EVOLVE 4.0 and reused by NASA ORDEM, DAS and
the ESA MASTER cross-checks. Every formula below is quoted in the docstring of
the function that implements it.

References
----------
.. [1] N. L. Johnson, P. H. Krisko, J.-C. Liou, P. D. Anz-Meador,
       "NASA's new breakup model of EVOLVE 4.0", Advances in Space Research
       28(9), 1377-1384, 2001.
.. [2] P. H. Krisko, "Proper Implementation of the 1998 NASA Breakup Model",
       Orbital Debris Quarterly News 15(4), 4-5, 2011.
.. [3] H. Klinkrad, "Space Debris: Models and Risk Analysis", Springer 2006,
       ch. 3 (collision and explosion fragmentation).
.. [4] NASA CARA Analysis Tools, CollisionConsequence: fragment counts above
       1 cm and 10 cm as a function of the two masses and the relative speed.
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
from pydantic import BaseModel, Field

from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM

# Specific energy at or above which a collision is catastrophic (J/g).
# Reference [1]: a collision is catastrophic when the ratio of the projectile
# kinetic energy to the target mass exceeds 40 J/g.
CATASTROPHIC_THRESHOLD_J_PER_G = 40.0

# Power-law exponents of the cumulative number distribution N(>Lc).
COLLISION_EXPONENT = 1.71
EXPLOSION_EXPONENT = 1.6

# Characteristic length regime boundaries for the A/M distribution (metres).
SMALL_FRAGMENT_LC_M = 0.08
LARGE_FRAGMENT_LC_M = 0.11

# Area-from-Lc break point (metres), reference [1] area law.
AREA_BREAK_LC_M = 0.00167

# Standard deviation of log10(delta-V) for both event types (reference [1]).
DELTA_V_SIGMA = 0.4

EventType = str  # "collision" or "explosion"
ParentType = str  # "spacecraft" or "rocket_body"


class Fragment(BaseModel):
    """One sampled debris fragment with its post-breakup ECI state."""

    index: int
    characteristic_length_m: float
    area_m2: float
    area_to_mass_m2_kg: float
    mass_kg: float
    delta_v_m_s: float
    delta_v_vector_km_s: tuple[float, float, float]
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]
    representative_count: float = Field(
        default=1.0,
        description="Real fragments this sample stands for when the cloud is capped.",
    )


class GabbardPoint(BaseModel):
    """One point of a Gabbard diagram: apogee and perigee altitude vs period."""

    index: int
    period_min: float
    apogee_km: float
    perigee_km: float
    semi_major_axis_km: float
    eccentricity: float
    bound: bool = True


class BreakupCloud(BaseModel):
    """Result of a breakup simulation."""

    event_type: EventType
    parent_type: ParentType
    epoch: datetime
    catastrophic: bool
    specific_energy_j_per_g: float
    effective_mass_kg: float
    min_characteristic_length_m: float
    total_fragments_modelled: float
    fragments_sampled: int
    representative_count: float
    n_above_1cm: float
    n_above_10cm: float
    seed: int
    fragments: list[Fragment]


class CollisionConsequence(BaseModel):
    """Fragment-count consequence summary for a hypothetical collision.

    Mirrors the NASA CARA CollisionConsequence product: the consequence of a
    conjunction is reported as debris counts, so a low-Pc event between two
    massive objects is still visible as a large potential debris source.
    """

    mass_primary_kg: float
    mass_secondary_kg: float
    relative_speed_km_s: float
    target_mass_kg: float
    projectile_mass_kg: float
    specific_energy_j_per_g: float
    catastrophic: bool
    effective_mass_kg: float
    n_above_1cm: float
    n_above_10cm: float
    n_above_1m: float


# --------------------------------------------------------------------------
# Fragment count laws
# --------------------------------------------------------------------------


def specific_energy_j_per_g(
    projectile_mass_kg: float,
    target_mass_kg: float,
    relative_speed_km_s: float,
) -> float:
    """Projectile kinetic energy per unit target mass, in J/g.

    E_p = (1/2 * m_projectile * v_rel^2) / m_target, with v_rel in m/s, the
    masses in kg and the target mass converted to grams. Reference [1].
    """
    if target_mass_kg <= 0.0:
        raise ValueError("target_mass_kg must be positive")
    v_m_s = relative_speed_km_s * 1000.0
    energy_j = 0.5 * projectile_mass_kg * v_m_s * v_m_s
    return energy_j / (target_mass_kg * 1000.0)


def is_catastrophic(
    projectile_mass_kg: float,
    target_mass_kg: float,
    relative_speed_km_s: float,
) -> bool:
    """True when the specific energy reaches the 40 J/g catastrophic threshold."""
    energy = specific_energy_j_per_g(projectile_mass_kg, target_mass_kg, relative_speed_km_s)
    return energy >= CATASTROPHIC_THRESHOLD_J_PER_G


def collision_effective_mass_kg(
    mass_a_kg: float,
    mass_b_kg: float,
    relative_speed_km_s: float,
) -> tuple[float, bool]:
    """Effective mass M for the collision number law, plus the catastrophic flag.

    References [1] and [2]: for a catastrophic collision M is the total mass of
    the two objects. For a non-catastrophic collision only the projectile takes
    part and the model uses M = m_projectile * v_rel^2 with v_rel in km/s, a
    documented dimensional convention of the model rather than a physical mass.
    """
    target = max(mass_a_kg, mass_b_kg)
    projectile = min(mass_a_kg, mass_b_kg)
    catastrophic = is_catastrophic(projectile, target, relative_speed_km_s)
    if catastrophic:
        return mass_a_kg + mass_b_kg, True
    return projectile * relative_speed_km_s**2, False


def fragment_count_collision(effective_mass_kg: float, lc_m: float) -> float:
    """N(>Lc) = 0.1 * M^0.75 * Lc^-1.71 for a collision. Reference [1].

    M is the effective mass in kg from collision_effective_mass_kg() and Lc the
    characteristic length cutoff in metres.
    """
    if lc_m <= 0.0:
        raise ValueError("lc_m must be positive")
    if effective_mass_kg <= 0.0:
        return 0.0
    return float(0.1 * effective_mass_kg**0.75 * lc_m**-COLLISION_EXPONENT)


def fragment_count_explosion(lc_m: float, scaling_factor: float = 1.0) -> float:
    """N(>Lc) = 6 * S * Lc^-1.6 for an explosion. Reference [1].

    S is the dimensionless scaling factor (1.0 for a standard upper stage);
    Lc is the characteristic length cutoff in metres.
    """
    if lc_m <= 0.0:
        raise ValueError("lc_m must be positive")
    return float(6.0 * scaling_factor * lc_m**-EXPLOSION_EXPONENT)


def collision_consequence(
    mass_primary_kg: float,
    mass_secondary_kg: float,
    relative_speed_km_s: float,
) -> CollisionConsequence:
    """Fragment counts above 1 cm, 10 cm and 1 m for a hypothetical collision.

    Design follows the NASA CARA CollisionConsequence tool (reference [4]).
    """
    target = max(mass_primary_kg, mass_secondary_kg)
    projectile = min(mass_primary_kg, mass_secondary_kg)
    energy = specific_energy_j_per_g(projectile, target, relative_speed_km_s)
    effective_mass, catastrophic = collision_effective_mass_kg(
        mass_primary_kg, mass_secondary_kg, relative_speed_km_s
    )
    return CollisionConsequence(
        mass_primary_kg=mass_primary_kg,
        mass_secondary_kg=mass_secondary_kg,
        relative_speed_km_s=relative_speed_km_s,
        target_mass_kg=target,
        projectile_mass_kg=projectile,
        specific_energy_j_per_g=energy,
        catastrophic=catastrophic,
        effective_mass_kg=effective_mass,
        n_above_1cm=fragment_count_collision(effective_mass, 0.01),
        n_above_10cm=fragment_count_collision(effective_mass, 0.10),
        n_above_1m=fragment_count_collision(effective_mass, 1.0),
    )


# --------------------------------------------------------------------------
# Size distribution
# --------------------------------------------------------------------------


def sample_characteristic_lengths(
    rng: np.random.Generator,
    count: int,
    lc_min_m: float,
    lc_max_m: float,
    exponent: float,
) -> np.ndarray:
    """Draw Lc from the model power law by inverse-CDF sampling.

    The model gives the cumulative count N(>L) = k * L^-a. The number of
    fragments in [Lmin, Lmax] is proportional to Lmin^-a - Lmax^-a, so the CDF
    restricted to that interval is

        F(L) = (Lmin^-a - L^-a) / (Lmin^-a - Lmax^-a)

    which inverts in closed form to

        L(u) = [Lmin^-a - u * (Lmin^-a - Lmax^-a)] ^ (-1/a),  u ~ U(0, 1)

    No rejection sampling is needed and the draw is exactly the model's size
    spectrum truncated at the cutoff.
    """
    if lc_max_m <= lc_min_m:
        raise ValueError("lc_max_m must exceed lc_min_m")
    low = lc_min_m**-exponent
    high = lc_max_m**-exponent
    u = rng.random(count)
    return np.asarray((low - u * (low - high)) ** (-1.0 / exponent))


# --------------------------------------------------------------------------
# Area-to-mass distribution (bimodal log-normal in log10(A/M))
# --------------------------------------------------------------------------


def _sc_params(lam: float) -> tuple[float, float, float, float, float]:
    """Spacecraft A/M parameters (alpha, mu1, sigma1, mu2, sigma2) at lam=log10(Lc).

    Piecewise-linear coefficient set of reference [1] for the spacecraft
    variant of the Lc > 11 cm bimodal distribution.
    """
    if lam <= -1.95:
        alpha = 0.0
    elif lam >= 0.55:
        alpha = 1.0
    else:
        alpha = 0.3 + 0.4 * (lam + 1.2)

    if lam <= -1.1:
        mu1 = -0.6
    elif lam >= 0.0:
        mu1 = -0.95
    else:
        mu1 = -0.6 - 0.318 * (lam + 1.1)

    if lam <= -1.3:
        sigma1 = 0.1
    elif lam >= -0.3:
        sigma1 = 0.3
    else:
        sigma1 = 0.1 + 0.2 * (lam + 1.3)

    if lam <= -0.7:
        mu2 = -1.2
    elif lam >= -0.1:
        mu2 = -2.0
    else:
        mu2 = -1.2 - 1.333 * (lam + 0.7)

    if lam <= -0.5:
        sigma2 = 0.5
    elif lam >= -0.3:
        sigma2 = 0.3
    else:
        sigma2 = 0.5 - (lam + 0.5)

    return alpha, mu1, sigma1, mu2, sigma2


def _rb_params(lam: float) -> tuple[float, float, float, float, float]:
    """Rocket-body A/M parameters (alpha, mu1, sigma1, mu2, sigma2) at lam=log10(Lc).

    Piecewise-linear coefficient set of reference [1] for the rocket-body
    variant of the Lc > 11 cm bimodal distribution.
    """
    if lam <= -1.4:
        alpha = 1.0
    elif lam >= 0.0:
        alpha = 0.5
    else:
        alpha = 1.0 - 0.3 * (lam + 1.4)

    if lam <= -0.5:
        mu1 = -0.45
    elif lam >= 0.0:
        mu1 = -0.9
    else:
        mu1 = -0.45 - 0.9 * (lam + 0.5)

    sigma1 = 0.55
    mu2 = -0.9

    if lam <= -1.0:
        sigma2 = 0.28
    elif lam >= 0.1:
        sigma2 = 0.1
    else:
        sigma2 = 0.28 - 0.1636 * (lam + 1.0)

    return alpha, mu1, sigma1, mu2, sigma2


def _soc_params(lam: float) -> tuple[float, float]:
    """Small-object (Lc < 8 cm) A/M parameters (mu, sigma), a single normal mode.

    Reference [1]: below 8 cm the spacecraft and rocket-body distributions
    collapse onto one normal distribution in log10(A/M).
    """
    if lam <= -1.75:
        mu = -0.3
    elif lam >= -1.25:
        mu = -1.0
    else:
        mu = -0.3 - 1.4 * (lam + 1.75)

    sigma = 0.2 if lam <= -3.5 else 0.2 + 0.1333 * (lam + 3.5)
    return mu, sigma


def sample_area_to_mass(
    rng: np.random.Generator,
    lc_m: float,
    parent_type: ParentType = "spacecraft",
) -> float:
    """Draw A/M in m^2/kg for one fragment of characteristic length lc_m.

    Reference [1]: log10(A/M) is bimodal normal for Lc > 11 cm (with separate
    spacecraft and rocket-body coefficient sets) and a single normal for
    Lc < 8 cm. Between 8 and 11 cm the model bridges the two linearly; the
    bridge is applied here as the probability of drawing from the large-object
    distribution, which reproduces the mixed population in that band.
    """
    lam = math.log10(lc_m)
    use_large = lc_m >= LARGE_FRAGMENT_LC_M
    if SMALL_FRAGMENT_LC_M <= lc_m < LARGE_FRAGMENT_LC_M:
        weight = (lc_m - SMALL_FRAGMENT_LC_M) / (LARGE_FRAGMENT_LC_M - SMALL_FRAGMENT_LC_M)
        use_large = bool(rng.random() < weight)

    if use_large:
        if parent_type == "rocket_body":
            alpha, mu1, sigma1, mu2, sigma2 = _rb_params(lam)
        else:
            alpha, mu1, sigma1, mu2, sigma2 = _sc_params(lam)
        chi = rng.normal(mu1, sigma1) if rng.random() < alpha else rng.normal(mu2, sigma2)
    else:
        mu, sigma = _soc_params(lam)
        chi = rng.normal(mu, sigma)

    return float(10.0**chi)


def area_from_characteristic_length(lc_m: float) -> float:
    """Cross-sectional area in m^2 from characteristic length in m.

    Reference [1]:
        A = 0.540424 * Lc^2                for Lc <  0.00167 m
        A = 0.556945 * Lc^2.0047077        for Lc >= 0.00167 m
    """
    if lc_m < AREA_BREAK_LC_M:
        return float(0.540424 * lc_m**2)
    return float(0.556945 * lc_m**2.0047077)


def mass_from_area(area_m2: float, area_to_mass_m2_kg: float) -> float:
    """Fragment mass in kg: m = A / (A/M), with A in m^2 and A/M in m^2/kg."""
    if area_to_mass_m2_kg <= 0.0:
        raise ValueError("area_to_mass_m2_kg must be positive")
    return area_m2 / area_to_mass_m2_kg


# --------------------------------------------------------------------------
# Delta-V distribution
# --------------------------------------------------------------------------


def delta_v_log_mean(area_to_mass_m2_kg: float, event_type: EventType) -> float:
    """Mean of log10(delta-V in m/s) for a given A/M. Reference [1].

        collisions: mu = 0.9 * log10(A/M) + 2.9
        explosions: mu = 0.2 * log10(A/M) + 1.85

    with sigma = 0.4 in both cases.
    """
    chi = math.log10(area_to_mass_m2_kg)
    if event_type == "explosion":
        return 0.2 * chi + 1.85
    return 0.9 * chi + 2.9


def sample_delta_v_m_s(
    rng: np.random.Generator,
    area_to_mass_m2_kg: float,
    event_type: EventType,
) -> float:
    """Draw a delta-V magnitude in m/s from the log-normal ejecta distribution."""
    mu = delta_v_log_mean(area_to_mass_m2_kg, event_type)
    return float(10.0 ** rng.normal(mu, DELTA_V_SIGMA))


def _isotropic_unit_vectors(rng: np.random.Generator, count: int) -> np.ndarray:
    """Uniform directions on the unit sphere, shape (count, 3).

    cos(theta) uniform on [-1, 1] with the azimuth uniform on [0, 2*pi) is an
    area-uniform draw, which is what the model's isotropic ejection assumes.
    """
    cos_theta = rng.uniform(-1.0, 1.0, count)
    sin_theta = np.sqrt(np.maximum(0.0, 1.0 - cos_theta**2))
    phi = rng.uniform(0.0, 2.0 * math.pi, count)
    return np.column_stack((sin_theta * np.cos(phi), sin_theta * np.sin(phi), cos_theta))


# --------------------------------------------------------------------------
# Cloud generation
# --------------------------------------------------------------------------


def simulate_breakup(
    position_km: tuple[float, float, float],
    velocity_km_s: tuple[float, float, float],
    epoch: datetime,
    event_type: EventType = "collision",
    parent_mass_kg: float = 1000.0,
    projectile_mass_kg: float | None = None,
    relative_speed_km_s: float = 10.0,
    parent_type: ParentType = "spacecraft",
    min_characteristic_length_m: float = 0.01,
    max_characteristic_length_m: float = 1.0,
    max_fragments: int = 500,
    explosion_scaling_factor: float = 1.0,
    seed: int = 0,
) -> BreakupCloud:
    """Generate a seeded NASA Standard Breakup Model fragment cloud.

    The cloud is created at the parent's ECI position; every fragment keeps that
    position and takes the parent velocity plus an isotropic delta-V drawn from
    the model's log-normal ejecta distribution.

    The total fragment count from the model is often tens of thousands, so the
    sample is capped at max_fragments and each sample carries a
    representative_count = N_total / n_sampled weight. Because the size spectrum
    is sampled by exact inverse CDF (see sample_characteristic_lengths), the
    capped sample is an unbiased draw from the full spectrum rather than a
    truncation of it.

    Parameters
    ----------
    parent_mass_kg : mass of the target or exploding object (kg).
    projectile_mass_kg : impactor mass for a collision; defaults to 1% of the
        parent mass, a typical small-debris impactor.
    relative_speed_km_s : impact speed for a collision (km/s).
    seed : any int; the same seed reproduces the cloud exactly.
    """
    rng = np.random.default_rng(seed)
    exponent = EXPLOSION_EXPONENT if event_type == "explosion" else COLLISION_EXPONENT

    if event_type == "explosion":
        catastrophic = False
        energy = 0.0
        effective_mass = parent_mass_kg
        n_total = fragment_count_explosion(min_characteristic_length_m, explosion_scaling_factor)

        def counter(lc: float) -> float:
            return fragment_count_explosion(lc, explosion_scaling_factor)
    else:
        m_p = projectile_mass_kg if projectile_mass_kg is not None else 0.01 * parent_mass_kg
        energy = specific_energy_j_per_g(m_p, parent_mass_kg, relative_speed_km_s)
        effective_mass, catastrophic = collision_effective_mass_kg(
            parent_mass_kg, m_p, relative_speed_km_s
        )
        n_total = fragment_count_collision(effective_mass, min_characteristic_length_m)

        def counter(lc: float) -> float:
            return fragment_count_collision(effective_mass, lc)

    n_sampled = int(min(max(1, round(n_total)), max_fragments))
    representative = n_total / n_sampled if n_sampled else 0.0

    lengths = sample_characteristic_lengths(
        rng, n_sampled, min_characteristic_length_m, max_characteristic_length_m, exponent
    )
    lengths = np.sort(lengths)[::-1]
    directions = _isotropic_unit_vectors(rng, n_sampled)

    r_parent = np.asarray(position_km, dtype=float)
    v_parent = np.asarray(velocity_km_s, dtype=float)
    r_tuple = (float(r_parent[0]), float(r_parent[1]), float(r_parent[2]))

    fragments: list[Fragment] = []
    for i in range(n_sampled):
        lc = float(lengths[i])
        am = sample_area_to_mass(rng, lc, parent_type)
        area = area_from_characteristic_length(lc)
        mass = mass_from_area(area, am)
        dv_m_s = sample_delta_v_m_s(rng, am, event_type)
        dv_vec = directions[i] * (dv_m_s / 1000.0)
        v_frag = v_parent + dv_vec
        fragments.append(
            Fragment(
                index=i,
                characteristic_length_m=lc,
                area_m2=area,
                area_to_mass_m2_kg=am,
                mass_kg=mass,
                delta_v_m_s=dv_m_s,
                delta_v_vector_km_s=(float(dv_vec[0]), float(dv_vec[1]), float(dv_vec[2])),
                position_km=r_tuple,
                velocity_km_s=(float(v_frag[0]), float(v_frag[1]), float(v_frag[2])),
                representative_count=representative,
            )
        )

    return BreakupCloud(
        event_type=event_type,
        parent_type=parent_type,
        epoch=epoch,
        catastrophic=catastrophic,
        specific_energy_j_per_g=energy,
        effective_mass_kg=effective_mass,
        min_characteristic_length_m=min_characteristic_length_m,
        total_fragments_modelled=n_total,
        fragments_sampled=n_sampled,
        representative_count=representative,
        n_above_1cm=counter(0.01),
        n_above_10cm=counter(0.10),
        seed=seed,
        fragments=fragments,
    )


# --------------------------------------------------------------------------
# Gabbard diagram
# --------------------------------------------------------------------------


def osculating_apsides(
    position_km: tuple[float, float, float] | np.ndarray,
    velocity_km_s: tuple[float, float, float] | np.ndarray,
) -> tuple[float, float, float, float]:
    """Two-body apogee/perigee altitude, semi-major axis and eccentricity.

    Returns (apogee_km, perigee_km, semi_major_axis_km, eccentricity) with the
    altitudes measured from the WGS72 equatorial radius. A non-positive
    semi-major axis (escape trajectory) is returned unchanged so the caller can
    flag it.
    """
    r_vec = np.asarray(position_km, dtype=float)
    v_vec = np.asarray(velocity_km_s, dtype=float)
    r = float(np.linalg.norm(r_vec))
    v2 = float(np.dot(v_vec, v_vec))
    mu = EARTH_MU_KM3_S2

    energy = v2 / 2.0 - mu / r
    if abs(energy) < 1e-12:
        return 0.0, 0.0, 0.0, 1.0
    a = -mu / (2.0 * energy)

    e_vec = ((v2 - mu / r) * r_vec - float(np.dot(r_vec, v_vec)) * v_vec) / mu
    e = float(np.linalg.norm(e_vec))

    if a <= 0.0:
        return 0.0, a * (1.0 - e) - EARTH_RADIUS_KM, a, e
    return a * (1.0 + e) - EARTH_RADIUS_KM, a * (1.0 - e) - EARTH_RADIUS_KM, a, e


def orbital_period_min(semi_major_axis_km: float) -> float:
    """Keplerian period in minutes for a bound semi-major axis (0.0 if unbound)."""
    if semi_major_axis_km <= 0.0:
        return 0.0
    return 2.0 * math.pi * math.sqrt(semi_major_axis_km**3 / EARTH_MU_KM3_S2) / 60.0


def gabbard_data(cloud: BreakupCloud) -> list[GabbardPoint]:
    """Gabbard diagram points for a fragment cloud.

    A Gabbard diagram plots each fragment's apogee and perigee altitude against
    its orbital period; a breakup shows up as the characteristic X crossing at
    the parent's period. Unbound fragments are returned with bound=False and a
    zero period so a plot can drop them.
    """
    points: list[GabbardPoint] = []
    for frag in cloud.fragments:
        apogee, perigee, a, e = osculating_apsides(frag.position_km, frag.velocity_km_s)
        bound = a > 0.0 and e < 1.0
        points.append(
            GabbardPoint(
                index=frag.index,
                period_min=orbital_period_min(a) if bound else 0.0,
                apogee_km=apogee if bound else 0.0,
                perigee_km=perigee,
                semi_major_axis_km=a,
                eccentricity=e,
                bound=bound,
            )
        )
    return points


def cloud_summary(cloud: BreakupCloud) -> dict[str, float]:
    """Aggregate statistics for a fragment cloud (masses, sizes, delta-V)."""
    if not cloud.fragments:
        return {}
    masses = np.array([f.mass_kg for f in cloud.fragments])
    lengths = np.array([f.characteristic_length_m for f in cloud.fragments])
    dvs = np.array([f.delta_v_m_s for f in cloud.fragments])
    ams = np.array([f.area_to_mass_m2_kg for f in cloud.fragments])
    return {
        "total_sampled_mass_kg": float(masses.sum()),
        "represented_mass_kg": float(masses.sum() * cloud.representative_count),
        "median_mass_kg": float(np.median(masses)),
        "max_characteristic_length_m": float(lengths.max()),
        "min_characteristic_length_m": float(lengths.min()),
        "median_delta_v_m_s": float(np.median(dvs)),
        "max_delta_v_m_s": float(dvs.max()),
        "median_area_to_mass_m2_kg": float(np.median(ams)),
    }
