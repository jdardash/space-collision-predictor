"""Network posture tests: bind address, CORS allowlist, rate limit, security headers.

Everything here is offline. The rate-limit tests lower the configured rate rather
than sending hundreds of requests, so the whole file runs in well under a second.
"""

import pytest
from fastapi.testclient import TestClient

from sda import ratelimit
from sda.api import app

client = TestClient(app)


# --- bind address ---


def test_bind_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("SDA_HOST", raising=False)
    assert ratelimit.bind_host() == "127.0.0.1"


def test_bind_host_reads_environment(monkeypatch):
    monkeypatch.setenv("SDA_HOST", "0.0.0.0")
    assert ratelimit.bind_host() == "0.0.0.0"


def test_bind_host_ignores_blank(monkeypatch):
    monkeypatch.setenv("SDA_HOST", "   ")
    assert ratelimit.bind_host() == "127.0.0.1"


def test_bind_port_default_and_override(monkeypatch):
    monkeypatch.delenv("SDA_PORT", raising=False)
    assert ratelimit.bind_port() == 8000
    monkeypatch.setenv("SDA_PORT", "9100")
    assert ratelimit.bind_port() == 9100


@pytest.mark.parametrize("value", ["not-a-port", "0", "70000", ""])
def test_bind_port_falls_back_on_bad_values(monkeypatch, value):
    monkeypatch.setenv("SDA_PORT", value)
    assert ratelimit.bind_port() == 8000


def test_container_images_bind_all_interfaces():
    """The Dockerfile and compose file must keep containers reachable."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "SDA_HOST=0.0.0.0" in (root / "Dockerfile").read_text(encoding="utf-8")
    assert "SDA_HOST=0.0.0.0" in (root / "docker-compose.yml").read_text(encoding="utf-8")


# --- CORS allowlist ---


def test_cors_origins_default_is_loopback(monkeypatch):
    monkeypatch.delenv("SDA_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("SDA_PORT", raising=False)
    origins = ratelimit.cors_origins()
    assert origins == ["http://localhost:8000", "http://127.0.0.1:8000"]
    assert "*" not in origins


def test_cors_origins_parses_list():
    origins = ratelimit.cors_origins("https://a.example.com/, https://b.example.com")
    assert origins == ["https://a.example.com", "https://b.example.com"]


def test_cors_allows_a_listed_origin():
    resp = client.get("/health", headers={"Origin": "http://localhost:8000"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:8000"
    # Credentials are off, so the browser cannot send cookies cross origin.
    assert "access-control-allow-credentials" not in resp.headers


def test_cors_rejects_an_unlisted_origin():
    resp = client.get("/health", headers={"Origin": "https://evil.example.com"})
    # The request itself still succeeds; the browser is the one that blocks it,
    # and it blocks it because the allow-origin header is absent.
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_rejects_an_unlisted_origin():
    resp = client.options(
        "/conjunctions",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in resp.headers


# --- security headers ---


def test_security_headers_present_on_every_response():
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_no_content_security_policy_is_set():
    # Deliberate: the globe loads CesiumJS and the plots load plotly.js from
    # public CDNs with inline scripts, so a CSP here would break the UI.
    resp = client.get("/health")
    assert "content-security-policy" not in {k.lower() for k in resp.headers}


def test_security_headers_on_error_responses():
    resp = client.get("/satellites/99999999")
    assert resp.status_code == 404
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


# --- rate limiting ---


@pytest.fixture
def tiny_rate_limit():
    """Lower the process limit to two requests, then restore the default."""
    limiter = ratelimit.configure_rate_limit("2/minute")
    assert limiter is not None
    limiter.reset()
    yield limiter
    ratelimit.configure_rate_limit(None)


def test_parse_rate_accepts_common_spellings():
    assert ratelimit.parse_rate("120/minute") == ratelimit.RateSpec(120, 60.0)
    assert ratelimit.parse_rate("5/second") == ratelimit.RateSpec(5, 1.0)
    assert ratelimit.parse_rate("90") == ratelimit.RateSpec(90, 60.0)


def test_parse_rate_disabled_values():
    assert ratelimit.parse_rate("off") is None
    assert ratelimit.parse_rate("0") is None
    assert ratelimit.parse_rate(None) is None


def test_parse_rate_rejects_garbage():
    with pytest.raises(ratelimit.RateLimitConfigError):
        ratelimit.parse_rate("fast")
    with pytest.raises(ratelimit.RateLimitConfigError):
        ratelimit.parse_rate("10/fortnight")


def test_rate_limit_returns_429_with_retry_after(tiny_rate_limit):
    path = "/api/rate-limit-probe"
    first = client.get(path)
    second = client.get(path)
    third = client.get(path)

    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429

    assert third.headers["X-RateLimit-Limit"] == "2"
    assert third.headers["X-RateLimit-Remaining"] == "0"
    assert int(third.headers["Retry-After"]) >= 1
    assert int(third.headers["X-RateLimit-Reset"]) >= 1
    assert third.json()["detail"] == "Rate limit exceeded"


def test_rate_limit_headers_on_allowed_requests(tiny_rate_limit):
    resp = client.get("/api/rate-limit-probe")
    assert resp.headers["X-RateLimit-Limit"] == "2"
    assert resp.headers["X-RateLimit-Remaining"] == "1"


def test_rate_limit_only_guards_configured_prefixes(tiny_rate_limit):
    for _ in range(6):
        resp = client.get("/health")
        assert resp.status_code == 200
    assert "X-RateLimit-Limit" not in resp.headers


def test_rate_limit_paths_default():
    assert ratelimit.rate_limit_paths("/api/,/agent/") == ("/api/", "/agent/")


def test_bucket_refills_over_time():
    spec = ratelimit.parse_rate("60/minute")
    assert spec is not None
    limiter = ratelimit.TokenBucketLimiter(spec)
    for _ in range(60):
        assert limiter.check("1.2.3.4", now=0.0).allowed
    assert not limiter.check("1.2.3.4", now=0.0).allowed
    # One token per second at 60/minute.
    assert limiter.check("1.2.3.4", now=1.5).allowed


def test_buckets_are_per_key():
    spec = ratelimit.parse_rate("1/minute")
    assert spec is not None
    limiter = ratelimit.TokenBucketLimiter(spec)
    assert limiter.check("1.1.1.1", now=0.0).allowed
    assert not limiter.check("1.1.1.1", now=0.0).allowed
    assert limiter.check("2.2.2.2", now=0.0).allowed


def test_bucket_map_is_bounded():
    spec = ratelimit.parse_rate("5/minute")
    assert spec is not None
    limiter = ratelimit.TokenBucketLimiter(spec, max_keys=16)
    for index in range(200):
        limiter.check(f"10.0.0.{index}", now=float(index))
    assert len(limiter._buckets) <= 16


def test_forwarded_header_is_not_trusted_by_default(monkeypatch):
    from starlette.requests import Request

    monkeypatch.delenv("SDA_RATE_LIMIT_TRUST_FORWARDED", raising=False)
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"9.9.9.9")],
        "client": ("5.5.5.5", 1234),
    }
    assert ratelimit.client_key(Request(scope)) == "5.5.5.5"

    monkeypatch.setenv("SDA_RATE_LIMIT_TRUST_FORWARDED", "1")
    assert ratelimit.client_key(Request(scope)) == "9.9.9.9"
