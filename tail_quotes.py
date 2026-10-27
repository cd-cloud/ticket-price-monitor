"""Pure quote identity, comparison and freshness rules (no provider I/O).

Unknown fare terms stay unknown. A search response is not a booking confirmation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any


def stable_key(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def normalized_cabin(value: Any) -> str:
    text = str(value or "").strip().lower()
    return {
        "经济舱": "economy", "超级经济舱": "premium_economy", "高端经济舱": "premium_economy",
        "商务舱": "business", "公务舱": "business", "头等舱": "first",
        "y": "economy", "s": "premium_economy", "c": "business", "f": "first",
    }.get(text, text)


def cabin_matches(actual: Any, requested: str) -> bool:
    allowed = {"economy_plus": {"economy", "premium_economy"}, "business_first": {"business", "first"}}
    return normalized_cabin(actual) in allowed.get(requested, {requested})


def clock_time(value: Any) -> str:
    match = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", str(value or ""))
    return f"{int(match[1]):02d}:{match[2]}" if match else ""


def first_leg_key(result: dict[str, Any]) -> str:
    legs = result.get("flight_details") or []
    leg = legs[0] if legs else {}
    identity = [
        leg.get("flight_no"), leg.get("departure_airport") or leg.get("origin"),
        leg.get("arrival_airport") or leg.get("destination"),
        leg.get("departure_date") or result.get("departure_date"),
        clock_time(leg.get("departure_time")), normalized_cabin(leg.get("cabin") or result.get("cabin")),
    ]
    # Do not group unrelated unknown flights together.
    if not leg.get("flight_no") or not leg.get("departure_time"):
        identity.append(stable_key(legs))
    return stable_key(identity)


def itinerary_key(result: dict[str, Any]) -> str:
    return stable_key({
        "provider": result.get("provider"),
        "date": result.get("departure_date"), "cabin": result.get("cabin"),
        "passengers": result.get("passengers", 1), "currency": result.get("currency"),
        "legs": [
            {k: clock_time(leg.get(k)) if k.endswith("_time") else normalized_cabin(leg.get(k)) if k == "cabin" else leg.get(k) for k in (
                "origin", "destination", "departure_airport", "arrival_airport",
                "departure_date", "departure_time", "arrival_time", "flight_no", "cabin",
            )}
            for leg in result.get("flight_details") or []
        ],
        "fare_product": (result.get("raw_payload") or {}).get("fare_product"),
    })


def quote_terms(provider: str, passengers: int, currency: str = "CNY", **overrides: Any) -> dict[str, Any]:
    # Current integrations do not prove tax inclusion or party availability.
    terms = {
        "currency": currency, "price_basis": "unknown", "tax_included": None,
        "requested_passengers": passengers, "priced_passengers": None,
        "inventory_verified": False, "fare_conditions": None,
        "baggage": "unknown", "ticket_rules": "unknown",
    }
    terms.update(overrides)
    return terms


def annotate_quote(result: dict[str, Any], *, ttl_minutes: int = 20, rechecked: bool = False) -> dict[str, Any]:
    raw = result.setdefault("raw_payload", {})
    raw.setdefault("quote_terms", quote_terms(
        str(result.get("provider", "")), int(result.get("passengers") or 1), result.get("currency") or "CNY",
    ))
    raw["itinerary_key"] = itinerary_key(result)
    raw["first_leg_key"] = first_leg_key(result)
    try:
        observed = datetime.fromisoformat(str(result["observed_at"]).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        raw["expires_at"] = (observed + timedelta(minutes=ttl_minutes)).isoformat()
    except (ValueError, KeyError, TypeError):
        raw["expires_at"] = None
    if rechecked:
        raw["rechecked_at"] = result.get("observed_at")
    legs = result.get("flight_details") or []
    airport_change = any(
        left.get("arrival_airport") and right.get("departure_airport")
        and left["arrival_airport"] != right["departure_airport"]
        for left, right in zip(legs, legs[1:])
    )
    raw["suitability"] = {
        "airport_change": airport_change,
        "baggage": "unknown", "ticket_rules": "unknown", "onward_impact": "unknown",
        "booking_confirmed": False,
    }
    return result


def public_quote_metadata(result: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    raw = result.get("raw_payload") or {}
    now = now or datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(str(raw.get("expires_at")).replace("Z", "+00:00"))
        fresh = expires > now
    except (ValueError, TypeError):
        fresh = False
    return {
        **{k: raw.get(k) for k in (
            "quote_terms", "expires_at", "rechecked_at", "recheck_outcome", "baselines", "suitability", "query_fingerprint", "search_metadata", "search_scope",
        )},
        "first_leg_key": raw.get("first_leg_key") or first_leg_key(result),
        "itinerary_key": raw.get("itinerary_key") or itinerary_key(result),
        "freshness": "fresh" if fresh else "stale",
    }


def select_tail_alternatives(results: list[dict[str, Any]], *, per_first_leg: int = 3) -> list[dict[str, Any]]:
    """Call only after all route/date/cabin/time checks; deduplicate exact offers."""
    unique: dict[str, dict[str, Any]] = {}
    for result in results:
        price = float(result.get("price") or 0)
        if not math.isfinite(price) or price <= 0:
            continue
        key = itinerary_key(result)
        if key not in unique or price < float(unique[key]["price"]):
            unique[key] = result
    counts: dict[str, int] = {}
    selected = []
    for result in sorted(unique.values(), key=lambda item: float(item["price"])):
        key = first_leg_key(result)
        if counts.get(key, 0) >= per_first_leg:
            continue
        counts[key] = counts.get(key, 0) + 1
        selected.append(result)
    return selected


def _comparison(result: dict[str, Any], baseline: dict[str, Any] | None, *, same_flight: bool) -> dict[str, Any]:
    if not baseline:
        return {"status": "unavailable", "price": None, "savings_amount": None, "reasons": ["未获取到可比基准"]}
    terms = (result.get("raw_payload") or {}).get("quote_terms") or {}
    other = baseline.get("quote_terms") or {}
    reasons = []
    for field in ("currency", "price_basis", "tax_included", "priced_passengers"):
        if terms.get(field) is None or other.get(field) is None or terms.get(field) == "unknown":
            reasons.append(f"{field} 未确认")
        elif terms[field] != other[field]:
            reasons.append(f"{field} 不一致")
    passengers = int(result.get("passengers") or 1)
    if passengers > 1:
        if not terms.get("inventory_verified") or not other.get("inventory_verified"):
            reasons.append("多人库存未确认")
        if terms.get("priced_passengers") != passengers or other.get("priced_passengers") != passengers:
            reasons.append("报价人数与请求人数不一致")
    if same_flight and (not terms.get("fare_conditions") or terms.get("fare_conditions") != other.get("fare_conditions")):
        reasons.append("票价条件未确认一致")
    try:
        a = datetime.fromisoformat(str(result["observed_at"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(baseline["observed_at"]).replace("Z", "+00:00"))
        if abs((a - b).total_seconds()) > 1200:
            reasons.append("采集时间相差超过20分钟")
    except (ValueError, KeyError, TypeError):
        reasons.append("采集时间未确认")
    price = float(baseline.get("price") or 0)
    if not math.isfinite(price) or price <= 0:
        reasons.append("基准价格无效")
    comparable = not reasons
    savings = round(price - float(result["price"]), 2) if comparable else None
    return {
        "status": "comparable" if comparable else "reference_only", "price": price,
        "observed_at": baseline.get("observed_at"), "review_url": baseline.get("review_url"),
        "quote_terms": other, "savings_amount": savings,
        "savings_percent": round(savings / price * 100, 2) if comparable else None,
        "reasons": reasons,
    }


def apply_baselines(results: list[dict[str, Any]], baseline: dict[str, Any] | None) -> list[dict[str, Any]]:
    for result in results:
        raw = result.setdefault("raw_payload", {})
        raw.setdefault("quote_terms", quote_terms(result.get("provider", ""), int(result.get("passengers") or 1)))
        market = baseline.get("market") if baseline and "market" in baseline else baseline
        same = ((baseline or {}).get("same_flights") or {}).get(first_leg_key(result))
        comparisons = {
            "market": _comparison(result, market, same_flight=False),
            "same_flight": _comparison(result, same, same_flight=True),
        }
        raw["baselines"] = comparisons
        values = {
            "baseline_price": comparisons["market"]["price"],
            "baseline_observed_at": (market or {}).get("observed_at"),
            "baseline_review_url": (market or {}).get("review_url"),
            "savings_amount": comparisons["market"]["savings_amount"],
            "savings_percent": comparisons["market"].get("savings_percent"),
        }
        raw.update(values)
        result.update(values)
    return results


def build_baseline_catalog(
    offers: list[dict[str, Any]], *, provider: str, departure_date: str, cabin: str,
    passengers: int, observed_at: str, review_url: str = "", time_start: str = "", time_end: str = "",
    preferred_airlines: list[str] | None = None,
) -> dict[str, Any] | None:
    from providers.ctrip_tail_parser import details_match_airlines
    candidates = []
    for offer in offers:
        details = offer.get("flight_details") or []
        if len(details) != 1 or not details_match_airlines(details, preferred_airlines):
            continue
        leg = details[0]
        if leg.get("departure_date") != departure_date:
            continue
        time = clock_time(leg.get("departure_time"))
        if (time_start or time_end) and not time:
            continue
        if time_start and time < time_start or time_end and time > time_end:
            continue
        if not cabin_matches(leg.get("cabin"), cabin):
            continue
        price = float(offer.get("price") or 0)
        if price <= 0 or not math.isfinite(price):
            continue
        item = {
            **offer, "price": price, "provider": provider, "departure_date": departure_date,
            "cabin": cabin, "passengers": passengers, "observed_at": observed_at,
            "review_url": offer.get("final_url") or review_url,
            "quote_terms": offer.get("quote_terms") or quote_terms(provider, passengers),
        }
        candidates.append(item)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["price"])
    same = {}
    for item in candidates:
        leg = item["flight_details"][0]
        if leg.get("flight_no") and leg.get("departure_time"):
            same.setdefault(first_leg_key(item), item)
    return {"market": candidates[0], "same_flights": same}
