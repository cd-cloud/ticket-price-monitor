"""Feizhu (Fliggy) provider — powered by flyai-cli (Fliggy MCP API).

This module replaces the previous browser-automation approach with direct
CLI calls to @fly-ai/flyai-cli, which accesses Fliggy's MCP API under the hood.

No browser automation is required. Trial mode may restrict results; quotas,
latency and availability are provider-controlled, not guaranteed by this adapter.

Prerequisite:
    npm install -g @fly-ai/flyai-cli
"""

from __future__ import annotations

from config_manager import ProviderConfig, RouteQuery
from providers.base import ParsedProviderResult, ProviderQueryOutcome
from providers import feizhu_flyai_adapter


name = "feizhu"
supports_multi_city = False
# Tail discovery is supported via the same flyai-cli infrastructure.
supports_tail_discovery = True


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

    from tail_quotes import cabin_matches
    from providers.ctrip_tail_parser import details_match_airlines

    filtered_offers = []
    for offer in offers:
        details = offer.get("flight_details") or []
        if route.transfer_policy == "direct_only" and len(details) != 1:
            continue
        if route.transfer_policy == "transfer_only" and len(details) < 2:
            continue
        if route.preferred_airlines and not details_match_airlines(details, route.preferred_airlines):
            continue
        if any(leg.get("cabin") and not cabin_matches(leg["cabin"], requested_cabin) for leg in details):
            continue
        if requested_cabin not in {"economy", "economy_plus", "any"} and any(not leg.get("cabin") for leg in details):
            continue
        filtered_offers.append(offer)

    if not filtered_offers:
        raise RuntimeError(
            f"No Feizhu flights matched cabin/transfer/airline conditions for {route.origin}→{route.destination} on {route.departure_date}."
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
        },
    ).to_outcome()
