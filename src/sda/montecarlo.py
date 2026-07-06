"""Monte Carlo miss distance uncertainty analysis.

Perturbs satellite positions at TCA by adding Gaussian noise scaled to
position uncertainty, producing a miss distance distribution to quantify
conjunction risk under uncertainty.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from sda.constants import DEFAULT_COMBINED_RADIUS_KM, DEFAULT_POSITION_SIGMA_KM
from sda.models import MonteCarloResult, TLERecord
from sda.propagator import build_satrec, datetime_to_jd


def run_monte_carlo(
    tle_primary: TLERecord,
    tle_secondary: TLERecord,
    tca: datetime,
    n_samples: int = 500,
    bstar_sigma_fraction: float = 0.1,
    position_sigma_km: float = DEFAULT_POSITION_SIGMA_KM,
    hard_body_radius_km: float = DEFAULT_COMBINED_RADIUS_KM,
    seed: int | None = None,
) -> MonteCarloResult:
    """Run Monte Carlo analysis of miss distance at TCA.

    Propagates both objects to TCA using SGP4, then perturbs the resulting
    positions with Gaussian noise (scaled by position_sigma_km and
    bstar_sigma_fraction) to produce a miss distance distribution.

    Parameters
    ----------
    tle_primary : TLE of primary object
    tle_secondary : TLE of secondary object
    tca : time of closest approach
    n_samples : number of Monte Carlo samples
    bstar_sigma_fraction : scales additional position uncertainty from drag
    position_sigma_km : 1-sigma position uncertainty per axis, km
    hard_body_radius_km : combined hard-body radius for collision counting

    Returns
    -------
    MonteCarloResult with distribution statistics
    """
    jd, fr = datetime_to_jd(tca)
    collisions = 0

    sat_p = build_satrec(tle_primary)
    sat_s = build_satrec(tle_secondary)

    # Get nominal positions at TCA
    err_p, pos_p_nom, _ = sat_p.sgp4(jd, fr)
    err_s, pos_s_nom, _ = sat_s.sgp4(jd, fr)

    if err_p != 0 or err_s != 0:
        return MonteCarloResult(
            mean_miss_km=float("inf"),
            std_miss_km=0.0,
            median_miss_km=float("inf"),
            percentile_5_km=float("inf"),
            percentile_95_km=float("inf"),
            min_miss_km=float("inf"),
            max_miss_km=float("inf"),
            n_samples=0,
            miss_distances=[],
            collision_probability_mc=0.0,
        )

    pos_p_nom = np.array(pos_p_nom)
    pos_s_nom = np.array(pos_s_nom)

    # Total position sigma includes baseline + BSTAR-induced uncertainty
    # BSTAR uncertainty grows with propagation time; approximate as scaling factor
    total_sigma = position_sigma_km * (1.0 + bstar_sigma_fraction)

    # Generate perturbed positions (seeded RNG for reproducibility)
    rng = np.random.default_rng(seed)
    noise_p = rng.normal(0, total_sigma, (n_samples, 3))
    noise_s = rng.normal(0, total_sigma, (n_samples, 3))

    positions_p = pos_p_nom + noise_p
    positions_s = pos_s_nom + noise_s

    # Compute miss distances vectorized
    diffs = positions_p - positions_s
    distances = np.linalg.norm(diffs, axis=1)

    collisions = int(np.sum(distances < hard_body_radius_km))

    return MonteCarloResult(
        mean_miss_km=float(np.mean(distances)),
        std_miss_km=float(np.std(distances)),
        median_miss_km=float(np.median(distances)),
        percentile_5_km=float(np.percentile(distances, 5)),
        percentile_95_km=float(np.percentile(distances, 95)),
        min_miss_km=float(np.min(distances)),
        max_miss_km=float(np.max(distances)),
        n_samples=len(distances),
        miss_distances=sorted(distances.tolist()),
        collision_probability_mc=collisions / len(distances),
    )
