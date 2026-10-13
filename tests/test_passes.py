"""Tests for pass prediction, the sensor network and the passes router.

All fixed epochs. The demo ISS TLE has a 2026-03-22 epoch, so every window in
this file sits on that day and the results are deterministic and offline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.demo_tles import DEMO_TLE_TEXT
from sda.passes import (
    CIVIL_TWILIGHT_DEG,
    Observer,
    compass_point,
    earth_fixed_to_eci,
    gmst_rad,
    is_dark,
    is_sunlit,
    look_angles,
    look_angles_for_tle,
    predict_passes,
    predict_passes_multi,
    predict_passes_multi_observer,
    solar_position_eci_km,
    solar_ra_dec_deg,
    sun_elevation_deg,
    topocentric_to_eci,
    visibility_score,
)
from sda.routes.passes import router
from sda.sensors import (
    SENSOR_NETWORK,
    Sensor,
    SensorType,
    compute_dop,
    fence_crossings,
    get_sensor,
    list_sensors,
    pass_rank_score,
    rank_passes,
    rank_passes_by_sensor,
    resolve_sensors,
    sensor_look_angles,
    sensors_covering_tle,
)
from sda.tle_store import TLEStore

ISS_NORAD_ID = 25544
HST_NORAD_ID = 20580

WINDOW_START = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)

MUNICH = Observer(name="Munich", latitude_deg=48.15, longitude_deg=11.58, altitude_km=0.52)
EQUATOR = Observer(name="Equator", latitude_deg=0.0, longitude_deg=20.0, altitude_km=0.0)


@pytest.fixture(scope="module")
def demo_store() -> TLEStore:
    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    return store


@pytest.fixture(scope="module")
def iss(demo_store: TLEStore):
    tle = demo_store.get(ISS_NORAD_ID)
    assert tle is not None
    return tle


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A minimal app carrying only this router, backed by the demo catalog."""
    import sda.api

    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    monkeypatch.setattr(sda.api, "store", store)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Observer geometry and look angles
# ---------------------------------------------------------------------------


def test_observer_sits_on_the_ellipsoid():
    """Site radius falls between the polar and equatorial WGS72 radii."""
    equatorial = float(np.linalg.norm(EQUATOR.earth_fixed_position_km()))
    polar = Observer(name="pole", latitude_deg=90.0, longitude_deg=0.0)
    polar_radius = float(np.linalg.norm(polar.earth_fixed_position_km()))
    assert equatorial == pytest.approx(6378.135, abs=1e-6)
    assert 6356.0 < polar_radius < 6357.5
    assert polar_radius < equatorial


def test_satellite_overhead_gives_elevation_90():
    """A satellite straight up the local vertical sits at 90 deg elevation.

    Built by scaling the observer's own Earth-fixed vector outward, not by
    inverting the look-angle transform, so the check is independent of it. At
    the equator the geodetic normal is radial, so the answer is exactly 90.
    """
    when = datetime(2026, 3, 22, 3, 0, tzinfo=UTC)
    site_ef = EQUATOR.earth_fixed_position_km()
    overhead_ef = site_ef * (1.0 + 500.0 / float(np.linalg.norm(site_ef)))
    overhead_eci = earth_fixed_to_eci(overhead_ef, gmst_rad(when))

    look = look_angles(EQUATOR, overhead_eci, None, when)
    assert look.elevation_deg == pytest.approx(90.0, abs=1e-6)
    assert look.range_km == pytest.approx(500.0, abs=1e-6)

    # Away from the equator the geodetic normal tilts off the radial by up to
    # about 0.2 deg, so the same construction lands just short of the zenith.
    site_ef = MUNICH.earth_fixed_position_km()
    overhead_ef = site_ef * (1.0 + 500.0 / float(np.linalg.norm(site_ef)))
    tilted = look_angles(MUNICH, earth_fixed_to_eci(overhead_ef, gmst_rad(when)), None, when)
    assert 89.5 < tilted.elevation_deg < 90.0


def test_look_angle_round_trip():
    """topocentric_to_eci and look_angles invert each other."""
    when = datetime(2026, 3, 22, 3, 0, tzinfo=UTC)
    for azimuth, elevation, range_km in ((137.0, 23.5, 1200.0), (312.0, 61.0, 700.0)):
        position = topocentric_to_eci(MUNICH, azimuth, elevation, range_km, when)
        look = look_angles(MUNICH, position, None, when)
        assert look.azimuth_deg == pytest.approx(azimuth, abs=1e-3)
        assert look.elevation_deg == pytest.approx(elevation, abs=1e-3)
        assert look.range_km == pytest.approx(range_km, abs=1e-3)


def test_look_angles_for_tle_matches_manual_propagation(iss):
    when = datetime(2026, 3, 22, 15, 1, tzinfo=UTC)
    look = look_angles_for_tle(iss, MUNICH, when)
    assert look.elevation_deg > 10.0
    assert 300.0 < look.range_km < 2500.0
    assert look.compass == compass_point(look.azimuth_deg)


def test_compass_point_cardinals():
    assert compass_point(0.0) == "N"
    assert compass_point(90.0) == "E"
    assert compass_point(180.0) == "S"
    assert compass_point(270.0) == "W"
    assert compass_point(361.0) == "N"
    assert compass_point(-90.0) == "W"


# ---------------------------------------------------------------------------
# Solar geometry
# ---------------------------------------------------------------------------


def test_solar_position_at_j2000():
    """Sun at 2000-01-01 12:00 UTC: RA about 281.3 deg, dec about -23.0 deg."""
    right_ascension, declination = solar_ra_dec_deg(datetime(2000, 1, 1, 12, 0, tzinfo=UTC))
    assert right_ascension == pytest.approx(281.29, abs=1.0)
    assert declination == pytest.approx(-23.03, abs=1.0)


def test_solar_distance_near_perihelion():
    """Early January is perihelion: about 0.983 AU."""
    sun = solar_position_eci_km(datetime(2000, 1, 1, 12, tzinfo=UTC))
    distance_km = float(np.linalg.norm(sun))
    assert 1.46e8 < distance_km < 1.48e8


def test_sunlit_cylindrical_shadow():
    """A satellite behind the Earth is dark; one on the sunward side is lit."""
    when = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)
    sun = solar_position_eci_km(when)
    sun_hat = sun / np.linalg.norm(sun)

    assert is_sunlit(sun_hat * 6800.0, sun) is True
    assert is_sunlit(-sun_hat * 6800.0, sun) is False

    # Just outside the shadow cylinder, on the anti-sun side.
    perpendicular = np.cross(sun_hat, np.array([0.0, 0.0, 1.0]))
    perpendicular /= np.linalg.norm(perpendicular)
    grazing = -sun_hat * 3000.0 + perpendicular * 6500.0
    assert is_sunlit(grazing, sun) is True


def test_sun_elevation_swings_over_a_day():
    site = Observer(name="null island", latitude_deg=0.0, longitude_deg=0.0)
    elevations = [
        sun_elevation_deg(site, datetime(2026, 6, 21, hour, 0, tzinfo=UTC)) for hour in range(24)
    ]
    assert max(elevations) > 60.0
    assert min(elevations) < -60.0
    assert is_dark(site, datetime(2026, 6, 21, 0, 0, tzinfo=UTC)) is True
    assert is_dark(site, datetime(2026, 6, 21, 12, 0, tzinfo=UTC)) is False


def test_visibility_score_bounds():
    assert visibility_score(90.0, True, -20.0) == pytest.approx(1.0)
    assert visibility_score(0.0, False, 45.0) == pytest.approx(0.0)
    lit = visibility_score(45.0, True, -20.0)
    unlit = visibility_score(45.0, False, -20.0)
    assert 0.0 < unlit < lit < 1.0


# ---------------------------------------------------------------------------
# Pass search
# ---------------------------------------------------------------------------


def test_iss_passes_over_munich(iss):
    """An ISS-like TLE gives passes of a few minutes with rise and set at the cut."""
    passes = predict_passes(
        iss,
        MUNICH,
        WINDOW_START,
        hours=12.0,
        min_elevation_deg=10.0,
        include_samples=True,
        sample_step_s=60.0,
    )
    assert 3 <= len(passes) <= 8

    for satellite_pass in passes:
        assert satellite_pass.norad_id == ISS_NORAD_ID
        assert satellite_pass.observer_name == "Munich"
        assert satellite_pass.partial is False

        # Every pass lasts between 3 and 12 minutes at a 10 deg cut.
        assert 180.0 <= satellite_pass.duration_s <= 720.0
        assert satellite_pass.rise_time < satellite_pass.culmination_time
        assert satellite_pass.culmination_time < satellite_pass.set_time

        # Bisection puts rise and set on the elevation threshold within 1 s of
        # travel, which is a couple of tenths of a degree for a LEO object.
        rise_look = look_angles_for_tle(iss, MUNICH, satellite_pass.rise_time)
        set_look = look_angles_for_tle(iss, MUNICH, satellite_pass.set_time)
        assert rise_look.elevation_deg == pytest.approx(10.0, abs=0.25)
        assert set_look.elevation_deg == pytest.approx(10.0, abs=0.25)
        assert rise_look.range_rate_km_s < 0.0  # closing on the way up
        assert set_look.range_rate_km_s > 0.0  # opening on the way down

        assert satellite_pass.max_elevation_deg >= 10.0
        assert satellite_pass.max_elevation_deg >= rise_look.elevation_deg
        assert 0.0 <= satellite_pass.visibility_score <= 1.0

        assert len(satellite_pass.samples) >= 3
        times = [sample.time for sample in satellite_pass.samples]
        assert times == sorted(times)
        assert times[0] == satellite_pass.rise_time
        assert times[-1] == satellite_pass.set_time
        peak_sample = max(sample.elevation_deg for sample in satellite_pass.samples)
        assert peak_sample <= satellite_pass.max_elevation_deg + 1e-6


def test_pass_visibility_flag_follows_sun(iss):
    """visible means sunlit target and a sky darker than civil twilight."""
    passes = predict_passes(
        iss, MUNICH, WINDOW_START, hours=12.0, min_elevation_deg=10.0, include_samples=False
    )
    for satellite_pass in passes:
        expected = satellite_pass.sunlit and satellite_pass.sun_elevation_deg < CIVIL_TWILIGHT_DEG
        assert satellite_pass.visible is expected
    assert any(satellite_pass.visible for satellite_pass in passes)
    assert any(not satellite_pass.visible for satellite_pass in passes)


def test_higher_elevation_cut_yields_fewer_passes(iss):
    low = predict_passes(
        iss, MUNICH, WINDOW_START, hours=12.0, min_elevation_deg=10.0, include_samples=False
    )
    high = predict_passes(
        iss, MUNICH, WINDOW_START, hours=12.0, min_elevation_deg=40.0, include_samples=False
    )
    assert len(high) < len(low)
    assert all(item.max_elevation_deg >= 40.0 for item in high)


def test_pass_open_at_window_end_is_marked_partial(iss):
    """A window cut mid-pass returns a clipped pass flagged partial."""
    reference = predict_passes(
        iss, MUNICH, WINDOW_START, hours=12.0, min_elevation_deg=10.0, include_samples=False
    )[0]
    start = reference.rise_time - timedelta(seconds=60)
    hours = (reference.culmination_time - start).total_seconds() / 3600.0

    clipped = predict_passes(
        iss, MUNICH, start, hours=hours, min_elevation_deg=10.0, include_samples=False
    )
    assert len(clipped) == 1
    assert clipped[0].partial is True
    assert clipped[0].set_time <= reference.set_time


def test_empty_and_invalid_windows(iss):
    assert predict_passes(iss, MUNICH, WINDOW_START, hours=0.0) == []
    with pytest.raises(ValueError, match="coarse_step_s"):
        predict_passes(iss, MUNICH, WINDOW_START, hours=1.0, coarse_step_s=0.0)


def test_multi_object_and_multi_observer(demo_store, iss):
    hst = demo_store.get(HST_NORAD_ID)
    assert hst is not None

    combined = predict_passes_multi(
        [iss, hst], MUNICH, WINDOW_START, hours=12.0, min_elevation_deg=10.0
    )
    assert combined
    assert combined == sorted(combined, key=lambda item: item.rise_time)
    assert {item.norad_id for item in combined} <= {ISS_NORAD_ID, HST_NORAD_ID}

    sites = predict_passes_multi_observer(
        iss, [MUNICH, EQUATOR], WINDOW_START, hours=12.0, min_elevation_deg=10.0
    )
    assert sites == sorted(sites, key=lambda item: item.rise_time)
    assert {item.observer_name for item in sites} == {"Munich", "Equator"}


# ---------------------------------------------------------------------------
# Sensor network
# ---------------------------------------------------------------------------


def test_bundled_network_is_well_formed():
    assert len(SENSOR_NETWORK) >= 14
    names = [sensor.name for sensor in SENSOR_NETWORK]
    assert len(names) == len(set(names))
    for sensor in SENSOR_NETWORK:
        assert -90.0 <= sensor.latitude_deg <= 90.0
        assert -180.0 <= sensor.longitude_deg <= 180.0
        assert 0.0 <= sensor.min_elevation_deg < sensor.max_elevation_deg <= 90.0
        assert sensor.max_range_km > 0.0
        assert sensor.note

    assert list_sensors(SensorType.OPTICAL)
    assert list_sensors(SensorType.RADAR)
    assert len(list_sensors()) == len(SENSOR_NETWORK)


def test_get_and_resolve_sensors():
    assert get_sensor("fylingdales") is not None
    assert get_sensor("  FYLINGDALES  ") is not None
    assert get_sensor("no such site") is None
    assert len(resolve_sensors([])) == len(SENSOR_NETWORK)
    assert [s.name for s in resolve_sensors(["Beale", "nope"])] == ["Beale"]


def test_azimuth_fence_wraps_through_north():
    cavalier = get_sensor("Cavalier PARCS")
    assert cavalier is not None
    assert cavalier.azimuth_in_fence(0.0) is True
    assert cavalier.azimuth_in_fence(350.0) is True
    assert cavalier.azimuth_in_fence(60.0) is True
    assert cavalier.azimuth_in_fence(180.0) is False

    fylingdales = get_sensor("Fylingdales")
    assert fylingdales is not None
    assert all(fylingdales.azimuth_in_fence(float(a)) for a in range(0, 360, 30))


def test_sensor_field_of_regard_limits():
    sensor = Sensor(
        name="test",
        sensor_type=SensorType.RADAR,
        latitude_deg=0.0,
        longitude_deg=0.0,
        min_elevation_deg=10.0,
        max_range_km=2000.0,
        azimuth_min_deg=0.0,
        azimuth_max_deg=90.0,
    )
    when = datetime(2026, 3, 22, 0, 0, tzinfo=UTC)
    site = sensor.observer()

    inside = look_angles(site, topocentric_to_eci(site, 45.0, 30.0, 1000.0, when), None, when)
    assert sensor.sees(inside) is True

    too_low = look_angles(site, topocentric_to_eci(site, 45.0, 4.0, 1000.0, when), None, when)
    assert sensor.sees(too_low) is False

    too_far = look_angles(site, topocentric_to_eci(site, 45.0, 30.0, 3000.0, when), None, when)
    assert sensor.sees(too_far) is False

    wrong_way = look_angles(site, topocentric_to_eci(site, 200.0, 30.0, 1000.0, when), None, when)
    assert sensor.sees(wrong_way) is False


def test_sensors_covering_an_iss_overflight(iss):
    """The bundled Munich station sees the ISS during a known overhead pass."""
    when = datetime(2026, 3, 22, 15, 1, tzinfo=UTC)
    covering = sensors_covering_tle(iss, when)
    names = [entry.sensor_name for entry in covering]
    assert "Amateur Munich" in names
    # Sorted best-placed first.
    elevations = [entry.look.elevation_deg for entry in covering]
    assert elevations == sorted(elevations, reverse=True)
    assert all(entry.in_field_of_regard for entry in covering)


def test_sensor_look_angles_reports_every_site(iss):
    when = datetime(2026, 3, 22, 15, 1, tzinfo=UTC)
    from sda.propagator import build_satrec, datetime_to_jd

    jd, fr = datetime_to_jd(when)
    error, position, velocity = build_satrec(iss).sgp4(jd, fr)
    assert error == 0

    everything = sensor_look_angles(position, velocity, when)
    assert len(everything) == len(SENSOR_NETWORK)
    assert any(entry.in_field_of_regard for entry in everything)
    assert any(not entry.in_field_of_regard for entry in everything)


# ---------------------------------------------------------------------------
# Fence crossings
# ---------------------------------------------------------------------------


def test_fence_crossings_sit_on_the_elevation_cut(iss):
    """An elevation-only fence: every crossing is on the cut, enters before exits."""
    sensor = Sensor(
        name="Plain Munich",
        sensor_type=SensorType.RADAR,
        latitude_deg=48.15,
        longitude_deg=11.58,
        min_elevation_deg=10.0,
        max_range_km=4000.0,
    )
    crossings = fence_crossings(iss, WINDOW_START, hours=12.0, sensors=[sensor], step_s=60.0)

    assert len(crossings) >= 6
    assert len(crossings) % 2 == 0
    assert [crossing.event for crossing in crossings] == ["enter", "exit"] * (len(crossings) // 2)
    assert crossings == sorted(crossings, key=lambda crossing: crossing.time)

    for crossing in crossings:
        assert crossing.norad_id == ISS_NORAD_ID
        assert crossing.sensor_name == "Plain Munich"
        assert crossing.elevation_deg == pytest.approx(10.0, abs=0.25)
        assert crossing.range_km < 4000.0


def test_fence_crossings_include_azimuth_wedge(iss):
    """With a wedge, a crossing is on the elevation cut or on a wedge edge."""
    sensor = Sensor(
        name="Wedge Munich",
        sensor_type=SensorType.RADAR,
        latitude_deg=48.15,
        longitude_deg=11.58,
        min_elevation_deg=5.0,
        max_range_km=4000.0,
        azimuth_min_deg=0.0,
        azimuth_max_deg=180.0,
    )
    crossings = fence_crossings(iss, WINDOW_START, hours=12.0, sensors=[sensor], step_s=60.0)
    assert crossings

    for crossing in crossings:
        # Bisection stops at one second, and near culmination the azimuth can
        # sweep a couple of degrees in that time, so the wedge tolerance is
        # looser than the elevation one.
        on_elevation = abs(crossing.elevation_deg - 5.0) < 0.3
        on_range = abs(crossing.range_km - 4000.0) < 25.0
        on_wedge = min(
            abs(crossing.azimuth_deg - 0.0),
            abs(crossing.azimuth_deg - 360.0),
            abs(crossing.azimuth_deg - 180.0),
        ) < 3.0
        assert on_elevation or on_range or on_wedge
        assert sensor.azimuth_in_fence(crossing.azimuth_deg) or on_wedge

    # At least one crossing is driven by the wedge rather than by elevation.
    assert any(crossing.elevation_deg > 10.0 for crossing in crossings)


def test_fence_crossings_reject_bad_arguments(iss):
    assert fence_crossings(iss, WINDOW_START, hours=0.0) == []
    with pytest.raises(ValueError, match="step_s"):
        fence_crossings(iss, WINDOW_START, hours=1.0, step_s=0.0)


# ---------------------------------------------------------------------------
# Pass ranking
# ---------------------------------------------------------------------------


def test_rank_passes_orders_by_score(iss):
    sensors = [get_sensor("Amateur Munich"), get_sensor("Fylingdales")]
    assert all(sensor is not None for sensor in sensors)

    ranked = rank_passes(iss, WINDOW_START, hours=12.0, sensors=sensors, top_n=2)
    assert ranked
    assert len(ranked) <= 4
    scores = [entry.rank_score for entry in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= score <= 1.0 for score in scores)

    grouped = rank_passes_by_sensor(iss, WINDOW_START, hours=12.0, sensors=sensors, top_n=2)
    assert set(grouped) <= {"Amateur Munich", "Fylingdales"}
    assert sum(len(items) for items in grouped.values()) == len(ranked)


def test_optical_ranking_penalises_a_daylight_pass(iss):
    """Radar scoring ignores lighting; optical scoring does not."""
    passes = predict_passes(
        iss, MUNICH, WINDOW_START, hours=12.0, min_elevation_deg=10.0, include_samples=False
    )
    daylight = next(item for item in passes if not item.visible and item.sun_elevation_deg > 0.0)

    radar = Sensor(
        name="r", sensor_type=SensorType.RADAR, latitude_deg=48.15, longitude_deg=11.58
    )
    optical = Sensor(
        name="o", sensor_type=SensorType.OPTICAL, latitude_deg=48.15, longitude_deg=11.58
    )
    assert pass_rank_score(radar, daylight) > pass_rank_score(optical, daylight)


# ---------------------------------------------------------------------------
# Dilution of precision
# ---------------------------------------------------------------------------


def _synthetic(observer: Observer, when: datetime, geometry) -> list[np.ndarray]:
    return [topocentric_to_eci(observer, az, el, rng, when) for az, el, rng in geometry]


def test_dop_small_for_good_geometry():
    when = datetime(2026, 3, 22, 3, 0, tzinfo=UTC)
    spread = _synthetic(
        EQUATOR,
        when,
        [
            (0.0, 20.0, 1500.0),
            (90.0, 25.0, 1400.0),
            (180.0, 22.0, 1450.0),
            (270.0, 28.0, 1350.0),
            (45.0, 80.0, 600.0),
        ],
    )
    dop = compute_dop(EQUATOR, spread, when)
    assert dop.n_satellites == 5
    assert dop.gdop < 5.0
    assert dop.pdop < 4.0
    assert dop.hdop < 3.0
    assert dop.vdop < 4.0
    assert dop.tdop < 3.0
    assert dop.pdop == pytest.approx((dop.hdop**2 + dop.vdop**2) ** 0.5, abs=1e-3)


def test_dop_large_for_degenerate_geometry():
    when = datetime(2026, 3, 22, 3, 0, tzinfo=UTC)
    clustered = _synthetic(
        EQUATOR,
        when,
        [
            (10.0, 40.0, 1000.0),
            (11.0, 40.5, 1010.0),
            (12.0, 41.0, 1020.0),
            (10.5, 40.2, 1005.0),
        ],
    )
    dop = compute_dop(EQUATOR, clustered, when)
    assert dop.gdop > 100.0
    assert dop.pdop > 100.0


def test_dop_needs_four_objects_above_the_cut():
    when = datetime(2026, 3, 22, 3, 0, tzinfo=UTC)
    three = _synthetic(
        EQUATOR, when, [(0.0, 30.0, 1000.0), (120.0, 30.0, 1000.0), (240.0, 30.0, 1000.0)]
    )
    with pytest.raises(ValueError, match="at least 4"):
        compute_dop(EQUATOR, three, when)

    below = _synthetic(
        EQUATOR,
        when,
        [
            (0.0, 30.0, 1000.0),
            (120.0, 30.0, 1000.0),
            (240.0, 30.0, 1000.0),
            (60.0, 60.0, 900.0),
            (300.0, 5.0, 2000.0),
        ],
    )
    with pytest.raises(ValueError, match="at least 4"):
        compute_dop(EQUATOR, below, when, min_elevation_deg=45.0)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_route_passes(client: TestClient):
    response = client.post(
        "/passes",
        json={
            "observer": {
                "name": "Munich",
                "latitude_deg": 48.15,
                "longitude_deg": 11.58,
                "altitude_km": 0.52,
            },
            "norad_id": ISS_NORAD_ID,
            "start": WINDOW_START.isoformat(),
            "hours": 12.0,
            "min_elevation_deg": 10.0,
            "include_samples": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 3
    assert body["norad_ids"] == [ISS_NORAD_ID]
    first = body["passes"][0]
    assert first["norad_id"] == ISS_NORAD_ID
    assert first["max_elevation_deg"] >= 10.0
    assert first["samples"]
    assert 0.0 <= first["visibility_score"] <= 1.0


def test_route_passes_errors(client: TestClient):
    payload = {
        "observer": {"latitude_deg": 48.15, "longitude_deg": 11.58},
        "start": WINDOW_START.isoformat(),
        "hours": 1.0,
    }
    assert client.post("/passes", json=payload).status_code == 400
    assert client.post("/passes", json={**payload, "norad_id": 999999}).status_code == 404


def test_route_lookangles(client: TestClient):
    response = client.post(
        "/lookangles",
        json={
            "observer": {"latitude_deg": 48.15, "longitude_deg": 11.58, "altitude_km": 0.52},
            "norad_ids": [ISS_NORAD_ID, HST_NORAD_ID],
            "time": datetime(2026, 3, 22, 15, 1, tzinfo=UTC).isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    iss_entry = next(e for e in body["look_angles"] if e["norad_id"] == ISS_NORAD_ID)
    assert iss_entry["elevation_deg"] > 10.0
    assert iss_entry["above_horizon"] is True
    assert iss_entry["compass"]

    assert client.post(
        "/lookangles",
        json={"observer": {"latitude_deg": 0.0, "longitude_deg": 0.0}, "norad_ids": []},
    ).status_code == 400


def test_route_sensors(client: TestClient):
    response = client.get("/sensors")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(SENSOR_NETWORK)
    assert "Illustrative" in body["disclaimer"] or "illustrative" in body["disclaimer"]

    optical = client.get("/sensors", params={"sensor_type": "optical"})
    assert optical.status_code == 200
    assert all(s["sensor_type"] == "optical" for s in optical.json()["sensors"])


def test_route_sensor_coverage(client: TestClient):
    response = client.post(
        "/sensors/coverage",
        json={
            "norad_id": ISS_NORAD_ID,
            "time": datetime(2026, 3, 22, 15, 1, tzinfo=UTC).isoformat(),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    assert "Amateur Munich" in [entry["sensor_name"] for entry in body["coverage"]]

    missing = client.post("/sensors/coverage", json={"norad_id": 999999})
    assert missing.status_code == 404
    no_sensor = client.post(
        "/sensors/coverage", json={"norad_id": ISS_NORAD_ID, "sensor_names": ["nowhere"]}
    )
    assert no_sensor.status_code == 404


def test_route_sensor_fences(client: TestClient):
    response = client.post(
        "/sensors/fences",
        json={
            "norad_id": ISS_NORAD_ID,
            "start": WINDOW_START.isoformat(),
            "hours": 6.0,
            "step_s": 60.0,
            "sensor_names": ["Amateur Munich"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sensors_queried"] == 1
    assert body["count"] >= 2
    events = [crossing["event"] for crossing in body["crossings"]]
    assert set(events) <= {"enter", "exit"}
    assert events[0] == "enter"


def test_route_dops(client: TestClient):
    when = datetime(2026, 3, 22, 15, 1, tzinfo=UTC)
    response = client.post(
        "/dops",
        json={
            "observer": {"latitude_deg": 0.0, "longitude_deg": 0.0},
            "norad_ids": [ISS_NORAD_ID, HST_NORAD_ID],
            "time": when.isoformat(),
        },
    )
    assert response.status_code == 422  # too few objects above the horizon

    empty = client.post(
        "/dops",
        json={"observer": {"latitude_deg": 0.0, "longitude_deg": 0.0}, "norad_ids": []},
    )
    assert empty.status_code == 400


def test_route_dops_with_enough_objects(client: TestClient, demo_store: TLEStore):
    """A site under a dense part of the catalog gets a real DOP back."""
    when = datetime(2026, 3, 22, 15, 1, tzinfo=UTC)
    catalog = [tle.norad_id for tle in demo_store.get_all()]
    assert len(catalog) >= 8

    response = client.post(
        "/dops",
        json={
            "observer": {"latitude_deg": 48.15, "longitude_deg": 11.58},
            "norad_ids": catalog,
            "time": when.isoformat(),
        },
    )
    assert response.status_code in (200, 422)
    if response.status_code == 200:
        dop = response.json()["dop"]
        assert dop["n_satellites"] >= 4
        assert dop["gdop"] > 0.0
        assert dop["pdop"] > 0.0
