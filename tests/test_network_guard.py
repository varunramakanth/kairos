"""The test suite must not be able to reach the network by default."""

import socket

import pytest
from pytest_socket import SocketConnectBlockedError


def test_outbound_connections_are_blocked() -> None:
    # 192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routable, so nothing is contacted
    # even if the guard is broken.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        with pytest.warns(UserWarning, match="192.0.2.1"), pytest.raises(SocketConnectBlockedError):
            sock.connect(("192.0.2.1", 443))


def test_loopback_is_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(server.getsockname())


@pytest.mark.live
def test_live_tests_are_skipped_without_live_flag() -> None:
    pytest.fail("live tests must be skipped unless LIVE=1")  # pragma: no cover
