"""Parse observed domestic one-way DOM into NON-BOOKABLE reference fares.

No generic numeric/text fallback. A starting fare is never a final total.
This module is deliberately not registered as a production quote handler.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from config_manager import RouteQuery


DOM_SCRIPT = Path(__file__).with_name("airchina_dom.js").read_text(encoding="utf-8")
CABINS = {"first": ("头等舱", "first"), "business": ("公务舱", "business"),
          "superEconomy": ("超级经济舱", "premium_economy"), "economy": ("经济舱", "economy")}
CABIN_FILTERS = {
    "economy": {"economy"}, "premium_economy": {"premium_economy"},
    "business": {"business"}, "first": {"first"},
    "economy_plus": {"economy", "premium_economy"}, "business_first": {"business", "first"},
}


def compact(value):
    return re.sub(r"\s+", "", value) if isinstance(value, str) else ""


def parse_reference_fares(snapshot: dict, route: RouteQuery) -> dict:
    """Validate URL AND visible context; price stays tied to its row and cabin.

    Dates may be historical here to permit fixture replay. The live entry point
    separately rejects past dates. Only the observed same-day single-flight DOM
    is accepted; unrecognized rows are skipped, never inferred from the request.
    """
    result = {"status": "unverified", "offers": [], "rejected_rows": 0,
              "filtered_offers": 0, "quote_saved": False, "parser_ready": False,
              "coverage_complete": False}

    def fail(reason):
        return {**result, "reason": reason}

    if not isinstance(snapshot, dict) or snapshot.get("schema") != "airchina_visible_oneway_v1":
        return fail("unknown_schema")
    if snapshot.get("verification_visible") is not False or snapshot.get("blocked_visible") is not False:
        return fail("access_unconfirmed")
    # Historical fixtures predate these observations. Live orchestration also
    # requires explicit False for every guard before accepting a stable sample.
    if snapshot.get("stale_visible") is True:
        return fail("stale_results")
    if snapshot.get("loading_visible") is True:
        return fail("results_loading")
    if snapshot.get("notice_confirmation_visible") is True:
        return fail("manual_notice_confirmation_required")
    if (route.route_type != "one_way" or route.return_date or route.segments
            or route.passengers != 1 or route.transfer_policy not in {"any", "direct_only"}
            or route.cabin not in CABIN_FILTERS):
        return fail("unsupported_request")
    try:
        expected_date = date.fromisoformat(route.departure_date).isoformat()
        parsed = urlsplit(snapshot.get("url", ""))
    except (TypeError, ValueError):
        return fail("invalid_context")
    origin, destination = route.origin.upper(), route.destination.upper()
    if not all(re.fullmatch(r"[A-Z]{3}", c) for c in (origin, destination)) or origin == destination:
        return fail("invalid_airports")
    if (parsed.scheme != "https" or parsed.netloc != "www.airchina.com.cn"
            or parsed.path != f"/flight/oneway/{origin.lower()}-{destination.lower()}/{expected_date}"):
        return fail("url_mismatch")
    if snapshot.get("header_count") != 1 or compact(snapshot.get("trip_type")) != "单程":
        return fail("ambiguous_header")
    route_text = snapshot.get("route_text")
    if not isinstance(route_text, str) or re.findall(r"\(([A-Z]{3})\)", route_text) != [origin, destination]:
        return fail("visible_route_mismatch")
    displayed_date = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:周[一二三四五六日天])?",
                                  compact(snapshot.get("date_text")))
    try:
        valid_date = displayed_date and date(*map(int, displayed_date.groups())).isoformat() == expected_date
    except ValueError:
        valid_date = False
    if not valid_date:
        return fail("visible_date_mismatch")
    if compact(snapshot.get("passengers_text")) != "1成人":
        return fail("visible_passengers_mismatch")
    if compact(snapshot.get("price_notice")) != "以下价格为每位成人旅客不含税票价":
        return fail("unknown_price_scope")
    headers = snapshot.get("cabin_headers")
    if not isinstance(headers, dict) or any(compact(headers.get(k)) != v[0] for k, v in CABINS.items()):
        return fail("unknown_cabin_columns")
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        return fail("missing_rows")
    airlines = {a.upper() for a in route.preferred_airlines or []}
    seen = {}
    conflicts = set()
    for row in rows:
        offers = _row_offers(row, expected_date, origin, destination)
        if offers is None:
            result["rejected_rows"] += 1
            continue
        for offer in offers:
            if (offer["cabin"] not in CABIN_FILTERS[route.cabin]
                    or airlines and offer["marketing_airline"] not in airlines):
                result["filtered_offers"] += 1
                continue
            key = (offer["flight_no"], offer["departure_time"], offer["cabin"])
            if key in seen and seen[key] != offer:
                conflicts.add(key)
            seen[key] = offer
    result["offers"] = sorted([o for k, o in seen.items() if k not in conflicts],
                              key=lambda o: (o["reference_fare"], o["departure_time"], o["cabin"]))
    result["conflicting_offers"] = len(conflicts)
    result["status"] = "reference_fares_observed" if result["offers"] else "inconclusive"
    result["reason"] = "starting_fares_not_final_quotes" if result["offers"] else "no_supported_visible_offer"
    return result


def _row_offers(row, departure_date, origin, destination):
    if not isinstance(row, dict):
        return None
    flights = row.get("flight_numbers")
    if (not isinstance(flights, list) or len(flights) != 1 or not isinstance(flights[0], str)
            or not re.fullmatch(r"[A-Z]{2}\d{3,4}", flights[0])):
        return None  # Codeshare/multiple-leg layouts require their own evidence.
    if row.get("between_text") != "":
        return None  # Missing, connecting or stopover indicators: never assume direct.
    for field, code in (("departure_airport", origin), ("arrival_airport", destination)):
        if not re.fullmatch(re.escape(code) + r"(?:\s+T[0-9A-Z]+)?", str(row.get(field) or "").strip()):
            return None
    times = [compact(row.get(f)) for f in ("departure_time", "arrival_time")]
    if not all(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
        return None  # "+1" needs explicit date handling before support.
    minutes = [int(t[:2]) * 60 + int(t[3:]) for t in times]
    duration = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", compact(row.get("duration")))
    if not duration or not duration.group() or minutes[1] <= minutes[0]:
        return None
    duration_minutes = int(duration[1] or 0) * 60 + int(duration[2] or 0)
    if duration_minutes != minutes[1] - minutes[0]:
        return None  # Timezone/connection ambiguity, not a guess.
    cabins = row.get("cabins")
    if not isinstance(cabins, dict):
        return None
    offers = []
    for source, (_label, normalized) in CABINS.items():
        cell = cabins.get(source)
        if not isinstance(cell, dict):
            return None
        if cell.get("price_text") is None and compact(cell.get("cell_text")) == "--":
            continue
        price = re.fullmatch(r"[￥¥]([1-9]\d{0,6}(?:\.\d{1,2})?)起", compact(cell.get("price_text")))
        if not price:
            return None  # Fail closed when product structure changes.
        offers.append({
            "provider": "airchina", "origin": origin, "destination": destination,
            "departure_date": departure_date, "arrival_date": departure_date,
            "flight_no": flights[0], "marketing_airline": flights[0][:2],
            "operating_airline": None,
            "departure_time": times[0], "arrival_time": times[1],
            "departure_airport_text": row["departure_airport"],
            "arrival_airport_text": row["arrival_airport"],
            "cabin": normalized, "passengers": 1,
            "reference_fare": float(price[1]), "currency": "CNY",
            "price_kind": "starting_fare", "tax_included": False,
            "price_unit": "per_adult", "total_price": None,
            "inventory_confirmed": False, "bookable_confirmed": False,
            "fare_product_confirmed": False, "source": "airchina_visible_cabin_column",
        })
    return offers


async def collect_reference_fares(page, route: RouteQuery) -> dict:
    """Capture context and rows atomically; parser rechecks before returning."""
    return parse_reference_fares(await page.evaluate("(" + DOM_SCRIPT + "\n)()"), route)
