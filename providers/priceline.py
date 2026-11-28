import re
from datetime import date
from urllib.parse import quote

from playwright.async_api import Page

from core.config_manager import ProviderConfig, RouteQuery
from providers.base import ParsedProviderResult, ProviderQueryOutcome
from tail.tail_quotes import quote_terms


name = "priceline"
supports_multi_city = False
supports_tail_discovery = False

PRICELINE_FLIGHTS_URL = "https://www.priceline.com/flights"
PRICE_PATTERN = re.compile(r"(?<!\d)\$\s*([1-9]\d{1,5}(?:\.\d{1,2})?)")


def cabin_label(cabin: str) -> str:
    normalized = (cabin or "economy").lower()
    mapping = {
        "economy": "Economy",
        "economy_plus": "Economy",
        "premium_economy": "Premium Economy",
        "business": "Business",
        "business_first": "Business",
        "first": "First",
    }
    return mapping.get(normalized, "Economy")


def build_search_url(route: RouteQuery) -> str:
    # Priceline often changes its client-side route shape, so this URL is primarily
    # a useful landing/search context. The form automation below still verifies results.
    return (
        f"{PRICELINE_FLIGHTS_URL}/?from={quote(route.origin)}"
        f"&to={quote(route.destination)}"
        f"&depart={quote(route.departure_date)}"
        f"&adults={quote(str(route.passengers))}"
        f"&cabin={quote(cabin_label(route.cabin))}"
    )


async def query_single_route_offer(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
):
    if route.return_date:
        raise RuntimeError("Provider priceline currently supports one-way queries only; round-trip automation is not verified.")
    if route.passengers != 1:
        raise RuntimeError("Provider priceline currently requires exactly one passenger for verified results.")
    if route.transfer_policy != "any":
        raise RuntimeError("Provider priceline cannot verify direct-only or transfer-only result filters.")
    if route.preferred_airlines:
        raise RuntimeError("Provider priceline cannot verify preferred-airline result filters.")
    await runtime.throttle(provider)
    await page.goto(PRICELINE_FLIGHTS_URL, wait_until="domcontentloaded", timeout=provider.timeout_ms)
    await page.wait_for_timeout(2500)
    await ensure_no_manual_verification(page)

    await choose_one_way(page, provider.timeout_ms)
    await fill_airport(page, provider.selectors.get("origin", []), route.origin, "origin", provider.timeout_ms)
    await fill_airport(page, provider.selectors.get("destination", []), route.destination, "destination", provider.timeout_ms)
    await fill_date(page, route.departure_date, provider.timeout_ms)
    await choose_cabin(page, cabin_label(route.cabin), provider.timeout_ms)
    await choose_passengers(page, route.passengers, provider.timeout_ms)
    await submit_search(page, provider.timeout_ms)
    await wait_for_results(page, provider.timeout_ms)
    await verify_result_context(page, route)

    offer = await extract_offer(runtime, page, route)
    return offer, page.url, await page.title()


async def query(
    runtime,
    provider: ProviderConfig,
    route: RouteQuery,
    page: Page,
    response_store: list[dict],
) -> ProviderQueryOutcome:
    if route.is_multi_city:
        raise RuntimeError(f"Provider {provider.name} does not support native multi-city automation in this tool yet.")
    offer, final_url, title = await query_single_route_offer(runtime, provider, route, page, response_store)
    return ParsedProviderResult(
        provider="priceline",
        price=offer.price,
        final_url=final_url,
        title=title,
        departure_time=offer.departure_time,
        row_text=offer.row_text,
        parser="priceline_visible",
        confidence="low",
        extra_payload={
            "detail_quality": "price_only",
            "detail_source": "visible_result",
            "quote_terms": quote_terms("priceline", route.passengers, "USD"),
            "validation": {
                "status": "needs_review",
                "scope": "visible_route_date_cabin_context",
                "version": 1,
            },
        },
    ).to_outcome()


async def choose_one_way(page: Page, timeout_ms: int) -> None:
    selectors = [
        "label:has-text('One-way')",
        "button:has-text('One-way')",
        "[role='radio']:has-text('One-way')",
        "text=One-way",
    ]
    for selector in selectors:
        try:
            await page.locator(selector).first.click(timeout=timeout_ms // 5)
            await page.wait_for_timeout(300)
            return
        except Exception:
            continue
    raise RuntimeError("Could not confirm Priceline one-way mode before searching.")


async def fill_airport(page: Page, selectors, value: str, field_name: str, timeout_ms: int) -> None:
    selector_list = [item for item in selectors if isinstance(item, str)] if isinstance(selectors, list) else []
    if field_name == "destination":
        selector_list.extend([
            "input[placeholder*='Going to']",
            "input[placeholder*='To']",
            "input[placeholder*='Going']",
            "input[aria-label*='To']",
            "input[aria-label*='Going']",
            "input[name='flights.0.endLocation']",
            "input[role='combobox']",
        ])
    else:
        selector_list.extend([
            "input[placeholder*='Departing from']",
            "input[placeholder*='From']",
            "input[placeholder*='Leaving']",
            "input[aria-label*='From']",
            "input[aria-label*='Leaving']",
            "input[name='flights.0.startLocation']",
            "input[role='combobox']",
        ])
    last_error: Exception | None = None
    per_selector_timeout = min(3500, max(1500, timeout_ms // 12))
    for selector in selector_list:
        try:
            input_box = page.locator(selector).first
            if await input_box.count() == 0:
                continue
            await input_box.click(timeout=per_selector_timeout)
            try:
                await input_box.fill("")
            except Exception:
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.press("Backspace")
            await input_box.fill(value)
            await page.wait_for_timeout(700)
            await select_airport_suggestion(page, value, timeout_ms)
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        f"Could not fill Priceline airport field with {value}. "
        "If Priceline is showing a human verification prompt, run "
        "`python main.py bootstrap-session --provider priceline --manual`, "
        "complete it in the browser, then retry."
    ) from last_error


async def select_airport_suggestion(page: Page, value: str, timeout_ms: int) -> None:
    selectors = [
        f"[role='option']:has-text('{value}')",
        f"li:has-text('{value}')",
        f"button:has-text('{value}')",
        f"div:has-text('{value}')",
    ]
    for selector in selectors:
        try:
            await page.locator(selector).first.click(timeout=timeout_ms // 6)
            await page.wait_for_timeout(300)
            return
        except Exception:
            continue
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(300)


async def fill_date(page: Page, departure_date: str, timeout_ms: int) -> None:
    selectors = [
        "input[type='date']",
        "button:has-text('Departing')",
        "button:has-text('Departing - Returning')",
        "input[placeholder*='Depart']",
        "input[aria-label*='Depart']",
        "button:has-text('Depart')",
        "button:has-text('Departure')",
    ]
    per_selector_timeout = min(3500, max(1500, timeout_ms // 12))
    for selector in selectors:
        try:
            target = page.locator(selector).first
            if await target.count() == 0:
                continue
            await target.click(timeout=per_selector_timeout)
            if selector.startswith("input"):
                try:
                    await target.fill(departure_date)
                    await page.keyboard.press("Enter")
                    return
                except Exception:
                    pass
            await click_date_in_calendar(page, departure_date, timeout_ms)
            return
        except Exception:
            continue
    raise RuntimeError(
        f"Could not set Priceline departure date {departure_date}. "
        "If Priceline is showing a human verification prompt, bootstrap a manual Priceline session first."
    )


async def click_date_in_calendar(page: Page, departure_date: str, timeout_ms: int) -> None:
    year, month, day = departure_date.split("-")
    day_number = str(int(day))
    selectors = [
        f"[aria-label*='{departure_date}']",
        f"[data-date='{departure_date}']",
        f"button:has-text('{day_number}')",
        f"[role='button']:has-text('{day_number}')",
    ]
    for selector in selectors:
        try:
            await page.locator(selector).first.click(timeout=timeout_ms // 6)
            await page.wait_for_timeout(300)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not click Priceline calendar date {year}-{month}-{day}")


async def choose_cabin(page: Page, label: str, timeout_ms: int) -> None:
    selectors = [
        "button:has-text('Cabin')",
        "button:has-text('Economy')",
        "select[aria-label*='Cabin']",
        "select",
    ]
    for selector in selectors:
        try:
            control = page.locator(selector).first
            if selector.startswith("select"):
                await control.select_option(label=label, timeout=timeout_ms // 5)
                return
            await control.click(timeout=timeout_ms // 5)
            await page.locator(f"text={label}").first.click(timeout=timeout_ms // 5)
            return
        except Exception:
            continue


async def choose_passengers(page: Page, passengers: int, timeout_ms: int) -> None:
    if passengers != 1:
        raise RuntimeError("Priceline result verification currently supports exactly one passenger.")


async def submit_search(page: Page, timeout_ms: int) -> None:
    selectors = [
        "button:has-text('Search')",
        "button[aria-label*='Search']",
        "button[type='submit']",
    ]
    per_selector_timeout = min(5000, max(2000, timeout_ms // 10))
    for selector in selectors:
        try:
            await page.locator(selector).first.click(timeout=per_selector_timeout)
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return
        except Exception:
            continue
    raise RuntimeError("Could not submit Priceline flight search.")


async def wait_for_results(page: Page, timeout_ms: int) -> None:
    await ensure_no_manual_verification(page)
    selectors = [
        "[data-testid*='price']",
        "[class*='price']",
        "text=/\\$\\s*\\d+/",
    ]
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(timeout=timeout_ms)
            await page.wait_for_timeout(1500)
            return
        except Exception:
            continue
    raise RuntimeError("Priceline search reached no recognizable price results.")


RESULT_CARD_SELECTORS = (
    "[data-testid='flight-card']",
    "[data-testid^='flight-card-']",
    "[data-testid='itinerary-card']",
    "[data-testid^='itinerary-card-']",
    "[class~='flight-card']",
    "[class~='itinerary-card']",
    "[class~='result-card']",
)
_CHILD_CARD_SELECTORS = (
    "[data-testid*='result']",
    "[class*='result-row']",
    "[class*='flight-row']",
    "[class*='itinerary-row']",
)


def _directional_route_matches(text: str, route: RouteQuery) -> bool:
    origin = route.origin.upper()
    destination = route.destination.upper()
    separator = r"(?:->|→|➜|\bto\b|[-–—])"
    normalized = str(text or "")
    pairs = [
        (left.upper(), right.upper())
        for left, right in re.findall(
            rf"(?<![A-Z0-9])([A-Z]{{3}})(?![A-Z0-9])\s*{separator}\s*"
            rf"(?=([A-Z]{{3}})(?![A-Z0-9]))",
            normalized,
            re.IGNORECASE,
        )
    ]
    current = origin
    consumed = 0
    while consumed < len(pairs) and pairs[consumed][0] == current:
        current = pairs[consumed][1]
        consumed += 1
    return bool(pairs) and current == destination and consumed == len(pairs)


def _cabin_occurrences(text: str, cabin: str) -> list[re.Match[str]]:
    normalized = str(text or "")
    lowered = normalized.lower()
    requested = (cabin or "economy").lower()
    if requested in {"economy", "economy_plus"}:
        matches = []
        for match in re.finditer(r"\beconomy\b", normalized, re.IGNORECASE):
            prefix = lowered[max(0, match.start() - 20):match.start()]
            if re.search(r"premium\s+$", prefix):
                continue
            matches.append(match)
        return matches
    if requested == "premium_economy":
        return list(re.finditer(r"\bpremium\s+economy\b", normalized, re.IGNORECASE))
    if requested == "business":
        return list(re.finditer(r"\bbusiness\b", normalized, re.IGNORECASE))
    if requested == "first":
        return list(re.finditer(r"\bfirst\b", normalized, re.IGNORECASE))
    if requested == "business_first":
        return [
            *re.finditer(r"\bbusiness\b", normalized, re.IGNORECASE),
            *re.finditer(r"\bfirst\b", normalized, re.IGNORECASE),
        ]
    return []


def result_card_matches_route(text: str, route: RouteQuery) -> bool:
    normalized = str(text or "")
    if not _directional_route_matches(normalized, route):
        return False
    try:
        departure = date.fromisoformat(route.departure_date)
    except ValueError:
        return False
    date_patterns = [
        rf"(?<!\d){re.escape(departure.isoformat())}(?!\d)",
        rf"(?<!\d){departure.month}/{departure.day}/{departure.year}(?!\d)",
        rf"(?<![A-Za-z]){re.escape(departure.strftime('%B'))}\s+{departure.day},\s*{departure.year}(?!\d)",
        rf"(?<![A-Za-z]){re.escape(departure.strftime('%b'))}\s+{departure.day},\s*{departure.year}(?!\d)",
    ]
    if not any(re.search(pattern, normalized, re.IGNORECASE) for pattern in date_patterns):
        return False
    return bool(_cabin_occurrences(normalized, route.cabin))


_ANY_CABIN_PATTERN = re.compile(
    r"\b(?:premium\s+economy|economy|business|first)\b",
    re.IGNORECASE,
)


def _cabin_price_window(text: str, occurrence: re.Match[str]) -> str:
    boundaries = ["|", ";", "\n"]
    left = max((text.rfind(mark, 0, occurrence.start()) for mark in boundaries), default=-1) + 1
    right_candidates = [text.find(mark, occurrence.end()) for mark in boundaries]
    right_candidates = [value for value in right_candidates if value >= 0]
    right = min(right_candidates, default=len(text))
    previous_marker = None
    for marker in _ANY_CABIN_PATTERN.finditer(text):
        if marker.end() <= occurrence.start():
            previous_marker = marker
        elif marker.start() >= occurrence.end():
            right = min(right, marker.start())
            break
    if previous_marker is not None:
        between_start = previous_marker.end()
        prior_prices = list(PRICE_PATTERN.finditer(text, between_start, occurrence.start()))
        left = max(left, prior_prices[-1].end() if prior_prices else between_start)
    if right <= left:
        left = max(0, occurrence.start() - 80)
        right = min(len(text), occurrence.end() + 120)
    return text[left:right]


def extract_prices_for_cabin(text: str, cabin: str) -> list[float]:
    """Extract prices near the requested cabin, never the cheapest unrelated cabin."""
    normalized = str(text or "")
    values: list[float] = []
    for occurrence in _cabin_occurrences(normalized, cabin):
        values.extend(extract_prices(_cabin_price_window(normalized, occurrence)))
    return list(dict.fromkeys(values))


async def _is_leaf_result_card(item) -> bool:
    for selector in RESULT_CARD_SELECTORS:
        try:
            descendants = item.locator(selector)
            if await descendants.count():
                return False
        except Exception:
            continue
    for selector in _CHILD_CARD_SELECTORS:
        try:
            descendants = item.locator(selector)
            if await descendants.count() > 0:
                return False
        except Exception:
            continue
    return True


async def _result_card_texts(page: Page):
    seen: set[str] = set()
    for selector in RESULT_CARD_SELECTORS:
        try:
            items = page.locator(selector)
            count = min(await items.count(), 30)
        except Exception:
            continue
        for index in range(count):
            item = items.nth(index)
            if not await _is_leaf_result_card(item):
                continue
            try:
                text = (await item.inner_text()).strip()
            except Exception:
                continue
            if text and text not in seen:
                seen.add(text)
                yield text


async def verify_result_context(page: Page, route: RouteQuery) -> None:
    async for text in _result_card_texts(page):
        if result_card_matches_route(text, route):
            return
    raise RuntimeError(
        "Priceline result cards did not confirm the requested route/date/cabin; "
        "refusing to save a quote."
    )


async def ensure_no_manual_verification(page: Page) -> None:
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return
    markers = [
        "Before we continue",
        "Press & Hold",
        "confirm you are a human",
        "not a bot",
    ]
    if any(marker in body for marker in markers):
        raise RuntimeError(
            "Priceline requires manual human verification. "
            "Run `python main.py bootstrap-session --provider priceline --manual`, "
            "complete the verification in the browser, then retry the query."
        )


async def extract_offer(runtime, page: Page, route: RouteQuery):
    candidates = []
    async for text in _result_card_texts(page):
        if not result_card_matches_route(text, route):
            continue
        prices = extract_prices_for_cabin(text, route.cabin)
        if len(prices) != 1:
            continue
        candidates.append(runtime.offer_details_type(price=prices[0], row_text=text))

    if not candidates:
        raise RuntimeError(
            "Could not extract one unambiguous Priceline fare from a result card matching "
            "the requested route/date/cabin."
        )

    return min(candidates, key=lambda offer: offer.price)


def extract_prices(text: str) -> list[float]:
    values = []
    normalized = text.replace(",", "")
    for match in PRICE_PATTERN.finditer(normalized):
        value = float(match.group(1))
        if 50 <= value <= 50000:
            values.append(value)
    return values
