"""Tests for atmospheric drag decay estimation."""

from datetime import datetime, timezone

from sda.models import TLERecord
from sda.decay import estimate_decay, DecayEstimate, _atmospheric_density


ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
)


def test_estimate_decay_returns_result():
    result = estimate_decay(ISS_TLE)
    assert isinstance(result, DecayEstimate)
    assert result.norad_id == 25544


def test_iss_altitude_reasonable():
    result = estimate_decay(ISS_TLE)
    # ISS orbits at ~400 km altitude
    assert 300 < result.altitude_km < 500


def test_iss_period_reasonable():
    result = estimate_decay(ISS_TLE)
    # ISS period ~92 minutes
    assert 85 < result.period_min < 100


def test_decay_rate_positive():
    result = estimate_decay(ISS_TLE)
    assert result.decay_rate_km_per_day > 0


def test_lifetime_positive():
    result = estimate_decay(ISS_TLE)
    assert result.estimated_lifetime_days > 0


def test_solar_max_shorter_lifetime():
    """Solar maximum → more drag → shorter lifetime."""
    min_result = estimate_decay(ISS_TLE, f107_solar_flux=70.0)
    max_result = estimate_decay(ISS_TLE, f107_solar_flux=250.0)
    assert max_result.estimated_lifetime_days < min_result.estimated_lifetime_days


def test_atmospheric_density_decreases_with_altitude():
    rho_200 = _atmospheric_density(200)
    rho_400 = _atmospheric_density(400)
    rho_800 = _atmospheric_density(800)
    assert rho_200 > rho_400 > rho_800


def test_atmospheric_density_zero_above_1200():
    assert _atmospheric_density(1500) == 0.0
