"""Operations routes: Space-Track ingest, alerting, reports, fleets, doctor.

These endpoints are the operator-facing side of the service: pulling elements
from the authoritative catalog, pushing conjunction alerts to a chat channel,
producing a report to attach to a ticket, screening a whole fleet, and checking
that the install is sound. Nothing here changes risk classification or any
orbital computation; the conjunction pipeline is called through its public
entry point and its results are only formatted.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from sda import alerts, doctor, spacetrack
from sda import fleet as fleet_mod
from sda import report as report_mod
from sda.fleet import FleetScreenRequest, FleetScreenResult, call_find_conjunctions
from sda.report import ReportRequest

router = APIRouter(tags=["Operations"])


# --- request / response models owned by this router ---


class SpaceTrackRefreshRequest(BaseModel):
    """Which element sets to pull from Space-Track."""

    norad_ids: list[int] | None = None
    object_class: str | None = Field(
        default=None,
        description="gp OBJECT_TYPE filter: PAYLOAD, ROCKET BODY, DEBRIS or UNKNOWN",
    )
    limit: int = Field(default=500, ge=1, le=20000)


class WebhookView(BaseModel):
    """A configured webhook with its secret path redacted."""

    url: str
    kind: str
    enabled: bool


class AlertConfigView(BaseModel):
    """Alert configuration as returned to a caller."""

    enabled: bool
    rules: alerts.AlertRules
    webhooks: list[WebhookView]
    dedupe_window_minutes: float


def _config_view(config: alerts.AlertConfig) -> AlertConfigView:
    return AlertConfigView(
        enabled=config.enabled,
        rules=config.rules,
        webhooks=[
            WebhookView(url=alerts.mask_url(w.url), kind=w.kind, enabled=w.enabled)
            for w in config.webhooks
        ],
        dedupe_window_minutes=config.dedupe_window_minutes,
    )


def _spacetrack_client() -> spacetrack.SpaceTrackClient:
    """Build a client, or raise the 503 that means 'no credentials'."""
    if not spacetrack.credentials_available():
        raise HTTPException(status_code=503, detail=spacetrack.MISSING_CREDENTIALS_MESSAGE)
    try:
        return spacetrack.SpaceTrackClient()
    except spacetrack.SpaceTrackCredentialsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# --- Space-Track ---


@router.post(
    "/tle/refresh/spacetrack",
    tags=["TLE"],
    summary="Refresh the catalog from Space-Track.org",
    response_description="Ingest counts before and after the refresh",
)
def refresh_from_spacetrack(request: SpaceTrackRefreshRequest) -> dict[str, Any]:
    """Pull the latest GP element sets from Space-Track into the catalog.

    Requires SDA_SPACETRACK_USER and SDA_SPACETRACK_PASS; without them the
    endpoint answers 503 rather than silently falling back to CelesTrak. All
    ingestion goes through TLEStore.load_from_text(), so every element set is
    validated by Satrec.twoline2rv() (WGS72) before it reaches the catalog.
    """
    from sda.api import store

    client = _spacetrack_client()
    try:
        with client:
            text = client.gp_latest(
                norad_ids=request.norad_ids,
                object_class=request.object_class,
                limit=request.limit,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except spacetrack.SpaceTrackAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except spacetrack.SpaceTrackError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    before = store.count()
    ingested = store.load_from_text(text) if text.strip() else 0
    return {
        "status": "ok",
        "source": "space-track",
        "ingested": ingested,
        "satellites_before": before,
        "satellites_after": store.count(),
    }


@router.get(
    "/reentries",
    tags=["TLE"],
    summary="Predicted reentries from Space-Track TIP messages",
    response_description="Normalized Tracking and Impact Prediction records",
)
def reentries(
    limit: int = Query(default=25, ge=1, le=200, description="Maximum TIP messages"),
) -> dict[str, Any]:
    """Latest Tracking and Impact Prediction messages (decaying objects)."""
    client = _spacetrack_client()
    try:
        with client:
            rows = client.tip_messages(limit=limit)
    except spacetrack.SpaceTrackError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    records = [spacetrack.normalize_tip(row) for row in rows]
    return {"count": len(records), "reentries": records}


@router.get(
    "/spacetrack/cdm",
    tags=["Conjunctions"],
    summary="Publicly released conjunction data messages from Space-Track",
)
def spacetrack_cdm(
    limit: int = Query(default=25, ge=1, le=200, description="Maximum CDM records"),
) -> dict[str, Any]:
    """Space-Track cdm_public rows, soonest TCA first.

    Only high-interest events are published there, so an empty list is a normal
    answer rather than a failure.
    """
    client = _spacetrack_client()
    try:
        with client:
            rows = client.cdm_public(limit=limit)
    except spacetrack.SpaceTrackError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"count": len(rows), "cdms": rows}


# --- Alerts ---


@router.get("/alerts/config", tags=["Alerts"], summary="Current alerting configuration")
def get_alerts_config() -> AlertConfigView:
    """Rules, dedupe window and webhook targets, with webhook paths redacted."""
    return _config_view(alerts.get_config())


@router.post("/alerts/config", tags=["Alerts"], summary="Replace the alerting configuration")
def set_alerts_config(config: alerts.AlertConfig) -> AlertConfigView:
    """Set rules and webhook targets for the running process.

    In-memory only: the configuration resets to SDA_WEBHOOK_URLS on restart.
    """
    return _config_view(alerts.set_config(config))


@router.get("/alerts/log", tags=["Alerts"], summary="Recently dispatched alerts")
def get_alerts_log(
    limit: int = Query(default=50, ge=1, le=250, description="Maximum entries"),
) -> dict[str, Any]:
    """The in-memory alert log, newest first."""
    entries = alerts.get_alert_log(limit=limit)
    return {"count": len(entries), "alerts": entries}


@router.delete("/alerts/log", tags=["Alerts"], summary="Clear the alert log")
def clear_alerts_log() -> dict[str, Any]:
    """Drop every entry from the in-memory alert log."""
    return {"status": "cleared", "entries_removed": alerts.clear_alert_log()}


@router.post("/alerts/test", tags=["Alerts"], summary="Send a synthetic alert to every webhook")
def post_alerts_test() -> dict[str, Any]:
    """Deliver a test alert so webhook wiring can be verified end to end."""
    config = alerts.get_config()
    alert = alerts.send_test_alert(config=config)
    return {
        "status": "sent" if alert.deliveries else "no_webhooks_configured",
        "alert": alert,
        "deliveries": alert.deliveries,
    }


# --- Reports ---


@router.post(
    "/report",
    tags=["Reports"],
    summary="Conjunction report as a self-contained HTML page or a PDF",
    response_description="text/html or application/pdf",
)
def conjunction_report(request: ReportRequest) -> Response:
    """Run the screening pipeline and render the result as a report.

    PDF output needs the optional fpdf2 package; without it the endpoint
    answers 501 with the install hint instead of returning a broken file.
    """
    from sda.api import store

    events = call_find_conjunctions(
        store, request.norad_ids, request.hours, request.threshold_km
    )

    if request.format == "pdf":
        try:
            pdf_bytes = report_mod.render_pdf_report(
                events,
                title=request.title,
                hours=request.hours,
                threshold_km=request.threshold_km,
            )
        except report_mod.PdfUnavailableError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="conjunction-report.pdf"'},
        )

    html = report_mod.render_html_report(
        events,
        title=request.title,
        hours=request.hours,
        threshold_km=request.threshold_km,
    )
    return HTMLResponse(content=html)


# --- Fleets ---


@router.get("/fleet/example", tags=["Fleet"], summary="Example fleet definition (YAML)")
def fleet_example() -> dict[str, Any]:
    """The bundled example fleet, as YAML text and as the parsed object."""
    return {
        "format": "yaml",
        "yaml": fleet_mod.EXAMPLE_FLEET_YAML,
        "parsed": fleet_mod.example_fleet(),
    }


@router.post(
    "/fleet/screen",
    tags=["Fleet"],
    summary="Screen an operator fleet against the tracked catalog",
    response_description="Events involving fleet objects, flagged against fleet thresholds",
)
def fleet_screen(request: FleetScreenRequest) -> FleetScreenResult:
    """Screen every fleet object against the catalog (or against the fleet).

    The fleet arrives either as a structured object or as YAML/JSON text in
    `fleet_text`.
    """
    from sda.api import store

    definition = request.fleet
    if definition is None:
        if not request.fleet_text:
            raise HTTPException(
                status_code=422, detail="Provide either 'fleet' or 'fleet_text'"
            )
        try:
            definition = fleet_mod.parse_fleet(request.fleet_text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not definition.norad_ids:
        raise HTTPException(status_code=422, detail="Fleet has no norad_ids")

    return fleet_mod.screen_fleet(
        definition,
        store=store,
        hours=request.hours,
        threshold_km=request.threshold_km,
        against=request.against,
        max_events=request.max_events,
    )


# --- Doctor ---


@router.get(
    "/doctor",
    tags=["System"],
    summary="Environment diagnostics (offline)",
    response_description="Package, environment and catalog checks",
)
def doctor_report() -> doctor.DoctorReport:
    """Run the offline diagnostics. Network probes stay CLI-only.

    A check that raises or takes longer than doctor.CHECK_TIMEOUT_SECONDS is reported
    as a FAIL row instead of holding the response.
    """
    return doctor.run_checks(network=False)
