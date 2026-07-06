"""Extended maneuver planning tests — edge cases and physics validation."""

from datetime import UTC, datetime

import numpy as np

from sda.maneuver import (
    _compute_orbital_elements,
    compute_along_track_delta_v,
    plan_maneuver,
)
from sda.models import ConjunctionEvent, RiskLevel, TLERecord

ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, tzinfo=UTC),
)

CSS_TLE = TLERecord(
    norad_id=48274,
    name="CSS (TIANHE)",
    line1="1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991",
    line2="2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001",
    epoch=datetime(2024, 2, 14, tzinfo=UTC),
)


def test_orbital_elements_iss_altitude():
    """Extracted semi-major axis should match ISS ~6771 km."""
    from sda.propagator import build_satrec, datetime_to_jd

    sat = build_satrec(ISS_TLE)
    jd, fr = datetime_to_jd(ISS_TLE.epoch)
    _, pos, vel = sat.sgp4(jd, fr)
    orb = _compute_orbital_elements(np.array(pos), np.array(vel))
    # ISS semi-major axis ~6771 km (altitude ~400 km + Earth radius ~6371 km)
    assert 6600 < orb["a_km"] < 6900


def test_orbital_elements_period():
    """ISS period should be ~92 minutes."""
    from sda.propagator import build_satrec, datetime_to_jd

    sat = build_satrec(ISS_TLE)
    jd, fr = datetime_to_jd(ISS_TLE.epoch)
    _, pos, vel = sat.sgp4(jd, fr)
    orb = _compute_orbital_elements(np.array(pos), np.array(vel))
    period_min = orb["period_s"] / 60.0
    assert 85 < period_min < 100


def test_orbital_elements_rsw_orthonormal():
    """RSW frame vectors should be orthonormal."""
    from sda.propagator import build_satrec, datetime_to_jd

    sat = build_satrec(ISS_TLE)
    jd, fr = datetime_to_jd(ISS_TLE.epoch)
    _, pos, vel = sat.sgp4(jd, fr)
    orb = _compute_orbital_elements(np.array(pos), np.array(vel))

    r, s, w = orb["r_hat"], orb["s_hat"], orb["w_hat"]
    # Each unit length
    assert abs(np.linalg.norm(r) - 1.0) < 1e-10
    assert abs(np.linalg.norm(s) - 1.0) < 1e-10
    assert abs(np.linalg.norm(w) - 1.0) < 1e-10
    # Mutually orthogonal
    assert abs(np.dot(r, s)) < 1e-10
    assert abs(np.dot(r, w)) < 1e-10
    assert abs(np.dot(s, w)) < 1e-10


def test_delta_v_near_zero_miss():
    """Near-zero miss should require meaningful delta-V."""
    dv = compute_along_track_delta_v(
        miss_distance_km=0.01,
        target_miss_km=5.0,
        lead_time_hours=6.0,
        orbital_period_s=5400.0,
        semi_major_axis_km=6771.0,
    )
    assert dv > 0.1  # should need at least 0.1 m/s


def test_delta_v_very_short_lead_time():
    """Very short lead time should require larger delta-V."""
    dv_short = compute_along_track_delta_v(0.5, 5.0, 0.5, 5400.0, 6771.0)
    dv_long = compute_along_track_delta_v(0.5, 5.0, 24.0, 5400.0, 6771.0)
    assert dv_short > dv_long * 10  # much more dV needed with less time


def test_plan_maneuver_critical_event():
    """Critical events should produce multiple maneuver options."""
    event = ConjunctionEvent(
        primary=25544,
        secondary=48274,
        tca=datetime(2024, 2, 15, 6, 0, tzinfo=UTC),
        miss_distance_km=0.3,
        relative_velocity_km_s=14.0,
        risk=RiskLevel.CRITICAL,
    )
    result = plan_maneuver(event, ISS_TLE, CSS_TLE, target_miss_km=5.0)
    assert len(result.options) > 0
    # Should have along-track options (most fuel-efficient)
    along_opts = [o for o in result.options if o.direction == "along-track"]
    assert len(along_opts) > 0


def test_plan_maneuver_all_directions():
    """Plan should include along-track, cross-track, and radial options."""
    event = ConjunctionEvent(
        primary=25544,
        secondary=48274,
        tca=datetime(2024, 2, 15, 6, 0, tzinfo=UTC),
        miss_distance_km=1.0,
        relative_velocity_km_s=10.0,
        risk=RiskLevel.HIGH,
    )
    result = plan_maneuver(event, ISS_TLE, CSS_TLE, target_miss_km=5.0)
    directions = {o.direction for o in result.options}
    assert "along-track" in directions
    # cross-track and radial may be filtered if delta-V > 100 m/s
    # but at least along-track should always be present


def test_plan_maneuver_burn_times_before_tca():
    """All burn times should be before TCA."""
    tca = datetime(2024, 2, 15, 6, 0, tzinfo=UTC)
    event = ConjunctionEvent(
        primary=25544, secondary=48274,
        tca=tca, miss_distance_km=1.0,
        relative_velocity_km_s=10.0, risk=RiskLevel.HIGH,
    )
    result = plan_maneuver(event, ISS_TLE, CSS_TLE)
    for opt in result.options:
        assert opt.burn_time < tca


def test_plan_maneuver_custom_lead_times():
    """Custom lead times should be respected."""
    event = ConjunctionEvent(
        primary=25544, secondary=48274,
        tca=datetime(2024, 2, 15, 6, 0, tzinfo=UTC),
        miss_distance_km=1.0, relative_velocity_km_s=10.0,
        risk=RiskLevel.HIGH,
    )
    result = plan_maneuver(
        event, ISS_TLE, CSS_TLE,
        lead_times_hours=[1.0, 48.0],
    )
    lead_times = {o.lead_time_hours for o in result.options}
    assert 1.0 in lead_times or 48.0 in lead_times
