import json
import re
from datetime import date, datetime
from typing import Any

from backends import backend_registry_payload
from providers.registry import provider_definition, provider_registry_payload


CABIN_OPTIONS = [
    "economy_plus",
    "business_first",
    "economy",
    "premium_economy",
    "business",
    "first",
]


def build_dashboard_payload(
    *,
    config,
    routes: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    report_paths: dict[str, str],
    last_run_summary: dict[str, Any],
    auto_query_interval_hours: int,
    auto_query_min_gap_minutes: int,
    session_hints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    latest_by_pair, previous_by_pair = build_snapshot_history_maps(snapshots)

    route_errors = summarize_route_errors(last_run_summary.get("errors", []))
    tasks = []
    for route in config.routes:
        providers = usable_provider_names(config, route.providers or list(config.providers.keys()))
        expired = route_is_expired(route)
        for provider_name in providers:
            key = f"{provider_name}::{route.route_key}"
            latest = latest_by_pair.get(key)
            previous = previous_by_pair.get(key)
            last_error = route_errors.get(route.route_key)
            display_latest = None if last_error else latest
            display_previous = None if last_error else previous
            raw_payload = parse_json_field(display_latest.get("raw_payload")) if display_latest else {}
            best_offer = raw_payload.get("best_offer", {}) if isinstance(raw_payload, dict) else {}
            offer_summary = extract_offer_summary(best_offer.get("row_text"))
            segments = raw_payload.get("segments", []) if isinstance(raw_payload, dict) else []
            if not segments:
                segments = [segment.to_dict() for segment in route.segments or []]
            network_flight_details = raw_payload.get("flight_details", []) if isinstance(raw_payload, dict) else []
            flight_details = (
                normalize_network_flight_details(network_flight_details, default_cabin=route.cabin)
                if isinstance(network_flight_details, list) and network_flight_details
                else extract_flight_details(
                    best_offer.get("row_text"),
                    segments=segments,
                    cabin=route.cabin,
                    origin=route.origin,
                    destination=route.destination,
                    departure_date=route.departure_date,
                )
            )
            tasks.append(
                {
                    "provider": provider_name,
                    "route_key": route.route_key,
                    "route_type": route.route_type,
                    "route_display": route.display_label,
                    "origin": route.origin,
                    "destination": route.destination,
                    "departure_date": route.departure_date,
                    "return_date": route.return_date,
                    "cabin": route.cabin,
                    "transfer_policy": route.transfer_policy,
                    "passengers": route.passengers,
                    "preferred_airlines": route.preferred_airlines or [],
                    "latest_price": display_latest["price"] if display_latest else None,
                    "previous_price": display_previous["price"] if display_previous else None,
                    "latest_price_delta_amount": price_delta_amount(display_latest, display_previous),
                    "latest_price_delta_direction": price_delta_direction(display_latest, display_previous),
                    "stale_latest_price": latest["price"] if last_error and latest else None,
                    "currency": display_latest["currency"] if display_latest else config.default_currency,
                    "last_query_time": display_latest["observed_at"] if display_latest else None,
                    "last_saved_time": display_latest["scraped_at"] if display_latest else None,
                    "last_error": last_error,
                    "status": build_task_status(display_latest, route.auto_query_enabled, last_error, expired),
                    "is_expired": expired,
                    "lowest_departure_time": best_offer.get("departure_time"),
                    "lowest_flight_no": extract_flight_number(best_offer.get("row_text")),
                    "lowest_departure_airport": offer_summary.get("departure_airport"),
                    "lowest_arrival_time": offer_summary.get("arrival_time"),
                    "lowest_arrival_airport": offer_summary.get("arrival_airport"),
                    "lowest_flight_details": flight_details,
                    "itinerary_summary": raw_payload.get("itinerary_summary"),
                    "segment_count": raw_payload.get("segment_count"),
                    "multi_city_segments": segments,
                    "auto_query_enabled": route.auto_query_enabled,
                    "auto_query_interval_hours": route.auto_query_interval_hours,
                    "auto_query_next_run_at": route.auto_query_next_run_at,
                    "group_id": route.group_id,
                    "group_label": route.group_label,
                }
            )

    task_groups = build_task_groups(tasks)
    run_feedback = build_run_feedback(
        latest_by_pair=latest_by_pair,
        previous_by_pair=previous_by_pair,
        last_run_summary=last_run_summary,
    )
    return {
        "tasks": tasks,
        "task_groups": task_groups,
        "routes": routes,
        "provider_options": [
            name
            for name, provider in config.providers.items()
            if getattr(provider, "enabled", False) and provider_has_handler(name)
        ],
        "browser_backend_registry": backend_registry_payload(),
        "provider_registry": provider_registry_payload(),
        "cabin_options": CABIN_OPTIONS,
        "snapshots": snapshots,
        "last_run": last_run_summary,
        "run_feedback": run_feedback,
        "report_paths": report_paths,
        "auto_query_policy": {
            "interval_hours": auto_query_interval_hours,
            "min_gap_minutes": auto_query_min_gap_minutes,
        },
        "session_hints": session_hints or [],
    }


def build_snapshot_history_maps(
    snapshots: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    latest_by_pair: dict[str, dict[str, Any]] = {}
    previous_by_pair: dict[str, dict[str, Any]] = {}
    for row in snapshots:
        key = f"{row['provider']}::{row['route_key']}"
        existing = latest_by_pair.get(key)
        if existing is None:
            latest_by_pair[key] = row
            continue
        existing_valid = snapshot_looks_valid(existing)
        current_valid = snapshot_looks_valid(row)
        if current_valid or not existing_valid:
            previous_by_pair[key] = existing
            latest_by_pair[key] = row
        elif key not in previous_by_pair:
            previous_by_pair[key] = row
    return latest_by_pair, previous_by_pair


def build_run_feedback(
    *,
    latest_by_pair: dict[str, dict[str, Any]],
    previous_by_pair: dict[str, dict[str, Any]],
    last_run_summary: dict[str, Any],
) -> dict[str, Any]:
    saved_pairs = [item for item in (last_run_summary.get("saved_pairs") or []) if isinstance(item, dict)]
    changes: list[dict[str, Any]] = []
    for item in saved_pairs:
        key = f"{item.get('provider')}::{item.get('route_key')}"
        latest = latest_by_pair.get(key)
        previous = previous_by_pair.get(key)
        latest_price = safe_float((latest or {}).get("price"))
        previous_price = safe_float((previous or {}).get("price"))
        direction = "new"
        amount = None
        if latest_price is not None and previous_price is not None:
            amount = round(latest_price - previous_price, 2)
            if amount < 0:
                direction = "down"
            elif amount > 0:
                direction = "up"
            else:
                direction = "same"
        changes.append(
            {
                "provider": item.get("provider"),
                "route_key": item.get("route_key"),
                "latest_price": latest_price,
                "previous_price": previous_price,
                "currency": (latest or {}).get("currency") or "CNY",
                "direction": direction,
                "delta_amount": amount,
                "observed_at": (latest or item).get("observed_at"),
            }
        )
    counts = {
        "down": sum(1 for item in changes if item["direction"] == "down"),
        "up": sum(1 for item in changes if item["direction"] == "up"),
        "same": sum(1 for item in changes if item["direction"] == "same"),
        "new": sum(1 for item in changes if item["direction"] == "new"),
    }
    top_down = sorted(
        [item for item in changes if item["direction"] == "down" and item["delta_amount"] is not None],
        key=lambda item: item["delta_amount"],
    )[:5]
    top_up = sorted(
        [item for item in changes if item["direction"] == "up" and item["delta_amount"] is not None],
        key=lambda item: item["delta_amount"],
        reverse=True,
    )[:5]
    return {
        "progress": {
            "running": bool(last_run_summary.get("running")),
            "saved": int(last_run_summary.get("saved", 0) or 0),
            "errors": list(last_run_summary.get("errors") or []),
            "completed_targets": int(last_run_summary.get("completed_targets", 0) or 0),
            "total_targets": int(last_run_summary.get("total_targets", 0) or 0),
            "current_target": last_run_summary.get("current_target"),
            "started_at": last_run_summary.get("started_at"),
            "finished_at": last_run_summary.get("finished_at"),
            "recent_successes": list(last_run_summary.get("recent_successes") or []),
            "recent_failures": list(last_run_summary.get("recent_failures") or []),
        },
        "changes": {
            "total_saved_pairs": len(saved_pairs),
            "counts": counts,
            "top_down": top_down,
            "top_up": top_up,
            "items": changes,
        },
    }


def usable_provider_names(config: Any, provider_names: list[str]) -> list[str]:
    return [
        provider_name
        for provider_name in provider_names
        if config.providers.get(provider_name)
        and config.providers[provider_name].enabled
        and provider_has_handler(provider_name)
    ]


def provider_has_handler(provider_name: str) -> bool:
    definition = provider_definition(provider_name)
    return bool(definition and definition.has_handler)


def route_is_expired(route: Any) -> bool:
    completion = route_completion_date(route)
    return bool(completion and completion < date.today())


def route_completion_date(route: Any) -> date | None:
    candidates: list[str] = []
    if getattr(route, "return_date", None):
        candidates.append(str(route.return_date))
    if getattr(route, "segments", None):
        candidates.extend(str(segment.departure_date) for segment in route.segments or [])
    if getattr(route, "departure_date", None):
        candidates.append(str(route.departure_date))
    parsed: list[date] = []
    for value in candidates:
        try:
            parsed.append(datetime.strptime(value, "%Y-%m-%d").date())
        except ValueError:
            continue
    return max(parsed) if parsed else None


def build_task_groups(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for task in tasks:
        group_id = task.get("group_id") or task.get("route_key")
        group = groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "group_label": task.get("group_label") or task.get("route_display") or group_id,
                "tasks": [],
            },
        )
        group["tasks"].append(task)

    payload: list[dict[str, Any]] = []
    for group in groups.values():
        group_tasks = group["tasks"]
        priced = [task for task in group_tasks if task.get("latest_price")]
        lowest = min(priced, key=lambda item: item["latest_price"]) if priced else None
        errors = [task for task in group_tasks if task.get("last_error")]
        origins = sorted({task.get("origin") for task in group_tasks if task.get("origin")})
        destinations = sorted({task.get("destination") for task in group_tasks if task.get("destination")})
        payload.append(
            {
                "group_id": group["group_id"],
                "group_label": group["group_label"],
                "route_count": len(group_tasks),
                "priced_count": len(priced),
                "error_count": len(errors),
                "origins": origins,
                "destinations": destinations,
                "lowest_price": lowest.get("latest_price") if lowest else None,
                "lowest_currency": lowest.get("currency") if lowest else None,
                "lowest_route_display": lowest.get("route_display") if lowest else None,
                "tasks": group_tasks,
            }
        )
    return payload


def parse_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except Exception:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def price_delta_amount(latest: dict[str, Any] | None, previous: dict[str, Any] | None) -> float | None:
    latest_price = safe_float((latest or {}).get("price"))
    previous_price = safe_float((previous or {}).get("price"))
    if latest_price is None or previous_price is None:
        return None
    return round(latest_price - previous_price, 2)


def price_delta_direction(latest: dict[str, Any] | None, previous: dict[str, Any] | None) -> str | None:
    if not latest:
        return None
    delta = price_delta_amount(latest, previous)
    if delta is None:
        return "new" if previous is None else None
    if delta < 0:
        return "down"
    if delta > 0:
        return "up"
    return "same"


def normalize_network_flight_details(details: list[Any], *, default_cabin: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(details, start=1):
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "segment_index": item.get("segment_index") or index,
                "origin": item.get("origin"),
                "destination": item.get("destination"),
                "departure_date": item.get("departure_date"),
                "airline": item.get("airline"),
                "flight_no": item.get("flight_no"),
                "cabin": item.get("cabin") or default_cabin,
                "seat_class": item.get("seat_class"),
                "departure_time": item.get("departure_time"),
                "departure_airport": item.get("departure_airport"),
                "arrival_time": item.get("arrival_time"),
                "arrival_airport": item.get("arrival_airport"),
                "aircraft": item.get("aircraft"),
            }
        )
    return normalized


def snapshot_looks_valid(snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return False
    payload = parse_json_field(snapshot.get("raw_payload"))
    search_url = str(payload.get("search_url", "")).lower()
    title = str(payload.get("title", ""))
    if "/booking/" in search_url or "/online/list/" in search_url:
        return True
    if "飞机票查询" in title and "机票预订" in title:
        return False
    return bool(snapshot.get("price"))


def summarize_route_errors(errors: Any) -> dict[str, str]:
    if not isinstance(errors, list):
        return {}
    by_route: dict[str, str] = {}
    for error in errors:
        text = str(error)
        match = re.search(r"route=([^ ]+) failed: (.+)", text)
        if match:
            by_route[match.group(1)] = match.group(2)
    return by_route


def build_task_status(
    latest: dict[str, Any] | None,
    auto_enabled: bool,
    last_error: str | None,
    expired: bool = False,
) -> str:
    if expired:
        return "expired"
    if last_error:
        return "error"
    if latest:
        return "ok"
    if auto_enabled:
        return "scheduled"
    return "idle"


def extract_flight_number(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    normalized = text.replace("\u00a0", " ").replace("\u806a", " ")
    match = re.search(r"\b([A-Z][A-Z0-9]\d{3,5})\b", normalized)
    if match:
        return match.group(1)
    return None


def extract_offer_summary(text: Any) -> dict[str, str | None]:
    if not isinstance(text, str) or not text.strip():
        return {
            "departure_airport": None,
            "arrival_time": None,
            "arrival_airport": None,
        }

    normalized = text.replace("\u00a0", " ").replace("\u806a", " ")
    parts = [part.strip() for part in normalized.split("|") if part.strip()]
    time_indices = [index for index, part in enumerate(parts) if re.fullmatch(r"\d{1,2}:\d{2}", part)]

    departure_airport = None
    arrival_time = None
    arrival_airport = None
    if len(time_indices) >= 2:
        departure_index = time_indices[0]
        arrival_index = time_indices[1]
        if departure_index + 1 < len(parts):
            departure_airport = parts[departure_index + 1]
        arrival_time = parts[arrival_index]
        if arrival_index + 1 < len(parts):
            arrival_airport = parts[arrival_index + 1]

    return {
        "departure_airport": departure_airport,
        "arrival_time": arrival_time,
        "arrival_airport": arrival_airport,
    }


def extract_flight_details(
    text: Any,
    *,
    segments: list[dict[str, Any]],
    cabin: str,
    origin: str | None = None,
    destination: str | None = None,
    departure_date: str | None = None,
) -> list[dict[str, Any]]:
    parsed = parse_flight_details_from_text(text, cabin=cabin)
    if parsed:
        for index, detail in enumerate(parsed):
            if index < len(segments):
                detail.setdefault("origin", segments[index].get("origin"))
                detail.setdefault("destination", segments[index].get("destination"))
                detail.setdefault("departure_date", segments[index].get("departure_date"))
            else:
                detail.setdefault("origin", origin)
                detail.setdefault("destination", destination)
                detail.setdefault("departure_date", departure_date)
        return parsed

    return [
        {
            "segment_index": index + 1,
            "origin": segment.get("origin"),
            "destination": segment.get("destination"),
            "departure_date": segment.get("departure_date"),
            "airline": None,
            "flight_no": None,
            "cabin": cabin,
            "departure_time": None,
            "departure_airport": None,
            "arrival_time": None,
            "arrival_airport": None,
            "aircraft": None,
        }
        for index, segment in enumerate(segments)
    ]


def parse_flight_details_from_text(text: Any, *, cabin: str) -> list[dict[str, Any]]:
    if not isinstance(text, str) or not text.strip():
        return []

    normalized = text.replace("\u00a0", " ").replace("\u806a", " ")
    parts = [part.strip() for part in normalized.split("|") if part.strip()]
    flight_positions: list[tuple[int, re.Match[str]]] = []
    for index, part in enumerate(parts):
        match = re.search(r"\b([A-Z][A-Z0-9]\d{3,5})\b", part)
        if match:
            flight_positions.append((index, match))

    details: list[dict[str, Any]] = []
    for segment_index, (part_index, match) in enumerate(flight_positions, start=1):
        part = parts[part_index]
        flight_no = match.group(1)
        airline = parts[part_index - 1] if part_index > 0 else None
        aircraft = part.replace(flight_no, "").strip(" -/") or None
        time_indices = [
            index
            for index in range(part_index + 1, len(parts))
            if re.fullmatch(r"\d{1,2}:\d{2}", parts[index])
        ]

        departure_index = time_indices[0] if time_indices else None
        arrival_index = time_indices[1] if len(time_indices) > 1 else None
        details.append(
            {
                "segment_index": segment_index,
                "airline": airline,
                "flight_no": flight_no,
                "cabin": cabin,
                "departure_time": parts[departure_index] if departure_index is not None else None,
                "departure_airport": parts[departure_index + 1] if departure_index is not None and departure_index + 1 < len(parts) else None,
                "arrival_time": parts[arrival_index] if arrival_index is not None else None,
                "arrival_airport": parts[arrival_index + 1] if arrival_index is not None and arrival_index + 1 < len(parts) else None,
                "aircraft": aircraft,
            }
        )

    return details
