# ADR-008: Web search as a client-side tool on Tavily

- **Status:** Accepted (2026-11-17, provider chosen by the owner)
- **Milestone:** M2

## Context

M2 needs cited answers to questions such as "what's new in agent evals?". That takes a search tool and a page reader. `PLAN.md` listed Brave, Tavily and Exa. The Claude API also offers server-side `web_search` and `web_fetch` tools that run inside Anthropic.

## Options

| Option | For | Against |
|---|---|---|
| **Client tool on Tavily** (chosen) | Results go through our tool layer: provenance `UNTRUSTED`, the untrusted-content wrapper, policy hooks, traces and cassettes all apply. Works on Bedrock as well as the Anthropic API. Snippets are made for agents. Free tier; basic search is one credit. | A new vendor and API key. We maintain the adapter. |
| Brave Search API | Independent index. | Raw web-search results; more snippet cleanup on our side. |
| Anthropic server-side `web_search` / `web_fetch` | No new vendor; billed on the existing account; built-in citations. | Runs inside the provider, so results never pass our wrapper, policy hooks or cassettes. Not available on Bedrock, which breaks provider parity (ADR-001). |

## Decision

`web_search` calls the Tavily Search API with `search_depth: basic`. `web_fetch` is our own: plain HTTP plus `trafilatura` to extract the main text as Markdown with links. Both are `UNTRUSTED` and idempotent. `web_search` is only offered to the model when `TAVILY_API_KEY` is set.

`web_fetch` refuses non-http(s) URLs and any host that resolves to a non-public address (loopback, private ranges, link-local including cloud metadata, multicast), and re-checks every redirect hop. DNS rebinding between the check and the connection is not covered; the M6 browser service's network sandbox is the place for that.

## Consequences

- Every search result and fetched page is data in a delimited block, visible in the trace and replayable from cassettes.
- Swapping providers means writing one adapter behind the same tool name and input model.
- Tool unit tests use `httpx2.MockTransport`. Recording real search and fetch responses for evals needs a live run, which is gated on the owner's go-ahead.
