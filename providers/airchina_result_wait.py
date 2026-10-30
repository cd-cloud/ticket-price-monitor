"""Bounded DOM result waiting, parallel to ctrip_result_wait, without retries."""
from __future__ import annotations

import asyncio
import copy
import math

from providers.airchina_page import snapshot_stop_state
from providers.airchina_parser import DOM_SCRIPT, parse_reference_fares


LIVE_GUARDS = (
    "verification_visible", "blocked_visible", "stale_visible",
    "notice_confirmation_visible", "loading_visible",
)


async def wait_for_results(page, route, *, expected_url, timeout_ms=10000, poll_ms=250):
    """Require two identical *whole* projections, not merely a flight count.

    No navigation, scrolling, dialog actions or response interception. Empty or
    changing lists are inconclusive, never evidence of no flights/full coverage.
    The poll cap also bounds mock/fast clocks; the wall clock bounds browser I/O.
    Raw snapshots stay in memory and never enter diagnostic reports.
    """
    if not 100 <= timeout_ms <= 60000 or not 1 <= poll_ms <= timeout_ms:
        raise ValueError("Invalid Air China result wait budget.")
    polls = 0

    def finish(state, reason, preview=None):
        return {
            "state": state,
            "wait_evidence": {"reason": reason, "polls": polls, "coverage_complete": False},
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
                if stop:
                    return finish(stop, stop)
                preview = parse_reference_fares(snapshot, route)
                ready = (
                    all(snapshot.get(field) is False for field in LIVE_GUARDS)
                    and preview["status"] == "reference_fares_observed"
                )
                if ready and snapshot == previous:
                    return finish("reference_fares_observed", "stable_visible_results", preview)
                previous = copy.deepcopy(snapshot) if ready else None
                if index + 1 < max(2, math.ceil(timeout_ms / poll_ms)):
                    await page.wait_for_timeout(poll_ms)
    except TimeoutError:
        return finish("query_page_unverified", "result_wait_timeout")
    return finish("query_page_unverified", "result_wait_budget")
