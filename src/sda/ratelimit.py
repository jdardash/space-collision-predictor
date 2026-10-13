"""Network posture middleware: per-IP rate limiting and security headers.

This module holds the pieces of the service's HTTP posture that are policy
rather than orbital mechanics: a token-bucket rate limiter keyed on the client
address, a small set of always-on response headers, and the helpers that read
the posture configuration out of the environment.

The per-IP keying, the refusal to trust client-supplied forwarding headers by
default, and the bounded key map are adapted from gods-eye-view (MIT); the
bucket algorithm itself and the FastAPI integration are written here.

Configuration
-------------
SDA_RATE_LIMIT
    Rate as "<count>/<period>", for example "120/minute", "10/second",
    "5000/hour". A bare integer is read as requests per minute. The values
    "0", "off", "none" and "unlimited" disable the limiter entirely.
    Default: "600/minute", generous enough that the bundled dashboards
    never trip it.
SDA_RATE_LIMIT_PATHS
    Comma-separated path prefixes the limiter applies to.
    Default: "/api/,/agent/" (the outward-facing feed proxies and the agent
    endpoints; the orbital analysis routes are left unmetered).
SDA_RATE_LIMIT_TRUST_FORWARDED
    "1"/"true" to take the client address from the first X-Forwarded-For hop.
    Off by default: the header is client-controlled, so trusting it lets a
    caller mint fresh quota on every request and grow the key map without
    bound. Turn it on only behind a proxy that overwrites the header.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

DEFAULT_RATE_LIMIT = "600/minute"
DEFAULT_RATE_LIMIT_PATHS = "/api/,/agent/"
DEFAULT_PORT = 8000

# A key-rotating caller must not be able to grow the bucket map without bound.
MAX_TRACKED_KEYS = 4096

_PERIOD_SECONDS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
}

_RATE_RE = re.compile(r"^\s*(\d+)\s*(?:/\s*([a-z]+))?\s*$", re.IGNORECASE)

_DISABLED_VALUES = {"", "0", "off", "none", "no", "false", "unlimited"}


class RateLimitConfigError(ValueError):
    """Raised when SDA_RATE_LIMIT cannot be parsed."""


@dataclass(frozen=True)
class RateSpec:
    """A parsed rate: `count` requests per `period_seconds`."""

    count: int
    period_seconds: float

    @property
    def per_second(self) -> float:
        return self.count / self.period_seconds


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of one bucket check, carrying the values for the headers."""

    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after_seconds: int


def parse_rate(spec: str | None) -> RateSpec | None:
    """Parse "120/minute" (or a bare count, read as per minute).

    Returns None when the limiter is disabled. Raises RateLimitConfigError on
    a value that is neither disabled nor parseable, so a typo in deployment
    configuration fails loudly instead of silently removing the limit.
    """
    if spec is None:
        return None
    text = spec.strip().lower()
    if text in _DISABLED_VALUES:
        return None
    match = _RATE_RE.match(text)
    if match is None:
        raise RateLimitConfigError(f"cannot parse rate limit {spec!r}; expected e.g. '120/minute'")
    count = int(match.group(1))
    if count <= 0:
        return None
    period_word = (match.group(2) or "minute").lower()
    period = _PERIOD_SECONDS.get(period_word)
    if period is None:
        raise RateLimitConfigError(
            f"unknown rate-limit period {period_word!r}; use second, minute, hour or day"
        )
    return RateSpec(count=count, period_seconds=period)


class TokenBucketLimiter:
    """Per-key token bucket.

    Each key gets a bucket of `spec.count` tokens that refills continuously at
    `spec.count / spec.period_seconds` tokens per second. One request costs one
    token; a request arriving at an empty bucket is refused and told how long
    to wait for the next token. Burst capacity equals the configured count and
    the sustained rate equals the configured rate.

    State is in-memory and per-process, so a multi-worker deployment limits
    each worker separately. That is a backstop against runaway clients, not a
    distributed quota.
    """

    def __init__(self, spec: RateSpec, max_keys: int = MAX_TRACKED_KEYS) -> None:
        self.spec = spec
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Drop all bucket state. Used by tests and by reconfiguration."""
        with self._lock:
            self._buckets.clear()

    def check(self, key: str, now: float | None = None) -> RateLimitDecision:
        """Consume one token for `key` and report the resulting decision."""
        moment = time.monotonic() if now is None else now
        capacity = float(self.spec.count)
        rate = self.spec.per_second

        with self._lock:
            tokens, last_seen = self._buckets.get(key, (capacity, moment))
            tokens = min(capacity, tokens + max(0.0, moment - last_seen) * rate)

            if tokens >= 1.0:
                tokens -= 1.0
                allowed = True
                retry_after = 0
            else:
                allowed = False
                retry_after = max(1, math.ceil((1.0 - tokens) / rate))

            self._buckets[key] = (tokens, moment)
            self._buckets.move_to_end(key)
            self._evict_locked(moment)

        deficit = capacity - tokens
        reset_seconds = 0 if deficit <= 0 else max(1, math.ceil(deficit / rate))
        return RateLimitDecision(
            allowed=allowed,
            limit=self.spec.count,
            remaining=int(tokens),
            reset_seconds=reset_seconds,
            retry_after_seconds=retry_after,
        )

    def _evict_locked(self, now: float) -> None:
        """Drop refilled (idle) buckets first, then the least recently used."""
        if len(self._buckets) <= self.max_keys:
            return
        capacity = float(self.spec.count)
        rate = self.spec.per_second
        idle = [
            key
            for key, (tokens, last_seen) in self._buckets.items()
            if min(capacity, tokens + max(0.0, now - last_seen) * rate) >= capacity
        ]
        for key in idle:
            del self._buckets[key]
            if len(self._buckets) <= self.max_keys:
                return
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)


# --- process-wide limiter, rebuildable so tests can lower the rate ---

_limiter: TokenBucketLimiter | None = None
_configured = False


def configure_rate_limit(spec: str | None = None) -> TokenBucketLimiter | None:
    """Build (or rebuild) the process limiter. `None` re-reads the environment."""
    global _limiter, _configured
    raw = os.environ.get("SDA_RATE_LIMIT", DEFAULT_RATE_LIMIT) if spec is None else spec
    parsed = parse_rate(raw)
    _limiter = None if parsed is None else TokenBucketLimiter(parsed)
    _configured = True
    return _limiter


def current_limiter() -> TokenBucketLimiter | None:
    """Return the process limiter, building it from the environment on first use."""
    if not _configured:
        configure_rate_limit()
    return _limiter


def rate_limit_paths(raw: str | None = None) -> tuple[str, ...]:
    """Path prefixes the limiter applies to."""
    text = os.environ.get("SDA_RATE_LIMIT_PATHS", DEFAULT_RATE_LIMIT_PATHS) if raw is None else raw
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _trust_forwarded() -> bool:
    return os.environ.get("SDA_RATE_LIMIT_TRUST_FORWARDED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def client_key(request: Request) -> str:
    """Rate-limit key for a request.

    The socket peer address is used unless forwarded headers are explicitly
    trusted, because X-Forwarded-For is client-controlled and a rotating value
    would mint fresh quota on every request.
    """
    if _trust_forwarded():
        forwarded = request.headers.get("x-forwarded-for", "")
        first_hop = forwarded.split(",")[0].strip()
        if first_hop:
            return first_hop
    client = request.client
    if client is not None and client.host:
        return str(client.host)
    return "local"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply the token bucket to the configured path prefixes."""

    def __init__(self, app: Callable[..., Awaitable[None]], paths: tuple[str, ...] | None = None):
        super().__init__(app)  # type: ignore[arg-type]
        self._paths = paths

    @property
    def paths(self) -> tuple[str, ...]:
        return rate_limit_paths() if self._paths is None else self._paths

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        limiter = current_limiter()
        path = request.url.path
        if limiter is None or not path.startswith(self.paths):
            return await call_next(request)

        decision = limiter.check(client_key(request))
        headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(decision.reset_seconds),
        }
        if not decision.allowed:
            headers["Retry-After"] = str(decision.retry_after_seconds)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
                headers=headers,
            )

        response = await call_next(request)
        for name, value in headers.items():
            response.headers[name] = value
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add the conservative response headers every route can safely carry.

    No Content-Security-Policy is set here on purpose: the WorldView globe
    loads CesiumJS, and the Plotly visualisation loads plotly.js, from public
    CDNs, and both dashboards use inline styles and scripts. A CSP that
    covered them would have to enumerate those origins and still allow
    'unsafe-inline', which is close to no policy at all, so CSP is left to
    whatever reverse proxy fronts a real deployment.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response


# --- bind address and CORS allowlist ---


def bind_host(raw: str | None = None) -> str:
    """Host the server binds to. Loopback by default; containers set 0.0.0.0."""
    value = os.environ.get("SDA_HOST", "127.0.0.1") if raw is None else raw
    host = value.strip()
    return host or "127.0.0.1"


def bind_port(raw: str | None = None) -> int:
    """Port the server binds to."""
    value = os.environ.get("SDA_PORT", str(DEFAULT_PORT)) if raw is None else raw
    try:
        port = int(str(value).strip())
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def default_cors_origins(port: int | None = None) -> list[str]:
    """Local development origins for the configured port."""
    effective = bind_port() if port is None else port
    return [
        f"http://localhost:{effective}",
        f"http://127.0.0.1:{effective}",
    ]


def cors_origins(raw: str | None = None, port: int | None = None) -> list[str]:
    """Parse SDA_CORS_ORIGINS into an allowlist.

    Comma separated, trailing slashes trimmed. An unset or empty value falls
    back to the loopback origins for the configured port. A literal "*" is
    honoured but is a deliberate operator choice rather than the default, and
    credentials stay off either way.
    """
    value = os.environ.get("SDA_CORS_ORIGINS", "") if raw is None else raw
    origins = [part.strip().rstrip("/") for part in value.split(",") if part.strip()]
    if not origins:
        return default_cors_origins(port)
    return origins
