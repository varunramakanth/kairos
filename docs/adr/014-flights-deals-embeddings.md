# ADR-014: Flights via SerpApi, deals by search and page reading, local embeddings

- **Status:** Accepted (2026-11-18; all three providers chosen by the owner)
- **Milestone:** M6

## Context

PLAN.md asked for one flight provider behind an adapter, a deals approach, and
embeddings for semantic memory recall. Provider availability was checked on
2026-11-18:

- **Amadeus:** shut down its self-service API in July 2026.
- **Kiwi Tequila:** appears to be invitation-only.
- **Duffel:** whether new developers still get a free sandbox is unclear, and its
  sandbox carries only a fictional airline.

## Decisions

### 1. Flights: `FlightProvider` interface, SerpApi's Google Flights first

- **Typed results:** `flights_search` returns typed offers, cheapest first, and a
  "Lowest price" line. A fare watch records that one stable number on every firing.
- **Mapping:** `SerpApiFlights` maps `best_flights` and `other_flights`, validated
  against Pydantic models of the documented response.
- **Cost:** the free plan has about 250 searches a month; one watch every 6 hours uses
  about 120.
- **Availability:** the tool is offered only when `SERPAPI_API_KEY` is set.
- **Errors:** errors become tool errors, and never contain the key.
- **Trust:** results are third-party content, so they are untrusted.
- **Swappable:** another provider (Duffel, or an MCP server) implements the protocol
  and nothing else changes.
- **Risk:** there is a reported, unverified Google lawsuit against SerpApi. The adapter
  boundary is the mitigation.

The PLAN.md fare-watch scenario passes end to end on Postgres with a fake clock: the
agent proposes, the user confirms, the watch fires five times, it alerts once per new
low with no duplicates, and the alerts go to the outbox without an approval.

### 2. Deals: search and page reading, no shopping API

- **Search and read:** `deals_search` uses Tavily to find shop pages, reads each one
  behind the same public-address guard as `web_fetch`, and extracts the prices visible
  on the page. Amounts that read as savings or instalments ("save $70", "$32/mo",
  "$100 off") are skipped.
- **Ranking:** pages that were read are ranked by their lowest price. Pages that could
  not be read never outrank them; their search-snippet prices are listed as unverified.
- **Keeping watch:** the agent proposes an ordinary watch on the best page.

**Known limit, seen in the recorded eval:** the big Canadian retailers render prices
with JavaScript, so fetched HTML often shows none, and pattern matching picks up
accessory prices. The agent reported this honestly ("no verified CAD price") and
offered a watch, and the judge scored the outcome as partial. The next steps are:

- read JavaScript-heavy shop pages through the browser service;
- or, if wanted later, a structured shopping source (SerpApi Google Shopping uses the
  same key and quota).

### 3. Embeddings: a local model, hybrid recall

- **Model:** `BAAI/bge-small-en-v1.5` through fastembed (ONNX). It has 384 dimensions,
  is about 67 MB and MIT licensed, and is cached in `~/.cache/kairos/models`, outside
  the iCloud-synced repository.
- **Privacy:** memories never leave the machine.
- **Loading:** it loads lazily in a worker thread, and one instance is shared per
  process.
- **Storage:** migration 0007 adds `vector(384)` with an HNSW cosine index.
- **Recall:** keyword and vector ranks are fused with reciprocal-rank fusion and
  weighted by recency. A similarity floor (0.55) keeps unrelated queries from
  returning anything.
- **Fallback:** if the model is unavailable, memories are stored without a vector and
  the worker backfills them at startup.
- **Turning it off:** `KAIROS_MEMORY_EMBEDDINGS=none` keeps keyword recall only. Tests
  pin it to `none`. A real-model test runs when the model is cached and is skipped in
  CI.

## Consequences

- **Eval coverage:** the gated suites now have 81 cases.
  - The browser suite (4) and an M6 suite (4) were recorded live for $0.20: memory
    recall, a memory proposal, memory poisoning, and deals.
  - Injection resistance is 1.0 across both, including a page that tried to plant a
    "preferred airline" memory.
- **Flight eval not yet recorded:** a live flight case waits for the owner's SerpApi
  key. The adapter, tool and watch scenario are tested offline.
- **Changing the embedding model:** a model with a different dimension needs a
  migration.
