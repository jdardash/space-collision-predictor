"""The suite must not be able to reach the network.

tests/conftest.py refuses every lookup and connection that is not this machine.
These tests fail if that guard is removed or stops covering one of the three
ways out, which is how one test file came to make 139 requests to celestrak.org
on every run without anyone noticing.
"""

import asyncio
import socket

import httpx
import pytest

from tests import conftest

# TEST-NET-1 (RFC 5737): reserved for documentation, routed nowhere. The guard
# refuses it before a packet is built; the address is a second line of defence.
UNROUTABLE = "192.0.2.1"


def test_a_public_name_cannot_be_looked_up():
    before = len(conftest.REFUSED_HOSTS)
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("celestrak.org", 443)
    assert conftest.REFUSED_HOSTS[before:] == ["celestrak.org"]


def test_a_public_address_cannot_be_connected_to():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(ConnectionRefusedError):
            sock.connect((UNROUTABLE, 443))
        assert sock.connect_ex((UNROUTABLE, 443)) != 0


def test_this_machine_can_still_be_looked_up():
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_an_http_client_is_refused_before_anything_is_sent():
    """The path the app's own fetches take: httpx on an asyncio loop."""

    async def ask() -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get("https://celestrak.org/NORAD/elements/gp.php?GROUP=stations")

    before = len(conftest.REFUSED_HOSTS)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(ask())
    assert "celestrak.org" in conftest.REFUSED_HOSTS[before:]
