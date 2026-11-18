"""``deals_search``: find product pages and the prices they show (PLAN.md 3.3, ADR-014).

Search plus page reading, no paid shopping API: Tavily finds candidate product pages,
each is fetched with the same public-address guard as ``web_fetch``, and the prices
visible on the page are pulled out with a pattern match. Prices found this way are
leads, not facts (a page can show list prices, bundles or other products), so the
tool says so and the agent should open the best page before quoting it. To keep
looking for a deal, the agent proposes a watch on the best product page.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx2
from pydantic import BaseModel, Field, SecretStr

from kairos_domain.content import Provenance
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk
from kairos_tools.registry import Tool, tool
from kairos_tools.web import Resolver, WebToolError, fetch_markdown, resolve_host, tavily_search

PAGES = 5
_AMOUNT = r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)"
_PRICE = re.compile(
    rf"(?:(?:CA|C|US)?\$|CAD\s?|USD\s?)\s?{_AMOUNT}(?!\d)|{_AMOUNT}\s?(?:CAD|USD|\$)(?![A-Za-z])",
    re.IGNORECASE,
)
MIN_PRICE = 5.0
"""Smaller amounts on a product page are almost always shipping, fees or ratings."""


class DealsInput(BaseModel):
    product: str = Field(min_length=3, max_length=200, description="What to buy, specifically.")
    max_price: float | None = Field(default=None, gt=0, description="Budget, if any.")
    currency: str = Field(default="CAD", pattern=r"^[A-Z]{3}$")
    sites: list[str] = Field(
        default=[],
        max_length=5,
        description="Limit to these shop domains, e.g. ['bestbuy.ca', 'canadacomputers.com'].",
    )


_AFTER = re.compile(
    r"^\s?(?:/\s?mo\b|/\s?month|per\s+month|a\s+month|monthly|off\b)", re.IGNORECASE
)
_BEFORE = re.compile(r"(?:save|saving|savings of)\s*$", re.IGNORECASE)
"""Words right after or before an amount that make it an instalment or a saving."""


def find_prices(text: str) -> list[float]:
    """Distinct prices in a page's text, lowest first. Amounts directly followed by
    "/mo" or "off", or preceded by "save", are instalments or savings and skipped."""
    found: set[float] = set()
    for match in _PRICE.finditer(text):
        if _AFTER.search(text[match.end() : match.end() + 16]) or _BEFORE.search(
            text[max(0, match.start() - 12) : match.start()]
        ):
            continue
        raw = next(group for group in match.groups() if group)
        try:
            value = float(re.sub(r"[,\s]", "", raw))
        except ValueError:
            continue
        if value >= MIN_PRICE:
            found.add(value)
    return sorted(found)


@dataclass(frozen=True)
class Candidate:
    title: str
    url: str
    prices: list[float]
    error: str | None = None


def describe(
    product: str, candidates: Sequence[Candidate], *, max_price: float | None, currency: str
) -> str:
    """Pages that were read, ranked by their lowest price; unread pages listed after,
    with any price their search snippet mentioned marked as unverified."""
    read = [c for c in candidates if c.error is None]
    priced = sorted((c for c in read if c.prices), key=lambda c: c.prices[0])
    lines = [f"Pages for {product!r}, by the lowest price each page shows ({currency}):"]
    if not priced:
        lines.append("No prices found on the pages read.")
    for c in priced:
        lowest = c.prices[0]
        within = " (within budget)" if max_price is not None and lowest <= max_price else ""
        shown = ", ".join(f"{p:,.2f}" for p in c.prices[:4])
        lines.append(f"- {lowest:,.2f}{within}: {c.title}\n  {c.url}\n  prices on page: {shown}")
    for c in read:
        if not c.prices:
            lines.append(f"- no price on page: {c.title} ({c.url})")
    for c in candidates:
        if c.error is not None:
            hint = (
                f"; its search snippet mentions {c.prices[0]:,.2f} (unverified)" if c.prices else ""
            )
            lines.append(f"- not read: {c.title} ({c.url}) [{c.error}]{hint}")
    lines.append(
        "Prices are read from page text and may include list prices or other items: open "
        "the best page (web_fetch or the browser) before quoting it. To keep looking, "
        "propose a watch on that page with a 'below' condition."
    )
    return "\n".join(lines)


def deals_search_tool(
    api_key: SecretStr, client: httpx2.AsyncClient, *, resolve: Resolver = resolve_host
) -> Tool[DealsInput]:
    @tool(name="deals_search", risk=ToolRisk.READ, provenance=Provenance.UNTRUSTED)
    async def deals_search(args: DealsInput, context: ToolContext) -> ToolOutput:
        """Look for a product's current prices: searches shops, reads the top product
        pages and lists the prices each shows, cheapest first. Use it for shopping
        questions ("a 27-inch 4K monitor under $400"). It cannot buy anything."""
        body: dict[str, object] = {
            "query": f"{args.product} price {args.currency}",
            "max_results": PAGES,
            "search_depth": "basic",
        }
        if args.sites:
            body["include_domains"] = args.sites
        try:
            results = await tavily_search(api_key, client, body)
        except WebToolError as error:
            return ToolOutput(content=str(error), is_error=True)
        if not results:
            return ToolOutput(content=f"No shop pages found for {args.product!r}.")

        async def read(result: dict[str, object]) -> Candidate:
            url, title = str(result.get("url", "")), str(result.get("title", "(untitled)"))
            try:
                text = await fetch_markdown(client, url, resolve=resolve)
            except WebToolError as error:
                snippet = str(result.get("content", ""))
                return Candidate(title, url, find_prices(snippet), error=str(error))
            return Candidate(title, url, find_prices(text))

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(read(r)) for r in results[:PAGES]]
        candidates = [t.result() for t in tasks]
        return ToolOutput(
            content=describe(
                args.product, candidates, max_price=args.max_price, currency=args.currency
            )
        )

    return deals_search
