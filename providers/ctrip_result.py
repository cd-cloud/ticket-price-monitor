"""Assemble only a frozen, validated Ctrip quote; never reselect its details."""
from copy import deepcopy
from typing import Any

from core.config_manager import RouteQuery
from providers import ctrip_network_parser, ctrip_itinerary
from providers.base import ParsedProviderResult, ProviderQueryOutcome
from tail.tail_quotes import quote_terms


def selected_quote(route, offer):
    match = deepcopy(getattr(offer, "matched_quote", None))
    if not match or match.get("source") != "flight_itinerary_list":
        raise RuntimeError("Ctrip quote unverified: calendar/visible-only prices cannot confirm the requested itinerary.")
    details = match.get("details") or []
    if route.is_multi_city:
        valid = ctrip_itinerary.multi_city_details_match_route(
            details, route, transfer_policy=route.transfer_policy)
    else:
        valid = ctrip_network_parser.details_match_single_route(details, route)
    if not valid or not ctrip_itinerary.details_match_preferences(details, route):
        raise RuntimeError("Ctrip selected quote no longer matches the requested conditions.")
    if match["total_price"] != offer.price:
        raise RuntimeError("Ctrip selected quote price and itinerary were changed independently.")
    return match


def _ctrip_quote_terms(route: RouteQuery, match: dict[str, Any]) -> dict[str, Any]:
    breakdown = match.get("price_breakdown") or {}
    has_explicit_tax = bool(breakdown.get("has_explicit_tax"))
    # Ctrip priceList stores adultPrice (+ adultTax when present) per adult.
    # For single-passenger queries we have an explicit per-person fare basis;
    # tax_included is True when adultTax was explicitly present and summed.
    return quote_terms(
        "ctrip",
        route.passengers,
        "CNY",
        price_basis="per_person",
        tax_included=True if has_explicit_tax else None,
        priced_passengers=1 if route.passengers == 1 else None,
        source_price_field="adultPrice+adultTax" if has_explicit_tax else "adultPrice",
    )


def _outcome(route, offer, final_url, title, *, extra=None):
    match = selected_quote(route, offer)
    details = match["details"]
    return ParsedProviderResult(
        provider="ctrip", price=match["total_price"], final_url=final_url, title=title,
        departure_time=details[0].get("departure_time"), row_text=offer.row_text,
        flight_details=details,
        parser="ctrip_multi_city_network" if route.is_multi_city else "ctrip_single_network",
        confidence="high",
        extra_payload={
            **(extra or {}), "detail_quality": "complete", "detail_source": "network_exact",
            "network_match_source": match["source"],
            "selected_quote": match,
            "quote_terms": _ctrip_quote_terms(route, match),
            "loading_evidence": match.get("loading_evidence"),
            "date_evidence": match.get("date_evidence"),
            "validation": {"status": "valid", "scope": "route_date_cabin_airline",
                           "version": 1},
        },
    ).to_outcome()


def build_multi_city_outcome(runtime: Any, route: RouteQuery, offer: Any, final_url: str,
                             title: str, response_store: list[dict]) -> ProviderQueryOutcome:
    return _outcome(route, offer, final_url, title, extra={
        "segment_count": len(route.segments or []),
        "segments": [segment.to_dict() for segment in route.segments or []],
        "itinerary_summary": runtime.build_itinerary_summary(route),
    })


def build_single_route_outcome(route: RouteQuery, offer: Any, final_url: str, title: str,
                              response_store: list[dict] | None = None) -> ProviderQueryOutcome:
    return _outcome(route, offer, final_url, title)
