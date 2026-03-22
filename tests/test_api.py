"""Tests for the FastAPI service."""

from fastapi.testclient import TestClient

from sda.api import app, store


client = TestClient(app)

# Sample TLE text (ISS)
SAMPLE_TLE = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014"""


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


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
    # Ensure ISS is loaded
    client.post("/tle", json={"tle_text": SAMPLE_TLE})

    resp = client.get("/satellites/25544")
    assert resp.status_code == 200
    data = resp.json()
    assert data["norad_id"] == 25544
    assert data["name"] == "ISS (ZARYA)"


def test_get_satellite_not_found():
    resp = client.get("/satellites/99999")
    assert resp.status_code == 404


def test_delete_satellite():
    client.post("/tle", json={"tle_text": SAMPLE_TLE})
    resp = client.delete("/satellites/25544")
    assert resp.status_code == 200


def test_conjunctions_endpoint():
    # Load two satellites
    tle_text = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014
CSS (TIANHE)
1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991
2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001"""

    client.post("/tle", json={"tle_text": tle_text})
    resp = client.post("/conjunctions", json={"hours": 2.0, "threshold_km": 50.0})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
