"""Lifecycle, background-task, and cache tests for sda.api.

All tests are offline-deterministic: httpx.AsyncClient is replaced with a
fake client returning canned responses (or canned failures), so no real
network calls ever happen. Async helpers are driven with asyncio.run()
inside synchronous tests (pytest-asyncio is not a dependency).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sda.api as api
from sda.api import app

# Sample TLE text (ISS) — same canonical test case as test_api.py
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

    # Only the ISS from the canned live response — demo fallback not triggered
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
    assert api._last_tle_refresh is not None


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
    assert all(s == 7200 for s in sleep_calls)


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


# --- run() entry point ---


def test_run_invokes_uvicorn(monkeypatch):
    import uvicorn

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    api.run()

    assert captured["args"][0] == "sda.api:app"
    assert captured["kwargs"]["port"] == 8000
    assert captured["kwargs"]["host"] == "0.0.0.0"
