import logging
import random
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from playwright.async_api import Page

from config_manager import ProviderConfig, RouteQuery
from providers import ctrip_page
from providers import ctrip_result
from providers.base import ProviderQueryOutcome
from providers.ctrip_support import ctrip_city_code, route_prefers_international_entry
from providers.human_actions import hover_then_click, random_scroll_and_pause


name = "ctrip"
supports_multi_city = True
supports_tail_discovery = True

CTRIP_CHANNEL_URL = "https://flights.ctrip.com/online/channel/domestic"
CTRIP_FLIGHTS_HOME_URL = "https://flights.ctrip.com"
CTRIP_RESULT_URL_MARKERS = ("/booking/", "/online/list/")
CTRIP_ONLINE_CABIN_PARAMS = {
    "economy_plus": "y_s",
    "business_first": "c_f",
    "economy": "y",
    "premium_economy": "s",
    "business": "c",
    "first": "f",
}
CTRIP_LANDING_PAGES = [
    "https://flights.ctrip.com/online/channel/domestic",
    "https://flights.ctrip.com/online/channel/international",
    "https://flights.ctrip.com",
    "https://www.ctrip.com/flights",
]
LOGGER = logging.getLogger(__name__)


async def query(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
) -> ProviderQueryOutcome:
    if route.is_multi_city:
        offer, final_url, title = await query_multi_city_offer(runtime, provider, route, page, response_store)
        return ctrip_result.build_multi_city_outcome(
            runtime,
            route,
            offer,
            final_url,
            title,
            response_store,
        )

    offer, final_url, title = await query_single_route_offer(runtime, provider, route, page, response_store)
    await runtime.ensure_ctrip_results_date(page, route, provider.timeout_ms)
    return ctrip_result.build_single_route_outcome(route, offer, final_url, title, response_store)


async def query_single_route_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
):
    return await query_native_single_route_offer(runtime, provider, route, page, response_store)


async def query_native_single_route_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
):
    if route.return_date:
        return await query_native_round_trip_offer(runtime, provider, route, page, response_store)

    await runtime.throttle(provider)
    direct_result = await load_direct_route_offer(runtime, provider, route, page, response_store)
    if direct_result is not None:
        return direct_result

    landing_url = (
        CTRIP_FLIGHTS_HOME_URL
        if route_prefers_international_entry(route)
        else random.choice(CTRIP_LANDING_PAGES)
    )
    await page.goto(
        landing_url,
        wait_until="domcontentloaded",
        timeout=provider.timeout_ms,
    )
    await page.wait_for_timeout(3000)
    await runtime.activate_ctrip_one_way(page, provider.timeout_ms)
    await runtime.prepare_ctrip_one_way_form(page, route, provider.timeout_ms)
    try:
        await submit_search(page, provider.timeout_ms)
        await ensure_results_page(page, provider.timeout_ms, route)
        await ensure_results_cabin_url(page, route, provider.timeout_ms)
    except Exception as exc:
        await runtime.assist_page(
            page,
            "ctrip_one_way_submit",
            "Ctrip one-way search did not reach the result page. Diagnose whether a date picker, city picker, or validation overlay is blocking submit.",
            {"route_key": route.route_key, "error": str(exc)},
        )
        raise
    await runtime.wait_for_results(provider, page)
    await runtime.warm_ctrip_result_data(page, response_store)
    offer = await runtime.extract_offer(provider, route, page, response_store)
    return offer, page.url, await page.title()


async def query_native_round_trip_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
):
    await runtime.throttle(provider)
    direct_result = await load_direct_route_offer(runtime, provider, route, page, response_store)
    if direct_result is not None:
        return direct_result

    landing_url = (
        CTRIP_FLIGHTS_HOME_URL
        if route_prefers_international_entry(route)
        else random.choice(CTRIP_LANDING_PAGES)
    )
    await page.goto(
        landing_url,
        wait_until="domcontentloaded",
        timeout=provider.timeout_ms,
    )
    await page.wait_for_timeout(3000)
    await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
    await runtime.activate_ctrip_round_trip(page, provider.timeout_ms)
    await runtime.prepare_ctrip_round_trip_form(page, route, provider.timeout_ms)
    try:
        await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
        await submit_search(page, provider.timeout_ms)
        await ensure_results_page(page, provider.timeout_ms, route)
        await ensure_results_cabin_url(page, route, provider.timeout_ms)
    except Exception as exc:
        await runtime.assist_page(
            page,
            "ctrip_round_trip_submit",
            "Ctrip round-trip search did not reach the result page. Diagnose whether a login popup, date picker, city picker, or validation overlay is blocking submit.",
            {"route_key": route.route_key, "error": str(exc)},
        )
        raise
    await runtime.wait_for_results(provider, page)
    await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
    await runtime.warm_ctrip_result_data(page, response_store)
    try:
        offer = await runtime.extract_offer(provider, route, page, response_store)
    except Exception:
        if await ctrip_page.has_login_overlay(page):
            raise RuntimeError(login_required_message(route, page))
        if await ctrip_page.has_no_results_notice(page):
            raise RuntimeError(no_flights_message(route, page))
        raise
    return offer, page.url, await page.title()


async def open_direct_results_page(page: Page, route: RouteQuery, timeout_ms: int) -> bool:
    try:
        await page.goto(build_online_results_url(route), wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(3500)
        return is_ctrip_results_url(page.url)
    except Exception:
        return False


async def load_direct_route_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
):
    if not await open_direct_results_page(page, route, provider.timeout_ms):
        return None
    await runtime.wait_for_results(provider, page)
    await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
    await runtime.warm_ctrip_result_data(page, response_store)
    try:
        offer = await runtime.extract_offer(provider, route, page, response_store)
    except Exception:
        if await ctrip_page.has_login_overlay(page):
            LOGGER.info("Ctrip direct results page is blocked by login overlay; retrying through search form route=%s", route.route_key)
            return None
        if await ctrip_page.has_no_results_notice(page):
            LOGGER.info(
                "Ctrip direct results page returned no flights; retrying through search form route=%s url=%s",
                route.route_key,
                page.url,
            )
            return None
        return None
    return offer, page.url, await page.title()


def build_online_results_url(route: RouteQuery) -> str:
    origin = ctrip_city_code(route.origin).lower()
    destination = ctrip_city_code(route.destination).lower()
    cabin = CTRIP_ONLINE_CABIN_PARAMS.get((route.cabin or "").lower(), "y")
    if route.return_date:
        route_part = f"round-{origin}-{destination}"
        date_part = f"{quote(route.departure_date)}_{quote(route.return_date)}"
    else:
        route_part = f"oneway-{origin}-{destination}"
        date_part = quote(route.departure_date)
    return (
        f"https://flights.ctrip.com/online/list/{route_part}"
        f"?depdate={date_part}&cabin={quote(cabin)}&adult={route.passengers}&child=0&infant=0"
    )


def no_flights_message(route: RouteQuery, page: Page) -> str:
    return (
        f"Ctrip returned no flights for route={route.route_key}; "
        f"final_url={page.url}; no reliable fare will be saved."
    )


def login_required_message(route: RouteQuery, page: Page) -> str:
    return (
        f"Ctrip login overlay blocked reliable results for route={route.route_key}; "
        f"final_url={page.url}; no reliable fare will be saved."
    )


async def query_tail_candidate_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
    *,
    origin_already_set: bool = False,
):
    await runtime.throttle(provider)
    direct_result = await load_direct_route_offer(runtime, provider, route, page, response_store)
    if direct_result is not None:
        return direct_result

    origin_already_set = await ensure_single_route_search_form(
        runtime,
        provider,
        route,
        page,
        origin_already_set=origin_already_set,
    )
    await runtime.prepare_ctrip_one_way_form(
        page,
        route,
        provider.timeout_ms,
        skip_origin=origin_already_set,
    )
    try:
        await submit_search(page, provider.timeout_ms)
        await ensure_results_page(page, provider.timeout_ms, route)
        await ensure_results_cabin_url(page, route, provider.timeout_ms)
    except Exception as exc:
        await runtime.assist_page(
            page,
            "ctrip_tail_submit",
            "Ctrip tail-discovery search did not reach the result page after filling origin, destination, and date on the form.",
            {"route_key": route.route_key, "error": str(exc), "origin_already_set": origin_already_set},
        )
        raise
    await runtime.wait_for_results(provider, page)
    await runtime.warm_ctrip_result_data(page, response_store)
    offer = await runtime.extract_offer(provider, route, page, response_store)
    return offer, page.url, await page.title()


async def ensure_single_route_search_form(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    *,
    origin_already_set: bool = False,
) -> bool:
    if is_ctrip_results_url(page.url):
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=provider.timeout_ms)
            await page.wait_for_timeout(1200)
        except Exception:
            pass

    if not await has_search_form(page):
        await runtime.throttle(provider)
        landing_url = (
            CTRIP_FLIGHTS_HOME_URL
            if route_prefers_international_entry(route)
            else random.choice(CTRIP_LANDING_PAGES)
        )
        await page.goto(
            landing_url,
            wait_until="domcontentloaded",
            timeout=provider.timeout_ms,
        )
        await page.wait_for_timeout(3000)
        origin_already_set = False

    await runtime.activate_ctrip_one_way(page, provider.timeout_ms)
    if not origin_already_set:
        await page.wait_for_timeout(500)
    return origin_already_set


async def has_search_form(page: Page) -> bool:
    try:
        return await page.locator("form#searchForm input[name='owACity']").first.count() > 0
    except Exception:
        return False


async def query_multi_city_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
):
    if not route.segments or len(route.segments) < 2:
        raise RuntimeError("Native multi-city query requires at least 2 segments.")

    round_trip_route = as_native_round_trip_route(route)
    if round_trip_route is not None:
        LOGGER.info(
            "Ctrip symmetric two-segment multi-city route will use native round-trip flow route=%s",
            route.route_key,
        )
        return await query_native_round_trip_offer(runtime, provider, round_trip_route, page, response_store)

    await runtime.throttle(provider)
    direct_result = await load_direct_multi_city_offer(runtime, provider, route, page, response_store)
    if direct_result is not None:
        return direct_result

    await page.goto(
        CTRIP_FLIGHTS_HOME_URL if route_prefers_international_entry(route) else CTRIP_CHANNEL_URL,
        wait_until="domcontentloaded",
        timeout=provider.timeout_ms,
    )
    await page.wait_for_timeout(3000)
    await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
    await runtime.activate_ctrip_multi_city(page, provider.timeout_ms)
    await runtime.prepare_ctrip_multi_city_form(page, route.segments, provider.timeout_ms)
    try:
        await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
        await submit_search(page, provider.timeout_ms)
        await ensure_results_page(page, provider.timeout_ms, route)
        await ensure_results_cabin_url(page, route, provider.timeout_ms)
    except Exception as exc:
        await runtime.assist_page(
            page,
            "ctrip_multi_city_submit",
            "Ctrip multi-city search did not reach the result page. Diagnose whether a date picker, city picker, or validation overlay is blocking submit.",
            {"route_key": route.route_key, "segments": [segment.to_dict() for segment in route.segments], "error": str(exc)},
        )
        raise
    await runtime.wait_for_results(provider, page)
    await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
    await runtime.warm_ctrip_result_data(page, response_store)
    network_match = runtime.extract_ctrip_multi_city_match_from_network(response_store, route)
    if network_match is not None:
        offer = runtime.offer_details_type(price=network_match["total_price"])
    else:
        try:
            offer = await runtime.extract_offer(provider, route, page, response_store)
        except Exception:
            if await ctrip_page.has_login_overlay(page):
                raise RuntimeError(login_required_message(route, page))
            raise
    return offer, page.url, await page.title()


def as_native_round_trip_route(route: RouteQuery) -> RouteQuery | None:
    segments = route.segments or []
    if len(segments) != 2:
        return None
    outbound, inbound = segments
    if ctrip_city_code(outbound.origin) != ctrip_city_code(inbound.destination):
        return None
    if ctrip_city_code(outbound.destination) != ctrip_city_code(inbound.origin):
        return None
    return RouteQuery(
        origin=outbound.origin,
        destination=outbound.destination,
        departure_date=outbound.departure_date,
        return_date=inbound.departure_date,
        route_type="round_trip",
        cabin=route.cabin,
        transfer_policy=route.transfer_policy,
        passengers=route.passengers,
        providers=route.providers,
        preferred_airlines=route.preferred_airlines,
    )


async def load_direct_multi_city_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
):
    try:
        await page.goto(build_multi_city_results_url(route), wait_until="domcontentloaded", timeout=provider.timeout_ms)
        await page.wait_for_timeout(3500)
        if not is_ctrip_results_url(page.url):
            return None
        await runtime.wait_for_results(provider, page)
        await runtime.dismiss_ctrip_login_overlays(page, provider.timeout_ms)
        await runtime.warm_ctrip_result_data(page, response_store)
        network_match = runtime.extract_ctrip_multi_city_match_from_network(response_store, route)
        if network_match is not None:
            offer = runtime.offer_details_type(price=network_match["total_price"])
        else:
            try:
                offer = await runtime.extract_offer(provider, route, page, response_store)
            except Exception:
                return None
        return offer, page.url, await page.title()
    except Exception:
        return None


def build_multi_city_results_url(route: RouteQuery) -> str:
    segments = route.segments or []
    route_codes: list[str] = []
    for segment in segments:
        route_codes.append(ctrip_city_code(segment.origin).lower())
        route_codes.append(ctrip_city_code(segment.destination).lower())
    date_part = "_".join(quote(segment.departure_date) for segment in segments)
    cabin = CTRIP_ONLINE_CABIN_PARAMS.get((route.cabin or "").lower(), "y")
    if route.cabin == "economy":
        cabin = "y_s"
    return (
        f"https://flights.ctrip.com/online/list/multi-{'-'.join(route_codes)}"
        f"?depdate={date_part}&cabin={quote(cabin)}&adult={route.passengers}&child=0&infant=0"
    )


async def submit_search(page: Page, timeout_ms: int) -> None:
    await random_scroll_and_pause(page, scroll_probability=0.3)
    button = page.locator("button.search-btn")
    try:
        await hover_then_click(page, "button.search-btn", timeout_ms=timeout_ms)
        return
    except Exception:
        pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(180)

    try:
        await page.locator("body").click(position={"x": 24, "y": 24}, timeout=timeout_ms // 8, force=True)
    except Exception:
        pass
    await page.wait_for_timeout(220)

    try:
        await button.click(timeout=max(4000, timeout_ms // 3), force=True)
        return
    except Exception:
        pass

    try:
        await page.locator("form#searchForm").evaluate(
            """(form) => {
                if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                    return;
                }
                if (form && typeof form.submit === 'function') {
                    form.submit();
                }
            }"""
        )
        return
    except Exception:
        pass

    await button.evaluate("(node) => node.click()")


async def ensure_results_page(page: Page, timeout_ms: int, route: RouteQuery) -> None:
    if is_ctrip_results_url(page.url):
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return

    try:
        await page.wait_for_url(lambda url: is_ctrip_results_url(url), timeout=timeout_ms // 2)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return
    except Exception:
        pass

    try:
        await page.locator("form#searchForm").press("Enter", timeout=timeout_ms // 6)
    except Exception:
        pass

    try:
        await page.wait_for_url(lambda url: is_ctrip_results_url(url), timeout=timeout_ms // 3)
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return
    except Exception:
        pass

    await dump_submit_debug(page, route)
    raise RuntimeError(f"Ctrip search did not reach a results page for route {route.route_key}. Current URL: {page.url}")


async def ensure_results_cabin_url(page: Page, route: RouteQuery, timeout_ms: int) -> None:
    expected_cabin = CTRIP_ONLINE_CABIN_PARAMS.get((route.cabin or "").lower(), "y")
    parts = urlsplit(page.url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("cabin") == expected_cabin:
        return

    query["cabin"] = expected_cabin
    corrected_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    LOGGER.info(
        "Ctrip search form reset cabin; correcting results URL route=%s previous_url=%s corrected_url=%s",
        route.route_key,
        page.url,
        corrected_url,
    )
    await page.goto(corrected_url, wait_until="domcontentloaded", timeout=timeout_ms)


def is_ctrip_results_url(url: str) -> bool:
    normalized = (url or "").lower()
    return any(marker in normalized for marker in CTRIP_RESULT_URL_MARKERS)


async def dump_submit_debug(page: Page, route: RouteQuery) -> None:
    try:
        from datetime import datetime
        from pathlib import Path
        import json

        dump_dir = Path("runtime") / "submit_debug"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        prefix = dump_dir / f"ctrip_submit_{route.origin}_{route.destination}_{stamp}"
        await page.screenshot(path=str(prefix.with_suffix(".png")), full_page=True)
        prefix.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        meta = {
            "route_key": route.route_key,
            "url": page.url,
            "title": await page.title(),
        }
        prefix.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
