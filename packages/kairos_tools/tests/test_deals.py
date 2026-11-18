"""deals_search: search, read pages behind the address guard, and pull out prices."""

import json

import httpx2
import pytest
from pydantic import SecretStr

from kairos_domain.tools import ToolContext
from kairos_tools.deals import deals_search_tool, find_prices


@pytest.mark.parametrize(
    ("text", "prices"),
    [
        ("Now $379.99, was $449.99", [379.99, 449.99]),
        ("CA$1,249 or C$ 1,199.00", [1199.0, 1249.0]),
        ("Price: 419.00 CAD. Ships for $3.99", [419.0]),  # under $5 is a fee, not a price
        ("4.6 out of 5 stars, 27-inch, 2160p", []),
        ("USD 299 each", [299.0]),
    ],
)
def test_prices_are_read_from_page_text(text: str, prices: list[float]) -> None:
    assert find_prices(text) == prices


PAGES = {
    "https://shop.example.com/acme-27uk": (
        "<html><body><h1>Acme 27UK 4K</h1><p>Now $379.99</p><p>Was $449.99</p></body></html>"
    ),
    "https://other.example.com/zenith-q27": (
        "<html><body><h1>Zenith Q27</h1><p>Price: 419.00 CAD</p></body></html>"
    ),
}


def respond(request: httpx2.Request) -> httpx2.Response:
    if request.url.host == "api.tavily.com":
        body = json.loads(request.content)
        assert body["query"] == "27-inch 4K monitor price CAD"
        assert body["include_domains"] == ["shop.example.com", "other.example.com"]
        results = [
            {"title": "Acme 27UK", "url": "https://shop.example.com/acme-27uk", "content": "deal"},
            {"title": "Zenith Q27", "url": "https://other.example.com/zenith-q27", "content": ""},
            {"title": "Internal", "url": "http://10.0.0.8/internal", "content": "only $99"},
        ]
        return httpx2.Response(200, json={"results": results})
    return httpx2.Response(200, headers={"content-type": "text/html"}, text=PAGES[str(request.url)])


async def public(host: str) -> list[str]:
    return ["93.184.215.14"]


async def test_deals_are_listed_cheapest_first_and_private_pages_are_refused() -> None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        run = deals_search_tool(SecretStr("tvly-key"), http, resolve=public)
        out = await run(
            {
                "product": "27-inch 4K monitor",
                "max_price": 400,
                "sites": ["shop.example.com", "other.example.com"],
            },
            ToolContext(run_id="r", tool_call_id="c", idempotency_key="c"),
        )
    assert not out.is_error
    lines = out.content.splitlines()
    assert lines[1] == "- 379.99 (within budget): Acme 27UK"
    assert any(line.startswith("- 419.00: Zenith Q27") for line in lines)
    # The private address was refused and never outranks pages that were read; its
    # snippet price is kept only as an unverified lead.
    (internal,) = (line for line in lines if "Internal" in line)
    assert internal.startswith("- not read: Internal (http://10.0.0.8/internal) [refused:")
    assert internal.endswith("its search snippet mentions 99.00 (unverified)")
    assert "open the best page" in out.content
    assert "tvly-key" not in out.content
