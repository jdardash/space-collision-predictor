"""Tests for the conjunction detection pipeline."""

from datetime import UTC, datetime

from sda.conjunction import (
    _refine_conjunction,
    classify_risk,
    clear_conjunction_history,
    find_conjunctions,
    get_conjunction_history,
)
from sda.models import RiskLevel, TLERecord
from sda.tle_store import TLEStore


def test_classify_risk_critical():
    assert classify_risk(0.3, 12.0) == RiskLevel.CRITICAL


def test_classify_risk_high_close():
    assert classify_risk(0.8, 5.0) == RiskLevel.HIGH


def test_classify_risk_high_fast():
    assert classify_risk(3.0, 15.0) == RiskLevel.HIGH


def test_classify_risk_moderate():
    assert classify_risk(3.0, 5.0) == RiskLevel.MODERATE


def test_classify_risk_low():
    assert classify_risk(7.0, 5.0) == RiskLevel.LOW


def test_classify_risk_negligible():
    assert classify_risk(15.0, 1.0) == RiskLevel.NEGLIGIBLE


def test_find_conjunctions_insufficient_sats():
    """Should return empty list with fewer than 2 satellites."""
    store = TLEStore()
    store.upsert(TLERecord(
        norad_id=25544,
        name="ISS",
        line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
        line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
        epoch=datetime(2024, 2, 14, tzinfo=UTC),
    ))
    events = find_conjunctions(store, hours=1.0)
    assert events == []


def test_find_conjunctions_runs():
    """Smoke test: conjunction pipeline runs without errors on two real TLEs."""
    store = TLEStore()
    # ISS
    store.upsert(TLERecord(
        norad_id=25544,
        name="ISS (ZARYA)",
        line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
        line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
        epoch=datetime(2024, 2, 14, tzinfo=UTC),
    ))
    # CSS (Tianhe)
    store.upsert(TLERecord(
        norad_id=48274,
        name="CSS (TIANHE)",
        line1="1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991",
        line2="2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001",
        epoch=datetime(2024, 2, 14, tzinfo=UTC),
    ))

    # Short window to keep test fast
    events = find_conjunctions(
        store,
        hours=2.0,
        threshold_km=50.0,
        start=datetime(2024, 2, 14, 12, 0, 0, tzinfo=UTC),
    )
    # We don't assert specific conjunctions — just that it ran cleanly
    assert isinstance(events, list)


ISS_LINE1 = "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997"
ISS_LINE2 = "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"
CSS_LINE1 = "1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991"
CSS_LINE2 = "2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001"

ISS_TLE = TLERecord(norad_id=25544, name="ISS", line1=ISS_LINE1, line2=ISS_LINE2,
                     epoch=datetime(2024, 2, 14, tzinfo=UTC))
CSS_TLE = TLERecord(norad_id=48274, name="CSS", line1=CSS_LINE1, line2=CSS_LINE2,
                     epoch=datetime(2024, 2, 14, tzinfo=UTC))


def test_conjunction_history_and_clear():
    """Test get/clear conjunction history."""
    clear_conjunction_history()

    # Run a conjunction to populate history
    store = TLEStore()
    store.upsert(ISS_TLE)
    store.upsert(CSS_TLE)
    find_conjunctions(store, hours=24.0, threshold_km=100.0,
                      start=datetime(2024, 2, 14, tzinfo=UTC))

    # Get history (may or may not have events depending on orbital geometry)
    history = get_conjunction_history()
    assert isinstance(history, list)

    # Filter by norad_id
    filtered = get_conjunction_history(norad_id=25544)
    assert isinstance(filtered, list)

    # Clear
    count = clear_conjunction_history()
    assert count >= 0
    assert len(get_conjunction_history()) == 0


def test_find_conjunctions_with_norad_ids():
    """Test conjunction analysis with specific NORAD IDs and deduplication."""
    store = TLEStore()
    store.upsert(ISS_TLE)
    store.upsert(CSS_TLE)

    # Specify IDs including a duplicate — should be deduplicated
    events = find_conjunctions(
        store, norad_ids=[25544, 48274, 25544],
        hours=2.0, threshold_km=100.0,
        start=datetime(2024, 2, 14, 12, 0, 0, tzinfo=UTC),
    )
    assert isinstance(events, list)


def test_find_conjunctions_with_nonexistent_ids():
    """Should return empty when all IDs are missing."""
    store = TLEStore()
    store.upsert(ISS_TLE)
    events = find_conjunctions(store, norad_ids=[99999, 88888], hours=1.0)
    assert events == []


def test_find_conjunctions_with_probability():
    """Test full pipeline including probability computation."""
    store = TLEStore()
    store.upsert(ISS_TLE)
    store.upsert(CSS_TLE)

    events = find_conjunctions(
        store, hours=48.0, threshold_km=100.0,
        start=datetime(2024, 2, 14, 0, 0, 0, tzinfo=UTC),
        compute_probability=True,
    )
    assert isinstance(events, list)
    # Verify sorting: CRITICAL first, then by miss distance
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

    # Check event fields
    for e in events:
        assert e.miss_distance_km >= 0
        assert e.relative_velocity_km_s >= 0
        assert e.risk in RiskLevel


def test_find_conjunctions_no_probability():
    """Test pipeline with probability disabled."""
    store = TLEStore()
    store.upsert(ISS_TLE)
    store.upsert(CSS_TLE)

    events = find_conjunctions(
        store, hours=24.0, threshold_km=100.0,
        start=datetime(2024, 2, 14, 0, 0, 0, tzinfo=UTC),
        compute_probability=False,
    )
    assert isinstance(events, list)
    for e in events:
        assert e.collision_probability is None


def test_refine_conjunction():
    """Test fine refinement of a coarse TCA."""
    coarse_tca = datetime(2024, 2, 14, 12, 0, 0, tzinfo=UTC)
    tca, miss_dist, rel_vel, pos_p, vel_p, pos_s, vel_s = _refine_conjunction(
        ISS_TLE, CSS_TLE, coarse_tca
    )
    assert isinstance(tca, datetime)
    assert miss_dist >= 0
    assert rel_vel >= 0
