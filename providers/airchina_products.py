"""Read individual fare products, without booking or pretending taxes are known."""
from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from time import monotonic

from providers.airchina_parser import CABINS, DOM_SCRIPT, compact, parse_reference_fares
from providers.airchina_page import snapshot_stop_state
from providers.airchina_result_wait import LIVE_GUARDS

# P0 only accepts combinations actually observed. Unknown layouts/classes must
# be sampled before enabling them; this also rejects stale cross-cabin details.
OBSERVED_SEAT_CLASSES = {"economy": set("KSVUY"), "business": set("RJ")}

def parse_product_fares(snapshot, route):
    """Validate against the same atomic row/header snapshot as starting fares."""
    references = parse_reference_fares(snapshot, route)
    result = {
        "status": "inconclusive", "products": [], "quote_saved": False,
        "parser_ready": False, "coverage_complete": False, "rejected_products": 0,
        "reason": references["reason"],
    }
    if references["status"] != "reference_fares_observed":
        return result
    # Old fixtures lack these fields: they cannot establish stable products.
    if snapshot.get("loading_visible") is not False:
        return {**result, "reason": "product_loading_unconfirmed"}
    if snapshot.get("notice_confirmation_visible") is not False:
        return {**result, "reason": "manual_notice_confirmation_required"}
    reference_by_key = {
        (o["flight_no"], o["departure_time"], o["cabin"]): o for o in references["offers"]
    }
    products, conflicts = {}, set()
    for row in snapshot["rows"]:
        if not isinstance(row, dict) or not row.get("products"):
            continue
        expanded = row.get("expanded_cabins")
        numbers = row.get("flight_numbers")
        if (not isinstance(expanded, list) or len(expanded) != 1 or expanded[0] not in CABINS
                or not isinstance(numbers, list) or len(numbers) != 1):
            result["rejected_products"] += 1
            continue
        key = (numbers[0], compact(row.get("departure_time")), CABINS[expanded[0]][1])
        reference = reference_by_key.get(key)
        # Revalidate this exact row, not a different duplicate with the same flight.
        own = parse_reference_fares({**snapshot, "rows": [row]}, route)
        if reference is None or reference not in own["offers"]:
            result["rejected_products"] += 1
            continue
        if not isinstance(row["products"], list):
            result["rejected_products"] += 1
            continue
        for item in row["products"]:
            product = _parse_product(item, reference)
            if product is None:
                result["rejected_products"] += 1
                continue
            identity = key + (product["seat_class"], product["product_name"])
            if identity in products and products[identity] != product:
                conflicts.add(identity)
            products[identity] = product
    result["products"] = sorted([p for k, p in products.items() if k not in conflicts],
                                key=lambda p: (p["base_fare"], p["flight_no"], p["seat_class"]))
    result["conflicting_products"] = len(conflicts)
    if result["products"]:
        result.update(status="fare_products_observed", reason="base_fares_excluding_tax")
    else:
        result["reason"] = "no_supported_expanded_product"
    return result


def _parse_product(item, reference):
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    seat = re.fullmatch(r"\(([A-Z])舱\)", compact(item.get("seat_class_text")))
    price = re.fullmatch(r"[¥￥]([1-9]\d{0,6}(?:\.\d{1,2})?)", compact(item.get("price_text")))
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or not seat or not price:
        return None
    if seat[1] not in OBSERVED_SEAT_CLASSES.get(reference["cabin"], set()):
        return None
    fare = float(price[1])
    if fare < reference["reference_fare"]:
        return None  # Stale list/product mismatch: do not manufacture savings.
    summaries = item.get("summaries")
    if not isinstance(summaries, list):
        return None
    details = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            return None
        label, value = summary.get("label"), summary.get("value")
        if label not in {"退改费", "行李额", "里程累积"}:
            continue
        if not isinstance(value, str) or len(value) > 300 or label in details:
            return None
        details[label] = value.strip()
    seat_text = compact(item.get("seat_text"))
    # Explicit zero in a visible non-placeholder element is not purchasable.
    # A missing seat label does NOT mean zero, unlimited, or enough inventory.
    stock = re.fullmatch(r"剩余(\d{1,2})张", seat_text)
    displayed_seats = int(stock[1]) if stock else None
    estimate = re.search(r"预估优惠[¥￥](\d+(?:\.\d{1,2})?)", compact(item.get("price_block_text")))
    context = {k: v for k, v in reference.items() if k not in {
        "reference_fare", "price_kind", "source", "fare_product_confirmed",
    }}
    return {
        **context, "product_name": name.strip(), "seat_class": seat[1],
        "base_fare": fare, "list_starting_fare": reference["reference_fare"],
        "price_kind": "product_base_fare", "fare_product_confirmed": True,
        "fare_rules_complete": False, "refund_change_summary": details.get("退改费"),
        "baggage_summary": details.get("行李额"), "mileage_summary": details.get("里程累积"),
        "displayed_seats": displayed_seats,
        "inventory_status": ("unknown" if displayed_seats is None else
                             "shown_zero" if displayed_seats == 0 else "shown_remaining"),
        "estimated_discount": float(estimate[1]) if estimate else None,
        "estimated_discount_applied": False,
        "source": "airchina_visible_expanded_product",
    }


async def expand_fare_products(page, route, *, flight_no, cabin, timeout_ms=10000):
    """Click ONE observed cabin, not a booking button; poll only DOM afterward."""
    if not re.fullmatch(r"[A-Z]{2}\d{3,4}", flight_no or ""):
        raise ValueError("请提供明确航班号，例如 CA1549。")
    source_cabin = next((k for k, (_, v) in CABINS.items() if v == cabin), None)
    if source_cabin is None or cabin not in OBSERVED_SEAT_CLASSES:
        raise ValueError("当前票价详情只验证了 economy / business 两个具体舱等。")
    if not 100 <= timeout_ms <= 60000:
        raise ValueError("详情读取超时必须为 100 到 60000 毫秒。")
    request = replace(route, cabin=cabin)
    script = "(" + DOM_SCRIPT + "\n)()"
    snapshot = await page.evaluate(script)
    preview = parse_reference_fares(snapshot, request)
    if (preview["status"] != "reference_fares_observed"
            or any(snapshot.get(field) is not False for field in LIVE_GUARDS)):
        return {"status": "inconclusive", "products": [], "reason": "page_not_ready", "quote_saved": False}
    expected_url = snapshot["url"]
    matches = [o for o in preview["offers"] if o["flight_no"] == flight_no]
    if len(matches) != 1:
        return {"status": "inconclusive", "products": [], "reason": "ambiguous_or_missing_flight", "quote_saved": False}
    flight = page.locator('[class*="shared-flights_number__"]').filter(has_text=re.compile("^" + re.escape(flight_no) + "$"))
    row = page.locator('[class*="style_singleFightWrapper__"]').filter(has=flight)
    if await row.count() != 1:
        return {"status": "inconclusive", "products": [], "reason": "ambiguous_dom_row", "quote_saved": False}
    cell = row.locator(f'[class*="cabinContent_{source_cabin}__"]')
    if await cell.count() != 1:
        return {"status": "inconclusive", "products": [], "reason": "ambiguous_dom_cabin", "quote_saved": False}
    # An existing expansion is read, not toggled shut.
    if "cabinContent_activeCabin__" not in (await cell.get_attribute("class") or ""):
        await cell.click(timeout=timeout_ms)  # No force, overlay removal or retries.
    deadline = monotonic() + timeout_ms / 1000
    previous = None
    while monotonic() < deadline:
        snapshot = await page.evaluate(script)
        stop = snapshot_stop_state(snapshot, expected_url)
        if stop:
            return {"status": "needs_attention", "products": [], "reason": stop, "quote_saved": False}
        parsed = parse_product_fares(snapshot, request)
        if any(snapshot.get(field) is not False for field in LIVE_GUARDS):
            parsed["products"] = []
        # Capture only the requested flight, not other expanded cards.
        parsed["products"] = [p for p in parsed["products"] if p["flight_no"] == flight_no]
        if parsed["products"] and parsed["products"] == previous:
            return parsed  # Two consistent observations, no extra HTTP search.
        previous = parsed["products"]
        await asyncio.sleep(.25)
    return {"status": "inconclusive", "products": [], "reason": "product_details_timeout", "quote_saved": False}
