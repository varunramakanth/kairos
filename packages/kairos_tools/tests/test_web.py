"""web_search and web_fetch against a mocked transport. No network."""

import json

import httpx2
import pytest
from pydantic import SecretStr

from kairos_domain.content import Provenance
from kairos_domain.tools import ToolContext
from kairos_tools.plan import plan_tool
from kairos_tools.web import web_fetch_tool, web_search_tool

CTX = ToolContext(run_id="run_1", tool_call_id="t1", idempotency_key="t1")
PAGE = """<html><head><title>Agent evals in 2026</title></head><body>
<nav>Home | About</nav>
<article><h1>Agent evals in 2026</h1>
<p>Teams now grade agents on whole traces, not just final answers. Tool-call accuracy,
planning quality and memory retrieval are measured separately, then rolled up.</p>
<p>See the <a href="https://example.org/report">full report</a> for details.</p>
</article></body></html>"""


async def public(host: str) -> list[str]:
    return ["93.184.215.14"]


def client(handler: httpx2.MockTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=handler, follow_redirects=False)


async def test_search_formats_tavily_results_and_sends_the_key() -> None:
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Evals guide",
                        "url": "https://a.example/evals",
                        "content": "Grade   the trace.",
                        "published_date": "2026-11-02",
                    },
                    {"title": "Other", "url": "https://b.example", "content": "More."},
                ]
            },
        )

    search = web_search_tool(SecretStr("tvly-test"), client(httpx2.MockTransport(handle)))
    output = await search({"query": "agent evals", "topic": "news"}, CTX)

    assert not output.is_error
    assert output.content.startswith("1. Evals guide (2026-11-02)\n   https://a.example/evals\n")
    assert "Grade the trace." in output.content
    assert "2. Other\n   https://b.example" in output.content
    (request,) = seen
    assert request.headers["authorization"] == "Bearer tvly-test"
    body = json.loads(request.content)
    assert body["search_depth"] == "basic"  # one credit per call
    assert body["topic"] == "news"
    assert search.spec.provenance is Provenance.UNTRUSTED


async def test_search_reports_http_errors_without_leaking_the_key() -> None:
    search = web_search_tool(
        SecretStr("tvly-secret"),
        client(httpx2.MockTransport(lambda r: httpx2.Response(401, json={"detail": "bad key"}))),
    )
    output = await search({"query": "x"}, CTX)
    assert output.is_error
    assert output.content == "search failed: HTTP 401"


async def test_fetch_returns_main_text_as_markdown() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    fetch = web_fetch_tool(client(httpx2.MockTransport(handle)), resolve=public)
    output = await fetch({"url": "https://news.example/evals"}, CTX)

    assert not output.is_error
    assert output.content.startswith("# Agent evals in 2026\nURL: https://news.example/evals\n")
    assert "whole traces" in output.content
    assert "https://example.org/report" in output.content
    assert "Home | About" not in output.content  # navigation is stripped


async def test_fetch_follows_redirects_and_checks_every_hop() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "short.example":
            return httpx2.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        return httpx2.Response(200, text="metadata")

    async def resolve(host: str) -> list[str]:
        return ["93.184.215.14"]

    fetch = web_fetch_tool(client(httpx2.MockTransport(handle)), resolve=resolve)
    output = await fetch({"url": "https://short.example/x"}, CTX)

    assert output.is_error
    assert output.content.startswith("refused: 169.254.169.254 resolves to a non-public address")


@pytest.mark.parametrize(
    ("url", "addresses"),
    [
        ("http://localhost:8000/admin", ["127.0.0.1"]),
        ("http://router.home/", ["192.168.1.1"]),
        ("http://[::1]/", []),
        ("http://10.0.0.5/", []),
        ("file:///etc/passwd", []),
    ],
)
async def test_fetch_refuses_non_public_targets(url: str, addresses: list[str]) -> None:
    async def resolve(host: str) -> list[str]:
        return addresses

    def never(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("must not connect")

    fetch = web_fetch_tool(client(httpx2.MockTransport(never)), resolve=resolve)
    output = await fetch({"url": url}, CTX)
    assert output.is_error
    assert output.content.startswith("refused:")


async def test_fetch_refuses_binary_content() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}
        )

    fetch = web_fetch_tool(client(httpx2.MockTransport(handle)), resolve=public)
    output = await fetch({"url": "https://a.example/x.pdf"}, CTX)
    assert output.is_error
    assert "application/pdf" in output.content


async def test_plan_tool_counts_completed_steps() -> None:
    plan = plan_tool()
    output = await plan(
        {
            "items": [
                {"step": "Search", "status": "completed"},
                {"step": "Read", "status": "in_progress"},
                {"step": "Answer", "status": "pending"},
            ]
        },
        CTX,
    )
    assert output.content == "Plan updated: 1 of 3 steps done."
    invalid = await plan({"items": [{"step": "x", "status": "done"}]}, CTX)
    assert invalid.is_error
