"""Feizhu (Fliggy) provider — powered by flyai-cli (Fliggy MCP API).

This module replaces the previous browser-automation approach with direct
CLI calls to @fly-ai/flyai-cli, which accesses Fliggy's MCP API under the hood.

No browser automation is required. Trial mode may restrict results; quotas,
latency and availability are provider-controlled, not guaranteed by this adapter.

Prerequisite:
    npm install -g @fly-ai/flyai-cli
"""

from __future__ import annotations

from core.config_manager import ProviderConfig, RouteQuery
from providers.base import ParsedProviderResult, ProviderQueryOutcome
from providers import feizhu_flyai_adapter
from providers import ctrip_itinerary
from tail.tail_quotes import quote_terms


name = "feizhu"
supports_multi_city = False
# Tail discovery is supported via the same flyai-cli infrastructure.
supports_tail_discovery = True


def details_match_request(details: list[dict], route: RouteQuery) -> bool:
    if not details or any(not isinstance(leg, dict) for leg in details):
        return False
    if not ctrip_itinerary.logical_legs_connected(details):
        return False
    first, last = details[0], details[-1]
    if str(first.get("departure_date") or "")[:10] != route.departure_date:
        return False
    if not ctrip_itinerary.endpoint_matches(first, route.origin):
        return False
    if not ctrip_itinerary.endpoint_matches(last, route.destination, arrival=True):
        return False
    if route.transfer_policy == "direct_only" and len(details) != 1:
        return False
    if route.transfer_policy == "transfer_only" and len(details) < 2:
        return False

    from tail.tail_quotes import cabin_matches
    from providers.ctrip_tail_parser import details_match_airlines

    requested_cabin = (route.cabin or "economy").lower()
    if any(not leg.get("cabin") or not cabin_matches(leg["cabin"], requested_cabin) for leg in details):
        return False
    return not route.preferred_airlines or all(
        details_match_airlines([leg], route.preferred_airlines)
        for leg in details
    )


async def query(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page,
    response_store: list[dict],
) -> ProviderQueryOutcome:
    """Query Feizhu via flyai-cli and return the cheapest flight offer."""
    if route.is_multi_city or route.return_date:
        raise RuntimeError("Feizhu provider currently supports one-way queries only.")
    if route.passengers != 1:
        raise RuntimeError(
            "Feizhu CLI currently returns single-passenger fares only; refusing to save multi-passenger quote without party pricing."
        )

    # Rate-limiting is still applied even though we're using a CLI.
    await runtime.throttle(provider)

    requested_cabin = (route.cabin or "economy").lower()
    journey_type = "1" if route.transfer_policy == "direct_only" else ("2" if route.transfer_policy == "transfer_only" else None)
    offers = await feizhu_flyai_adapter.search_flights(
        origin=route.origin,
        destination=route.destination,
        departure_date=route.departure_date,
        cabin=requested_cabin,
        journey_type=journey_type,
        timeout_seconds=max(10, provider.timeout_ms // 1000),
    )

    if not offers:
        raise RuntimeError(
            f"No Feizhu flights found for {route.origin}→{route.destination} on {route.departure_date}."
        )

    filtered_offers = [
        offer for offer in offers
        if details_match_request(offer.get("flight_details") or [], route)
    ]

    if not filtered_offers:
        raise RuntimeError(
            f"No Feizhu flights matched route/date/cabin/transfer/airline conditions for {route.origin}→{route.destination} on {route.departure_date}."
        )

    best = filtered_offers[0]
    return ParsedProviderResult(
        provider="feizhu",
        price=best["price"],
        final_url=best["final_url"],
        title=best["title"],
        departure_time=best["departure_time"],
        row_text=best["row_text"],
        flight_details=best["flight_details"],
        parser="feizhu_flyai_cli",
        confidence="high",
        extra_payload={
            "detail_quality": "complete",
            "detail_source": "flyai_mcp_api",
            "journey_type": best.get("journey_type"),
            "total_duration": best.get("total_duration"),
            "alternatives_count": len(offers),
            "search_metadata": best.get("search_metadata") or {},
            "source_price_field": best.get("source_price_field"),
            "source_price_value": best.get("source_price_value"),
            "quote_terms": quote_terms(
                "feizhu",
                route.passengers,
                "CNY",
                price_basis="per_person",
                priced_passengers=1 if route.passengers == 1 else None,
                source_price_field=best.get("source_price_field") or "ticketPrice",
            ),
            "validation": {
                "status": "valid",
                "scope": "route_date_cabin_transfer_airline",
                "version": 1,
            },
        },
    ).to_outcome()
