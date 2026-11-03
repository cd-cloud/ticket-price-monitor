from __future__ import annotations

import sys
import threading
import asyncio
import math
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from config_manager import ConfigManager, RouteQuery
from data_storage import PriceRepository
from providers.airchina_page import build_search_url
from query_service import QueryService
from providers.airchina_source_contract import CABIN_NAMES, CAPABILITIES

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "flyclaw"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from airport_manager import airport_manager


def build_query_service(config_path: str | None = None) -> QueryService:
    manager = ConfigManager(Path(config_path) if config_path else None)
    config = manager.load()
    repository = PriceRepository(config.database_path)
    repository.initialize()
    return QueryService(
        config_manager=manager,
        repository=repository,
        run_lock=threading.Lock(),
        get_config=lambda: config,
        refresh_config=lambda: None,
    )


def create_airchina_source(query_service: QueryService, **options):
    from sources.airchina import AirChinaSource

    return AirChinaSource(query_service=query_service, **options)


async def query_airchina_source(source, origin: str, destination: str, date: str, **kwargs) -> list[dict]:
    route = RouteQuery(
        origin=origin.upper(),
        destination=destination.upper(),
        departure_date=date,
        cabin=kwargs.get("cabin", "economy"),
        transfer_policy="direct_only",
        passengers=1,
        preferred_airlines=["CA"],
        providers=["airchina"],
    )
    report = await source.query_service.inspect_airchina_experiment(
        route,
        flight_no=source.flight_no,
        progress=source.progress,
        manual_handler=source.manual_handler,
        cancel_event=source.cancel_event,
        timeout_seconds=kwargs.get("timeout", source.timeout),
        auto_confirm_notices=source.auto_confirm_notices,
        notice_callback=source.notice_callback,
        diagnostic_callback=source.diagnostic_callback,
        entry_mode=source.entry_mode,
        entry_callback=source.entry_callback,
        session_mode=source.session_mode,
        session_pool=source.session_pool,
        session_callback=source.session_callback,
        network_callback=source.network_callback,
        reference_cache=source.reference_cache,
    )
    source.last_report = report
    records = flyclaw_records_from_report(report, route)
    if source.flight_no:
        records = [record for record in records if record["flight_number"] == source.flight_no]
    limit = kwargs.get("limit")
    return records[:limit] if limit is not None else records


async def query_airchina_plan(source, plan) -> list[dict]:
    """Serial city expansion, one total deadline, no retry after a blocked pair.

    last_report remains the native report for the existing experimental UI.
    last_query explains the aggregate coverage; partial success is never no-fares.
    """
    source.last_query = {
        "state": "running", "capabilities": deepcopy(CAPABILITIES),
        "planned_pairs": [{"origin": r.origin, "destination": r.destination} for r in plan.routes],
        "attempted_pairs": [], "airport_search_complete": False,
        "coverage_complete": False, "requested_max_stops": plan.stops,
        "connection_coverage": "unsupported", "records_before_limit": 0,
        "truncated": False,
    }
    audit = source.last_query
    records = []
    deadline = asyncio.get_running_loop().time() + plan.timeout
    try:
        async with asyncio.timeout(plan.timeout):
            for route in plan.routes:
                if source.cancel_event.is_set():
                    raise asyncio.CancelledError
                entry = {"origin": route.origin, "destination": route.destination, "state": "running"}
                audit["attempted_pairs"].append(entry)
                pair_records = await query_airchina_source(
                    source, route.origin, route.destination, route.departure_date,
                    cabin=route.cabin, timeout=max(.001, deadline - asyncio.get_running_loop().time()),
                )
                if source.cancel_event.is_set():
                    raise asyncio.CancelledError
                state = (source.last_report or {}).get("state", "invalid_report")
                entry["state"] = state
                if state not in {"reference_fares_observed", "fare_products_observed"}:
                    audit["state"] = "partial" if records else "inconclusive"
                    audit["stop_reason"] = state
                    break
                records.extend(pair_records)
            else:
                audit["airport_search_complete"] = True
                audit["state"] = "reference_fares_observed" if records else "inconclusive"
    except asyncio.CancelledError:
        audit["state"] = "cancelled"
        source.last_report = {"state": "cancelled", "quote_saved": False, "coverage_complete": False}
        if audit["attempted_pairs"] and audit["attempted_pairs"][-1]["state"] == "running":
            audit["attempted_pairs"][-1]["state"] = "cancelled"
        raise
    except Exception as exc:
        # Never log raw browser errors, URLs, request material or user credentials.
        reason = "timeout" if isinstance(exc, TimeoutError) else "query_failed"
        audit.update(state="partial" if records else "error", stop_reason=reason)
        source.last_report = {"state": "error", "reason": reason, "quote_saved": False,
                              "coverage_complete": False, "message": "国航查询未完成，未自动重试。"}
        if audit["attempted_pairs"]:
            audit["attempted_pairs"][-1]["state"] = reason
    records.sort(key=lambda r: (r["price"], r["scheduled_departure"],
                                r["origin_iata"], r["destination_iata"], r["flight_number"]))
    audit["records_before_limit"] = len(records)
    audit["truncated"] = plan.limit is not None and len(records) > plan.limit
    if plan.limit is not None:
        records = records[:plan.limit]
    for record in records:
        record.update(
            airport_search_complete=audit["airport_search_complete"],
            coverage_complete=False, connection_coverage="unsupported",
            requested_max_stops=plan.stops,
        )
    return records


def flyclaw_records_from_report(report: dict, route: RouteQuery) -> list[dict]:
    if not isinstance(report, dict) or report.get("state") not in {
        "reference_fares_observed", "fare_products_observed",
    }:
        return []
    preview = report.get("reference_preview")
    if not isinstance(preview, dict):
        return []
    offers = preview.get("offers")
    if not isinstance(offers, list):
        return []
    records = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        flight_no = str(offer.get("flight_no") or "")
        if not re.fullmatch(r"[A-Z]{2}\d{3,4}", flight_no):
            continue
        if (offer.get("departure_date") != route.departure_date
                or offer.get("cabin") != route.cabin
                or offer.get("origin", route.origin) != route.origin
                or offer.get("destination", route.destination) != route.destination
                or offer.get("currency", "CNY") != "CNY"):
            continue
        try:
            raw_price = offer["reference_fare"]
            price = float(raw_price)
            if isinstance(raw_price, bool) or not math.isfinite(price) or price <= 0:
                continue
            departure = f"{offer['departure_date']}T{offer['departure_time']}"
            arrival = f"{offer['arrival_date']}T{offer['arrival_time']}"
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", t) for t in (departure, arrival)):
            continue
        duration = _duration_minutes(departure, arrival)
        if duration is None:
            continue
        segment = {
            "flight_number": flight_no,
            "origin_iata": route.origin,
            "destination_iata": route.destination,
            "departure": departure,
            "arrival": arrival,
            "duration_minutes": duration,
        }
        records.append({
            "flight_number": flight_no,
            "airline": offer.get("marketing_airline") or "CA",
            "origin_iata": route.origin,
            "origin_city": (airport_manager.get_info(route.origin) or {}).get("city_cn", ""),
            "destination_iata": route.destination,
            "destination_city": (airport_manager.get_info(route.destination) or {}).get("city_cn", ""),
            "scheduled_departure": departure,
            "scheduled_arrival": arrival,
            "actual_departure": None,
            "actual_arrival": None,
            "status": "reference_only",
            "aircraft_type": "",
            "delay_minutes": None,
            "price": price,
            "currency": "CNY",
            "price_kind": "starting_fare",
            "price_unit": "per_adult",
            "tax_included": False,
            "total_price": None,
            "bookable_confirmed": False,
            "reference_only": True,
            "stops": 0,
            "duration_minutes": duration,
            "cabin_class": CABIN_NAMES[route.cabin],
            "cabin": route.cabin,
            "trip_type": "one_way",
            "source": "airchina",
            "query_policy": deepcopy(report.get("query_policy", {})),
            "segments": [segment],
            "layover_cities": [],
            "layover_minutes": [],
            "max_layover_minutes": 0,
            "review_url": build_search_url(route),
        })
    records.sort(key=lambda item: (item["price"], item["scheduled_departure"], item["flight_number"]))
    return records


def _duration_minutes(departure: str, arrival: str) -> int | None:
    try:
        seconds = (datetime.fromisoformat(arrival) - datetime.fromisoformat(departure)).total_seconds()
        return int(seconds // 60) if seconds > 0 else None
    except ValueError:
        return None
