"""Tests for CCSDS CDM ingestion: KVN parsing and Pc from real covariances."""

from datetime import UTC, datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sda.api import app
from sda.cdm import compute_pc_from_cdm, generate_cdm, parse_cdm
from sda.models import ConjunctionEvent, RiskLevel
from sda.probability import compute_collision_probability_full, rtn_covariance_to_eci

client = TestClient(app)

SAMPLE_EVENT = ConjunctionEvent(
    primary=25544,
    secondary=48274,
    primary_name="ISS (ZARYA)",
    secondary_name="CSS (TIANHE)",
    tca=datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC),
    miss_distance_km=2.5,
    relative_velocity_km_s=8.3,
    risk=RiskLevel.MODERATE,
)

# Realistic operational-style CDM: full states (CCSDS default km) and
# RTN position covariances (CCSDS default m**2). Object1 sits on the
# x-axis moving along +y, so its RTN frame coincides with ECI axes.
FULL_CDM = """\
CCSDS_CDM_VERS                = 1.0
CREATION_DATE                 = 2026-07-06T00:00:00.000
ORIGINATOR                    = TEST-HARNESS
MESSAGE_ID                    = TEST-0001
TCA                           = 2026-07-06T12:00:00.000
MISS_DISTANCE                 = 500.0 [m]
RELATIVE_SPEED                = 14800.0 [m/s]
COLLISION_PROBABILITY         = 2.5e-05

OBJECT                        = OBJECT1
OBJECT_DESIGNATOR             = 25544
OBJECT_NAME                   = ISS (ZARYA)
X                             = 7000.0 [km]
Y                             = 0.0 [km]
Z                             = 0.0 [km]
X_DOT                         = 0.0 [km/s]
Y_DOT                         = 7.5 [km/s]
Z_DOT                         = 0.0 [km/s]
CR_R                          = 10000.0 [m**2]
CT_R                          = 0.0 [m**2]
CT_T                          = 40000.0 [m**2]
CN_R                          = 0.0 [m**2]
CN_T                          = 0.0 [m**2]
CN_N                          = 2500.0

OBJECT                        = OBJECT2
OBJECT_DESIGNATOR             = 48274
OBJECT_NAME                   = CSS (TIANHE)
X                             = 7000.3 [km]
Y                             = 0.0 [km]
Z                             = 0.4 [km]
X_DOT                         = 0.0 [km/s]
Y_DOT                         = -7.3 [km/s]
Z_DOT                         = 0.0 [km/s]
CR_R                          = 22500.0 [m**2]
CT_R                          = 0.0 [m**2]
CT_T                          = 90000.0 [m**2]
CN_R                          = 0.0 [m**2]
CN_T                          = 0.0 [m**2]
CN_N                          = 4900.0 [m**2]
"""

RELATIVE_ONLY_CDM = """\
CCSDS_CDM_VERS                = 1.0
MESSAGE_ID                    = REL-0001
TCA                           = 2026-07-06T12:00:00.000
MISS_DISTANCE                 = 500.0 [m]
RELATIVE_SPEED                = 14800.0 [m/s]
RELATIVE_POSITION_R           = 300.0 [m]
RELATIVE_POSITION_T           = 0.0 [m]
RELATIVE_POSITION_N           = 400.0 [m]
RELATIVE_VELOCITY_R           = 0.0 [m/s]
RELATIVE_VELOCITY_T           = -14800.0 [m/s]
RELATIVE_VELOCITY_N           = 0.0 [m/s]

OBJECT                        = OBJECT1
OBJECT_DESIGNATOR             = 25544
CR_R                          = 10000.0 [m**2]
CT_R                          = 0.0 [m**2]
CT_T                          = 40000.0 [m**2]
CN_R                          = 0.0 [m**2]
CN_T                          = 0.0 [m**2]
CN_N                          = 2500.0 [m**2]

OBJECT                        = OBJECT2
OBJECT_DESIGNATOR             = 48274
"""


class TestParseCdm:
    def test_roundtrip_of_generated_cdm(self):
        text = generate_cdm(SAMPLE_EVENT, collision_probability=1.2e-4)
        parsed = parse_cdm(text)
        assert parsed.miss_distance_km == pytest.approx(2.5)  # [km] bracket honored
        assert parsed.relative_speed_km_s == pytest.approx(8.3)
        assert parsed.stated_collision_probability == pytest.approx(1.2e-4)
        assert parsed.tca == datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)
        assert parsed.object1.designator == "25544"
        assert parsed.object2.name == "CSS (TIANHE)"

    def test_full_cdm_fields(self):
        parsed = parse_cdm(FULL_CDM)
        assert parsed.miss_distance_km == pytest.approx(0.5)  # 500 m -> km
        assert parsed.relative_speed_km_s == pytest.approx(14.8)
        assert parsed.object1.position_km == pytest.approx((7000.0, 0.0, 0.0))
        assert parsed.object2.velocity_km_s == pytest.approx((0.0, -7.3, 0.0))
        cov1 = np.array(parsed.object1.cov_rtn_km2)
        # 10000 m**2 -> 1e-2 km**2; bare CN_N uses the CCSDS default m**2
        assert cov1[0, 0] == pytest.approx(1e-2)
        assert cov1[1, 1] == pytest.approx(4e-2)
        assert cov1[2, 2] == pytest.approx(2.5e-3)
        assert np.allclose(cov1, cov1.T)

    def test_not_a_cdm_raises(self):
        with pytest.raises(ValueError, match="CCSDS_CDM_VERS"):
            parse_cdm("hello world")

    def test_unsupported_unit_raises(self):
        bad = "CCSDS_CDM_VERS = 1.0\nMISS_DISTANCE = 1.0 [furlong]\n"
        with pytest.raises(ValueError, match="furlong"):
            parse_cdm(bad)

    def test_comments_and_blank_lines_ignored(self):
        parsed = parse_cdm(generate_cdm(SAMPLE_EVENT))
        assert parsed.message_id is not None


class TestComputePcFromCdm:
    def test_eci_path_matches_direct_computation(self):
        parsed = parse_cdm(FULL_CDM)
        result = compute_pc_from_cdm(parsed, hard_body_radius_km=0.020)
        assert result.frame == "ECI"

        r1, v1 = np.array([7000.0, 0.0, 0.0]), np.array([0.0, 7.5, 0.0])
        r2, v2 = np.array([7000.3, 0.0, 0.4]), np.array([0.0, -7.3, 0.0])
        cov1 = rtn_covariance_to_eci(np.diag([1e-2, 4e-2, 2.5e-3]), r1, v1)
        cov2 = rtn_covariance_to_eci(np.diag([2.25e-2, 9e-2, 4.9e-3]), r2, v2)
        expected = compute_collision_probability_full(
            miss_vector_km=r1 - r2,
            rel_velocity_km_s=v1 - v2,
            cov_primary_eci_km2=cov1,
            cov_secondary_eci_km2=cov2,
            combined_radius_km=0.020,
        )
        assert result.probability_foster == pytest.approx(expected["probability"], rel=1e-12)
        assert result.probability_chan == pytest.approx(expected["probability_chan"], rel=1e-12)
        assert result.miss_distance_km == pytest.approx(0.5)
        assert result.stated_collision_probability == pytest.approx(2.5e-05)

    def test_relative_state_fallback(self):
        parsed = parse_cdm(RELATIVE_ONLY_CDM)
        result = compute_pc_from_cdm(parsed, hard_body_radius_km=0.020)
        assert result.frame == "RTN_OBJECT1"
        assert result.miss_distance_km == pytest.approx(0.5)
        assert any("RTN frame of OBJECT1" in a for a in result.assumptions)
        assert any("isotropic default" in a for a in result.assumptions)
        assert 0.0 <= result.probability_foster <= 1.0

    def test_no_state_information_raises(self):
        parsed = parse_cdm(generate_cdm(SAMPLE_EVENT))  # our generator emits no states
        with pytest.raises(ValueError, match="encounter geometry"):
            compute_pc_from_cdm(parsed)


class TestIngestEndpoint:
    def test_ingest_full_cdm(self):
        resp = client.post(
            "/conjunctions/cdm/ingest",
            json={"cdm_text": FULL_CDM, "hard_body_radius_km": 0.020},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pc"]["frame"] == "ECI"
        assert body["pc"]["probability_foster"] > 0.0
        assert body["parsed"]["object1"]["name"] == "ISS (ZARYA)"
        assert body["pc"]["stated_collision_probability"] == pytest.approx(2.5e-05)

    def test_ingest_rejects_non_cdm(self):
        resp = client.post("/conjunctions/cdm/ingest", json={"cdm_text": "not a cdm"})
        assert resp.status_code == 422
        assert "CCSDS_CDM_VERS" in resp.json()["detail"]

    def test_ingest_rejects_geometry_free_cdm(self):
        text = generate_cdm(SAMPLE_EVENT)
        resp = client.post("/conjunctions/cdm/ingest", json={"cdm_text": text})
        assert resp.status_code == 422
        assert "encounter geometry" in resp.json()["detail"]
