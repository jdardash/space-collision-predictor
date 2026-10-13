"""Lifecycle, background-task, and cache tests for sda.api.

All tests are offline-deterministic: httpx.AsyncClient is replaced with a
fake client returning canned responses (or canned failures), so no real
network calls ever happen. Async helpers are driven with asyncio.run()
inside synchronous tests (pytest-asyncio is not a dependency).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sda.api as api
from sda.api import app

# Sample TLE text (ISS), same canonical test case as test_api.py
SAMPLE_TLE = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"""


@pytest.fixture(autouse=True)
def isolate_module_state():
    """Snapshot and restore module-level singleton state around each test."""
    saved_catalog = dict(api.store._catalog)
    saved_cache = dict(api._cache)
    saved_f107 = api._live_f107
    saved_refresh = api._last_tle_refresh
    saved_startup = api._metrics["startup_time"]
    yield
    api.store._catalog.clear()
    api.store._catalog.update(saved_catalog)
    api._cache.clear()
    api._cache.update(saved_cache)
    api._live_f107 = saved_f107
    api._last_tle_refresh = saved_refresh
    api._metrics["startup_time"] = saved_startup


CACHE_ENV = "SDA_TLE_CACHE_DIR"


@pytest.fixture(autouse=True)
def clear_tle_cache_env(monkeypatch):
    """A developer's real SDA_TLE_CACHE_DIR must never reach these tests.

    Without this, the fetch tests below would overwrite a real <group>.tle with the
    one-satellite fixture text.
    """
    monkeypatch.delenv(CACHE_ENV, raising=False)


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        return self._json


def make_fake_client(get_handler, init_exc: Exception | None = None):
    """Build a fake httpx.AsyncClient class whose get() delegates to get_handler(url)."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            if init_exc is not None:
                raise init_exc

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return get_handler(url)

    return _FakeAsyncClient


# --- cache helpers ---


def test_set_and_get_cached_within_ttl():
    api._cache.clear()
    api._set_cached("k", {"value": 42})
    assert api._get_cached("k", ttl_sec=60.0) == {"value": 42}


def test_get_cached_expired_entry():
    api._cache.clear()
    api._set_cached("k", {"value": 42})
    api._cache["k"]["ts"] -= 100.0  # age the entry past any small TTL
    assert api._get_cached("k", ttl_sec=1.0) is None


def test_get_cached_missing_key():
    api._cache.clear()
    assert api._get_cached("nonexistent", ttl_sec=60.0) is None


# --- _fetch_celestrak_group ---


def test_fetch_celestrak_group_success():
    api.store._catalog.clear()
    client = make_fake_client(lambda url: FakeResponse(200, SAMPLE_TLE))()
    count = asyncio.run(api._fetch_celestrak_group(client, "stations"))
    assert count == 1
    assert api.store.get(25544) is not None


def test_fetch_celestrak_group_non_200():
    client = make_fake_client(lambda url: FakeResponse(500, "server error"))()
    assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 0


def test_fetch_celestrak_group_empty_body():
    client = make_fake_client(lambda url: FakeResponse(200, "   \n  "))()
    assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 0


def test_fetch_celestrak_group_network_error():
    def _raise(url):
        raise RuntimeError("connection refused")

    client = make_fake_client(_raise)()
    assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 0


# --- _seed_catalog ---


def test_seed_catalog_live_success(monkeypatch):
    api.store._catalog.clear()
    api._last_tle_refresh = None
    fake_cls = make_fake_client(lambda url: FakeResponse(200, SAMPLE_TLE))
    monkeypatch.setattr(api.httpx, "AsyncClient", fake_cls)

    asyncio.run(api._seed_catalog())

    # Only the ISS from the canned live response; demo fallback not triggered
    assert api.store.count() == 1
    assert api.store.get(25544) is not None
    assert api._last_tle_refresh is not None


def test_seed_catalog_falls_back_to_demo_on_fetch_failure(monkeypatch):
    api.store._catalog.clear()
    api._last_tle_refresh = None

    def _raise(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(_raise))

    asyncio.run(api._seed_catalog())

    # All groups failed -> bundled DEMO_TLE_TEXT loaded
    assert api.store.count() > 10
    assert api.store.get(25544) is not None
    # Nothing live arrived, so there is no refresh to report.
    assert api._last_tle_refresh is None


def test_seed_catalog_falls_back_to_demo_on_client_error(monkeypatch):
    api.store._catalog.clear()
    fake_cls = make_fake_client(
        lambda url: FakeResponse(200, SAMPLE_TLE),
        init_exc=RuntimeError("cannot create client"),
    )
    monkeypatch.setattr(api.httpx, "AsyncClient", fake_cls)

    asyncio.run(api._seed_catalog())

    assert api.store.count() > 10  # demo fallback


# --- _fetch_live_f107 ---


def test_fetch_live_f107_observed_flux(monkeypatch):
    api._live_f107 = 150.0

    def handler(url):
        if url == api.NOAA_F107_OBSERVED_URL:
            return FakeResponse(200, json_data=[{"flux": 111.0}, {"flux": 142.5}])
        return FakeResponse(500)

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(handler))
    asyncio.run(api._fetch_live_f107())
    assert api._live_f107 == 142.5


def test_fetch_live_f107_predicted_fallback(monkeypatch):
    api._live_f107 = 150.0
    now_str = datetime.now(UTC).strftime("%Y-%m")

    def handler(url):
        if url == api.NOAA_F107_OBSERVED_URL:
            return FakeResponse(500)
        return FakeResponse(
            200,
            json_data=[
                {"time-tag": "1999-01", "predicted_ssn": 55.0},
                {"time-tag": now_str, "predicted_ssn": 100.0},
            ],
        )

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(handler))
    asyncio.run(api._fetch_live_f107())
    assert api._live_f107 == pytest.approx(67.0 + 0.572 * 100.0)


def test_fetch_live_f107_out_of_range_flux_no_match(monkeypatch):
    """Out-of-range observed flux and no matching predicted month leaves value unchanged."""
    api._live_f107 = 150.0

    def handler(url):
        if url == api.NOAA_F107_OBSERVED_URL:
            return FakeResponse(200, json_data=[{"flux": 999.0}])
        return FakeResponse(200, json_data=[{"time-tag": "1999-01", "predicted_ssn": 55.0}])

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(handler))
    asyncio.run(api._fetch_live_f107())
    assert api._live_f107 == 150.0


def test_fetch_live_f107_network_error(monkeypatch):
    api._live_f107 = 150.0

    def _raise(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(_raise))
    asyncio.run(api._fetch_live_f107())
    assert api._live_f107 == 150.0


# --- background refresh loop ---


def test_background_refresh_loop_refreshes_and_periodically_fetches_f107(monkeypatch):
    seed_calls: list[int] = []
    f107_calls: list[int] = []
    sleep_calls: list[float] = []

    async def fake_seed():
        seed_calls.append(1)

    async def fake_f107():
        f107_calls.append(1)

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(api, "_seed_catalog", fake_seed)
    monkeypatch.setattr(api, "_fetch_live_f107", fake_f107)
    # Only asyncio.sleep is used inside the loop
    monkeypatch.setattr(api, "asyncio", SimpleNamespace(sleep=fake_sleep))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(api._background_refresh_loop())

    assert len(seed_calls) == 3
    assert len(f107_calls) == 1  # every 3rd cycle
    assert all(s == 7500 for s in sleep_calls)


def test_initial_live_fetch_calls_seed_and_f107(monkeypatch):
    calls: list[str] = []

    async def fake_seed():
        calls.append("seed")

    async def fake_f107():
        calls.append("f107")

    monkeypatch.setattr(api, "_seed_catalog", fake_seed)
    monkeypatch.setattr(api, "_fetch_live_f107", fake_f107)

    asyncio.run(api._initial_live_fetch())
    assert calls == ["seed", "f107"]


# --- lifespan ---


def test_lifespan_loads_demo_tles_and_sets_startup_time(monkeypatch):
    async def noop_fetch():
        return None

    async def noop_loop():
        return None

    # No network task must ever start
    monkeypatch.setattr(api, "_initial_live_fetch", noop_fetch)
    monkeypatch.setattr(api, "_background_refresh_loop", noop_loop)

    api.store._catalog.clear()
    api._metrics["startup_time"] = None

    with TestClient(app) as client:
        assert api._metrics["startup_time"] is not None
        # Bundled demo TLEs loaded synchronously at startup
        assert api.store.count() > 10
        assert api.store.get(25544) is not None
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    # Exiting the context runs the shutdown path (task cancellation) cleanly


def _count_background_starts(monkeypatch) -> list[str]:
    started: list[str] = []

    async def fetch():
        started.append("fetch")

    async def loop():
        started.append("loop")

    monkeypatch.setattr(api, "_initial_live_fetch", fetch)
    monkeypatch.setattr(api, "_background_refresh_loop", loop)
    return started


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_offline_start_serves_the_bundled_catalog_and_starts_no_fetch(monkeypatch, value):
    """SDA_OFFLINE is what the static demo build bakes under."""
    started = _count_background_starts(monkeypatch)
    monkeypatch.setenv("SDA_OFFLINE", value)
    api.store._catalog.clear()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert api.store.get(25544) is not None

    assert started == []


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
def test_without_the_offline_switch_both_background_tasks_start(monkeypatch, value):
    started = _count_background_starts(monkeypatch)
    if value is None:
        monkeypatch.delenv("SDA_OFFLINE", raising=False)
    else:
        monkeypatch.setenv("SDA_OFFLINE", value)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert sorted(started) == ["fetch", "loop"]


# --- disk cache of the last good catalog (SDA_TLE_CACHE_DIR) ---


def _checksum(line: str) -> int:
    return sum(int(c) if c.isdigit() else 1 if c == "-" else 0 for c in line[:68]) % 10


def make_tle(norad_id: int, name: str = "CACHED SAT", epoch: str = "24045.51782528") -> str:
    """3-line TLE with a valid checksum for any NORAD id and epoch (YYDDD.DDDDDDDD)."""
    ident = f"{norad_id:05d}"
    line1 = f"1 {ident}U 98067A   {epoch}  .00012516  00000+0  22596-3 0  999"
    line2 = f"2 {ident}  51.6412 210.9280 0004885 231.2372 247.0342 15.4958438744001"
    return f"{name}\n{line1}{_checksum(line1)}\n{line2}{_checksum(line2)}"


def age_file(path, hours: float) -> None:
    stamp = time.time() - hours * 3600.0
    os.utime(path, (stamp, stamp))


@pytest.fixture
def offline_lifespan(monkeypatch):
    """Run the real lifespan startup without starting any network task."""

    async def noop():
        return None

    monkeypatch.setattr(api, "_initial_live_fetch", noop)
    monkeypatch.setattr(api, "_background_refresh_loop", noop)


@pytest.mark.parametrize("value", [None, ""])
def test_cache_disabled_reads_and_writes_nothing(tmp_path, monkeypatch, offline_lifespan, value):
    """Unset or empty means off. An empty value must not fall back to the working directory."""
    if value is not None:
        monkeypatch.setenv(CACHE_ENV, value)
    monkeypatch.chdir(tmp_path)
    stray = tmp_path / "stray.tle"
    stray.write_text(make_tle(90001), encoding="utf-8")
    api.store._catalog.clear()

    with TestClient(app):
        assert api.store.get(90001) is None  # startup read nothing

    client = make_fake_client(lambda url: FakeResponse(200, SAMPLE_TLE))()
    assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["stray.tle"]  # fetch wrote nothing
    assert stray.read_text(encoding="utf-8") == make_tle(90001)


def test_fetch_writes_group_file_and_creates_missing_dir(tmp_path, monkeypatch):
    cache_dir = tmp_path / "nested" / "tle-cache"
    monkeypatch.setenv(CACHE_ENV, str(cache_dir))
    api.store._catalog.clear()
    client = make_fake_client(lambda url: FakeResponse(200, SAMPLE_TLE))()

    assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 1

    # Exactly one file: no temp file left behind by the atomic write
    assert [p.name for p in cache_dir.iterdir()] == ["stations.tle"]
    assert (cache_dir / "stations.tle").read_text(encoding="utf-8") == SAMPLE_TLE


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, "GP data has not updated since your last successful download."),
        (500, "server error"),
        (200, "   \n  "),
        (200, "this is not a TLE"),
    ],
)
def test_failed_fetch_leaves_cached_group_untouched(tmp_path, monkeypatch, status, body):
    """A 403 or an empty or garbage 200 must never overwrite the last good copy."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    good = make_tle(90001)
    kept = tmp_path / "stations.tle"
    kept.write_text(good, encoding="utf-8")
    # Past the repeat window, or the request below is never made and the test
    # passes without the failed reply ever reaching the cache code.
    age_file(kept, hours=3.0)
    seen: list[str] = []

    def handler(url):
        seen.append(url)
        return FakeResponse(status, body)

    client = make_fake_client(handler)()

    assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 0

    assert len(seen) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stations.refused", "stations.tle"]
    assert kept.read_text(encoding="utf-8") == good


def test_cache_write_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so the cache dir below it cannot be created")
    monkeypatch.setenv(CACHE_ENV, str(blocker / "cache"))
    api.store._catalog.clear()
    client = make_fake_client(lambda url: FakeResponse(200, SAMPLE_TLE))()

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(api._fetch_celestrak_group(client, "stations")) == 1

    assert api.store.get(25544) is not None  # the fetch itself still succeeded
    assert "stations.tle" in caplog.text


def test_startup_with_cache_and_failing_network_keeps_cached_objects(
    tmp_path, monkeypatch, offline_lifespan
):
    cached_iss = make_tle(25544, "ISS (ZARYA)", epoch="26200.50000000")  # newer than the demo
    (tmp_path / "active.tle").write_text(make_tle(90001), encoding="utf-8")
    (tmp_path / "running.tle").write_text(cached_iss, encoding="utf-8")
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))

    def _raise(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(_raise))
    api.store._catalog.clear()

    with TestClient(app):
        # Startup order: bundled demo first, then the cache over it
        assert api.store.count() > 10
        assert api.store.get(90001) is not None
        iss = api.store.get(25544)
        assert iss is not None
        assert iss.line1 == cached_iss.splitlines()[1]

        # Every group now fails: the demo fallback must not undo the cached elements
        asyncio.run(api._seed_catalog())
        assert api.store.get(90001) is not None
        iss = api.store.get(25544)
        assert iss is not None
        assert iss.line1 == cached_iss.splitlines()[1]
        assert api.store.count() > 10


def test_startup_skips_cache_files_older_than_limit(
    tmp_path, monkeypatch, offline_lifespan, caplog
):
    assert api.TLE_CACHE_MAX_AGE_HOURS == 72.0
    limit = api.TLE_CACHE_MAX_AGE_HOURS
    (tmp_path / "fresh.tle").write_text(make_tle(90001), encoding="utf-8")
    (tmp_path / "stale.tle").write_text(make_tle(90002), encoding="utf-8")
    age_file(tmp_path / "fresh.tle", limit - 1.0)
    age_file(tmp_path / "stale.tle", limit + 1.0)
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    api.store._catalog.clear()

    with caplog.at_level(logging.INFO), TestClient(app):
        assert api.store.get(90001) is not None
        assert api.store.get(90002) is None

    assert "stale.tle" in caplog.text  # the skip is logged, with the file name
    assert "1 file" in caplog.text  # and so is what was loaded


def test_startup_survives_corrupt_and_unreadable_cache_files(
    tmp_path, monkeypatch, offline_lifespan
):
    (tmp_path / "binary.tle").write_bytes(b"\xff\xfe\x00\x01garbage\x00" * 50)
    (tmp_path / "truncated.tle").write_text(
        "ISS (ZARYA)\n1 25544U 98067A   24045.51782528  .0001\n", encoding="utf-8"
    )
    (tmp_path / "directory.tle").mkdir()  # reading it raises
    (tmp_path / "good.tle").write_text(make_tle(90001), encoding="utf-8")
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    api.store._catalog.clear()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert api.store.get(90001) is not None
        assert api.store.count() > 10  # the demo catalog is intact


def test_cache_never_replaces_newer_elements_with_older_ones(
    tmp_path, monkeypatch, offline_lifespan
):
    """TLEStore.upsert is last-write-wins; the cache loader must compare epochs itself."""
    newer = make_tle(90001, epoch="26200.50000000")
    older = make_tle(90001, epoch="24045.51782528")
    # Sorted by name the older set loads last, which a plain load would let win
    (tmp_path / "a-newer.tle").write_text(newer, encoding="utf-8")
    (tmp_path / "z-older.tle").write_text(older, encoding="utf-8")
    # The bundled demo ISS (epoch 26081) is newer than this 2024 ISS element set
    (tmp_path / "m-iss.tle").write_text(SAMPLE_TLE, encoding="utf-8")
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    api.store._catalog.clear()

    with TestClient(app):
        kept = api.store.get(90001)
        assert kept is not None
        assert kept.line1 == newer.splitlines()[1]
        iss = api.store.get(25544)
        assert iss is not None
        assert iss.line1 != SAMPLE_TLE.splitlines()[1]


# --- run() entry point ---


# --- CelesTrak repeat-download window ---

REFUSAL = "GP data has not updated since your last successful download."


def counting_client(status: int = 200, text: str = SAMPLE_TLE):
    """A fake client class plus the list of URLs it was asked for."""
    seen: list[str] = []

    def handler(url):
        seen.append(url)
        return FakeResponse(status, text)

    return seen, make_fake_client(handler)


def restart() -> None:
    """Stand in for a new process: what the old one remembered about its own
    CelesTrak requests is gone, and only the cache directory is left."""
    api._forget_celestrak()


def test_restart_does_not_ask_again_for_a_group_it_just_downloaded(tmp_path, monkeypatch):
    """CelesTrak answers 403 to a repeat download inside its two-hour window, and enough
    of those get an address blocked. A second fetch, as a restart makes, must stay home."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    api.store._catalog.clear()
    seen, client_cls = counting_client()

    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 1
    restart()
    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 0
    assert len(seen) == 1


def test_a_refused_group_is_not_asked_again_inside_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    seen, client_cls = counting_client(403, REFUSAL)

    for _ in range(3):
        assert asyncio.run(api._fetch_celestrak_group(client_cls(), "active")) == 0
        restart()
    assert len(seen) == 1
    assert not (tmp_path / "active.tle").exists()


@pytest.mark.parametrize("marker", ["stations.tle", "stations.refused"])
def test_group_is_asked_again_once_the_window_has_passed(tmp_path, monkeypatch, marker):
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    api.store._catalog.clear()
    path = tmp_path / marker
    path.write_text(SAMPLE_TLE if marker.endswith(".tle") else "", encoding="utf-8")
    age_file(path, hours=api.CELESTRAK_REPEAT_WINDOW_S / 3600.0 + 0.05)
    seen, client_cls = counting_client()

    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 1
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, "server error"), (404, "Not Found"), (200, "   "), (200, "No GP data found")],
)
def test_any_reply_without_elements_rests_the_group(tmp_path, monkeypatch, status, body):
    """Only a 403 used to be remembered, so a group answering anything else with no
    element sets in it was asked again on every restart and every manual refresh."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    seen, client_cls = counting_client(status, body)

    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 0
    restart()
    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 0
    assert len(seen) == 1
    assert not (tmp_path / "stations.tle").exists()


def test_a_request_that_got_no_reply_can_be_retried_at_once(tmp_path, monkeypatch):
    """Nothing reached CelesTrak's counter, so there is nothing to wait out."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    seen: list[str] = []

    def unreachable(url):
        seen.append(url)
        raise RuntimeError("connection reset")

    client_cls = make_fake_client(unreachable)
    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 0
    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 0
    assert len(seen) == 2


def test_a_group_that_gives_nothing_says_why_in_the_log(tmp_path, monkeypatch, caplog):
    """The fetch swallowed every failure without a word; a group could go missing for
    days with nothing to read."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    _, refused_cls = counting_client(403, REFUSAL)

    def unreachable(url):
        raise TimeoutError("timed out")

    with caplog.at_level(logging.INFO, logger=api.logger.name):
        asyncio.run(api._fetch_celestrak_group(refused_cls(), "active"))
        asyncio.run(api._fetch_celestrak_group(make_fake_client(unreachable)(), "geo"))

    lines = [r.getMessage() for r in caplog.records]
    assert any("active" in line and "HTTP 403" in line for line in lines), lines
    assert any("geo" in line and "TimeoutError" in line for line in lines), lines


def test_seed_logs_what_the_refresh_brought_in(monkeypatch, caplog):
    api.store._catalog.clear()
    served = {"stations"}

    def handler(url):
        group = url.split("GROUP=")[1].split("&")[0]
        return FakeResponse(200, SAMPLE_TLE) if group in served else FakeResponse(500, "down")

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(handler))

    with caplog.at_level(logging.INFO, logger=api.logger.name):
        asyncio.run(api._seed_catalog())

    total = len(api.CELESTRAK_GROUPS)
    summary = [r.getMessage() for r in caplog.records if "groups asked" in r.getMessage()]
    assert summary == [f"CelesTrak: 1 objects from 1 of {total} groups asked"], summary


def test_without_a_cache_dir_the_process_remembers_on_its_own():
    """No disk to keep the time of the last request on. A second refresh, or the
    globe asking for the group, must still stay inside the window; this used to send
    every request again."""
    api.store._catalog.clear()
    seen, client_cls = counting_client()

    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 1
    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 0
    assert len(seen) == 1

    restart()  # across a restart only the cache directory carries the window
    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "stations")) == 1
    assert len(seen) == 2


def test_overlapping_refreshes_ask_each_group_once(monkeypatch):
    """The startup fetch and a POST /tle/refresh that lands while it is on the wire
    both asked every group, because neither had an answer to remember yet."""
    api.store._catalog.clear()
    seen: list[str] = []

    class SlowClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            seen.append(url)
            await asyncio.sleep(0.01)  # still in flight when the next caller arrives
            return FakeResponse(200, SAMPLE_TLE)

    monkeypatch.setattr(api.httpx, "AsyncClient", SlowClient)

    async def five_at_once():
        await asyncio.gather(*(api._seed_catalog() for _ in range(5)))

    asyncio.run(five_at_once())

    assert len(seen) == len(api.CELESTRAK_GROUPS)
    assert len(set(seen)) == len(seen)


def test_a_request_that_is_cancelled_frees_the_group():
    """A request torn down mid-flight (shutdown, a dropped connection) must not leave
    the group marked as being asked for ever."""
    started = asyncio.Event()

    class HangingClient:
        async def get(self, url):
            started.set()
            await asyncio.sleep(3600)

    async def cancel_mid_flight():
        task = asyncio.ensure_future(api._fetch_celestrak_group(HangingClient(), "geo"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_mid_flight())
    api.store._catalog.clear()
    seen, client_cls = counting_client()
    assert asyncio.run(api._fetch_celestrak_group(client_cls(), "geo")) == 1
    assert len(seen) == 1


def test_a_refresh_that_brings_nothing_never_puts_bundled_elements_over_newer_ones(monkeypatch):
    """Without a cache dir the fallback was a plain load of the bundled catalog, which
    is last-write-wins: one failed refresh put the bundled ISS elements back over the
    ones a good refresh had brought, and those feed the conjunction screen."""
    api.store._catalog.clear()
    api.store.load_from_text(make_tle(25544, name="ISS (ZARYA)", epoch="30045.51782528"))
    newer = api.store.get(25544).epoch

    def _raise(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(_raise))

    asyncio.run(api._seed_catalog())

    assert api.store.get(25544).epoch == newer
    assert api.store.count() > 10  # the rest of the bundled catalog still arrives


def test_a_refresh_that_asked_nobody_reports_the_download_it_is_running_on(
    tmp_path, monkeypatch
):
    """After a restart inside the window every group rests. /health then said the
    catalog had been refreshed at the moment the process started."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    for group in api.CELESTRAK_GROUPS:
        path = tmp_path / f"{group}.tle"
        path.write_text(SAMPLE_TLE, encoding="utf-8")
        age_file(path, hours=1.0)
    api._last_tle_refresh = None
    seen, client_cls = counting_client()
    monkeypatch.setattr(api.httpx, "AsyncClient", client_cls)

    asyncio.run(api._seed_catalog())

    assert seen == []
    age_s = (datetime.now(UTC) - api._last_tle_refresh).total_seconds()
    assert 3500 < age_s < 3700

    reported = api._last_tle_refresh
    asyncio.run(api._seed_catalog())  # still resting: nothing new to report
    assert api._last_tle_refresh == reported


@pytest.mark.parametrize("reply", ["no connection", "refused", "empty"])
def test_a_refresh_that_brought_nothing_home_keeps_the_time_of_the_last_download(
    monkeypatch, reply
):
    """The machine slept overnight, the loop woke before the network did, every group
    failed to connect, and /health said the catalog had been refreshed at that moment.
    It was still running on the download from the evening before."""
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    api._last_tle_refresh = earlier

    def handler(url):
        if reply == "no connection":
            raise api.httpx.ConnectError("no route to host")
        if reply == "refused":
            return FakeResponse(403, "GP data has not updated since your last download")
        return FakeResponse(200, "   ")

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(handler))

    assert asyncio.run(api._seed_catalog()) == len(api.CELESTRAK_GROUPS)
    assert api._last_tle_refresh == earlier


def test_a_refresh_that_brought_one_group_home_reports_now(monkeypatch):
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    api._last_tle_refresh = earlier

    def handler(url):
        if "GROUP=stations&" in url:
            return FakeResponse(200, SAMPLE_TLE)
        raise api.httpx.ConnectError("no route to host")

    monkeypatch.setattr(api.httpx, "AsyncClient", make_fake_client(handler))

    asyncio.run(api._seed_catalog())

    assert (datetime.now(UTC) - api._last_tle_refresh).total_seconds() < 60


def test_health_reports_the_refresh_interval_the_loop_really_uses():
    live = TestClient(app).get("/health").json()["live_data"]
    assert live["auto_refresh_hours"] * 3600 == pytest.approx(api.TLE_REFRESH_INTERVAL_S, abs=30)


def test_the_loop_comes_back_early_for_groups_a_restart_skipped(tmp_path, monkeypatch):
    """The loop's first sleep is the time left in the window, not a flat two hours.
    Nothing checked that the loop used it."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    marker = tmp_path / "active.refused"
    marker.write_text("", encoding="utf-8")
    age_file(marker, hours=1.0)
    sleeps: list[float] = []

    async def fake_seed():
        return None

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(api, "_seed_catalog", fake_seed)
    monkeypatch.setattr(api, "asyncio", SimpleNamespace(sleep=fake_sleep))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(api._background_refresh_loop())

    remaining = api.CELESTRAK_REPEAT_WINDOW_S - 3600.0
    assert remaining <= sleeps[0] <= remaining + 120
    assert sleeps[1:] == [7500, 7500]


def test_the_window_is_two_hours_and_the_loop_runs_past_it():
    """CelesTrak asks for one download of a group per two hours, so nothing shorter
    will do. The loop runs minutes past the window, or the pass after a startup fetch
    would find that fetch too recent and skip every group."""
    assert api.CELESTRAK_REPEAT_WINDOW_S == 7200
    assert api.TLE_REFRESH_INTERVAL_S - api.CELESTRAK_REPEAT_WINDOW_S >= 300


def test_first_refresh_after_a_restart_waits_only_for_the_window(tmp_path, monkeypatch):
    """A restart that skipped a group comes back for it when CelesTrak will answer,
    not a full two hours later."""
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    marker = tmp_path / "active.refused"
    marker.write_text("", encoding="utf-8")
    age_file(marker, hours=1.0)

    remaining = api.CELESTRAK_REPEAT_WINDOW_S - 3600.0
    assert remaining <= api._first_refresh_delay_s() <= remaining + 120


def test_first_refresh_is_the_normal_interval_when_nothing_was_skipped(tmp_path, monkeypatch):
    assert api._first_refresh_delay_s() == 7500
    monkeypatch.setenv(CACHE_ENV, str(tmp_path))
    assert api._first_refresh_delay_s() == 7500
    stale = tmp_path / "geo.tle"
    stale.write_text(SAMPLE_TLE, encoding="utf-8")
    age_file(stale, hours=5.0)
    assert api._first_refresh_delay_s() == 7500


def test_refresh_route_reports_this_refresh_not_the_one_before(monkeypatch):
    """POST /tle/refresh answered with the time and flux it read before refreshing."""
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    later = datetime(2026, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(api, "_last_tle_refresh", earlier)
    monkeypatch.setattr(api, "_live_f107", 100.0)

    async def fake_seed():
        api._last_tle_refresh = later

    async def fake_f107():
        api._live_f107 = 175.5

    monkeypatch.setattr(api, "_seed_catalog", fake_seed)
    monkeypatch.setattr(api, "_fetch_live_f107", fake_f107)

    body = TestClient(app).post("/tle/refresh").json()

    assert body["timestamp"] == later.isoformat()
    assert body["f107_solar_flux"] == 175.5


def test_refresh_route_says_so_when_every_group_is_resting(monkeypatch):
    """Inside CelesTrak's window nothing is downloaded, and the answer read
    'refreshed' with a new timestamp all the same."""
    api.store._catalog.clear()
    seen, client_cls = counting_client()
    monkeypatch.setattr(api.httpx, "AsyncClient", client_cls)
    total = len(api.CELESTRAK_GROUPS)

    def asked() -> int:
        return sum("celestrak" in url for url in seen)

    client = TestClient(app)
    first = client.post("/tle/refresh").json()
    assert asked() == total
    second = client.post("/tle/refresh").json()

    assert (first["status"], first["groups_asked"]) == ("refreshed", total)
    assert (second["status"], second["groups_asked"]) == ("waiting", 0)
    assert asked() == total
    assert second["timestamp"] == first["timestamp"]


def test_run_invokes_uvicorn(monkeypatch):
    import uvicorn

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.delenv("SDA_HOST", raising=False)
    monkeypatch.delenv("SDA_PORT", raising=False)
    api.run()

    assert captured["args"][0] == "sda.api:app"
    assert captured["kwargs"]["port"] == 8000
    # The default bind is loopback. SDA_HOST overrides it and the container
    # images set SDA_HOST=0.0.0.0 so published ports keep working.
    assert captured["kwargs"]["host"] == "127.0.0.1"
