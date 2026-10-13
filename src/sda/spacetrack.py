"""Space-Track.org client: authenticated GP (TLE), TIP and public CDM queries.

Space-Track is the authoritative public catalog operated by the 18th/19th Space
Defense Squadron. Access needs a free account; credentials come from the
environment (SDA_SPACETRACK_USER / SDA_SPACETRACK_PASS) and are never written to
disk or into responses.

Frames and units are unchanged by this module: it returns raw TLE text, which is
handed to TLEStore.load_from_text() for validation through Satrec.twoline2rv()
(WGS72), so every state vector derived from it stays ECI/TEME in km and km/s.

Three query classes are wrapped:

gp
    Latest general-perturbations element sets, requested in TLE format so the
    existing store parser can consume them unchanged.
tip
    Tracking and Impact Prediction messages: predicted reentry time and
    footprint for decaying objects.
cdm_public
    The subset of conjunction data messages Space-Track publishes without a
    per-operator agreement (currently high-interest events only).

Client-side throttling honours the published guidance of at most 30 requests
per minute and 300 per hour; the limiter here is deliberately a little stricter
than the server so a burst never earns an account-level block.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from urllib.parse import quote

import httpx

SPACETRACK_BASE = "https://www.space-track.org"
LOGIN_PATH = "/ajaxauth/login"
QUERY_PATH = "/basicspacedata/query"

# Published Space-Track guidance, applied client-side.
MAX_REQUESTS_PER_MINUTE = 28
MAX_REQUESTS_PER_HOUR = 280

DEFAULT_GP_LIMIT = 500
DEFAULT_TIP_LIMIT = 25
DEFAULT_CDM_LIMIT = 25

# gp OBJECT_TYPE values accepted by the object_class argument.
OBJECT_CLASSES = ("PAYLOAD", "ROCKET BODY", "DEBRIS", "UNKNOWN")

USER_ENV = "SDA_SPACETRACK_USER"
PASS_ENV = "SDA_SPACETRACK_PASS"


class SpaceTrackError(RuntimeError):
    """Any Space-Track failure that a route should turn into an HTTP error."""


class SpaceTrackCredentialsError(SpaceTrackError):
    """No usable credentials in the environment."""


class SpaceTrackAuthError(SpaceTrackError):
    """Space-Track rejected the credentials."""


def credentials(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """Return (user, password) from the environment, or None when unset."""
    source = os.environ if env is None else env
    user = (source.get(USER_ENV) or "").strip()
    password = (source.get(PASS_ENV) or "").strip()
    if not user or not password:
        return None
    return user, password


def credentials_available(env: dict[str, str] | None = None) -> bool:
    """True when both credential variables are set to something non-empty."""
    return credentials(env) is not None


MISSING_CREDENTIALS_MESSAGE = (
    "Space-Track credentials are not configured. Create a free account at "
    "https://www.space-track.org and set "
    f"{USER_ENV} and {PASS_ENV} in the server environment."
)


class Throttle:
    """Sliding-window request throttle over a minute and an hour.

    `sleep` and `clock` are injectable so tests can drive it without waiting.
    """

    def __init__(
        self,
        per_minute: int = MAX_REQUESTS_PER_MINUTE,
        per_hour: int = MAX_REQUESTS_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.per_minute = per_minute
        self.per_hour = per_hour
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= 3600.0:
            self._stamps.popleft()

    def wait_seconds(self, now: float) -> float:
        """Seconds to wait before the next request may go out."""
        self._prune(now)
        waits = [0.0]
        recent_minute = [t for t in self._stamps if now - t < 60.0]
        if len(recent_minute) >= self.per_minute:
            waits.append(60.0 - (now - recent_minute[0]))
        if len(self._stamps) >= self.per_hour:
            waits.append(3600.0 - (now - self._stamps[0]))
        return max(waits)

    def acquire(self) -> float:
        """Block until a request slot is free. Returns the seconds slept."""
        now = self._clock()
        wait = self.wait_seconds(now)
        if wait > 0:
            self._sleep(wait)
            now = self._clock()
            self._prune(now)
        self._stamps.append(now)
        return wait


def _norad_filter(norad_ids: Sequence[int]) -> str:
    """Space-Track list syntax for a NORAD id filter."""
    return ",".join(str(int(n)) for n in norad_ids)


class SpaceTrackClient:
    """Small authenticated client over httpx with cookie session reuse.

    One login per client instance; the httpx cookie jar carries the session
    cookie across queries. Pass `client` to inject a stub in tests.
    """

    def __init__(
        self,
        user: str | None = None,
        password: str | None = None,
        base_url: str = SPACETRACK_BASE,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        if user is None or password is None:
            found = credentials()
            if found is None:
                raise SpaceTrackCredentialsError(MISSING_CREDENTIALS_MESSAGE)
            user, password = found
        self._user = user
        self._password = password
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self.throttle = throttle if throttle is not None else Throttle()
        self._logged_in = False

    # --- session handling ---

    def login(self) -> None:
        """Authenticate and keep the session cookie on the httpx client."""
        self.throttle.acquire()
        try:
            resp = self._client.post(
                f"{self.base_url}{LOGIN_PATH}",
                data={"identity": self._user, "password": self._password},
            )
        except httpx.HTTPError as exc:
            raise SpaceTrackError(f"Space-Track login failed: {exc}") from exc
        if resp.status_code != 200:
            raise SpaceTrackAuthError(
                f"Space-Track login rejected with HTTP {resp.status_code}"
            )
        body = (getattr(resp, "text", "") or "").strip()
        if "Failed" in body or "failed" in body:
            raise SpaceTrackAuthError("Space-Track login rejected the supplied credentials")
        self._logged_in = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SpaceTrackClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- queries ---

    def _query(self, predicate: str) -> httpx.Response:
        """Run one basicspacedata query, logging in first when needed."""
        if not self._logged_in:
            self.login()
        url = f"{self.base_url}{QUERY_PATH}/{predicate}"
        self.throttle.acquire()
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise SpaceTrackError(f"Space-Track query failed: {exc}") from exc

        if resp.status_code == 401:
            # Session expired: one re-login, then one retry.
            self._logged_in = False
            self.login()
            self.throttle.acquire()
            try:
                resp = self._client.get(url)
            except httpx.HTTPError as exc:
                raise SpaceTrackError(f"Space-Track query failed: {exc}") from exc

        if resp.status_code == 429:
            raise SpaceTrackError("Space-Track rate limit hit; back off and retry later")
        if resp.status_code != 200:
            raise SpaceTrackError(f"Space-Track query returned HTTP {resp.status_code}")
        return resp

    def gp_latest(
        self,
        norad_ids: Iterable[int] | None = None,
        object_class: str | None = None,
        limit: int = DEFAULT_GP_LIMIT,
        max_epoch_age_days: int = 30,
    ) -> str:
        """Latest GP element sets as TLE text.

        Either filter by explicit NORAD ids, or by gp OBJECT_TYPE (PAYLOAD,
        ROCKET BODY, DEBRIS, UNKNOWN). With neither, the most recently updated
        on-orbit objects are returned. The caller must pass the result to
        TLEStore.load_from_text(); this module never builds TLERecords itself.
        """
        parts = ["class/gp"]
        ids = list(norad_ids) if norad_ids is not None else []
        if ids:
            parts.append(f"NORAD_CAT_ID/{_norad_filter(ids)}")
        if object_class:
            normalized = object_class.strip().upper()
            if normalized not in OBJECT_CLASSES:
                raise ValueError(
                    f"unknown object_class {object_class!r}; expected one of "
                    + ", ".join(OBJECT_CLASSES)
                )
            parts.append(f"OBJECT_TYPE/{quote(normalized)}")
        if not ids:
            # Only on-orbit objects with a recent element set.
            parts.append("DECAY_DATE/null-val")
            parts.append(f"EPOCH/%3Enow-{int(max_epoch_age_days)}")
        parts.append("orderby/EPOCH%20desc")
        parts.append(f"limit/{int(limit)}")
        parts.append("format/tle")
        resp = self._query("/".join(parts))
        return str(getattr(resp, "text", "") or "")

    def tip_messages(self, limit: int = DEFAULT_TIP_LIMIT) -> list[dict[str, Any]]:
        """Tracking and Impact Prediction messages (predicted reentries)."""
        predicate = "/".join(
            ["class/tip", "orderby/MSG_EPOCH%20desc", f"limit/{int(limit)}", "format/json"]
        )
        resp = self._query(predicate)
        return _as_rows(resp)

    def cdm_public(self, limit: int = DEFAULT_CDM_LIMIT) -> list[dict[str, Any]]:
        """Publicly released conjunction data messages, soonest TCA first.

        Space-Track only publishes a subset here; operator-specific CDMs need a
        separate agreement, so an empty list is a normal result.
        """
        predicate = "/".join(
            ["class/cdm_public", "orderby/TCA%20asc", f"limit/{int(limit)}", "format/json"]
        )
        resp = self._query(predicate)
        return _as_rows(resp)


def _as_rows(resp: object) -> list[dict[str, Any]]:
    """Coerce a JSON response body into a list of dict rows."""
    payload = resp.json() if hasattr(resp, "json") else None
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def normalize_tip(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten one TIP message into the fields the /reentries route returns."""

    def _num(key: str) -> float | None:
        value = row.get(key)
        if value is None or value == "":
            return None
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    norad_raw = row.get("NORAD_CAT_ID")
    norad_id: int | None
    try:
        norad_id = int(str(norad_raw)) if norad_raw is not None and norad_raw != "" else None
    except (TypeError, ValueError):
        norad_id = None

    return {
        "norad_id": norad_id,
        "object_name": row.get("OBJECT_NAME") or row.get("SAT_NAME") or "",
        "decay_epoch": row.get("DECAY_EPOCH"),
        "message_epoch": row.get("MSG_EPOCH"),
        "window_minutes": _num("WINDOW"),
        "latitude_deg": _num("LAT"),
        "longitude_deg": _num("LON"),
        "incl_deg": _num("INCL"),
        "next_report_hours": _num("NEXT_REPORT"),
        "high_interest": str(row.get("HIGH_INTEREST", "")).upper() in {"Y", "TRUE", "1"},
        "direction": row.get("DIRECTION") or "",
    }


def fetch_gp_text(
    norad_ids: Iterable[int] | None = None,
    object_class: str | None = None,
    limit: int = DEFAULT_GP_LIMIT,
    client: SpaceTrackClient | None = None,
) -> str:
    """Convenience wrapper returning raw TLE text, managing the client."""
    if client is not None:
        return client.gp_latest(norad_ids=norad_ids, object_class=object_class, limit=limit)
    with SpaceTrackClient() as owned:
        return owned.gp_latest(norad_ids=norad_ids, object_class=object_class, limit=limit)


def fetch_reentries(
    limit: int = DEFAULT_TIP_LIMIT,
    client: SpaceTrackClient | None = None,
) -> list[dict[str, Any]]:
    """Normalized TIP reentry predictions, managing the client."""
    if client is not None:
        rows = client.tip_messages(limit=limit)
    else:
        with SpaceTrackClient() as owned:
            rows = owned.tip_messages(limit=limit)
    return [normalize_tip(row) for row in rows]
