"""Offline UI stress checks using system Chrome and realistic local data volumes."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient
from playwright.async_api import async_playwright
from web_ui import create_app
from test_tail_improvements import known_terms, quote
from test_web_ui_validation import write_config


DESTINATIONS = ["CAN", "SZX", "KMG", "XMN", "CTU", "CKG", "WUH", "XIY"]


def dashboard_payload():
    tasks = []
    routes = []
    for index in range(100):
        multi = index % 10 == 0
        provider = "ctrip" if index % 3 else "feizhu"
        origin = "BJS" if index % 2 else "PEK"
        destination = DESTINATIONS[index % len(DESTINATIONS)]
        key = f"route-{index}"
        group = f"group-{index // 5}" if index >= 80 else None
        segments = [
            {"origin": origin, "destination": "SHA", "departure_date": "2027-06-20"},
            {"origin": "SHA", "destination": destination, "departure_date": "2027-06-21"},
        ] if multi else None
        route = {
            "route_key": key, "origin": origin, "destination": destination,
            "departure_date": "2027-06-20", "return_date": None,
            "route_type": "multi_city" if multi else "one_way", "cabin": "economy",
            "transfer_policy": "any", "passengers": 1, "providers": [provider],
            "preferred_airlines": ["CA"] if index % 2 else [],
            "segments": segments, "auto_query_enabled": index % 4 == 0,
            "auto_query_interval_hours": 12, "auto_query_next_run_at": "2027-06-01T01:00:00Z",
            "group_id": group, "group_label": f"长线组合 {index}" if group else None,
        }
        routes.append(route)
        tasks.append({
            **route, "provider": provider, "route_display": f"{origin}->{destination}",
            "multi_city_segments": segments, "segment_count": 2 if multi else 1,
            "itinerary_summary": "北京 -> 上海 -> 广州" if multi else "",
            "status": "error" if index % 11 == 0 else "expired" if index % 13 == 0 else "ok",
            "latest_price": 500 + index if index % 7 else None, "currency": "CNY",
            "last_query_time": "2027-05-01T01:00:00Z", "last_error": "模拟平台返回了一段需要展示的较长错误信息" if index % 11 == 0 else "",
            "is_expired": index % 13 == 0, "lowest_flight_no": "CA1001" if index % 7 else "",
            "lowest_departure_time": "09:00" if index % 7 else "",
            "lowest_flight_details": segments or [],
        })
    return {
        "tasks": tasks, "routes": routes, "provider_options": ["ctrip", "feizhu"],
        "cabin_options": ["economy", "business"], "last_run": {"running": False, "saved": 0, "errors": []},
        "run_feedback": {"progress": {"running": False}, "changes": {}}, "session_hints": [],
    }


def stress_quotes():
    rows = []
    now = datetime.now(timezone.utc)
    for index in range(240):
        provider = "feizhu" if index % 3 == 0 else "ctrip"
        destination = DESTINATIONS[index % len(DESTINATIONS)]
        flight = f"CA{1000 + index}"
        item = quote(destination, price=500 + index, flight=flight, provider=provider)
        item.update({
            "id": index + 1, "itinerary_key": f"itinerary-{index}", "first_leg_key": flight,
            "expires_at": (now + timedelta(minutes=20)).isoformat() if index % 9 else "2020-01-01T00:00:00Z",
            "validation": {"status": "valid" if index % 7 else "needs_review"},
            "quote_terms": known_terms(), "baselines": {}, "savings_amount": None,
            "detail_source": "network_exact", "detail_quality": "complete",
        })
        if index == 0:
            item["flight_details"][1]["airline"] = "一段很长的航司与共享航班说明信息" * 4
            item["search_metadata"] = {"system_message": "模拟平台提示：结果需要人工复核。" * 3}
        rows.append(item)
    return rows


def stress_jobs():
    return [{
        "job_id": f"stress-{index}", "status": "finished" if index else "paused",
        "created_at": f"2027-05-{index + 1:02d}T01:00:00Z", "updated_at": f"2027-05-{index + 1:02d}T01:00:00Z",
        "payload": {"origin": "BJS", "transfer": "SHA", "departure_date": "2027-06-20", "provider": "both"},
        "progress": {"message": "本地压力测试任务", "saved": 200},
        "result": None, "error": None,
    } for index in range(20)]


async def run(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "config.json"
        write_config(config)
        client = TestClient(create_app(str(config)))
        dashboard = dashboard_payload()
        quotes = stress_quotes()
        jobs = stress_jobs()
        posts = []
        slow_quotes = {"enabled": False}
        metrics = {"tasks": 100, "stored_quotes": 240, "jobs": 20, "viewports": []}

        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})

            async def route_request(route):
                url = urlsplit(route.request.url)
                assert url.hostname == "127.0.0.1", f"Unexpected external request: {url.hostname}"
                if route.request.method == "POST":
                    posts.append((url.path, route.request.post_data))
                    if url.path == "/api/tail-discovery/run":
                        return await route.fulfill(status=200, json={
                            "job_id": "stress-submit", "status": "running", "payload": json.loads(route.request.post_data or "{}"),
                            "progress": {"message": "本地模拟运行中"},
                        })
                if url.path == "/api/dashboard":
                    return await route.fulfill(status=200, json=dashboard)
                if url.path == "/api/tail-discovery/jobs/latest":
                    return await route.fulfill(status=200, json={"job": jobs[0]})
                if url.path == "/api/tail-discovery/jobs" and not parse_qs(url.query):
                    return await route.fulfill(status=200, json={"jobs": jobs})
                if url.path == "/api/tail-discovery/jobs" and parse_qs(url.query).get("limit"):
                    return await route.fulfill(status=200, json={"jobs": jobs})
                if url.path.startswith("/api/tail-discovery/jobs/"):
                    job_id = url.path.rsplit("/", 1)[-1]
                    job = next((item for item in jobs if item["job_id"] == job_id), jobs[0])
                    return await route.fulfill(status=200, json=job)
                if url.path == "/api/tail-discovery/results":
                    if slow_quotes["enabled"]:
                        await asyncio.sleep(0.05)
                    offset = int(parse_qs(url.query).get("offset", ["0"])[0])
                    limit = int(parse_qs(url.query).get("limit", ["200"])[0])
                    batch = quotes[offset:offset + limit]
                    next_offset = offset + len(batch)
                    return await route.fulfill(status=200, json={
                        "results": batch, "has_more": next_offset < len(quotes), "next_offset": next_offset,
                    })
                response = client.get(url.path + ("?" + url.query if url.query else ""))
                await route.fulfill(
                    status=response.status_code,
                    content_type=response.headers.get("content-type", "text/plain"),
                    body=response.content,
                )

            await context.route("**/*", route_request)
            page = await context.new_page()
            errors = []
            console_errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

            started = asyncio.get_running_loop().time()
            await page.goto("http://127.0.0.1:8765/")
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 200")
            metrics["initial_load_seconds"] = round(asyncio.get_running_loop().time() - started, 3)
            metrics["initial_dom_nodes"] = await page.evaluate("document.querySelectorAll('*').length")
            metrics["dashboard_render_ms"] = await page.evaluate("""data => {
                const start = performance.now();
                renderDashboard(data);
                return Math.round((performance.now() - start) * 10) / 10;
            }""", dashboard)
            duplicate_ids = await page.evaluate("""() => {
                const counts = {};
                document.querySelectorAll('[id]').forEach(el => counts[el.id] = (counts[el.id] || 0) + 1);
                return Object.entries(counts).filter(([, count]) => count > 1);
            }""")
            assert not duplicate_ids, duplicate_ids
            unnamed = await page.evaluate("""() => [...document.querySelectorAll('button, a[href], input, select, summary')]
                .filter(el => el.getClientRects().length > 0)
                .filter(el => !(el.getAttribute('aria-label') || el.getAttribute('title') ||
                    (el.labels && el.labels.length) || el.innerText.trim() || el.value))
                .map(el => el.outerHTML)""")
            assert not unnamed, unnamed
            await page.screenshot(path=str(output / "stress-routes-desktop.png"))
            assert await page.locator(".card-actions button").first.evaluate("el => getComputedStyle(el).whiteSpace === 'nowrap'")
            assert await page.locator(".card-actions button").first.evaluate("el => el.getBoundingClientRect().height < 60")

            await page.evaluate("setWorkspaceTab('tasks', {persist:false})")
            assert await page.locator("#taskTableBody tr").count() >= 80
            await page.locator("#routeSearchInput").fill("NO-SUCH-ROUTE")
            assert "暂无任务" in await page.locator("#taskTableBody").inner_text()
            await page.locator("#routeSearchInput").fill("")
            assert await page.locator("#taskTableBody tr").count() >= 80
            await page.locator("#providerFilter").select_option("feizhu")
            assert await page.locator("#taskTableBody tr").count() > 10
            await page.locator("#providerFilter").select_option("")
            await page.locator("[data-task-view='multi_city']").click()
            assert await page.locator("#taskTableBody tr").count() > 0
            assert await page.locator("[data-task-view='multi_city']").get_attribute("aria-pressed") == "true"
            await page.locator("[data-task-view='all']").click()
            assert await page.locator("[data-task-view='all']").get_attribute("aria-pressed") == "true"
            trigger = page.locator("button[data-task-action='show-detail']").first
            await trigger.focus()
            await page.keyboard.press("Enter")
            assert await page.locator("#taskDetailDrawer").is_visible()
            assert await page.locator(".drawer-panel").get_attribute("role") == "dialog"
            assert await page.locator(".drawer-panel").get_attribute("aria-modal") == "true"
            assert await page.locator("button[data-drawer-close]").evaluate("el => el === document.activeElement")
            await page.keyboard.press("Shift+Tab")
            assert await page.locator("#taskDetailDrawer").evaluate("el => el.contains(document.activeElement)")
            await page.keyboard.press("Tab")
            assert await page.locator("#taskDetailDrawer").evaluate("el => el.contains(document.activeElement)")
            await page.keyboard.press("Escape")
            assert await page.locator("#taskDetailDrawer").is_hidden()
            assert await trigger.evaluate("el => el === document.activeElement")

            await page.evaluate("setWorkspaceTab('tail', {persist:false})")
            await page.locator("#tailLoadMoreBtn").click()
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 240")
            assert await page.locator("#tailLoadMoreBtn").is_hidden()
            await page.locator("#tailResultFilterSummary").click()
            await page.locator("#tailResultProviderFilter").select_option("feizhu")
            assert await page.locator(".tail-result-card").count() == 80
            await page.locator("#tailClearFiltersBtn").click()
            assert await page.locator(".tail-result-card").count() == 240
            for mode in ["time", "confidence", "savings", "price"] * 3:
                await page.locator("#tailSortSelect").select_option(mode)
            slow_quotes["enabled"] = True
            for view in ["history", "current"] * 5:
                await page.locator(f"[data-tail-view='{view}']").click()
            await page.wait_for_function("tailState.results.view === 'current' && tailState.results.items.length === 200")
            slow_quotes["enabled"] = False
            metrics["slow_response_view_switch"] = True
            await page.locator(".tail-details > summary").first.click()
            assert await page.locator(".tail-details").first.evaluate("el => el.open")
            await page.evaluate("fetchTailResults({background:true})")
            assert await page.locator(".tail-details").first.evaluate("el => el.open")

            for index in range(20):
                await page.evaluate("setWorkspaceTab('tasks', {persist:false}); setWorkspaceTab('tail', {persist:false})")
            assert await page.locator("body").get_attribute("data-workspace") == "tail"
            assert not posts, posts

            for width in [1280, 768, 390, 320]:
                await page.set_viewport_size({"width": width, "height": 900})
                await page.evaluate("document.activeElement?.blur(); window.scrollTo(0,0)")
                await page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                overflow = await page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                metrics["viewports"].append({"width": width, "overflow": overflow})
                assert not overflow, f"horizontal overflow at {width}"
                assert await page.locator("#tailRunBtn").is_visible()
                assert await page.locator("#tailRunBtn").evaluate("el => el.getBoundingClientRect().height >= 44")
                await page.screenshot(path=str(output / f"stress-tail-{width}.png"))

            await page.locator("#tailQueryEditor > summary").click()
            await page.locator("#tailOriginInput").fill("BJS")
            await page.locator("#tailTransferInput").fill("SHA")
            await page.locator("#tailDateInput").fill("2027-06-20")
            await page.locator("#tailProviderSelect").select_option("ctrip")
            await page.locator("#tailRunBtn").evaluate("el => { el.click(); el.click(); }")
            await page.wait_for_function("tailState.job.activeId === 'stress-submit'")
            assert len(posts) == 1, posts

            metrics["final_dom_nodes"] = await page.evaluate("document.querySelectorAll('*').length")
            metrics["post_count"] = len(posts)
            metrics["page_errors"] = errors
            metrics["console_errors"] = console_errors
            assert not errors, errors
            assert not console_errors, console_errors
            await browser.close()
        client.close()

    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, **metrics}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    asyncio.run(run(Path(args.output_dir).resolve()))
