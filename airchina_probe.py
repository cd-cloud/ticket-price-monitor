"""Explicit, local-only P0 probe; no account login, booking, or quote writes."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config_manager import RouteQuery
from provider_runtime import ProviderRuntime
from providers import airchina


async def inspect_airchina_page(
    automation, route: RouteQuery, *, manual_wait_seconds: int = 0,
    capture_visible_page: bool = False, flight_no: str | None = None,
) -> Path:
    """One navigation, one browser context, no query/backend retry.

    An optional explicit wait lets the user handle the site themselves. We do
    not click a verification challenge, acknowledge travel warnings, or submit
    a booking. Captures, when requested, stay under ignored runtime/.
    """
    target = airchina.validate_inspection(
        route, flight_no=flight_no, manual_wait_seconds=manual_wait_seconds,
    )  # Fail before creating a browser page.
    provider = automation.app_config.providers.get("airchina")
    if provider is None:
        raise ValueError("配置中缺少 airchina 占位项；请参考 config.example.json。")
    # enabled=False remains intentional: explicit diagnostic use is not query
    # admission. Never modify the user's provider configuration.
    page, context = await automation._new_page(provider)
    report = {
        "provider": "airchina", "stage": "P0_probe",
        "requested": {
            "origin": route.origin.upper(), "destination": route.destination.upper(),
            "departure_date": route.departure_date, "passengers": route.passengers,
            "cabin": route.cabin, "transfer_policy": route.transfer_policy,
            "preferred_airlines": route.preferred_airlines or [],
            "flight_no": flight_no,
        },
        "requested_url": target,
        "parser_ready": False, "quote_saved": False,
        "final_quote_supported": False, "coverage_complete": False,
        "filters_applied": False,
        "limitations": ["诊断入口不应用舱位、航司或中转筛选，不产生报价。"],
    }
    try:
        try:
            report.update(await airchina.inspect(
                ProviderRuntime(automation), provider, route, page,
                flight_no=flight_no, manual_wait_seconds=manual_wait_seconds,
            ))
        except Exception as exc:
            # Exception messages may include tokenized URLs. Persist only class.
            report.update(state="probe_error", error_type=type(exc).__name__,
                          message="读取官网页面失败；没有自动重试，也没有保存报价。")

        folder = automation.app_config.runtime_dir / "airchina_probes" / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        )
        folder.mkdir(parents=True, exist_ok=False)
        if capture_visible_page:
            # Explicit opt-in: visible text/screenshot may contain account info.
            # Do not store full HTML, scripts, cookies, headers, or network bodies.
            try:
                text_path = folder / "visible-page.txt"
                text_path.write_text(await page.locator("body").inner_text(timeout=5000), encoding="utf-8")
                report["visible_text_path"] = str(text_path)
                screenshot_path = folder / "page.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                report["screenshot_path"] = str(screenshot_path)
            except Exception as exc:
                report["capture_error_type"] = type(exc).__name__
        report["captured_at"] = datetime.now(timezone.utc).isoformat()
        path = folder / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    finally:
        await automation._close_context_quietly(context)


async def run_airchina_probe(config_manager, args) -> int:
    from browser_automation import BrowserAutomation

    route = RouteQuery(origin=args.origin, destination=args.destination,
                       departure_date=args.date, passengers=1, cabin=args.cabin,
                       providers=["airchina"])
    airchina.validate_inspection(
        route, flight_no=args.flight_no, manual_wait_seconds=args.manual_wait_seconds,
    )
    config = replace(config_manager.load(), headless=False)
    # Do not load/decrypt any stored credentials or consume the Fliggy API.
    async with BrowserAutomation(config, {}, backend_override=args.backend) as automation:
        path = await inspect_airchina_page(
            automation, route, manual_wait_seconds=args.manual_wait_seconds,
            capture_visible_page=args.capture_visible_page,
            flight_no=args.flight_no,
        )
    report = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({"state": report["state"], "report_path": str(path),
                      "quote_saved": False, "message": report["message"]}, ensure_ascii=False))
    # A successful capture is not a successful query.
    return 2
