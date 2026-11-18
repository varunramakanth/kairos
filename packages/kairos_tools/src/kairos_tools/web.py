"""Web tools: ``web_search`` (Tavily) and ``web_fetch`` (plain HTTP).

Both return content from the open web, so both are ``UNTRUSTED``: the harness wraps
their output as data before the model sees it.

``web_fetch`` refuses addresses on private, loopback, link-local and other
non-public networks, and checks every redirect hop, so a page cannot steer the
agent at the local machine, the home network or a cloud metadata endpoint. The
check resolves the host before connecting; a DNS answer that changes between the
check and the connection (DNS rebinding) is not covered and is left to the
browser service's network sandbox (M6).
"""

import asyncio
import ipaddress
import socket
import warnings
from collections.abc import Awaitable, Callable
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx2
from pydantic import BaseModel, Field, SecretStr

from kairos_domain.content import Provenance
from kairos_domain.tools import ToolContext, ToolOutput
from kairos_tools.registry import Tool, tool

with warnings.catch_warnings():
    # trafilatura pulls in urllib3's optional SOCKS support, which warns when PySocks
    # is missing. We never use a SOCKS proxy.
    warnings.filterwarnings("ignore", message="SOCKS support in urllib3")
    import trafilatura

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
USER_AGENT = "KairosAgent/0.1"
MAX_REDIRECTS = 5
MAX_BYTES = 2_000_000
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/json")

type Resolver = Callable[[str], Awaitable[list[str]]]
"""Returns the IP addresses a host name resolves to."""


async def resolve_host(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class BlockedAddressError(Exception):
    """The URL points at a network the agent may not reach."""


async def check_public_url(url: str, resolve: Resolver) -> None:
    """Raise ``BlockedAddressError`` unless ``url`` is http(s) on public addresses only."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddressError(f"only http and https URLs are allowed, got {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise BlockedAddressError("URL has no host")
    try:
        addresses = [host] if _is_ip(host) else await resolve(host)
    except OSError as error:
        raise BlockedAddressError(f"cannot resolve {host}: {error}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise BlockedAddressError(f"{host} resolves to a non-public address ({ip})")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# --- web_search -----------------------------------------------------------------------


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=400, description="What to search for.")
    max_results: int = Field(default=5, ge=1, le=10, description="How many results to return.")
    topic: Literal["general", "news"] = Field(
        default="general", description="Use news for recent events and announcements."
    )
    time_range: Literal["day", "week", "month", "year"] | None = Field(
        default=None, description="Only return pages published within this period."
    )


def web_search_tool(api_key: SecretStr, client: httpx2.AsyncClient) -> Tool[WebSearchInput]:
    """Build ``web_search`` on the Tavily Search API (basic depth: one credit per call)."""

    @tool(provenance=Provenance.UNTRUSTED)
    async def web_search(args: WebSearchInput, context: ToolContext) -> ToolOutput:
        """Search the web. Returns titles, URLs and short snippets. Snippets are often
        incomplete: use web_fetch on the most relevant URLs before relying on them."""
        body: dict[str, object] = {
            "query": args.query,
            "max_results": args.max_results,
            "topic": args.topic,
            "search_depth": "basic",
            "include_published_date": True,
        }
        if args.time_range:
            body["time_range"] = args.time_range
        try:
            response = await client.post(
                TAVILY_SEARCH_URL,
                json=body,
                headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
                timeout=20.0,
            )
        except httpx2.HTTPError as error:
            return ToolOutput(content=f"search failed: {type(error).__name__}", is_error=True)
        if response.status_code != 200:
            return ToolOutput(content=f"search failed: HTTP {response.status_code}", is_error=True)
        results = response.json().get("results", [])
        if not results:
            return ToolOutput(content=f"No results for {args.query!r}.")
        lines: list[str] = []
        for number, result in enumerate(results, start=1):
            published = result.get("published_date")
            dated = f" ({published})" if published else ""
            lines.append(
                f"{number}. {result.get('title', '(untitled)')}{dated}\n"
                f"   {result.get('url', '')}\n"
                f"   {' '.join(str(result.get('content', '')).split())}"
            )
        return ToolOutput(content="\n\n".join(lines))

    return web_search


# --- web_fetch ------------------------------------------------------------------------


class WebFetchInput(BaseModel):
    url: str = Field(description="The full http(s) URL of the page to read.")


def web_fetch_tool(
    client: httpx2.AsyncClient, *, resolve: Resolver = resolve_host
) -> Tool[WebFetchInput]:
    """Build ``web_fetch``. ``client`` must not follow redirects on its own."""

    @tool(provenance=Provenance.UNTRUSTED)
    async def web_fetch(args: WebFetchInput, context: ToolContext) -> ToolOutput:
        """Read a web page and return its main text as Markdown, with links. Use it
        to read pages found with web_search or given by the user."""
        url = args.url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                await check_public_url(url, resolve)
                async with client.stream(
                    "GET", url, headers={"User-Agent": USER_AGENT}, timeout=20.0
                ) as response:
                    if response.is_redirect and "location" in response.headers:
                        url = urljoin(url, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        return ToolOutput(
                            content=f"fetch failed: HTTP {response.status_code} for {url}",
                            is_error=True,
                        )
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    if content_type and content_type not in TEXT_TYPES:
                        return ToolOutput(
                            content=f"cannot read {content_type or 'this content'} at {url}",
                            is_error=True,
                        )
                    raw = await _read_capped(response)
                    return ToolOutput(content=_to_markdown(raw, url=url, content_type=content_type))
            return ToolOutput(content=f"too many redirects starting at {args.url}", is_error=True)
        except BlockedAddressError as error:
            return ToolOutput(content=f"refused: {error}", is_error=True)
        except httpx2.HTTPError as error:
            return ToolOutput(
                content=f"fetch failed: {type(error).__name__} for {url}", is_error=True
            )

    return web_fetch


async def _read_capped(response: httpx2.Response) -> str:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BYTES:
            break
    return b"".join(chunks)[:MAX_BYTES].decode(response.encoding or "utf-8", errors="replace")


def html_to_markdown(raw: str, *, url: str) -> str:
    """Main text of an HTML page as Markdown with links (also used by the browser)."""
    return _to_markdown(raw, url=url, content_type="text/html")


def _to_markdown(raw: str, *, url: str, content_type: str) -> str:
    if content_type in ("text/plain", "application/json"):
        return f"URL: {url}\n\n{raw}"
    text = trafilatura.extract(
        raw, url=url, output_format="markdown", include_links=True, include_tables=True
    )
    metadata = trafilatura.extract_metadata(raw, default_url=url)
    title = metadata.title if metadata and metadata.title else None
    heading = f"# {title}\n" if title else ""
    body = text or "(no readable text found on this page)"
    return f"{heading}URL: {url}\n\n{body}"
