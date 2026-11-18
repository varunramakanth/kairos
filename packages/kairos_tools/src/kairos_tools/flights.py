"""Flight search behind a provider interface (PLAN.md 3.3, ADR-014).

``flights_search`` returns structured offers sorted by price, so a fare watch can
record one stable number (the lowest price) on every firing. The first provider is
SerpApi's Google Flights API; another (Duffel, an MCP server) implements
``FlightProvider`` and nothing else changes.

Results come from a third party and are treated as untrusted content.
"""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Literal, Protocol

import httpx2
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

from kairos_domain.content import Provenance
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk
from kairos_tools.registry import Tool, tool

SERPAPI_URL = "https://serpapi.com/search.json"
TRAVEL_CLASSES = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
MAX_STOPS = {None: 0, 0: 1, 1: 2, 2: 3}
"""Our ``max_stops`` to SerpApi's ``stops`` code (0 means any)."""


class FlightQuery(BaseModel):
    """A round trip (with ``return_date``) or one way."""

    origin: str = Field(pattern=r"^[A-Z]{3}$", description="IATA airport code, e.g. YYZ.")
    destination: str = Field(pattern=r"^[A-Z]{3}$", description="IATA airport code, e.g. CJB.")
    outbound_date: date
    return_date: date | None = Field(default=None, description="Omit for a one-way trip.")
    adults: int = Field(default=1, ge=1, le=9)
    travel_class: Literal["economy", "premium_economy", "business", "first"] = "economy"
    max_stops: int | None = Field(default=None, ge=0, le=2, description="None means any.")
    currency: str = Field(default="CAD", pattern=r"^[A-Z]{3}$")


class FlightLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    airline: str
    flight_number: str
    departure_airport: str
    departure_time: str
    arrival_airport: str
    arrival_time: str
    duration_minutes: int


class FlightOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: float
    currency: str
    total_duration_minutes: int
    stops: int
    legs: list[FlightLeg]
    """The outbound journey. For a round trip, ``price`` is the round-trip total."""

    @property
    def airlines(self) -> list[str]:
        return list(dict.fromkeys(leg.airline for leg in self.legs))


class FlightSearchResult(BaseModel):
    query: FlightQuery
    offers: list[FlightOffer]
    """Cheapest first."""
    provider: str
    fetched_at: datetime
    price_level: str | None = None
    typical_price_range: tuple[float, float] | None = None

    @property
    def lowest_price(self) -> float | None:
        return self.offers[0].price if self.offers else None


class FlightProvider(Protocol):
    """Where flight offers come from."""

    @property
    def name(self) -> str: ...

    async def search(self, query: FlightQuery) -> FlightSearchResult: ...


class FlightSearchError(Exception):
    """The provider could not answer (bad request, quota, outage)."""


class SerpApiFlights:
    """Google Flights results through SerpApi (``engine=google_flights``).

    The free plan allows about 250 searches a month: one watch checking every six
    hours uses about 120.
    """

    name = "serpapi-google-flights"

    def __init__(self, api_key: SecretStr, http: httpx2.AsyncClient, *, clock: Any = None) -> None:
        self._key = api_key
        self._http = http
        self._now = clock.now if clock is not None else lambda: datetime.now().astimezone()

    def params(self, query: FlightQuery) -> dict[str, str]:
        params = {
            "engine": "google_flights",
            "departure_id": query.origin,
            "arrival_id": query.destination,
            "outbound_date": query.outbound_date.isoformat(),
            "type": "1" if query.return_date else "2",
            "adults": str(query.adults),
            "travel_class": str(TRAVEL_CLASSES[query.travel_class]),
            "stops": str(MAX_STOPS[query.max_stops]),
            "currency": query.currency,
            "hl": "en",
            "gl": "ca",
            "sort_by": "2",  # price
        }
        if query.return_date:
            params["return_date"] = query.return_date.isoformat()
        return params

    async def search(self, query: FlightQuery) -> FlightSearchResult:
        params = self.params(query) | {"api_key": self._key.get_secret_value()}
        try:
            response = await self._http.get(SERPAPI_URL, params=params, timeout=60.0)
        except httpx2.HTTPError as error:
            raise FlightSearchError(f"SerpApi unreachable: {type(error).__name__}") from error
        try:
            data: dict[str, Any] = response.json()
        except ValueError as error:
            raise FlightSearchError(
                f"SerpApi returned non-JSON ({response.status_code})"
            ) from error
        if response.status_code != 200 or "error" in data:
            message = str(data.get("error", f"HTTP {response.status_code}"))
            raise FlightSearchError(f"SerpApi: {message}")
        return parse_serpapi(data, query, fetched_at=self._now())


class _Airport(BaseModel):
    id: str = "?"
    time: str = ""


class _Segment(BaseModel):
    airline: str = "?"
    flight_number: str = ""
    departure_airport: _Airport = _Airport()
    arrival_airport: _Airport = _Airport()
    duration: int = 0


class _Option(BaseModel):
    price: float | None = None
    total_duration: int = 0
    flights: list[_Segment] = []
    layovers: list[dict[str, JsonValue]] = []


class _Insights(BaseModel):
    price_level: str | None = None
    typical_price_range: list[float] | None = None


class _SerpApiResponse(BaseModel):
    """The parts of SerpApi's Google Flights response we use; the rest is ignored."""

    best_flights: list[_Option] = []
    other_flights: list[_Option] = []
    price_insights: _Insights | None = None


def parse_serpapi(
    data: dict[str, Any], query: FlightQuery, *, fetched_at: datetime
) -> FlightSearchResult:
    """SerpApi's ``best_flights`` and ``other_flights`` as offers, cheapest first.
    Options without a price ("price unavailable") are skipped."""
    response = _SerpApiResponse.model_validate(data)
    offers = [
        FlightOffer(
            price=option.price,
            currency=query.currency,
            total_duration_minutes=option.total_duration,
            stops=len(option.layovers),
            legs=[
                FlightLeg(
                    airline=s.airline,
                    flight_number=s.flight_number,
                    departure_airport=s.departure_airport.id,
                    departure_time=s.departure_airport.time,
                    arrival_airport=s.arrival_airport.id,
                    arrival_time=s.arrival_airport.time,
                    duration_minutes=s.duration,
                )
                for s in option.flights
            ],
        )
        for option in [*response.best_flights, *response.other_flights]
        if option.price is not None and option.flights
    ]
    offers.sort(key=lambda o: (o.price, o.total_duration_minutes))
    insights = response.price_insights or _Insights()
    typical = insights.typical_price_range
    return FlightSearchResult(
        query=query,
        offers=offers,
        provider=SerpApiFlights.name,
        fetched_at=fetched_at,
        price_level=insights.price_level,
        typical_price_range=(typical[0], typical[1]) if typical and len(typical) == 2 else None,
    )


def describe(result: FlightSearchResult, *, top: int = 5) -> str:
    """The result as the model reads it: the lowest price first, then the offers."""
    q = result.query
    trip = f"{q.origin} → {q.destination}, {q.outbound_date}" + (
        f" to {q.return_date} (round trip)" if q.return_date else " (one way)"
    )
    if not result.offers:
        return f"No priced flights found for {trip}."
    lines = [f"Flights {trip}, {q.adults} adult(s), {q.travel_class}:"]
    lines.append(f"Lowest price: {result.lowest_price:.0f} {q.currency}")
    if result.price_level:
        typical = (
            f", typical {result.typical_price_range[0]:.0f}-{result.typical_price_range[1]:.0f}"
            if result.typical_price_range
            else ""
        )
        lines.append(f"Google's price level: {result.price_level}{typical}")
    for offer in result.offers[:top]:
        first, last = offer.legs[0], offer.legs[-1]
        hours, minutes = divmod(offer.total_duration_minutes, 60)
        lines.append(
            f"- {offer.price:.0f} {offer.currency}: {', '.join(offer.airlines)}, "
            f"{offer.stops} stop(s), {hours}h{minutes:02d}m, departs {first.departure_time} "
            f"from {first.departure_airport}, arrives {last.arrival_time} at {last.arrival_airport}"
        )
    lines.append(f"Source: {result.provider}, {result.fetched_at:%Y-%m-%d %H:%M %Z}")
    return "\n".join(lines)


def flights_search_tool(provider: FlightProvider) -> Tool[FlightQuery]:
    @tool(name="flights_search", risk=ToolRisk.READ, provenance=Provenance.UNTRUSTED)
    async def flights_search(args: FlightQuery, context: ToolContext) -> ToolOutput:
        """Search flights between two airports (IATA codes) for given dates, cheapest
        first. Give return_date for a round trip. In a fare watch, record the "Lowest
        price" line as the observation. Prices are indicative; you cannot book."""
        try:
            result = await provider.search(args)
        except FlightSearchError as error:
            return ToolOutput(content=f"flight search failed: {error}", is_error=True)
        return ToolOutput(content=describe(result))

    return flights_search


class StaticFlights:
    """A provider that serves prepared results, for tests and offline evals. Each search
    returns the next result in ``prices`` (one cheapest-offer price per call)."""

    name = "static"

    def __init__(self, prices: Sequence[float], *, fetched_at: datetime) -> None:
        self._prices = list(prices)
        self._fetched_at = fetched_at
        self.queries: list[FlightQuery] = []

    async def search(self, query: FlightQuery) -> FlightSearchResult:
        self.queries.append(query)
        price = self._prices.pop(0)
        leg = FlightLeg(
            airline="Air Canada",
            flight_number="AC 848",
            departure_airport=query.origin,
            departure_time=f"{query.outbound_date} 18:30",
            arrival_airport=query.destination,
            arrival_time=f"{query.outbound_date} 23:55",
            duration_minutes=1525,
        )
        offer = FlightOffer(
            price=price, currency=query.currency, total_duration_minutes=1525, stops=1, legs=[leg]
        )
        return FlightSearchResult(
            query=query, offers=[offer], provider=self.name, fetched_at=self._fetched_at
        )
