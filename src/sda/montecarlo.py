"""Monte Carlo miss distance uncertainty analysis.

Perturbs satellite positions at TCA by adding Gaussian noise scaled to
position uncertainty, producing a miss distance distribution to quantify
conjunction risk under uncertainty.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from dataclasses import dataclass

import numpy as np
from sgp4.api import Satrec, WGS72

from sda.models import TLERecord
from sda.propagator import datetime_to_jd


# Default position uncertainty (1-sigma) in km per axis
DEFAULT_POSITION_SIGMA_KM = 0.050  # 50 m — typical for LEO with fresh TLEs


@dataclass
class MonteCarloResult:
    """Results from Monte Carlo miss distance analysis."""
    mean_miss_km: float
    std_miss_km: float
    median_miss_km: float
    percentile_5_km: float
    percentile_95_km: float
    min_miss_km: float
    max_miss_km: float
    n_samples: int
    miss_distances: list[float]
    collision_probability_mc: float  # fraction of samples below hard-body radius


def run_monte_carlo(
    tle_primary: TLERecord,
    tle_secondary: TLERecord,
    tca: datetime,
    n_samples: int = 500,
    bstar_sigma_fraction: float = 0.1,
    position_sigma_km: float = DEFAULT_POSITION_SIGMA_KM,
    hard_body_radius_km: float = 0.020,
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
    miss_distances = []
    collisions = 0

    sat_p = Satrec.twoline2rv(tle_primary.line1, tle_primary.line2, WGS72)
    sat_s = Satrec.twoline2rv(tle_secondary.line1, tle_secondary.line2, WGS72)

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

    # Generate perturbed positions
    noise_p = np.random.normal(0, total_sigma, (n_samples, 3))
    noise_s = np.random.normal(0, total_sigma, (n_samples, 3))

    positions_p = pos_p_nom + noise_p
    positions_s = pos_s_nom + noise_s

    # Compute miss distances vectorized
    diffs = positions_p - positions_s
    distances = np.linalg.norm(diffs, axis=1)

    miss_distances = distances.tolist()
    collisions = int(np.sum(distances < hard_body_radius_km))

    arr = np.array(miss_distances)
    return MonteCarloResult(
        mean_miss_km=float(np.mean(arr)),
        std_miss_km=float(np.std(arr)),
        median_miss_km=float(np.median(arr)),
        percentile_5_km=float(np.percentile(arr, 5)),
        percentile_95_km=float(np.percentile(arr, 95)),
        min_miss_km=float(np.min(arr)),
        max_miss_km=float(np.max(arr)),
        n_samples=len(miss_distances),
        miss_distances=sorted(miss_distances),
        collision_probability_mc=collisions / len(miss_distances),
    )
