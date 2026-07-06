"""Tests for the SGP4 propagation engine."""

import math
from datetime import UTC, datetime

from sda.models import TLERecord
from sda.propagator import (
    build_satrec,
    compute_distance,
    compute_relative_velocity,
    datetime_to_jd,
    jd_to_datetime,
    propagate_at,
    propagate_window,
    propagate_window_numpy,
)

# ISS (ZARYA) TLE — epoch 2024
ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, 12, 25, 40, tzinfo=UTC),
)


def test_build_satrec():
    satrec = build_satrec(ISS_TLE)
    assert satrec is not None
    assert satrec.satnum == 25544


def test_propagate_at():
    dt = datetime(2024, 2, 14, 13, 0, 0, tzinfo=UTC)
    sv = propagate_at(build_satrec(ISS_TLE), dt)

    # ISS should be in LEO: ~400 km altitude → ~6771 km from center
    r = math.sqrt(sum(c**2 for c in sv.position_km))
    assert 6200 < r < 7200, f"ISS radius {r} km outside LEO bounds"

    # Velocity should be ~7.7 km/s for LEO
    v = math.sqrt(sum(c**2 for c in sv.velocity_km_s))
    assert 6.5 < v < 8.5, f"ISS velocity {v} km/s outside LEO bounds"


def test_propagate_window():
    start = datetime(2024, 2, 14, 12, 0, 0, tzinfo=UTC)
    svs = propagate_window(ISS_TLE, start, hours=1.0, step_seconds=60.0)

    # 1 hour at 60s steps = 61 points
    assert len(svs) == 61


def test_propagate_window_numpy():
    start = datetime(2024, 2, 14, 12, 0, 0, tzinfo=UTC)
    pos, vel, times = propagate_window_numpy(ISS_TLE, start, hours=1.0, step_seconds=60.0)

    assert pos.shape == (61, 3)
    assert vel.shape == (61, 3)
    assert len(times) == 61


def test_compute_distance():
    d = compute_distance((1.0, 0.0, 0.0), (4.0, 0.0, 0.0))
    assert abs(d - 3.0) < 1e-10


def test_compute_relative_velocity():
    rv = compute_relative_velocity((1.0, 2.0, 3.0), (4.0, 2.0, 3.0))
    assert abs(rv - 3.0) < 1e-10


def test_jd_roundtrip():
    dt = datetime(2024, 6, 15, 10, 30, 0, tzinfo=UTC)
    jd, fr = datetime_to_jd(dt)
    dt2 = jd_to_datetime(jd, fr)
    delta = abs((dt - dt2).total_seconds())
    assert delta < 1.0, f"JD roundtrip error: {delta}s"
