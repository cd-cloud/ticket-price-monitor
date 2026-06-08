"""Expand multi-city input groups into concrete RouteQuery objects."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from typing import Any

from config_manager import RouteQuery, RouteSegment, normalize_airport_code, normalize_transfer_policy


CITY_SPLIT_PATTERN = re.compile(r"[/,，、;\s]+")


def parse_city_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = CITY_SPLIT_PATTERN.split(str(value or ""))
    codes = [
        normalize_airport_code(item)
        for item in raw_items
        if str(item or "").strip()
    ]
    return list(dict.fromkeys([code for code in codes if code]))


def payload_has_city_groups(payload: dict[str, Any]) -> bool:
    if payload.get("route_batch"):
        return True
    if "origin_options" in payload or "destination_options" in payload:
        return True
    for segment in payload.get("segments") or []:
        if isinstance(segment, dict) and ("origin_options" in segment or "destination_options" in segment):
            return True
    if len(parse_city_list(payload.get("origin_options") or payload.get("origin"))) > 1:
        return True
    if len(parse_city_list(payload.get("destination_options") or payload.get("destination"))) > 1:
        return True
    for segment in payload.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        if len(parse_city_list(segment.get("origin_options") or segment.get("origin"))) > 1:
            return True
        if len(parse_city_list(segment.get("destination_options") or segment.get("destination"))) > 1:
            return True
    return False


def expand_route_payload(payload: dict[str, Any], *, auto_query_interval_hours: int) -> list[RouteQuery]:
    route_type = str(payload.get("route_type") or "one_way")
    group_id = str(payload.get("group_id") or "").strip() or _default_group_id(payload)
    group_label = str(payload.get("group_label") or "").strip() or _default_group_label(payload)

    if route_type == "multi_city":
        routes = _expand_multi_city(payload, group_id, group_label, auto_query_interval_hours)
    else:
        routes = _expand_single_or_return(payload, group_id, group_label, auto_query_interval_hours)

    return routes


def _expand_single_or_return(
    payload: dict[str, Any],
    group_id: str,
    group_label: str,
    auto_query_interval_hours: int,
) -> list[RouteQuery]:
    origins = parse_city_list(payload.get("origin_options") or payload.get("origin"))
    destinations = parse_city_list(payload.get("destination_options") or payload.get("destination"))
    if not origins:
        raise ValueError("Route group requires at least one origin.")
    if not destinations:
        raise ValueError("Route group requires at least one destination.")
    routes: list[RouteQuery] = []
    for origin, destination in itertools.product(origins, destinations):
        routes.append(_base_route(payload, group_id, group_label, auto_query_interval_hours, origin, destination))
    return routes


def _expand_multi_city(
    payload: dict[str, Any],
    group_id: str,
    group_label: str,
    auto_query_interval_hours: int,
) -> list[RouteQuery]:
    segment_options: list[list[RouteSegment]] = []
    for item in payload.get("segments") or []:
        if not isinstance(item, dict):
            continue
        origins = parse_city_list(item.get("origin_options") or item.get("origin"))
        destinations = parse_city_list(item.get("destination_options") or item.get("destination"))
        departure_date = str(item.get("departure_date") or "")
        options = [
            RouteSegment(origin=origin, destination=destination, departure_date=departure_date)
            for origin, destination in itertools.product(origins, destinations)
        ]
        if not options:
            raise ValueError("Each multi-city segment needs at least one origin and destination.")
        segment_options.append(options)
    if len(segment_options) < 2:
        raise ValueError("multi_city route groups require at least two complete segments.")

    routes: list[RouteQuery] = []
    expansion_mode = str(payload.get("expansion_mode") or "cartesian")
    if expansion_mode == "paired":
        routes.extend(_paired_multi_city_routes(payload, segment_options, group_id, group_label, auto_query_interval_hours))
    else:
        for segments in itertools.product(*segment_options):
            routes.append(_multi_city_route(payload, list(segments), group_id, group_label, auto_query_interval_hours))
    return routes


def _paired_multi_city_routes(
    payload: dict[str, Any],
    segment_options: list[list[RouteSegment]],
    group_id: str,
    group_label: str,
    auto_query_interval_hours: int,
) -> list[RouteQuery]:
    lengths = {len(options) for options in segment_options if len(options) > 1}
    if len(lengths) > 1:
        raise ValueError("Paired expansion requires multi-option segments to have the same option count.")
    count = next(iter(lengths), 1)
    routes: list[RouteQuery] = []
    for index in range(count):
        segments = [
            options[index] if len(options) > 1 else options[0]
            for options in segment_options
        ]
        routes.append(_multi_city_route(payload, segments, group_id, group_label, auto_query_interval_hours))
    return routes


def _base_route(
    payload: dict[str, Any],
    group_id: str,
    group_label: str,
    auto_query_interval_hours: int,
    origin: str,
    destination: str,
) -> RouteQuery:
    return RouteQuery(
        origin=origin,
        destination=destination,
        departure_date=str(payload["departure_date"]),
        return_date=str(payload.get("return_date") or "").strip() or None,
        route_type="one_way",
        cabin=str(payload.get("cabin", "economy")).lower(),
        transfer_policy=normalize_transfer_policy(payload.get("transfer_policy", "any")),
        passengers=int(payload.get("passengers", 1)),
        providers=_string_list(payload.get("providers")),
        preferred_airlines=_upper_list(payload.get("preferred_airlines")),
        segments=None,
        auto_query_enabled=bool(payload.get("auto_query_enabled", False)),
        auto_query_interval_hours=auto_query_interval_hours,
        auto_query_next_run_at=str(payload.get("auto_query_next_run_at") or "").strip() or None,
        group_id=group_id,
        group_label=group_label,
    )


def _multi_city_route(
    payload: dict[str, Any],
    segments: list[RouteSegment],
    group_id: str,
    group_label: str,
    auto_query_interval_hours: int,
) -> RouteQuery:
    return RouteQuery(
        origin=segments[0].origin,
        destination=segments[-1].destination,
        departure_date=segments[0].departure_date,
        return_date=segments[-1].departure_date if len(segments) > 1 else None,
        route_type="multi_city",
        cabin=str(payload.get("cabin", "economy")).lower(),
        transfer_policy=normalize_transfer_policy(payload.get("transfer_policy", "any")),
        passengers=int(payload.get("passengers", 1)),
        providers=_string_list(payload.get("providers")),
        preferred_airlines=_upper_list(payload.get("preferred_airlines")),
        segments=segments,
        auto_query_enabled=bool(payload.get("auto_query_enabled", False)),
        auto_query_interval_hours=auto_query_interval_hours,
        auto_query_next_run_at=str(payload.get("auto_query_next_run_at") or "").strip() or None,
        group_id=group_id,
        group_label=group_label,
    )


def _string_list(value: Any) -> list[str] | None:
    items = [str(item).strip() for item in value or [] if str(item).strip()]
    return items or None


def _upper_list(value: Any) -> list[str] | None:
    items = [str(item).strip().upper() for item in value or [] if str(item).strip()]
    return items or None


def _default_group_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "rg_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _default_group_label(payload: dict[str, Any]) -> str:
    text = str(payload.get("label") or "").strip()
    if text:
        return text
    if str(payload.get("route_type") or "one_way") == "multi_city":
        return "多程组合查询"
    return "多城市查询"
