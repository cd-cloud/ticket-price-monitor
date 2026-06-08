from __future__ import annotations

from config_manager import RouteQuery


def looks_like_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = [
        "access denied",
        "forbidden",
        "429",
        "too many requests",
        "rate limit",
        "访问频繁",
        "请稍后重试",
        "您的访问过于频繁",
        "系统繁忙",
    ]
    return any(marker in text for marker in markers)


def looks_like_no_fare(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = [
        "returned no flights",
        "no reliable fare will be saved",
        "未找到符合条件的航班",
        "暂时无法查询到对应价格",
    ]
    return any(marker in text for marker in markers)


def looks_like_ctrip_profile_pollution(provider_name: str, backend_name: str | None, exc: Exception) -> bool:
    if provider_name != "ctrip" or str(backend_name or "").strip().lower() != "chrome":
        return False
    text = str(exc).lower()
    return "ctrip login overlay blocked reliable results" in text


def should_stop_after_no_fare(
    provider_name: str,
    route: RouteQuery,
    attempt_index: int,
    backend_count: int,
) -> bool:
    del route
    if attempt_index >= backend_count - 1:
        return True
    if provider_name == "ctrip":
        return False
    return True
