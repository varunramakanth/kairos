"""An isolated headless browser that reads pages as accessibility snapshots.

Safety model (ADR-012):

- **Isolation:** one fresh, incognito-style context per session: no saved profile,
  cookies or logins, downloads refused, service workers blocked, no permissions.
  The browser runs in the browser service's own process, not the agent's.
- **Public addresses only:** every request the page makes (documents, scripts,
  images, XHR, redirects) passes the same check as ``web_fetch``: http(s) only, and
  the host must resolve to public addresses. Web sockets are refused.
- **Reading vs acting:** ``click`` follows links, tabs and disclosure controls only,
  and ``search`` types only into search boxes. Typing into other fields and pressing
  buttons are separate tools that the agent's policy gates behind approval.
- **Untrusted output:** snapshots and extracted text are page content; the agent
  wraps them as data.

Known gap: the address check runs per request, and Chromium resolves the name again
when it connects, so a DNS rebinding attack could still race it. Pinning addresses
through a filtering proxy is planned for M7 hardening.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    WebSocketRoute,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from kairos_tools.web import (
    BlockedAddressError,
    Resolver,
    check_public_url,
    html_to_markdown,
    resolve_host,
)

SNAPSHOT_CHARS = 30_000
NAVIGATION_TIMEOUT_MS = 20_000
ACTION_TIMEOUT_MS = 8_000
LOCAL_SCHEMES = frozenset({"data", "blob", "about"})


class BrowserError(Exception):
    """A browser action that failed in a way the model should hear about."""


@dataclass(frozen=True)
class ElementInfo:
    tag: str
    role: str
    type: str
    href: str
    in_form: bool
    in_search: bool
    expanded: bool


Kind = Literal["read", "search", "act"]


def classify(element: ElementInfo) -> Kind:
    """What touching this element amounts to.

    ``read``: follows a link or reveals content (links, tabs, disclosure controls).
    ``search``: a search box, which only queries the site.
    ``act``: anything else (buttons, form fields), which may change something.
    """
    if element.tag == "a" and urlsplit(element.href).scheme in ("http", "https"):
        return "read"
    if element.role in ("link", "tab", "treeitem") or element.tag == "summary":
        return "read"
    if element.expanded and not element.in_form:
        return "read"  # a disclosure button (aria-expanded) only shows or hides content
    if (
        element.role == "searchbox"
        or element.type == "search"
        or (element.in_search and element.tag in ("input", "textarea"))
    ):
        return "search"
    return "act"


_DESCRIBE = """el => ({
  tag: el.tagName.toLowerCase(),
  role: el.getAttribute('role') || '',
  type: (el.getAttribute('type') || '').toLowerCase(),
  href: el.href || '',
  in_form: !!el.closest('form'),
  in_search: !!el.closest('[role=search], form[role=search], search'),
  expanded: el.hasAttribute('aria-expanded'),
})"""


class BrowserSession:
    """One page in one throwaway context. Start with ``start()``, end with ``close()``.

    ``fixtures`` maps exact URLs to HTML served without touching the network; tests
    and evals use it for planted pages. ``resolve`` is injectable for the same reason.
    """

    def __init__(
        self,
        *,
        resolve: Resolver = resolve_host,
        fixtures: Mapping[str, str] | None = None,
        launcher: Callable[[Playwright], Awaitable[Browser]] | None = None,
    ) -> None:
        self._resolve = resolve
        self._fixtures = dict(fixtures or {})
        self._launcher = launcher
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.blocked: list[str] = []

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        launch = self._launcher or _headless_chromium
        self._browser = await launch(self._playwright)
        self._context = await self._browser.new_context(
            accept_downloads=False,
            service_workers="block",
            java_script_enabled=True,
            permissions=[],
        )
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self._context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        await self._context.route("**/*", self._guard)
        await self._context.route_web_socket("**/*", self._refuse_socket)
        self._page = await self._context.new_page()

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserError("the browser is not started")
        return self._page

    async def _guard(self, route: Route) -> None:
        url = route.request.url
        if url in self._fixtures:
            await route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=self._fixtures[url]
            )
            return
        if urlsplit(url).scheme in LOCAL_SCHEMES:
            await route.continue_()
            return
        try:
            await check_public_url(url, self._resolve)
        except BlockedAddressError as error:
            self.blocked.append(f"{url}: {error}")
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _refuse_socket(self, socket: WebSocketRoute) -> None:
        self.blocked.append(f"{socket.url}: web sockets are not allowed")
        await socket.close(code=1008, reason="blocked by Kairos")

    # --- reading -----------------------------------------------------------------

    async def navigate(self, url: str) -> str:
        try:
            await check_public_url(url, self._resolve)
        except BlockedAddressError as error:
            if url not in self._fixtures:
                raise BrowserError(str(error)) from error
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as error:
            raise BrowserError(f"could not open {url}: {_first_line(error)}") from error
        return await self.snapshot()

    async def snapshot(self) -> str:
        page = self.page
        tree = await page.aria_snapshot(mode="ai")
        if len(tree) > SNAPSHOT_CHARS:
            tree = (
                tree[:SNAPSHOT_CHARS] + "\n… (snapshot truncated; use browser_extract for the text)"
            )
        return f"URL: {page.url}\nTitle: {await page.title()}\n\n{tree}"

    async def extract(self) -> str:
        page = self.page
        return html_to_markdown(await page.content(), url=page.url)

    async def describe(self, ref: str) -> ElementInfo:
        locator = self.page.get_by_ref(ref)
        try:
            raw: dict[str, Any] = await locator.evaluate(_DESCRIBE)
        except PlaywrightError as error:
            raise BrowserError(
                f"no element {ref} on the page; take a new browser_snapshot ({_first_line(error)})"
            ) from error
        return ElementInfo(**raw)

    async def click(self, ref: str) -> str:
        """Follow a link, open a tab or expand a section. Refuses anything that acts."""
        element = await self.describe(ref)
        kind = classify(element)
        if kind != "read":
            raise BrowserError(
                f"{ref} ({element.role or element.tag}) may change something; "
                "use browser_press (needs the user's approval) or browser_search for a search box"
            )
        return await self._do(lambda: self.page.get_by_ref(ref).click())

    async def search(self, ref: str, query: str) -> str:
        """Type a query into a search box and submit it."""
        element = await self.describe(ref)
        if classify(element) != "search":
            raise BrowserError(f"{ref} is not a search box; use browser_type (needs approval)")

        async def run() -> None:
            box = self.page.get_by_ref(ref)
            await box.fill(query)
            await box.press("Enter")

        return await self._do(run)

    # --- acting (gated behind approval by the agent's policy) ----------------------

    async def type(self, ref: str, text: str, *, submit: bool) -> str:
        async def run() -> None:
            field = self.page.get_by_ref(ref)
            await field.fill(text)
            if submit:
                await field.press("Enter")

        await self.describe(ref)
        return await self._do(run)

    async def press(self, ref: str) -> str:
        await self.describe(ref)
        return await self._do(lambda: self.page.get_by_ref(ref).click())

    async def _do(self, action: Callable[[], Awaitable[None]]) -> str:
        try:
            await action()
            await self.page.wait_for_load_state("domcontentloaded")
        except PlaywrightError as error:
            raise BrowserError(_first_line(error)) from error
        return await self.snapshot()


async def _headless_chromium(playwright: Playwright) -> Browser:
    return await playwright.chromium.launch(headless=True)


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
