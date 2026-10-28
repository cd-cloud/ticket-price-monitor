"""Bounded progressive result collection, never a full-inventory assertion."""
from providers.ctrip_itinerary import walk_data_nodes


def flight_progress(responses):
    count = itineraries = fares = 0
    for item in responses:
        if item.get("status", 200) != 200:
            continue
        nodes = list(walk_data_nodes(item.get("payload")))
        if not nodes:
            continue
        count += 1
        for node in nodes:
            rows = [row for row in node["flightItineraryList"] if isinstance(row, dict)]
            itineraries += len(rows)
            fares += sum(len(row.get("priceList") or []) for row in rows)
    return count, itineraries, fares


async def wait_for_progressive_results(page, responses, *, max_polls=12, quiet_polls=3, poll_ms=1000):
    """Allow late batches, lazy scrolling and decoding, within a finite poll budget."""
    if max_polls < 1 or quiet_polls < 1 or poll_ms < 1:
        raise ValueError("Result wait budgets must be positive.")
    previous = None
    quiet = stalled = 0
    old_y = None
    progress = flight_progress(responses)
    cached = getattr(responses, "loading_evidence", None)

    def finish(reason, polls):
        report = {
            "reason": reason, "polls": polls, "progress": list(progress),
            "inflight": getattr(responses, "inflight", 0),
            "query_generation": getattr(responses, "generation", None),
            "coverage_complete": False,
        }
        if hasattr(responses, "loading_evidence"):
            responses.loading_evidence = report
        return report

    for index in range(max_polls):
        try:
            state = await page.evaluate(
                """() => ({
                    y: Math.round(window.scrollY || 0),
                    height: Math.round(document.documentElement.scrollHeight || document.body.scrollHeight || 0),
                    viewport: Math.round(window.innerHeight || 0),
                    cards: document.querySelectorAll('.list-content-item-transit, .search-result-item, .box-row').length,
                    loading: [...document.querySelectorAll('.loading, .loading-mask, [aria-busy="true"]')]
                        .some(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden')
                        || /加载中|正在加载|查询中|搜索中|loading\\.\\.\\./i.test(document.body?.innerText || '')
                })"""
            )
        except Exception:
            state = None
        progress = flight_progress(responses)
        pending = getattr(responses, "inflight", 0)
        known_state = isinstance(state, dict) and {"y", "height", "viewport", "cards", "loading"} <= state.keys()
        state = state if known_state else {}
        y = int(state.get("y") or 0)
        can_scroll = known_state and int(state["height"]) > int(state["viewport"]) + y + 80
        ready = known_state and not state["loading"] and not pending
        has_data = progress[1] > 0 or int(state.get("cards") or 0) > 0
        signature = (progress, state.get("cards"), state.get("height"), y)
        if (index == 0 and cached and cached.get("reason") == "quiet"
                and cached.get("query_generation") == getattr(responses, "generation", None)
                and cached.get("progress") == list(progress) and ready and has_data and not can_scroll):
            return finish("quiet", 0)
        if ready and has_data and not can_scroll and signature == previous:
            quiet += 1
        else:
            quiet = 0
        if quiet >= quiet_polls:
            return finish("quiet", index + 1)
        if can_scroll:
            stalled = stalled + 1 if old_y == y and signature == previous else 0
            if stalled >= 3 and ready:
                return finish("scroll_stalled", index + 1)
            try:
                await page.evaluate("window.scrollBy(0, Math.max(300, Math.floor(window.innerHeight * 0.65)))")
            except Exception:
                pass
        old_y, previous = y, signature
        if index < max_polls - 1:
            await page.wait_for_timeout(poll_ms)
    return finish("wait_budget", max_polls)
