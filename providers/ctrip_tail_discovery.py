"""Ctrip tail-discovery page parsing and diagnostics helpers.

The main BrowserAutomation class still owns browser lifecycle and network
helpers. This module owns page-level tail-discovery fallback parsing and local
diagnostic artifacts so the coordinator can keep slimming down over time.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from adaptive_selectors import observe_selector_candidates
from providers.ctrip_support import ctrip_city_code
from html_text_cleaner import clean_html_text
from providers import ctrip
from providers import ctrip_itinerary
from providers import ctrip_network_parser
from providers import ctrip_tail_parser


LOGGER = logging.getLogger(__name__)


def response_store_has_flight_itineraries(response_store: list[dict], *, data_walker: Any) -> bool:
    for item in response_store:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        for _data_node in data_walker(payload):
            return True
    return False


def extract_tail_match_from_network(
    automation: Any,
    response_store: list[dict],
    *,
    origin: str,
    transfer: str,
    destination: str,
    preferred_airlines: list[str] | None = None,
) -> dict[str, object] | None:
    best_match: tuple[float, dict, dict] | None = None
    for item in response_store:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        for data_node in ctrip_itinerary.walk_data_nodes(payload):
            itineraries = data_node.get("flightItineraryList")
            if not isinstance(itineraries, list):
                continue
            for itinerary in itineraries:
                if not isinstance(itinerary, dict):
                    continue
                for price in itinerary.get("priceList") or []:
                    if not isinstance(price, dict):
                        continue
                    total = ctrip_itinerary.total_price(price)
                    if total is None:
                        continue
                    details = ctrip_itinerary.build_itinerary_details(itinerary, price)
                    if not details_match_tail_route(details, origin, transfer, destination):
                        continue
                    if not ctrip_tail_parser.details_match_airlines(details, preferred_airlines):
                        continue
                    if best_match is None or total < best_match[0]:
                        best_match = (total, itinerary, price)

    if best_match is None:
        return None
    total_price, itinerary, price = best_match
    return {
        "total_price": total_price,
        "details": ctrip_itinerary.build_itinerary_details(itinerary, price),
        "detail_source": "network_exact",
        "detail_quality": "complete",
    }


def network_diagnostics(
    automation: Any,
    response_store: list[dict],
    *,
    origin: str,
    transfer: str,
    destination: str,
    preferred_airlines: list[str] | None = None,
) -> str:
    itinerary_count = 0
    priced_count = 0
    route_like_count = 0
    transfer_like_count = 0
    airline_filtered_count = 0
    top_routes: list[str] = []
    sample_urls: list[str] = []
    seen_routes: set[str] = set()
    for item in response_store:
        url = str(item.get("url") or "")
        if url and len(sample_urls) < 4:
            sample_urls.append(url[:120])
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        for data_node in ctrip_itinerary.walk_data_nodes(payload):
            itineraries = data_node.get("flightItineraryList")
            if not isinstance(itineraries, list):
                continue
            for itinerary in itineraries:
                if not isinstance(itinerary, dict):
                    continue
                itinerary_count += 1
                for price in itinerary.get("priceList") or []:
                    if not isinstance(price, dict):
                        continue
                    total = ctrip_itinerary.total_price(price)
                    if total is None:
                        continue
                    priced_count += 1
                    details = ctrip_itinerary.build_itinerary_details(itinerary, price)
                    summary = summarize_tail_details(details)
                    if summary and summary not in seen_routes and len(top_routes) < 5:
                        top_routes.append(f"{summary} ¥{total:.0f}")
                        seen_routes.add(summary)
                    route_like = bool(details) and code_matches_target(
                        details[0].get("origin") or details[0].get("departure_airport"),
                        origin,
                    ) and code_matches_target(
                        details[-1].get("destination") or details[-1].get("arrival_airport"),
                        destination,
                    )
                    if route_like:
                        route_like_count += 1
                    if details_match_tail_route(details, origin, transfer, destination):
                        transfer_like_count += 1
                        if preferred_airlines and not ctrip_tail_parser.details_match_airlines(details, preferred_airlines):
                            airline_filtered_count += 1
    if itinerary_count == 0:
        suffix = f" sample_urls={' | '.join(sample_urls)}" if sample_urls else ""
        return f"Network diagnostics: {len(response_store)} captured responses, no flightItineraryList found.{suffix}"
    parts = [
        f"Network diagnostics: itineraries={itinerary_count}",
        f"priced={priced_count}",
        f"same_route={route_like_count}",
        f"via_{transfer}={transfer_like_count}",
    ]
    if preferred_airlines:
        parts.append(f"airline_filtered={airline_filtered_count}")
    if top_routes:
        parts.append("sample_routes=" + " / ".join(top_routes))
    return ", ".join(parts) + "."


def summarize_tail_details(details: list[dict[str, object]]) -> str:
    if not details:
        return ""
    points: list[str] = []
    first = details[0]
    first_origin = first.get("origin") or first.get("departure_airport")
    if first_origin:
        points.append(str(first_origin).upper())
    for detail in details:
        arrival = detail.get("destination") or detail.get("arrival_airport")
        if arrival:
            points.append(str(arrival).upper())
    airlines = ",".join(
        str(detail.get("airline") or "").upper()
        for detail in details
        if detail.get("airline")
    )
    route = "->".join(points)
    return f"{route}({airlines})" if airlines else route


def details_match_tail_route(
    details: list[dict[str, object]],
    origin: str,
    transfer: str,
    destination: str,
) -> bool:
    if len(details) < 2:
        return False
    first = details[0]
    last = details[-1]
    if not code_matches_target(first.get("origin") or first.get("departure_airport"), origin):
        return False
    if not code_matches_target(last.get("destination") or last.get("arrival_airport"), destination):
        return False

    for left, right in zip(details, details[1:]):
        arrival = left.get("destination") or left.get("arrival_airport")
        departure = right.get("origin") or right.get("departure_airport")
        if code_matches_target(arrival, transfer) or code_matches_target(departure, transfer):
            return True
    return False


def code_matches_target(value: object, target: str) -> bool:
    if not value:
        return False
    source = str(value).upper()
    normalized_target = str(target).upper()
    return source == normalized_target or ctrip_city_code(source) == ctrip_city_code(normalized_target)


async def start_trace(
    context: Any,
    *,
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
) -> bool:
    try:
        await context.tracing.start(
            name=f"ctrip_tail_{origin}_{transfer}_{destination}_{departure_date}",
            screenshots=True,
            snapshots=True,
            sources=True,
        )
        return True
    except Exception as exc:
        LOGGER.info("failed to start ctrip tail trace: %s", exc)
        return False


async def stop_trace(automation: Any, context: Any, *, trace_started: bool):
    if not trace_started:
        return None
    trace_dir = automation.app_config.runtime_dir / "tail_traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"ctrip_tail_{datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}.zip"
    try:
        await context.tracing.stop(path=str(trace_path))
        return trace_path
    except Exception as exc:
        LOGGER.info("failed to stop ctrip tail trace: %s", exc)
        try:
            await context.tracing.stop()
        except Exception:
            pass
        return None


async def goto_tail_results(automation: Any, provider: Any, route: Any, page: Any) -> None:
    await automation._throttle(provider)
    search_url = automation._build_search_url(provider, route)
    await page.goto(search_url, wait_until="domcontentloaded", timeout=provider.timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=max(5000, provider.timeout_ms // 3))
    except Exception:
        pass
    if not ctrip.is_ctrip_results_url(page.url):
        raise RuntimeError(f"Ctrip direct result URL did not open for route {route.route_key}. Current URL: {page.url}")
    await page.wait_for_timeout(3500)
    await automation._prepare_results_page(provider, page, route)
    await automation._ensure_ctrip_results_date(page, route, provider.timeout_ms)
    await automation._wait_for_results(provider, page)


async def warm_tail_result_data(automation: Any, page: Any, response_store: list[dict]) -> None:
    if automation._response_store_has_flight_itineraries(response_store):
        return
    for _ in range(3):
        await page.wait_for_timeout(900)
        if automation._response_store_has_flight_itineraries(response_store):
            return

    previous_scroll = -1
    stalled_rounds = 0
    for _index in range(10):
        try:
            state = await page.evaluate(
                """() => ({
                    y: Math.round(window.scrollY || 0),
                    height: Math.round(document.documentElement.scrollHeight || document.body.scrollHeight || 0),
                    viewport: Math.round(window.innerHeight || 0),
                    cards: document.querySelectorAll('.list-content-item-transit, .search-result-item, .box-row').length,
                    loading: /加载|loading|查询中|搜索中/i.test(document.body?.innerText || '')
                })"""
            )
        except Exception:
            state = {}
        if automation._response_store_has_flight_itineraries(response_store):
            return
        if int(state.get("cards") or 0) > 0 and not bool(state.get("loading")):
            await page.wait_for_timeout(1200)
            if automation._response_store_has_flight_itineraries(response_store):
                return
        scroll_y = int(state.get("y") or 0)
        scroll_height = int(state.get("height") or 0)
        viewport = int(state.get("viewport") or 0)
        can_scroll = scroll_height > viewport + scroll_y + 80
        if not can_scroll:
            await page.wait_for_timeout(1200)
            continue
        if scroll_y == previous_scroll:
            stalled_rounds += 1
        else:
            stalled_rounds = 0
        if stalled_rounds >= 2:
            return
        previous_scroll = scroll_y
        try:
            await page.evaluate("window.scrollBy(0, Math.max(300, Math.floor(window.innerHeight * 0.65)))")
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        if automation._response_store_has_flight_itineraries(response_store):
            return


async def extract_tail_match_from_page(
    page: Any,
    *,
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
    cabin: str,
    preferred_airlines: list[str] | None = None,
) -> dict[str, object] | None:
    cards = await ctrip_tail_parser.extract_transit_cards_from_page(page)
    dom_match = ctrip_tail_parser.find_tail_match_from_cards(
        cards,
        origin=origin,
        transfer=transfer,
        destination=destination,
        departure_date=departure_date,
        cabin=cabin,
        preferred_airlines=preferred_airlines,
    )
    if dom_match is not None:
        return dom_match

    try:
        text = await page.locator("body").inner_text()
    except Exception:
        return None
    blocks = await ctrip_tail_parser.text_blocks_from_page(page, text)
    text_match = ctrip_tail_parser.find_tail_match_from_text_blocks(
        blocks,
        origin=origin,
        transfer=transfer,
        destination=destination,
        departure_date=departure_date,
        cabin=cabin,
        preferred_airlines=preferred_airlines,
    )
    if text_match is not None:
        return text_match

    try:
        cleaned = clean_html_text(await page.content(), url=page.url)
    except Exception:
        cleaned = {}
    cleaned_text = str(cleaned.get("text") or "")
    if cleaned_text and cleaned_text != text:
        cleaned_blocks = ctrip_tail_parser.split_result_blocks(cleaned_text)
        cleaned_match = ctrip_tail_parser.find_tail_match_from_text_blocks(
            cleaned_blocks,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
            cabin=cabin,
            preferred_airlines=preferred_airlines,
        )
        if cleaned_match is not None:
            cleaned_match["detail_source"] = "cleaned_text_fallback"
            cleaned_match["detail_quality"] = "needs_review"
            cleaned_match["text_cleaner"] = {
                "engine": cleaned.get("engine"),
                "available": cleaned.get("available"),
                "length": cleaned.get("length"),
            }
            return cleaned_match
    return None


async def extract_tail_page_diagnostics(
    automation: Any,
    page: Any,
    *,
    response_store: list[dict] | None = None,
    origin: str = "",
    transfer: str,
    destination: str = "",
    preferred_airlines: list[str] | None = None,
) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception as exc:
        return f"Could not read result page text: {exc}"

    try:
        cleaned = clean_html_text(await page.content(), url=page.url)
    except Exception:
        cleaned = {}
    cleaned_text = str(cleaned.get("text") or "")
    cards = await ctrip_tail_parser.extract_transit_cards_from_page(page)
    blocks = await ctrip_tail_parser.text_blocks_from_page(page, text)
    for block in ctrip_tail_parser.split_result_blocks(cleaned_text):
        if block and block not in blocks:
            blocks.append(block)
    preview_text = cleaned_text or text
    message = ctrip_tail_parser.build_diagnostics(
        cards,
        blocks,
        transfer=transfer,
        page_preview=" ".join(preview_text.split())[:260],
    )
    if cleaned:
        message = (
            f"{message} | Text cleaner: {cleaned.get('engine')} "
            f"available={cleaned.get('available')} length={cleaned.get('length')}."
        )
    network_message = automation._ctrip_tail_network_diagnostics(
        response_store or [],
        origin=origin,
        transfer=transfer,
        destination=destination,
        preferred_airlines=preferred_airlines,
    )
    if network_message:
        message = f"{message} | {network_message}"
    return message


async def dump_tail_diagnostics(
    automation: Any,
    page: Any,
    *,
    response_store: list[dict],
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
) -> dict[str, str] | None:
    try:
        dump_dir = automation.app_config.runtime_dir / "tail_diagnostics"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        prefix = dump_dir / f"ctrip_tail_{origin}_{transfer}_{destination}_{departure_date}_{stamp}"
        screenshot_path = prefix.with_suffix(".png")
        html_path = prefix.with_suffix(".html")
        text_path = prefix.with_suffix(".txt")
        clean_text_path = prefix.with_suffix(".clean.txt")
        json_path = prefix.with_suffix(".json")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
        try:
            body_text = await page.locator("body").inner_text(timeout=5000)
            text_path.write_text(body_text, encoding="utf-8")
        except Exception:
            body_text = ""
            text_path.write_text("", encoding="utf-8")
        cleaned = clean_html_text(html, url=page.url)
        clean_text_path.write_text(str(cleaned.get("text") or ""), encoding="utf-8")
        try:
            selector_observations = await observe_selector_candidates(
                page,
                ctrip_tail_parser.TRANSIT_CARD_SELECTORS,
            )
        except Exception as exc:
            selector_observations = [{"error": str(exc)}]
        payload = {
            "route": {
                "origin": origin,
                "transfer": transfer,
                "destination": destination,
                "departure_date": departure_date,
            },
            "page": {
                "url": page.url,
                "title": await page.title(),
            },
            "text_cleaner": {
                "available": cleaned.get("available"),
                "engine": cleaned.get("engine"),
                "reason": cleaned.get("reason"),
                "length": cleaned.get("length"),
                "clean_text_path": str(clean_text_path),
                "raw_text_length": len(body_text),
            },
            "adaptive_selectors": selector_observations,
            "network": {
                "captured_count": len(response_store),
                "has_flight_itinerary_list": automation._response_store_has_flight_itineraries(response_store),
                "responses": [
                    ctrip_network_parser.summarize_response_for_diagnostics(item)
                    for item in response_store[-120:]
                ],
            },
            "captured_at": datetime.utcnow().isoformat() + "Z",
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "json_path": str(json_path),
            "screenshot_path": str(screenshot_path),
            "html_path": str(html_path),
            "text_path": str(text_path),
            "clean_text_path": str(clean_text_path),
        }
    except Exception as exc:
        LOGGER.info("failed to dump ctrip tail diagnostics: %s", exc)
        return None
