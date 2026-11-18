"""Flight search: the SerpApi adapter on a canned response (no network), and the tool."""

import json
from datetime import UTC, date, datetime

import httpx2
import pytest
from pydantic import SecretStr

from kairos_domain.tools import ToolContext
from kairos_tools.flights import (
    FlightQuery,
    SerpApiFlights,
    StaticFlights,
    describe,
    flights_search_tool,
)

NOW = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)

# The shape of SerpApi's documented google_flights response, trimmed to what we read.
RESPONSE = {
    "search_metadata": {"status": "Success"},
    "best_flights": [
        {
            "flights": [
                {
                    "departure_airport": {
                        "name": "Toronto Pearson",
                        "id": "YYZ",
                        "time": "2026-12-10 18:30",
                    },
                    "arrival_airport": {"name": "Dubai", "id": "DXB", "time": "2026-12-11 15:50"},
                    "duration": 800,
                    "airline": "Emirates",
                    "flight_number": "EK 242",
                },
                {
                    "departure_airport": {"name": "Dubai", "id": "DXB", "time": "2026-12-11 21:45"},
                    "arrival_airport": {
                        "name": "Coimbatore",
                        "id": "CJB",
                        "time": "2026-12-12 03:10",
                    },
                    "duration": 235,
                    "airline": "Emirates",
                    "flight_number": "EK 532",
                },
            ],
            "layovers": [{"duration": 355, "name": "Dubai", "id": "DXB"}],
            "total_duration": 1390,
            "price": 1612,
            "type": "Round trip",
        }
    ],
    "other_flights": [
        {
            "flights": [
                {
                    "departure_airport": {
                        "name": "Toronto Pearson",
                        "id": "YYZ",
                        "time": "2026-12-10 21:00",
                    },
                    "arrival_airport": {
                        "name": "Coimbatore",
                        "id": "CJB",
                        "time": "2026-12-12 09:40",
                    },
                    "duration": 1960,
                    "airline": "Air India",
                    "flight_number": "AI 188",
                }
            ],
            "layovers": [
                {"duration": 300, "name": "Delhi", "id": "DEL"},
                {"duration": 120, "name": "Chennai", "id": "MAA"},
            ],
            "total_duration": 1960,
            "price": 1388,
        },
        {"flights": [{"airline": "Unknown"}], "total_duration": 1500},  # no price: skipped
    ],
    "price_insights": {
        "lowest_price": 1388,
        "price_level": "typical",
        "typical_price_range": [1300, 1800],
    },
}

QUERY = FlightQuery(
    origin="YYZ",
    destination="CJB",
    outbound_date=date(2026, 12, 10),
    return_date=date(2026, 12, 20),
)


def client(handler: httpx2.MockTransport) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=handler)


async def test_serpapi_results_are_parsed_cheapest_first() -> None:
    seen: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=RESPONSE)

    async with client(httpx2.MockTransport(respond)) as http:
        result = await SerpApiFlights(SecretStr("key-123"), http).search(QUERY)

    (request,) = seen
    params = dict(request.url.params)
    assert params["engine"] == "google_flights"
    assert params | {"api_key": "x"} == {
        "engine": "google_flights",
        "departure_id": "YYZ",
        "arrival_id": "CJB",
        "outbound_date": "2026-12-10",
        "return_date": "2026-12-20",
        "type": "1",
        "adults": "1",
        "travel_class": "1",
        "stops": "0",
        "currency": "CAD",
        "hl": "en",
        "gl": "ca",
        "sort_by": "2",
        "api_key": "x",
    }
    assert [o.price for o in result.offers] == [1388, 1612]
    assert result.lowest_price == 1388
    cheapest = result.offers[0]
    assert (cheapest.stops, cheapest.airlines) == (2, ["Air India"])
    assert result.offers[1].airlines == ["Emirates"]
    assert (result.price_level, result.typical_price_range) == ("typical", (1300, 1800))

    text = describe(result)
    assert "Lowest price: 1388 CAD" in text
    assert "YYZ → CJB, 2026-12-10 to 2026-12-20 (round trip)" in text
    assert "key-123" not in text


async def test_errors_become_tool_errors_and_never_leak_the_key() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": "Invalid API key."})

    async with client(httpx2.MockTransport(respond)) as http:
        run = flights_search_tool(SerpApiFlights(SecretStr("key-123"), http))
        out = await run(
            json.loads(QUERY.model_dump_json()),
            ToolContext(run_id="r", tool_call_id="c", idempotency_key="c"),
        )
    assert out.is_error
    assert out.content == "flight search failed: SerpApi: Invalid API key."


async def test_the_tool_validates_airports_and_uses_any_provider() -> None:
    provider = StaticFlights([1250.0], fetched_at=NOW)
    run = flights_search_tool(provider)
    context = ToolContext(run_id="r", tool_call_id="c", idempotency_key="c")
    bad = await run(
        {"origin": "Toronto", "destination": "CJB", "outbound_date": "2026-12-10"}, context
    )
    assert bad.is_error
    good = await run(
        {"origin": "YYZ", "destination": "CJB", "outbound_date": "2026-12-10"}, context
    )
    assert "Lowest price: 1250 CAD" in good.content
    assert "(one way)" in good.content
    assert provider.queries[0].return_date is None


def test_one_way_and_cabin_parameters() -> None:
    flights = SerpApiFlights(SecretStr("k"), httpx2.AsyncClient())
    params = flights.params(
        FlightQuery(
            origin="YYZ",
            destination="LIS",
            outbound_date=date(2027, 3, 20),
            travel_class="business",
            max_stops=0,
            adults=2,
        )
    )
    assert (params["type"], params["travel_class"], params["stops"], params["adults"]) == (
        "2",
        "3",
        "1",
        "2",
    )
    assert "return_date" not in params


@pytest.mark.parametrize("payload", [{"best_flights": []}, {}])
async def test_no_priced_flights(payload: dict[str, object]) -> None:
    async with client(httpx2.MockTransport(lambda r: httpx2.Response(200, json=payload))) as http:
        result = await SerpApiFlights(SecretStr("k"), http).search(QUERY)
    assert result.lowest_price is None
    assert describe(result).startswith("No priced flights found")
