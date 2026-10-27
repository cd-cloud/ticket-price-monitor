"""Optional offline browser smoke: system Chrome, mocked API, no provider traffic.
Usage: python tests/browser_tail_smoke.py --output-dir /absolute/path
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright
from web_ui import create_app
from data_storage import PriceRepository
from tail_quotes import annotate_quote, apply_baselines
from tail_discovery_service import validate_tail_results_for_run
from test_tail_improvements import quote, known_terms
from test_web_ui_validation import write_config


async def run(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "config.json"
        write_config(config)
        client = TestClient(create_app(str(config)))
        repo = PriceRepository(root / "runtime" / "test.db")
        repo.initialize()
        for index, destination in enumerate(["CAN", "SZX", "KMG", "XMN"]):
            item = quote(destination, price=700 + 20 * index)
            item["raw_payload"]["quote_terms"] = known_terms()
            item, = validate_tail_results_for_run([item], origin="BJS", transfer="SHA", departure_date=item["departure_date"], preferred_airlines=[], cabin="economy")[0]
            annotate_quote(item)
            apply_baselines([item], {"price": 1000, "observed_at": item["observed_at"], "quote_terms": known_terms()})
            repo.insert_tail_discovery_result(item)
        old = quote("CAN", price=500, time="02:00", flight="CA1002")
        old["observed_at"] = "2020-01-01T00:00:00+00:00"
        old["raw_payload"]["recheck_outcome"] = {"status": "inconclusive"}
        annotate_quote(old)
        repo.insert_tail_discovery_result(old)
        repo.upsert_tail_discovery_job({"job_id": "offline-demo", "status": "paused", "payload": {},
            "progress": {"message": "已达到城市查询预算，可继续未完成项。", "attempted": 4, "matched": 4,
                "candidate_policy": {"ranking": "contextual_history_v1", "provider_calls": {"used": 4, "limit": 20},
                    "next_candidates": [{"destination": "CAN", "reasons": ["近期有条件相近的有效联程证据", "耗时采用历史估计"]}]}}})
        actions = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 1000})
            async def route_request(route):
                from urllib.parse import urlsplit
                url = urlsplit(route.request.url)
                # Every URL is intercepted, including any accidentally introduced third-party request.
                assert url.hostname == "127.0.0.1", f"Unexpected external request: {url.hostname}"
                if route.request.method == "POST":
                    actions.append((url.path, route.request.post_data))
                    return await route.fulfill(status=200, json={"job_id": "ui-action", "status": "finished"})
                if url.path == "/api/tail-discovery/jobs/ui-action":
                    return await route.fulfill(status=200, json={"job_id": "ui-action", "status": "finished"})
                response = client.get(url.path + ("?" + url.query if url.query else ""))
                await route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "text/plain"), body=response.content)
            await context.route("**/*", route_request)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto("http://127.0.0.1:8765/")
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length >= 4")
            await page.evaluate("setWorkspaceTab('tail', {persist:false})")
            await page.locator("details.tail-date-group").evaluate_all("els => els.forEach(el => el.open=true)")
            await page.locator(".tail-priority-details").evaluate_all("els => els.forEach(el => el.open=true)")
            await page.screenshot(path=str(output / "tail-desktop.png"), full_page=True)
            assert await page.locator(".tail-flight-group").count() == 2
            assert await page.locator("#tailPassengersInput").count() == 1
            assert await page.locator("#tailFilterScopeInput").input_value() == "first_leg"
            assert await page.locator("#tailBaselineBudgetInput").input_value() == "3"
            assert await page.get_by_text("最近复核无法确认原行程", exact=False).count() == 1
            await page.locator("button[data-tail-action='recheck']").first.click()
            await page.wait_for_timeout(100)
            assert any(path.endswith("/recheck") for path, _ in actions)
            await page.locator("button[data-tail-action='resume']").first.click()
            await page.wait_for_timeout(100)
            assert any(path.endswith("/restart") and json.loads(body)["mode"] == "resume" for path, body in actions)
            await page.locator("#tailProviderSelect").select_option("feizhu")
            await page.locator("#tailSearchScopeInput").select_option("open")
            assert await page.locator("#tailOpenScopeNotice").is_visible()
            assert await page.locator("#tailBudgetCitiesInput").is_disabled()
            assert await page.locator("#tailTargetInput").is_disabled()
            assert not await page.locator("#tailDiscoveryForm .tail-candidates").is_visible()
            await page.locator("#tailOriginInput").fill("BJS")
            await page.locator("#tailTransferInput").fill("CGO")
            await page.locator("#tailDateInput").fill("2027-06-20")
            await page.locator("#tailFilterScopeInput").select_option("all_legs")
            await page.locator("#tailBaselineBudgetInput").fill("2")
            await page.locator("#tailRunBtn").click()
            await page.wait_for_timeout(150)
            submitted = [json.loads(body) for path, body in actions if path == "/api/tail-discovery/run"]
            assert submitted and submitted[-1]["search_scope"] == "open"
            assert submitted[-1]["candidates"] == []
            assert submitted[-1]["max_candidates"] == 0
            assert submitted[-1]["target_results"] == 0
            assert submitted[-1]["filter_scope"] == "all_legs"
            assert submitted[-1]["max_baseline_queries"] == 2
            assert submitted[-1]["max_provider_calls"] == 20
            await page.locator("#tailSearchScopeInput").select_option("hybrid")
            assert await page.locator("#tailHybridScopeNotice").is_visible()
            assert await page.locator("#tailDiscoveryForm .tail-candidates").is_visible()
            await page.locator("#tailFollowupBudgetInput").fill("2")
            await page.locator("#tailCallBudgetInput").fill("4")
            await page.locator("#tailRunBtn").click()
            await page.wait_for_timeout(150)
            submitted = [json.loads(body) for path, body in actions if path == "/api/tail-discovery/run"]
            assert submitted[-1]["search_scope"] == "hybrid"
            assert submitted[-1]["max_followup_candidates"] == 2
            assert submitted[-1]["max_provider_calls"] == 4
            assert submitted[-1]["max_candidates"] == 0
            await page.screenshot(path=str(output / "tail-open-desktop.png"), full_page=True)
            for width in (390, 320):
                await page.set_viewport_size({"width":width,"height":844})
                await page.screenshot(path=str(output / f"tail-mobile-{width}.png"), full_page=True)
                overflow = await page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                assert not overflow, f"horizontal overflow at {width}px"
            await page.locator("#tailProviderSelect").select_option("ctrip")
            assert await page.locator("#tailSearchScopeInput").input_value() == "candidates"
            assert await page.locator("#tailSearchScopeInput").is_disabled()
            assert await page.locator("#tailDiscoveryForm .tail-candidates").is_visible()
            assert not errors, errors
            await browser.close()
        client.close()
    print(json.dumps({"ok": True, "viewports": [1280,390,320], "actions": len(actions)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    asyncio.run(run(Path(args.output_dir).resolve()))
