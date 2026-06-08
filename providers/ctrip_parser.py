import re
from typing import Any

from playwright.async_api import Page

from config_manager import RouteQuery
from providers.ctrip_support import offer_matches_cabin


PRICE_PATTERN = re.compile(r"(?<!\d)(?:CNY|RMB|¥)?\s*([1-9]\d{1,5}(?:\.\d{1,2})?)")
TIME_PATTERN = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
FLIGHT_NO_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]\d{3,5})\b")
MIN_REASONABLE_PRICE = 200
MAX_REASONABLE_PRICE = 50000
KNOWN_NON_PRICE_VALUES = {95010}
PROMO_SPLIT_TOKENS = [
    "类似",
    "国内旅游目的地",
    "热门目的地",
    "酒店",
    "景点",
    "门票",
    "跟团",
    "自由行",
    "玩乐",
    "攻略",
]
PRICE_MARKERS = ["¥", "CNY", "RMB", "起", "含税价", "未税价"]
TRANSFER_BLOCK_TOKENS = [
    "中转",
    "转机",
    "经停",
    "转昆明",
    "转上海",
    "转北京",
    "转广州",
    "转深圳",
    "转成都",
]
FLIGHT_CONTEXT_TOKENS = [
    "航班详情",
    "含税价",
    "未税价",
    "订票",
    "直飞",
    "中转",
    "经停",
    "飞行",
    "机场",
    "T1",
    "T2",
    "T3",
]
DATE_STRIP_PATTERN = re.compile(r"\d{2}-\d{2}[\u4e00-\u9fa5]{0,2}\s*¥\d+")


async def extract_offer(automation, page: Page, route: RouteQuery):
    candidates = []
    row_selectors = [".box-row", ".search-result-item", "li[class*='flight']"]
    price_selectors = [
        ".box-operate [class*='price']",
        "[class*='price-detail']",
        "span.price",
        "[class*='price']",
    ]

    for row_selector in row_selectors:
        rows = page.locator(row_selector)
        try:
            count = min(await rows.count(), 20)
        except Exception:
            continue

        for row_index in range(count):
            row = rows.nth(row_index)
            try:
                row_text = sanitize_offer_text((await row.inner_text()).strip())
            except Exception:
                row_text = None
            departure_time = extract_departure_time(row_text)

            for price_selector in price_selectors:
                try:
                    texts = await row.locator(price_selector).all_inner_texts()
                except Exception:
                    continue
                for text in texts:
                    cleaned_text = sanitize_offer_text(text)
                    for price in extract_reasonable_prices(cleaned_text):
                        candidates.append(
                            automation.offer_details_type(
                                price=price,
                                departure_time=departure_time,
                                row_text=row_text,
                            )
                        )

    candidates.extend(await extract_offer_from_visible_text(automation, page))

    if not candidates:
        selectors = automation._selector_list(automation.app_config.providers["ctrip"], "lowest_price")
        texts = await automation._texts_for_selectors(page, selectors, limit=20)
        for text in texts:
            cleaned_text = sanitize_offer_text(text)
            primary_price = extract_primary_price(cleaned_text)
            if primary_price is not None:
                candidates.append(automation.offer_details_type(price=primary_price, row_text=cleaned_text))

    filtered = [
        offer
        for offer in candidates
        if is_reasonable_price(offer.price)
        and offer_looks_like_result_row(offer.row_text)
        and offer_matches_cabin(offer.row_text, route.cabin)
        and offer_matches_transfer_policy(offer.row_text, route.transfer_policy)
    ]
    if not filtered:
        return None
    return min(filtered, key=lambda offer: offer.price)


def parse_single_flight_details(text: Any, route: RouteQuery) -> list[dict[str, Any]]:
    if not isinstance(text, str) or not text.strip():
        return []

    normalized = text.replace("\u00a0", " ").replace("\u806a", " ")
    parts = [part.strip() for part in normalized.split("|") if part.strip()]
    flight_part_index: int | None = None
    flight_no: str | None = None
    for index, part in enumerate(parts):
        match = FLIGHT_NO_PATTERN.search(part)
        if match:
            flight_part_index = index
            flight_no = match.group(1)
            break
    if flight_part_index is None or not flight_no:
        return []

    time_indices = [index for index, part in enumerate(parts) if TIME_PATTERN.fullmatch(part)]
    departure_index = time_indices[0] if time_indices else None
    arrival_index = time_indices[1] if len(time_indices) > 1 else None
    flight_part = parts[flight_part_index]
    aircraft = flight_part.replace(flight_no, "").strip(" -/") or None
    airline = parts[flight_part_index - 1] if flight_part_index > 0 else None

    return [
        {
            "segment_index": 1,
            "origin": route.origin,
            "destination": route.destination,
            "departure_date": route.departure_date,
            "airline": airline,
            "flight_no": flight_no,
            "cabin": route.cabin,
            "departure_time": parts[departure_index] if departure_index is not None else None,
            "departure_airport": (
                parts[departure_index + 1]
                if departure_index is not None and departure_index + 1 < len(parts)
                else None
            ),
            "arrival_time": parts[arrival_index] if arrival_index is not None else None,
            "arrival_airport": (
                parts[arrival_index + 1]
                if arrival_index is not None and arrival_index + 1 < len(parts)
                else None
            ),
            "aircraft": aircraft,
        }
    ]


async def extract_offer_from_visible_text(automation, page: Page) -> list:
    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        return []

    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    candidates = []
    for index, line in enumerate(lines):
        cleaned_line = sanitize_offer_text(line)
        if not cleaned_line:
            continue
        primary_price = extract_primary_price(cleaned_line)
        if primary_price is None:
            continue

        previous_times: list[str] = []
        row_tokens = [cleaned_line]
        cursor = index - 1
        while cursor >= 0 and len(row_tokens) < 8:
            token = lines[cursor]
            if contains_promo_tokens(token):
                break
            row_tokens.insert(0, sanitize_offer_text(token))
            if TIME_PATTERN.fullmatch(token):
                previous_times.append(token)
            cursor -= 1

        departure_time = previous_times[-1] if previous_times else None
        row_text = " | ".join(token for token in row_tokens if token)
        candidates.append(
            automation.offer_details_type(
                price=primary_price,
                departure_time=departure_time,
                row_text=row_text,
            )
        )
    return candidates


def extract_visible_price_tokens(text: str) -> list[float]:
    normalized = sanitize_offer_text(text).strip().replace(",", "")
    if not normalized:
        return []
    if re.fullmatch(r"\d{3,5}(?:\.\d{1,2})?", normalized):
        value = float(normalized)
        return [value] if is_reasonable_price(value) else []
    if any(marker in normalized for marker in PRICE_MARKERS):
        return extract_reasonable_prices(normalized)
    return []


def extract_reasonable_prices(text: str) -> list[float]:
    numbers = []
    for match in PRICE_PATTERN.finditer(sanitize_offer_text(text).replace(",", "")):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if is_reasonable_price(value):
            numbers.append(value)
    return numbers


def extract_primary_price(text: str | None) -> float | None:
    prices = extract_visible_price_tokens(text or "")
    return prices[0] if prices else None


def sanitize_offer_text(text: str | None) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    for token in PROMO_SPLIT_TOKENS:
        marker = normalized.find(token)
        if marker > 0:
            normalized = normalized[:marker].strip(" |/")
            break
    return normalized


def contains_promo_tokens(text: str | None) -> bool:
    normalized = (text or "").strip()
    return any(token in normalized for token in PROMO_SPLIT_TOKENS)


def offer_looks_like_result_row(text: str | None) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return False
    if TIME_PATTERN.search(normalized):
        return True
    if any(token in normalized for token in FLIGHT_CONTEXT_TOKENS):
        return True
    if len(DATE_STRIP_PATTERN.findall(normalized)) >= 2:
        return False
    return False


def is_reasonable_price(value: float) -> bool:
    return MIN_REASONABLE_PRICE <= value <= MAX_REASONABLE_PRICE and int(value) not in KNOWN_NON_PRICE_VALUES


def extract_departure_time(text: str | None) -> str | None:
    if not text:
        return None
    match = TIME_PATTERN.search(text)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def offer_matches_transfer_policy(text: str | None, policy: str | None) -> bool:
    if (policy or "any").lower() != "direct_only":
        return True
    if not text:
        return True
    return not any(token in text for token in TRANSFER_BLOCK_TOKENS)
