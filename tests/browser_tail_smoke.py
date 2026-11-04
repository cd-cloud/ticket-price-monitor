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
        dual = {"status":"running", "payload":None, "rows":None}
        def dual_job():
            return {"job_id":"dual-demo", "status":dual["status"], "payload":dual["payload"],
                "progress":{"saved":5, "message":"模拟双平台进度；已完成结果分别保留", "children":[
                    {"job_id":"dual-c", "provider":"ctrip", "status":"finished", "saved":4,
                     "payload":{**(dual["payload"] or {}),"provider":"ctrip","search_scope":"candidates"},
                     "progress":{"message":"已查询候选城市"}},
                    {"job_id":"dual-f", "provider":"feizhu", "status":"running" if dual["status"] == "running" else "failed", "saved":1,
                     "payload":{**(dual["payload"] or {}),"provider":"feizhu","search_scope":"open"},
                     "error":None if dual["status"] == "running" else "模拟平台暂时不可用，已保存结果保留",
                     "progress":{"message":"已返回开放发现报价"}},
                ]}}
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
                    submitted = json.loads(route.request.post_data or "{}")
                    if url.path == "/api/tail-discovery/run" and submitted.get("provider") == "both":
                        dual["payload"] = submitted
                        existing = client.get("/api/tail-discovery/results?job_id=offline-demo").json()["results"]
                        extra = {**existing[0], "id":999, "provider":"feizhu", "price":650,
                                 "quote_terms":{"price_basis":"per_person"}, "baselines":{}, "savings_amount":None}
                        dual["rows"] = [*existing, extra]
                        return await route.fulfill(status=200, json=dual_job())
                    return await route.fulfill(status=200, json={"job_id": "ui-action", "status": "finished"})
                if url.path == "/api/tail-discovery/jobs/dual-demo":
                    return await route.fulfill(status=200, json=dual_job())
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
                    if job_id == "dual-demo":
                        return await route.fulfill(status=200, json={"results":dual["rows"],"has_more":False,"next_offset":5})
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
            assert "已停止，可继续" in await page.locator("#tailTaskContextSummary").inner_text()
            assert "完整购票行程" in await page.locator(".tail-itinerary-option").first.inner_text()
            assert "同口径参考" in await page.locator(".tail-decision-difference").first.inner_text()
            assert "primary" in await page.locator("button[data-tail-action='resume']").get_attribute("class")
            assert await page.locator("button[data-tail-action='resume']").inner_text() == "继续此任务"
            assert "ghost" in await page.locator("#tailRunBtn").get_attribute("class")
            assert await page.locator("#tailRunBtn").inner_text() == "使用新条件查询"
            assert "上方待提交条件与此任务不同" in await page.locator("#tailDraftNotice").inner_text()
            assert await page.locator("#tailDiscoveryPanel").get_attribute("data-has-results") == "true"
            assert await page.locator("#tailDiscoveryPanel .panel-head .muted").is_hidden()
            assert await page.locator("#tailJobSummary").get_attribute("data-active") == "false"
            await page.evaluate("""() => {
                tailSelectedJob = {...tailSelectedJob, status:'finished'};
                renderTailJobProgress(tailSelectedJob);
            }""")
            assert "primary" in await page.locator("#tailViewResultsBtn").get_attribute("class")
            assert await page.locator("#tailViewResultsBtn").inner_text() == "查看方案 ↓"
            await page.evaluate("""() => {
                tailSelectedJob = {...tailSelectedJob, status:'paused'};
                renderTailJobProgress(tailSelectedJob);
            }""")
            assert "ghost" in await page.locator("#tailViewResultsBtn").get_attribute("class")
            assert "primary" in await page.locator("button[data-tail-action='resume']").get_attribute("class")
            await page.locator("#tailViewResultsBtn").click()
            assert await page.locator("#tailResultsHeading").evaluate("el => el === document.activeElement")
            assert await page.locator(".tail-flight-group").count() == 1
            assert await page.locator("#tailProviderSelect").input_value() == "both"
            assert not await page.locator("#tailSearchSettings").get_attribute("open")
            assert not await page.locator("#tailFilters").get_attribute("open")
            assert not await page.locator("#tailAdvancedBudgets").get_attribute("open")
            assert await page.locator("#tailProviderQuickFilter").is_hidden()
            assert await page.locator("#tailCallBudgetInput").evaluate("el => !el.disabled")
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
            await page.locator("#tailProviderSelect").select_option("ctrip")
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
            assert await page.locator("#tailRunBtn").inner_text() == "使用新条件查询"
            assert len(actions) == 0, "editing conditions must not start a query"
            await page.locator("#tailDateInput").fill("2027-06-20")
            assert await page.locator("#tailDraftNotice").is_hidden()
            assert await page.locator("#tailRunBtn").inner_text() == "使用表单条件重新查询"
            await page.reload()
            await page.wait_for_function("document.querySelector('#tailRunBtn')?.disabled === false")
            await page.evaluate("setWorkspaceTab('tail', {persist:false})")
            assert await page.locator("#tailOriginInput").input_value() == "BJS"
            assert await page.evaluate("selectedTailCandidates().join(',')") == "CAN"
            await page.wait_for_function("document.querySelector('#tailQueryEditor').open === false")
            await page.locator("#tailQueryEditor > summary").click()
            await page.locator("#tailProviderSelect").select_option("feizhu")
            await page.locator("#tailSearchSettings > summary").click()
            await page.locator("#tailAdvancedBudgets > summary").click()
            assert await page.locator("#tailCallBudgetInput").evaluate("el => el.closest('details').id") == "tailAdvancedBudgets"
            await page.locator("#tailSearchScopeInput").select_option("open")
            assert await page.locator("#tailOpenScopeNotice").is_visible()
            assert await page.locator("#tailBudgetCitiesInput").is_disabled()
            assert not await page.locator("#tailDiscoveryForm .tail-candidates").is_visible()
            await page.locator("#tailFilters > summary").click()
            await page.locator("#tailFilterScopeInput").select_option("all_legs")
            await page.locator("#tailBaselineBudgetInput").fill("2")
            await page.locator("#tailProviderSelect").select_option("both")
            await page.evaluate("document.querySelector('.tail-candidate-box').open=false")
            for width in (1280, 390, 320):
                await page.set_viewport_size({"width":width,"height":900})
                await page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                assert await page.locator(".tail-options-grid").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length === 1")
                assert await page.locator("#tailFilters > summary").evaluate("el => getComputedStyle(el, '::after').backgroundImage !== 'none'")
                assert await page.locator(".tail-settings-grid").first.evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length === (innerWidth >= 961 ? 3 : 1)")
                if width <= 640:
                    assert await page.locator(".tail-run-actions").evaluate("el => getComputedStyle(el).position === 'static'")
                    assert await page.locator("#tailFilters > summary").evaluate("el => getComputedStyle(el).flexWrap === 'wrap'")
                overflow = await page.evaluate("""() => ({
                    overflow: document.documentElement.scrollWidth > innerWidth + 1,
                    elements: [...document.querySelectorAll('#tailDiscoveryPanel *')].map(el => ({
                        tag: el.tagName, cls: String(el.className), right: el.getBoundingClientRect().right,
                        width: el.getBoundingClientRect().width,
                    })).filter(el => el.right > innerWidth + 1).slice(0, 12),
                })""")
                assert not overflow["overflow"], f"expanded settings overflow {width}: {overflow['elements']}"
                await page.screenshot(path=str(output / f"tail-settings-expanded-{width}.png"), full_page=True)
            assert len(actions) == 0, "expanding settings must not start a query"
            await page.set_viewport_size({"width":1280,"height":900})
            await page.locator("#tailProviderSelect").select_option("feizhu")
            await page.locator("#tailSearchScopeInput").select_option("open")
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
            await page.locator("[data-tail-view='history']").click()
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 5")
            assert "下方结果另有所属" in await page.locator("#tailJobSummary").inner_text()
            live["quote_error"] = False
            await page.locator("[data-tail-view='current']").click()
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4")
            assert "下方结果另有所属" not in await page.locator("#tailJobSummary").inner_text()
            await page.locator("#tailTaskContextDetails > summary").click()
            await page.locator("#tailJobPicker").select_option("offline-demo")
            await page.wait_for_function("tailSelectedJob?.job_id === 'offline-demo'")
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 4")
            job_context = await page.locator("#tailJobSummary").inner_text()
            assert "后台执行：" in job_context and "下方结果另有所属" in job_context, job_context
            assert "当前结果：北京 (BJS) → 上海 (SHA)" in await page.locator("#tailResultIdentity").inner_text()
            live["quote_error"] = False
            await page.evaluate("selectTailJob('live')")
            assert await page.evaluate("tailSelectedJobId") == "live"
            assert "下方结果另有所属" not in await page.locator("#tailJobSummary").inner_text()
            await page.locator("#tailJobPicker").select_option("offline-demo")
            await page.wait_for_function("tailSelectedJob?.job_id === 'offline-demo'")
            await page.locator("#tailTaskContextDetails > summary").click()
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
                if width <= 640:
                    navigation = preview.locator("#workspaceTabSelect")
                    assert await navigation.is_visible()
                    assert await navigation.locator("option").count() == 6
                    assert await navigation.input_value() == "tail"
                    assert "甩尾发现 · 4" in await navigation.locator("option:checked").inner_text()
                    assert not await preview.locator("[data-workspace-tab='experiments']").is_visible()
                    await navigation.select_option("experiments")
                    assert await preview.locator("body").get_attribute("data-workspace") == "experiments"
                    assert await preview.locator("#airchinaExperimentPanel").is_visible()
                    assert await navigation.input_value() == "experiments"
                    await navigation.select_option("tail")
                    assert await preview.locator("#tailDiscoveryPanel").is_visible()
                    assert await navigation.input_value() == "tail"
                    assert await preview.locator(".tail-result-summary").first.evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length === 1")
                    assert await preview.locator(".tail-result-decision").first.evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length === 1")
                    assert await preview.locator(".tail-result-card").first.evaluate("""el => {
                        const route = el.querySelector('.tail-result-main').getBoundingClientRect();
                        const price = el.querySelector('.tail-price').getBoundingClientRect();
                        return price.top >= route.bottom - 1;
                    }""")
                    await preview.locator("#tailQueryEditor > summary").click()
                    assert await preview.locator(".tail-basic-grid").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length === 1")
                    assert await preview.locator("#tailRunBtn").inner_text() == "使用表单条件重新查询"
                    assert "表单条件开始新查询" in await preview.locator("#tailRunHint").inner_text()
                    await preview.locator("#tailQueryEditor > summary").click()
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
            # One user action creates one dual-provider parent, not competing POSTs.
            await page.locator("#tailQueryEditor > summary").click()
            await page.locator("#tailProviderSelect").select_option("both")
            await page.locator("#tailSearchSettings > summary").click()
            assert await page.locator("#tailFeizhuScopeInput").is_visible()
            assert await page.locator("#tailFeizhuScopeInput").evaluate("el => el.closest('details').id") == "tailSearchSettings"
            assert await page.locator("#tailBudgetCitiesInput").is_enabled()
            assert await page.locator("#tailCallBudgetInput").is_enabled()
            await page.locator("#tailFeizhuScopeInput").select_option("hybrid")
            assert await page.locator("#tailFollowupBudgetInput").is_enabled()
            await page.locator("#tailFeizhuScopeInput").select_option("open")
            await page.locator("#tailRunBtn").click()
            await page.wait_for_function("document.querySelectorAll('.tail-result-card').length === 5 && document.querySelectorAll('.tail-provider-progress-card').length === 2")
            assert len(actions) == 5, "dual search must use exactly one POST"
            assert dual["payload"]["provider"] == "both"
            assert dual["payload"]["feizhu_search_scope"] == "open"
            assert dual["payload"]["candidates"] == ["CAN"]
            assert dual["payload"]["search_scope"] == "candidates"
            assert await page.locator("#tailRunBtn").is_disabled()
            assert await page.locator("#tailRunBtn").inner_text() == "查询进行中"
            assert await page.locator(".tail-flight-group").count() == 1, await page.locator(".tail-flight-group").evaluate_all("els => els.map(el => ({key:el.dataset.stableKey, text:el.innerText}))")
            assert await page.locator(".tail-result-card").count() == 5
            assert await page.locator(".tail-itinerary-option").count() == 4
            shared = page.locator(".tail-itinerary-option.has-multiple-providers")
            assert await shared.count() == 1
            assert "厦门 (XMN)" in await shared.locator(".tail-itinerary-heading").inner_text()
            assert "广州 (CAN)" not in await shared.locator(".tail-itinerary-heading").inner_text()
            assert "同行程平台独立报价" in await shared.locator(".tail-itinerary-heading").inner_text()
            assert await shared.locator(".tail-result-card").count() == 2
            assert await shared.locator(".tail-result-card[data-provider='ctrip']").count() == 1
            assert await shared.locator(".tail-result-card[data-provider='feizhu']").count() == 1
            single = page.locator(".tail-itinerary-option:not(.has-multiple-providers)")
            assert await single.count() == 3
            await page.locator(".tail-alternatives > summary").click()
            assert await single.first.locator(".tail-missing-provider").is_visible()
            missing_text = await single.first.locator(".tail-missing-provider").inner_text()
            assert missing_text == "飞猪暂无同行程报价", missing_text
            await page.locator(".tail-alternatives > summary").click()
            assert "按完整行程比较" in await page.locator(".tail-flight-best").inner_text()
            quick = page.locator("#tailProviderQuickFilter")
            assert await quick.is_visible()
            assert "全部 5" in await quick.inner_text()
            await quick.locator("[data-tail-provider='feizhu']").click()
            assert await page.locator(".tail-result-card").count() == 1
            assert await page.locator("#tailResultProviderFilter").input_value() == "feizhu"
            assert await quick.locator("[data-tail-provider='feizhu']").get_attribute("aria-pressed") == "true"
            await quick.locator("[data-tail-provider='']").click()
            assert await page.locator(".tail-result-card").count() == 5
            assert await page.locator("#tailResultProviderFilter").input_value() == ""
            assert len(actions) == 5, "local provider filters must not query a platform"
            assert await page.locator(".tail-provider-progress-card").evaluate_all("els => els.every(el => !el.open)")
            await page.locator(".tail-provider-progress-card").first.locator("summary").click()
            assert await page.locator(".tail-provider-progress-card").first.evaluate("el => el.open")
            dual["status"] = "partial"
            await page.evaluate("pollTailDiscoveryJob('dual-demo',true)")
            await page.wait_for_function("!document.querySelector('#tailRunBtn').disabled")
            assert "携程 + 飞猪" in await page.locator("#tailResultIdentity").inner_text()
            assert "部分平台未完成" in await page.locator("#tailJobSummary").inner_text()
            assert await page.get_by_role("button", name="继续未完成平台", exact=True).is_visible()
            assert await page.locator("#tailJobSummary .tail-job-head button.primary").count() == 1
            assert "primary" in await page.locator("button[data-tail-action='resume']").get_attribute("class")
            assert "ghost" in await page.locator("button[data-tail-action='retry-failed']").get_attribute("class")
            assert await page.locator(".tail-provider-progress-card").first.evaluate("el => el.open")
            assert await page.locator(".tail-provider-progress-card").nth(1).locator("summary .tail-warning").is_visible()
            await page.locator(".tail-provider-progress-card").first.locator("summary").click()
            await page.locator("#tailResultFilterSummary").click()
            await page.locator("#tailResultProviderFilter").select_option("feizhu")
            assert await page.locator(".tail-result-card").count() == 1
            assert "无法判断能省多少" in await page.locator(".tail-result-card").inner_text()
            await page.locator("#tailClearFiltersBtn").click()
            assert await page.locator(".tail-result-card").count() == 5
            await page.locator("#tailResultFilterSummary").click()
            await page.evaluate("document.querySelector('#tailTaskContextDetails').open=false")
            for width in (1280, 390, 320):
                await page.set_viewport_size({"width":width,"height":900})
                await page.evaluate("document.activeElement?.blur(); window.scrollTo(0,0)")
                await page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"), f"dual overflow {width}"
                await page.screenshot(path=str(output / f"tail-dual-{width}.png"), full_page=True)
            await page.evaluate("""() => {
                tailResultView = 'current'; tailSelectedJobId = ''; tailSelectedJob = null;
                tailDiscoveryResults = []; tailResultsLoaded = true;
                renderTailResults([]); updateTailBusyControls();
            }""")
            assert await page.locator("#tailSummary").inner_text() == "尚未开始查询"
            assert await page.locator(".tail-empty-state strong").inner_text() == "先选择我要乘坐的行程"
            assert await page.locator("#tailRunBtn").inner_text() == "开始查询"
            assert "primary" in await page.locator("#tailRunBtn").get_attribute("class")
            assert not errors, errors
            await browser.close()
        client.close()
    print(json.dumps({"ok": True, "viewports": [1280,390,320], "actions": len(actions)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    asyncio.run(run(Path(args.output_dir).resolve()))
