"""Air China P0 reconnaissance, NOT a fare parser.

The one-way URL was observed through the public booking form on 2026-10-30.
After manual verification, visible flight/cabin starting fares were observed.
Expanded base-fare products were also observed, but no tax-inclusive final total
has been verified; calendar prices never become quotes.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit

from config_manager import RouteQuery


HOME_URL = "https://www.airchina.com.cn/zh-CN"
QUERY_NOT_READY = (
    "国航最终报价适配尚未完成税费及完整行程验证，暂不支持自动保存价格；"
    "请先使用 airchina-probe 诊断。不会使用日历价或通用价格提取兜底。"
)
# Do not silently turn a multi-airport city request into one arbitrary airport.
CITY_ONLY_CODES = {"BJS", "TYO", "OSA", "SEL", "LON", "PAR", "NYC", "CHI",
                   "WAS", "YTO", "YMQ", "ROM", "MIL", "MOW", "SPK", "JKT"}


def build_search_url(route: RouteQuery, *, today: date | None = None) -> str:
    if route.route_type != "one_way" or route.return_date or route.segments:
        raise ValueError("国航诊断第一步仅支持单程，不会把往返或多程降级成单程。")
    if route.passengers != 1:
        raise ValueError("国航当前诊断仅验证 1 成人入口，不会默默忽略人数。")
    origin, destination = route.origin.upper(), route.destination.upper()
    for code in (origin, destination):
        if not re.fullmatch(r"[A-Z]{3}", code) or code in CITY_ONLY_CODES:
            raise ValueError("请提供明确机场三字码，例如 PEK，而不是城市聚合码 BJS。")
    if origin == destination:
        raise ValueError("出发机场和到达机场必须不同。")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", route.departure_date):
        raise ValueError("日期必须为 YYYY-MM-DD。")
    departure = date.fromisoformat(route.departure_date)
    if departure < (today or date.today()):
        raise ValueError("不能诊断过去日期的航班。")
    return f"https://www.airchina.com.cn/flight/oneway/{origin.lower()}-{destination.lower()}/{departure.isoformat()}"


def classify_page(url: str, text: str, expected_url: str, *, http_status: int | None = None) -> dict:
    """Only classify access/evidence. Even a matching page is NOT a valid quote.

    Never copy arbitrary page text, query strings, or fragments into the report:
    redirects and login screens can contain credentials or account information.
    """
    parsed, expected = urlsplit(url), urlsplit(expected_url)
    official = parsed.scheme == "https" and parsed.netloc == "www.airchina.com.cn"
    route_matches = official and parsed.path == expected.path
    if http_status in {401, 403, 429, 432}:
        state = "blocked"
    elif not official:
        state = "unexpected_page"
    elif any(marker in text for marker in ("安全验证", "请点击上图中的", "请完成验证", "滑动滑块")):
        state = "needs_verification"
    elif any(marker in text for marker in ("访问过于频繁", "访问异常", "访问被拒绝")):
        state = "blocked"
    elif http_status is not None and http_status >= 400:
        state = "http_error"
    elif not route_matches:
        state = "unexpected_page"
    elif "停留时间过长" in text and "价格" in text:
        state = "stale_results"
    elif "确认，查看下一个提示" in text:
        state = "needs_notice_confirmation"
    else:
        state = "query_page_unverified"
    return {
        "state": state,
        "official_host": official,
        "route_url_matches": route_matches,
        "selection_page_visible": "选择航班" in text,
        "http_status": http_status,
        "parser_ready": False,
        "quote_saved": False,
        "message": {
            "needs_verification": "国航要求人工验证；未尝试破解、切换后端或重发查询。",
            "blocked": "访问被限制；停止自动请求，请勿连续重试。",
            "unexpected_page": "未停留在预期官方查询页面，不能据此判断航班或价格。",
            "http_error": "官网返回 HTTP 错误，未提取价格。",
            "stale_results": "官网提示结果已过期；不读取旧价格，不自动确认或重新查询。",
            "needs_notice_confirmation": "乘机提示需要用户自行确认；程序不会自动确认或读取遮挡后的价格。",
            "query_page_unverified": "已读取页面，但航班、舱位及价格绑定仍待验证；不保存报价。",
        }[state],
    }


def snapshot_stop_state(snapshot: dict, expected_url: str) -> str | None:
    """Terminal DOM states shared by list waiting and product expansion."""
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "airchina_visible_oneway_v1":
        return "invalid_snapshot"
    try:
        actual, expected = urlsplit(snapshot.get("url", "")), urlsplit(expected_url)
    except (TypeError, ValueError):
        return "unexpected_page"
    if (actual.scheme != "https" or actual.netloc != "www.airchina.com.cn"
            or actual.path != expected.path):
        return "unexpected_page"
    for field, state in (
        ("blocked_visible", "blocked"),
        ("verification_visible", "needs_verification"),
        ("stale_visible", "stale_results"),
        ("notice_confirmation_visible", "needs_notice_confirmation"),
    ):
        if snapshot.get(field) is True:
            return state
    return None
