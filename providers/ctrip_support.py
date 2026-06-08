from config_manager import RouteQuery


CTRIP_CITY_CODE_MAP = {
    "SHA": "SHA",
    "PVG": "SHA",
    "PEK": "BJS",
    "PKX": "BJS",
    "NAY": "BJS",
    "TFU": "CTU",
    "HND": "TYO",
    "NRT": "TYO",
    "ICN": "SEL",
    "GMP": "SEL",
}

CTRIP_DOMESTIC_CITY_CODES = {
    "BJS",
    "PEK",
    "PKX",
    "NAY",
    "SHA",
    "PVG",
    "CTU",
    "TFU",
    "CAN",
    "SZX",
    "KMG",
    "SYX",
    "CKG",
    "HGH",
    "SIA",
    "XIY",
    "URC",
    "HAK",
    "NKG",
    "CSX",
    "WUH",
    "XMN",
    "HRB",
    "SHE",
    "TAO",
    "CGO",
    "TSN",
    "DLC",
    "FOC",
    "TYN",
    "NNG",
    "KWE",
    "LHW",
    "KHN",
    "WNZ",
    "YNT",
    "WUX",
    "NGB",
    "HFE",
    "JJN",
    "ZUH",
    "LXA",
    "XNN",
    "INC",
    "YIH",
    "KOW",
    "CZX",
    "NTG",
    "YCU",
    "HET",
    "HLD",
    "MDG",
    "KRL",
    "KRY",
    "JHG",
    "LJG",
    "DYG",
    "ZHA",
    "SWA",
}

CTRIP_SPECIAL_REGION_CODES = {
    "HKG",
    "MFM",
    "TPE",
    "TSA",
    "KHH",
    "RMQ",
    "TNN",
    "KHN",
}

ECONOMY_KEYWORDS = ["经济舱", "超级经济舱", "超级经济", "premium economy", "economy"]
BUSINESS_FIRST_KEYWORDS = ["商务舱", "公务舱", "头等舱", "business class", "business", "first class", "first"]

CABIN_KEYWORD_GROUPS = {
    "economy_plus": {
        "positive": ECONOMY_KEYWORDS,
        "negative": BUSINESS_FIRST_KEYWORDS,
    },
    "business_first": {
        "positive": BUSINESS_FIRST_KEYWORDS,
        "negative": ECONOMY_KEYWORDS,
    },
    "economy": {
        "positive": ["经济舱", "economy"],
        "negative": ["超级经济舱", "超级经济", "商务舱", "公务舱", "头等舱", "business", "first"],
    },
    "premium_economy": {
        "positive": ["超级经济舱", "超级经济", "premium economy"],
        "negative": ["商务舱", "公务舱", "头等舱", "business", "first"],
    },
    "business": {
        "positive": ["商务舱", "公务舱", "business"],
        "negative": ["头等舱", "first"],
    },
    "first": {
        "positive": ["头等舱", "first"],
        "negative": [],
    },
}


def ctrip_city_code(airport_code: str) -> str:
    return CTRIP_CITY_CODE_MAP.get(airport_code.upper(), airport_code.upper())


def is_ctrip_domestic_code(airport_code: str) -> bool:
    normalized = ctrip_city_code(airport_code)
    return normalized in CTRIP_DOMESTIC_CITY_CODES


def is_ctrip_special_region_code(airport_code: str) -> bool:
    return airport_code.upper() in CTRIP_SPECIAL_REGION_CODES


def is_ctrip_international_or_special_region(airport_code: str) -> bool:
    return is_ctrip_special_region_code(airport_code) or not is_ctrip_domestic_code(airport_code)


def route_prefers_international_entry(route: RouteQuery) -> bool:
    airports = [route.origin, route.destination]
    airports.extend(segment.origin for segment in route.segments or [])
    airports.extend(segment.destination for segment in route.segments or [])
    relevant_airports = [code for code in airports if code]
    if not relevant_airports:
        return False
    return all(is_ctrip_international_or_special_region(code) for code in relevant_airports)


def needs_ctrip_native_single_search(route: RouteQuery) -> bool:
    codes = {
        route.origin.upper(),
        route.destination.upper(),
        CTRIP_CITY_CODE_MAP.get(route.origin.upper(), route.origin.upper()),
        CTRIP_CITY_CODE_MAP.get(route.destination.upper(), route.destination.upper()),
    }
    return not codes.issubset(CTRIP_DOMESTIC_CITY_CODES)


def provider_cabin_value(provider_name: str, cabin: str) -> str:
    normalized = (cabin or "").strip().lower()
    if provider_name == "ctrip":
        if normalized == "economy_plus":
            return "economy"
        if normalized == "business_first":
            return "business"
    return normalized or "economy"


def offer_matches_cabin(row_text: str | None, cabin: str) -> bool:
    cabin_key = (cabin or "").strip().lower()
    if not cabin_key:
        return True
    group = CABIN_KEYWORD_GROUPS.get(cabin_key)
    if not group:
        return True
    if not row_text:
        return True

    normalized = row_text.lower()
    positive = [token.lower() for token in group["positive"]]
    negative = [token.lower() for token in group["negative"]]
    has_positive = any(token in normalized for token in positive)
    has_negative = any(token in normalized for token in negative)
    if has_positive:
        return True
    if has_negative:
        return False
    return True
