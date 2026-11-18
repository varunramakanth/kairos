"""The pinning proxy: one resolution per connection, private addresses refused, and a
DNS-rebinding attack against the real browser stopped."""

import asyncio
import os
from pathlib import Path

import pytest

from kairos_browser.proxy import PinningProxy
from kairos_browser.session import BrowserSession


class Rebinding:
    """Answers public addresses for the first ``public_calls`` lookups, then private:
    the classic rebinding trick against check-then-connect."""

    def __init__(self, public_calls: int) -> None:
        self.calls = 0
        self._public = public_calls

    async def __call__(self, host: str) -> list[str]:
        self.calls += 1
        return ["93.184.215.14"] if self.calls <= self._public else ["10.0.0.7"]


async def request(proxy: PinningProxy, head: bytes) -> bytes:
    host, port = proxy.url.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(head)
    await writer.drain()
    reply = await reader.read(4096)
    writer.close()
    return reply


async def test_private_addresses_and_odd_ports_are_refused() -> None:
    async def private(host: str) -> list[str]:
        return ["192.168.1.10"]

    async with PinningProxy(resolve=private) as proxy:
        reply = await request(proxy, b"CONNECT router.example:443 HTTP/1.1\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        assert b"non-public address (192.168.1.10)" in reply
        reply = await request(proxy, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 403")
        reply = await request(proxy, b"CONNECT shop.example:22 HTTP/1.1\r\n\r\n")
        assert b"port 22 is not allowed" in reply


async def test_it_connects_to_exactly_the_address_it_checked() -> None:
    resolver = Rebinding(public_calls=1)
    connected: list[tuple[str, int]] = []

    async def echo_upstream(
        host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        connected.append((host, port))
        server = await asyncio.start_server(
            lambda r, w: w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"), "127.0.0.1", 0
        )
        address = server.sockets[0].getsockname()
        return await asyncio.open_connection(address[0], address[1])

    async with PinningProxy(resolve=resolver, connect=echo_upstream) as proxy:
        reply = await request(
            proxy, b"GET http://shop.example/item HTTP/1.1\r\nHost: shop.example\r\n\r\n"
        )
    assert reply.startswith(b"HTTP/1.1 200 OK")
    assert resolver.calls == 1  # resolved once ...
    assert connected == [("93.184.215.14", 80)]  # ... and connected to that address


def chromium_installed() -> bool:
    roots = (Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright")
    return any(root.exists() and any(root.glob("chromium*")) for root in roots)


@pytest.mark.skipif(
    not chromium_installed() and os.environ.get("KAIROS_REQUIRE_BROWSER") != "1",
    reason="Chromium not installed (uv run playwright install chromium)",
)
async def test_the_browser_cannot_be_rebound_onto_the_private_network() -> None:
    # Two public answers pass the early checks (navigate and the route guard); the
    # proxy's own lookup gets the private answer and refuses to connect.
    resolver = Rebinding(public_calls=2)
    session = BrowserSession(resolve=resolver)
    await session.start()
    try:
        snapshot = await session.navigate("http://rebind.example/")
        assert session.proxy is not None
        assert any("non-public address (10.0.0.7)" in r for r in session.proxy.refused)
        # What the page shows is the proxy's refusal, not anything from the private host.
        assert "non-public address (10.0.0.7)" in snapshot
    finally:
        await session.close()
