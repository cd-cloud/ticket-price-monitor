"""Expand multi-city input groups into concrete RouteQuery objects."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from typing import Any

from core.config_manager import RouteQuery, RouteSegment, normalize_airport_code, normalize_transfer_policy


CITY_SPLIT_PATTERN = re.compile(r"[/,，、;\s]+")
MAX_CITY_OPTIONS = 50
MAX_MULTI_CITY_SEGMENTS = 6
MAX_EXPANDED_ROUTES = 100
MAX_GROUP_LABEL_LENGTH = 120
MAX_RAW_CITY_TEXT_LENGTH = 4096
MAX_RAW_CITY_ITEM_LENGTH = 256
MAX_RAW_PROVIDER_OPTIONS = 10
MAX_RAW_AIRLINE_OPTIONS = 20
MAX_GROUP_ID_LENGTH = 80
MAX_RAW_PAYLOAD_STRING_LENGTH = 8192
MAX_RAW_PAYLOAD_NODES = 10000
MAX_RAW_PAYLOAD_DEPTH = 8
MAX_RAW_PAYLOAD_KEYS = 500


def _validate_payload_shape(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if depth > MAX_RAW_PAYLOAD_DEPTH:
        raise ValueError("Route payload is nested too deeply.")
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_RAW_PAYLOAD_NODES:
        raise ValueError("Route payload contains too many values.")
    if isinstance(value, str):
        if len(value) > MAX_RAW_PAYLOAD_STRING_LENGTH:
            raise ValueError("Route payload contains an oversized string.")
        return
    if isinstance(value, dict):
        if len(value) > MAX_RAW_PAYLOAD_KEYS:
            raise ValueError("Route payload contains too many fields.")
        for key, item in value.items():
            if len(str(key)) > MAX_RAW_PAYLOAD_STRING_LENGTH:
                raise ValueError("Route payload contains an oversized field name.")
            _validate_payload_shape(item, depth=depth + 1, counter=counter)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_RAW_PAYLOAD_NODES:
            raise ValueError("Route payload contains too many items.")
        for item in value:
            _validate_payload_shape(item, depth=depth + 1, counter=counter)


def _raw_city_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        if len(value) > MAX_CITY_OPTIONS:
            raise ValueError(f"At most {MAX_CITY_OPTIONS} city options are allowed per field.")
        raw_items = value
    else:
        text = str(value or "")
        if len(text) > MAX_RAW_CITY_TEXT_LENGTH:
            raise ValueError("City option input is too large.")
        raw_items = CITY_SPLIT_PATTERN.split(text)
        if len(raw_items) > MAX_CITY_OPTIONS:
            raise ValueError(f"At most {MAX_CITY_OPTIONS} city options are allowed per field.")
    for item in raw_items:
        if len(str(item or "")) > MAX_RAW_CITY_ITEM_LENGTH:
            raise ValueError("Each city option is too long.")
    return raw_items


def parse_city_list(value: Any) -> list[str]:
    codes = [
        normalize_airport_code(item)
        for item in _raw_city_items(value)
        if str(item or "").strip()
    ]
    unique = list(dict.fromkeys([code for code in codes if code]))
    if len(unique) > MAX_CITY_OPTIONS:
        raise ValueError(f"At most {MAX_CITY_OPTIONS} city options are allowed per field.")
    return unique


def _raw_segments(payload: dict[str, Any]) -> list[Any]:
    segments = payload.get("segments") or []
    if not isinstance(segments, list):
        raise ValueError("segments must be a list.")
    if len(segments) > MAX_MULTI_CITY_SEGMENTS:
        raise ValueError(
            f"Multi-city routes support at most {MAX_MULTI_CITY_SEGMENTS} segments."
        )
    return segments


def validate_raw_payload_bounds(payload: dict[str, Any]) -> None:
    """Reject oversized raw collections before normalization or payload hashing."""
    if not isinstance(payload, dict):
        raise ValueError("Route payload must be an object.")
    for field, maximum in (("group_id", MAX_GROUP_ID_LENGTH), ("group_label", MAX_GROUP_LABEL_LENGTH), ("label", MAX_GROUP_LABEL_LENGTH)):
        value = payload.get(field)
        if isinstance(value, str) and len(value) > maximum:
            raise ValueError(f"{field} must be at most {maximum} characters.")
    _bounded_list(
        payload.get("providers"),
        maximum=MAX_RAW_PROVIDER_OPTIONS,
        label="provider",
    )
    _bounded_list(
        payload.get("preferred_airlines"),
        maximum=MAX_RAW_AIRLINE_OPTIONS,
        label="preferred airline",
    )
    _validate_payload_shape(payload)
    segments = _raw_segments(payload)
    for field in ("origin_options", "destination_options", "origin", "destination"):
        if field in payload:
            parse_city_list(payload.get(field))
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Each multi-city segment must be an object.")
        for field in ("origin_options", "destination_options", "origin", "destination"):
            if field in segment:
                parse_city_list(segment.get(field))


def payload_has_city_groups(payload: dict[str, Any]) -> bool:
    validate_raw_payload_bounds(payload)
    if payload.get("route_batch"):
        return True
    origin_value = payload.get("origin_options") if "origin_options" in payload else payload.get("origin")
    destination_value = (
        payload.get("destination_options")
        if "destination_options" in payload
        else payload.get("destination")
    )
    origins = parse_city_list(origin_value)
    destinations = parse_city_list(destination_value)
    if "origin_options" in payload or "destination_options" in payload:
        return True
    segments = _raw_segments(payload)
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if "origin_options" in segment or "destination_options" in segment:
            parse_city_list(
                segment.get("origin_options")
                if "origin_options" in segment
                else segment.get("origin")
            )
            parse_city_list(
                segment.get("destination_options")
                if "destination_options" in segment
                else segment.get("destination")
            )
            return True
    if len(origins) > 1 or len(destinations) > 1:
        return True
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if len(parse_city_list(segment.get("origin"))) > 1:
            return True
        if len(parse_city_list(segment.get("destination"))) > 1:
            return True
    return False


def expand_route_payload(payload: dict[str, Any], *, auto_query_interval_hours: int) -> list[RouteQuery]:
    validate_raw_payload_bounds(payload)
    route_type = str(payload.get("route_type") or "one_way")
    group_label = str(payload.get("group_label") or "").strip() or _default_group_label(payload)
    if len(group_label) > MAX_GROUP_LABEL_LENGTH:
        raise ValueError(f"Route group label must be at most {MAX_GROUP_LABEL_LENGTH} characters.")
    group_id = str(payload.get("group_id") or "").strip() or _default_group_id(payload)

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
    if len(origins) * len(destinations) > MAX_EXPANDED_ROUTES:
        raise ValueError(f"Route group cannot expand beyond {MAX_EXPANDED_ROUTES} routes.")
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
    segments_payload = _raw_segments(payload)
    segment_options: list[list[RouteSegment]] = []
    for item in segments_payload:
        if not isinstance(item, dict):
            raise ValueError("Each multi-city segment must be an object.")
        origins = parse_city_list(item.get("origin_options") or item.get("origin"))
        destinations = parse_city_list(item.get("destination_options") or item.get("destination"))
        if len(origins) * len(destinations) > MAX_EXPANDED_ROUTES:
            raise ValueError(
                f"Each multi-city segment cannot expand beyond {MAX_EXPANDED_ROUTES} options."
            )
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
    if expansion_mode not in {"paired", "cartesian"}:
        raise ValueError("expansion_mode must be paired or cartesian.")
    if expansion_mode == "paired":
        routes.extend(_paired_multi_city_routes(payload, segment_options, group_id, group_label, auto_query_interval_hours))
    else:
        route_count = 1
        for options in segment_options:
            route_count *= len(options)
            if route_count > MAX_EXPANDED_ROUTES:
                raise ValueError(f"Route group cannot expand beyond {MAX_EXPANDED_ROUTES} routes.")
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
    if count > MAX_EXPANDED_ROUTES:
        raise ValueError(f"Route group cannot expand beyond {MAX_EXPANDED_ROUTES} routes.")
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
        target_price=_parse_optional_target_price(payload.get("target_price")),
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
        target_price=_parse_optional_target_price(payload.get("target_price")),
    )


def _parse_optional_target_price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("target_price must be a positive number.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_price must be a positive number.") from exc
    if not (0 < numeric <= 1000000):
        raise ValueError("target_price must be between 1 and 1000000.")
    return round(numeric, 2)


def _bounded_list(value: Any, *, maximum: int, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    if len(value) > maximum:
        raise ValueError(f"At most {maximum} {label} items are allowed.")
    if any(len(str(item or "")) > MAX_RAW_CITY_ITEM_LENGTH for item in value):
        raise ValueError(f"Each {label} item is too long.")
    return value


def _string_list(value: Any) -> list[str] | None:
    items = [
        str(item).strip()
        for item in _bounded_list(
            value, maximum=MAX_RAW_PROVIDER_OPTIONS, label="provider"
        )
        if str(item).strip()
    ]
    return items or None


def _upper_list(value: Any) -> list[str] | None:
    items = [
        str(item).strip().upper()
        for item in _bounded_list(
            value, maximum=MAX_RAW_AIRLINE_OPTIONS, label="preferred airline"
        )
        if str(item).strip()
    ]
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
