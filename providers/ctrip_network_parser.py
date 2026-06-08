"""Ctrip network-response parsing helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from config_manager import RouteQuery
from providers import ctrip_itinerary


def extract_price_from_network(response_store: list[dict]) -> float | None:
    candidates: list[float] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if any(token in lowered for token in ["price", "fare", "ticket", "adultprice", "sortprice"]):
                    maybe = ctrip_itinerary.coerce_price(value)
                    if maybe is not None:
                        candidates.append(maybe)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for item in response_store:
        walk(item.get("payload"))

    filtered = [price for price in candidates if 200 <= price <= 5000]
    if not filtered:
        return None
    return min(filtered)


def extract_single_route_price_from_network(response_store: list[dict], route: RouteQuery) -> float | None:
    match = extract_single_route_match_from_network(response_store, route)
    if match is not None:
        return float(match["total_price"])
    return None


def extract_single_route_match_from_network(response_store: list[dict], route: RouteQuery) -> dict[str, object] | None:
    exact_low_price_matches: list[float] = []
    itinerary_matches: list[tuple[float, list[dict[str, object]]]] = []

    for item in response_store:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        for data_node in ctrip_itinerary.walk_data_nodes(payload):
            itineraries = data_node.get("flightItineraryList")
            if not isinstance(itineraries, list):
                continue
            for itinerary in itineraries:
                if not isinstance(itinerary, dict):
                    continue
                for price in itinerary.get("priceList") or []:
                    if not isinstance(price, dict):
                        continue
                    total = ctrip_itinerary.total_price(price)
                    if total is None:
                        continue
                    details = ctrip_itinerary.build_itinerary_details(itinerary, price)
                    if details_match_single_route(details, route):
                        itinerary_matches.append((total, details))

        for price_item in walk_price_list_items(payload):
            total = total_from_low_price_item(price_item)
            if total is None:
                continue
            depart_date = ctrip_date_to_iso(price_item.get("departDate"))
            return_date = ctrip_date_to_iso(price_item.get("returnDate"))
            if depart_date != route.departure_date:
                continue
            if route.return_date:
                if return_date == route.return_date:
                    exact_low_price_matches.append(total)
            elif not return_date:
                exact_low_price_matches.append(total)

    if itinerary_matches:
        total, details = min(itinerary_matches, key=lambda item: item[0])
        return {
            "total_price": total,
            "details": details,
            "source": "flight_itinerary_list",
        }
    if exact_low_price_matches:
        return {
            "total_price": min(exact_low_price_matches),
            "details": [],
            "source": "low_price_calendar",
        }
    return None


def extract_multi_city_match_from_network(
    response_store: list[dict],
    route: RouteQuery,
    *,
    preferred_total: float | None = None,
) -> dict[str, object] | None:
    best_match: tuple[int, int, dict, dict] | None = None

    for item in response_store:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        for data_node in ctrip_itinerary.walk_data_nodes(payload):
            itineraries = data_node.get("flightItineraryList")
            if not isinstance(itineraries, list):
                continue
            for itinerary in itineraries:
                if not isinstance(itinerary, dict):
                    continue
                for price in itinerary.get("priceList") or []:
                    if not isinstance(price, dict):
                        continue
                    total = _total_from_price_fields(price)
                    details = ctrip_itinerary.build_itinerary_details(itinerary, price)
                    if not ctrip_itinerary.multi_city_details_match_route(details, route):
                        continue
                    if route.transfer_policy == "direct_only":
                        if not ctrip_itinerary.multi_city_details_are_direct(details, route):
                            continue
                        score = (1, -total)
                    else:
                        if preferred_total is not None and abs(total - preferred_total) < 0.01:
                            score = (2, len(details))
                        else:
                            score = (1, -total)
                    if (
                        route.transfer_policy != "direct_only"
                        and preferred_total is not None
                        and abs(total - preferred_total) > 0.01
                        and best_match is not None
                        and best_match[:2] >= score
                    ):
                        continue
                    if best_match is None or score > best_match[:2]:
                        best_match = (score[0], score[1], itinerary, price)

    if best_match is None:
        return None

    _, _, itinerary, price = best_match
    return {
        "total_price": _total_from_price_fields(price),
        "details": ctrip_itinerary.build_itinerary_details(itinerary, price),
    }


def summarize_response_for_diagnostics(item: dict) -> dict[str, object]:
    payload = item.get("payload")
    summary: dict[str, object] = {
        "url": item.get("url"),
        "status": item.get("status"),
        "has_flight_itinerary_list": False,
        "top_keys": [],
        "shape": "",
    }
    if isinstance(payload, dict):
        summary["top_keys"] = list(payload.keys())[:20]
        summary["has_flight_itinerary_list"] = any(True for _ in ctrip_itinerary.walk_data_nodes(payload))
        summary["shape"] = payload_shape(payload)
    elif isinstance(payload, list):
        summary["shape"] = f"list[{len(payload)}]"
    else:
        summary["shape"] = type(payload).__name__
    return summary


def walk_price_list_items(node: object):
    if isinstance(node, dict):
        price_list = node.get("priceList")
        if isinstance(price_list, list):
            for item in price_list:
                if isinstance(item, dict):
                    yield item
        for value in node.values():
            yield from walk_price_list_items(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_price_list_items(item)


def total_from_low_price_item(item: dict) -> float | None:
    for key in ("totalPrice", "price"):
        value = ctrip_itinerary.coerce_price(item.get(key))
        if value is not None and 200 <= value <= 100000:
            return value
    return None


CTRIP_DATE_PATTERN = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def ctrip_date_to_iso(value: object) -> str | None:
    text = str(value or "").strip()
    match = CTRIP_DATE_PATTERN.fullmatch(text)
    if not match:
        return None
    millis = int(match.group(1))
    if millis < 0:
        return None
    offset_text = match.group(2) or "+0000"
    sign = 1 if offset_text[0] == "+" else -1
    hours = int(offset_text[1:3])
    minutes = int(offset_text[3:5])
    tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return datetime.fromtimestamp(millis / 1000, tz=tz).date().isoformat()


def details_match_single_route(details: list[dict[str, object]], route: RouteQuery) -> bool:
    if not details:
        return False
    if route.return_date:
        return details_match_round_trip(details, route)

    first = details[0]
    last = details[-1]
    if str(first.get("departure_date") or "")[:10] != route.departure_date:
        return False
    origin = str(first.get("origin") or first.get("departure_airport") or "").upper()
    destination = str(last.get("destination") or last.get("arrival_airport") or "").upper()
    if origin != route.origin.upper() or destination != route.destination.upper():
        return False
    if route.transfer_policy == "direct_only" and len(details) != 1:
        return False
    return True


def details_match_round_trip(details: list[dict[str, object]], route: RouteQuery) -> bool:
    outbound = details_for_logical_segment(details, 1, route.departure_date)
    inbound = details_for_logical_segment(details, 2, route.return_date or "")
    if not outbound or not inbound:
        return False

    if not details_match_direction(outbound, route.origin, route.destination, route.departure_date):
        return False
    if not details_match_direction(inbound, route.destination, route.origin, route.return_date or ""):
        return False
    if route.transfer_policy == "direct_only" and (len(outbound) != 1 or len(inbound) != 1):
        return False
    return True


def details_for_logical_segment(
    details: list[dict[str, object]],
    segment_index: int,
    departure_date: str,
) -> list[dict[str, object]]:
    by_segment = [
        item
        for item in details
        if int(item.get("segment_index", 0) or 0) == segment_index
    ]
    if by_segment:
        return by_segment
    return [
        item
        for item in details
        if str(item.get("departure_date") or "")[:10] == departure_date
    ]


def details_match_direction(
    details: list[dict[str, object]],
    origin_code: str,
    destination_code: str,
    departure_date: str,
) -> bool:
    if not details:
        return False
    first = details[0]
    last = details[-1]
    if str(first.get("departure_date") or "")[:10] != departure_date:
        return False
    origin = str(first.get("origin") or first.get("departure_airport") or "").upper()
    destination = str(last.get("destination") or last.get("arrival_airport") or "").upper()
    return origin == origin_code.upper() and destination == destination_code.upper()


def payload_shape(payload: dict) -> str:
    parts: list[str] = []
    for key, value in list(payload.items())[:10]:
        if isinstance(value, dict):
            parts.append(f"{key}:dict({len(value)})")
        elif isinstance(value, list):
            parts.append(f"{key}:list({len(value)})")
        else:
            parts.append(f"{key}:{type(value).__name__}")
    return ", ".join(parts)


def _cheapest_multi_city_match(response_store: list[dict]) -> tuple[int, int, dict, dict] | None:
    best_match: tuple[int, int, dict, dict] | None = None
    for item in response_store:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        for data_node in ctrip_itinerary.walk_data_nodes(payload):
            itineraries = data_node.get("flightItineraryList")
            if not isinstance(itineraries, list):
                continue
            for itinerary in itineraries:
                if not isinstance(itinerary, dict):
                    continue
                for price in itinerary.get("priceList") or []:
                    if not isinstance(price, dict):
                        continue
                    total = _total_from_price_fields(price)
                    score = (1, -total)
                    if best_match is None or score > best_match[:2]:
                        best_match = (score[0], score[1], itinerary, price)
    return best_match


def _total_from_price_fields(price: dict) -> float:
    return float(price.get("adultPrice", 0) or 0) + float(price.get("adultTax", 0) or 0)
