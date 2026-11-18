"""A pinning forward proxy for the browser: resolve once, check, connect to that address.

Chromium is launched with this proxy for every request (loopback included), so the
browser never resolves names itself. For each connection the proxy resolves the
host once, refuses it unless every address is public, and connects to the address it
checked. A name that answers with a public address for the check and a private one
for the connection (DNS rebinding) therefore cannot reach the private network.

Supports ``CONNECT`` (HTTPS, end to end encrypted through the tunnel) and plain
``http://`` requests (absolute-form, one request per connection). Only ports 80 and
443 are allowed.
"""

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from contextlib import suppress
from urllib.parse import urlsplit

import structlog

from kairos_tools.web import Resolver, resolve_host

log = structlog.get_logger(__name__)

ALLOWED_PORTS = frozenset({80, 443})
MAX_HEAD = 64 * 1024


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return ip.is_global and not ip.is_multicast


class Refused(Exception):
    pass


type Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def _open(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


class PinningProxy:
    """``async with PinningProxy() as proxy:`` then point Chromium at ``proxy.url``."""

    def __init__(
        self,
        *,
        resolve: Resolver = resolve_host,
        allowed: Callable[[str], bool] = is_public,
        ports: frozenset[int] = ALLOWED_PORTS,
        connect: Connector = _open,
    ) -> None:
        self._resolve = resolve
        self._allowed = allowed
        self._ports = ports
        self._connect = connect
        self._server: asyncio.Server | None = None
        self.refused: list[str] = []

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("proxy not started")
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def __aenter__(self) -> "PinningProxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def pin(self, host: str, port: int) -> str:
        """The one address to connect to, after checking every address the name has."""
        if port not in self._ports:
            raise Refused(f"port {port} is not allowed")
        try:
            addresses = [host] if _is_ip(host) else await self._resolve(host)
        except OSError as error:
            raise Refused(f"cannot resolve {host}: {error}") from error
        if not addresses:
            raise Refused(f"{host} has no addresses")
        for address in addresses:
            if not self._allowed(address):
                raise Refused(f"{host} resolves to a non-public address ({address})")
        return addresses[0]

    async def _handle(self, client: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await client.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        if len(head) > MAX_HEAD:
            writer.close()
            return
        request_line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            await _reply(writer, 400, "Bad Request")
            return
        try:
            if method.upper() == "CONNECT":
                host, port = _host_port(target, default=443)
                address = await self.pin(host, port)
                upstream_reader, upstream_writer = await self._connect(address, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await _pipe(client, writer, upstream_reader, upstream_writer)
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise Refused("only absolute http:// requests or CONNECT are proxied")
                port = parts.port or 80
                address = await self.pin(parts.hostname, port)
                upstream_reader, upstream_writer = await self._connect(address, port)
                path = parts.path or "/"
                if parts.query:
                    path += f"?{parts.query}"
                headers = [
                    line
                    for line in rest.split(b"\r\n")
                    if line and not line.lower().startswith((b"proxy-", b"connection:"))
                ]
                upstream_writer.write(
                    f"{method} {path} {version}\r\n".encode("latin-1")
                    + b"\r\n".join([*headers, b"Connection: close"])
                    + b"\r\n\r\n"
                )
                await upstream_writer.drain()
                await _pipe(client, writer, upstream_reader, upstream_writer)
        except Refused as refusal:
            self.refused.append(f"{target}: {refusal}")
            log.info("browser_proxy_refused", target=target, reason=str(refusal))
            await _reply(writer, 403, "Forbidden", str(refusal))
        except OSError as error:
            await _reply(writer, 502, "Bad Gateway", str(error))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _host_port(target: str, *, default: int) -> tuple[str, int]:
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit():
        return target, default
    return host.strip("[]"), int(port)


async def _reply(writer: asyncio.StreamWriter, status: int, reason: str, body: str = "") -> None:
    payload = body.encode()
    with suppress(OSError):
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
    writer.close()


async def _pipe(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        with suppress(OSError, asyncio.IncompleteReadError):
            while chunk := await source.read(65536):
                sink.write(chunk)
                await sink.drain()
        with suppress(OSError):
            sink.close()

    await asyncio.gather(copy(client_reader, upstream_writer), copy(upstream_reader, client_writer))
