"""Session-wide test setup: the repo root on sys.path, and no network.

`scripts/` is not part of the installed package (only `src/sda` is), so a test
that exercises a build script cannot import it without the repo root on the
path. pytest imports conftest before collecting any test module, which is what
lets those tests keep their imports at the top of the file.

The network guard is installed at import for the same reason: it has to be in
place before the first test module pulls in the app. Several tests run the real
app lifespan, whose background task asks CelesTrak for every group and NOAA for
the solar flux. Until this guard existed one run of
tests/test_build_static_demo.py alone made 139 requests to celestrak.org, and
CelesTrak blocks an address that repeats a download inside its two-hour window.
Every lookup of a name that is not this machine is now refused before a DNS
query or a connection is made, so the lifespan's fetch fails the way it does on
a machine with no network and the bundled catalog is used. A test that needs an
upstream reply supplies a fake client, as tests/test_api_lifecycle.py does.

Names are refused at lookup and literal addresses at connect. A literal
address reached through the asyncio proactor loop on Windows does not pass
through socket.connect; nothing in this project connects by literal address.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


#: Hosts a test reached for and was refused, in order. Read it when a test
#: behaves as if it were offline and you want to know what it asked for.
REFUSED_HOSTS: list[str] = []

# "testserver" is the host name Starlette's TestClient puts in its URLs.
_LOCAL_NAMES = frozenset({"localhost", "testserver", "::1", "0.0.0.0", ""})
_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _host_name(host: object) -> str:
    """The host as text. asyncio hands getaddrinfo the name as bytes."""
    if host is None:
        return ""
    return host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)


def _is_local(name: str) -> bool:
    return name in _LOCAL_NAMES or name.startswith("127.")


def _guarded_getaddrinfo(host, *args, **kwargs):
    name = _host_name(host)
    if _is_local(name):
        return _real_getaddrinfo(host, *args, **kwargs)
    REFUSED_HOSTS.append(name)
    raise socket.gaierror(11001, f"tests are offline: lookup of {name!r} refused")


def _guarded_connect(self, address):
    if isinstance(address, tuple):
        name = _host_name(address[0])
        if not _is_local(name):
            REFUSED_HOSTS.append(name)
            raise ConnectionRefusedError(f"tests are offline: connect to {name!r} refused")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if isinstance(address, tuple):
        name = _host_name(address[0])
        if not _is_local(name):
            REFUSED_HOSTS.append(name)
            return 10061  # WSAECONNREFUSED
    return _real_connect_ex(self, address)


socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _celestrak_memory_starts_empty() -> None:
    """The app remembers which CelesTrak groups it asked for and keeps their text.
    Each test starts the way a new process does, or one test's fake download
    would rest the next test's groups."""
    api = sys.modules.get("sda.api")
    if api is not None:
        api._forget_celestrak()


@pytest.fixture(autouse=True)
def _no_real_tle_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Several tests run the real app lifespan, which loads SDA_TLE_CACHE_DIR
    when it is set. A developer's cache must never leak into a test catalog."""
    monkeypatch.delenv("SDA_TLE_CACHE_DIR", raising=False)
