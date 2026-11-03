"""Offline real-Chrome exercise of production form selectors and entry flow."""
import asyncio
from datetime import date, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config_manager import RouteQuery
from playwright.async_api import async_playwright
from providers.airchina_form import FormEntry


async def run():
    requested = RouteQuery("PEK", "SHA", (date.today() + timedelta(days=40)).isoformat())
    template = (ROOT / "tests/fixtures/airchina_form_mock.html").read_text()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        for scenario in ("success", "autofocus_destination", "delayed_picker", "delayed_candidates", "picker_not_ready",
                         "verification_candidates", "blocked_candidates",
                         "special", "missing_airport", "wrong_date", "portal_candidates", "no_candidates"):
            context = await browser.new_context()
            requests = []
            async def intercept(route):
                requests.append(route.request.url)
                # Intercept every request; never contact an airline.
                await route.fulfill(content_type="text/html", body=template.replace("__SCENARIO__", json.dumps(scenario)))
            await context.route("**/*", intercept)
            page = await context.new_page()
            errors, audits = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            entry = FormEntry(page, requested, callback=audits.append)
            result = await entry.run(30000)
            count = await page.locator('body').get_attribute('data-submissions')
            assert not errors, errors
            assert len(requests) == 1, requests  # One homepage, then in-page transition.
            if scenario in {"success", "autofocus_destination", "delayed_picker", "delayed_candidates"}:
                assert result["state"] == "query_submitted", result
                assert count == "1"
                conditions = json.loads(await page.locator('body').get_attribute('data-conditions'))
                assert conditions == {"origin": "PEK", "destination": "SHA", "date": requested.departure_date,
                                      "counts": {"成人": 1, "儿童": 0, "婴儿": 0}}
                assert all(result["entry_evidence"]["checks"].values())
                assert result["entry_evidence"]["result_page_observed"]
                assert await page.locator('body').get_attribute('data-input-events') == "2"
            else:
                assert result["state"] == {
                    "verification_candidates": "needs_verification",
                    "blocked_candidates": "blocked",
                }.get(scenario, "form_entry_unverified"), result
                assert count == "0"
                assert not result["entry_evidence"]["submit_attempted"]
                assert result["entry_evidence"]["failed_stage"] == {
                    "special": "checking_form",
                    "missing_airport": "selecting_origin",
                    "wrong_date": "selecting_date",
                    "portal_candidates": "selecting_origin", "no_candidates": "selecting_origin",
                    "picker_not_ready": "selecting_origin", "verification_candidates": "selecting_origin",
                    "blocked_candidates": "selecting_origin",
                }[scenario]
                if scenario == "picker_not_ready":
                    assert result["entry_evidence"]["reason"] == "airport_waiting_picker_timeout", result
                    assert await page.locator('body').get_attribute('data-input-events') is None
                if scenario in {"verification_candidates", "blocked_candidates"}:
                    assert await page.locator('body').get_attribute('data-input-events') == "1"
                if scenario in {"missing_airport", "portal_candidates", "no_candidates"}:
                    e = result["entry_evidence"]
                    assert e["reason"] == "airport_waiting_candidates_timeout", e
                    a = e["airport_observation"]
                    assert a["input_matches"] is True and a["scoped_exact"] == 0, a
                    assert a["page_exact"] == int(scenario == "portal_candidates"), a
                    assert a["page_candidates"] == {
                        "missing_airport": 2, "portal_candidates": 3, "no_candidates": 0,
                    }[scenario], a
            await context.close()
        await browser.close()
    print("Air China form offline smoke passed: exact airport despite fuzzy decoys, dynamic placeholders, "
          "month traversal, passenger reset, one SPA submission, wrong date/airport/special fail closed.")


if __name__ == "__main__":
    asyncio.run(run())
