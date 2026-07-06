"""Coverage-gap tests for tle_store.py and conjunction.py.

Covers TLE freshness branches, parser edge cases, and the conjunction
pipeline internals (empty propagation, coarse-screen skip, fine refinement).
Offline-deterministic: no network, no wall-clock dependence beyond epoch math.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

import sda.conjunction as conj
from sda.conjunction import (
    _refine_conjunction,
    _tle_age_hours,
    clear_conjunction_history,
    find_conjunctions,
    get_conjunction_history,
)
from sda.models import RiskLevel, TLERecord
from sda.tle_store import TLEStore, compute_freshness

# Canonical ISS TLE (NORAD 25544), epoch 2024-02-14 ~12:25 UTC
ISS_NAME = "ISS (ZARYA)"
ISS_LINE1 = "1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997"
ISS_LINE2 = "2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"

# Clone of the ISS orbit with a different NORAD ID and a tiny mean-anomaly
# offset (+0.03 deg ~ 3.5 km along-track) — guarantees a close approach.
CLONE_LINE1 = "1 25545U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997"
CLONE_LINE2 = "2 25545  51.6412 210.9280 0004885 231.2372 247.0642 15.49584387440014"

EPOCH_START = datetime(2024, 2, 14, 13, 0, tzinfo=UTC)


def _make_record(norad_id: int, epoch: datetime) -> TLERecord:
    return TLERecord(
        norad_id=norad_id,
        name=f"TEST {norad_id}",
        line1=ISS_LINE1,
        line2=ISS_LINE2,
        epoch=epoch,
    )


def _loaded_store(text: str) -> TLEStore:
    store = TLEStore()
    store.load_from_text(text)
    return store


@pytest.fixture()
def clean_history():
    """Snapshot and restore the module-level conjunction history."""
    saved = list(conj._conjunction_history)
    conj._conjunction_history.clear()
    yield
    conj._conjunction_history.clear()
    conj._conjunction_history.extend(saved)


# --- tle_store: get_freshness ---


def test_get_freshness_unknown_satellite_returns_none():
    store = TLEStore()
    assert store.get_freshness(999999) is None


def test_get_freshness_known_satellite():
    store = _loaded_store(f"{ISS_NAME}\n{ISS_LINE1}\n{ISS_LINE2}")
    freshness = store.get_freshness(25544)
    assert freshness is not None
    assert freshness.norad_id == 25544
    assert freshness.freshness in {"FRESH", "AGING", "STALE", "EXPIRED"}


# --- tle_store: parser edge cases ---


def test_load_from_text_two_line_without_name():
    store = _loaded_store(f"{ISS_LINE1}\n{ISS_LINE2}")
    assert store.count() == 1
    record = store.get(25544)
    assert record is not None
    assert record.name == "OBJECT 25544"


def test_load_from_text_skips_garbage_lines():
    raw = f"random garbage line\n{ISS_NAME}\n{ISS_LINE1}\n{ISS_LINE2}\ntrailing junk"
    store = _loaded_store(raw)
    assert store.count() == 1
    assert store.get(25544).name == ISS_NAME


def test_load_from_text_only_garbage_ingests_nothing():
    store = _loaded_store("this is not a TLE\nneither is this\nnor this")
    assert store.count() == 0


def test_load_from_text_parse_exception_is_skipped():
    # Lines pass the "1 "/"2 " prefix check but the catalog number is not
    # numeric, so parsing raises and the pair is skipped without crashing.
    bad = "1 XXXXX bad line one\n2 XXXXX bad line two"
    store = _loaded_store(bad)
    assert store.count() == 0

    # A bad pair followed by a valid set: the valid set still ingests
    store2 = _loaded_store(f"{bad}\n{ISS_LINE1}\n{ISS_LINE2}")
    assert store2.count() == 1
    assert store2.get(25544) is not None


# --- tle_store: compute_freshness branches ---


def test_compute_freshness_fresh():
    record = _make_record(90001, datetime.now(UTC) - timedelta(hours=1))
    freshness = compute_freshness(record)
    assert freshness.freshness == "FRESH"
    assert freshness.accuracy_warning is None
    assert 0.5 < freshness.age_hours < 2.0


def test_compute_freshness_aging():
    record = _make_record(90002, datetime.now(UTC) - timedelta(hours=100))
    freshness = compute_freshness(record)
    assert freshness.freshness == "AGING"
    assert freshness.accuracy_warning is not None


def test_compute_freshness_stale():
    record = _make_record(90003, datetime.now(UTC) - timedelta(hours=200))
    freshness = compute_freshness(record)
    assert freshness.freshness == "STALE"
    assert "stale" in freshness.accuracy_warning.lower()


def test_compute_freshness_expired():
    record = _make_record(90004, datetime.now(UTC) - timedelta(hours=400))
    freshness = compute_freshness(record)
    assert freshness.freshness == "EXPIRED"
    assert freshness.accuracy_warning is not None
    assert freshness.age_days == pytest.approx(400 / 24.0, abs=0.1)


def test_compute_freshness_naive_epoch():
    # Naive epochs are treated as UTC
    naive_now = datetime.now(UTC).replace(tzinfo=None)
    record = _make_record(90005, naive_now - timedelta(hours=1))
    assert compute_freshness(record).freshness == "FRESH"


# --- conjunction: _tle_age_hours ---


def test_tle_age_hours_naive_inputs_and_clamping():
    now = datetime.now(UTC)
    naive_now = now.replace(tzinfo=None)
    record = _make_record(90010, now - timedelta(hours=10))

    # Aware epoch, naive `at`
    age = _tle_age_hours(record, naive_now)
    assert age == pytest.approx(10.0, abs=0.1)

    # Naive epoch, aware `at`
    naive_record = _make_record(90011, naive_now - timedelta(hours=5))
    age = _tle_age_hours(naive_record, now)
    assert age == pytest.approx(5.0, abs=0.1)

    # Future epoch clamps to zero
    future_record = _make_record(90012, now + timedelta(hours=48))
    assert _tle_age_hours(future_record, now) == 0.0


# --- conjunction: empty-propagation branches ---


def test_refine_conjunction_empty_propagation(monkeypatch):
    def empty_propagation(tle, start, hours=24.0, step_seconds=60.0):
        return np.zeros((0, 3)), np.zeros((0, 3)), []

    monkeypatch.setattr(conj, "propagate_window_numpy", empty_propagation)

    tle_a = _make_record(25544, EPOCH_START)
    tle_b = _make_record(25545, EPOCH_START)
    coarse_tca = EPOCH_START + timedelta(minutes=10)

    tca, miss, rel_vel, pos_p, vel_p, pos_s, vel_s = _refine_conjunction(
        tle_a, tle_b, coarse_tca
    )
    assert tca == coarse_tca
    assert miss == float("inf")
    assert rel_vel == 0.0
    assert pos_p == (0, 0, 0)
    assert vel_s == (0, 0, 0)


def test_find_conjunctions_skips_pairs_with_empty_positions(monkeypatch, clean_history):
    # Non-empty times but zero-length position arrays exercise the coarse
    # screening n == 0 continue branch.
    def degenerate_propagation(tle, start, hours=24.0, step_seconds=60.0):
        return np.zeros((0, 3)), np.zeros((0, 3)), [start]

    monkeypatch.setattr(conj, "propagate_window_numpy", degenerate_propagation)

    store = TLEStore()
    store.upsert(_make_record(25544, EPOCH_START))
    store.upsert(_make_record(25545, EPOCH_START))

    events = find_conjunctions(store, hours=0.1, start=EPOCH_START)
    assert events == []


# --- conjunction: fine refinement loop with a real close pair ---


def test_find_conjunctions_detects_close_pair(clean_history):
    store = _loaded_store(
        f"{ISS_NAME}\n{ISS_LINE1}\n{ISS_LINE2}\n"
        f"ISS CLONE\n{CLONE_LINE1}\n{CLONE_LINE2}"
    )
    assert store.count() == 2

    events = find_conjunctions(
        store, hours=0.5, threshold_km=10.0, start=EPOCH_START
    )

    assert len(events) >= 1
    event = events[0]
    assert {event.primary, event.secondary} == {25544, 25545}
    assert isinstance(event.risk, RiskLevel)
    assert 0.0 <= event.miss_distance_km <= 10.0
    assert event.relative_velocity_km_s >= 0.0
    assert event.primary_name in {ISS_NAME, "ISS CLONE"}

    # Events are recorded in history and filterable by NORAD ID
    history = get_conjunction_history()
    assert len(history) >= 1
    assert get_conjunction_history(norad_id=25544)
    assert get_conjunction_history(norad_id=424242) == []

    cleared = clear_conjunction_history()
    assert cleared >= 1
    assert get_conjunction_history() == []


def test_find_conjunctions_without_probability(clean_history):
    store = _loaded_store(
        f"{ISS_NAME}\n{ISS_LINE1}\n{ISS_LINE2}\n"
        f"ISS CLONE\n{CLONE_LINE1}\n{CLONE_LINE2}"
    )
    events = find_conjunctions(
        store,
        hours=0.5,
        threshold_km=10.0,
        start=EPOCH_START,
        compute_probability=False,
    )
    assert len(events) >= 1
    assert all(event.collision_probability is None for event in events)


def test_find_conjunctions_pc_failure_yields_event_without_probability(
    monkeypatch, clean_history
):
    """A Pc computation failure is logged and the event still returned."""

    def broken_pc(**kwargs):
        raise ValueError("singular covariance")

    monkeypatch.setattr(conj, "compute_pc_for_conjunction", broken_pc)

    store = _loaded_store(
        f"{ISS_NAME}\n{ISS_LINE1}\n{ISS_LINE2}\n"
        f"ISS CLONE\n{CLONE_LINE1}\n{CLONE_LINE2}"
    )
    events = find_conjunctions(store, hours=0.5, threshold_km=10.0, start=EPOCH_START)
    assert len(events) >= 1
    assert all(event.collision_probability is None for event in events)


def test_find_conjunctions_fewer_than_two_satellites(clean_history):
    store = _loaded_store(f"{ISS_NAME}\n{ISS_LINE1}\n{ISS_LINE2}")
    assert find_conjunctions(store, hours=0.5, start=EPOCH_START) == []
