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
        ids = []
        for index, destination in enumerate(["CAN", "SZX", "KMG", "XMN"]):
            item = quote(destination, price=700 + 20 * index, provider="ctrip")
            item["raw_payload"]["quote_terms"] = known_terms()
            item, = validate_tail_results_for_run([item], origin="BJS", transfer="SHA", departure_date=item["departure_date"], preferred_airlines=[], cabin="economy")[0]
            annotate_quote(item)
            apply_baselines([item], {"price": 1000, "observed_at": item["observed_at"], "quote_terms": known_terms()})
            ids.append(repo.insert_tail_discovery_result(item))
        old = quote("CAN", price=500, time="02:00", flight="CA1002", provider="ctrip")
        old["observed_at"] = "2020-01-01T00:00:00+00:00"
        old["raw_payload"]["recheck_outcome"] = {"status": "inconclusive"}
        annotate_quote(old)
        repo.insert_tail_discovery_result(old)
        repo.upsert_tail_discovery_job({"job_id": "offline-demo", "status": "paused", "payload": {"origin":"BJS", "transfer":"SHA", "departure_date":"2027-06-20", "provider":"ctrip"},
            "progress": {"message": "已达到城市查询预算，可继续未完成项。", "attempted": 4, "matched": 4, "saved":4,
                "candidate_policy": {"ranking": "contextual_history_v1", "provider_calls": {"used": 4, "limit": 20},
                    "next_candidates": [{"destination": "CAN", "reasons": ["近期有条件相近的有效联程证据", "耗时采用历史估计"]}]}}})
        with repo.connect() as conn:
            conn.execute("INSERT INTO tail_candidate_checkpoints (job_id,destination,fingerprint,status,attempt,result_ids,updated_at) VALUES (?,?,?,?,?,?,?)",
                         ("offline-demo", "CAN", "test", "matched", "{}", json.dumps(ids), repo.now_iso()))
        actions = []
        live = {"status":"running", "offline":False, "quote_error":False}
        live_reads = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 1000})
            async def route_request(route):
                from urllib.parse import urlsplit, parse_qs
                url = urlsplit(route.request.url)
                # Every URL is intercepted, including any accidentally introduced third-party request.
                assert url.hostname == "127.0.0.1", f"Unexpected external request: {url.hostname}"
                if route.request.method == "POST":
                    actions.append((url.path, route.request.post_data))
                    return await route.fulfill(status=200, json={"job_id": "ui-action", "status": "finished"})
                if url.path == "/api/tail-discovery/jobs/ui-action":
                    return await route.fulfill(status=200, json={"job_id": "ui-action", "status": "finished"})
                if url.path == "/api/tail-discovery/jobs/live":
                    live_reads.append(dict(live))
                    if live["offline"]:
                        return await route.fulfill(status=503, json={"detail":"offline test"})
                    return await route.fulfill(status=200, json={
                        "job_id":"live", "status":live["status"],
                        "payload":{"origin":"BJS","transfer":"SHA","departure_date":"2027-06-20"},
                        "progress":{"saved":4,"attempted":1,"total":5,"message":"模拟查询中"}})
                if url.path == "/api/tail-discovery/results":
                    job_id = parse_qs(url.query).get("job_id", [""])[0]
                    if job_id == "ui-action":
                        return await route.fulfill(status=200, json={"results":[], "has_more":False,"next_offset":0})
                    if job_id == "live":
                        if live["quote_error"]:
                            return await route.fulfill(status=503, json={"detail":"quote service offline"})
                        response = client.get("/api/tail-discovery/results?job_id=offline-demo")
                        return await route.fulfill(status=200, json=response.json())
                response = client.get(url.path + ("?" + url.query if url.query else ""))
                await route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "text/plain"), body=response.content)
            await context.route("**/*", route_request)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto("http://127.0.0.1:8765/")
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4")
            await page.evaluate("setWorkspaceTab('tail', {persist:false})")
            await page.wait_for_function("document.querySelector('#tailQueryEditor').open === false")
            assert not await page.locator("#tailResultFilters").evaluate("el => el.open")
            assert not await page.locator("#tailTaskContextDetails").evaluate("el => el.open")
            assert await page.locator("button[data-tail-action='explore']").is_hidden()
            assert "2027-06-20" in await page.locator("#tailResultIdentity").inner_text()
            assert "完整购票行程" in await page.locator(".tail-result-card").first.inner_text()
            assert "可比参考价差" in await page.locator(".tail-price").first.inner_text()
            await page.locator("#tailViewResultsBtn").click()
            assert await page.locator("#tailResultsHeading").evaluate("el => el === document.activeElement")
            assert await page.locator(".tail-flight-group").count() == 1
            assert not await page.locator("#tailSearchSettings").get_attribute("open")
            assert not await page.locator("#tailFilters").get_attribute("open")
            assert await page.locator("#tailCallBudgetInput").is_hidden()
            await page.screenshot(path=str(output / "tail-desktop.png"), full_page=True)
            await page.locator(".tail-alternatives > summary").first.click()
            await page.locator(".tail-details > summary").first.click()
            await page.evaluate("fetchTailResults()")
            assert await page.locator(".tail-alternatives").first.evaluate("el => el.open")
            assert await page.locator(".tail-details").first.evaluate("el => el.open")
            await page.locator("[data-tail-view='history']").click()
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 5")
            assert await page.locator(".tail-flight-group").count() == 2
            assert "跨任务" in await page.locator("#tailResultIdentity").inner_text()
            await page.locator("#tailResultFilters > summary").click()
            await page.locator("#tailTrustInput").select_option("stale")
            assert await page.locator(".tail-result-card").count() == 1
            await page.locator("#tailClearFiltersBtn").click()
            await page.locator("[data-tail-view='current']").click()
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4")
            await page.locator("#tailMaxPriceInput").fill("1")
            assert await page.get_by_text("已有报价，但被列表筛选隐藏了", exact=True).count() == 1
            await page.locator("#tailClearFiltersBtn").click()
            # Changes to route or airline must not silently overwrite custom cities.
            await page.locator("#tailQueryEditor > summary").click()
            await page.locator("#tailSearchSettings > summary").click()
            await page.locator(".tail-candidate-box > summary").click()
            await page.locator("[data-candidates='none']").click()
            await page.locator("#tailCandidateSearch").fill("CAN")
            await page.locator("[data-candidates='all']").click()
            assert await page.evaluate("selectedTailCandidates().join(',')") == "CAN"
            await page.locator("#tailOriginInput").fill("BJS")
            await page.locator("#tailTransferInput").fill("SHA")
            await page.locator("#tailDateInput").fill("2027-06-20")
            await page.locator("#tailDateInput").blur()
            assert await page.evaluate("selectedTailCandidates().join(',')") == "CAN"
            assert await page.locator("#tailDraftNotice").is_hidden()
            await page.locator("#tailDateInput").fill("2027-06-21")
            assert "条件已修改" in await page.locator("#tailDraftNotice").inner_text()
            assert "2027-06-20" in await page.locator("#tailResultIdentity").inner_text()
            assert "2027-06-21" not in await page.locator("#tailResultIdentity").inner_text()
            assert len(actions) == 0, "editing conditions must not start a query"
            await page.locator("#tailDateInput").fill("2027-06-20")
            assert await page.locator("#tailDraftNotice").is_hidden()
            await page.reload()
            await page.wait_for_function("document.querySelector('#tailRunBtn')?.disabled === false")
            await page.evaluate("setWorkspaceTab('tail', {persist:false})")
            assert await page.locator("#tailOriginInput").input_value() == "BJS"
            assert await page.evaluate("selectedTailCandidates().join(',')") == "CAN"
            await page.wait_for_function("document.querySelector('#tailQueryEditor').open === false")
            await page.locator("#tailQueryEditor > summary").click()
            await page.locator("#tailProviderSelect").select_option("feizhu")
            await page.locator("#tailSearchSettings > summary").click()
            await page.locator("#tailSearchScopeInput").select_option("open")
            assert await page.locator("#tailOpenScopeNotice").is_visible()
            assert await page.locator("#tailBudgetCitiesInput").is_disabled()
            assert not await page.locator("#tailDiscoveryForm .tail-candidates").is_visible()
            await page.locator("#tailFilters > summary").click()
            await page.locator("#tailFilterScopeInput").select_option("all_legs")
            await page.locator("#tailBaselineBudgetInput").fill("2")
            await page.locator("#tailRunBtn").click()
            await page.wait_for_function("document.querySelector('#tailRunBtn').disabled === false")
            submitted = [json.loads(body) for path, body in actions if path == "/api/tail-discovery/run"]
            assert submitted[-1]["search_scope"] == "open"
            assert submitted[-1]["candidates"] == []
            assert submitted[-1]["max_candidates"] == 0
            assert submitted[-1]["target_results"] == 0
            assert submitted[-1]["filter_scope"] == "all_legs"
            assert submitted[-1]["max_baseline_queries"] == 2
            assert submitted[-1]["max_provider_calls"] == 20
            await page.locator("#tailQueryEditor > summary").click()
            await page.locator("#tailSearchScopeInput").select_option("hybrid")
            assert await page.locator("#tailHybridScopeNotice").is_visible()
            await page.locator("#tailFollowupBudgetInput").fill("2")
            await page.locator("#tailCallBudgetInput").fill("4")
            await page.locator("#tailRunBtn").click()
            await page.wait_for_function("document.querySelector('#tailRunBtn').disabled === false")
            submitted = [json.loads(body) for path, body in actions if path == "/api/tail-discovery/run"]
            assert submitted[-1]["search_scope"] == "hybrid"
            assert submitted[-1]["max_followup_candidates"] == 2
            assert submitted[-1]["max_provider_calls"] == 4
            # Exercise real browser lifecycle without platform traffic.
            await page.evaluate("startTailJobPolling({job_id:'live',status:'running',payload:{origin:'BJS',transfer:'SHA'}})")
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4")
            assert await page.locator("#tailRunBtn").is_disabled()
            assert await page.locator("#tailQueryFields").evaluate("el => el.disabled")
            assert await page.locator("#tailOriginInput").is_disabled()
            live["offline"] = True
            await page.evaluate("pollTailDiscoveryJob('live',true)")
            await page.wait_for_function("document.querySelector('#tailConnectionStatus').textContent.includes('连接中断')")
            assert await page.evaluate("activeTailJobId") == "live"
            assert await page.locator("#tailRunBtn").is_disabled()
            assert await page.locator(".tail-result-card").count() == 4
            live.update(offline=False, quote_error=True)
            await page.evaluate("pollTailDiscoveryJob('live',true)")
            await page.wait_for_function("document.querySelector('#tailListError').hidden === false")
            assert await page.evaluate("activeTailJobId") == "live"
            assert await page.locator("#tailRunBtn").is_disabled()
            live.update(quote_error=False, status="finished")
            await page.evaluate("pollTailDiscoveryJob('live',true)")
            await page.wait_for_function("document.querySelector('#tailRunBtn').disabled === false && document.querySelector('#tailListError').hidden")
            assert await page.evaluate("activeTailJobId") == ""
            assert await page.locator("#tailConnectionStatus").is_hidden()
            # Restore a useful fixture view for visual checks, without provider calls.
            await page.locator("#tailTaskContextDetails > summary").click()
            await page.locator("#tailJobPicker").select_option("offline-demo")
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4")
            await page.evaluate("document.querySelector('#tailQueryEditor').open=true; document.querySelector('#tailSearchSettings').open=false; document.querySelector('#tailFilters').open=false")
            await page.locator("#tailProviderSelect").select_option("ctrip")
            assert await page.locator("#tailSearchScopeInput").input_value() == "candidates"
            await page.evaluate("""() => {
                document.querySelector('#tailQueryEditor').open=false;
                document.querySelector('#tailTaskContextDetails').open=false;
                document.querySelector('#tailResultFilters').open=false;
                document.querySelectorAll('#tailResults details').forEach(el => el.open=false);
            }""")
            for width in (1280, 390, 320):
                # A fresh page also verifies the persisted, result-first landing
                # state without depending on earlier disclosure interactions.
                preview = await context.new_page()
                preview.on("pageerror", lambda error: errors.append(str(error)))
                await preview.set_viewport_size({"width":width,"height":900})
                await preview.goto("http://127.0.0.1:8765/")
                await preview.evaluate("setWorkspaceTab('tail', {persist:false})")
                await preview.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4 && !document.querySelector('#tailQueryEditor').open")
                await preview.evaluate("document.fonts.ready")
                await preview.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                await preview.screenshot(path=str(output / f"tail-{width}.png"), full_page=True)
                assert not await preview.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), f"overflow {width}"
                await preview.locator("#tailResultFilterSummary").click()
                await preview.locator("#tailMaxPriceInput").fill("800")
                assert "1 项" in await preview.locator("#tailResultFilterSummary").inner_text()
                assert not await preview.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), f"filter overflow {width}"
                await preview.locator("#tailClearFiltersBtn").click()
                await preview.locator("#tailResultFilterSummary").click()
                await preview.locator("#tailTaskContextDetails > summary").click()
                assert await preview.locator("#tailJobPicker").is_visible()
                assert not await preview.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), f"context overflow {width}"
                await preview.locator("#tailTaskContextDetails > summary").click()
                await preview.locator("#tailViewResultsBtn").click()
                assert await preview.locator("#tailResultsHeading").evaluate("el => el === document.activeElement")
                # Read-only refresh must not jump back to the form or reopen settings.
                before = await preview.locator(".tail-flight-group").first.evaluate("el => el.getBoundingClientRect().top")
                await preview.evaluate("fetchTailResults({background:true})")
                after = await preview.locator(".tail-flight-group").first.evaluate("el => el.getBoundingClientRect().top")
                assert abs(before-after) < 3
                assert not await preview.locator("#tailQueryEditor").evaluate("el => el.open")
                await preview.close()
            # Keyboard users can reach settings and all visible controls retain names.
            assert await page.locator("#tailOriginInput").evaluate("el => el.labels.length") > 0
            assert await page.locator("#tailJobPicker").evaluate("el => el.labels.length") > 0
            # Hidden settings expand on native validity errors rather than
            # trapping focus in a closed details element.
            await page.set_viewport_size({"width":1280,"height":900})
            await page.locator("#tailQueryEditor > summary").click()
            await page.evaluate("document.querySelector('#tailPassengersInput').value='0'")
            await page.locator("#tailRunBtn").click()
            assert await page.locator("#tailFilters").evaluate("el => el.open")
            assert len(actions) == 2, "invalid input must not dispatch a query"
            await page.evaluate("document.querySelector('#tailPassengersInput').value='1'")
            await page.locator("button[data-tail-action='recheck']").first.click()
            await page.wait_for_function("document.querySelector('#tailRunBtn').disabled === false")
            assert any(path.endswith("/recheck") for path, _ in actions)
            await page.locator("#tailTaskContextDetails > summary").click()
            await page.locator("#tailJobPicker").select_option("offline-demo")
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4")
            await page.locator("button[data-tail-action='resume']").click()
            await page.wait_for_function("document.querySelector('#tailRunBtn').disabled === false")
            assert any(path.endswith("/restart") and json.loads(body)["mode"] == "resume" for path, body in actions)
            assert not errors, errors
            await browser.close()
        client.close()
    print(json.dumps({"ok": True, "viewports": [1280,390,320], "actions": len(actions)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    asyncio.run(run(Path(args.output_dir).resolve()))
