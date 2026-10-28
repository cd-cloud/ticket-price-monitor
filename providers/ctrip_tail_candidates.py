"""Keep date evidence scoped to the candidate that actually proves it."""
from copy import deepcopy
import math

from providers.ctrip_itinerary import endpoint_matches
from tail_quotes import stable_key
from tail_validation import chronology_checks


def network_candidate_confirms_date(match, route, transfer):
    """Check actual A-B-C dates without applying whole-trip cabin preferences."""
    if match.get("detail_source") != "network_exact":
        return False
    legs = match.get("details") or []
    if len(legs) != 2 or any(not isinstance(leg, dict) for leg in legs):
        return False
    try:
        price = float(match.get("total_price"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0:
        return False
    first, second = legs
    if not (
        endpoint_matches(first, route.origin)
        and endpoint_matches(first, transfer, arrival=True)
        and endpoint_matches(second, transfer)
        and endpoint_matches(second, route.destination, arrival=True)
        and first.get("departure_date") == route.departure_date
    ):
        return False
    reasons, warnings = chronology_checks(legs, route.departure_date)
    return not reasons and not warnings


def prepare_network_candidates(evidence, matches, route, transfer):
    """Return detached candidates and a report; a network proof is not UI proof."""
    if evidence.get("overlay_visible") or evidence.get("status") == "mismatch":
        kind = "mismatch" if evidence.get("status") == "mismatch" else "unconfirmed"
        raise RuntimeError(f"Ctrip result date {kind}: conflicting date or open date editor.")
    ui_confirmed = evidence.get("matched") and evidence.get("status") == "confirmed"
    if ui_confirmed:
        candidates = deepcopy(matches)
        for match in candidates:
            match["date_evidence"] = deepcopy(evidence)
        return candidates, deepcopy(evidence)

    # Failed UI inspection is not equivalent to an inspected-but-unknown date.
    candidates = []
    if evidence.get("status") == "unknown" and not evidence.get("error"):
        for match in matches:
            if not network_candidate_confirms_date(match, route, transfer):
                continue
            candidate = deepcopy(match)
            candidate["date_evidence"] = {
                **deepcopy(evidence), "matched": True, "status": "confirmed",
                "ui_status": "unknown", "matched_source": "network_itinerary",
                "matched_value": route.departure_date, "scope": "candidate",
                "response_url": match.get("response_url"),
                "query_generation": match.get("query_generation"),
            }
            candidates.append(candidate)
    if not candidates:
        raise RuntimeError(
            "Ctrip result date unconfirmed: no visible selected date or complete matching A-B-C itinerary."
        )
    return candidates, {
        **deepcopy(evidence), "matched": True, "status": "confirmed",
        "ui_status": "unknown", "matched_source": "network_itinerary",
        "scope": "network_candidates_only", "confirmed_candidates": len(candidates),
    }


def merge_tail_candidates(network, page):
    """Union whole quotes; never fill a cheap DOM quote with network fields."""
    merged, seen = [], set()
    for match in [*network, *page]:
        # Only exact duplicates are collapsed. Different prices, sources,
        # products, partial legs and evidence must retain their own identity.
        key = stable_key(match)
        if key not in seen:
            seen.add(key)
            merged.append(deepcopy(match))
    return merged
