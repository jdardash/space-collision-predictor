"""Webhook alerting for conjunction events.

Turns detected conjunctions into notifications on Slack, Discord or any
endpoint that accepts a JSON POST. The module owns three pieces of state that
live for the process lifetime: the active configuration, a short history of
delivered alerts, and the dedupe/trend bookkeeping that stops the same event
from being announced on every screening run.

Configuration
-------------
SDA_WEBHOOK_URLS
    Comma-separated webhook targets. Each entry is a bare URL, whose flavour
    is inferred from the host, or "kind=url" to force one ("slack=", "discord="
    or "generic="). Example:
    "https://hooks.slack.com/services/AAA/BBB/CCC,generic=https://ops.example/hook"

Rules
-----
Three independent triggers, any of which fires an alert:

probability
    Pc at or above `pc_threshold` (default 1e-4, the CARA/NASA "yellow" level).
risk
    Risk level at or above `min_risk` (default HIGH), using the ordering
    NEGLIGIBLE < LOW < MODERATE < HIGH < CRITICAL. The classification itself
    is not touched here; this module only reads it.
trend
    Pc for the same object pair grew by at least `pc_increase_factor` since
    the previous run, which is the case an operator most wants to see even
    when the absolute number is still small.

Deduplication is per (primary, secondary, TCA-to-the-minute) inside
`dedupe_window_minutes`, so a run every few minutes over the same window does
not re-announce an event that has not changed.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from sda.models import ConjunctionEvent, RiskLevel

WEBHOOK_ENV = "SDA_WEBHOOK_URLS"

SLACK_HOSTS = ("hooks.slack.com",)
DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")

MAX_LOG_ENTRIES = 250

# NEGLIGIBLE is the floor, CRITICAL the ceiling.
_RISK_ORDER: dict[str, int] = {
    RiskLevel.NEGLIGIBLE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MODERATE: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}

# Discord embed colours (decimal RGB), dullest to loudest.
_RISK_COLORS: dict[str, int] = {
    RiskLevel.NEGLIGIBLE: 0x808080,
    RiskLevel.LOW: 0x3AA655,
    RiskLevel.MODERATE: 0xE0A000,
    RiskLevel.HIGH: 0xE05A00,
    RiskLevel.CRITICAL: 0xC0202C,
}


# --- configuration models ---


class WebhookTarget(BaseModel):
    """One outbound webhook."""

    url: str
    kind: str = "generic"
    enabled: bool = True


class AlertRules(BaseModel):
    """Thresholds that decide whether an event is worth announcing."""

    pc_threshold: float | None = Field(default=1e-4, ge=0.0, le=1.0)
    min_risk: RiskLevel | None = RiskLevel.HIGH
    alert_on_pc_increase: bool = True
    pc_increase_factor: float = Field(default=2.0, gt=1.0)
    min_pc_for_trend: float = Field(default=1e-7, ge=0.0, le=1.0)


class AlertConfig(BaseModel):
    """Active alerting configuration."""

    enabled: bool = True
    rules: AlertRules = Field(default_factory=AlertRules)
    webhooks: list[WebhookTarget] = Field(default_factory=list)
    dedupe_window_minutes: float = Field(default=60.0, ge=0.0)


class AlertDelivery(BaseModel):
    """Result of one POST to one webhook."""

    target: str
    kind: str
    status_code: int | None = None
    ok: bool = False
    error: str | None = None


class Alert(BaseModel):
    """A single announced conjunction."""

    id: str
    created_at: datetime
    primary: int
    secondary: int
    primary_name: str = ""
    secondary_name: str = ""
    tca: datetime
    miss_distance_km: float
    relative_velocity_km_s: float
    risk: RiskLevel
    probability: float | None = None
    previous_probability: float | None = None
    reasons: list[str] = Field(default_factory=list)
    deliveries: list[AlertDelivery] = Field(default_factory=list)


# --- webhook target parsing ---


def detect_webhook_kind(url: str) -> str:
    """Classify a webhook URL as slack, discord or generic."""
    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in SLACK_HOSTS):
        return "slack"
    if any(host == h or host.endswith("." + h) for h in DISCORD_HOSTS):
        return "discord"
    return "generic"


def parse_webhook_targets(raw: str | None = None) -> list[WebhookTarget]:
    """Parse SDA_WEBHOOK_URLS into targets, inferring the kind per entry."""
    value = os.environ.get(WEBHOOK_ENV, "") if raw is None else raw
    targets: list[WebhookTarget] = []
    for chunk in value.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        kind: str | None = None
        for prefix in ("slack=", "discord=", "generic="):
            if entry.lower().startswith(prefix):
                kind = prefix[:-1]
                entry = entry[len(prefix) :].strip()
                break
        if not entry.lower().startswith(("http://", "https://")):
            continue
        targets.append(WebhookTarget(url=entry, kind=kind or detect_webhook_kind(entry)))
    return targets


def mask_url(url: str) -> str:
    """Redact the secret path of a webhook so it can appear in responses."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown-host"
    if not parsed.path or parsed.path == "/":
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}/..."


# --- module state ---

_config: AlertConfig | None = None
_log: list[Alert] = []
_last_pc: dict[tuple[int, int], float] = {}
_recent: dict[tuple[int, int, str], datetime] = {}


def get_config() -> AlertConfig:
    """Active configuration, seeded from the environment on first use."""
    global _config
    if _config is None:
        _config = AlertConfig(webhooks=parse_webhook_targets())
    return _config


def set_config(config: AlertConfig) -> AlertConfig:
    """Replace the active configuration."""
    global _config
    _config = config
    return _config


def reset_state() -> None:
    """Clear configuration, log and dedupe state. Used by tests."""
    global _config
    _config = None
    _log.clear()
    _last_pc.clear()
    _recent.clear()


def get_alert_log(limit: int = 100) -> list[Alert]:
    """Most recent alerts, newest first."""
    if limit <= 0:
        return []
    return list(reversed(_log[-limit:]))


def clear_alert_log() -> int:
    """Empty the alert log. Returns how many entries were dropped."""
    count = len(_log)
    _log.clear()
    return count


# --- rule evaluation ---


def _risk_rank(risk: str) -> int:
    return _RISK_ORDER.get(str(risk), 0)


def _pair_key(event: ConjunctionEvent) -> tuple[int, int]:
    return (min(event.primary, event.secondary), max(event.primary, event.secondary))


def _dedupe_key(event: ConjunctionEvent) -> tuple[int, int, str]:
    pair = _pair_key(event)
    return (pair[0], pair[1], event.tca.strftime("%Y-%m-%dT%H:%M"))


def event_probability(event: ConjunctionEvent) -> float | None:
    """Pc attached to an event, or None when it was not computed."""
    pc = event.collision_probability
    return None if pc is None else float(pc.probability)


def evaluate_event(
    event: ConjunctionEvent,
    rules: AlertRules,
    previous_pc: float | None = None,
) -> list[str]:
    """Reasons this event should alert. An empty list means stay quiet."""
    reasons: list[str] = []
    pc = event_probability(event)

    if rules.pc_threshold is not None and pc is not None and pc >= rules.pc_threshold:
        reasons.append(f"Pc {pc:.3e} at or above threshold {rules.pc_threshold:.3e}")

    if rules.min_risk is not None and _risk_rank(event.risk) >= _risk_rank(rules.min_risk):
        reasons.append(f"risk {event.risk} at or above {rules.min_risk}")

    if (
        rules.alert_on_pc_increase
        and pc is not None
        and previous_pc is not None
        and previous_pc > 0.0
        and pc >= rules.min_pc_for_trend
        and pc >= previous_pc * rules.pc_increase_factor
    ):
        reasons.append(
            f"Pc rising: {previous_pc:.3e} -> {pc:.3e} "
            f"(x{pc / previous_pc:.1f} since the previous run)"
        )

    return reasons


def _is_duplicate(event: ConjunctionEvent, window_minutes: float, now: datetime) -> bool:
    """True when this (pair, TCA) already alerted inside the window."""
    if window_minutes <= 0:
        return False
    key = _dedupe_key(event)
    seen = _recent.get(key)
    return seen is not None and now - seen < timedelta(minutes=window_minutes)


def _remember(event: ConjunctionEvent, now: datetime) -> None:
    _recent[_dedupe_key(event)] = now
    # Bounded: drop entries older than a day so a long-running process does not
    # accumulate one key per screened pair forever.
    cutoff = now - timedelta(days=1)
    for key in [k for k, seen in _recent.items() if seen < cutoff]:
        del _recent[key]


# --- payload builders ---


def _summary_line(alert: Alert) -> str:
    pc_text = "n/a" if alert.probability is None else f"{alert.probability:.3e}"
    return (
        f"[{alert.risk}] {alert.primary} vs {alert.secondary} at "
        f"{alert.tca.isoformat()}: miss {alert.miss_distance_km:.3f} km, "
        f"rel vel {alert.relative_velocity_km_s:.3f} km/s, Pc {pc_text}"
    )


def _detail_lines(alert: Alert) -> list[str]:
    primary = f"{alert.primary} {alert.primary_name}".strip()
    secondary = f"{alert.secondary} {alert.secondary_name}".strip()
    lines = [
        f"Primary: {primary}",
        f"Secondary: {secondary}",
        f"TCA (UTC): {alert.tca.isoformat()}",
        f"Miss distance: {alert.miss_distance_km:.3f} km",
        f"Relative velocity: {alert.relative_velocity_km_s:.3f} km/s",
        f"Risk: {alert.risk}",
    ]
    if alert.probability is not None:
        lines.append(f"Collision probability: {alert.probability:.3e}")
    if alert.previous_probability is not None:
        lines.append(f"Previous probability: {alert.previous_probability:.3e}")
    if alert.reasons:
        lines.append("Triggered by: " + "; ".join(alert.reasons))
    return lines


def build_slack_payload(alert: Alert) -> dict[str, Any]:
    """Slack incoming-webhook payload (text plus one section block)."""
    body = "\n".join(_detail_lines(alert))
    return {
        "text": f"SDA conjunction alert: {_summary_line(alert)}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Conjunction alert: {alert.risk}"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        ],
    }


def build_discord_payload(alert: Alert) -> dict[str, Any]:
    """Discord webhook payload with a colour-coded embed."""
    return {
        "content": f"SDA conjunction alert: {_summary_line(alert)}",
        "embeds": [
            {
                "title": f"{alert.primary} vs {alert.secondary} ({alert.risk})",
                "description": "\n".join(_detail_lines(alert)),
                "color": _RISK_COLORS.get(str(alert.risk), 0x808080),
            }
        ],
    }


def build_generic_payload(alert: Alert) -> dict[str, Any]:
    """Full alert record for a generic JSON consumer."""
    payload = alert.model_dump(mode="json", exclude={"deliveries"})
    payload["summary"] = _summary_line(alert)
    payload["source"] = "space-collision-predictor"
    return payload


def build_payload(alert: Alert, kind: str) -> dict[str, Any]:
    if kind == "slack":
        return build_slack_payload(alert)
    if kind == "discord":
        return build_discord_payload(alert)
    return build_generic_payload(alert)


# --- dispatch ---


def _post(client: httpx.Client, target: WebhookTarget, alert: Alert) -> AlertDelivery:
    payload = build_payload(alert, target.kind)
    try:
        resp = client.post(target.url, json=payload)
    except Exception as exc:  # network failures must not break a screening run
        return AlertDelivery(
            target=mask_url(target.url), kind=target.kind, ok=False, error=str(exc)
        )
    status = int(getattr(resp, "status_code", 0) or 0)
    return AlertDelivery(
        target=mask_url(target.url),
        kind=target.kind,
        status_code=status,
        ok=200 <= status < 300,
        error=None if 200 <= status < 300 else f"HTTP {status}",
    )


def _deliver(alert: Alert, targets: Sequence[WebhookTarget], client: httpx.Client | None) -> None:
    active = [t for t in targets if t.enabled]
    if not active:
        return
    if client is not None:
        alert.deliveries = [_post(client, t, alert) for t in active]
        return
    with httpx.Client(timeout=10.0) as owned:
        alert.deliveries = [_post(owned, t, alert) for t in active]


def _record(alert: Alert) -> None:
    _log.append(alert)
    if len(_log) > MAX_LOG_ENTRIES:
        del _log[: len(_log) - MAX_LOG_ENTRIES]


def _alert_from_event(
    event: ConjunctionEvent,
    reasons: list[str],
    previous_pc: float | None,
    now: datetime,
) -> Alert:
    return Alert(
        id=uuid.uuid4().hex[:12],
        created_at=now,
        primary=event.primary,
        secondary=event.secondary,
        primary_name=event.primary_name,
        secondary_name=event.secondary_name,
        tca=event.tca,
        miss_distance_km=event.miss_distance_km,
        relative_velocity_km_s=event.relative_velocity_km_s,
        risk=event.risk,
        probability=event_probability(event),
        previous_probability=previous_pc,
        reasons=reasons,
    )


def dispatch_alerts(
    events: Iterable[ConjunctionEvent],
    config: AlertConfig | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Evaluate events, send webhooks for the ones that pass, and log them.

    Safe to call from a route after every screening run: deduplication and the
    Pc trend memory live here, so callers do not need any state of their own.
    Delivery failures are recorded on the alert rather than raised.
    """
    active = get_config() if config is None else config
    moment = datetime.now(UTC) if now is None else now
    sent: list[Alert] = []

    if not active.enabled:
        return sent

    for event in events:
        pair = _pair_key(event)
        previous_pc = _last_pc.get(pair)
        pc = event_probability(event)

        reasons = evaluate_event(event, active.rules, previous_pc)
        if pc is not None:
            _last_pc[pair] = pc

        if not reasons:
            continue
        if _is_duplicate(event, active.dedupe_window_minutes, moment):
            continue

        alert = _alert_from_event(event, reasons, previous_pc, moment)
        _deliver(alert, active.webhooks, client)
        _record(alert)
        _remember(event, moment)
        sent.append(alert)

    return sent


def send_test_alert(
    config: AlertConfig | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> Alert:
    """Deliver a synthetic alert so an operator can verify webhook wiring."""
    active = get_config() if config is None else config
    moment = datetime.now(UTC) if now is None else now
    alert = Alert(
        id=uuid.uuid4().hex[:12],
        created_at=moment,
        primary=25544,
        secondary=48274,
        primary_name="ISS (ZARYA)",
        secondary_name="CSS (TIANHE)",
        tca=moment,
        miss_distance_km=0.42,
        relative_velocity_km_s=11.7,
        risk=RiskLevel.CRITICAL,
        probability=1.2e-3,
        reasons=["test alert requested by operator"],
    )
    _deliver(alert, active.webhooks, client)
    _record(alert)
    return alert
