"""Explainable history learning. Route evidence is not inventory or a fare quote."""
from datetime import date, datetime, timezone
import math

from providers.ctrip_tail_parser import normalize_airline_filters
from tail_quotes import stable_key, itinerary_key
from tail_service import validate_tail_result


def history_context(request):
    return {
        key: getattr(request, key) for key in (
            "provider", "origin", "transfer", "departure_date", "cabin", "passengers",
            "filter_scope", "departure_time_start", "departure_time_end", "max_price")
    } | {"airlines": sorted(normalize_airline_filters(request.preferred_airlines)),
         "version": 1}


def route_observations(rows, request, kept):
    """Extract structural evidence BEFORE user filters, without mutating quotes."""
    selected = {itinerary_key(row): row for row in kept}
    observations = {}
    for row in rows:
        validation = validate_tail_result(
            row, origin=request.origin, transfer=request.transfer,
            destination=row["destination"], departure_date=request.departure_date,
            preferred_airlines=[], filter_scope="first_leg")
        if validation["status"] == "invalid":
            continue
        chosen = selected.get(itinerary_key(row))
        quote_valid = bool(chosen and (chosen.get("validation") or {}).get("status") == "valid")
        key = stable_key(row.get("flight_details"))
        observations[key] = {
            "observation_key": key, "destination": row["destination"],
            "quality": validation["status"], "observed_at": row["observed_at"],
            "flight_details": row.get("flight_details") or [],
            "quote_matched": quote_valid,
            "quote_outcome": "matched" if quote_valid else "review" if chosen else "filtered",
            "savings_amount": chosen.get("savings_amount") if quote_valid else None,
            "currency": row.get("currency") or "CNY",
        }
    return list(observations.values())


def age_weight(observed_at, now):
    try:
        stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        # Ignore future evidence in offline replay; do not leak outcomes backwards.
        age = (now - stamp).total_seconds() / 86400
        return 0.0 if age < 0 or age > 90 else .5 ** (age / 7)
    except (ValueError, TypeError):
        return 0.0


def context_similarity(old, current, observed_at, now):
    if not old or not old.get("version"):
        return .1
    weight = 1.0
    for field, mismatch in (("provider", .15), ("cabin", .3), ("filter_scope", .35),
                            ("passengers", .5), ("airlines", .35),
                            ("departure_time_start", .6), ("departure_time_end", .6),
                            ("max_price", .6)):
        if old.get(field) != current.get(field):
            weight *= mismatch
    try:
        previous_day = date.fromisoformat(old["departure_date"])
        current_day = date.fromisoformat(current["departure_date"])
        observed_day = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
        if previous_day != current_day:
            weight *= .8 if previous_day.weekday() == current_day.weekday() else .4
        lead_difference = abs((previous_day - observed_day).days - (current_day - now.date()).days)
        weight *= max(.15, 1 / (1 + lead_difference / 14))
    except (ValueError, TypeError, KeyError):
        weight *= .2
    return weight


def rank_candidates(candidates, context, observations=(), outcomes=(), edges=(), legacy=(),
                    *, now=None, position=0):
    """Scores are heuristics, not calibrated probabilities or savings promises."""
    now = now or datetime.now(timezone.utc)
    stats = {code: {"route": 0., "yes": 0., "no": 0., "cost": [], "savings": [],
                    "seen": False, "reasons": set()} for code in candidates}
    # One positive observation per source query + city, not one vote per fare product.
    positive_groups = {}
    for row in observations:
        code = row["destination"]
        if code not in stats or row["quality"] != "valid":
            continue
        w = age_weight(row["observed_at"], now) * context_similarity(
            row["context"], context, row["observed_at"], now)
        group = (code, row["root_job_id"], row["source_candidate"])
        positive_groups[group] = max(positive_groups.get(group, 0), w)
    for (code, _, _), weight in positive_groups.items():
        stats[code]["route"] += weight
        if weight > .05:
            stats[code]["reasons"].add("近期有条件相近的有效联程证据")
    for row in outcomes:
        code = row["destination"]
        if code not in stats:
            continue
        st = stats[code]
        w = age_weight(row["observed_at"], now) * context_similarity(
            row["context"], context, row["observed_at"], now)
        if w <= 0:
            continue
        if row["outcome"] == "matched":
            st["yes"] += w
            st["seen"] = True
            if w > .05:
                st["reasons"].add("相近条件下曾获得已校验候选")
        elif row["outcome"] == "filtered" and row["context"].get("provider") == context["provider"]:
            # Failed user constraints reduce quote utility, not route existence.
            st["no"] += w
            st["seen"] = True
            if w > .05:
                st["reasons"].add("有路线但历史报价未满足筛选条件")
        # Incomplete / failed / omitted results are never negative route evidence.
        if (row["query_scope"] == "candidates" and row["context"].get("provider") == context["provider"]
                and row["outcome"] in {"matched", "filtered"}):
            cost = row.get("elapsed_seconds")
            if cost and math.isfinite(float(cost)) and float(cost) > 0:
                st["cost"].append((min(float(cost), 600), w))
        savings = row.get("savings_amount")
        if savings is not None and row.get("currency") == "CNY" and math.isfinite(float(savings)):
            st["savings"].append((max(0, float(savings)), w))
    # Legacy data has no comparable context or confidence: positive hints only.
    legacy_weights = {}
    for row in legacy:
        code = row["destination"]
        if code in stats and row["status"] == "matched":
            weight = .1 * age_weight(row["observed_at"], now)
            legacy_weights[code] = min(.3, legacy_weights.get(code, 0) + weight)
            if weight > .01:
                stats[code]["reasons"].add("旧任务命中，仅作弱提示")
                stats[code]["seen"] = True
    for code, weight in legacy_weights.items():
        stats[code]["route"] += weight
    edge_weights = {}
    for row in edges:
        code = row["destination"]
        if code in stats:
            weight = .1 * age_weight(row["last_observed_at"], now)
            edge_weights[code] = max(edge_weights.get(code, 0), weight)
            if weight > .01:
                stats[code]["reasons"].add("曾发现 B→C 航段，不代表当前联程可售")
    for code, weight in edge_weights.items():
        stats[code]["route"] += weight

    def average(items, fallback):
        total = sum(w for _, w in items)
        return sum(v * w for v, w in items) / total if total > 0 else fallback

    ranked = []
    for code, st in stats.items():
        cost = average(st["cost"], 10 if context["provider"] == "feizhu" else 90)
        hit = (1 + st["yes"]) / (2 + st["yes"] + st["no"])
        value = 1 + min(1., average(st["savings"], 0) / 500)
        score = (0.35 + .65 * hit) * (1 + math.log1p(st["route"])) * value / max(1, cost)
        reasons = sorted(st["reasons"])
        reasons.append("耗时采用历史估计" if st["cost"] else "耗时暂无足够历史，采用平台先验")
        ranked.append({"destination": code, "score": round(score, 6),
                       "estimated_seconds": round(cost, 2), "reasons": reasons,
                       "unexplored": not st["seen"]})
    ranked.sort(key=lambda row: (-row["score"], candidates.index(row["destination"])))
    # Reserve every fifth position (and the second initial slot) for exploration.
    if any(not row["unexplored"] for row in ranked):
        for index in range(len(ranked)):
            if (position + index) % 5 != 1:
                continue
            unseen = next((j for j in range(index, len(ranked)) if ranked[j]["unexplored"]), None)
            if unseen is not None:
                row = ranked.pop(unseen)
                row["reasons"].append("探索位：保留新城市查询机会")
                ranked.insert(index, row)
    return ranked
