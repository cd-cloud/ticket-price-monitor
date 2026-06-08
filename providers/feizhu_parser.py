"""Parsing helpers for Feizhu flight search results."""

from __future__ import annotations

import json
import re
from typing import Any

from providers import ctrip_tail_discovery
from providers import ctrip_tail_parser


PRICE_PATTERN = re.compile(r"(?<!\d)(?:CNY|RMB|[\u00a5\uffe5])?\s*([1-9]\d{1,5})(?:\.\d{1,2})?")
PRICE_KEYWORDS = ("price", "fare", "amount", "adult", "ticket", "sale", "sort", "total")
FLIGHT_LIST_KEYS = (
    "flight",
    "flights",
    "flightList",
    "flightInfoList",
    "flightInfo",
    "flightInfos",
    "flightItems",
    "flightItemList",
    "dataList",
    "items",
    "list",
    "result",
    "results",
    "recommendList",
    "recommendRouteList",
    "solutionList",
    "itineraryList",
    "itineraries",
)
SEGMENT_KEYS = (
    "flightSegments",
    "flightSegment",
    "flightSegmentList",
    "segment",
    "segments",
    "segmentInfo",
    "segmentInfoList",
    "segmentList",
    "transferSegments",
    "transferSegmentList",
    "transferFlightList",
    "flightList",
    "legs",
    "legList",
    "journeys",
    "journeyList",
    "slices",
    "sliceList",
    "bounds",
    "boundList",
    "flights",
    "flightInfoList",
)
CABIN_KEYS = (
    "cabin",
    "cabinInfo",
    "cabinInfos",
    "cabinList",
    "seatInfo",
    "seatInfos",
    "seatList",
    "priceInfo",
    "priceInfos",
    "priceList",
    "prices",
    "fare",
    "fareInfo",
    "fareInfos",
    "fareList",
)
FLIGHT_WRAPPER_KEYS = (
    "flightInfo",
    "flight",
    "segment",
    "segmentInfo",
    "flightSegment",
    "leg",
    "bound",
    "slice",
)
PRICE_KEYS = (
    "bestPrice",
    "price",
    "ticketPrice",
    "adultPrice",
    "totalPrice",
    "lowestPrice",
    "lowPrice",
    "salePrice",
    "displayPrice",
    "amount",
    "fareAmount",
)
JSONP_PATTERN = re.compile(r"^[\w.$]+\((.*)\)\s*;?\s*$", re.S)
VERIFICATION_URL_MARKERS = ("_____tmd_____", "punish", "newslidecaptcha", "x5secdata")
VERIFICATION_PAYLOAD_MARKERS = (
    "puzzle-verify",
    "drag-verify",
    "click-verify",
    "slider-error",
    "captcha",
    "verify",
    "baxia",
    "x5sec",
)


def extract_prices(text: str) -> list[float]:
    normalized = str(text or "").replace(",", "")
    prices: list[float] = []
    for match in PRICE_PATTERN.finditer(normalized):
        value = float(match.group(1))
        if 50 <= value <= 50000:
            prices.append(value)
    return prices


def extract_prices_from_network(response_store: list[dict]) -> list[float]:
    prices: list[float] = []

    def walk(node: Any, *, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if any(token in lowered for token in PRICE_KEYWORDS):
                    maybe = coerce_price(value)
                    if maybe is not None:
                        prices.append(maybe)
                walk(value, key_hint=lowered)
        elif isinstance(node, list):
            for item in node:
                walk(item, key_hint=key_hint)
        elif key_hint and any(token in key_hint for token in PRICE_KEYWORDS):
            maybe = coerce_price(node)
            if maybe is not None:
                prices.append(maybe)

    for item in response_store:
        walk(item.get("payload"))
    return sorted({price for price in prices if 50 <= price <= 50000})


def extract_best_flight_match_from_network(response_store: list[dict]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in response_store:
        if not response_item_looks_like_flight_search(item):
            continue
        payload = item.get("payload")
        if not isinstance(payload, (dict, list)):
            continue
        for flight, cabin in iter_flight_cabin_candidates(payload):
            if cabin_has_restricted_notice(cabin):
                continue
            price = candidate_price(flight, cabin)
            if price is None:
                continue
            candidates.append(
                {
                    "total_price": price,
                    "details": [flight_detail(flight, cabin)],
                    "source": "searchow_flight_list",
                }
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item["total_price"])


def extract_tail_match_from_network(
    response_store: list[dict],
    *,
    origin: str,
    transfer: str,
    destination: str,
    preferred_airlines: list[str] | None = None,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in response_store:
        if not response_item_looks_like_flight_search(item):
            continue
        payload = item.get("payload")
        if not isinstance(payload, (dict, list)):
            continue
        for flight, cabin in iter_flight_cabin_candidates(payload):
            if cabin_has_restricted_notice(cabin):
                continue
            price = candidate_price(flight, cabin)
            if price is None:
                continue
            details = flight_details_from_node(flight, cabin)
            if not ctrip_tail_discovery.details_match_tail_route(details, origin, transfer, destination):
                continue
            if not ctrip_tail_parser.details_match_airlines(details, preferred_airlines):
                continue
            candidates.append(
                {
                    "total_price": price,
                    "details": details,
                    "source": "searchow_flight_list",
                    "detail_source": "network_flight_list",
                    "detail_quality": "complete",
                }
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item["total_price"])


def build_network_diagnostics(response_store: list[dict], *, transfer: str | None = None) -> dict[str, Any]:
    payloads = [item for item in response_store if response_item_looks_like_flight_search(item)]
    candidates = summarize_flight_candidates(response_store)
    priced = [item for item in candidates if item.get("price") is not None]
    via_transfer = [
        item
        for item in candidates
        if transfer and transfer.upper() in {str(code).upper() for code in item.get("route_codes", [])}
    ]
    return {
        "captured": len(response_store),
        "flight_payloads": len(payloads),
        "itineraries": len(candidates),
        "priced": len(priced),
        "via_transfer": len(via_transfer),
        "lowest_prices": sorted({item["price"] for item in priced})[:6],
        "sample_routes": [format_candidate_summary(item) for item in candidates[:8]],
        "sample_payload_shapes": summarize_payload_shapes(payloads[:8]),
    }


def format_network_diagnostics(response_store: list[dict], *, transfer: str) -> str:
    diagnostics = build_network_diagnostics(response_store, transfer=transfer)
    route_text = "; ".join(diagnostics["sample_routes"]) or "none"
    price_text = ", ".join(str(int(price)) for price in diagnostics["lowest_prices"]) or "none"
    blocker_text = " verification_payloads=yes," if response_store_has_verification(response_store) else ""
    return (
        "Feizhu network diagnostics: "
        f"{blocker_text}"
        f"captured={diagnostics['captured']}, "
        f"flight_payloads={diagnostics['flight_payloads']}, "
        f"itineraries={diagnostics['itineraries']}, "
        f"priced={diagnostics['priced']}, "
        f"via_{transfer}={diagnostics['via_transfer']}, "
        f"lowest_prices={price_text}, "
        f"sample_routes={route_text}, "
        f"payload_shapes={'; '.join(diagnostics['sample_payload_shapes']) or 'none'}."
    )


def response_store_has_verification(response_store: list[dict]) -> bool:
    for item in response_store:
        url = str(item.get("url") or "").lower()
        if any(marker in url for marker in VERIFICATION_URL_MARKERS):
            return True
        shape = payload_shape(item.get("payload")).lower()
        if any(marker in shape for marker in VERIFICATION_PAYLOAD_MARKERS):
            return True
    return False


def summarize_payload_shapes(items: list[dict[str, Any]]) -> list[str]:
    shapes: list[str] = []
    seen: set[str] = set()
    for item in items:
        payload = item.get("payload")
        shape = payload_shape(payload)
        if not shape or shape in seen:
            continue
        seen.add(shape)
        shapes.append(shape)
        if len(shapes) >= 5:
            break
    return shapes


def payload_shape(node: Any, *, max_depth: int = 3) -> str:
    parts: list[str] = []

    def walk(value: Any, prefix: str, depth: int) -> None:
        if depth > max_depth or len(parts) >= 12:
            return
        if isinstance(value, dict):
            keys = [str(key) for key in value.keys()][:8]
            if keys:
                parts.append(f"{prefix or '$'}{{{','.join(keys)}}}")
            for key in keys[:4]:
                child = value.get(key)
                if isinstance(child, (dict, list)):
                    walk(child, f"{prefix}.{key}" if prefix else key, depth + 1)
        elif isinstance(value, list):
            parts.append(f"{prefix or '$'}[{len(value)}]")
            if value:
                walk(value[0], f"{prefix}[0]" if prefix else "$[0]", depth + 1)

    walk(node, "", 0)
    return " > ".join(parts)


def summarize_flight_candidates(response_store: list[dict]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    seen: set[tuple[str, float | None]] = set()
    for item in response_store:
        if not response_item_looks_like_flight_search(item):
            continue
        payload = item.get("payload")
        if not isinstance(payload, (dict, list)):
            continue
        for flight, cabin in iter_flight_cabin_candidates(payload):
            if cabin_has_restricted_notice(cabin):
                continue
            details = flight_details_from_node(flight, cabin)
            route_codes = route_codes_from_details(details)
            if not route_codes:
                continue
            price = candidate_price(flight, cabin)
            route_key = "->".join(route_codes)
            marker = (route_key, price)
            if marker in seen:
                continue
            seen.add(marker)
            summaries.append(
                {
                    "route": route_key,
                    "route_codes": route_codes,
                    "price": price,
                    "flight_nos": [detail.get("flight_no") for detail in details if detail.get("flight_no")],
                }
            )
    return sorted(summaries, key=lambda item: (item.get("price") is None, item.get("price") or 999999, item["route"]))


def format_candidate_summary(candidate: dict[str, Any]) -> str:
    route = str(candidate.get("route") or "")
    price = candidate.get("price")
    flight_nos = "/".join(str(item) for item in candidate.get("flight_nos", [])[:3])
    price_text = f"CNY {int(price)}" if isinstance(price, (int, float)) else "no_price"
    return " ".join(part for part in [route, flight_nos, price_text] if part)


def route_codes_from_details(details: list[dict[str, Any]]) -> list[str]:
    if not details:
        return []
    route: list[str] = []
    for index, detail in enumerate(details):
        origin = detail.get("origin") or detail.get("departure_airport")
        destination = detail.get("destination") or detail.get("arrival_airport")
        if index == 0 and origin:
            route.append(str(origin).upper())
        if destination:
            route.append(str(destination).upper())
    return route


def response_item_looks_like_flight_search(item: dict[str, Any]) -> bool:
    url = str(item.get("url") or "").lower()
    if any(token in url for token in ("searchow/search", "flight_search", "flight", "jipiao")):
        return True
    payload = item.get("payload")
    if not isinstance(payload, (dict, list)):
        return False
    return bool(iter_flight_cabin_candidates(payload))


def iter_flight_cabin_candidates(payload: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[int] = set()

    def add_flight(flight: dict[str, Any]) -> None:
        marker = id(flight)
        if marker in seen:
            return
        seen.add(marker)
        cabins = cabin_nodes(flight) or [{}]
        for cabin in cabins:
            candidates.append((flight, cabin))

    def walk(node: Any, *, key_hint: str = "") -> None:
        if isinstance(node, dict):
            if node_looks_like_flight(node):
                add_flight(node)
                return
            for key, value in node.items():
                if key in FLIGHT_LIST_KEYS and isinstance(value, list):
                    for child in value:
                        walk(child, key_hint=key)
                elif key_hint in FLIGHT_LIST_KEYS or key in SEGMENT_KEYS:
                    walk(value, key_hint=key)
                elif isinstance(value, (dict, list)):
                    walk(value, key_hint=key)
        elif isinstance(node, list):
            for child in node:
                walk(child, key_hint=key_hint)

    walk(payload)
    return candidates


def node_looks_like_flight(node: dict[str, Any]) -> bool:
    keys = set(node.keys())
    has_identity = bool(
        keys
        & {
            "flightNo",
            "flightNumber",
            "flightNoShow",
            "flightNoText",
            "flightCode",
            "flightNum",
            "airlineCode",
            "airline",
            "airlineName",
            "carrier",
            "carrierCode",
            "marketingAirlineCode",
            "depAirport",
            "depAirportCode",
            "departureAirport",
            "departureAirportCode",
            "arrAirport",
            "arrAirportCode",
            "arrivalAirport",
            "arrivalAirportCode",
        }
    )
    has_time = bool(
        keys
        & {
            "depTime",
            "departureTime",
            "depDateTime",
            "departureDateTime",
            "takeoffTime",
            "arrTime",
            "arrivalTime",
            "arrDateTime",
            "arrivalDateTime",
            "landingTime",
        }
    )
    has_segments = any(isinstance(node.get(key), list) for key in SEGMENT_KEYS)
    has_price = candidate_price(node, {}) is not None or any(candidate_price({}, cabin) is not None for cabin in cabin_nodes(node))
    return (has_identity and has_time) or (has_segments and has_price)


def cabin_nodes(flight: dict[str, Any]) -> list[dict[str, Any]]:
    cabins: list[dict[str, Any]] = []
    for key in CABIN_KEYS:
        value = flight.get(key)
        if isinstance(value, dict):
            cabins.append(value)
        elif isinstance(value, list):
            cabins.extend(item for item in value if isinstance(item, dict))
    return cabins


def cabin_has_restricted_notice(cabin: dict[str, Any]) -> bool:
    notices = cabin.get("notices")
    if not isinstance(notices, list):
        return False
    text = " ".join(str(item) for item in notices)
    lowered = text.lower()
    return (
        ("限" in text and ("乘客" in text or "年龄" in text))
        or ("restrict" in lowered and ("passenger" in lowered or "age" in lowered))
        or ("闄" in text and "涔樺" in text)
    )


def candidate_price(flight: dict[str, Any], cabin: dict[str, Any]) -> float | None:
    return first_valid_price(cabin, PRICE_KEYS) or first_valid_price(flight, PRICE_KEYS)


def first_valid_price(node: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = coerce_price(node.get(key))
        if value is not None and 100 <= value <= 50000:
            return value
    return None


def flight_detail(flight: dict[str, Any], cabin: dict[str, Any]) -> dict[str, Any]:
    dep_time = str(first_present(flight, "depTime", "departureTime", "depDateTime", "departureDateTime", "takeoffTime") or "")
    arr_time = str(first_present(flight, "arrTime", "arrivalTime", "arrDateTime", "arrivalDateTime", "landingTime") or "")
    return {
        "segment_index": 1,
        "airline": first_present(
            flight,
            "airlineCode",
            "airline",
            "airlineName",
            "carrierCode",
            "carrier",
            "marketingAirlineCode",
        ),
        "flight_no": first_present(flight, "flightNo", "flightNumber", "flightNoShow", "flightNoText", "flightCode", "flightNum"),
        "cabin": first_present(cabin, "specialType", "cabinName", "cabin"),
        "seat_class": first_present(cabin, "cabin", "cabinCode", "seatClass"),
        "departure_time": dep_time.split(" ")[1] if " " in dep_time else dep_time or None,
        "departure_airport": first_present(
            flight,
            "depAirport",
            "depAirportCode",
            "departureAirport",
            "departureAirportCode",
            "depAirportName",
        ),
        "arrival_time": arr_time.split(" ")[1] if " " in arr_time else arr_time or None,
        "arrival_airport": first_present(
            flight,
            "arrAirport",
            "arrAirportCode",
            "arrivalAirport",
            "arrivalAirportCode",
            "arrAirportName",
        ),
        "origin": first_present(
            flight,
            "depCity",
            "depCityCode",
            "departureCity",
            "departureCityCode",
            "depAirport",
            "depAirportCode",
            "departureAirportCode",
        ),
        "destination": first_present(
            flight,
            "arrCity",
            "arrCityCode",
            "arrivalCity",
            "arrivalCityCode",
            "arrAirport",
            "arrAirportCode",
            "arrivalAirportCode",
        ),
        "departure_date": dep_time.split(" ")[0] if " " in dep_time else None,
        "aircraft": first_present(flight, "flightType", "aircraft", "planeType"),
        "stops": first_present(flight, "stop", "stopover"),
    }


def flight_details_from_node(flight: dict[str, Any], cabin: dict[str, Any]) -> list[dict[str, Any]]:
    nested = nested_flight_nodes(flight)
    if nested:
        details: list[dict[str, Any]] = []
        for index, leg in enumerate(nested, start=1):
            leg_cabin = leg.get("cabin") if isinstance(leg.get("cabin"), dict) else cabin
            detail = flight_detail(leg, leg_cabin)
            detail["segment_index"] = index
            details.append(detail)
        return details
    detail = flight_detail(flight, cabin)
    return [detail]


def nested_flight_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    for key in SEGMENT_KEYS:
        value = node.get(key)
        if isinstance(value, list):
            flattened = flatten_flight_nodes(value)
            if len(flattened) >= 2:
                return flattened
    return []


def flatten_flight_nodes(items: list[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if node_looks_like_flight(item) and not any(isinstance(item.get(key), list) for key in SEGMENT_KEYS):
            flattened.append(item)
            continue
        wrapped = first_wrapped_flight(item)
        if wrapped is not None:
            flattened.append(wrapped)
            continue
        flattened.extend(nested_flight_nodes(item))
    return flattened


def first_wrapped_flight(node: dict[str, Any]) -> dict[str, Any] | None:
    for key in FLIGHT_WRAPPER_KEYS:
        value = node.get(key)
        if isinstance(value, dict) and node_looks_like_flight(value):
            return value
    return None


def first_present(node: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_json_or_jsonp(text: str) -> Any | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except Exception:
        pass
    match = JSONP_PATTERN.match(stripped)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def coerce_price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.replace(",", "").strip()
        if not stripped:
            return None
        match = PRICE_PATTERN.search(stripped)
        if match:
            return float(match.group(1))
        try:
            return float(stripped)
        except ValueError:
            return None
    return None
