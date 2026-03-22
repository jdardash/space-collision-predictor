"""Tests for Monte Carlo miss distance analysis."""

from datetime import datetime, timezone

from sda.models import TLERecord
from sda.montecarlo import run_monte_carlo, MonteCarloResult


ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
)

CSS_TLE = TLERecord(
    norad_id=48274,
    name="CSS (TIANHE)",
    line1="1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991",
    line2="2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001",
    epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
)


def test_monte_carlo_returns_result():
    result = run_monte_carlo(
        ISS_TLE, CSS_TLE,
        tca=datetime(2024, 2, 14, 13, 0, 0, tzinfo=timezone.utc),
        n_samples=50,
    )
    assert isinstance(result, MonteCarloResult)
    assert result.n_samples > 0


def test_monte_carlo_statistics():
    result = run_monte_carlo(
        ISS_TLE, CSS_TLE,
        tca=datetime(2024, 2, 14, 13, 0, 0, tzinfo=timezone.utc),
        n_samples=50,
    )
    assert result.mean_miss_km > 0
    assert result.std_miss_km >= 0
    assert result.min_miss_km <= result.mean_miss_km
    assert result.max_miss_km >= result.mean_miss_km


def test_monte_carlo_sorted_distances():
    result = run_monte_carlo(
        ISS_TLE, CSS_TLE,
        tca=datetime(2024, 2, 14, 13, 0, 0, tzinfo=timezone.utc),
        n_samples=50,
    )
    # miss_distances should be sorted
    for i in range(len(result.miss_distances) - 1):
        assert result.miss_distances[i] <= result.miss_distances[i + 1]


def test_monte_carlo_percentiles():
    result = run_monte_carlo(
        ISS_TLE, CSS_TLE,
        tca=datetime(2024, 2, 14, 13, 0, 0, tzinfo=timezone.utc),
        n_samples=100,
    )
    assert result.percentile_5_km <= result.median_miss_km
    assert result.median_miss_km <= result.percentile_95_km


def test_monte_carlo_collision_probability():
    result = run_monte_carlo(
        ISS_TLE, CSS_TLE,
        tca=datetime(2024, 2, 14, 13, 0, 0, tzinfo=timezone.utc),
        n_samples=50,
    )
    assert 0.0 <= result.collision_probability_mc <= 1.0
