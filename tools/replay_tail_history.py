"""Offline ranking comparison; requires fully labelled candidate sets.

Usage: python tools/replay_tail_history.py cases.json --budget 3
Input: [{"observed_at": ISO timestamp, "request": tail request, "candidates": [...],
         "gold": {"CAN": {"outcome": "matched", "elapsed_seconds": 10}, ...}}]
Both strategies share strictly earlier observed history. A missing counterfactual
is NOT a failed query: that case is excluded from the comparison.
No providers, credentials or writable databases are used.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tail_history import history_context, rank_candidates
from tail_service import normalize_tail_discovery_request


def replay(cases, budget=3):
    if not isinstance(budget, int) or budget < 1:
        raise ValueError("budget must be a positive integer")
    history, evaluations = [], []
    skipped = 0
    for case in sorted(cases, key=lambda item: item["observed_at"]):
        now = datetime.fromisoformat(case["observed_at"].replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("observed_at requires an explicit timezone")
        candidates = list(dict.fromkeys(case["candidates"]))
        context = history_context(normalize_tail_discovery_request({**case["request"], "candidates": candidates}))
        # Only strictly earlier scenarios may inform this case, even at identical timestamps.
        prior = [row for row in history if datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) < now]
        ordered = [row["destination"] for row in rank_candidates(
            candidates, context, outcomes=prior, now=now)]
        gold = case.get("gold") or {}
        if all(code in gold for code in candidates):
            fixed, ranked = candidates[:budget], ordered[:budget]
            evaluations.append({
                "observed_at": case["observed_at"], "fixed": fixed, "ranked": ranked,
                "candidate_queries": len(fixed),
                "fixed_valid": sum(gold[code].get("outcome") == "matched" for code in fixed),
                "ranked_valid": sum(gold[code].get("outcome") == "matched" for code in ranked),
            })
        else:
            skipped += 1
        for code, label in gold.items():
            if code not in candidates:
                continue
            history.append({"destination": code, "observed_at": case["observed_at"],
                            "context": context, "query_scope": "candidates",
                            "outcome": label.get("outcome", "unknown"),
                            "elapsed_seconds": label.get("elapsed_seconds"),
                            "savings_amount": label.get("savings_amount"),
                            "currency": label.get("currency", "CNY")})
    queries = sum(row["candidate_queries"] for row in evaluations)
    return {
        "evaluated_cases": len(evaluations), "skipped_incomplete_cases": skipped,
        "candidate_queries_per_strategy": queries,
        "fixed_valid_per_100_candidate_queries": (
            round(sum(row["fixed_valid"] for row in evaluations) * 100 / queries, 2) if queries else None),
        "ranked_valid_per_100_candidate_queries": (
            round(sum(row["ranked_valid"] for row in evaluations) * 100 / queries, 2) if queries else None),
        "cases": evaluations,
        "limitations": "共享严格早于本案例的历史；只比较有完整标签的候选集。每候选一次搜索，不含初始基准或补查调用；不等于线上节省效果。",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--budget", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(replay(json.loads(args.cases.read_text()), args.budget), ensure_ascii=False, indent=2))
