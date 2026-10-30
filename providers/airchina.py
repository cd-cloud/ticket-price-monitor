"""Air China orchestration following the Ctrip provider module boundaries.

Sessions and throttling belong to the shared runtime; selectors and validation
belong to Air China. Inspection is usable now. Production query remains gated.
No Ctrip selectors, login retries, profile rotation or generic price fallback.
"""
from __future__ import annotations

import re

from providers.airchina_page import QUERY_NOT_READY, build_search_url, classify_page
from providers.airchina_result import build_preview_result
from providers.airchina_result_wait import wait_for_results


name = "airchina"
supports_multi_city = False
supports_tail_discovery = False


def validate_inspection(route, *, flight_no=None, manual_wait_seconds=0):
    target = build_search_url(route)
    if flight_no:
        if not re.fullmatch(r"[A-Z]{2}\d{3,4}", flight_no):
            raise ValueError("请提供明确航班号，例如 CA1549。")
        if route.cabin not in {"economy", "business"}:
            raise ValueError("当前票价详情只支持 economy / business。")
    if not 0 <= manual_wait_seconds <= 300:
        raise ValueError("人工处理等待时间必须为 0 到 300 秒。")
    return target


async def query(runtime, provider, route, page, response_store):
    """Future FlightProvider integration seam; never return a fake total price."""
    raise RuntimeError(QUERY_NOT_READY)


async def inspect(runtime, provider, route, page, *, flight_no=None, manual_wait_seconds=0,
                  progress=None, manual_handler=None):
    """Navigate once, classify, wait for stable evidence, optionally expand ONE cabin.

    Caller owns context lifetime and local report persistence. Runtime is the
    same ProviderRuntime facade used by Ctrip, not BrowserAutomation internals.
    """
    target = validate_inspection(
        route, flight_no=flight_no, manual_wait_seconds=manual_wait_seconds,
    )
    if progress:
        progress("navigating")
    await runtime.throttle(provider)
    response = await page.goto(target, wait_until="domcontentloaded", timeout=provider.timeout_ms)
    status = getattr(response, "status", None)
    if status is None or status < 400:
        await page.wait_for_timeout(3000)
    text = await page.locator("body").inner_text(timeout=5000)
    report = classify_page(page.url, text, target, http_status=status)
    if manual_wait_seconds and report["state"] in {
        "needs_verification", "needs_notice_confirmation", "query_page_unverified",
    }:
        print(f"国航诊断：请自行查看浏览器；将在 {manual_wait_seconds} 秒后重新读取页面。"
              "程序不会操作验证、提示确认或预订按钮。")
        await page.wait_for_timeout(manual_wait_seconds * 1000)
        text = await page.locator("body").inner_text(timeout=5000)
        report = classify_page(page.url, text, target, http_status=status)
    # Only a user signal resumes the SAME page. No navigation/search retry.
    for manual_round in range(4):
        if report["state"] in {"needs_verification", "needs_notice_confirmation"}:
            if manual_handler is None or manual_round == 3:
                return report
            await manual_handler(report["state"])
            text = await page.locator("body").inner_text(timeout=5000)
            report = classify_page(page.url, text, target, http_status=status)
            if report["state"] in {"needs_verification", "needs_notice_confirmation"}:
                continue
        if report["state"] != "query_page_unverified":
            return report
        if progress:
            progress("reading_results")
        waited = await wait_for_results(
            page, route, expected_url=target, timeout_ms=min(provider.timeout_ms, 10000),
        )
        products = None
        if waited["state"] == "reference_fares_observed" and flight_no:
            from providers.airchina_products import expand_fare_products
            if progress:
                progress("reading_products")
            products = await expand_fare_products(
                page, route, flight_no=flight_no, cabin=route.cabin,
                timeout_ms=min(provider.timeout_ms, 10000),
            )
        report = {**report, **build_preview_result(waited, products)}
        if report["state"] not in {"needs_verification", "needs_notice_confirmation"}:
            return report
    return report
