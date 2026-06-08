import re
from typing import Any

from adaptive_selectors import SelectorCandidate, observe_selector_candidates
from providers.ctrip_support import ctrip_city_code
from tail_discovery import TAIL_CODE_TEXT


TRANSIT_CARD_SELECTOR = ".list-content-item-transit"
TRANSIT_CARD_SELECTORS = [
    SelectorCandidate(
        ".list-content-item-transit",
        "ctrip_transit_card_primary",
        must_contain_any=("\u8f6c", "\u4e2d\u8f6c", "transfer"),
        nice_to_have_any=("\u8ba2\u7968", "\u822a\u73ed", "\u00a5", "\uffe5"),
    ),
    SelectorCandidate(
        ".list-content-item:has(.transfer-info)",
        "ctrip_transit_card_with_transfer_info",
        must_contain_any=("\u8f6c", "\u4e2d\u8f6c", "transfer"),
        nice_to_have_any=("\u8ba2\u7968", "\u822a\u73ed", "\u00a5", "\uffe5"),
    ),
    SelectorCandidate(
        ".search-result-item:has(.transfer-info)",
        "ctrip_search_result_with_transfer_info",
        must_contain_any=("\u8f6c", "\u4e2d\u8f6c", "transfer"),
        nice_to_have_any=("\u8ba2\u7968", "\u822a\u73ed", "\u00a5", "\uffe5"),
    ),
]
TRANSIT_CARD_LIMIT = 40
AIRLINE_ALIASES = {
    "CA": ("CA", "\u4e2d\u56fd\u56fd\u822a", "\u56fd\u822a", "\u5927\u8fde\u822a\u7a7a", "AIR CHINA"),
    "MU": ("MU", "\u4e1c\u65b9\u822a\u7a7a", "\u4e1c\u822a", "CHINA EASTERN"),
    "CZ": ("CZ", "\u5357\u65b9\u822a\u7a7a", "\u5357\u822a", "CHINA SOUTHERN"),
    "ZH": ("ZH", "\u6df1\u5733\u822a\u7a7a", "\u6df1\u822a", "SHENZHEN AIRLINES"),
    "HU": ("HU", "\u6d77\u5357\u822a\u7a7a", "\u6d77\u822a", "HAINAN AIRLINES"),
    "MF": ("MF", "\u53a6\u95e8\u822a\u7a7a", "\u53a6\u822a", "XIAMEN AIR"),
    "SC": ("SC", "\u5c71\u4e1c\u822a\u7a7a", "\u5c71\u822a", "SHANDONG AIRLINES"),
    "JD": ("JD", "\u9996\u90fd\u822a\u7a7a", "\u9996\u822a", "CAPITAL AIRLINES"),
    "KN": ("KN", "\u4e2d\u56fd\u8054\u5408\u822a\u7a7a", "\u8054\u822a", "CHINA UNITED"),
    "AQ": ("AQ", "\u4e5d\u5143\u822a\u7a7a", "\u4e5d\u5143", "9 AIR"),
    "3U": ("3U", "\u56db\u5ddd\u822a\u7a7a", "\u5ddd\u822a", "SICHUAN AIRLINES"),
    "TV": ("TV", "\u897f\u85cf\u822a\u7a7a", "\u85cf\u822a", "TIBET AIRLINES"),
    "KY": ("KY", "\u6606\u660e\u822a\u7a7a", "\u6606\u822a", "KUNMING AIRLINES"),
    "NX": ("NX", "\u6fb3\u95e8\u822a\u7a7a", "\u6fb3\u822a", "AIR MACAU"),
    "FM": ("FM", "\u4e0a\u6d77\u822a\u7a7a", "\u4e0a\u822a", "SHANGHAI AIRLINES"),
    "HO": ("HO", "\u5409\u7965\u822a\u7a7a", "\u5409\u7965", "JUNEYAO AIRLINES"),
    "9C": ("9C", "\u6625\u79cb\u822a\u7a7a", "\u6625\u79cb", "SPRING AIRLINES"),
    "GS": ("GS", "\u5929\u6d25\u822a\u7a7a", "\u5929\u822a", "TIANJIN AIRLINES"),
    "GJ": ("GJ", "\u957f\u9f99\u822a\u7a7a", "\u957f\u9f99", "LOONG AIR"),
    "EU": ("EU", "\u6210\u90fd\u822a\u7a7a", "\u6210\u822a", "CHENGDU AIRLINES"),
    "DR": ("DR", "\u745e\u4e3d\u822a\u7a7a", "\u745e\u4e3d", "RUILI AIRLINES"),
}
AIRLINE_GROUPS = {
    "\u56fd\u822a\u7cfb\u822a\u53f8": ("CA", "ZH", "TV", "SC", "KY", "NX"),
    "\u56fd\u822a\u7cfb": ("CA", "ZH", "TV", "SC", "KY", "NX"),
    "AIR_CHINA_GROUP": ("CA", "ZH", "TV", "SC", "KY", "NX"),
}


def tail_text_tokens(code: str) -> tuple[str, ...]:
    normalized = str(code or "").upper()
    tokens = {normalized, ctrip_city_code(normalized)}
    tokens.update(TAIL_CODE_TEXT.get(normalized, ()))
    tokens.update(TAIL_CODE_TEXT.get(ctrip_city_code(normalized), ()))
    return tuple(token for token in tokens if token)


def extract_price_from_text(text: str) -> float | None:
    normalized = text.replace(",", "")
    matches = []
    matches.extend(re.findall(r"[\u00a5\uffe5]\s*([1-9]\d{2,5})", normalized))
    matches.extend(re.findall(r"([1-9]\d{2,5})\s*\u8d77", normalized))
    if not matches:
        return None
    prices = [float(item) for item in matches if 200 <= float(item) <= 100000]
    return min(prices) if prices else None


def extract_transfer_mentions(text: str) -> list[str]:
    mentions = re.findall(
        r"(?<!\u4e2d)\u8f6c\s*([\u4e00-\u9fffA-Za-z]{1,12})(?=\d|[hH]|\s*\d|[\uff0c,\u3001\s]|$)",
        text,
    )
    cleaned: list[str] = []
    for item in mentions:
        value = item.strip()
        if not value or value in cleaned:
            continue
        cleaned.append(value)
    return cleaned


def split_result_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    for part in text.split("\u8ba2\u7968"):
        candidate = part.strip()
        if not candidate:
            continue
        start = max(
            candidate.rfind("\u4f4e\u4ef7\u63d0\u9192"),
            candidate.rfind("\u822a\u53f8\u6bd4\u4ef7\u8868"),
            candidate.rfind("\u542b\u7a0e\u4ef7", 0, max(0, len(candidate) - 80)),
        )
        if start > 0 and start < len(candidate) - 20:
            candidate = candidate[start:]
        blocks.append(candidate)
    return blocks


def find_tail_match_from_cards(
    cards: list[dict[str, Any]],
    *,
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
    cabin: str,
    preferred_airlines: list[str] | None = None,
) -> dict[str, Any] | None:
    tokens = tail_text_tokens(transfer)
    best: dict[str, Any] | None = None
    for card in cards:
        price = coerce_price(card.get("price"))
        if price is None:
            continue
        searchable = " ".join(str(card.get(key) or "") for key in ("text", "transfer_text", "transfer_name"))
        if not any(token and token in searchable for token in tokens):
            continue
        details = build_tail_details_from_card(
            card,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
            cabin=cabin,
        )
        if not details_match_airlines(details, preferred_airlines):
            continue
        candidate = {
            "total_price": price,
            "details": details,
            "card": card,
            "detail_source": "dom_transit_card",
            "detail_quality": "partial",
        }
        if best is None or price < float(best["total_price"]):
            best = candidate
    return best


def find_tail_match_from_text_blocks(
    blocks: list[str],
    *,
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
    cabin: str,
    preferred_airlines: list[str] | None = None,
) -> dict[str, Any] | None:
    tokens = tail_text_tokens(transfer)
    best: tuple[float, str] | None = None
    for block in blocks:
        compact = " ".join(part.strip() for part in block.splitlines() if part.strip())
        if not is_probable_tail_result_block(compact, transfer=transfer):
            continue
        if not any(token and token in compact for token in tokens):
            continue
        price = extract_price_from_text(compact)
        if price is None:
            continue
        isolated = isolate_tail_text_block(compact, transfer=transfer)
        details = build_tail_details_from_text_block(
            isolated,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
            cabin=cabin,
        )
        if not details_have_text_fallback_evidence(details, isolated):
            continue
        if not details_match_airlines(details, preferred_airlines):
            continue
        if best is None or price < best[0]:
            best = (price, isolated)
    if best is None:
        return None
    price, row_text = best
    row_text = isolate_tail_text_block(row_text, transfer=transfer)
    return {
        "total_price": price,
        "details": build_tail_details_from_text_block(
            row_text,
            origin=origin,
            transfer=transfer,
            destination=destination,
            departure_date=departure_date,
            cabin=cabin,
        ),
        "detail_source": "text_fallback",
        "detail_quality": "partial",
    }


def build_tail_details_from_card(
    card: dict[str, Any],
    *,
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
    cabin: str,
) -> list[dict[str, Any]]:
    airline_items = card.get("airline_items")
    if not isinstance(airline_items, list):
        airline_items = []
    first_flight = airline_items[0] if airline_items and isinstance(airline_items[0], dict) else {}
    second_flight = airline_items[1] if len(airline_items) > 1 and isinstance(airline_items[1], dict) else {}
    transfer_name = str(card.get("transfer_name") or transfer)
    cabin_texts = card.get("cabin_texts")
    if not isinstance(cabin_texts, list):
        cabin_texts = []
    first_cabin = str(cabin_texts[0]) if cabin_texts else cabin
    second_cabin = str(cabin_texts[1]) if len(cabin_texts) > 1 else first_cabin
    row_text = str(card.get("text") or "")
    return [
        {
            "segment_index": 1,
            "origin": origin,
            "destination": transfer,
            "departure_airport": card.get("departure_airport") or origin,
            "arrival_airport": transfer_name,
            "departure_date": departure_date,
            "departure_time": card.get("departure_time"),
            "arrival_time": None,
            "airline": first_flight.get("airline") if isinstance(first_flight, dict) else None,
            "flight_no": first_flight.get("flight_no") if isinstance(first_flight, dict) else None,
            "aircraft": first_flight.get("plane_text") if isinstance(first_flight, dict) else None,
            "cabin": first_cabin,
            "row_text": row_text,
            "tail_transfer": transfer,
            "transfer_layover": card.get("transfer_layover"),
            "transfer_duration": card.get("transfer_duration"),
        },
        {
            "segment_index": 2,
            "origin": transfer,
            "destination": destination,
            "departure_airport": transfer_name,
            "arrival_airport": card.get("arrival_airport") or destination,
            "departure_date": departure_date,
            "departure_time": None,
            "arrival_time": card.get("arrival_time"),
            "airline": second_flight.get("airline") if isinstance(second_flight, dict) else None,
            "flight_no": second_flight.get("flight_no") if isinstance(second_flight, dict) else None,
            "aircraft": second_flight.get("plane_text") if isinstance(second_flight, dict) else None,
            "cabin": second_cabin,
            "row_text": row_text,
            "tail_transfer": transfer,
            "transfer_layover": card.get("transfer_layover"),
            "transfer_duration": card.get("transfer_duration"),
        },
    ]


def build_tail_details_from_text_block(
    row_text: str,
    *,
    origin: str,
    transfer: str,
    destination: str,
    departure_date: str,
    cabin: str,
) -> list[dict[str, Any]]:
    row_text = isolate_tail_text_block(row_text, transfer=transfer)
    lines = [line.strip().replace("\xa0", " ") for line in row_text.splitlines() if line.strip()]
    compact = " ".join(lines) if lines else row_text
    flight_lines = extract_flight_lines(compact, lines)
    times = re.findall(r"\b([0-2]?\d:[0-5]\d)\b", compact)
    airports = extract_airport_mentions(compact)
    transfer_names = extract_transfer_mentions(compact)
    transfer_name = transfer_names[0] if transfer_names else transfer
    first_flight = parse_flight_line(flight_lines[0] if flight_lines else "")
    second_flight = parse_flight_line(flight_lines[1] if len(flight_lines) > 1 else "")
    airline_names = extract_airline_names(compact)
    if not first_flight.get("airline") and airline_names:
        first_flight["airline"] = airline_names[0]
    if not second_flight.get("airline") and len(airline_names) > 1:
        second_flight["airline"] = airline_names[1]
    first_departure = times[0] if times else None
    final_arrival = times[-1] if times else None
    first_airport = airports[0] if airports else origin
    final_airport = airports[-1] if airports else destination
    summary_text = compact[:500]
    return [
        {
            "segment_index": 1,
            "origin": origin,
            "destination": transfer,
            "departure_airport": first_airport,
            "arrival_airport": transfer_name,
            "departure_date": departure_date,
            "departure_time": first_departure,
            "arrival_time": None,
            "airline": first_flight.get("airline"),
            "flight_no": first_flight.get("flight_no"),
            "aircraft": first_flight.get("aircraft"),
            "cabin": cabin,
            "tail_transfer": transfer,
            "summary_text": summary_text,
        },
        {
            "segment_index": 2,
            "origin": transfer,
            "destination": destination,
            "departure_airport": transfer_name,
            "arrival_airport": final_airport,
            "departure_date": departure_date,
            "departure_time": None,
            "arrival_time": final_arrival,
            "airline": second_flight.get("airline"),
            "flight_no": second_flight.get("flight_no"),
            "aircraft": second_flight.get("aircraft"),
            "cabin": cabin,
            "tail_transfer": transfer,
            "summary_text": summary_text,
        },
    ]


def is_probable_tail_result_block(text: str, *, transfer: str) -> bool:
    compact = " ".join(str(text or "").replace("\xa0", " ").split())
    if not compact or len(compact) > 2500:
        return False
    noise_markers = (
        "暂时无法查询到对应价格",
        "航班信息免责声明",
        "热门机票",
        "机票工具箱",
        "网站导航",
        "隐私政策",
        "现在注册携程会员",
        "旅游资讯",
    )
    if any(marker in compact for marker in noise_markers):
        return False
    transfer_tokens = [token for token in tail_text_tokens(transfer) if token]
    has_explicit_transfer = any(
        re.search(rf"(?:中转|转)\s*{re.escape(token)}", compact)
        for token in transfer_tokens
        if not token.isascii()
    )
    if not has_explicit_transfer:
        return False
    if extract_price_from_text(compact) is None:
        return False
    if len(re.findall(r"\b[0-2]?\d:[0-5]\d\b", compact)) < 2:
        return False
    if not re.search(r"(?<![A-Z0-9])([A-Z0-9]{2})\s*\d{3,4}(?![A-Z0-9])", compact):
        return False
    return True


def details_have_text_fallback_evidence(details: list[dict[str, Any]], row_text: str) -> bool:
    if len(details) < 2:
        return False
    flight_numbers = [str(item.get("flight_no") or "").strip() for item in details]
    if not any(flight_numbers):
        return False
    if len(re.findall(r"\b[0-2]?\d:[0-5]\d\b", row_text or "")) < 2:
        return False
    summary = " ".join(str(item.get("summary_text") or "") for item in details)
    if any(marker in summary for marker in ("航班信息免责声明", "热门机票", "机票工具箱", "网站导航")):
        return False
    return True


def build_diagnostics(cards: list[dict[str, Any]], blocks: list[str], *, transfer: str, page_preview: str = "") -> str:
    summaries: list[dict[str, Any]] = []
    for card in cards:
        summaries.append(
            {
                "price": coerce_price(card.get("price")),
                "transfers": [str(card.get("transfer_name") or "")],
            }
        )
    for block in blocks:
        compact = " ".join(part.strip() for part in block.splitlines() if part.strip())
        summaries.append(
            {
                "price": extract_price_from_text(compact),
                "transfers": extract_transfer_mentions(compact),
            }
        )
    summaries = [item for item in summaries if item["price"] is not None or item["transfers"]]
    summaries.sort(key=lambda item: float(item["price"] or 999999))
    transfer_tokens = "/".join(tail_text_tokens(transfer)[:5])
    if not summaries:
        return (
            f"No parseable flight cards were found. Looking for transfer tokens: {transfer_tokens}. "
            f"Page text preview: {page_preview[:260]}"
        )
    top = summaries[:6]
    transfer_names = sorted({name for item in top for name in item["transfers"] if name}) or ["none"]
    price_text = ", ".join(
        f"{int(item['price']) if item['price'] else '-'} via {('/'.join(item['transfers']) or 'direct/unknown')}"
        for item in top
    )
    return (
        f"Looking for transfer tokens: {transfer_tokens}. "
        f"Visible transfer mentions in top cards: {', '.join(transfer_names)}. "
        f"Top prices: {price_text}."
    )


def isolate_tail_text_block(text: str, *, transfer: str) -> str:
    compact = " ".join(str(text or "").replace("\xa0", " ").split())
    if not compact:
        return ""
    tokens = [token for token in tail_text_tokens(transfer) if token and not token.isascii()]
    transfer_positions = [
        match.start()
        for token in tokens
        for match in re.finditer(rf"(?<!\u4e2d)\u8f6c\s*{re.escape(token)}", compact)
    ]
    if not transfer_positions:
        return compact
    position = min(transfer_positions)
    start_markers = [
        compact.rfind("\u8ba2\u7968", 0, position),
        compact.rfind("\u4e2d\u8f6c\u7ec4\u5408\u8d2d\u4e70\u987b\u77e5", 0, position),
    ]
    start = max(start_markers)
    start = 0 if start < 0 else start + (2 if compact[start : start + 2] == "\u8ba2\u7968" else 0)
    end = compact.find("\u8ba2\u7968", position)
    if end < 0:
        end = len(compact)
    return compact[start:end].strip() or compact


async def extract_transit_cards_from_page(page) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    try:
        observations = await observe_selector_candidates(page, TRANSIT_CARD_SELECTORS)
    except Exception:
        observations = [{"selector": TRANSIT_CARD_SELECTOR, "label": "ctrip_transit_card_primary"}]

    for observation in observations:
        selector = str(observation.get("selector") or TRANSIT_CARD_SELECTOR)
        is_primary_selector = selector == TRANSIT_CARD_SELECTOR
        if int(observation.get("score") or -100) <= 0 and not is_primary_selector:
            continue
        try:
            cards = page.locator(selector)
            count = min(await cards.count(), TRANSIT_CARD_LIMIT)
        except Exception:
            continue
        parsed = []
        seen_texts: set[str] = set()
        for index in range(count):
            try:
                row = cards.nth(index)
                await row.evaluate("(node, index) => node.setAttribute('data-tail-card-index', String(index))", index)
                card = await row.evaluate(TRANSIT_CARD_SCRIPT)
            except Exception:
                continue
            if not isinstance(card, dict) or not card.get("text"):
                continue
            text = str(card.get("text") or "")
            if text in seen_texts:
                continue
            seen_texts.add(text)
            card["selector_label"] = observation.get("label")
            card["selector"] = observation.get("selector")
            card["selector_score"] = observation.get("score")
            parsed.append(card)
        if parsed:
            return parsed
    return []


async def text_blocks_from_page(page, body_text: str) -> list[str]:
    blocks: list[str] = []
    for selector in [".box-row", ".search-result-item"]:
        try:
            rows = page.locator(selector)
            count = min(await rows.count(), 30)
        except Exception:
            continue
        for index in range(count):
            try:
                text = (await rows.nth(index).inner_text()).strip()
            except Exception:
                continue
            if text and text not in blocks:
                blocks.append(text)
    for block in split_result_blocks(body_text):
        if block and block not in blocks:
            blocks.append(block)
    return blocks


def coerce_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.replace(",", "").strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return float(text)
    return None


def first_time_in_text(text: str) -> str | None:
    match = re.search(r"\b([0-2]\d:[0-5]\d)\b", text)
    return match.group(1) if match else None


def last_time_in_text(text: str) -> str | None:
    matches = re.findall(r"\b([0-2]\d:[0-5]\d)\b", text)
    return matches[-1] if matches else None


def first_line(text: str) -> str | None:
    for part in re.split(r"\s{2,}|\|", text):
        cleaned = part.strip()
        if cleaned:
            return cleaned[:60]
    return None


def extract_airport_mentions(text: str) -> list[str]:
    mentions = re.findall(r"[\u4e00-\u9fff]{2,18}\u673a\u573a(?:T\d)?", text)
    cleaned: list[str] = []
    for mention in mentions:
        value = mention.strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def extract_flight_lines(compact: str, lines: list[str]) -> list[str]:
    direct_lines = [line for line in lines if re.search(r"[A-Z0-9]{2}\s*\d{3,4}", line)]
    if direct_lines:
        return direct_lines
    matches = re.findall(r"([\u4e00-\u9fffA-Za-z|]{0,12}[A-Z0-9]{2}\s*\d{3,4}[^\s]*)", compact)
    return [match.strip() for match in matches if match.strip()]


def extract_airline_names(text: str) -> list[str]:
    names = re.findall(r"[\u4e00-\u9fff]{2,8}\u822a\u7a7a", text)
    cleaned: list[str] = []
    for name in names:
        if name in {"航空"}:
            continue
        if name and name not in cleaned:
            cleaned.append(name)
    if len(cleaned) == 1 and text.count(cleaned[0]) >= 2:
        cleaned.append(cleaned[0])
    return cleaned[:2]


def parse_flight_line(text: str) -> dict[str, str | None]:
    cleaned = " ".join(str(text or "").split())
    match = re.search(r"([A-Z0-9]{2})\s*(\d{3,4})", cleaned)
    if not match:
        return {"airline": None, "flight_no": None, "aircraft": None}
    flight_no = f"{match.group(1)}{match.group(2)}"
    airline = cleaned[: match.start()].strip() or match.group(1)
    aircraft = cleaned[match.end() :].strip() or None
    return {"airline": airline, "flight_no": flight_no, "aircraft": aircraft}


def details_match_airlines(details: list[dict[str, Any]], preferred_airlines: list[str] | None) -> bool:
    filters = normalize_airline_filters(preferred_airlines)
    if not filters:
        return True
    for detail in details:
        tokens = airline_detail_tokens(detail)
        if tokens & filters:
            return True
    return False


def normalize_airline_filters(values: list[str] | None) -> set[str]:
    filters: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        group_codes = AIRLINE_GROUPS.get(text.upper()) or AIRLINE_GROUPS.get(text)
        if group_codes:
            for code in group_codes:
                filters.update(airline_alias_tokens(code))
            continue
        filters.update(airline_alias_tokens(text))
    return filters


def airline_detail_tokens(detail: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for value in [detail.get("airline"), detail.get("flight_no")]:
        tokens.update(airline_alias_tokens(str(value or "")))
    flight_no = str(detail.get("flight_no") or "").strip().upper()
    prefix_match = re.match(r"([A-Z0-9]{2})", flight_no)
    if prefix_match:
        tokens.update(airline_alias_tokens(prefix_match.group(1)))
    return tokens


def airline_alias_tokens(value: str) -> set[str]:
    normalized = " ".join(str(value or "").upper().split())
    if not normalized:
        return set()
    tokens = {normalized}
    prefix_match = re.match(r"([A-Z0-9]{2})\s*\d{3,4}", normalized)
    if prefix_match:
        tokens.add(prefix_match.group(1))
    for code, aliases in AIRLINE_ALIASES.items():
        alias_tokens = {str(alias).upper() for alias in aliases}
        if normalized == code or normalized in alias_tokens or any(alias in normalized for alias in alias_tokens if len(alias) > 1):
            tokens.add(code)
            tokens.update(alias_tokens)
    return tokens


TRANSIT_CARD_SCRIPT = """
(card) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const textOf = (selector) => clean(card.querySelector(selector)?.innerText || "");
  const joinText = (selector) => Array.from(card.querySelectorAll(selector)).map((node) => clean(node.innerText)).filter(Boolean).join(" ");
  const airlineItems = Array.from(card.querySelectorAll(".airline-item")).map((item) => {
    const airline = clean(item.querySelector(".airline-name")?.innerText || item.querySelector("img")?.getAttribute("alt") || "");
    const planeText = clean(item.querySelector(".plane-No")?.innerText || "");
    const match = planeText.match(/[A-Z0-9]{2}\\s*\\d{3,4}/);
    return {
      airline,
      plane_text: planeText,
      flight_no: match ? match[0].replace(/\\s+/g, "") : "",
    };
  });
  const transferSpans = Array.from(card.querySelectorAll(".transfer-info span")).map((node) => clean(node.innerText)).filter(Boolean);
  const priceText = textOf(".price-detail .price") || textOf(".price-detail");
  const priceMatch = priceText.replace(/,/g, "").match(/[¥￥]?\\s*([1-9]\\d{2,5})/);
  const classNames = Array.from(card.querySelectorAll(".rate-detail .className, .rate-detail span"))
    .map((node) => clean(node.innerText))
    .filter(Boolean);
          return {
            card_index: Number(card.getAttribute("data-tail-card-index") || -1),
            text: clean(card.innerText),
    airline_items: airlineItems,
    departure_time: textOf(".depart-box .time"),
    departure_airport: joinText(".depart-box .airport span") || textOf(".depart-box .airport"),
    arrival_time: textOf(".arrive-box .time"),
    arrival_airport: joinText(".arrive-box .airport span") || textOf(".arrive-box .airport"),
    transfer_duration: textOf(".transfer-duration"),
    transfer_text: textOf(".transfer-info"),
    transfer_name: transferSpans[0] || "",
    transfer_layover: transferSpans[1] || "",
    price: priceMatch ? Number(priceMatch[1]) : null,
    cabin_texts: classNames,
  };
}
"""
