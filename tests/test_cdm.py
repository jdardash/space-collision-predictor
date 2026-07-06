"""Tests for CCSDS CDM generation."""

from datetime import UTC, datetime

from sda.cdm import _extract_intl_designator, generate_cdm, generate_cdm_batch
from sda.models import ConjunctionEvent, RiskLevel, TLERecord

ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, tzinfo=UTC),
)

SAMPLE_EVENT = ConjunctionEvent(
    primary=25544,
    secondary=48274,
    primary_name="ISS (ZARYA)",
    secondary_name="CSS (TIANHE)",
    tca=datetime(2024, 2, 15, 6, 30, 0, tzinfo=UTC),
    miss_distance_km=2.5,
    relative_velocity_km_s=8.3,
    risk=RiskLevel.MODERATE,
)


def test_generate_cdm_contains_header():
    cdm = generate_cdm(SAMPLE_EVENT)
    assert "CCSDS_CDM_VERS" in cdm
    assert "1.0" in cdm


def test_generate_cdm_contains_tca():
    cdm = generate_cdm(SAMPLE_EVENT)
    assert "2024-02-15T06:30:00" in cdm


def test_generate_cdm_contains_miss_distance():
    cdm = generate_cdm(SAMPLE_EVENT)
    assert "2.500000" in cdm


def test_generate_cdm_contains_objects():
    cdm = generate_cdm(SAMPLE_EVENT, tle_primary=ISS_TLE)
    assert "OBJECT1" in cdm
    assert "OBJECT2" in cdm
    assert "ISS (ZARYA)" in cdm


def test_generate_cdm_with_pc():
    cdm = generate_cdm(SAMPLE_EVENT, collision_probability=1.5e-4)
    assert "COLLISION_PROBABILITY" in cdm
    assert "1.5" in cdm


def test_generate_cdm_gravity_model():
    cdm = generate_cdm(SAMPLE_EVENT)
    assert "WGS-72" in cdm


def test_extract_intl_designator():
    result = _extract_intl_designator(ISS_TLE)
    assert result == "98067A"


def test_generate_cdm_batch():
    events = [SAMPLE_EVENT]
    results = generate_cdm_batch(events)
    assert len(results) == 1
    assert results[0]["primary"] == 25544
    assert results[0]["risk"] == "MODERATE"
    assert "CCSDS_CDM_VERS" in results[0]["cdm_text"]
