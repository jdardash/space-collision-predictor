"""Tests for the NASA standard breakup model and the state-to-TLE fitter.

Offline and deterministic: every random draw is seeded and every orbit comes
from the bundled demo catalog.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from sda.breakup import (
    area_from_characteristic_length,
    cloud_summary,
    collision_consequence,
    collision_effective_mass_kg,
    delta_v_log_mean,
    fragment_count_collision,
    fragment_count_explosion,
    gabbard_data,
    is_catastrophic,
    mass_from_area,
    simulate_breakup,
    specific_energy_j_per_g,
)
from sda.constants import EARTH_MU_KM3_S2, EARTH_RADIUS_KM
from sda.demo_tles import DEMO_TLE_TEXT
from sda.propagator import build_satrec, propagate_at
from sda.rv2tle import fragments_to_tles, state_to_tle
from sda.tle_store import TLEStore

EPOCH = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
ISS_NORAD = 25544
GOES16_NORAD = 41866


@pytest.fixture(scope="module")
def demo_store() -> TLEStore:
    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    return store


@pytest.fixture(scope="module")
def iss_state(demo_store: TLEStore):
    tle = demo_store.get(ISS_NORAD)
    assert tle is not None
    return propagate_at(build_satrec(tle), EPOCH)


# --------------------------------------------------------------------------
# Fragment count laws
# --------------------------------------------------------------------------


def test_collision_count_matches_the_published_power_law():
    """N(>Lc) = 0.1 M^0.75 Lc^-1.71 with Lc = 1 cm on a 1000 kg collision."""
    expected = 0.1 * 1000.0**0.75 * 0.01**-1.71
    assert fragment_count_collision(1000.0, 0.01) == pytest.approx(expected, rel=1e-12)
    assert fragment_count_collision(1000.0, 0.01) == pytest.approx(46773.51412871981, rel=1e-9)


def test_collision_count_at_ten_centimetres_and_one_metre():
    assert fragment_count_collision(1000.0, 0.10) == pytest.approx(
        0.1 * 1000.0**0.75 * 0.10**-1.71, rel=1e-12
    )
    assert fragment_count_collision(1000.0, 1.0) == pytest.approx(17.782794100389228, rel=1e-9)


def test_collision_count_scales_with_mass_to_the_three_quarters():
    small = fragment_count_collision(1000.0, 0.01)
    big = fragment_count_collision(8000.0, 0.01)
    assert big / small == pytest.approx(8.0**0.75, rel=1e-12)


def test_explosion_count_matches_the_published_power_law():
    """N(>Lc) = 6 S Lc^-1.6, S = 1 by default."""
    assert fragment_count_explosion(0.1) == pytest.approx(6.0 * 0.1**-1.6, rel=1e-12)
    assert fragment_count_explosion(0.1) == pytest.approx(238.86430233209836, rel=1e-9)
    assert fragment_count_explosion(0.1, scaling_factor=2.0) == pytest.approx(
        2.0 * fragment_count_explosion(0.1), rel=1e-12
    )


def test_zero_and_negative_inputs_are_rejected_or_empty():
    with pytest.raises(ValueError):
        fragment_count_collision(1000.0, 0.0)
    with pytest.raises(ValueError):
        fragment_count_explosion(-1.0)
    assert fragment_count_collision(0.0, 0.01) == 0.0


# --------------------------------------------------------------------------
# Catastrophic threshold and effective mass
# --------------------------------------------------------------------------


def test_specific_energy_uses_joules_per_gram():
    """0.5 m_p v^2 over the target mass in grams. 10 kg at 10 km/s into 1000 kg."""
    energy = specific_energy_j_per_g(10.0, 1000.0, 10.0)
    assert energy == pytest.approx(0.5 * 10.0 * (10.0 * 1000.0) ** 2 / (1000.0 * 1000.0))
    assert energy == pytest.approx(500.0)
    assert is_catastrophic(10.0, 1000.0, 10.0) is True


def test_forty_joules_per_gram_is_the_catastrophic_boundary():
    # Pick a projectile mass that lands exactly on 40 J/g for a 1000 kg target.
    target = 1000.0
    speed_m_s = 10.0 * 1000.0
    projectile = 40.0 * target * 1000.0 / (0.5 * speed_m_s**2)
    assert specific_energy_j_per_g(projectile, target, 10.0) == pytest.approx(40.0)
    assert is_catastrophic(projectile * 0.99, target, 10.0) is False
    assert is_catastrophic(projectile * 1.01, target, 10.0) is True


def test_effective_mass_is_total_mass_when_catastrophic():
    mass, catastrophic = collision_effective_mass_kg(1000.0, 10.0, 10.0)
    assert catastrophic is True
    assert mass == pytest.approx(1010.0)


def test_effective_mass_uses_projectile_energy_when_not_catastrophic():
    # A 0.1 kg projectile at 1 km/s into 5000 kg is far below 40 J/g.
    mass, catastrophic = collision_effective_mass_kg(5000.0, 0.1, 1.0)
    assert catastrophic is False
    assert mass == pytest.approx(0.1 * 1.0**2)


def test_collision_consequence_orders_the_size_bins():
    consequence = collision_consequence(1000.0, 10.0, 10.0)
    assert consequence.catastrophic is True
    assert consequence.n_above_1cm > consequence.n_above_10cm > consequence.n_above_1m
    assert consequence.n_above_1cm == pytest.approx(
        fragment_count_collision(consequence.effective_mass_kg, 0.01)
    )


# --------------------------------------------------------------------------
# Size, area, mass and delta-V distributions
# --------------------------------------------------------------------------


def test_area_law_switches_regime_at_the_documented_length():
    small = area_from_characteristic_length(0.001)
    assert small == pytest.approx(0.540424 * 0.001**2, rel=1e-12)
    large = area_from_characteristic_length(0.5)
    assert large == pytest.approx(0.556945 * 0.5**2.0047077, rel=1e-12)
    # The two branches agree closely at the break point.
    below = area_from_characteristic_length(0.00167 * 0.999)
    above = area_from_characteristic_length(0.00167 * 1.001)
    assert below == pytest.approx(above, rel=0.02)


def test_mass_follows_from_area_and_area_to_mass():
    assert mass_from_area(0.1, 0.05) == pytest.approx(2.0)


def test_delta_v_log_mean_uses_the_documented_coefficients():
    chi = math.log10(0.1)
    assert delta_v_log_mean(0.1, "collision") == pytest.approx(0.9 * chi + 2.9)
    assert delta_v_log_mean(0.1, "explosion") == pytest.approx(0.2 * chi + 1.85)


# --------------------------------------------------------------------------
# Fragment cloud
# --------------------------------------------------------------------------


def test_cloud_is_reproducible_from_the_seed(iss_state):
    kwargs = dict(
        position_km=iss_state.position_km,
        velocity_km_s=iss_state.velocity_km_s,
        epoch=EPOCH,
        parent_mass_kg=1000.0,
        projectile_mass_kg=10.0,
        relative_speed_km_s=10.0,
        max_fragments=60,
    )
    first = simulate_breakup(seed=7, **kwargs)
    second = simulate_breakup(seed=7, **kwargs)
    third = simulate_breakup(seed=8, **kwargs)

    assert first.fragments_sampled == second.fragments_sampled == 60
    for a, b in zip(first.fragments, second.fragments, strict=True):
        assert a.characteristic_length_m == b.characteristic_length_m
        assert a.mass_kg == b.mass_kg
        assert a.delta_v_vector_km_s == b.delta_v_vector_km_s
    assert [f.delta_v_m_s for f in first.fragments] != [f.delta_v_m_s for f in third.fragments]


def test_cloud_counts_match_the_power_law(iss_state):
    cloud = simulate_breakup(
        position_km=iss_state.position_km,
        velocity_km_s=iss_state.velocity_km_s,
        epoch=EPOCH,
        parent_mass_kg=1000.0,
        projectile_mass_kg=10.0,
        relative_speed_km_s=10.0,
        min_characteristic_length_m=0.01,
        max_fragments=100,
        seed=3,
    )
    assert cloud.catastrophic is True
    assert cloud.specific_energy_j_per_g == pytest.approx(500.0)
    assert cloud.effective_mass_kg == pytest.approx(1010.0)
    assert cloud.n_above_1cm == pytest.approx(fragment_count_collision(1010.0, 0.01))
    assert cloud.n_above_10cm == pytest.approx(fragment_count_collision(1010.0, 0.10))
    assert cloud.n_above_1cm > cloud.n_above_10cm
    # Sampling is capped, so each sampled fragment stands for many real ones.
    assert cloud.fragments_sampled == 100
    assert cloud.representative_count == pytest.approx(
        cloud.total_fragments_modelled / 100.0, rel=1e-9
    )
    # Lengths are sorted largest first and stay inside the requested band.
    lengths = [f.characteristic_length_m for f in cloud.fragments]
    assert lengths == sorted(lengths, reverse=True)
    assert min(lengths) >= 0.01


def test_explosion_cloud_uses_the_explosion_law(iss_state):
    cloud = simulate_breakup(
        position_km=iss_state.position_km,
        velocity_km_s=iss_state.velocity_km_s,
        epoch=EPOCH,
        event_type="explosion",
        parent_mass_kg=1500.0,
        min_characteristic_length_m=0.01,
        max_fragments=50,
        seed=11,
    )
    assert cloud.catastrophic is False
    assert cloud.n_above_1cm == pytest.approx(fragment_count_explosion(0.01))
    # Explosion ejecta are slower than hypervelocity collision ejecta.
    assert np.median([f.delta_v_m_s for f in cloud.fragments]) < 300.0


def test_fragment_states_are_the_parent_state_plus_the_ejecta_impulse(iss_state):
    cloud = simulate_breakup(
        position_km=iss_state.position_km,
        velocity_km_s=iss_state.velocity_km_s,
        epoch=EPOCH,
        max_fragments=25,
        seed=5,
    )
    parent_v = np.array(iss_state.velocity_km_s)
    for fragment in cloud.fragments:
        assert fragment.position_km == pytest.approx(iss_state.position_km)
        delta = np.array(fragment.velocity_km_s) - parent_v
        assert delta == pytest.approx(np.array(fragment.delta_v_vector_km_s), abs=1e-12)
        assert np.linalg.norm(delta) * 1000.0 == pytest.approx(fragment.delta_v_m_s, rel=1e-9)


def test_cloud_summary_reports_finite_statistics(iss_state):
    cloud = simulate_breakup(
        position_km=iss_state.position_km,
        velocity_km_s=iss_state.velocity_km_s,
        epoch=EPOCH,
        max_fragments=40,
        seed=2,
    )
    summary = cloud_summary(cloud)
    assert summary["total_sampled_mass_kg"] > 0.0
    assert summary["max_characteristic_length_m"] >= summary["min_characteristic_length_m"]
    assert summary["represented_mass_kg"] >= summary["total_sampled_mass_kg"]


# --------------------------------------------------------------------------
# Gabbard diagram
# --------------------------------------------------------------------------


def test_gabbard_points_agree_with_the_fragment_states(iss_state):
    cloud = simulate_breakup(
        position_km=iss_state.position_km,
        velocity_km_s=iss_state.velocity_km_s,
        epoch=EPOCH,
        max_fragments=30,
        seed=13,
    )
    points = gabbard_data(cloud)
    assert len(points) == len(cloud.fragments)

    by_index = {point.index: point for point in points}
    for fragment in cloud.fragments:
        point = by_index[fragment.index]
        r_vec = np.array(fragment.position_km)
        v_vec = np.array(fragment.velocity_km_s)
        r = float(np.linalg.norm(r_vec))
        v2 = float(v_vec @ v_vec)
        energy = 0.5 * v2 - EARTH_MU_KM3_S2 / r
        if energy >= 0.0:
            assert point.bound is False
            continue
        sma = -EARTH_MU_KM3_S2 / (2.0 * energy)
        h_vec = np.cross(r_vec, v_vec)
        e_vec = np.cross(v_vec, h_vec) / EARTH_MU_KM3_S2 - r_vec / r
        ecc = float(np.linalg.norm(e_vec))
        assert point.bound is True
        assert point.semi_major_axis_km == pytest.approx(sma, rel=1e-9)
        assert point.eccentricity == pytest.approx(ecc, rel=1e-9)
        assert point.apogee_km == pytest.approx(sma * (1.0 + ecc) - EARTH_RADIUS_KM, rel=1e-9)
        assert point.perigee_km == pytest.approx(sma * (1.0 - ecc) - EARTH_RADIUS_KM, rel=1e-9)
        expected_period = 2.0 * math.pi * math.sqrt(sma**3 / EARTH_MU_KM3_S2) / 60.0
        assert point.period_min == pytest.approx(expected_period, rel=1e-9)
        assert point.apogee_km >= point.perigee_km


# --------------------------------------------------------------------------
# State to TLE
# --------------------------------------------------------------------------


@pytest.mark.parametrize("norad_id", [ISS_NORAD, GOES16_NORAD])
def test_state_to_tle_round_trip_is_under_one_kilometre(demo_store: TLEStore, norad_id: int):
    """Fit a TLE to a propagated state and check the fit reproduces that state."""
    source = demo_store.get(norad_id)
    assert source is not None
    truth = propagate_at(build_satrec(source), EPOCH)

    result = state_to_tle(
        truth.position_km, truth.velocity_km_s, EPOCH, norad_id=90001, name="ROUNDTRIP"
    )
    assert result.tle is not None
    assert result.converged is True
    assert result.position_error_km < 1.0

    # The returned lines must survive the catalog validator, and propagating
    # them must land back on the original state.
    store = TLEStore()
    loaded = store.load_from_text(f"ROUNDTRIP\n{result.tle.line1}\n{result.tle.line2}")
    assert loaded == 1
    refit = store.get(90001)
    assert refit is not None
    state = propagate_at(build_satrec(refit), EPOCH)
    error = math.dist(state.position_km, truth.position_km)
    assert error < 1.0


def test_fragment_tles_load_into_a_catalog(iss_state):
    cloud = simulate_breakup(
        position_km=iss_state.position_km,
        velocity_km_s=iss_state.velocity_km_s,
        epoch=EPOCH,
        max_fragments=40,
        seed=7,
    )
    fitted = fragments_to_tles(cloud, base_norad_id=91000, max_objects=40)
    assert fitted.fitted > 0
    assert fitted.fitted + fitted.skipped == 40
    assert fitted.max_position_error_km < 1.0

    store = TLEStore()
    loaded = store.load_from_text(fitted.tle_text)
    assert loaded == fitted.fitted
    for norad_id in fitted.norad_ids:
        record = store.get(norad_id)
        assert record is not None
        state = propagate_at(build_satrec(record), EPOCH)
        assert math.isfinite(state.position_km[0])
