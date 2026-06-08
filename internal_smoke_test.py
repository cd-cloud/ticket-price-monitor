"""Authorized internal smoke-test runner.

This runner is intentionally not a stealth or anti-detection tool. For sites
protected by Cloudflare, configure test-environment allow rules, Turnstile test
keys, or Cloudflare Access service tokens instead of hiding automation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright


VERIFICATION_KEYWORDS = (
    "cloudflare",
    "verify you are human",
    "checking your browser",
    "just a moment",
    "turnstile",
    "captcha",
    "人机验证",
    "验证您是真人",
)


def build_smoke_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authorized internal smoke-test runner")
    parser.add_argument("--url", required=True, help="Authorized internal/staging URL to test")
    parser.add_argument("--ready-selector", default="", help="Selector that proves the page is ready")
    parser.add_argument("--output-dir", default="./runtime/internal_smoke_tests")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--locale", default="zh-CN")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--viewport", default="1365x900", help="Viewport as WIDTHxHEIGHT")
    parser.add_argument(
        "--access-service-token-env",
        default="CF_ACCESS_CLIENT_ID,CF_ACCESS_CLIENT_SECRET",
        help="Env vars containing Cloudflare Access client id/secret; use empty string to disable",
    )
    return parser


async def run_internal_smoke_test(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    prefix = output_dir / f"smoke_{stamp}"
    viewport = parse_viewport(args.viewport)
    headers = cloudflare_access_headers(args.access_service_token_env)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=bool(args.headless))
        context = await browser.new_context(
            locale=args.locale,
            timezone_id=args.timezone,
            viewport=viewport,
            extra_http_headers=headers,
        )
        page = await context.new_page()
        result: dict[str, Any] = {
            "url": args.url,
            "ready_selector": args.ready_selector,
            "headless": bool(args.headless),
            "locale": args.locale,
            "timezone": args.timezone,
            "viewport": viewport,
            "used_cloudflare_access_headers": bool(headers),
            "started_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        }
        exit_code = 0
        try:
            response = await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            result["status"] = response.status if response else None
            if args.ready_selector:
                await page.locator(args.ready_selector).first.wait_for(timeout=args.timeout_ms)
                result["ready_selector_matched"] = True
            body_text = await safe_body_text(page)
            result["verification_detected"] = detect_verification(body_text, page.url)
            if result["verification_detected"]:
                result["recommendation"] = (
                    "Do not hide automation. Configure Cloudflare test keys, Access service tokens, "
                    "or a staging WAF skip/allow rule for this authorized test path."
                )
                exit_code = 2
            result["final_url"] = page.url
            result["title"] = await page.title()
            result["text_preview"] = " ".join(body_text.split())[:500]
        except Exception as exc:
            result["error"] = str(exc)
            exit_code = 1
        finally:
            try:
                await page.screenshot(path=str(prefix.with_suffix(".png")), full_page=True)
            except Exception:
                pass
            try:
                prefix.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            except Exception:
                pass
            result["finished_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            prefix.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await context.close()
            await browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


def parse_viewport(value: str) -> dict[str, int]:
    try:
        width_text, height_text = str(value or "").lower().split("x", 1)
        width = max(320, min(int(width_text), 3840))
        height = max(320, min(int(height_text), 2160))
        return {"width": width, "height": height}
    except Exception:
        return {"width": 1365, "height": 900}


def cloudflare_access_headers(env_spec: str) -> dict[str, str]:
    if not str(env_spec or "").strip():
        return {}
    parts = [part.strip() for part in env_spec.split(",") if part.strip()]
    if len(parts) != 2:
        return {}
    client_id = os.getenv(parts[0], "").strip()
    client_secret = os.getenv(parts[1], "").strip()
    if not client_id or not client_secret:
        return {}
    return {
        "CF-Access-Client-Id": client_id,
        "CF-Access-Client-Secret": client_secret,
    }


async def safe_body_text(page: Any) -> str:
    try:
        return await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def detect_verification(text: str, url: str = "") -> bool:
    haystack = f"{text or ''} {url or ''}".lower()
    return any(keyword.lower() in haystack for keyword in VERIFICATION_KEYWORDS)


def main() -> int:
    parser = build_smoke_parser()
    return asyncio.run(run_internal_smoke_test(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
