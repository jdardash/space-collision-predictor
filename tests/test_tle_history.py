"""Tests for the TLE history archive, maneuver detection, replay and routes.

The synthetic element-set series are built with sda.rv2tle.format_tle_lines and
decayed at exactly the rate sda.decay predicts for each element set, so a clean
series must produce no detections and an injected burn must stand out.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.constants import EARTH_MU_KM3_S2
from sda.decay import estimate_decay
from sda.demo_tles import DEMO_TLE_TEXT
from sda.replay import replay_at, select_snapshots
from sda.routes.history import router as history_router
from sda.rv2tle import ClassicalElements, format_tle_lines
from sda.tle_history import (
    TLEHistoryStore,
    detect_decay_anomalies,
    detect_maneuvers,
    format_change_log,
    mean_elements,
    orbit_change_log,
    reset_history_store,
    robust_sigma,
    semi_major_axis_km,
)
from sda.tle_store import TLEStore

START = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
SERIES_NORAD = 90050
# Ten times the ISS drag term, so each two-day step loses close to 1 km of
# semi-major axis: a detector that ignored decay would flag every interval.
NDOT_FIELD_REV_DAY2_OVER_2 = 0.0015089
BASE_SMA_KM = 6778.0
BASE_INCLINATION_DEG = 51.6343
BASE_ECCENTRICITY = 0.0006186


def _tle_text(
    sma_km: float,
    epoch: datetime,
    inclination_deg: float = BASE_INCLINATION_DEG,
    eccentricity: float = BASE_ECCENTRICITY,
    ndot: float = NDOT_FIELD_REV_DAY2_OVER_2,
    norad_id: int = SERIES_NORAD,
    name: str = "SERIES",
) -> str:
    elements = ClassicalElements(
        semi_major_axis_km=sma_km,
        eccentricity=eccentricity,
        inclination_rad=math.radians(inclination_deg),
        raan_rad=math.radians(10.0),
        arg_perigee_rad=math.radians(220.0),
        mean_anomaly_rad=math.radians(140.0),
    )
    line1, line2 = format_tle_lines(
        norad_id, epoch, elements, ndot_rev_day2_over_2=ndot
    )
    return f"{name}\n{line1}\n{line2}"


def build_series(
    steps: int = 6,
    interval_days: float = 2.0,
    burn_at: int | None = None,
    burn_sma_km: float = 0.0,
    burn_inclination_deg: float = 0.0,
) -> str:
    """Build a decaying element-set series, optionally with one burn.

    The semi-major axis of each step is the previous one minus exactly the
    decay sda.decay predicts for that element set, so the series is consistent
    with the model the detector uses.
    """
    sma = BASE_SMA_KM
    inclination = BASE_INCLINATION_DEG
    epoch = START
    blocks: list[str] = []
    for step in range(steps):
        if burn_at is not None and step == burn_at:
            sma += burn_sma_km
            inclination += burn_inclination_deg
        text = _tle_text(sma, epoch, inclination_deg=inclination)
        blocks.append(text)
        staging = TLEStore()
        assert staging.load_from_text(text) == 1
        record = staging.get(SERIES_NORAD)
        assert record is not None
        rate = estimate_decay(record).decay_rate_km_per_day
        sma -= rate * interval_days
        epoch += timedelta(days=interval_days)
    return "\n".join(blocks)


@pytest.fixture
def clean_history() -> TLEHistoryStore:
    history = TLEHistoryStore(load_existing=False)
    history.add_from_text(build_series())
    return history


@pytest.fixture
def burn_history() -> TLEHistoryStore:
    history = TLEHistoryStore(load_existing=False)
    history.add_from_text(build_series(burn_at=3, burn_sma_km=3.0))
    return history


# --------------------------------------------------------------------------
# Element helpers
# --------------------------------------------------------------------------


def test_semi_major_axis_matches_the_kepler_relation():
    # A 90 minute orbit: n = 16 rev/day.
    sma = semi_major_axis_km(16.0)
    period_s = 2.0 * math.pi * math.sqrt(sma**3 / EARTH_MU_KM3_S2)
    assert period_s == pytest.approx(86400.0 / 16.0, rel=1e-9)
    with pytest.raises(ValueError):
        semi_major_axis_km(0.0)


def test_mean_elements_read_the_tle_fields(clean_history: TLEHistoryStore):
    snapshot = clean_history.history(SERIES_NORAD)[0]
    elements = mean_elements(snapshot)
    assert elements.semi_major_axis_km == pytest.approx(BASE_SMA_KM, abs=0.01)
    assert elements.inclination_deg == pytest.approx(BASE_INCLINATION_DEG, abs=1e-4)
    assert elements.eccentricity == pytest.approx(BASE_ECCENTRICITY, abs=1e-7)
    assert elements.ndot_rev_day2 == pytest.approx(2.0 * NDOT_FIELD_REV_DAY2_OVER_2, rel=1e-6)
    assert elements.apogee_km > elements.perigee_km


def test_robust_sigma_ignores_a_single_outlier():
    quiet = [0.0, 0.01, -0.01, 0.005, -0.005]
    assert robust_sigma(quiet) < 0.02
    assert robust_sigma([*quiet, 50.0]) < 0.05
    assert robust_sigma([1.0, 2.0]) == 0.0


# --------------------------------------------------------------------------
# Snapshot store
# --------------------------------------------------------------------------


def test_store_keys_on_norad_id_and_epoch(clean_history: TLEHistoryStore):
    history = clean_history.history(SERIES_NORAD)
    assert len(history) == 6
    epochs = [snapshot.epoch for snapshot in history]
    assert epochs == sorted(epochs)
    assert clean_history.count() == 6
    assert clean_history.object_count() == 1
    assert clean_history.norad_ids() == [SERIES_NORAD]


def test_repeated_snapshots_of_the_same_epoch_are_not_duplicated():
    history = TLEHistoryStore(load_existing=False)
    text = build_series(steps=3)
    assert history.add_from_text(text) == 3
    assert history.add_from_text(text) == 0
    assert history.count() == 3


def test_record_snapshot_archives_a_live_catalog():
    store = TLEStore()
    loaded = store.load_from_text(DEMO_TLE_TEXT)
    history = TLEHistoryStore(load_existing=False)
    assert history.record_snapshot(store) == loaded
    assert history.object_count() == loaded
    assert history.record_snapshot(store) == 0


def test_latest_at_picks_the_newest_element_set_at_or_before_the_epoch(
    clean_history: TLEHistoryStore,
):
    snapshots = clean_history.history(SERIES_NORAD)
    target = snapshots[2].epoch + timedelta(hours=6)
    chosen = clean_history.latest_at(SERIES_NORAD, target)
    assert chosen is not None
    assert chosen.epoch == snapshots[2].epoch
    assert clean_history.latest_at(SERIES_NORAD, START - timedelta(days=1)) is None


def test_jsonl_persistence_round_trip(tmp_path):
    directory = tmp_path / "history"
    first = TLEHistoryStore(directory=directory)
    first.add_from_text(build_series(steps=4))
    assert (directory / f"{SERIES_NORAD}.jsonl").exists()

    second = TLEHistoryStore(directory=directory)
    assert second.count() == 4
    assert [s.epoch for s in second.history(SERIES_NORAD)] == [
        s.epoch for s in first.history(SERIES_NORAD)
    ]
    assert second.persistent is True
    # Reloading must not duplicate what is already in memory.
    assert second.load_persisted() == 0


def test_history_directory_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SDA_HISTORY_DIR", str(tmp_path / "env_history"))
    history = TLEHistoryStore()
    assert history.directory is not None
    assert history.directory.is_dir()
    history.add_from_text(build_series(steps=2))
    assert (history.directory / f"{SERIES_NORAD}.jsonl").exists()


def test_store_is_in_memory_without_a_directory(monkeypatch):
    monkeypatch.delenv("SDA_HISTORY_DIR", raising=False)
    history = TLEHistoryStore()
    assert history.persistent is False
    assert history.directory is None


# --------------------------------------------------------------------------
# Maneuver detection
# --------------------------------------------------------------------------


def test_clean_decaying_series_produces_no_false_positives(clean_history: TLEHistoryStore):
    snapshots = clean_history.history(SERIES_NORAD)
    # The raw drift is large enough that ignoring decay would flag every step.
    first = mean_elements(snapshots[0])
    second = mean_elements(snapshots[1])
    assert first.semi_major_axis_km - second.semi_major_axis_km > 0.5
    assert detect_maneuvers(snapshots) == []
    assert clean_history.maneuvers(SERIES_NORAD) == []


def test_injected_delta_v_is_detected_with_the_right_magnitude(
    burn_history: TLEHistoryStore,
):
    snapshots = burn_history.history(SERIES_NORAD)
    events = detect_maneuvers(snapshots)
    assert len(events) == 1
    event = events[0]
    assert event.norad_id == SERIES_NORAD
    assert event.start_epoch == snapshots[2].epoch
    assert event.end_epoch == snapshots[3].epoch
    assert event.residual_semi_major_axis_km == pytest.approx(3.0, abs=0.05)
    assert event.expected_decay_km > 0.5

    # Vis-viva: dv = v |da| / (2a) with v = sqrt(mu / a).
    speed_m_s = 1000.0 * math.sqrt(EARTH_MU_KM3_S2 / BASE_SMA_KM)
    expected_dv = speed_m_s * 3.0 / (2.0 * BASE_SMA_KM)
    assert event.delta_v_along_track_m_s == pytest.approx(expected_dv, rel=0.02)
    assert event.delta_v_total_m_s == pytest.approx(expected_dv, rel=0.05)
    assert event.reasons
    assert event.confidence in {"LOW", "MODERATE", "HIGH"}


def test_plane_change_is_detected_cross_track():
    history = TLEHistoryStore(load_existing=False)
    history.add_from_text(build_series(burn_at=2, burn_inclination_deg=0.05))
    events = detect_maneuvers(history.history(SERIES_NORAD))
    assert len(events) == 1
    event = events[0]
    assert event.delta_inclination_deg == pytest.approx(0.05, abs=2e-4)
    speed_m_s = 1000.0 * math.sqrt(EARTH_MU_KM3_S2 / BASE_SMA_KM)
    expected_dv = 2.0 * speed_m_s * math.sin(math.radians(0.05) / 2.0)
    assert event.delta_v_cross_track_m_s == pytest.approx(expected_dv, rel=0.02)


def test_a_tiny_change_stays_under_the_absolute_floor():
    history = TLEHistoryStore(load_existing=False)
    history.add_from_text(build_series(burn_at=3, burn_sma_km=0.05))
    assert detect_maneuvers(history.history(SERIES_NORAD)) == []


def test_single_snapshot_history_yields_nothing():
    history = TLEHistoryStore(load_existing=False)
    history.add_from_text(build_series(steps=1))
    assert detect_maneuvers(history.history(SERIES_NORAD)) == []
    assert detect_decay_anomalies(history.history(SERIES_NORAD)) == []
    assert orbit_change_log(history.history(SERIES_NORAD)) == []


# --------------------------------------------------------------------------
# Decay anomaly and change log
# --------------------------------------------------------------------------


def test_clean_series_raises_no_decay_anomaly(clean_history: TLEHistoryStore):
    assert detect_decay_anomalies(clean_history.history(SERIES_NORAD)) == []


def test_drag_term_jump_raises_a_decay_anomaly():
    history = TLEHistoryStore(load_existing=False)
    history.add_from_text(_tle_text(BASE_SMA_KM, START))
    history.add_from_text(
        _tle_text(
            BASE_SMA_KM - 1.0,
            START + timedelta(days=2),
            ndot=NDOT_FIELD_REV_DAY2_OVER_2 * 6.0,
        )
    )
    anomalies = detect_decay_anomalies(history.history(SERIES_NORAD))
    assert len(anomalies) == 1
    assert anomalies[0].reasons
    assert anomalies[0].norad_id == SERIES_NORAD


def test_orbit_change_log_merges_events(burn_history: TLEHistoryStore):
    entries = orbit_change_log(burn_history.history(SERIES_NORAD))
    assert len(entries) >= 1
    assert entries[0].kind == "maneuver"
    assert entries[0].delta_v_m_s > 0.0
    text = format_change_log(entries)
    assert "maneuver" in text
    assert text.isascii()
    assert entries == sorted(entries, key=lambda entry: (entry.start_epoch, entry.kind))


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def test_replay_uses_the_element_set_current_at_the_epoch(clean_history: TLEHistoryStore):
    snapshots = clean_history.history(SERIES_NORAD)
    target = snapshots[2].epoch + timedelta(hours=3)
    selected, missing = select_snapshots(clean_history, target)
    assert missing == []
    assert [s.epoch for s in selected] == [snapshots[2].epoch]

    result = replay_at(clean_history, target)
    assert result.epoch == target
    assert result.requested == 1
    assert result.resolved == 1
    obj = result.objects[0]
    assert obj.norad_id == SERIES_NORAD
    assert obj.element_epoch == snapshots[2].epoch
    assert obj.element_age_days == pytest.approx(0.125, abs=1e-6)
    assert math.isfinite(obj.position_km[0])
    assert 6000.0 < math.dist(obj.position_km, (0.0, 0.0, 0.0)) < 8000.0
    assert result.screened is False


def test_replay_reports_objects_with_no_element_set(clean_history: TLEHistoryStore):
    result = replay_at(clean_history, START - timedelta(days=5), norad_ids=[SERIES_NORAD])
    assert result.resolved == 0
    assert result.missing == [SERIES_NORAD]
    assert result.objects == []


def test_replay_screen_calls_the_conjunction_entry_point():
    store = TLEStore()
    store.load_from_text(DEMO_TLE_TEXT)
    history = TLEHistoryStore(load_existing=False)
    history.record_snapshot(store)
    epoch = min(snapshot.epoch for snapshot in history.history(25544)) + timedelta(hours=2)

    result = replay_at(history, epoch, norad_ids=[25544, 20580], screen=True, hours=1.0)
    assert result.resolved == 2
    # The screen is optional infrastructure: either it ran, or the failure was
    # reported rather than raised.
    assert result.screened or result.warnings


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SDA_HISTORY_DIR", raising=False)
    reset_history_store()

    import sda.api as api

    api.store.load_from_text(DEMO_TLE_TEXT)

    app = FastAPI()
    app.include_router(history_router)
    with TestClient(app) as test_client:
        yield test_client

    reset_history_store()


def test_snapshot_and_history_endpoints(client: TestClient):
    response = client.post("/tle/history/snapshot", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["recorded"] > 0
    assert body["total_snapshots"] == body["recorded"]
    assert body["persistent"] is False

    response = client.get("/tle/history/25544")
    assert response.status_code == 200
    body = response.json()
    assert body["norad_id"] == 25544
    assert body["count"] == 1
    assert body["entries"][0]["line1"].startswith("1 25544")

    assert client.get("/tle/history/999999").status_code == 404


def test_snapshot_endpoint_accepts_tle_text(client: TestClient):
    response = client.post(
        "/tle/history/snapshot", json={"tle_text": build_series(burn_at=3, burn_sma_km=3.0)}
    )
    assert response.status_code == 200
    assert response.json()["recorded"] == 6

    response = client.get(f"/tle/history/{SERIES_NORAD}/maneuvers")
    assert response.status_code == 200
    body = response.json()
    assert body["snapshots"] == 6
    assert len(body["maneuvers"]) == 1
    assert body["maneuvers"][0]["delta_v_total_m_s"] > 0.0
    assert len(body["change_log"]) >= 1

    assert client.get("/tle/history/999999/maneuvers").status_code == 404


def test_replay_endpoint(client: TestClient):
    client.post("/tle/history/snapshot", json={})
    history_response = client.get("/tle/history/25544").json()
    epoch = history_response["entries"][0]["epoch"]

    response = client.post(
        "/replay",
        json={"epoch": epoch, "norad_ids": [25544, 20580], "screen": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 2
    assert body["resolved"] == 2
    assert body["screened"] is False
    assert len(body["objects"]) == 2
    assert all(len(obj["position_km"]) == 3 for obj in body["objects"])


def test_replay_endpoint_without_history_returns_404(client: TestClient):
    response = client.post("/replay", json={"epoch": START.isoformat()})
    assert response.status_code == 404


def test_breakup_endpoint_on_a_catalog_object(client: TestClient):
    response = client.post(
        "/breakup/simulate",
        json={
            "norad_id": 25544,
            "event_type": "collision",
            "parent_mass_kg": 1000.0,
            "projectile_mass_kg": 10.0,
            "relative_speed_km_s": 10.0,
            "max_fragments": 25,
            "seed": 4,
            "include_tles": True,
            "max_tle_objects": 25,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["catastrophic"] is True
    assert body["specific_energy_j_per_g"] == pytest.approx(500.0)
    assert body["n_above_1cm"] > body["n_above_10cm"] > 0.0
    assert body["fragments_sampled"] == 25
    assert len(body["gabbard"]) == 25
    assert body["tle_text"] is not None
    assert body["tles_fitted"] + body["tles_skipped"] == 25
    if body["tles_fitted"]:
        store = TLEStore()
        assert store.load_from_text(body["tle_text"]) == body["tles_fitted"]


def test_breakup_endpoint_validates_its_input(client: TestClient):
    assert client.post("/breakup/simulate", json={"norad_id": 999999}).status_code == 404
    assert client.post("/breakup/simulate", json={}).status_code == 422


def test_breakup_endpoint_accepts_an_explicit_state(client: TestClient):
    response = client.post(
        "/breakup/simulate",
        json={
            "position_km": [7000.0, 0.0, 0.0],
            "velocity_km_s": [0.0, 7.546, 0.0],
            "epoch": START.isoformat(),
            "event_type": "explosion",
            "parent_mass_kg": 1500.0,
            "max_fragments": 20,
            "seed": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["catastrophic"] is False
    assert body["fragments_sampled"] == 20
    assert body["summary"]["total_sampled_mass_kg"] > 0.0
