"""Extended decay estimation tests — edge cases and physics validation."""

from datetime import UTC, datetime

from sda.decay import _atmospheric_density, estimate_decay
from sda.models import TLERecord

ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, tzinfo=UTC),
)

# High-altitude satellite (GEO-transfer-like orbit)
HIGH_ALT_TLE = TLERecord(
    norad_id=99999,
    name="HIGH-ALT-TEST",
    line1="1 99999U 20001A   24045.50000000  .00000010  00000+0  10000-5 0  9991",
    line2="2 99999  28.5000 180.0000 7300000  90.0000 270.0000  2.00560000 10001",
    epoch=datetime(2024, 2, 14, tzinfo=UTC),
)


def test_iss_eccentricity_near_circular():
    """ISS orbit should be nearly circular."""
    result = estimate_decay(ISS_TLE)
    assert result.eccentricity < 0.01


def test_iss_bstar_positive():
    """ISS BSTAR should be positive (drag coefficient)."""
    result = estimate_decay(ISS_TLE)
    assert result.bstar > 0


def test_perigee_less_than_apogee():
    """Perigee altitude should always be <= apogee."""
    result = estimate_decay(ISS_TLE)
    assert result.perigee_km <= result.apogee_km


def test_decay_category_valid():
    """Lifetime category should be one of the defined values."""
    valid_categories = {"days", "weeks", "months", "years", "decades", "stable"}
    result = estimate_decay(ISS_TLE)
    assert result.estimated_lifetime_category in valid_categories


def test_reentry_risk_valid():
    """Reentry risk should be one of the defined values."""
    valid_risks = {"IMMINENT", "HIGH", "MODERATE", "LOW", "MINIMAL"}
    result = estimate_decay(ISS_TLE)
    assert result.reentry_risk in valid_risks


def test_atmospheric_density_monotonic():
    """Density should decrease monotonically with altitude (100-1000 km)."""
    prev = _atmospheric_density(100)
    for alt in range(150, 1050, 50):
        curr = _atmospheric_density(alt)
        assert curr < prev, f"Density not monotonic at {alt} km"
        prev = curr


def test_atmospheric_density_below_100():
    """Below 100 km should return sea-level-ish density."""
    rho = _atmospheric_density(50)
    assert rho > 1.0  # ~1.2 kg/m³ at sea level


def test_atmospheric_density_scale():
    """Density at 400 km should be on order of 1e-12 kg/m³."""
    rho = _atmospheric_density(400)
    assert 1e-13 < rho < 1e-10


def test_solar_min_note():
    """Solar minimum should produce appropriate note."""
    result = estimate_decay(ISS_TLE, f107_solar_flux=70.0)
    assert "minimum" in result.solar_activity_note.lower()


def test_solar_max_note():
    """Solar maximum should produce appropriate note."""
    result = estimate_decay(ISS_TLE, f107_solar_flux=250.0)
    assert "maximum" in result.solar_activity_note.lower()


def test_moderate_solar_note():
    """Moderate solar activity should produce appropriate note."""
    result = estimate_decay(ISS_TLE, f107_solar_flux=150.0)
    assert "moderate" in result.solar_activity_note.lower()


def test_high_alt_longer_lifetime():
    """Higher altitude satellite should have longer lifetime than ISS."""
    iss = estimate_decay(ISS_TLE)
    high = estimate_decay(HIGH_ALT_TLE)
    assert high.estimated_lifetime_days > iss.estimated_lifetime_days
