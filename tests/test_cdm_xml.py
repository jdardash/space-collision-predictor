"""CDM XML ingestion: CCSDS 508.0-B-1 XML form, dispatch, and the writer field.

Offline and deterministic. The XML fixture below is a complete two-segment
CDM: header, relative metadata with a nested relativeStateVector, per-object
metadata with COMMENT lines, state vectors in km, and a full 21-element 6x6
RTN covariance on OBJECT1 (position block only on OBJECT2).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sda.cdm import (
    DEFAULT_PC_METHOD_NAME,
    compute_pc_from_cdm,
    generate_cdm,
    parse_cdm,
    parse_cdm_detailed,
)
from sda.models import ConjunctionEvent, RiskLevel
from sda.routes.pc import router as pc_router

XML_CDM = """<?xml version="1.0" encoding="UTF-8"?>
<ndm:cdm xmlns:ndm="urn:ccsds:recommendation:navigation:schema:ndmxml"
         id="CCSDS_CDM_VERS" version="1.0">
  <header>
    <COMMENT>Sample XML CDM for the SDA collision predictor tests</COMMENT>
    <CREATION_DATE>2026-01-01T00:00:00.000</CREATION_DATE>
    <ORIGINATOR>SDA-XML-TEST</ORIGINATOR>
    <MESSAGE_ID>XML-0001</MESSAGE_ID>
  </header>
  <body>
    <relativeMetadataData>
      <COMMENT>Screening volume 1 km</COMMENT>
      <TCA>2026-01-02T03:04:05.000</TCA>
      <MISS_DISTANCE units="m">500.0</MISS_DISTANCE>
      <RELATIVE_SPEED units="m/s">14000.0</RELATIVE_SPEED>
      <relativeStateVector>
        <RELATIVE_POSITION_R units="m">300.0</RELATIVE_POSITION_R>
        <RELATIVE_POSITION_T units="m">400.0</RELATIVE_POSITION_T>
        <RELATIVE_POSITION_N units="m">0.0</RELATIVE_POSITION_N>
        <RELATIVE_VELOCITY_R units="m/s">10.0</RELATIVE_VELOCITY_R>
        <RELATIVE_VELOCITY_T units="m/s">-13990.0</RELATIVE_VELOCITY_T>
        <RELATIVE_VELOCITY_N units="m/s">20.0</RELATIVE_VELOCITY_N>
      </relativeStateVector>
      <COLLISION_PROBABILITY>2.5e-05</COLLISION_PROBABILITY>
      <COLLISION_PROBABILITY_METHOD>FOSTER-1992</COLLISION_PROBABILITY_METHOD>
    </relativeMetadataData>
    <segment>
      <metadata>
        <COMMENT>Primary is an operational payload</COMMENT>
        <OBJECT>OBJECT1</OBJECT>
        <OBJECT_DESIGNATOR>25544</OBJECT_DESIGNATOR>
        <CATALOG_NAME>SATCAT</CATALOG_NAME>
        <OBJECT_NAME>ISS (ZARYA)</OBJECT_NAME>
        <INTERNATIONAL_DESIGNATOR>1998-067A</INTERNATIONAL_DESIGNATOR>
        <EPHEMERIS_NAME>SGP4</EPHEMERIS_NAME>
        <COVARIANCE_METHOD>CALCULATED</COVARIANCE_METHOD>
        <REF_FRAME>EME2000</REF_FRAME>
      </metadata>
      <data>
        <stateVector>
          <X units="km">6800.0</X>
          <Y units="km">100.0</Y>
          <Z units="km">50.0</Z>
          <X_DOT units="km/s">0.5</X_DOT>
          <Y_DOT units="km/s">7.0</Y_DOT>
          <Z_DOT units="km/s">1.0</Z_DOT>
        </stateVector>
        <covarianceMatrix>
          <CR_R units="m**2">2500.0</CR_R>
          <CT_R units="m**2">100.0</CT_R>
          <CT_T units="m**2">40000.0</CT_T>
          <CN_R units="m**2">0.0</CN_R>
          <CN_T units="m**2">0.0</CN_T>
          <CN_N units="m**2">900.0</CN_N>
          <CRDOT_R units="m**2/s">0.0</CRDOT_R>
          <CRDOT_T units="m**2/s">0.0</CRDOT_T>
          <CRDOT_N units="m**2/s">0.0</CRDOT_N>
          <CRDOT_RDOT units="m**2/s**2">1.0</CRDOT_RDOT>
          <CTDOT_R units="m**2/s">0.0</CTDOT_R>
          <CTDOT_T units="m**2/s">0.0</CTDOT_T>
          <CTDOT_N units="m**2/s">0.0</CTDOT_N>
          <CTDOT_RDOT units="m**2/s**2">0.0</CTDOT_RDOT>
          <CTDOT_TDOT units="m**2/s**2">2.0</CTDOT_TDOT>
          <CNDOT_R units="m**2/s">0.0</CNDOT_R>
          <CNDOT_T units="m**2/s">0.0</CNDOT_T>
          <CNDOT_N units="m**2/s">0.0</CNDOT_N>
          <CNDOT_RDOT units="m**2/s**2">0.0</CNDOT_RDOT>
          <CNDOT_TDOT units="m**2/s**2">0.0</CNDOT_TDOT>
          <CNDOT_NDOT units="m**2/s**2">3.0</CNDOT_NDOT>
        </covarianceMatrix>
      </data>
    </segment>
    <segment>
      <metadata>
        <COMMENT>Secondary is debris</COMMENT>
        <OBJECT>OBJECT2</OBJECT>
        <OBJECT_DESIGNATOR>48274</OBJECT_DESIGNATOR>
        <CATALOG_NAME>SATCAT</CATALOG_NAME>
        <OBJECT_NAME>DEBRIS FRAGMENT</OBJECT_NAME>
        <REF_FRAME>EME2000</REF_FRAME>
      </metadata>
      <data>
        <stateVector>
          <X units="km">6800.3</X>
          <Y units="km">100.4</Y>
          <Z units="km">50.0</Z>
          <X_DOT units="km/s">0.51</X_DOT>
          <Y_DOT units="km/s">-6.99</Y_DOT>
          <Z_DOT units="km/s">1.02</Z_DOT>
        </stateVector>
        <covarianceMatrix>
          <CR_R units="km**2">0.0025</CR_R>
          <CT_R units="km**2">0.0</CT_R>
          <CT_T units="km**2">0.09</CT_T>
          <CN_R units="km**2">0.0</CN_R>
          <CN_T units="km**2">0.0</CN_T>
          <CN_N units="km**2">0.0016</CN_N>
        </covarianceMatrix>
      </data>
    </segment>
  </body>
</ndm:cdm>
"""

KVN_TWIN = """CCSDS_CDM_VERS                = 1.0
CREATION_DATE                 = 2026-01-01T00:00:00.000
ORIGINATOR                    = SDA-XML-TEST
MESSAGE_ID                    = XML-0001
TCA                           = 2026-01-02T03:04:05.000
MISS_DISTANCE                 = 500.0 [m]
RELATIVE_SPEED                = 14000.0 [m/s]
RELATIVE_POSITION_R           = 300.0 [m]
RELATIVE_POSITION_T           = 400.0 [m]
RELATIVE_POSITION_N           = 0.0 [m]
RELATIVE_VELOCITY_R           = 10.0 [m/s]
RELATIVE_VELOCITY_T           = -13990.0 [m/s]
RELATIVE_VELOCITY_N           = 20.0 [m/s]
COLLISION_PROBABILITY         = 2.5e-05
COLLISION_PROBABILITY_METHOD  = FOSTER-1992

OBJECT                        = OBJECT1
OBJECT_DESIGNATOR             = 25544
OBJECT_NAME                   = ISS (ZARYA)
X                             = 6800.0 [km]
Y                             = 100.0 [km]
Z                             = 50.0 [km]
X_DOT                         = 0.5 [km/s]
Y_DOT                         = 7.0 [km/s]
Z_DOT                         = 1.0 [km/s]
CR_R                          = 2500.0 [m**2]
CT_R                          = 100.0 [m**2]
CT_T                          = 40000.0 [m**2]
CN_R                          = 0.0 [m**2]
CN_T                          = 0.0 [m**2]
CN_N                          = 900.0 [m**2]

OBJECT                        = OBJECT2
OBJECT_DESIGNATOR             = 48274
OBJECT_NAME                   = DEBRIS FRAGMENT
X                             = 6800.3 [km]
Y                             = 100.4 [km]
Z                             = 50.0 [km]
X_DOT                         = 0.51 [km/s]
Y_DOT                         = -6.99 [km/s]
Z_DOT                         = 1.02 [km/s]
CR_R                          = 0.0025 [km**2]
CT_R                          = 0.0 [km**2]
CT_T                          = 0.09 [km**2]
CN_R                          = 0.0 [km**2]
CN_T                          = 0.0 [km**2]
CN_N                          = 0.0016 [km**2]
"""


class TestXmlParsing:
    def test_dispatch_reports_xml(self):
        detail = parse_cdm_detailed(XML_CDM)
        assert detail.source_format == "XML"
        assert parse_cdm_detailed(KVN_TWIN).source_format == "KVN"

    def test_header_and_relative_metadata(self):
        parsed = parse_cdm(XML_CDM)
        assert parsed.message_id == "XML-0001"
        assert parsed.originator == "SDA-XML-TEST"
        assert parsed.tca == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        assert parsed.miss_distance_km == pytest.approx(0.5)
        assert parsed.relative_speed_km_s == pytest.approx(14.0)
        assert parsed.stated_collision_probability == pytest.approx(2.5e-5)

    def test_units_attribute_is_honored(self):
        parsed = parse_cdm(XML_CDM)
        # 300 m and 400 m become km; the nested relativeStateVector is reached.
        assert parsed.relative_position_rtn_km == pytest.approx((0.3, 0.4, 0.0))
        assert parsed.relative_velocity_rtn_km_s == pytest.approx((0.01, -13.99, 0.02))

    def test_objects_and_states(self):
        parsed = parse_cdm(XML_CDM)
        assert parsed.object1.designator == "25544"
        assert parsed.object1.name == "ISS (ZARYA)"
        assert parsed.object2.designator == "48274"
        assert parsed.object1.position_km == pytest.approx((6800.0, 100.0, 50.0))
        assert parsed.object2.velocity_km_s == pytest.approx((0.51, -6.99, 1.02))

    def test_position_block_of_a_6x6_covariance(self):
        detail = parse_cdm_detailed(XML_CDM)
        cov = np.asarray(detail.parsed.object1.cov_rtn_km2, dtype=float)
        assert cov.shape == (3, 3)
        # 2500 m**2 is 2.5e-3 km**2; the matrix is symmetric.
        assert cov[0][0] == pytest.approx(2.5e-3)
        assert cov[1][1] == pytest.approx(0.04)
        assert cov[2][2] == pytest.approx(9e-4)
        assert cov[0][1] == pytest.approx(cov[1][0])
        assert cov[0][1] == pytest.approx(1e-4)

    def test_full_6x6_is_carried_through(self):
        detail = parse_cdm_detailed(XML_CDM)
        full = np.asarray(detail.object1.covariance_rtn_6x6_km2, dtype=float)
        assert full.shape == (6, 6)
        assert np.allclose(full, full.T)
        assert full[3][3] == pytest.approx(1e-6)  # 1 m**2/s**2
        assert full[5][5] == pytest.approx(3e-6)
        # OBJECT2 supplied only the position block, so its velocity terms are 0.
        second = np.asarray(detail.object2.covariance_rtn_6x6_km2, dtype=float)
        assert second[0][0] == pytest.approx(0.0025)
        assert second[4][4] == pytest.approx(0.0)

    def test_comments_are_captured_per_section(self):
        detail = parse_cdm_detailed(XML_CDM)
        assert any("Sample XML CDM" in c for c in detail.comments)
        assert any("Screening volume" in c for c in detail.comments)
        assert detail.object1.comments == ["Primary is an operational payload"]
        assert detail.object2.comments == ["Secondary is debris"]

    def test_stated_method_is_read(self):
        assert parse_cdm_detailed(XML_CDM).collision_probability_method == "FOSTER-1992"

    def test_metadata_is_kept(self):
        detail = parse_cdm_detailed(XML_CDM)
        assert detail.object1.metadata["REF_FRAME"] == "EME2000"
        assert detail.object1.metadata["INTERNATIONAL_DESIGNATOR"] == "1998-067A"


class TestXmlMatchesKvn:
    def test_same_structure_from_both_formats(self):
        xml = parse_cdm(XML_CDM)
        kvn = parse_cdm(KVN_TWIN)
        assert xml.message_id == kvn.message_id
        assert xml.tca == kvn.tca
        assert xml.miss_distance_km == pytest.approx(kvn.miss_distance_km)
        assert xml.relative_speed_km_s == pytest.approx(kvn.relative_speed_km_s)
        assert xml.relative_position_rtn_km == pytest.approx(kvn.relative_position_rtn_km)
        assert xml.object1.designator == kvn.object1.designator
        assert xml.object2.name == kvn.object2.name
        assert np.allclose(xml.object1.position_km, kvn.object1.position_km)
        assert np.allclose(
            np.asarray(xml.object1.cov_rtn_km2, dtype=float),
            np.asarray(kvn.object1.cov_rtn_km2, dtype=float),
        )

    def test_same_pc_from_both_formats(self):
        xml_pc = compute_pc_from_cdm(parse_cdm(XML_CDM))
        kvn_pc = compute_pc_from_cdm(parse_cdm(KVN_TWIN))
        assert xml_pc.probability_foster == pytest.approx(kvn_pc.probability_foster, rel=1e-12)
        assert xml_pc.frame == "ECI"
        assert 0.0 <= xml_pc.probability_foster <= 1.0


class TestRejections:
    def test_malformed_xml(self):
        with pytest.raises(ValueError, match="malformed XML"):
            parse_cdm("<cdm><header><ORIGINATOR>x</header></cdm>")

    def test_wrong_root_element(self):
        with pytest.raises(ValueError, match="root element"):
            parse_cdm('<?xml version="1.0"?><oem><header/></oem>')

    def test_non_cdm_text_still_names_the_missing_keyword(self):
        with pytest.raises(ValueError, match="missing CCSDS_CDM_VERS"):
            parse_cdm("SOME_KEY = 1.0\nOTHER = 2.0\n")

    def test_unknown_unit_is_rejected(self):
        bad = XML_CDM.replace(
            'units="m">500.0</MISS_DISTANCE>', 'units="furlong">500.0</MISS_DISTANCE>'
        )
        with pytest.raises(ValueError):
            parse_cdm(bad)


class TestWriterMethodField:
    def _event(self):
        return ConjunctionEvent(
            primary=25544,
            secondary=48274,
            tca=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            miss_distance_km=0.5,
            relative_velocity_km_s=14.0,
            risk=RiskLevel.HIGH,
        )

    def test_method_is_emitted_next_to_the_probability(self):
        text = generate_cdm(self._event(), collision_probability=2.5e-5)
        assert "COLLISION_PROBABILITY " in text
        assert f"COLLISION_PROBABILITY_METHOD  = {DEFAULT_PC_METHOD_NAME}" in text

    def test_method_is_overridable_and_round_trips(self):
        text = generate_cdm(
            self._event(), collision_probability=2.5e-5, collision_probability_method="ALFANO-2005"
        )
        detail = parse_cdm_detailed(text)
        assert detail.collision_probability_method == "ALFANO-2005"
        assert detail.parsed.stated_collision_probability == pytest.approx(2.5e-5)

    def test_no_probability_means_no_method_line(self):
        text = generate_cdm(self._event())
        assert "COLLISION_PROBABILITY" not in text

    def test_generated_message_has_no_leading_indentation(self):
        text = generate_cdm(self._event(), collision_probability=2.5e-5)
        assert text.startswith("CCSDS_CDM_VERS")
        assert all(not line.startswith(" ") for line in text.splitlines())


class TestCdmEndpoint:
    def _client(self):
        app = FastAPI()
        app.include_router(pc_router)
        return TestClient(app)

    def test_xml_cdm_through_the_router(self):
        response = self._client().post("/pc/cdm", json={"cdm_text": XML_CDM})
        assert response.status_code == 200
        body = response.json()
        assert body["source_format"] == "XML"
        assert body["object1"] == "25544"
        assert body["stated_collision_probability_method"] == "FOSTER-1992"
        assert 0.0 <= body["probabilities"]["foster"] <= 1.0
        assert body["comments"]

    def test_kvn_cdm_through_the_same_endpoint(self):
        response = self._client().post("/pc/cdm", json={"cdm_text": KVN_TWIN})
        assert response.status_code == 200
        assert response.json()["source_format"] == "KVN"

    def test_bad_cdm_is_a_422(self):
        response = self._client().post("/pc/cdm", json={"cdm_text": "not a cdm at all"})
        assert response.status_code == 422
