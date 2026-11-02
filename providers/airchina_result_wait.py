"""Bounded DOM result waiting, parallel to ctrip_result_wait, without retries."""
from __future__ import annotations

import asyncio
import copy
import math
import time

from providers.airchina_page import snapshot_stop_state
from providers.airchina_parser import DOM_SCRIPT, parse_reference_fares


LIVE_GUARDS = (
    "verification_visible", "blocked_visible", "stale_visible",
    "notice_confirmation_visible", "loading_visible",
    "no_results_visible",
)

CONTEXT_FIELDS = ("route_text", "date_text", "trip_type", "passengers_text", "price_notice")


def _diagnostic(snapshot, preview, stop):
    """Only fixed keys/enums, booleans and counts; never page strings or URLs."""
    data = snapshot if isinstance(snapshot, dict) else {}
    rows = data.get("rows")
    return {
        "parser_reason": stop or preview["reason"],
        "visible_rows": len(rows) if isinstance(rows, list) else None,
        "header_count": data.get("header_count") if type(data.get("header_count")) is int else None,
        "context_present": {key: bool(isinstance(data.get(key), str) and data[key].strip())
                            for key in CONTEXT_FIELDS},
        "guards": {key: data[key] if type(data.get(key)) is bool else None for key in LIVE_GUARDS},
        "accepted_offers": len(preview.get("offers", [])),
        **{key: preview.get(key, 0) for key in ("rejected_rows", "filtered_offers", "conflicting_offers")},
        "row_rejection_reasons": preview.get("row_rejection_reasons", {}),
    }


async def wait_for_results(page, route, *, expected_url, timeout_ms=30000, poll_ms=500,
                           diagnostic_callback=None):
    """Require two matching validated fare samples, not entire volatile DOM.

    No navigation, scrolling, dialog actions or response interception. Empty or
    changing lists are inconclusive, never evidence of no flights/full coverage.
    The poll cap also bounds mock/fast clocks; the wall clock bounds browser I/O.
    Raw snapshots stay in memory and never enter diagnostic reports.
    """
    if not 100 <= timeout_ms <= 60000 or not 1 <= poll_ms <= timeout_ms:
        raise ValueError("Invalid Air China result wait budget.")
    polls = 0
    started = time.monotonic()
    last = None
    transitions = []
    stable_samples = 0

    def evidence(reason):
        return {
            "reason": reason, "polls": polls, "coverage_complete": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "budget_ms": timeout_ms, "poll_ms": poll_ms, "stable_samples": stable_samples,
            "last_observation": copy.deepcopy(last),
            "transitions": copy.deepcopy(transitions),
        }

    def finish(state, reason, preview=None):
        diagnostic = evidence(reason)
        if diagnostic_callback:
            diagnostic_callback(diagnostic)
        return {
            "state": state,
            "wait_evidence": diagnostic,
            "reference_preview": preview or {
                "status": "unverified", "reason": reason, "offers": [],
                "quote_saved": False, "parser_ready": False, "coverage_complete": False,
            },
        }

    previous = None
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            for index in range(max(2, math.ceil(timeout_ms / poll_ms))):
                snapshot = await page.evaluate("(" + DOM_SCRIPT + "\n)()")
                polls += 1
                stop = snapshot_stop_state(snapshot, expected_url)
                preview = parse_reference_fares(snapshot, route) if not stop else {"reason": stop}
                observation = _diagnostic(snapshot, preview, stop)
                changed = observation != last
                last = observation
                if changed:
                    transitions.append({"poll": polls, "parser_reason": last["parser_reason"],
                                        "visible_rows": last["visible_rows"]})
                    transitions[:] = transitions[-8:]
                if stop:
                    stable_samples = 0
                    return finish(stop, stop)
                ready = (
                    all(snapshot.get(field) is False for field in LIVE_GUARDS)
                    and preview["status"] == "reference_fares_observed"
                )
                # Every poll independently revalidates URL, visible route/date,
                # passenger count, price scope and cabin headings. Only normalized
                # offers and list-quality evidence participate in stability:
                # unrelated product expansions/animations must not reset it.
                key = {"preview": preview, "visible_rows": last["visible_rows"]} if ready else None
                stable_samples = stable_samples + 1 if ready and key == previous else int(ready)
                if stable_samples >= 2:
                    return finish("reference_fares_observed", "stable_visible_results", preview)
                previous = copy.deepcopy(key)
                if changed and diagnostic_callback:
                    diagnostic_callback(evidence("observing"))
                if index + 1 < max(2, math.ceil(timeout_ms / poll_ms)):
                    await page.wait_for_timeout(poll_ms)
    except TimeoutError:
        return finish("query_page_unverified", "result_wait_timeout")
    return finish("query_page_unverified", "result_wait_budget")
