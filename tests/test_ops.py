"""Operations tests: Space-Track client, alerting, reports, fleets, doctor.

Offline and deterministic. Every httpx call is replaced by a stub, and the
conjunction pipeline is stubbed where a route needs events, so nothing here
depends on catalog contents or on propagation timing.
"""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sda import alerts, doctor, fleet, ratelimit, report, spacetrack
from sda.api import app
from sda.models import CollisionProbability, ConjunctionEvent, RiskLevel

client = TestClient(app)

TWO_SATS_TLE = """ISS (ZARYA)
1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997
2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014
CSS (TIANHE)
1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991
2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001"""

TCA = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)


def make_event(
    primary: int = 25544,
    secondary: int = 48274,
    miss_km: float = 0.42,
    risk: RiskLevel = RiskLevel.CRITICAL,
    pc: float | None = 1.0e-3,
    tca: datetime = TCA,
) -> ConjunctionEvent:
    probability = None
    if pc is not None:
        probability = CollisionProbability(
            probability=pc,
            miss_distance_km=miss_km,
            combined_sigma_km=0.5,
            mahalanobis_distance=miss_km / 0.5,
            hard_body_radius_km=0.02,
        )
    return ConjunctionEvent(
        primary=primary,
        secondary=secondary,
        primary_name="ISS (ZARYA)",
        secondary_name="CSS (TIANHE)",
        tca=tca,
        miss_distance_km=miss_km,
        relative_velocity_km_s=11.7,
        risk=risk,
        collision_probability=probability,
    )


@pytest.fixture(autouse=True)
def clean_alert_state():
    alerts.reset_state()
    yield
    alerts.reset_state()


@pytest.fixture
def no_spacetrack_credentials(monkeypatch):
    monkeypatch.delenv(spacetrack.USER_ENV, raising=False)
    monkeypatch.delenv(spacetrack.PASS_ENV, raising=False)


# --- httpx stubs ---


class StubResponse:
    def __init__(self, status_code: int = 200, text: str = "", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("response carries no JSON")
        return self._payload


class StubHttpClient:
    """Minimal stand-in for httpx.Client covering post/get/close."""

    def __init__(self, responses=None, login=None):
        self._responses = list(responses or [])
        self.login_response = login if login is not None else StubResponse(200, "")
        self.login_calls = 0
        self.login_payloads: list[dict] = []
        self.get_urls: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, data=None, json=None, **kwargs):
        if data is not None:
            self.login_calls += 1
            self.login_payloads.append(data)
            return self.login_response
        self.posts.append((url, json or {}))
        return StubResponse(204)

    def get(self, url, **kwargs):
        self.get_urls.append(url)
        if not self._responses:
            return StubResponse(200, "")
        return self._responses.pop(0)

    def close(self):
        self.closed = True


def stub_throttle() -> spacetrack.Throttle:
    return spacetrack.Throttle(clock=lambda: 0.0, sleep=lambda _s: None)


# Captured before any monkeypatch so a patched factory can still build a real client.
REAL_CLIENT_CLASS = spacetrack.SpaceTrackClient


def make_spacetrack_client(responses=None, login=None) -> tuple[object, StubHttpClient]:
    http = StubHttpClient(responses=responses, login=login)
    st = REAL_CLIENT_CLASS(
        user="operator", password="secret", client=http, throttle=stub_throttle()
    )
    return st, http


# --- Space-Track client ---


def test_credentials_read_from_environment():
    assert spacetrack.credentials({}) is None
    assert spacetrack.credentials_available({"SDA_SPACETRACK_USER": "u"}) is False
    found = spacetrack.credentials(
        {"SDA_SPACETRACK_USER": "u", "SDA_SPACETRACK_PASS": "p"}
    )
    assert found == ("u", "p")


def test_client_without_credentials_raises(no_spacetrack_credentials):
    with pytest.raises(spacetrack.SpaceTrackCredentialsError):
        spacetrack.SpaceTrackClient()


def test_gp_latest_logs_in_once_and_returns_tle_text():
    st, http = make_spacetrack_client([StubResponse(200, TWO_SATS_TLE)])
    text = st.gp_latest(norad_ids=[25544, 48274], limit=10)

    assert text == TWO_SATS_TLE
    assert http.login_calls == 1
    assert http.login_payloads[0] == {"identity": "operator", "password": "secret"}
    url = http.get_urls[0]
    assert "class/gp" in url
    assert "NORAD_CAT_ID/25544,48274" in url
    assert "format/tle" in url
    assert "limit/10" in url

    # Session reuse: a second query does not log in again.
    http._responses.append(StubResponse(200, TWO_SATS_TLE))
    st.gp_latest(norad_ids=[25544], limit=1)
    assert http.login_calls == 1


def test_gp_latest_text_goes_through_the_store_parser():
    from sda.tle_store import TLEStore

    st, _http = make_spacetrack_client([StubResponse(200, TWO_SATS_TLE)])
    store = TLEStore()
    ingested = store.load_from_text(st.gp_latest(limit=5))
    assert ingested == 2
    assert store.get(25544) is not None


def test_gp_latest_rejects_unknown_object_class():
    st, _http = make_spacetrack_client([StubResponse(200, "")])
    with pytest.raises(ValueError):
        st.gp_latest(object_class="SPACE JUNK")


def test_expired_session_triggers_one_relogin():
    st, http = make_spacetrack_client(
        [StubResponse(401), StubResponse(200, TWO_SATS_TLE)]
    )
    assert st.gp_latest(limit=2) == TWO_SATS_TLE
    assert http.login_calls == 2
    assert len(http.get_urls) == 2


def test_rate_limited_query_raises():
    st, _http = make_spacetrack_client([StubResponse(429)])
    with pytest.raises(spacetrack.SpaceTrackError, match="rate limit"):
        st.gp_latest(limit=2)


def test_login_rejection_raises_auth_error():
    st, _http = make_spacetrack_client(login=StubResponse(200, "Login Failed"))
    with pytest.raises(spacetrack.SpaceTrackAuthError):
        st.gp_latest(limit=1)


def test_tip_messages_are_normalized():
    row = {
        "NORAD_CAT_ID": "25544",
        "OBJECT_NAME": "ISS (ZARYA)",
        "DECAY_EPOCH": "2024-03-01 12:00:00",
        "MSG_EPOCH": "2024-02-29 12:00:00",
        "WINDOW": "60",
        "LAT": "-12.5",
        "LON": "130.0",
        "HIGH_INTEREST": "Y",
    }
    st, _http = make_spacetrack_client([StubResponse(200, payload=[row])])
    records = [spacetrack.normalize_tip(r) for r in st.tip_messages(limit=5)]
    assert records[0]["norad_id"] == 25544
    assert records[0]["window_minutes"] == 60.0
    assert records[0]["latitude_deg"] == -12.5
    assert records[0]["high_interest"] is True


def test_throttle_waits_when_the_minute_budget_is_spent():
    slept: list[float] = []
    now = [0.0]
    throttle = spacetrack.Throttle(
        per_minute=3, per_hour=100, clock=lambda: now[0], sleep=slept.append
    )
    for _ in range(3):
        assert throttle.acquire() == 0.0
    assert throttle.acquire() > 0.0
    assert slept and slept[0] > 0.0


# --- Space-Track routes ---


def test_refresh_without_credentials_returns_503(no_spacetrack_credentials):
    resp = client.post("/tle/refresh/spacetrack", json={"limit": 5})
    assert resp.status_code == 503
    assert "space-track" in resp.json()["detail"].lower()
    assert spacetrack.USER_ENV in resp.json()["detail"]


def test_reentries_without_credentials_returns_503(no_spacetrack_credentials):
    resp = client.get("/reentries")
    assert resp.status_code == 503


def test_refresh_ingests_through_the_store(monkeypatch):
    monkeypatch.setenv(spacetrack.USER_ENV, "operator")
    monkeypatch.setenv(spacetrack.PASS_ENV, "secret")

    def factory(*args, **kwargs):
        st, _http = make_spacetrack_client([StubResponse(200, TWO_SATS_TLE)])
        return st

    monkeypatch.setattr(spacetrack, "SpaceTrackClient", factory)
    resp = client.post("/tle/refresh/spacetrack", json={"norad_ids": [25544, 48274]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "space-track"
    assert body["ingested"] == 2


def test_reentries_route_returns_normalized_rows(monkeypatch):
    monkeypatch.setenv(spacetrack.USER_ENV, "operator")
    monkeypatch.setenv(spacetrack.PASS_ENV, "secret")
    row = {"NORAD_CAT_ID": "48274", "OBJECT_NAME": "CSS", "WINDOW": "30"}

    def factory(*args, **kwargs):
        st, _http = make_spacetrack_client([StubResponse(200, payload=[row])])
        return st

    monkeypatch.setattr(spacetrack, "SpaceTrackClient", factory)
    resp = client.get("/reentries?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["reentries"][0]["norad_id"] == 48274


# --- alert rules ---


def test_rules_fire_on_probability_threshold():
    rules = alerts.AlertRules(pc_threshold=1e-4, min_risk=None, alert_on_pc_increase=False)
    reasons = alerts.evaluate_event(make_event(pc=1e-3), rules)
    assert any("threshold" in r for r in reasons)
    quiet = alerts.evaluate_event(make_event(pc=1e-9), rules)
    assert quiet == []


def test_rules_fire_on_risk_level():
    rules = alerts.AlertRules(pc_threshold=None, min_risk=RiskLevel.HIGH)
    assert alerts.evaluate_event(make_event(risk=RiskLevel.HIGH, pc=None), rules)
    assert alerts.evaluate_event(make_event(risk=RiskLevel.LOW, pc=None), rules) == []


def test_rules_fire_when_probability_climbs():
    rules = alerts.AlertRules(pc_threshold=None, min_risk=None, pc_increase_factor=2.0)
    reasons = alerts.evaluate_event(make_event(pc=4e-5), rules, previous_pc=1e-5)
    assert any("rising" in r for r in reasons)
    steady = alerts.evaluate_event(make_event(pc=1.1e-5), rules, previous_pc=1e-5)
    assert steady == []


def test_dispatch_deduplicates_the_same_pair_and_tca():
    config = alerts.AlertConfig(webhooks=[], dedupe_window_minutes=60.0)
    first = alerts.dispatch_alerts([make_event()], config=config, now=TCA)
    second = alerts.dispatch_alerts([make_event()], config=config, now=TCA + timedelta(minutes=5))
    assert len(first) == 1
    assert second == []
    # A different TCA for the same pair is a different event.
    later = alerts.dispatch_alerts(
        [make_event(tca=TCA + timedelta(hours=6))],
        config=config,
        now=TCA + timedelta(minutes=6),
    )
    assert len(later) == 1


def test_dedupe_window_expires():
    config = alerts.AlertConfig(webhooks=[], dedupe_window_minutes=10.0)
    alerts.dispatch_alerts([make_event()], config=config, now=TCA)
    again = alerts.dispatch_alerts(
        [make_event()], config=config, now=TCA + timedelta(minutes=11)
    )
    assert len(again) == 1


def test_disabled_config_sends_nothing():
    config = alerts.AlertConfig(enabled=False)
    assert alerts.dispatch_alerts([make_event()], config=config, now=TCA) == []


# --- webhook payloads and delivery ---


def test_webhook_kind_detection():
    assert alerts.detect_webhook_kind("https://hooks.slack.com/services/T/B/X") == "slack"
    assert alerts.detect_webhook_kind("https://discord.com/api/webhooks/1/x") == "discord"
    assert alerts.detect_webhook_kind("https://ops.example.com/hook") == "generic"


def test_webhook_targets_parse_with_explicit_kinds():
    targets = alerts.parse_webhook_targets(
        "https://hooks.slack.com/services/A/B/C, generic=https://ops.example.com/hook, junk"
    )
    assert [t.kind for t in targets] == ["slack", "generic"]


def test_mask_url_hides_the_secret_path():
    masked = alerts.mask_url("https://hooks.slack.com/services/T000/B000/SECRET")
    assert "SECRET" not in masked
    assert masked == "https://hooks.slack.com/..."


def test_payload_shapes_per_webhook_kind():
    config = alerts.AlertConfig(
        webhooks=[
            alerts.WebhookTarget(url="https://hooks.slack.com/services/A/B/C", kind="slack"),
            alerts.WebhookTarget(url="https://discord.com/api/webhooks/1/x", kind="discord"),
            alerts.WebhookTarget(url="https://ops.example.com/hook", kind="generic"),
        ]
    )
    http = StubHttpClient()
    sent = alerts.dispatch_alerts([make_event()], config=config, client=http, now=TCA)

    assert len(sent) == 1
    assert len(http.posts) == 3
    slack_payload = http.posts[0][1]
    discord_payload = http.posts[1][1]
    generic_payload = http.posts[2][1]

    assert "text" in slack_payload and "blocks" in slack_payload
    assert "content" in discord_payload and discord_payload["embeds"]
    assert generic_payload["source"] == "space-collision-predictor"
    assert generic_payload["primary"] == 25544
    assert generic_payload["risk"] == "CRITICAL"
    assert all(d.ok for d in sent[0].deliveries)


def test_delivery_failure_is_recorded_not_raised():
    class BoomClient(StubHttpClient):
        def post(self, url, data=None, json=None, **kwargs):
            raise RuntimeError("connection refused")

    config = alerts.AlertConfig(
        webhooks=[alerts.WebhookTarget(url="https://ops.example.com/hook", kind="generic")]
    )
    sent = alerts.dispatch_alerts([make_event()], config=config, client=BoomClient(), now=TCA)
    assert len(sent) == 1
    assert sent[0].deliveries[0].ok is False
    assert "connection refused" in sent[0].deliveries[0].error


# --- alert routes ---


def test_alert_config_round_trip_masks_webhooks():
    payload = {
        "enabled": True,
        "rules": {"pc_threshold": 1e-5, "min_risk": "HIGH"},
        "webhooks": [{"url": "https://hooks.slack.com/services/T/B/SECRET", "kind": "slack"}],
        "dedupe_window_minutes": 30.0,
    }
    resp = client.post("/alerts/config", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["dedupe_window_minutes"] == 30.0
    assert body["webhooks"][0]["url"] == "https://hooks.slack.com/..."
    assert "SECRET" not in resp.text

    again = client.get("/alerts/config")
    assert again.json()["rules"]["pc_threshold"] == 1e-5


def test_alert_log_route_lists_then_clears():
    alerts.dispatch_alerts(
        [make_event()], config=alerts.AlertConfig(webhooks=[]), now=TCA
    )
    resp = client.get("/alerts/log")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1

    cleared = client.delete("/alerts/log")
    assert cleared.json()["entries_removed"] == 1
    assert client.get("/alerts/log").json()["count"] == 0


def test_alert_test_route_without_webhooks():
    client.post("/alerts/config", json={"enabled": True, "webhooks": []})
    resp = client.post("/alerts/test")
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_webhooks_configured"
    assert resp.json()["alert"]["reasons"]


# --- reports ---


def test_html_report_is_self_contained_and_ascii():
    html = report.render_html_report(
        [make_event(), make_event(secondary=20580, miss_km=3.2, risk=RiskLevel.MODERATE)],
        hours=24.0,
        threshold_km=10.0,
        generated_at=TCA,
    )
    html.encode("ascii")  # raises if a non-ASCII character slipped in
    assert html.startswith("<!DOCTYPE html>")
    assert "http://" not in html and "https://" not in html
    assert "25544" in html and "48274" in html
    assert "CRITICAL" in html


def test_report_lists_probability_by_method():
    event = make_event()
    methods = report.pc_methods_for(event)
    assert "2D (Chan/Alfano)" in methods
    html = report.render_html_report([event], generated_at=TCA)
    assert "2D (Chan/Alfano)" in html


def test_empty_report_still_renders():
    html = report.render_html_report([], hours=6.0, threshold_km=5.0, generated_at=TCA)
    assert "0" in html
    html.encode("ascii")


def test_report_route_returns_html(monkeypatch):
    monkeypatch.setattr(
        "sda.conjunction.find_conjunctions",
        lambda store, norad_ids=None, hours=24.0, threshold_km=10.0, **kw: [make_event()],
    )
    resp = client.post("/report", json={"format": "html", "hours": 12.0})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Conjunction" in resp.text


def test_pdf_report_reports_the_missing_dependency(monkeypatch):
    if report.pdf_available():
        pytest.skip("fpdf2 is installed, so the 501 path cannot be exercised")
    monkeypatch.setattr(
        "sda.conjunction.find_conjunctions",
        lambda store, norad_ids=None, hours=24.0, threshold_km=10.0, **kw: [make_event()],
    )
    resp = client.post("/report", json={"format": "pdf"})
    assert resp.status_code == 501
    assert "pip install fpdf2" in resp.json()["detail"]


# --- fleets ---


class FakeTle:
    def __init__(self, norad_id: int):
        self.norad_id = norad_id


class FakeStore:
    def __init__(self, norad_ids):
        self._ids = list(norad_ids)

    def get_all(self):
        return [FakeTle(n) for n in self._ids]

    def count(self):
        return len(self._ids)


def test_example_fleet_parses():
    definition = fleet.example_fleet()
    assert definition.norad_ids
    assert definition.hard_body_radius_km == definition.hard_body_radius_m / 1000.0


def test_parse_fleet_accepts_yaml_and_json():
    from_json = fleet.parse_fleet('{"name": "ops", "norad_ids": [25544], "hard_body_radius_m": 5}')
    assert from_json.name == "ops"
    assert from_json.norad_ids == [25544]

    from_yaml = fleet.parse_fleet(fleet.EXAMPLE_FLEET_YAML)
    assert from_yaml.norad_ids


def test_parse_fleet_rejects_rubbish():
    with pytest.raises(ValueError):
        fleet.parse_fleet("not a mapping at all")


def test_call_find_conjunctions_passes_only_known_arguments(monkeypatch):
    seen = {}

    def narrow(store, hours=24.0):
        seen["kwargs"] = {"hours": hours}
        seen["store"] = store
        return [make_event()]

    monkeypatch.setattr("sda.conjunction.find_conjunctions", narrow)
    events = fleet.call_find_conjunctions("STORE", [25544], 6.0, 12.0)
    assert len(events) == 1
    assert seen["kwargs"] == {"hours": 6.0}
    assert seen["store"] == "STORE"


def test_screen_fleet_keeps_only_fleet_events(monkeypatch):
    events = [
        make_event(primary=25544, secondary=48274, miss_km=0.4, risk=RiskLevel.CRITICAL),
        make_event(primary=11111, secondary=22222, miss_km=1.0, risk=RiskLevel.HIGH),
    ]
    monkeypatch.setattr(
        "sda.conjunction.find_conjunctions",
        lambda store, norad_ids=None, hours=24.0, threshold_km=10.0, **kw: events,
    )
    definition = fleet.FleetDefinition(name="ops", norad_ids=[25544, 99999])
    result = fleet.screen_fleet(definition, store=FakeStore([25544, 48274]), hours=24.0)

    assert result.fleet_name == "ops"
    assert result.events_total == 1
    assert result.events_flagged == 1
    assert result.events[0].fleet_norad_id == 25544
    assert result.events[0].other_norad_id == 48274
    assert result.events[0].pc_scaled_to_fleet_hbr is not None
    assert result.missing_norad_ids == [99999]


def test_fleet_example_route():
    resp = client.get("/fleet/example")
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "yaml"
    assert body["parsed"]["norad_ids"]


def test_fleet_screen_route(monkeypatch):
    monkeypatch.setattr(
        "sda.conjunction.find_conjunctions",
        lambda store, norad_ids=None, hours=24.0, threshold_km=10.0, **kw: [make_event()],
    )
    client.post("/tle", json={"tle_text": TWO_SATS_TLE})
    resp = client.post(
        "/fleet/screen",
        json={"fleet": {"name": "ops", "norad_ids": [25544]}, "hours": 12.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fleet_name"] == "ops"
    assert body["events_total"] == 1
    assert body["events"][0]["other_norad_id"] == 48274


def test_fleet_screen_route_requires_a_fleet():
    resp = client.post("/fleet/screen", json={"hours": 12.0})
    assert resp.status_code == 422


# --- doctor ---


def test_doctor_runs_offline():
    result = doctor.run_checks(network=False)
    sections = {check.section for check in result.checks}
    assert "network" not in sections
    assert any(check.name == "python" for check in result.checks)
    assert result.network_checked is False
    assert result.ok + result.warnings + result.failures <= len(result.checks)


def test_doctor_masks_secret_environment_values(monkeypatch):
    monkeypatch.setenv("SDA_SPACETRACK_PASS", "hunter2-very-secret")
    checks = doctor.check_environment()
    rendered = " ".join(check.detail for check in checks)
    assert "hunter2-very-secret" not in rendered
    masked = doctor.mask("hunter2-very-secret")
    assert "hunter2" not in masked
    assert "*" in masked


def test_doctor_table_is_ascii_and_bounded():
    report_obj = doctor.run_checks(network=False)
    table = doctor.format_table(report_obj)
    table.encode("ascii")
    assert "python" in table
    assert max(len(line) for line in table.splitlines()) < 120


def test_doctor_cli_exit_code(capsys):
    code = doctor.main(["--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert '"checks"' in captured.out


def test_doctor_route():
    resp = client.get("/doctor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]
    assert "python" in {check["name"] for check in body["checks"]}


def _stub_fast_checks(monkeypatch):
    """Replace every check except python so only the one under test can be slow."""
    monkeypatch.setattr(doctor, "check_packages", lambda: [])
    monkeypatch.setattr(doctor, "check_environment", lambda: [])
    monkeypatch.setattr(
        doctor,
        "check_catalog",
        lambda: doctor.CheckResult(section="catalog", name="demo TLEs", status=doctor.STATUS_OK),
    )


def _hanging_python_check(release: threading.Event):
    def check() -> doctor.CheckResult:
        release.wait(5)  # safety cap; the tests set the event as soon as they finish
        return doctor.CheckResult(section="runtime", name="python", status=doctor.STATUS_OK)

    return check


def test_doctor_marks_a_hung_check_failed_instead_of_waiting(monkeypatch):
    _stub_fast_checks(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(doctor, "check_python", _hanging_python_check(release))

    started = time.monotonic()
    try:
        result = doctor.run_checks(network=False, timeout=0.05)
    finally:
        release.set()
    elapsed = time.monotonic() - started

    row = next(check for check in result.checks if check.name == "python")
    assert row.status == doctor.STATUS_FAIL
    assert "0.05" in row.detail
    assert result.failures == 1
    assert not result.healthy
    assert elapsed < 1.0


def test_doctor_reports_a_raising_check_as_failed(monkeypatch):
    _stub_fast_checks(monkeypatch)

    def boom() -> doctor.CheckResult:
        raise OSError("management service unavailable")

    monkeypatch.setattr(doctor, "check_python", boom)

    result = doctor.run_checks(network=False)

    row = next(check for check in result.checks if check.name == "python")
    assert row.status == doctor.STATUS_FAIL
    assert "OSError" in row.detail
    assert result.failures == 1


def test_doctor_route_answers_within_budget_when_a_check_hangs(monkeypatch):
    _stub_fast_checks(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(doctor, "check_python", _hanging_python_check(release))
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT_SECONDS", 0.05)

    started = time.monotonic()
    try:
        resp = client.get("/doctor")
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert elapsed < 1.0
    rows = {check["name"]: check for check in resp.json()["checks"]}
    assert rows["python"]["status"] == "FAIL"
    assert rows["demo TLEs"]["status"] == "OK"


def _python_row(report) -> doctor.CheckResult:
    return next(check for check in report.checks if check.name == "python")


def _live_doctor_threads(name: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == f"doctor-{name}" and t.is_alive()]


def _wait_for_doctor_threads(name: str) -> None:
    for thread in _live_doctor_threads(name):
        thread.join(2.0)


def test_doctor_does_not_start_a_hung_check_again(monkeypatch):
    """GET /doctor is unauthenticated: a stuck check must cost one thread, not one per request."""
    _stub_fast_checks(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(doctor, "check_python", _hanging_python_check(release))
    baseline = len(_live_doctor_threads("python"))

    try:
        rows = [
            _python_row(doctor.run_checks(network=False, timeout=0.02)) for _ in range(25)
        ]
        extra = len(_live_doctor_threads("python")) - baseline
    finally:
        release.set()

    assert extra <= 1
    assert all(row.status == doctor.STATUS_FAIL for row in rows)
    assert "no answer within" in rows[0].detail
    assert all("still running from an earlier request" in row.detail for row in rows[1:])


def test_doctor_runs_a_check_again_once_its_hang_clears(monkeypatch):
    _stub_fast_checks(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(doctor, "check_python", _hanging_python_check(release))

    try:
        stuck = _python_row(doctor.run_checks(network=False, timeout=0.02))
    finally:
        release.set()
    _wait_for_doctor_threads("python")
    recovered = _python_row(doctor.run_checks(network=False, timeout=1.0))

    assert stuck.status == doctor.STATUS_FAIL
    assert recovered.status == doctor.STATUS_OK


def test_doctor_calls_close_together_share_one_run(monkeypatch):
    _stub_fast_checks(monkeypatch)
    gate = threading.Event()
    runs: list[int] = []

    def slow_python() -> doctor.CheckResult:
        runs.append(1)
        gate.wait(5)
        return doctor.CheckResult(section="runtime", name="python", status=doctor.STATUS_OK)

    monkeypatch.setattr(doctor, "check_python", slow_python)
    reports: list[doctor.DoctorReport] = []
    callers = [
        threading.Thread(
            target=lambda: reports.append(doctor.run_checks(network=False, timeout=3.0))
        )
        for _ in range(5)
    ]
    try:
        for caller in callers:
            caller.start()
        time.sleep(0.2)  # every caller is now waiting on the one run
    finally:
        gate.set()
    for caller in callers:
        caller.join(5.0)

    assert len(runs) == 1
    assert len(reports) == 5
    assert all(_python_row(rep).status == doctor.STATUS_OK for rep in reports)


def test_doctor_healthy_report_is_unaffected_by_the_guard(monkeypatch):
    _stub_fast_checks(monkeypatch)
    monkeypatch.setattr(
        doctor,
        "check_python",
        lambda: doctor.CheckResult(section="runtime", name="python", status=doctor.STATUS_OK),
    )

    first = doctor.run_checks(network=False)
    second = doctor.run_checks(network=False)

    for rep in (first, second):
        assert rep.failures == 0
        assert rep.healthy
        assert {c.name for c in rep.checks} == {"python", "demo TLEs"}
        assert not any("earlier request" in c.detail for c in rep.checks)


# --- posture wiring cross-check ---


def test_ops_router_is_registered():
    paths = set(app.openapi()["paths"])
    for expected in (
        "/tle/refresh/spacetrack",
        "/reentries",
        "/alerts/config",
        "/alerts/log",
        "/alerts/test",
        "/report",
        "/fleet/example",
        "/fleet/screen",
        "/doctor",
    ):
        assert expected in paths


def test_ops_paths_are_not_rate_limited_by_default():
    # The limiter guards /api/ and /agent/; operator routes sit outside it.
    assert not any(p.startswith(ratelimit.rate_limit_paths()) for p in ("/doctor", "/report"))
