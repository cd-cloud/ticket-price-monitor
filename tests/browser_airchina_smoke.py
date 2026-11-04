"""Optional offline Chrome UI smoke. All requests are intercepted; no airline I/O."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from datetime import date, timedelta
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect
from test_experimental_queries import config_file
from web_ui import create_app
from providers.airchina_notices import NoticeHandler
from providers.airchina_parser import DOM_SCRIPT


async def run(output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        with TestClient(create_app(str(config_file(Path(temp))))) as client:
            jobs, actions, errors = [], [], []
            session_actions = []
            session = {"state": "closed", "reused": False, "backup": {"available": False}}
            policy = {"paused": False, "pause_reason": None, "active": False, "retry_after_seconds": 0,
                      "min_interval_seconds": 30, "cache_ttl_seconds": 120,
                      "browser_runs": 1, "cache_hits": 0}
            policy_actions = []
            offline = False
            async with async_playwright() as p:
                browser = await p.chromium.launch(channel="chrome", headless=True)
                context = await browser.new_context(viewport={"width": 1280, "height": 1000})

                async def intercept(route):
                    nonlocal offline
                    request = route.request
                    url = urlsplit(request.url)
                    assert url.hostname == "127.0.0.1", "External traffic not permitted."
                    if url.path.startswith("/api/experiments/airchina"):
                        if request.method == "GET":
                            if offline:
                                return await route.fulfill(status=503, json={"detail": "模拟离线"})
                            if url.path.endswith("/session"):
                                if session["state"] == "opening":
                                    session.update(state="waiting_user")
                                return await route.fulfill(json=copy.deepcopy(session))
                            return await route.fulfill(json={"jobs": jobs, "query_policy": policy})
                        if url.path.endswith("/policy/resume"):
                            policy_actions.append(json.loads(request.post_data))
                            policy.update(paused=False, pause_reason=None)
                            return await route.fulfill(json=policy)
                        if request.method == "DELETE" and url.path.endswith("/session/backup"):
                            session_actions.append(url.path)
                            session["backup"] = {"available": False}
                            return await route.fulfill(json={"available": False})
                        if url.path.endswith("/session/prepare") or url.path.endswith("/session/restore"):
                            session_actions.append(url.path)
                            session.update(state="opening", retention_reason=None)
                            return await route.fulfill(json=copy.deepcopy(session), status=202)
                        if url.path.endswith("/session/ready"):
                            session_actions.append(url.path)
                            submitted = json.loads(request.post_data)
                            session.update(state="ready", retention_reason="manual_session_prepared")
                            session["backup"] = {
                                "available": submitted["backup"],
                                "saved_at": "2026-11-02T08:00:00+00:00" if submitted["backup"] else None,
                            }
                            return await route.fulfill(json=copy.deepcopy(session))
                        if url.path.endswith("/session/cancel-prepare"):
                            session_actions.append(url.path)
                            session.update(state="closed")
                            return await route.fulfill(json=copy.deepcopy(session))
                        actions.append(url.path)
                        if url.path.endswith("/session/close"):
                            session.update(state="closed")
                            return await route.fulfill(json={"closed": True})
                        if url.path.endswith("/continue"):
                            jobs[0]["session_evidence"]["state"] = "retained"
                            jobs[0].update(status="completed_reference", message="离线模拟：不含税参考价",
                                result={"reference_preview": {"offers": [{
                                    "flight_no": "CA1549", "departure_time": "16:30", "arrival_time": "18:45",
                                    "cabin": "economy", "reference_fare": 600,
                                }]}})
                        elif url.path.endswith("/cancel"):
                            jobs[0].update(status="cancelled", message="已取消", result=None)
                        else:
                            submitted = json.loads(request.post_data)
                            assert submitted["auto_confirm_notices"] is True
                            assert submitted["entry_mode"] in {"form", "results"}
                            assert submitted["interaction_mode"] in {"stop", "wait"}
                            expected_session = "reuse" if submitted["entry_mode"] == "results" else "persistent"
                            assert submitted["session_mode"] == expected_session
                            stopped = submitted["interaction_mode"] == "stop"
                            jobs[:] = [{
                                "job_id": "offline-demo", "provider": "airchina",
                                "status": "inconclusive" if stopped else "waiting_user",
                                "stage": "needs_verification", "request": json.loads(request.post_data),
                                "message": "请在项目浏览器中手动完成验证；离线模拟，无真实官网请求。",
                                "created_at": "2026-10-30T08:00:00Z", "updated_at": "2026-10-30T08:00:00Z",
                                "deadline_at": "2026-10-30T08:04:00Z", "result": None, "quote_saved": False,
                                "entry_evidence": {"mode":"form", "stage":"result_page", "reason":"result_page_observed",
                                    "submit_attempted":True, "result_page_observed":True,
                                    "checks":{"origin":True, "destination":True, "departure_date":True, "passengers":True}},
                                "access_events": [{"state":"needs_verification", "phase":"result_page",
                                    "action":"stop" if stopped else "wait", "observed_at":"2026-10-30T08:00:00Z"}],
                                "session_evidence": {"mode":"persistent", "state":"closed" if stopped else "in_use",
                                    "reused":False, "reason":"first_use", "use_number":1},
                                "notices": [{
                                    "notice_id": "offline-notice", "title": "测试提示 <script>error()</script>",
                                    "body": "测试提示正文，仅用于离线展示。<img src=x onerror=alert(1)>",
                                    "status": "auto_confirmed", "button": "我知道了",
                                    "observed_at": "2026-10-30T08:00:00Z",
                                }],
                            }]
                            if stopped:
                                jobs[0]["message"] = "离线模拟：无人值守已停止，不等于无票。"
                        return await route.fulfill(json=copy.deepcopy(jobs[0]), status=202)
                    assert request.method == "GET", f"Unexpected mutation: {url.path}"
                    response = client.get(url.path + ("?" + url.query if url.query else ""))
                    await route.fulfill(status=response.status_code, body=response.content,
                                        content_type=response.headers.get("content-type", "text/plain"))

                await context.route("**/*", intercept)
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto("http://127.0.0.1/")
                await page.locator('[data-workspace-tab="experiments"]').click()
                form = page.locator("#airchinaExperimentForm")
                await expect(page.locator("#airchinaAdvancedSettings")).not_to_have_attribute("open", "")
                await form.locator("#airchinaAdvancedSettings summary").click()
                await expect(page.locator("#airchinaAdvancedSettings")).to_have_attribute("open", "")
                await expect(form.locator('[name="query_engine"]')).to_have_value("native")
                await form.locator('[name="query_engine"]').select_option("flyclaw")
                await expect(form.locator('[name="query_engine"]')).to_have_value("flyclaw")
                await form.locator('[name="query_engine"]').select_option("native")
                await expect(form.locator('[name="entry_mode"]')).to_have_value("form")
                await expect(form.locator('[name="interaction_mode"]')).to_have_value("wait")
                await expect(form.locator('[name="session_mode"]')).to_have_value("persistent")
                await page.locator("#airchinaSessionPrepare").click()
                await expect(page.locator("#airchinaSessionReady")).to_be_visible()
                await page.locator("#airchinaSessionReady").click()
                await expect(page.locator("#airchinaSessionStatus")).to_contain_text("会话可复用")
                await expect(page.locator("#airchinaSessionStatus")).to_contain_text("本机加密备份")
                assert session_actions == [
                    "/api/experiments/airchina/session/prepare",
                    "/api/experiments/airchina/session/ready",
                ]
                await form.locator('[name="origin"]').fill("PEK")
                await form.locator('[name="destination"]').fill("SHA")
                await form.locator('[name="departure_date"]').fill((date.today() + timedelta(days=10)).isoformat())
                await form.locator('[name="entry_mode"]').select_option("form")
                await form.locator('[type="submit"]').click()
                await expect(page.locator("#airchinaExperimentContinue")).to_be_visible()
                await expect(page.locator("#airchinaExperimentStatus")).to_contain_text("官网表单")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("提交前核对")
                await expect(page.locator('[data-diagnostic="access"]')).not_to_have_attribute("open", "")
                await page.locator('[data-diagnostic="access"] summary').click()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("当时进入人工等待")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("本次新建")
                await expect(page.locator("#airchinaSessionClose")).to_be_disabled()
                await expect(page.locator("#airchinaExperimentNotices")).to_contain_text("已自动确认")
                await expect(page.locator("#airchinaNoticesDetails")).not_to_have_attribute("open", "")
                await page.locator("#airchinaNoticesDetails > summary").click()
                await page.locator("#airchinaExperimentNotices summary").click()
                await expect(page.locator("#airchinaExperimentNotices pre")).to_be_visible()
                assert await page.locator("#airchinaExperimentNotices script, #airchinaExperimentNotices img").count() == 0
                await expect(form.locator('[type="submit"]')).to_be_disabled()
                await page.screenshot(path=str(output / "airchina-waiting-offline.png"), full_page=True)
                await page.locator("#airchinaExperimentContinue").click()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("¥600 起")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("不含税")
                await expect(page.locator("#airchinaExperimentContinue")).to_be_hidden()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("任务结束时保留")
                await expect(page.locator("#airchinaExperimentNotices pre")).to_be_visible()
                await expect(page.locator('[data-diagnostic="access"]')).to_have_attribute("open", "")
                await expect(page.locator("#airchinaNoticesDetails")).to_have_attribute("open", "")
                assert len(actions) == 2
                await page.screenshot(path=str(output / "airchina-reference-offline.png"), full_page=True)
                jobs[0]["result"]["query_policy"] = {
                    "source": "cache", "observed_at": "2026-11-03T03:00:00+00:00",
                    "age_seconds": 30, "ttl_seconds": 120,
                }
                policy.update(paused=True, pause_reason="needs_verification", cache_hits=1)
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("缓存参考（未重新查价）")
                await expect(page.locator("#airchinaQueryPolicy")).to_contain_text("已暂停：验证码")
                await expect(page.locator("#airchinaQueryResume")).to_be_enabled()
                await page.locator("#airchinaSessionCard .airchina-secondary summary").click()
                page.once("dialog", lambda dialog: dialog.accept())
                await page.locator("#airchinaQueryResume").click()
                await expect(page.locator("#airchinaQueryResume")).to_be_hidden()
                assert policy_actions == [{"confirmed": True}]
                assert len(actions) == 2  # Resume never submits a query.
                policy.update(paused=True, pause_reason="rate_limited", retry_after_seconds=60)
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator("#airchinaQueryResume")).to_be_disabled()
                await page.screenshot(path=str(output / "airchina-policy-offline.png"), full_page=True)
                policy.update(paused=False, pause_reason=None, retry_after_seconds=0)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.wait_for_timeout(200)
                await page.locator("#airchinaExperimentResults").scroll_into_view_if_needed()
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                await page.screenshot(path=str(output / "airchina-mobile-offline.png"))
                offline = True
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator("#airchinaExperimentError")).to_contain_text("连接状态未知")
                await expect(form.locator('[type="submit"]')).to_be_disabled()
                assert len(actions) == 2  # Never auto-resubmit after network errors.
                offline = False
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(form.locator('[type="submit"]')).to_be_enabled()
                jobs[0].update(status="inconclusive", entry_evidence=None, access_events=None, session_evidence=None,
                              result={"wait_evidence": {
                    "reason": "result_wait_budget", "polls": 40, "coverage_complete": False,
                }})
                await page.locator("#airchinaExperimentRefresh").click()
                # Legacy jobs have no detailed samples: don't invent a zero-second
                # budget or claim there was no DOM observation.
                await expect(page.locator("#airchinaExperimentResults")).to_be_empty()
                jobs[0]["network_diagnostics"] = {
                    "schema": "airchina_network_diagnostics_v1", "attached": True, "finished": True,
                    "elapsed_ms": 14000, "environment": {"backend": "chrome", "persistent_profile": True,
                        "storage_state_loaded": False, "profile_role": "primary"},
                    "counts": {"requests": 8, "responses": 7, "query_candidates": 1, "failed": 1},
                    "http_statuses": {"200": 6, "403": 1}, "failure_types": {"dns": 1},
                    "script_error_groups": [{"kind":"TypeError", "category":"undefined_access", "count":23,
                        "first_phase":"selecting_origin", "last_phase":"waiting_user",
                        "first_at_ms":8000, "last_at_ms":14000}],
                    "phases": [{"phase": "submitting_form", "at_ms": 6000},
                               {"phase": "result_page", "at_ms": 10000}],
                    "events": [{"id": 8, "event": "response", "phase": "submitting_form",
                        "event_phase": "result_page", "scope": "official", "resource": "fetch",
                        "status": 403, "elapsed_ms": 4000}],
                }
                jobs[0]["entry_evidence"] = {
                    "stage":"stopped", "reason":"airport_waiting_candidates_timeout",
                    "airport_step":"waiting_candidates", "airport_observation":{
                        "side":"origin", "step":"waiting_candidates", "input_matches":True,
                        "scoped_candidates":0, "page_candidates":3, "scoped_exact":0, "page_exact":1,
                    },
                }
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator('[data-diagnostic="network"]')).not_to_have_attribute("open", "")
                await page.locator('[data-diagnostic="network"] > summary').click()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("单次查询网络诊断")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("国航主档案")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("403 × 1")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("读取或设置空值属性 × 23")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("精确匹配：0/1")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("未等到容器内精确匹配")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("不证明登录或验证有效")
                await page.get_by_text("响应与失败摘要（有界、已脱敏）", exact=True).click()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("官网主站 · fetch · HTTP 403")
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                await page.screenshot(path=str(output / "airchina-network-offline.png"), full_page=True)
                jobs[0]["network_diagnostics"] = None
                jobs[0].update(status="inconclusive", result=None, diagnostics={
                    "polls": 60, "stable_samples": 0, "elapsed_ms": 30000, "budget_ms": 30000,
                    "last_observation": {
                        "parser_reason": "page_reports_no_results", "visible_rows": 0,
                        "accepted_offers": 0, "rejected_rows": 0, "filtered_offers": 0, "conflicting_offers": 0,
                        "context_present": {"price_notice": False},
                        "guards": {"no_results_visible": True}, "row_rejection_reasons": {},
                    },
                })
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator('[data-diagnostic="reading"]')).not_to_have_attribute("open", "")
                await page.locator('[data-diagnostic="reading"] > summary').click()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("官网显示“查询无结果")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("可见航班行：0")
                await expect(page.locator("#airchinaExperimentResults table")).to_have_count(0)
                await expect(page.locator("#airchinaExperimentNotices pre")).to_be_visible()
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                await page.screenshot(path=str(output / "airchina-diagnostics-offline.png"), full_page=True)
                # Stop mode is a terminal result, never a fake resumable session.
                await form.locator('[name="interaction_mode"]').select_option("stop")
                await form.locator('[type="submit"]').click()
                await expect(page.locator("#airchinaExperimentStatus")).to_contain_text("无人值守已停止")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("当时停止读取")
                await expect(page.locator("#airchinaExperimentContinue")).to_be_hidden()
                await expect(page.locator("#airchinaExperimentCancel")).to_be_hidden()
                await expect(form.locator('[type="submit"]')).to_be_enabled()
                await expect(page.locator("#airchinaExperimentResults table")).to_have_count(0)
                assert len(actions) == 3  # No automatic continue or retry.
                await page.locator("#airchinaSessionClose").click()
                await expect(page.locator("#airchinaSessionStatus")).to_contain_text("会话已关闭")
                assert len(actions) == 4 and actions[-1].endswith("/session/close")
                await page.screenshot(path=str(output / "airchina-unattended-offline.png"), full_page=True)
                await form.locator('[name="entry_mode"]').select_option("results")
                await expect(form.locator('[name="interaction_mode"]')).to_have_value("stop")
                await expect(form.locator('[name="interaction_mode"]')).to_be_disabled()
                await expect(form.locator('[name="session_mode"]')).to_have_value("reuse")
                await expect(form.locator('[name="session_mode"]')).to_be_disabled()
                await form.locator('[type="submit"]').click()
                await expect(page.locator("#airchinaExperimentError")).to_be_empty()
                assert jobs[0]["request"]["interaction_mode"] == "stop"
                assert jobs[0]["request"]["session_mode"] == "reuse"
                assert len(actions) == 5
                jobs[0]["request"]["entry_mode"] = "results"
                jobs[0]["entry_evidence"] = {
                    "mode": "results", "stage": "stopped", "reason": "refresh_not_observed",
                    "baseline_validated": True, "submit_attempted": True, "refresh_observed": False,
                }
                jobs[0]["network_diagnostics"] = {
                    "schema": "airchina_network_diagnostics_v1", "attached": True, "finished": True,
                    "lifecycle": {"main_document_requests": 0, "dom_content_loaded": 0, "main_frame_navigations": 1},
                }
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator("#airchinaExperimentStatus")).to_contain_text("结果页续查")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("不能把旧票价作为新结果")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("主文档请求 0")
                await expect(page.locator("#airchinaExperimentResults table")).to_have_count(0)
                await page.screenshot(path=str(output / "airchina-requery-offline.png"), full_page=True)
                jobs[0].update(status="waiting_user", stage="form_inspection",
                               message="离线模拟：表单失败，保留窗口供检查，不自动重提。")
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator("#airchinaExperimentContinue")).to_have_text("结束检查并关闭")
                await expect(form.locator('[type="submit"]')).to_be_disabled()
                await expect(page.locator("#airchinaSessionClose")).to_be_disabled()
                assert len(actions) == 5
                jobs[0]["stage"] = "needs_verification"
                await page.locator("#airchinaExperimentRefresh").click()
                await expect(page.locator("#airchinaExperimentContinue")).to_have_text("我已手动处理，继续读取")
                assert not errors, errors
                # Real browser DOM exercise of the production notice handler,
                # but all HTML and navigation are synthetic/offline.
                fixture = json.loads((ROOT / "tests/fixtures/airchina_travel_notices.json").read_text())
                modal_page = await context.new_page()
                async def fake_site(route):
                    await route.fulfill(content_type="text/html", body="""<!doctype html>
                        <style>.dialog_mainContent__test {width:600px;padding:20px}</style>
                        <div id="notices"></div>""")
                await modal_page.route("**/*", fake_site)
                expected_url = "https://www.airchina.com.cn/flight/oneway/pek-sha/2099-01-01"
                await modal_page.goto(expected_url)
                await modal_page.evaluate("""items => {
                    let index = 0;
                    const root = document.querySelector('#notices');
                    function render() {
                        root.replaceChildren();
                        if (index >= items.length) return;
                        const box = document.createElement('div');
                        box.className = 'dialog_mainContent__test';
                        const title = document.createElement('div');
                        title.className = 'model_header__test';
                        title.textContent = items[index].title;
                        const body = document.createElement('pre');
                        body.className = 'model_body__test';
                        body.textContent = items[index].body;
                        const footer = document.createElement('div');
                        footer.className = 'model_footer__test';
                        const button = document.createElement('div');
                        button.className = 'model_btn__test';
                        button.textContent = items[index].buttons[0];
                        button.onclick = () => { index++; render(); };
                        footer.append(button); box.append(title, body, footer); root.append(box);
                    }
                    render();
                }""", fixture["notices"])
                audits = []
                handler = NoticeHandler(enabled=True, callback=lambda rows: audits.append(rows))
                assert await handler.handle(modal_page, expected_url) == "clear"
                assert len(handler.entries) == 3
                assert all(n["status"] == "auto_confirmed" for n in handler.entries)
                assert await modal_page.locator('[class*="dialog_mainContent__"]').count() == 0
                # Exercise the read-only collector on observed empty-state text;
                # a calendar amount must never become a flight row.
                await modal_page.locator("#notices").evaluate("""el => {
                    el.textContent = '查询无结果～\\n可尝试更换日期或稍后重试\\n11月10日 ¥600';
                }""")
                empty = await modal_page.evaluate("(" + DOM_SCRIPT + ")()")
                assert empty["no_results_visible"] is True
                assert empty["rows"] == []
                await modal_page.locator("#notices").evaluate("el => { el.style.display = 'none'; }")
                hidden = await modal_page.evaluate("(" + DOM_SCRIPT + ")()")
                assert hidden["no_results_visible"] is False

                await page.reload()
                await page.locator("#workspaceTabSelect").select_option("experiments")
                assert await page.locator("#workspaceTabSelect").input_value() == "experiments"
                form = page.locator("#airchinaExperimentForm")
                await expect(form.locator('[name="origin"]')).to_have_value("PEK")
                await expect(form.locator('[name="destination"]')).to_have_value("SHA")
                saved = json.loads(await page.evaluate("localStorage.getItem('airchina_experiment_form_v1')"))
                assert set(saved) == {"origin", "destination", "departure_date", "cabin", "query_engine",
                                      "entry_mode", "interaction_mode", "session_mode", "flight_no",
                                      "auto_confirm_notices"}
                assert "password" not in saved and "token" not in saved and "cookie" not in saved
                await page.locator("#airchinaFormReset").click()
                await expect(form.locator('[name="origin"]')).to_have_value("")
                await expect(form.locator('[name="query_engine"]')).to_have_value("native")
                await page.set_viewport_size({"width": 320, "height": 740})
                await page.wait_for_timeout(200)
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                await page.screenshot(path=str(output / "airchina-mobile-320-offline.png"), full_page=True)
                await browser.close()
    print("Air China offline UI smoke passed: submit, manual continue, unattended stop/access history, reference-only results, notice audit/XSS, mobile, offline recovery, diagnostics, empty-state collector, 3-modal production handler.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output_dir))
