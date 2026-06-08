from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from browser_session_policy import BrowserSessionMetadata
from config_manager import RouteQuery
from providers.base import ProviderQueryOutcome


@dataclass(slots=True)
class QueryResult:
    provider: str
    route: RouteQuery
    price: float
    currency: str
    observed_at: str
    notes: list[str]
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class OfferDetails:
    price: float
    departure_time: str | None = None
    row_text: str | None = None


def build_query_result(
    *,
    provider_name: str,
    route: RouteQuery,
    outcome: ProviderQueryOutcome,
    session_metadata: BrowserSessionMetadata,
    response_store: list[dict[str, Any]],
    automation_backend: str,
    browser_use_status: Any,
    browser_use_note: str,
    default_currency: str,
    include_browser_backend_note: bool,
) -> QueryResult:
    raw_payload = {
        "automation_backend": automation_backend,
        **session_metadata.to_payload(),
        "browser_use": {
            "available": browser_use_status.available,
            "reason": browser_use_status.reason,
            "config_dir": browser_use_status.config_dir,
        },
        "search_url": outcome.final_url,
        "title": outcome.title,
        "route_type": route.route_type,
        "best_offer": {
            "price": outcome.price,
            "departure_time": outcome.departure_time,
            "row_text": outcome.row_text,
        },
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "network_hits": response_store[-20:],
    }
    raw_payload.update(outcome.extra_payload)
    if outcome.flight_details:
        raw_payload["flight_details"] = outcome.flight_details

    notes = [
        f"local browser automation via {automation_backend}",
        browser_use_note,
        f"provider={provider_name}",
    ]
    if include_browser_backend_note:
        notes.append(f"browser_backend={session_metadata.browser_backend}")
    notes.append(f"final_url={outcome.final_url}")

    return QueryResult(
        provider=provider_name,
        route=route,
        price=outcome.price,
        currency="USD" if provider_name == "priceline" else default_currency,
        observed_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        notes=notes,
        raw_payload=raw_payload,
    )
