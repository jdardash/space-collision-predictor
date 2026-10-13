"""Tests for full-catalog screening, regime survey, formations and risk score.

Everything here is offline and deterministic: TLEs are either the bundled demo
catalog or built in-process, and every propagation start time is pinned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sda.api import app
from sda.conjunction import (
    classify_risk,
    find_conjunctions,
    resolve_screening_method,
    screen_coarse,
    screen_regime,
)
from sda.demo_tles import DEMO_TLE_TEXT
from sda.models import CollisionProbability, ConjunctionEvent, RiskLevel
from sda.screening import (
    BUNDLED_FORMATION_RULES,
    FORMATION_HEURISTIC_LIMITS,
    SCIPY_AVAILABLE,
    SPATIAL_AUTO_MIN_OBJECTS,
    FormationRule,
    active_spatial_backend,
    altitude_band_km,
    brute_force_pairs_within,
    classify_regime,
    classify_regime_from_line2,
    default_threshold_for_regime,
    formation_rules_with_extras,
    grid_pairs_within,
    launch_prefix,
    match_formation,
    mean_elements_from_line2,
    object_class,
    pair_regime,
    proximity_score,
    query_pairs_within,
    risk_score,
    risk_score_breakdown,
    shell_bands,
    shell_prefilter_margin_km,
    shells_overlap,
    size_score,
    urgency_score,
)
from sda.tle_store import TLEStore

client = TestClient(app)

START = datetime(2026, 1, 1, tzinfo=UTC)

_BASE_LINE1 = "1 25544U 98067A   26001.50000000  .00000000  00000+0  00000+0 0  9990"


def _checksum(stem: str) -> str:
    """Standard TLE modulo-10 checksum over the first 68 columns."""
    total = sum(int(c) for c in stem if c.isdigit()) + stem.count("-")
    return str(total % 10)


def make_tle_lines(
    norad_id: int,
    mean_motion: float,
    eccentricity: float = 0.0001,
    inclination: float = 51.6,
    raan: float = 0.0,
    arg_perigee: float = 0.0,
    mean_anomaly: float = 0.0,
    intl: str = "98067A",
) -> tuple[str, str]:
    """Build a column-correct TLE pair for a synthetic object."""
    stem1 = f"1 {norad_id:05d}U {intl:<8s}" + _BASE_LINE1[17:68]
    ecc_digits = f"{eccentricity:.7f}"[2:]
    stem2 = (
        f"2 {norad_id:05d} {inclination:8.4f} {raan:8.4f} {ecc_digits} "
        f"{arg_perigee:8.4f} {mean_anomaly:8.4f} {mean_motion:11.8f}{1:5d}"
    )
    return stem1 + _checksum(stem1), stem2 + _checksum(stem2)


def make_tle_text(norad_id: int, name: str, **kwargs: float | str) -> str:
    """A three-line TLE block ready for TLEStore.load_from_text."""
    line1, line2 = make_tle_lines(norad_id, **kwargs)  # type: ignore[arg-type]
    return f"{name}\n{line1}\n{line2}\n"


def demo_store() -> TLEStore:
    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    return store


# --------------------------------------------------------------------------- #
# Synthetic TLE helper sanity
# --------------------------------------------------------------------------- #


def test_make_tle_lines_are_ingestable():
    store = TLEStore()
    count = store.load_from_text(make_tle_text(90001, "SYNTH LEO", mean_motion=15.5))
    assert count == 1
    assert store.get(90001) is not None


def test_make_tle_lines_have_standard_length():
    line1, line2 = make_tle_lines(90002, mean_motion=1.00270000)
    assert len(line1) == 69
    assert len(line2) == 69


# --------------------------------------------------------------------------- #
# Method resolution
# --------------------------------------------------------------------------- #


def test_resolve_method_auto_small_catalog_is_allpairs():
    assert resolve_screening_method("auto", SPATIAL_AUTO_MIN_OBJECTS - 1) == "allpairs"


def test_resolve_method_auto_large_catalog_is_spatial():
    assert resolve_screening_method("auto", SPATIAL_AUTO_MIN_OBJECTS) == "spatial"


def test_resolve_method_explicit_is_respected():
    assert resolve_screening_method("allpairs", 10_000) == "allpairs"
    assert resolve_screening_method("spatial", 2) == "spatial"


def test_resolve_method_rejects_unknown():
    with pytest.raises(ValueError, match="unknown screening method"):
        resolve_screening_method("kdtree", 10)


# --------------------------------------------------------------------------- #
# Equivalence: spatial path must match the all-pairs path
# --------------------------------------------------------------------------- #


def test_spatial_and_allpairs_produce_identical_events():
    store = demo_store()
    allpairs = find_conjunctions(
        store, hours=6.0, threshold_km=25.0, start=START, method="allpairs"
    )
    spatial = find_conjunctions(
        store, hours=6.0, threshold_km=25.0, start=START, method="spatial"
    )

    assert allpairs, "demo catalog should produce at least one event"
    by_pair_a = {(e.primary, e.secondary): e for e in allpairs}
    by_pair_b = {(e.primary, e.secondary): e for e in spatial}
    assert set(by_pair_a) == set(by_pair_b)

    for key, event_a in by_pair_a.items():
        event_b = by_pair_b[key]
        assert abs((event_a.tca - event_b.tca).total_seconds()) <= 1.0
        assert abs(event_a.miss_distance_km - event_b.miss_distance_km) <= 0.001


def test_coarse_candidate_sets_match_exactly():
    store = demo_store()
    tles = store.get_all()
    allpairs, _, method_a = screen_coarse(
        tles, START, hours=6.0, coarse_margin_km=250.0, method="allpairs"
    )
    spatial, _, method_b = screen_coarse(
        tles, START, hours=6.0, coarse_margin_km=250.0, method="spatial"
    )
    assert (method_a, method_b) == ("allpairs", "spatial")
    assert {(c[0], c[1]) for c in allpairs} == {(c[0], c[1]) for c in spatial}

    spatial_by_pair = {(c[0], c[1]): c for c in spatial}
    for i, j, dist, step in allpairs:
        _, _, other_dist, other_step = spatial_by_pair[(i, j)]
        assert step == other_step
        assert dist == pytest.approx(other_dist, abs=1e-9)


def test_auto_method_matches_explicit_choice():
    store = demo_store()
    auto = find_conjunctions(store, hours=4.0, threshold_km=20.0, start=START, method="auto")
    expected = resolve_screening_method("auto", len(store.get_all()))
    explicit = find_conjunctions(store, hours=4.0, threshold_km=20.0, start=START, method=expected)
    assert [e.model_dump() for e in auto] == [e.model_dump() for e in explicit]
    assert all(e.screening_method == expected for e in auto)


def test_brute_backend_matches_default_backend():
    store = demo_store()
    default = find_conjunctions(store, hours=4.0, threshold_km=20.0, start=START, method="spatial")
    brute = find_conjunctions(
        store, hours=4.0, threshold_km=20.0, start=START,
        method="spatial", spatial_backend="brute",
    )
    assert {(e.primary, e.secondary) for e in default} == {(e.primary, e.secondary) for e in brute}


# --------------------------------------------------------------------------- #
# Shell prefilter
# --------------------------------------------------------------------------- #


def test_mean_elements_recover_iss_altitude():
    _, line2 = make_tle_lines(25544, mean_motion=15.49584387, eccentricity=0.0004885)
    elements = mean_elements_from_line2(line2)
    assert elements.valid
    assert 380.0 < elements.perigee_alt_km < 440.0
    assert elements.apogee_alt_km >= elements.perigee_alt_km


def test_altitude_band_of_unparseable_tle_spans_everything():
    class Broken:
        norad_id = 1
        name = "BROKEN"
        line1 = "1 00001U"
        line2 = "2 00001 garbage"

    assert altitude_band_km(Broken()) == (0.0, 1.0e6)


def test_shells_of_leo_and_geo_do_not_overlap():
    store = TLEStore()
    store.load_from_text(make_tle_text(90010, "SYNTH LEO", mean_motion=15.5))
    store.load_from_text(make_tle_text(90011, "SYNTH GEO", mean_motion=1.00270000))
    perigee, apogee = shell_bands(store.get_all())
    margin = shell_prefilter_margin_km(10.0)
    assert not bool(shells_overlap(perigee[0], apogee[0], perigee[1], apogee[1], margin))


def test_shells_of_two_similar_leo_objects_overlap():
    store = TLEStore()
    store.load_from_text(make_tle_text(90012, "SYNTH LEO A", mean_motion=15.5))
    store.load_from_text(make_tle_text(90013, "SYNTH LEO B", mean_motion=15.48))
    perigee, apogee = shell_bands(store.get_all())
    margin = shell_prefilter_margin_km(10.0)
    assert bool(shells_overlap(perigee[0], apogee[0], perigee[1], apogee[1], margin))


def test_prefilter_never_drops_a_real_event():
    store = demo_store()
    tles = store.get_all()
    filtered, _, _ = screen_coarse(
        tles, START, hours=6.0, coarse_margin_km=100.0,
        method="allpairs", shell_prefilter=True,
    )
    unfiltered, _, _ = screen_coarse(
        tles, START, hours=6.0, coarse_margin_km=100.0,
        method="allpairs", shell_prefilter=False,
    )
    assert {(c[0], c[1]) for c in filtered} == {(c[0], c[1]) for c in unfiltered}


def test_prefilter_margin_grows_with_threshold():
    assert shell_prefilter_margin_km(10.0) < shell_prefilter_margin_km(50.0)
    assert shell_prefilter_margin_km(0.0) == 200.0


# --------------------------------------------------------------------------- #
# Spatial index backends
# --------------------------------------------------------------------------- #


def _pair_set(pairs: np.ndarray) -> set[tuple[int, int]]:
    return {(int(a), int(b)) for a, b in pairs}


def test_grid_hash_matches_brute_force_on_random_points():
    rng = np.random.default_rng(20260101)
    points = rng.normal(scale=60.0, size=(500, 3))
    for radius in (5.0, 25.0, 120.0):
        assert _pair_set(grid_pairs_within(points, radius)) == _pair_set(
            brute_force_pairs_within(points, radius)
        )


def test_grid_hash_matches_brute_force_on_clustered_points():
    rng = np.random.default_rng(7)
    clusters = rng.normal(scale=3.0, size=(300, 3)) + rng.integers(
        0, 4, size=(300, 1)
    ) * 500.0
    assert _pair_set(grid_pairs_within(clusters, 8.0)) == _pair_set(
        brute_force_pairs_within(clusters, 8.0)
    )


def test_grid_hash_matches_brute_force_with_extreme_outliers():
    # A decaying TLE can leave SGP4 with a finite but enormous position. The
    # dense cell key would overflow, so the lexicographic key takes over; the
    # answer must stay exact either way.
    rng = np.random.default_rng(11)
    points = rng.normal(scale=50.0, size=(250, 3))
    points[:5] = rng.normal(scale=1.0e9, size=(5, 3))
    assert _pair_set(grid_pairs_within(points, 20.0)) == _pair_set(
        brute_force_pairs_within(points, 20.0)
    )


def test_grid_hash_falls_back_on_non_finite_points():
    points = np.zeros((4, 3))
    points[2, 0] = np.nan
    assert _pair_set(grid_pairs_within(points, 1.0)) == _pair_set(
        brute_force_pairs_within(points, 1.0)
    )


def test_grid_hash_handles_degenerate_inputs():
    assert grid_pairs_within(np.zeros((1, 3)), 10.0).shape == (0, 2)
    assert grid_pairs_within(np.zeros((0, 3)), 10.0).shape == (0, 2)
    assert grid_pairs_within(np.zeros((4, 3)), 0.0).shape == (0, 2)
    # Four coincident points: every unordered pair, exactly once.
    assert grid_pairs_within(np.zeros((4, 3)), 1.0).shape == (6, 2)


def test_query_pairs_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unknown spatial backend"):
        query_pairs_within(np.zeros((3, 3)), 1.0, backend="octree")


def test_auto_backend_name_is_reported():
    assert active_spatial_backend() == ("scipy" if SCIPY_AVAILABLE else "grid")


@pytest.mark.skipif(not SCIPY_AVAILABLE, reason="scipy is an optional dependency")
def test_scipy_backend_matches_brute_force():
    rng = np.random.default_rng(3)
    points = rng.normal(scale=40.0, size=(200, 3))
    assert _pair_set(query_pairs_within(points, 12.0, backend="scipy")) == _pair_set(
        brute_force_pairs_within(points, 12.0)
    )


# --------------------------------------------------------------------------- #
# Regime classification and survey
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mean_motion", "eccentricity", "expected"),
    [
        (15.49584387, 0.0004885, "LEO"),
        (14.20000000, 0.0010000, "LEO"),
        (2.00560000, 0.0050000, "MEO"),
        (1.00270000, 0.0001000, "GEO"),
        (2.00600000, 0.7200000, "HEO"),
        (0.90000000, 0.0010000, "OTHER"),
    ],
)
def test_classify_regime_from_mean_elements(mean_motion, eccentricity, expected):
    _, line2 = make_tle_lines(90020, mean_motion=mean_motion, eccentricity=eccentricity)
    assert classify_regime_from_line2(line2) == expected


def test_classify_regime_of_unparseable_line_is_other():
    assert classify_regime_from_line2("2 00001 nonsense") == "OTHER"


def test_classify_regime_on_a_record():
    store = TLEStore()
    store.load_from_text(make_tle_text(90021, "SYNTH GEO", mean_motion=1.00270000))
    record = store.get(90021)
    assert record is not None
    assert classify_regime(record) == "GEO"


def test_regime_defaults_widen_with_altitude():
    assert default_threshold_for_regime("LEO") < default_threshold_for_regime("GEO")
    assert default_threshold_for_regime("leo") == default_threshold_for_regime("LEO")
    assert default_threshold_for_regime("NOT_A_REGIME") == 10.0


def test_pair_regime_labels_mixed_pairs():
    assert pair_regime("LEO", "LEO") == "LEO"
    assert pair_regime("LEO", "GEO") == "MIXED"


def test_screen_regime_only_screens_that_regime():
    store = demo_store()
    result = screen_regime(store, "LEO", hours=4.0, threshold_km=20.0, start=START)
    assert result.regime == "LEO"
    assert result.threshold_km == 20.0
    assert result.objects_screened == sum(
        1 for t in store.get_all() if classify_regime(t) == "LEO"
    )
    assert all(e.regime == "LEO" for e in result.events)


def test_screen_regime_uses_the_regime_default_threshold():
    store = demo_store()
    result = screen_regime(store, "GEO", hours=2.0, start=START)
    assert result.threshold_km == default_threshold_for_regime("GEO")


def test_screen_regime_ranks_by_closest_approach_and_limits():
    store = demo_store()
    result = screen_regime(store, "LEO", hours=6.0, threshold_km=25.0, start=START, limit=3)
    distances = [e.miss_distance_km for e in result.events]
    assert distances == sorted(distances)
    assert len(result.events) <= 3


def test_screen_regime_can_drop_formation_pairs():
    store = demo_store()
    kept = screen_regime(store, "LEO", hours=6.0, threshold_km=25.0, start=START)
    dropped = screen_regime(
        store, "LEO", hours=6.0, threshold_km=25.0, start=START, include_formations=False
    )
    assert any(e.formation_excluded for e in kept.events)
    assert not any(e.formation_excluded for e in dropped.events)
    assert len(dropped.events) < len(kept.events)


def test_screen_regime_with_no_members_is_empty():
    store = TLEStore()
    store.load_from_text(make_tle_text(90030, "SYNTH LEO", mean_motion=15.5))
    result = screen_regime(store, "MEO", hours=1.0, start=START)
    assert result.objects_screened == 0
    assert result.events == []


# --------------------------------------------------------------------------- #
# Formation exclusion table
# --------------------------------------------------------------------------- #


def test_launch_prefix_from_line1():
    line1, _ = make_tle_lines(25544, mean_motion=15.5, intl="98067A")
    assert launch_prefix(line1) == "98067"
    assert launch_prefix("1 25544U") == ""


def test_iss_complex_matches_a_visitor():
    line1, _ = make_tle_lines(25544, mean_motion=15.5, intl="98067A")
    visitor, _ = make_tle_lines(57000, mean_motion=15.5, intl="23093A")
    rule = match_formation(25544, "ISS (ZARYA)", line1, 57000, "PROGRESS-MS 24", visitor)
    assert rule is not None
    assert rule.key == "iss_complex"


def test_formation_pair_needs_both_members():
    line1, _ = make_tle_lines(25544, mean_motion=15.5)
    other, _ = make_tle_lines(43013, mean_motion=15.0)
    assert match_formation(25544, "ISS (ZARYA)", line1, 43013, "NOAA 20", other) is None


def test_tandem_x_matches_by_name():
    line1, _ = make_tle_lines(31698, mean_motion=15.2, intl="07026A")
    line2, _ = make_tle_lines(36605, mean_motion=15.2, intl="10030A")
    rule = match_formation(31698, "TERRASAR-X", line1, 36605, "TANDEM-X", line2)
    assert rule is not None
    assert rule.key == "tandem_x"


def test_starlink_rule_requires_the_same_launch():
    same_a, _ = make_tle_lines(44713, mean_motion=15.06, intl="19074A")
    same_b, _ = make_tle_lines(44714, mean_motion=15.06, intl="19074B")
    other, _ = make_tle_lines(45000, mean_motion=15.06, intl="20001A")
    matched = match_formation(
        44713, "STARLINK-1007", same_a, 44714, "STARLINK-1008", same_b
    )
    assert matched is not None
    assert matched.key == "starlink_same_launch"
    assert match_formation(
        44713, "STARLINK-1007", same_a, 45000, "STARLINK-2000", other
    ) is None


def test_user_supplied_rules_extend_the_bundled_table():
    custom = FormationRule(
        key="test_pair",
        label="Test pair",
        reason="unit_test",
        norad_ids=frozenset({90040, 90041}),
    )
    rules = formation_rules_with_extras([custom])
    assert len(rules) == len(BUNDLED_FORMATION_RULES) + 1
    line, _ = make_tle_lines(90040, mean_motion=15.0)
    matched = match_formation(90040, "A", line, 90041, "B", line, rules=rules)
    assert matched is not None
    assert matched.key == "test_pair"
    assert match_formation(90040, "A", line, 90041, "B", line) is None


def test_formation_flag_is_carried_not_dropped():
    store = demo_store()
    events = find_conjunctions(store, hours=6.0, threshold_km=25.0, start=START)
    flagged = [e for e in events if e.formation_excluded]
    assert flagged, "the demo catalog contains ISS complex pairs"
    assert all(e.formation_label for e in flagged)


def test_heuristic_limits_are_documented():
    assert len(FORMATION_HEURISTIC_LIMITS) >= 4
    assert all(isinstance(limit, str) and limit for limit in FORMATION_HEURISTIC_LIMITS)


# --------------------------------------------------------------------------- #
# Composite risk score
# --------------------------------------------------------------------------- #


def build_event(
    miss_km: float = 1.0,
    hours_ahead: float = 6.0,
    probability: float | None = None,
    primary_name: str = "SAT ALPHA",
    secondary_name: str = "SAT BETA",
) -> ConjunctionEvent:
    pc = None
    if probability is not None:
        pc = CollisionProbability(
            probability=probability,
            miss_distance_km=miss_km,
            combined_sigma_km=0.1,
            mahalanobis_distance=miss_km / 0.1,
            hard_body_radius_km=0.02,
        )
    return ConjunctionEvent(
        primary=90050,
        secondary=90051,
        primary_name=primary_name,
        secondary_name=secondary_name,
        tca=START + timedelta(hours=hours_ahead),
        miss_distance_km=miss_km,
        relative_velocity_km_s=10.0,
        risk=classify_risk(miss_km, 10.0),
        collision_probability=pc,
    )


def test_risk_score_is_bounded():
    for miss in (0.0, 0.5, 5.0, 50.0, 500.0):
        for hours in (-1.0, 0.0, 1.0, 24.0, 500.0):
            score = risk_score(build_event(miss_km=miss, hours_ahead=hours), now=START)
            assert 0.0 <= score <= 100.0


def test_risk_score_worst_case_is_one_hundred():
    event = build_event(
        miss_km=0.0,
        hours_ahead=1.0,
        primary_name="COSMOS 2251 DEB",
        secondary_name="SL-16 R/B",
    )
    assert risk_score(event, now=START, primary_size="large", secondary_size="large") == 100.0


def test_risk_score_best_case_is_near_zero():
    event = build_event(miss_km=200.0, hours_ahead=500.0)
    score = risk_score(event, now=START, primary_size="small", secondary_size="small")
    assert 0.0 <= score < 10.0


def test_risk_score_rises_as_miss_distance_falls():
    close = risk_score(build_event(miss_km=0.2), now=START)
    far = risk_score(build_event(miss_km=20.0), now=START)
    assert close > far


def test_risk_score_prefers_probability_over_miss_distance():
    breakdown = risk_score_breakdown(build_event(miss_km=9.0, probability=1e-4), now=START)
    assert breakdown.proximity_source == "pc"
    assert breakdown.proximity == pytest.approx(500.0 / 6.0, abs=0.01)
    fallback = risk_score_breakdown(build_event(miss_km=9.0), now=START)
    assert fallback.proximity_source == "miss_distance"


def test_proximity_score_boundaries():
    assert proximity_score(1e-3, 0.0)[0] == pytest.approx(100.0)
    assert proximity_score(1e-9, 0.0)[0] == pytest.approx(0.0)
    assert proximity_score(1e-6, 0.0)[0] == pytest.approx(50.0)
    assert proximity_score(1.0, 0.0)[0] == 100.0
    assert proximity_score(0.0, 0.0) == (0.0, "pc")
    assert proximity_score(None, 0.0)[0] == pytest.approx(100.0)
    assert proximity_score(None, 2.0)[0] == pytest.approx(50.0)


def test_urgency_score_boundaries():
    assert urgency_score(None) == 0.0
    assert urgency_score(-1.0) == 0.0
    assert urgency_score(0.0) == 0.0
    assert urgency_score(2.0) == 100.0
    assert urgency_score(72.0) == 0.0
    assert urgency_score(100.0) == 0.0
    assert urgency_score(37.0) == pytest.approx(50.0)


def test_size_score_takes_the_larger_object():
    assert size_score("small", "large") == 100.0
    assert size_score("small", "small") == 30.0
    assert size_score("nonsense", "nonsense") == size_score("unknown", "unknown")


def test_object_class_name_heuristics():
    assert object_class("COSMOS 2251 DEB") == "DEBRIS"
    assert object_class("SL-16 R/B") == "ROCKET_BODY"
    assert object_class("ISS (ZARYA)") == "PAYLOAD"


def test_maneuverable_override_lowers_the_score():
    event = build_event(miss_km=1.0, hours_ahead=1.0)
    both_active = risk_score(event, now=START)
    neither_active = risk_score(
        event, now=START, primary_maneuverable=False, secondary_maneuverable=False
    )
    assert neither_active > both_active


def test_risk_score_does_not_touch_classify_risk():
    event = build_event(miss_km=0.3)
    assert event.risk == RiskLevel.CRITICAL
    risk_score(event, now=START)
    assert event.risk == RiskLevel.CRITICAL


def test_pipeline_attaches_a_risk_score():
    store = demo_store()
    events = find_conjunctions(store, hours=4.0, threshold_km=20.0, start=START)
    assert events
    assert all(e.risk_score is not None and 0.0 <= e.risk_score <= 100.0 for e in events)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

_ROUTE_TLE_TEXT = DEMO_TLE_TEXT


def _seed_api_store() -> None:
    resp = client.post("/tle", json={"tle_text": _ROUTE_TLE_TEXT})
    assert resp.status_code == 200


def test_conjunctions_route_accepts_a_method():
    _seed_api_store()
    resp = client.post(
        "/conjunctions", json={"hours": 2.0, "threshold_km": 25.0, "method": "spatial"}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, list)
    assert all(e["screening_method"] == "spatial" for e in payload)


def test_conjunctions_route_rejects_an_unknown_method():
    resp = client.post(
        "/conjunctions", json={"hours": 2.0, "threshold_km": 25.0, "method": "octree"}
    )
    assert resp.status_code == 422


def test_conjunctions_route_can_filter_formations():
    _seed_api_store()
    body = {"hours": 6.0, "threshold_km": 25.0}
    kept = client.post("/conjunctions", json={**body, "include_formations": True}).json()
    dropped = client.post("/conjunctions", json={**body, "include_formations": False}).json()
    assert any(e["formation_excluded"] for e in kept)
    assert not any(e["formation_excluded"] for e in dropped)
    assert len(dropped) < len(kept)


def test_survey_route_returns_a_regime_result():
    _seed_api_store()
    resp = client.post("/conjunctions/survey", json={"regime": "LEO", "hours": 2.0, "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["regime"] == "LEO"
    assert data["threshold_km"] == default_threshold_for_regime("LEO")
    assert data["screening_method"] in {"allpairs", "spatial"}
    assert len(data["events"]) <= 5
    distances = [e["miss_distance_km"] for e in data["events"]]
    assert distances == sorted(distances)


def test_survey_route_rejects_an_unknown_regime():
    resp = client.post("/conjunctions/survey", json={"regime": "CISLUNAR"})
    assert resp.status_code == 422


def test_exclusions_route_lists_the_bundled_table():
    resp = client.get("/conjunctions/exclusions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == len(BUNDLED_FORMATION_RULES)
    keys = {rule["key"] for rule in data["rules"]}
    assert {"iss_complex", "tandem_x", "grace_fo", "starlink_same_launch"} <= keys
    assert data["heuristic_limits"] == list(FORMATION_HEURISTIC_LIMITS)
    assert data["regime_default_threshold_km"]["GEO"] > (
        data["regime_default_threshold_km"]["LEO"]
    )
