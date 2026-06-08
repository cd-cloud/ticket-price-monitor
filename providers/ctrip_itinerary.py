"""Helpers for parsing Ctrip itinerary payloads captured from network responses."""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from config_manager import RouteQuery
from providers.ctrip_support import ctrip_city_code


def total_price(price: dict[str, Any]) -> float | None:
    try:
        adult_price = float(price.get("adultPrice", 0) or 0)
        adult_tax = float(price.get("adultTax", 0) or 0)
    except Exception:
        return None
    total = adult_price + adult_tax
    if 200 <= total <= 100000:
        return total
    return None


def multi_city_details_are_direct(details: list[dict[str, object]], route: RouteQuery) -> bool:
    return multi_city_details_match_route(details, route, direct_only=True)


def multi_city_details_match_route(
    details: list[dict[str, object]],
    route: RouteQuery,
    *,
    direct_only: bool = False,
) -> bool:
    segments = route.segments or []
    if not details or not segments:
        return False
    grouped: dict[int, list[dict[str, object]]] = {}
    for item in details:
        segment_index = int(item.get("segment_index", 0) or 0)
        grouped.setdefault(segment_index, []).append(item)
    if len(grouped) != len(segments):
        return False
    for expected_index, segment in enumerate(segments, start=1):
        flights = grouped.get(expected_index, [])
        if not flights or (direct_only and len(flights) != 1):
            return False
        first = flights[0]
        last = flights[-1]
        origin = str(first.get("origin") or first.get("departure_airport") or "")
        destination = str(last.get("destination") or last.get("arrival_airport") or "")
        departure_date = str(first.get("departure_date") or "")[:10]
        if ctrip_city_code(origin) != ctrip_city_code(segment.origin):
            return False
        if ctrip_city_code(destination) != ctrip_city_code(segment.destination):
            return False
        if departure_date != segment.departure_date:
            return False
    return True


def walk_data_nodes(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if isinstance(node.get("flightItineraryList"), list):
            yield node
        for value in node.values():
            yield from walk_data_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_data_nodes(item)


def build_itinerary_details(itinerary: dict[str, Any], price: dict[str, Any]) -> list[dict[str, object]]:
    details = build_multi_city_details(itinerary, price)
    if details:
        return details
    return build_domestic_online_details(itinerary, price)


def build_multi_city_details(itinerary: dict[str, Any], price: dict[str, Any]) -> list[dict[str, object]]:
    seat_map: dict[tuple[int, int], dict[str, object]] = {}
    for unit in price.get("priceUnitList") or []:
        if not isinstance(unit, dict):
            continue
        for seat in unit.get("flightSeatList") or []:
            if not isinstance(seat, dict):
                continue
            key = (int(seat.get("segmentNo", 0) or 0), int(seat.get("sequenceNo", 0) or 0))
            seat_map[key] = seat

    details: list[dict[str, object]] = []
    for flight_info in extract_flight_info_list(price):
        segment_no = int(flight_info.get("SegmentNo", 0) or 0)
        sequence_no = int(flight_info.get("SequenceNo", 0) or 0)
        seat = seat_map.get((segment_no, sequence_no), {})
        details.append(
            {
                "segment_index": segment_no,
                "airline": flight_info.get("MarketingCarrierCode"),
                "flight_no": flight_info.get("MarketingFlightNo"),
                "cabin": normalize_cabin_from_seat(seat),
                "seat_class": seat.get("seatClass"),
                "departure_time": format_ctrip_dt(flight_info.get("TakeOffDateTime")),
                "departure_airport": flight_info.get("DepartureAirportCode"),
                "arrival_time": format_ctrip_dt(flight_info.get("ArrivalDateTime")),
                "arrival_airport": flight_info.get("ArrivalAirportCode"),
                "origin": flight_info.get("DepartureCityCode"),
                "destination": flight_info.get("ArrivalCityCode"),
                "departure_date": str(flight_info.get("TakeOffDateTime", "")).split(" ")[0] or None,
            }
        )
    return details


def build_domestic_online_details(itinerary: dict[str, Any], price: dict[str, Any]) -> list[dict[str, object]]:
    seat_map: dict[tuple[int, int], dict[str, object]] = {}
    for unit in price.get("priceUnitList") or []:
        if not isinstance(unit, dict):
            continue
        for seat in unit.get("flightSeatList") or []:
            if not isinstance(seat, dict):
                continue
            key = (int(seat.get("segmentNo", 0) or 0), int(seat.get("sequenceNo", 0) or 0))
            seat_map[key] = seat

    details: list[dict[str, object]] = []
    for segment in itinerary.get("flightSegments") or []:
        if not isinstance(segment, dict):
            continue
        segment_no = int(segment.get("segmentNo", 0) or segment.get("segmentIndex", 0) or 1)
        for flight in segment.get("flightList") or []:
            if not isinstance(flight, dict):
                continue
            sequence_no = int(flight.get("sequenceNo", 0) or len(details) + 1)
            seat = seat_map.get((segment_no, sequence_no), {})
            departure_time = (
                flight.get("departureDateTime")
                or flight.get("takeOffDateTime")
                or flight.get("TakeOffDateTime")
            )
            arrival_time = flight.get("arrivalDateTime") or flight.get("ArrivalDateTime")
            details.append(
                {
                    "segment_index": segment_no,
                    "airline": flight.get("marketAirlineCode") or flight.get("airlineCode"),
                    "flight_no": flight.get("flightNo") or flight.get("marketFlightNo"),
                    "cabin": normalize_cabin_from_seat(seat) or normalize_cabin_code(price.get("cabin")),
                    "seat_class": seat.get("seatClass"),
                    "departure_time": format_ctrip_dt(departure_time),
                    "departure_airport": flight.get("departureAirportCode"),
                    "arrival_time": format_ctrip_dt(arrival_time),
                    "arrival_airport": flight.get("arrivalAirportCode"),
                    "origin": flight.get("departureCityCode") or flight.get("departureAirportCode"),
                    "destination": flight.get("arrivalCityCode") or flight.get("arrivalAirportCode"),
                    "departure_date": str(departure_time or "").split(" ")[0] or None,
                }
            )
    return details


def extract_flight_info_list(price: dict[str, Any]) -> list[dict[str, Any]]:
    luggage_visa_key = price.get("luggageVisaKey")
    if not isinstance(luggage_visa_key, str) or not luggage_visa_key.strip():
        return []
    try:
        payload = json.loads(luggage_visa_key)
    except Exception:
        return []
    criteria = payload.get("criteria")
    if not isinstance(criteria, dict):
        return []
    items = criteria.get("FlightInfoList")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def normalize_cabin_from_seat(seat: dict[str, object]) -> str | None:
    cabin = str(seat.get("cabinClass", "") or "").upper()
    mapping = {
        "Y": "economy",
        "C": "business",
        "J": "business",
        "F": "first",
        "W": "premium_economy",
        "S": "premium_economy",
    }
    return mapping.get(cabin)


def normalize_cabin_code(value: object) -> str | None:
    text = str(value or "").upper()
    if "F" in text:
        return "first"
    if "C" in text or "J" in text:
        return "business"
    if "W" in text or "S" in text:
        return "premium_economy"
    if "Y" in text:
        return "economy"
    return None


def format_ctrip_dt(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(" ")
    if len(parts) == 2:
        return parts[1][:5]
    return text[:5] or None


def coerce_price(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.replace(",", "").strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return float(text)
    return None
