"""Root pytest configuration.

Network policy: tests may only talk to loopback (for testcontainers Postgres and
local fakes). Anything else raises ``pytest_socket.SocketConnectBlockedError``.
Tests marked ``live`` are skipped unless ``LIVE=1`` is set, in which case they
get real sockets.
"""

import os

import pytest

LIVE = os.environ.get("LIVE") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_live = pytest.mark.skip(reason="live test; set LIVE=1 to run")
    for item in items:
        if "live" not in item.keywords:
            continue
        if LIVE:
            item.add_marker(pytest.mark.enable_socket)
        else:
            item.add_marker(skip_live)
