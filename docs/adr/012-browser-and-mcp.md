# ADR-012: The browser as an MCP server, behind a generic MCP client

- **Status:** Accepted (2026-11-18; the browser design was approved by the owner)
- **Milestone:** M6

## Context

Some pages need a real browser: content rendered by JavaScript, site search,
following links between pages. Search (Tavily) and `web_fetch` stay the default,
because they are much cheaper. The owner also asked how new capabilities should be
added without writing one integration per service. The answer is the Model Context
Protocol (MCP).

## Decisions

### 1. A generic MCP client in `kairos_tools`

- **One configuration entry per server:** `McpServerConfig` starts a server as a
  subprocess over stdio, lists its tools, and registers each as a Kairos tool. A new
  capability is a configuration entry (`KAIROS_MCP_SERVERS`), not new code.
- **Kairos decides trust, not the server:** a server's `readOnlyHint` and similar
  annotations are hints from code we do not control, and they are ignored.
- **Safe defaults:** an unlisted tool is `write_external` and `untrusted`, so under the
  strict policy it waits for approval and its output is wrapped as data.
- **Allow list and prefixes:** `allow` limits which of a server's tools are offered.
  Names are prefixed (`browser_navigate`), so a server can't shadow a built-in tool.
- **Failures stay contained:** a server that fails to start is skipped and logged, and
  a call that fails becomes a tool error. Neither takes the agent down.

### 2. The browser is our own Python MCP server, not Microsoft's Playwright MCP

- **Why not Playwright MCP:** it is the obvious off-the-shelf choice. But it can only
  allow or block named sites, and the requirement is that only public addresses are
  reachable, checked the same way as in `web_fetch`.
- **Every request is checked:** our server (`services/browser`, Playwright for Python)
  intercepts every request the page makes, including scripts, images, XHR and
  redirects, and applies `check_public_url` to each one. Web sockets are refused.
- **Same snapshot design:** `page.aria_snapshot(mode="ai")` returns the accessibility
  tree with element references, and `page.get_by_ref` acts on them. It is the
  mechanism Playwright MCP uses.

### 3. Reading is free, acting needs approval

| Tool | Risk | Does |
|---|---|---|
| `browser_navigate`, `browser_snapshot`, `browser_extract` | read | open a page; return its snapshot or main text |
| `browser_click` | read | follows links, switches tabs, expands sections. **Refuses** buttons and form controls |
| `browser_search` | read | types into a **search box** only, and submits |
| `browser_type` | write_external (approval) | enters text into any other field |
| `browser_press` | write_external (approval) | presses buttons (submit, buy, accept) |

The split is enforced in two places:

- **The browser service:** it classifies the target element before a reading tool
  touches it.
- **The agent's policy:** it gates the acting tools.

A prompt injection that says "press Buy now" therefore produces an approval request,
never a purchase.

### 4. Isolation

- **Fresh state:** each session uses a new, incognito-style browser context. It has no
  saved profile, cookies or logins; downloads are refused, service workers are blocked
  and no permissions are granted.
- **Separate process:** the browser runs in the browser service's process, not the
  agent's.
- **Untrusted output:** everything the browser returns is untrusted content.

### 5. Computer use (screenshots) is out

Kairos does not need desktop control, and accessibility snapshots are cheaper and more
reliable for web pages. Tool results are text-only today, so there is no screenshot tool.

## Consequences

- **Enabling it:** set `KAIROS_BROWSER=1`, after `uv run playwright install chromium`
  (`just sync` runs it). CI installs Chromium and runs the browser tests against fixture
  pages, with no network.
- **Evals:** cases tagged `browser` get the browser tools when recorded. The browser
  suite covers JavaScript pages, site search, and two injections that try to make the
  agent press or type on a page.
- **Known gap, DNS rebinding:** the address check runs per request, and Chromium resolves
  the name again when it connects. A rebinding attack could therefore race the check.
  Pinning addresses through a filtering proxy is planned for M7 hardening.
- **Known gap, iframes:** pages inside iframes are visible in snapshots, but the
  read-versus-act classification is untested there.
