"""Tests for the FastAPI service."""

from fastapi.testclient import TestClient

from sda.api import app

client = TestClient(app)

# Sample TLE text (ISS)
SAMPLE_TLE = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"""

TWO_SATS_TLE = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014
CSS (TIANHE)
1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991
2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001"""


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "stale_tles" in data
    assert "version" in data


def test_ingest_and_list():
    resp = client.post("/tle", json={"tle_text": SAMPLE_TLE})
    assert resp.status_code == 200
    assert resp.json()["ingested"] >= 1

    resp = client.get("/satellites")
    assert resp.status_code == 200
    satellites = resp.json()
    norad_ids = [s["norad_id"] for s in satellites]
    assert 25544 in norad_ids


def test_get_satellite():
    client.post("/tle", json={"tle_text": SAMPLE_TLE})
    resp = client.get("/satellites/25544")
    assert resp.status_code == 200
    data = resp.json()
    assert data["norad_id"] == 25544
    assert data["name"] == "ISS (ZARYA)"
    assert "freshness" in data


def test_get_satellite_not_found():
    resp = client.get("/satellites/99999")
    assert resp.status_code == 404


def test_delete_satellite():
    client.post("/tle", json={"tle_text": SAMPLE_TLE})
    resp = client.delete("/satellites/25544")
    assert resp.status_code == 200


def test_conjunctions_endpoint():
    client.post("/tle", json={"tle_text": TWO_SATS_TLE})
    resp = client.post("/conjunctions", json={"hours": 2.0, "threshold_km": 50.0})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_metrics_endpoint():
    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "satellites_tracked" in data
    assert "catalog_freshness" in data
    assert "performance" in data


def test_tle_freshness_endpoint():
    client.post("/tle", json={"tle_text": SAMPLE_TLE})
    resp = client.get("/tle/freshness")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    if data:
        assert "freshness" in data[0]
        assert "age_hours" in data[0]


def test_tle_stale_endpoint():
    resp = client.get("/tle/stale?threshold_hours=0.001")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_conjunction_history():
    resp = client.get("/conjunctions/history")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_conjunction_cdm():
    client.post("/tle", json={"tle_text": TWO_SATS_TLE})
    resp = client.post("/conjunctions/cdm", json={"hours": 2.0, "threshold_km": 50.0})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_decay_endpoint():
    client.post("/tle", json={"tle_text": SAMPLE_TLE})
    resp = client.get("/satellites/25544/decay")
    assert resp.status_code == 200
    data = resp.json()
    assert "altitude_km" in data
    assert "estimated_lifetime_days" in data
    assert "reentry_risk" in data


def test_decay_not_found():
    resp = client.get("/satellites/99999/decay")
    assert resp.status_code == 404


def test_maneuver_endpoint():
    client.post("/tle", json={"tle_text": TWO_SATS_TLE})
    resp = client.post("/maneuver", json={
        "primary_norad_id": 25544,
        "secondary_norad_id": 48274,
        "tca": "2024-02-15T06:00:00Z",
        "target_miss_km": 5.0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "options" in data
    assert "recommended" in data


def test_montecarlo_endpoint():
    client.post("/tle", json={"tle_text": TWO_SATS_TLE})
    resp = client.post("/montecarlo", json={
        "primary_norad_id": 25544,
        "secondary_norad_id": 48274,
        "tca": "2024-02-15T06:00:00Z",
        "n_samples": 20,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "statistics" in data
    assert "collision_probability_mc" in data
    assert "histogram_bins" in data
