import logging
import random
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page

from config_manager import RouteQuery, RouteSegment
from providers.ctrip_support import ctrip_city_code, is_ctrip_international_or_special_region
from providers.human_actions import human_like_type, hover_then_click, random_scroll_and_pause
from tail_discovery import TAIL_CODE_TEXT


LOGGER = logging.getLogger(__name__)


async def prepare_results_page(automation, page: Page, route: RouteQuery, timeout_ms: int) -> None:
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(2500)
    if not await page_has_departure_date(page, route.departure_date):
        await try_set_results_departure_date(page, route.departure_date, timeout_ms)

    await apply_cabin_filter(automation, page, route, timeout_ms)


async def ensure_results_departure_date(page: Page, route: RouteQuery, timeout_ms: int) -> None:
    if await page_has_departure_date(page, route.departure_date):
        return
    await dump_datepicker_debug(page, route.departure_date, "results-page-date-check")
    raise RuntimeError(f"Ctrip result date mismatch: expected {route.departure_date}.")


async def try_set_results_departure_date(page: Page, departure_date: str, timeout_ms: int) -> None:
    selectors = [
        ".modifyDate.depart-date",
        ".flt-date .modifyDate",
        "[u_key='date_picker']",
        "input.form-input-v3[readonly]",
        "input[aria-label*='日期']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 6)
        except Exception:
            count = 0
        for index in range(count):
            try:
                await choose_date_by_trigger(page, selector, index, departure_date, timeout_ms)
                if await page_has_departure_date(page, departure_date):
                    await page.wait_for_timeout(1800)
                    return
            except Exception:
                continue
    LOGGER.warning("ctrip result date could not be set to %s", departure_date)


async def page_has_departure_date(page: Page, departure_date: str) -> bool:
    evidence = await collect_results_date_evidence(page, departure_date)
    return bool(evidence.get("matched"))


async def collect_results_date_evidence(page: Page, departure_date: str) -> dict[str, object]:
    expected = departure_date.strip()
    evidence: dict[str, object] = {
        "expected": expected,
        "url": page.url,
        "url_dates": extract_url_date_values(page.url),
        "url_date_matched": False,
        "input_values": [],
        "page_dates": [],
        "overlay_visible": False,
        "matched": False,
        "matched_source": None,
        "matched_value": None,
    }
    if not expected:
        return evidence
    variants = date_text_variants(expected)
    for key, value in evidence["url_dates"].items():
        if date_value_matches(str(value), expected):
            evidence["url_date_matched"] = True

    input_locators = [
        page.locator("input"),
        page.locator(".modifyDate input"),
        page.locator("[u_key='date_picker'] input"),
    ]
    input_values: list[str] = []
    for locator in input_locators:
        try:
            count = min(await locator.count(), 10)
        except Exception:
            count = 0
        for index in range(count):
            try:
                value = (await locator.nth(index).input_value(timeout=800)).strip()
            except Exception:
                continue
            if not value or value in input_values:
                continue
            input_values.append(value)
            if date_value_matches(value, expected):
                evidence.update(
                    {
                        "input_values": input_values[:12],
                        "matched": True,
                        "matched_source": "input",
                        "matched_value": value,
                    }
                )
                return evidence
    evidence["input_values"] = input_values[:12]

    overlay_visible = await is_datepicker_overlay_visible(page)
    evidence["overlay_visible"] = overlay_visible
    if overlay_visible:
        return evidence

    try:
        text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        text = ""
    normalized = " ".join(text.split())
    page_dates = extract_page_departure_dates(normalized, reference_year=expected[:4])
    evidence["page_dates"] = page_dates[:5]
    if page_dates and not any(date_value_matches(value, expected) for value in page_dates):
        evidence.update(
            {
                "matched": False,
                "matched_source": "page_date_mismatch",
                "matched_value": page_dates[0],
            }
        )
        return evidence
    for variant in variants:
        if variant and variant in normalized:
            evidence.update({"matched": True, "matched_source": "body", "matched_value": variant})
            return evidence
    if evidence.get("url_date_matched") and not normalized:
        evidence.update({"matched": True, "matched_source": "url_only_empty_body", "matched_value": expected})
        return evidence
    return evidence


def extract_url_date_values(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    keys = ("depdate", "ddate", "date", "dDate", "departDate", "departureDate")
    values: dict[str, str] = {}
    for key in keys:
        if query.get(key):
            values[key] = str(query[key][0])
    return values


def date_value_matches(value: str, departure_date: str) -> bool:
    normalized = (value or "").strip()
    if not normalized:
        return False
    return normalized == departure_date or any(variant and variant in normalized for variant in date_text_variants(departure_date))


def extract_page_departure_dates(text: str, reference_year: str | int | None = None) -> list[str]:
    patterns = [
        r"出发日期[:：]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        r"出发日期[^0-9]{0,12}(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    ]
    dates: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, text or ""):
            value = match.replace("/", "-")
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d")
                value = parsed.strftime("%Y-%m-%d")
            except Exception:
                pass
            if value not in dates:
                dates.append(value)
    try:
        year = int(reference_year) if reference_year else None
    except Exception:
        year = None
    if year:
        month_day_patterns = [
            r"(?:单程|往返|多程)[^0-9]{0,20}(\d{1,2})月(\d{1,2})日",
            r"出发日期[^0-9]{0,20}(\d{1,2})月(\d{1,2})日",
            r"\b(\d{1,2})月(\d{1,2})日周[一二三四五六日天]",
        ]
        for pattern in month_day_patterns:
            for month, day in re.findall(pattern, text or ""):
                try:
                    value = datetime(year, int(month), int(day)).strftime("%Y-%m-%d")
                except Exception:
                    continue
                if value not in dates:
                    dates.append(value)
    return dates


def date_text_variants(departure_date: str) -> list[str]:
    try:
        target = datetime.strptime(departure_date, "%Y-%m-%d")
    except Exception:
        return [departure_date]
    return [
        departure_date,
        f"{target.year}/{target.month:02d}/{target.day:02d}",
        f"{target.year}/{target.month}/{target.day}",
        f"{target.month:02d}月{target.day:02d}日",
        f"{target.month}月{target.day}日",
        f"{target.month:02d}-{target.day:02d}",
    ]


async def apply_cabin_filter(automation, page: Page, route: RouteQuery, timeout_ms: int) -> None:
    cabin_key = (route.cabin or "").strip().lower()
    labels_by_cabin = {
        "economy_plus": ["经济舱", "超级经济舱", "超级经济"],
        "business_first": ["商务舱", "公务舱", "头等舱"],
        "economy": ["经济舱"],
        "premium_economy": ["超级经济舱", "超级经济"],
        "business": ["商务舱", "公务舱"],
        "first": ["头等舱"],
    }
    labels = labels_by_cabin.get(cabin_key)
    if not labels:
        return

    try:
        await expand_filter_section(page, ["舱位", "舱等"], timeout_ms)
        clicked = 0
        for label in labels:
            if await click_filter_label(page, label, timeout_ms):
                clicked += 1
                await page.wait_for_timeout(600)
        if clicked:
            LOGGER.info("applied ctrip cabin filter route=%s cabin=%s labels=%s", route.route_key, cabin_key, labels)
            await page.wait_for_timeout(1800)
    except Exception:
        LOGGER.exception("failed to apply ctrip cabin filter route=%s cabin=%s", route.route_key, cabin_key)


async def expand_filter_section(page: Page, labels: list[str], timeout_ms: int) -> None:
    for label in labels:
        locators = [
            page.get_by_text(label, exact=True),
            page.locator(f"text={label}"),
        ]
        for locator in locators:
            try:
                count = min(await locator.count(), 4)
            except Exception:
                continue
            for index in range(count):
                target = locator.nth(index)
                try:
                    await target.scroll_into_view_if_needed(timeout=timeout_ms // 6)
                    await target.click(timeout=timeout_ms // 6)
                    await page.wait_for_timeout(500)
                    return
                except Exception:
                    continue


async def click_filter_label(page: Page, label: str, timeout_ms: int) -> bool:
    locators = [
        page.get_by_text(label, exact=True),
        page.locator(f"label:has-text('{label}')"),
        page.locator(f"[class*='filter']:has-text('{label}')"),
        page.locator(f"[class*='Filter']:has-text('{label}')"),
    ]
    for locator in locators:
        try:
            count = min(await locator.count(), 8)
        except Exception:
            continue
        for index in range(count):
            target = locator.nth(index)
            try:
                await target.scroll_into_view_if_needed(timeout=timeout_ms // 6)
                await target.click(timeout=timeout_ms // 6)
                return True
            except Exception:
                try:
                    checkbox = target.locator("xpath=ancestor-or-self::*[1]//input[@type='checkbox']").first
                    await checkbox.check(timeout=timeout_ms // 6)
                    return True
                except Exception:
                    continue
    return False


async def activate_multi_city(page: Page, timeout_ms: int) -> None:
    for selector in [
        "ul.form-select-radio-group li.last",
        "ul.form-select-radio-group li:nth-child(3)",
        "ul.form-select-radio-group li:last-child",
    ]:
        try:
            await page.locator(selector).first.click(timeout=timeout_ms // 4)
            await page.wait_for_timeout(1200)
            active = page.locator("ul.form-select-radio-group li.active").nth(0)
            if await active.count():
                active_text = (await active.inner_text()).strip()
                if "多程" in active_text:
                    return
        except Exception:
            continue
    raise RuntimeError("Could not activate Ctrip multi-city tab.")


async def activate_one_way(page: Page, timeout_ms: int) -> None:
    for selector in [
        "ul.form-select-radio-group li:nth-child(1)",
        "ul.form-select-radio-group li:first-child",
    ]:
        try:
            await page.locator(selector).first.click(timeout=timeout_ms // 4)
            await page.wait_for_timeout(1000)
            return
        except Exception:
            continue
    raise RuntimeError("Could not activate Ctrip one-way tab.")


async def activate_round_trip(page: Page, timeout_ms: int) -> None:
    for selector in [
        "ul.form-select-radio-group li:nth-child(2)",
        "ul.form-select-radio-group li:has-text('往返')",
    ]:
        try:
            await page.locator(selector).first.click(timeout=timeout_ms // 4)
            await page.wait_for_timeout(1000)
            return
        except Exception:
            continue
    raise RuntimeError("Could not activate Ctrip round-trip tab.")


async def dismiss_login_overlays(page: Page, timeout_ms: int) -> None:
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(150)
    selectors = [
        "[class*='login'] [class*='close']",
        "[class*='Login'] [class*='close']",
        "[class*='login'] [class*='icon-close']",
        "[class*='Login'] [class*='icon-close']",
        "[class*='passport'] [class*='close']",
        "[class*='modal'] [class*='close']",
        "[class*='popup'] [class*='close']",
        "[class*='pop'] [class*='close']",
        "[class*='dialog'] [class*='close']",
        "[role='dialog'] [class*='close']",
        "[role='dialog'] button[aria-label='Close']",
        "[role='dialog'] button[aria-label='关闭']",
        "[aria-modal='true'] [class*='close']",
        ".ant-modal-close",
        ".c-modal-close",
        ".modal-close",
        ".popup-close",
        "button:has-text('以后再说')",
        "button:has-text('暂不登录')",
        "button:has-text('稍后')",
        "text=以后再说",
        "text=暂不登录",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 4)
            for index in range(count):
                item = locator.nth(index)
                if await item.is_visible(timeout=500):
                    await item.click(timeout=max(800, timeout_ms // 20), force=True)
                    await page.wait_for_timeout(400)
                    return
        except Exception:
            continue


async def has_login_overlay(page: Page) -> bool:
    selectors = [
        "[role='dialog']",
        "[aria-modal='true']",
        "[class*='login']",
        "[class*='Login']",
        "[class*='passport']",
        "[class*='modal']",
        "[class*='popup']",
    ]
    markers = [
        "\u8d26\u53f7\u5bc6\u7801\u767b\u5f55",
        "\u626b\u7801\u767b\u5f55",
        "\u9a8c\u8bc1\u7801\u767b\u5f55",
        "\u514d\u8d39\u6ce8\u518c",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 12)
            for index in range(count):
                item = locator.nth(index)
                if not await item.is_visible(timeout=300):
                    continue
                text = await item.inner_text(timeout=800)
                if any(marker in text for marker in markers):
                    return True
        except Exception:
            continue
    return False


async def has_no_results_notice(page: Page) -> bool:
    try:
        text = await page.locator("body").inner_text(timeout=2500)
    except Exception:
        return False
    return any(
        marker in text
        for marker in [
            "未找到符合条件的航班",
            "暂时无法查询到对应价格",
            "无航班",
            "航班座位已售完",
            "no flights",
            "no result",
        ]
    )


async def prepare_one_way_form(
    automation,
    page: Page,
    route: RouteQuery,
    timeout_ms: int,
    *,
    skip_origin: bool = False,
) -> None:
    await dismiss_login_overlays(page, timeout_ms)
    if not skip_origin:
        await choose_city(automation, page, "owDCity", route.origin, timeout_ms)
    await choose_city(automation, page, "owACity", route.destination, timeout_ms)
    await choose_date_by_trigger(
        page,
        "form#searchForm .form-item-v3.flt-date .modifyDate",
        0,
        route.departure_date,
        timeout_ms,
    )


async def prepare_round_trip_form(automation, page: Page, route: RouteQuery, timeout_ms: int) -> None:
    await dismiss_login_overlays(page, timeout_ms)
    await choose_city(automation, page, "owDCity", route.origin, timeout_ms)
    await choose_city(automation, page, "owACity", route.destination, timeout_ms)
    await choose_date_by_trigger(
        page,
        "form#searchForm .form-item-v3.flt-date .modifyDate",
        0,
        route.departure_date,
        timeout_ms,
    )
    if route.return_date:
        await choose_date_by_trigger(
            page,
            "form#searchForm .form-item-v3.flt-date .modifyDate",
            1,
            route.return_date,
            timeout_ms,
        )


async def prepare_multi_city_form(automation, page: Page, segments: list[RouteSegment], timeout_ms: int) -> None:
    await sync_multi_city_rows(page, len(segments), timeout_ms)
    for index, segment in enumerate(segments):
        segment_index = index + 1
        await choose_city(automation, page, f"mtDCity{segment_index}", segment.origin, timeout_ms)
        await choose_city(automation, page, f"mtACity{segment_index}", segment.destination, timeout_ms)
        await choose_date(page, index, segment.departure_date, timeout_ms)


async def sync_multi_city_rows(page: Page, segment_count: int, timeout_ms: int) -> None:
    while await page.locator("form#searchForm .form-line").count() > segment_count:
        await page.locator("a.remove-trip-btn").first.click(timeout=timeout_ms // 5)
        await page.wait_for_timeout(600)
    while await page.locator("form#searchForm .form-line").count() < segment_count:
        await page.locator("a.add-trip-btn").click(timeout=timeout_ms // 5)
        await page.wait_for_timeout(600)


async def choose_city(automation, page: Page, input_name: str, airport_code: str, timeout_ms: int) -> None:
    city_code = ctrip_city_code(airport_code)
    input_locator = page.locator(f"input[name='{input_name}']").first
    poi_scope = input_locator.locator("xpath=ancestor::div[contains(@class,'cflt-poi')]").first
    await input_locator.click(timeout=timeout_ms // 4)
    await page.wait_for_timeout(random.randint(300, 700))

    if is_ctrip_international_or_special_region(airport_code):
        await activate_city_picker_tab(page, poi_scope, prefer_international=True, timeout_ms=timeout_ms)
    else:
        await activate_city_picker_tab(page, poi_scope, prefer_international=False, timeout_ms=timeout_ms)

    if await click_matching_city_candidate(poi_scope, airport_code, city_code, timeout_ms // 4):
        await page.wait_for_timeout(random.randint(400, 800))
        await random_scroll_and_pause(page, scroll_probability=0.15)
        return

    await activate_city_picker_tab(
        page,
        poi_scope,
        prefer_international=not is_ctrip_international_or_special_region(airport_code),
        timeout_ms=timeout_ms,
    )
    if await click_matching_city_candidate(poi_scope, airport_code, city_code, timeout_ms // 4):
        await page.wait_for_timeout(random.randint(400, 800))
        await random_scroll_and_pause(page, scroll_probability=0.15)
        return

    tried_terms: list[str] = []
    for search_term in city_search_terms(airport_code, city_code):
        tried_terms.append(search_term)
        await input_locator.click(timeout=timeout_ms // 5)
        await human_like_type(page, f"input[name='{input_name}']", search_term, timeout_ms=timeout_ms // 5)
        await page.wait_for_timeout(random.randint(800, 1400))
        if await click_matching_city_candidate(poi_scope, airport_code, city_code, timeout_ms // 4):
            await page.wait_for_timeout(random.randint(400, 800))
            await random_scroll_and_pause(page, scroll_probability=0.15)
            return
        try:
            await input_locator.press("Control+A", timeout=timeout_ms // 10)
        except Exception:
            pass

    await dump_city_picker_debug(page, input_name, airport_code, tried_terms)
    raise RuntimeError(
        f"Could not select Ctrip city option for {airport_code}; tried {', '.join(tried_terms)}."
    )


async def activate_city_picker_tab(page: Page, poi_scope, prefer_international: bool, timeout_ms: int) -> None:
    tab_locator = poi_scope.locator(".city-picker-selector:not(.hide) .tab-item")
    try:
        count = await tab_locator.count()
    except Exception:
        return
    if count < 2:
        return

    preferred_indices = [1, 0] if prefer_international else [0, 1]
    for index in preferred_indices:
        try:
            tab = tab_locator.nth(index)
            await tab.click(timeout=timeout_ms // 5)
            await page.wait_for_timeout(700)
            return
        except Exception:
            continue


def city_option_patterns(airport_code: str, city_code: str) -> list[str]:
    airport_code = airport_code.upper()
    city_code = city_code.upper()
    patterns = [
        f".city-picker-selector:not(.hide) li[data-u_remark*='({airport_code})']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='({city_code})']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='|{airport_code}]']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='|{city_code}]']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='{airport_code})']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='{city_code})']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='{airport_code}']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='{city_code}']",
    ]
    for term in city_search_terms(airport_code, city_code):
        patterns.extend(search_term_option_patterns(term, airport_code, city_code))
    return list(dict.fromkeys(patterns))


def city_search_terms(airport_code: str, city_code: str) -> list[str]:
    airport_code = airport_code.upper()
    city_code = city_code.upper()
    terms: list[str] = []
    for code in (city_code, airport_code):
        for token in TAIL_CODE_TEXT.get(code, ()):
            if token and token not in terms:
                terms.append(token)
    for token in (city_code, airport_code):
        if token and token not in terms:
            terms.append(token)
    return terms


def search_term_option_patterns(search_term: str, airport_code: str, city_code: str) -> list[str]:
    text = str(search_term or "").strip()
    if not text:
        return []
    airport_code = airport_code.upper()
    city_code = city_code.upper()
    return [
        f".city-picker-selector:not(.hide) li[data-u_remark*='{text}'][data-u_remark*='{airport_code}']",
        f".city-picker-selector:not(.hide) li[data-u_remark*='{text}'][data-u_remark*='{city_code}']",
        f".city-picker-selector:not(.hide) li:has-text('{text}'):has-text('{airport_code}')",
        f".city-picker-selector:not(.hide) li:has-text('{text}'):has-text('{city_code}')",
        f".city-picker-selector:not(.hide) li:has-text('{text}')",
    ]


def city_candidate_matches(candidate_text: str, candidate_remark: str, airport_code: str, city_code: str) -> bool:
    combined = f"{candidate_text} {candidate_remark}".upper()
    codes = list(dict.fromkeys([airport_code.upper(), city_code.upper()]))
    for code in codes:
        if code and re.search(rf"(?<![A-Z]){re.escape(code)}(?![A-Z])", combined):
            return True
    for code in codes:
        for token in TAIL_CODE_TEXT.get(code, ()):
            normalized = str(token or "").strip()
            if normalized and not normalized.isascii() and normalized.upper() in combined:
                return True
    return False


async def click_matching_city_candidate(poi_scope, airport_code: str, city_code: str, timeout_ms: int) -> bool:
    selectors = [
        ".city-picker-selector:not(.hide) li:not(.disabled)",
        ".city-picker-selector:not(.hide) [role='option']",
        ".city-picker-selector:not(.hide) .city-item",
        ".city-picker-selector:not(.hide) .item",
        ".city-picker-selector:not(.hide) dd",
        "li:not(.disabled)",
        "[role='option']",
    ]
    for selector in selectors:
        try:
            locator = poi_scope.locator(selector)
            count = min(await locator.count(), 30)
            for index in range(count):
                candidate = locator.nth(index)
                text = " ".join((await candidate.inner_text(timeout=timeout_ms // 2)).split())
                remark = (await candidate.get_attribute("data-u_remark", timeout=timeout_ms // 2)) or ""
                if not city_candidate_matches(text, remark, airport_code, city_code):
                    continue
                await candidate.click(timeout=timeout_ms, force=True)
                LOGGER.info(
                    "selected verified Ctrip city candidate airport=%s city=%s text=%s remark=%s",
                    airport_code,
                    city_code,
                    text,
                    remark,
                )
                return True
        except Exception:
            continue
    return False


async def click_visible_city_candidate(poi_scope, timeout_ms: int) -> bool:
    selectors = [
        ".city-picker-selector:not(.hide) li:not(.disabled)",
        ".city-picker-selector:not(.hide) [role='option']",
        ".city-picker-selector:not(.hide) .city-item",
        ".city-picker-selector:not(.hide) .item",
        ".city-picker-selector:not(.hide) dd",
        "li:not(.disabled)",
        "[role='option']",
    ]
    for selector in selectors:
        try:
            locator = poi_scope.locator(selector)
            count = min(await locator.count(), 8)
            for index in range(count):
                candidate = locator.nth(index)
                text = " ".join((await candidate.inner_text(timeout=timeout_ms // 2)).split())
                if not text or text in {"国内", "国际/港澳台", "热门", "历史"}:
                    continue
                await candidate.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            continue
    return False


async def accept_first_city_candidate_by_keyboard(input_locator, page: Page, timeout_ms: int) -> bool:
    try:
        await input_locator.press("ArrowDown", timeout=timeout_ms)
        await page.wait_for_timeout(180)
        await input_locator.press("Enter", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def fill_city_input_directly(input_locator, page: Page, airport_code: str, city_code: str, timeout_ms: int) -> bool:
    value = city_code.upper() or airport_code.upper()
    try:
        await input_locator.evaluate(
            """(node, value) => {
                node.focus();
                node.value = value;
                for (const eventName of ["input", "change", "blur"]) {
                    node.dispatchEvent(new Event(eventName, { bubbles: true }));
                }
            }""",
            value,
        )
        await page.keyboard.press("Escape")
        try:
            await page.locator("body").click(position={"x": 24, "y": 24}, timeout=timeout_ms, force=True)
        except Exception:
            pass
        actual = (await input_locator.input_value(timeout=timeout_ms)).strip().upper()
        return value in actual or airport_code.upper() in actual
    except Exception:
        return False


async def dump_city_picker_debug(page: Page, input_name: str, airport_code: str, tried_terms: list[str]) -> None:
    try:
        import json

        dump_dir = Path("runtime") / "city_picker_debug"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        prefix = dump_dir / f"ctrip_city_{input_name}_{airport_code}_{stamp}"
        await page.screenshot(path=str(prefix.with_suffix(".png")), full_page=True)
        prefix.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        prefix.with_suffix(".json").write_text(
            json.dumps(
                {
                    "input_name": input_name,
                    "airport_code": airport_code,
                    "tried_terms": tried_terms,
                    "url": page.url,
                    "title": await page.title(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.debug("failed to dump ctrip city picker debug artifacts: %s", exc)


async def choose_date(page: Page, picker_index: int, departure_date: str, timeout_ms: int) -> None:
    await choose_date_by_trigger(
        page,
        f"#multiDatePicker{picker_index} .modifyDate",
        0,
        departure_date,
        timeout_ms,
    )


async def choose_date_by_trigger(
    page: Page,
    trigger_selector: str,
    picker_index: int,
    departure_date: str,
    timeout_ms: int,
) -> None:
    trigger = page.locator(trigger_selector).nth(picker_index)
    if await trigger_has_date_value(trigger, departure_date):
        return

    await trigger.click(timeout=timeout_ms // 4, force=True)
    await page.wait_for_timeout(500)
    if await try_click_date_candidate(page, departure_date, timeout_ms // 4):
        await settle_datepicker(page, trigger, timeout_ms)
        if await trigger_has_date_value(trigger, departure_date):
            await page.wait_for_timeout(700)
            return

    if await try_assign_date_value(trigger, departure_date):
        await settle_datepicker(page, trigger, timeout_ms)
        if await trigger_has_date_value(trigger, departure_date):
            await page.wait_for_timeout(700)
            return

    if await try_fill_date_input(trigger, departure_date, timeout_ms):
        await settle_datepicker(page, trigger, timeout_ms)
        if await trigger_has_date_value(trigger, departure_date):
            await page.wait_for_timeout(700)
            return

    await trigger.click(timeout=timeout_ms // 4, force=True)
    await page.wait_for_timeout(300)
    await navigate_calendar_to_month_safe(page, departure_date, timeout_ms)
    if await try_click_date_candidate(page, departure_date, timeout_ms // 4):
        await settle_datepicker(page, trigger, timeout_ms)
        if await trigger_has_date_value(trigger, departure_date):
            await page.wait_for_timeout(700)
            return

    if await try_assign_date_value(trigger, departure_date):
        await settle_datepicker(page, trigger, timeout_ms)
        if await trigger_has_date_value(trigger, departure_date):
            await page.wait_for_timeout(700)
            return

    if await try_fill_date_input(trigger, departure_date, timeout_ms):
        await settle_datepicker(page, trigger, timeout_ms)
        if await trigger_has_date_value(trigger, departure_date):
            await page.wait_for_timeout(700)
            return

    await dump_datepicker_debug(page, departure_date, trigger_selector)
    raise RuntimeError(f"Could not select Ctrip date {departure_date}.")


async def trigger_has_date_value(trigger, departure_date: str) -> bool:
    expected = departure_date.strip()
    inputs = [trigger.locator("input").first]
    for locator in inputs:
        try:
            value = (await locator.input_value(timeout=1500)).strip()
        except Exception:
            continue
        if value == expected or expected.replace("-", "/") in value:
            return True
    try:
        text = (await trigger.inner_text()).strip()
    except Exception:
        text = ""
    return expected in text or expected.replace("-", "/") in text


async def try_assign_date_value(trigger, departure_date: str) -> bool:
    script = """(root, dateValue) => {
        const input = root.querySelector('input');
        if (!input) return false;
        input.removeAttribute('readonly');
        input.removeAttribute('disabled');
        const previousValue = input.value;
        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
        if (nativeSetter) {
            nativeSetter.call(input, dateValue);
        } else {
            input.value = dateValue;
        }
        input.setAttribute('value', dateValue);
        if (input._valueTracker) {
            input._valueTracker.setValue(previousValue);
        }
        input.dispatchEvent(new Event('focus', { bubbles: true }));
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        input.dispatchEvent(new Event('blur', { bubbles: true }));
        input.dispatchEvent(new Event('focusout', { bubbles: true }));
        const field = root.closest('.form-item-v3');
        if (field) {
            field.classList.remove('none-value');
            field.classList.remove('error');
            const hint = field.querySelector('.error-hint');
            if (hint) hint.style.display = 'none';
        }
        return input.value || '';
    }"""
    try:
        result = await trigger.evaluate(script, departure_date)
        return bool(result) and departure_date in str(result)
    except Exception:
        return False


async def try_fill_date_input(trigger, departure_date: str, timeout_ms: int) -> bool:
    try:
        input_locator = trigger.locator("input").first
        await input_locator.evaluate("(node) => { node.removeAttribute('readonly'); node.removeAttribute('disabled'); }")
        await input_locator.fill(departure_date, timeout=timeout_ms // 8)
        await input_locator.press("Tab", timeout=timeout_ms // 8)
        return True
    except Exception:
        return False


async def settle_datepicker(page: Page, trigger, timeout_ms: int) -> None:
    try:
        await trigger.evaluate("(node) => node.blur()")
    except Exception:
        pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(180)

    try:
        await page.locator("body").click(position={"x": 24, "y": 24}, timeout=timeout_ms // 6, force=True)
    except Exception:
        pass
    await page.wait_for_timeout(180)

    # Multi-city date headers can linger above the submit button even after a valid date click.
    for _ in range(3):
        if not await is_datepicker_overlay_visible(page):
            return
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


async def is_datepicker_overlay_visible(page: Page) -> bool:
    selectors = [
        ".date-header",
        ".date-picker",
        "[class*='date-picker']",
        "[class*='calendar']",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 6)
        except Exception:
            continue
        for index in range(count):
            try:
                if await locator.nth(index).is_visible():
                    return True
            except Exception:
                continue
    return False


async def get_active_calendar_scope(page: Page):
    selectors = [
        ".calendar-modal:not([style*='display: none'])",
        "[class*='calendar-modal']:not([style*='display: none'])",
        ".date-picker:not([style*='display: none'])",
        "[class*='date-picker']:not([style*='display: none'])",
        "[class*='calendar']:not([style*='display: none'])",
        ".react-datepicker:not([style*='display: none'])",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 8)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    return item
            except Exception:
                continue
    return page.locator("body")


async def try_click_date_candidate(page: Page, departure_date: str, timeout_ms: int) -> bool:
    calendar = await get_active_calendar_scope(page)
    target_day = str(int(departure_date[-2:]))
    selectors = [
        f"[data-testid='date-day-{departure_date}']:not(.date-disabled)",
        f"[data-date='{departure_date}']:not(.date-disabled)",
        f"[title='{departure_date}']:not(.disabled):not(.date-disabled)",
        f"td[title='{departure_date}']:not(.disabled):not(.date-disabled)",
        f"div[title='{departure_date}']:not(.disabled):not(.date-disabled)",
    ]
    for selector in selectors:
        try:
            locator = calendar.locator(selector).first
            if await locator.count():
                await locator.click(timeout=timeout_ms)
                return True
        except Exception:
            continue

    locator_groups = [
        calendar.get_by_text(target_day, exact=True),
        calendar.locator("td, button, div, span").get_by_text(target_day, exact=True),
    ]
    for locator in locator_groups:
        try:
            count = min(await locator.count(), 12)
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                class_name = ((await candidate.get_attribute("class")) or "").lower()
                aria_disabled = ((await candidate.get_attribute("aria-disabled")) or "").lower()
                if "disabled" in class_name or aria_disabled == "true":
                    continue
                await candidate.click(timeout=timeout_ms)
                return True
            except Exception:
                continue
    return False


async def navigate_calendar_to_month_safe(page: Page, departure_date: str, timeout_ms: int) -> None:
    target = datetime.strptime(departure_date, "%Y-%m-%d")
    target_labels = [
        f"{target.year}\u5e74{target.month}\u6708",
        f"{target.year}-{target.month:02d}",
        f"{target.year}/{target.month:02d}",
        f"{target.month}\u6708",
    ]

    for _ in range(12):
        calendar = await get_active_calendar_scope(page)
        try:
            calendar_text = await calendar.inner_text(timeout=timeout_ms // 6)
        except Exception:
            calendar_text = ""
        if any(label in calendar_text for label in target_labels):
            return

        moved = await click_calendar_nav_safe(page, timeout_ms // 6)
        if not moved:
            return
        await page.wait_for_timeout(350)


async def click_calendar_nav_safe(page: Page, timeout_ms: int) -> bool:
    calendar = await get_active_calendar_scope(page)
    try:
        icons = calendar.locator(".next-ico.iconf-right")
        count = await icons.count()
        for index in range(count - 1, -1, -1):
            item = icons.nth(index)
            try:
                if await item.is_visible():
                    await item.click(timeout=timeout_ms, force=True)
                    return True
            except Exception:
                try:
                    await item.evaluate("(node) => node.click()")
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    selectors = [
        "button[aria-label*='\u4e0b\u4e00']",
        "button[aria-label*='next']",
        "[class*='month-next']",
        "[class*='nextMonth']",
        "[class*='arrow-right']",
        "[class*='next']",
        "button:has-text('>')",
    ]
    for selector in selectors:
        try:
            locator = calendar.locator(selector).first
            if await locator.count():
                await locator.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def navigate_calendar_to_month(page: Page, departure_date: str, timeout_ms: int) -> None:
    await navigate_calendar_to_month_safe(page, departure_date, timeout_ms)


async def click_calendar_nav(page: Page, timeout_ms: int) -> bool:
    return await click_calendar_nav_safe(page, timeout_ms)


async def dump_datepicker_debug(page: Page, departure_date: str, trigger_selector: str) -> None:
    try:
        dump_dir = Path("runtime") / "datepicker_debug"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        prefix = dump_dir / f"ctrip_datepicker_{departure_date}_{stamp}"

        await page.screenshot(path=str(prefix.with_suffix(".png")), full_page=True)
        prefix.with_suffix(".html").write_text(await page.content(), encoding="utf-8")

        body_text = await page.locator("body").inner_text()
        prefix.with_suffix(".txt").write_text(body_text, encoding="utf-8")

        meta = {
            "departure_date": departure_date,
            "trigger_selector": trigger_selector,
            "url": page.url,
            "captured_at": datetime.utcnow().isoformat() + "Z",
        }
        prefix.with_suffix(".json").write_text(str(meta), encoding="utf-8")
        LOGGER.warning("saved ctrip datepicker debug artifacts to %s", prefix)
    except Exception:
        LOGGER.exception("failed to dump ctrip datepicker debug artifacts for %s", departure_date)
