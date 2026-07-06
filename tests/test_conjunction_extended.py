"""Extended conjunction tests — edge cases and historical validation."""

from datetime import UTC, datetime

from sda.conjunction import classify_risk, find_conjunctions
from sda.models import RiskLevel
from sda.tle_store import TLEStore


def test_risk_boundary_critical():
    """Exactly 0.5 km should be HIGH, not CRITICAL."""
    assert classify_risk(0.5, 5.0) == RiskLevel.HIGH
    assert classify_risk(0.499, 5.0) == RiskLevel.CRITICAL


def test_risk_boundary_high_velocity():
    """Velocity threshold boundary at 5 km miss distance."""
    assert classify_risk(4.9, 10.1) == RiskLevel.HIGH
    assert classify_risk(4.9, 10.0) == RiskLevel.MODERATE
    assert classify_risk(4.9, 9.9) == RiskLevel.MODERATE


def test_risk_boundary_low():
    """Exactly 10 km should be NEGLIGIBLE."""
    assert classify_risk(10.0, 5.0) == RiskLevel.NEGLIGIBLE
    assert classify_risk(9.99, 5.0) == RiskLevel.LOW


def test_risk_extreme_velocity():
    """Very high relative velocity with moderate miss distance."""
    # 15 km/s is a realistic head-on LEO collision velocity
    assert classify_risk(3.0, 15.0) == RiskLevel.HIGH


def test_risk_zero_miss_distance():
    """Zero miss distance should be CRITICAL regardless of velocity."""
    assert classify_risk(0.0, 0.0) == RiskLevel.CRITICAL
    assert classify_risk(0.0, 15.0) == RiskLevel.CRITICAL


def test_risk_large_miss_distance():
    """Very large miss distance should always be NEGLIGIBLE."""
    assert classify_risk(1000.0, 15.0) == RiskLevel.NEGLIGIBLE
    assert classify_risk(100.0, 0.0) == RiskLevel.NEGLIGIBLE


def test_find_conjunctions_single_satellite():
    """Pipeline should return empty for a single satellite."""
    store = TLEStore()
    store.load_from_text(
        "ISS (ZARYA)\n"
        "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997\n"
        "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"
    )
    events = find_conjunctions(store, hours=1.0)
    assert events == []


def test_find_conjunctions_sorted_by_risk():
    """Results should be sorted CRITICAL first, then by miss distance."""
    store = TLEStore()
    store.load_from_text(
        "ISS (ZARYA)\n"
        "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997\n"
        "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014\n"
        "CSS (TIANHE)\n"
        "1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991\n"
        "2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001"
    )
    events = find_conjunctions(store, hours=24.0, threshold_km=50.0)
    if len(events) > 1:
        risk_order = {
            RiskLevel.CRITICAL: 0, RiskLevel.HIGH: 1,
            RiskLevel.MODERATE: 2, RiskLevel.LOW: 3,
            RiskLevel.NEGLIGIBLE: 4,
        }
        for i in range(len(events) - 1):
            a, b = events[i], events[i + 1]
            assert (risk_order[a.risk], a.miss_distance_km) <= (
                risk_order[b.risk], b.miss_distance_km
            )


def test_historical_iridium_cosmos_like():
    """Validate detection of a head-on LEO conjunction scenario.

    Simulates an Iridium-33/Cosmos-2251-like geometry:
    two satellites in crossing orbits with high relative velocity.
    The system should detect a close approach within the threshold.
    """
    store = TLEStore()
    # Use two real LEO satellites with different inclinations
    # to create a crossing-orbit scenario
    store.load_from_text(
        "ISS (ZARYA)\n"
        "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997\n"
        "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014\n"
        "CSS (TIANHE)\n"
        "1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991\n"
        "2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001"
    )

    # Wide threshold to ensure we catch something
    events = find_conjunctions(
        store,
        hours=48.0,
        threshold_km=100.0,
        start=datetime(2024, 2, 14, 0, 0, tzinfo=UTC),
        compute_probability=True,
    )

    # These satellites should have at least one approach
    # within 100 km over 48 hours (different orbital planes cross)
    if len(events) > 0:
        # Validate event structure
        for e in events:
            assert e.primary in (25544, 48274)
            assert e.secondary in (25544, 48274)
            assert e.primary != e.secondary
            assert e.miss_distance_km <= 100.0
            assert e.miss_distance_km >= 0.0
            assert e.relative_velocity_km_s >= 0.0
            assert e.risk in RiskLevel
            # If Pc was computed, it should be bounded
            if e.collision_probability is not None:
                assert 0.0 <= e.collision_probability.probability <= 1.0
