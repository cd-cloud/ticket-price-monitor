import re
from urllib.parse import quote

from playwright.async_api import Page

from config_manager import ProviderConfig, RouteQuery
from providers.base import ParsedProviderResult, ProviderQueryOutcome


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
                await page.keyboard.press("Control+A")
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
    if passengers <= 1:
        return
    try:
        await page.locator("button:has-text('Traveler')").first.click(timeout=timeout_ms // 6)
    except Exception:
        return
    for _ in range(passengers - 1):
        for selector in ["button[aria-label*='adult'][aria-label*='increase']", "button:has-text('+')"]:
            try:
                await page.locator(selector).first.click(timeout=timeout_ms // 8)
                break
            except Exception:
                continue


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
    selectors = [
        "[data-testid*='flight-card']",
        "[class*='flight']",
        "[class*='result']",
        "[class*='price']",
    ]
    for selector in selectors:
        try:
            items = page.locator(selector)
            count = min(await items.count(), 30)
        except Exception:
            continue
        for index in range(count):
            try:
                text = (await items.nth(index).inner_text()).strip()
            except Exception:
                continue
            for price in extract_prices(text):
                candidates.append(runtime.offer_details_type(price=price, row_text=text))

    if not candidates:
        body = await page.locator("body").inner_text()
        for price in extract_prices(body):
            candidates.append(runtime.offer_details_type(price=price, row_text=body[:500]))

    if not candidates:
        raise RuntimeError("Could not extract Priceline fare from visible results.")

    return min(candidates, key=lambda offer: offer.price)


def extract_prices(text: str) -> list[float]:
    values = []
    normalized = text.replace(",", "")
    for match in PRICE_PATTERN.finditer(normalized):
        value = float(match.group(1))
        if 50 <= value <= 50000:
            values.append(value)
    return values
