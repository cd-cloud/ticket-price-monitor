"""FlyClaw-compatible request planning without network access or guessed routes.

Only capability supported by the validated Air China transport is advertised.
Fliggy signing, device context and credentials are deliberately not portable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math

from config_manager import RouteQuery
from providers.airchina_form import validate_entry

CABIN_ALIASES = {
    "economy": "economy", "premium": "premium_economy",
    "premium_economy": "premium_economy", "business": "business", "first": "first",
}
CABIN_NAMES = {
    "economy": "经济舱", "premium_economy": "超级经济舱",
    "business": "公务舱", "first": "头等舱",
}
CAPABILITIES = {
    "one_way": True, "round_trip": False, "connecting_journeys": False,
    "city_expansion": True, "cabins": list(CABIN_NAMES),
    "flight_number_only": False, "final_price": False,
    "transport": "validated_official_browser",
}


@dataclass(frozen=True)
class QueryPlan:
    routes: tuple[RouteQuery, ...]
    timeout: float
    limit: int | None
    stops: int | str


def plan_query(origin, destination, departure_date, *, return_date, adults, cabin,
               stops, limit, timeout, max_airport_pairs, entry_mode, flight_no):
    # Imports the vendored local index, never updates it or performs I/O online.
    from vendor.flyclaw.airport_manager import airport_manager

    if return_date is not None:
        raise ValueError("Air China supports one-way only; verified round-trip journey/price evidence is unavailable.")
    if type(adults) is not int or adults != 1:
        raise ValueError("Air China requires exactly one adult.")
    if not isinstance(cabin, str) or cabin not in CABIN_ALIASES:
        raise ValueError("Cabin must be economy, premium, premium_economy, business or first.")
    if not ((type(stops) is int and stops in (0, 1, 2)) or stops == "any"):
        raise ValueError("Stops must be 0, 1, 2 or 'any' (maximum stops, not exact stops).")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("Limit must be a positive integer or None.")
    if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Timeout must be finite and positive.")
    if type(max_airport_pairs) is not int or not 1 <= max_airport_pairs <= 16:
        raise ValueError("Airport-pair budget must be between 1 and 16.")
    try:
        if date.fromisoformat(departure_date).isoformat() != departure_date:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Date must be YYYY-MM-DD.") from None
    if flight_no and CABIN_ALIASES[cabin] not in {"economy", "business"}:
        raise ValueError("Product expansion by flight number supports economy/business only.")

    def resolve(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Provide an airport code or an unambiguous city name.")
        codes = list(dict.fromkeys(airport_manager.resolve_all(value.strip())))
        # Do not use the upstream fallback that reintroduces inactive airports.
        codes = [code for code in codes if airport_manager.is_active(code)]
        if not codes:
            raise ValueError("Airport/city could not be resolved to an active airport.")
        return codes

    origins, destinations = resolve(origin), resolve(destination)
    if set(origins) & set(destinations):
        raise ValueError("Origin and destination airport sets must not overlap.")
    if len(origins) * len(destinations) > max_airport_pairs:
        raise ValueError("City search exceeds airport-pair budget; select explicit airports.")
    if entry_mode == "results" and len(origins) * len(destinations) != 1:
        raise ValueError("Retained-result requery requires one explicit airport pair.")
    routes = []
    for start in origins:
        for end in destinations:
            route = RouteQuery(
                origin=start, destination=end, departure_date=departure_date,
                cabin=CABIN_ALIASES[cabin], transfer_policy="direct_only", passengers=1,
                preferred_airlines=["CA"], providers=["airchina"],
            )
            validate_entry(route, entry_mode)
            routes.append(route)
    return QueryPlan(tuple(routes), float(timeout), limit, stops)
