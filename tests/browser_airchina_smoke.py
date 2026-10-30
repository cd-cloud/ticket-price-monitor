"""Optional offline Chrome UI smoke. All requests are intercepted; no airline I/O."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect
from test_experimental_queries import config_file
from web_ui import create_app


async def run(output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        with TestClient(create_app(str(config_file(Path(temp))))) as client:
            jobs, actions, errors = [], [], []
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
                            return await route.fulfill(json={"jobs": jobs})
                        actions.append(url.path)
                        if url.path.endswith("/continue"):
                            jobs[0].update(status="completed_reference", message="离线模拟：不含税参考价",
                                result={"reference_preview": {"offers": [{
                                    "flight_no": "CA1549", "departure_time": "16:30", "arrival_time": "18:45",
                                    "cabin": "economy", "reference_fare": 600,
                                }]}})
                        elif url.path.endswith("/cancel"):
                            jobs[0].update(status="cancelled", message="已取消", result=None)
                        else:
                            jobs[:] = [{
                                "job_id": "offline-demo", "provider": "airchina", "status": "waiting_user",
                                "stage": "needs_verification", "request": json.loads(request.post_data),
                                "message": "请在项目浏览器中手动完成验证；离线模拟，无真实官网请求。",
                                "created_at": "2026-10-30T08:00:00Z", "updated_at": "2026-10-30T08:00:00Z",
                                "deadline_at": "2026-10-30T08:04:00Z", "result": None, "quote_saved": False,
                            }]
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
                await form.locator('[name="origin"]').fill("PEK")
                await form.locator('[name="destination"]').fill("SHA")
                await form.locator('[name="departure_date"]').fill("2099-01-01")
                await form.locator('[type="submit"]').click()
                await expect(page.locator("#airchinaExperimentContinue")).to_be_visible()
                await expect(form.locator('[type="submit"]')).to_be_disabled()
                await page.screenshot(path=str(output / "airchina-waiting-offline.png"), full_page=True)
                await page.locator("#airchinaExperimentContinue").click()
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("¥600 起")
                await expect(page.locator("#airchinaExperimentResults")).to_contain_text("不含税")
                await expect(page.locator("#airchinaExperimentContinue")).to_be_hidden()
                assert len(actions) == 2
                await page.screenshot(path=str(output / "airchina-reference-offline.png"), full_page=True)
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
                assert not errors, errors
                await browser.close()
    print("Air China offline UI smoke passed: submit, manual continue, reference-only results, mobile, offline recovery.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output_dir))
