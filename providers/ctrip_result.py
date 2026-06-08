"""Ctrip result assembly helpers.

The browser flow finds a visible offer and captures network responses. This
module turns those raw pieces into the common ProviderQueryOutcome shape.
"""

from typing import Any

from config_manager import RouteQuery
from providers import ctrip_network_parser
from providers import ctrip_parser
from providers.base import ParsedProviderResult, ProviderQueryOutcome


def _single_route_detail_quality(route: RouteQuery, details: list[dict[str, object]]) -> str:
    if not details:
        return "price_only"
    if route.return_date and len(details) < 2:
        return "partial"
    return "complete"


def build_multi_city_outcome(
    runtime: Any,
    route: RouteQuery,
    offer: Any,
    final_url: str,
    title: str,
    response_store: list[dict],
) -> ProviderQueryOutcome:
    network_match = runtime.extract_ctrip_multi_city_match_from_network(
        response_store,
        route,
        preferred_total=offer.price,
    )
    if route.transfer_policy == "direct_only":
        if network_match is None:
            raise RuntimeError("No direct-only itinerary found for this route.")
        offer = runtime.offer_details_type(
            price=network_match["total_price"],
            departure_time=offer.departure_time,
            row_text=offer.row_text,
        )

    return ParsedProviderResult(
        provider="ctrip",
        price=offer.price,
        final_url=final_url,
        title=title,
        departure_time=offer.departure_time,
        row_text=offer.row_text,
        flight_details=(network_match or {}).get("details", []),
        extra_payload={
            "segment_count": len(route.segments or []),
            "segments": [segment.to_dict() for segment in route.segments or []],
            "itinerary_summary": runtime.build_itinerary_summary(route),
            "detail_quality": "complete" if network_match else "partial",
            "detail_source": "network_exact" if network_match else "visible_row",
        },
        parser="ctrip_multi_city_network" if network_match else "ctrip_multi_city_visible",
        confidence="high" if network_match else "medium",
    ).to_outcome()


def build_single_route_outcome(
    route: RouteQuery,
    offer: Any,
    final_url: str,
    title: str,
    response_store: list[dict] | None = None,
) -> ProviderQueryOutcome:
    network_match = ctrip_network_parser.extract_single_route_match_from_network(response_store or [], route)
    network_details = (network_match or {}).get("details", [])
    visible_details = ctrip_parser.parse_single_flight_details(offer.row_text, route)
    details = network_details or visible_details
    parser = "ctrip_single_network" if network_match else "ctrip_single_visible"
    confidence = "high" if network_details else ("medium" if visible_details else "low")
    detail_source = "visible_row" if visible_details else "price_only"
    if network_details:
        detail_source = "network_exact"
    elif network_match:
        detail_source = "network_low_price_calendar"
    extra_payload = {
        "detail_quality": _single_route_detail_quality(route, details),
        "detail_source": detail_source,
    }
    if network_match:
        extra_payload["network_match_source"] = network_match.get("source")
    return ParsedProviderResult(
        provider="ctrip",
        price=offer.price,
        final_url=final_url,
        title=title,
        departure_time=offer.departure_time,
        row_text=offer.row_text,
        flight_details=details,
        extra_payload=extra_payload,
        parser=parser,
        confidence=confidence,
    ).to_outcome()
