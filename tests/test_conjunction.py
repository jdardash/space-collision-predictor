"""Tests for the conjunction detection pipeline."""

from datetime import datetime, timezone

from sda.conjunction import classify_risk, find_conjunctions
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
        epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
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
        epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
    ))
    # CSS (Tianhe)
    store.upsert(TLERecord(
        norad_id=48274,
        name="CSS (TIANHE)",
        line1="1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991",
        line2="2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001",
        epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
    ))

    # Short window to keep test fast
    events = find_conjunctions(
        store,
        hours=2.0,
        threshold_km=50.0,
        start=datetime(2024, 2, 14, 12, 0, 0, tzinfo=timezone.utc),
    )
    # We don't assert specific conjunctions — just that it ran cleanly
    assert isinstance(events, list)
