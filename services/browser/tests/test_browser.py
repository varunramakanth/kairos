"""The browser service: element classification, the address guard, and real Chromium
on fixture pages (no network). Skipped when Playwright's Chromium is not installed
(`uv run playwright install chromium`)."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import Client
from mcp_types import TextContent

from kairos_browser.server import build_server
from kairos_browser.session import BrowserError, BrowserSession, ElementInfo, classify

ADDRESSES = {"shop.example.com": ["93.184.215.14"], "intranet.example.com": ["10.0.0.5"]}


async def fake_resolve(host: str) -> list[str]:
    return ADDRESSES.get(host, ["93.184.215.14"])


def element(**overrides: object) -> ElementInfo:
    base: dict[str, object] = {
        "tag": "button",
        "role": "",
        "type": "",
        "href": "",
        "in_form": False,
        "in_search": False,
        "expanded": False,
    }
    return ElementInfo(**(base | overrides))  # type: ignore[arg-type]  # test helper over a dataclass


@pytest.mark.parametrize(
    ("info", "kind"),
    [
        (element(tag="a", href="https://shop.example.com/p/1"), "read"),
        (element(tag="a", href="javascript:buy()"), "act"),
        (element(role="tab"), "read"),
        (element(tag="summary"), "read"),
        (element(expanded=True), "read"),
        (element(expanded=True, in_form=True), "act"),
        (element(tag="input", type="search"), "search"),
        (element(tag="input", in_search=True), "search"),
        (element(tag="input", type="email", in_form=True), "act"),
        (element(type="submit", in_form=True), "act"),
        (element(), "act"),
    ],
)
def test_elements_are_classified_as_reading_searching_or_acting(
    info: ElementInfo, kind: str
) -> None:
    assert classify(info) == kind


SHOP = "https://shop.example.com/monitors"
PAGE = """<html><head><title>Monitors</title></head><body>
<nav><a href="https://shop.example.com/deals">Deals</a></nav>
<form role="search" action="https://shop.example.com/search">
  <input type="search" name="q" aria-label="Search"></form>
<main><h1>27-inch 4K monitors</h1><p>Acme 27UK: $379.99</p>
<form action="https://shop.example.com/cart"><button type="submit">Add to cart</button></form>
<img src="http://intranet.example.com/beacon.png" alt="">
</main></body></html>"""
DEALS = (
    "<html><head><title>Deals</title></head>"
    "<body><h1>Deals</h1><p>All monitors 10% off.</p></body></html>"
)


def chromium_installed() -> bool:
    cache = Path.home() / "Library/Caches/ms-playwright"
    linux = Path.home() / ".cache/ms-playwright"
    return any(p.exists() and any(p.glob("chromium*")) for p in (cache, linux))


needs_chromium = pytest.mark.skipif(
    not chromium_installed() and os.environ.get("KAIROS_REQUIRE_BROWSER") != "1",
    reason="Chromium not installed (uv run playwright install chromium)",
)


@pytest.fixture
async def session() -> AsyncIterator[BrowserSession]:
    browser = BrowserSession(
        resolve=fake_resolve,
        fixtures={SHOP: PAGE, "https://shop.example.com/deals": DEALS},
    )
    await browser.start()
    try:
        yield browser
    finally:
        await browser.close()


@needs_chromium
async def test_reads_a_page_as_a_snapshot_and_follows_links(session: BrowserSession) -> None:
    snapshot = await session.navigate(SHOP)
    assert "Title: Monitors" in snapshot
    assert "Acme 27UK: $379.99" in snapshot
    assert "[ref=" in snapshot
    # The beacon on a private address was refused, not fetched.
    assert any("intranet.example.com" in b for b in session.blocked)

    deals_ref = _ref(snapshot, 'link "Deals"')
    after = await session.click(deals_ref)
    assert "Title: Deals" in after
    assert "All monitors 10% off." in await session.extract()


@needs_chromium
async def test_buttons_and_forms_need_the_acting_tools(session: BrowserSession) -> None:
    snapshot = await session.navigate(SHOP)
    with pytest.raises(BrowserError, match="browser_press"):
        await session.click(_ref(snapshot, 'button "Add to cart"'))
    search_ref = _ref(snapshot, 'searchbox "Search"')
    with pytest.raises(BrowserError, match="browser_search"):
        await session.click(search_ref)


@needs_chromium
async def test_private_and_non_http_addresses_are_refused(session: BrowserSession) -> None:
    with pytest.raises(BrowserError, match="non-public address"):
        await session.navigate("http://intranet.example.com/")
    with pytest.raises(BrowserError, match="only http and https"):
        await session.navigate("file:///etc/passwd")


@needs_chromium
async def test_the_mcp_server_exposes_the_tools_and_reports_errors() -> None:
    browser = BrowserSession(resolve=fake_resolve, fixtures={SHOP: PAGE})
    async with Client(build_server(browser)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"navigate", "snapshot", "extract", "click", "search", "type", "press"}
        result = await client.call_tool("navigate", {"url": SHOP})
        assert not result.is_error
        block = result.content[0]
        assert isinstance(block, TextContent)
        assert "27-inch 4K monitors" in block.text
        refused = await client.call_tool("navigate", {"url": "http://intranet.example.com/"})
        assert refused.is_error


def _ref(snapshot: str, label: str) -> str:
    line = next(line for line in snapshot.splitlines() if label in line)
    return line.split("[ref=", 1)[1].split("]", 1)[0]
