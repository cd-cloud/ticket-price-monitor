from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

from config_manager import ConfigManager, RouteQuery
from data_storage import PriceRepository
from providers.airchina_page import build_search_url
from query_service import QueryService

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
        timeout_seconds=source.timeout,
        auto_confirm_notices=source.auto_confirm_notices,
        notice_callback=source.notice_callback,
        diagnostic_callback=source.diagnostic_callback,
        entry_mode=source.entry_mode,
        entry_callback=source.entry_callback,
        session_mode=source.session_mode,
        session_pool=source.session_pool,
        session_callback=source.session_callback,
        network_callback=source.network_callback,
    )
    source.last_report = report
    records = flyclaw_records_from_report(report, route)
    limit = kwargs.get("limit")
    return records[:limit] if limit is not None else records


def flyclaw_records_from_report(report: dict, route: RouteQuery) -> list[dict]:
    if report.get("state") != "reference_fares_observed":
        return []
    offers = (report.get("reference_preview") or {}).get("offers") or []
    records = []
    for offer in offers:
        flight_no = str(offer.get("flight_no") or "")
        departure = f"{offer['departure_date']}T{offer['departure_time']}"
        arrival = f"{offer['arrival_date']}T{offer['arrival_time']}"
        duration = _duration_minutes(departure, arrival)
        segment = {
            "flight_number": flight_no,
            "origin_iata": route.origin,
            "destination_iata": route.destination,
            "departure": departure,
            "arrival": arrival,
            "duration_minutes": duration or 0,
        }
        records.append({
            "flight_number": flight_no,
            "airline": offer.get("marketing_airline") or "CA",
            "origin_iata": route.origin,
            "origin_city": airport_manager.get_display_name(route.origin),
            "destination_iata": route.destination,
            "destination_city": airport_manager.get_display_name(route.destination),
            "scheduled_departure": departure,
            "scheduled_arrival": arrival,
            "actual_departure": None,
            "actual_arrival": None,
            "status": "reference_only",
            "aircraft_type": "",
            "delay_minutes": None,
            "price": offer["reference_fare"],
            "currency": "CNY",
            "price_kind": "starting_fare",
            "price_unit": "per_adult",
            "tax_included": False,
            "total_price": None,
            "bookable_confirmed": False,
            "reference_only": True,
            "stops": 0,
            "duration_minutes": duration,
            "cabin_class": offer.get("cabin"),
            "source": "airchina",
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
