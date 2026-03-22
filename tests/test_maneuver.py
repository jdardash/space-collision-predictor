"""Tests for maneuver planning module."""

import math
from datetime import datetime, timezone

from sda.models import TLERecord, ConjunctionEvent, RiskLevel
from sda.maneuver import plan_maneuver, compute_along_track_delta_v, ManeuverPlan


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


def test_along_track_delta_v_basic():
    """Along-track dV should be positive when miss < target."""
    dv = compute_along_track_delta_v(
        miss_distance_km=1.0,
        target_miss_km=5.0,
        lead_time_hours=12.0,
        orbital_period_s=5400.0,
        semi_major_axis_km=6771.0,
    )
    assert dv > 0


def test_along_track_delta_v_zero_when_sufficient():
    """dV should be zero when miss already exceeds target."""
    dv = compute_along_track_delta_v(
        miss_distance_km=10.0,
        target_miss_km=5.0,
        lead_time_hours=12.0,
        orbital_period_s=5400.0,
        semi_major_axis_km=6771.0,
    )
    assert dv == 0.0


def test_along_track_delta_v_decreases_with_lead_time():
    """More lead time → less dV needed (secular drift grows linearly)."""
    dv_short = compute_along_track_delta_v(2.0, 5.0, 2.0, 5400.0, 6771.0)
    dv_long = compute_along_track_delta_v(2.0, 5.0, 24.0, 5400.0, 6771.0)
    assert dv_long < dv_short


def test_plan_maneuver_no_action_needed():
    """No maneuver when miss distance already exceeds target."""
    event = ConjunctionEvent(
        primary=25544, secondary=48274,
        tca=datetime(2024, 2, 15, 6, 0, tzinfo=timezone.utc),
        miss_distance_km=10.0, relative_velocity_km_s=8.0,
        risk=RiskLevel.LOW,
    )
    result = plan_maneuver(event, ISS_TLE, CSS_TLE, target_miss_km=5.0)
    assert isinstance(result, ManeuverPlan)
    assert result.warning is not None
    assert len(result.options) == 0


def test_plan_maneuver_produces_options():
    """Should produce at least one option when miss < target."""
    event = ConjunctionEvent(
        primary=25544, secondary=48274,
        tca=datetime(2024, 2, 15, 6, 0, tzinfo=timezone.utc),
        miss_distance_km=1.0, relative_velocity_km_s=10.0,
        risk=RiskLevel.HIGH,
    )
    result = plan_maneuver(event, ISS_TLE, CSS_TLE, target_miss_km=5.0)
    assert len(result.options) > 0
    assert result.recommended is not None


def test_plan_maneuver_recommended_is_minimum():
    """Recommended should be the minimum dV option."""
    event = ConjunctionEvent(
        primary=25544, secondary=48274,
        tca=datetime(2024, 2, 15, 6, 0, tzinfo=timezone.utc),
        miss_distance_km=1.0, relative_velocity_km_s=10.0,
        risk=RiskLevel.HIGH,
    )
    result = plan_maneuver(event, ISS_TLE, CSS_TLE, target_miss_km=5.0)
    if result.recommended and len(result.options) > 1:
        assert result.recommended.delta_v_m_s <= min(o.delta_v_m_s for o in result.options)
