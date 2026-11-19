"""One same-condition query through the retained results page's visible form.

No goto, reload, retry, cookie/storage inspection or internal request replay.
Opening the form is not a query; unchanged old fares cannot count as a result.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

from playwright.async_api import TimeoutError as BrowserTimeout

from providers.airchina_form import FormEntry, FormStop, SUBMIT, matches
from providers.airchina_page import snapshot_stop_state
from providers.airchina_parser import DOM_SCRIPT, parse_reference_fares
from providers.airchina_result_wait import LIVE_GUARDS


class ResultsRequery(FormEntry):
    def __init__(self, page, route, *, callback=None, progress=None):
        super().__init__(page, route, callback=callback, progress=progress)
        self.form_url = self.target
        self.evidence.update(
            mode="results", stage="checking_retained_results",
            baseline_validated=False, refresh_observed=False,
            refresh_signal=None, observation_only_before_submit=True,
        )

    async def result_snapshot(self):
        data = await self.page.evaluate("(" + DOM_SCRIPT + "\n)()")
        if not isinstance(data, dict) or data.get("schema") != "airchina_visible_oneway_v1":
            raise FormStop("invalid_result_snapshot")
        stop = snapshot_stop_state(data, self.target)
        if stop:
            raise FormStop(stop, stop)
        if any(type(data.get(key)) is not bool for key in LIVE_GUARDS):
            raise FormStop("result_access_unconfirmed")
        if not isinstance(data.get("rows"), list):
            raise FormStop("invalid_result_snapshot")
        return data

    async def check_baseline(self):
        data = await self.result_snapshot()
        if (any(data[key] for key in LIVE_GUARDS)
                or parse_reference_fares(data, self.route)["status"] != "reference_fares_observed"):
            raise FormStop("retained_results_mismatch")

    async def observe_refresh(self):
        # Running alongside the click avoids missing a short loading interval.
        # False negatives fail closed. A transition is not proof of server-side
        # freshness, inventory or a successful business response.
        for _ in range(200):
            data = await self.result_snapshot()
            signal = ("loading_visible" if data["loading_visible"] else
                      "previous_rows_cleared" if not data["rows"] else None)
            if signal:
                self.publish("observing_refresh", "visible_refresh_observed",
                             refresh_observed=True, refresh_signal=signal)
                return
            await self.page.wait_for_timeout(50)
        raise FormStop("refresh_not_observed")

    async def run(self, timeout_ms):
        watcher = None
        try:
            async with asyncio.timeout(min(timeout_ms, 30000) / 1000):
                self.publish("checking_retained_results")
                await self.check_baseline()
                self.publish("opening_results_form", baseline_validated=True)
                data = await self.read()
                if data.get("submit_count") == 0:
                    await self.click(self.page.get_by_text("重新查询", exact=True))
                await self.wait(lambda s: s.get("submit_count") == 1, "results_form_not_ready")
                checks = matches(await self.read(), self.route)
                self.publish("checking_form", checks=checks)
                if not all(checks.values()):
                    raise FormStop("form_conditions_mismatch")
                # Deliberately do not edit/reselect conditions in this controlled
                # experiment: compare only same route, date and ordinary adult.
                await self.check_baseline()
                if self.progress:
                    self.progress("submitting_form")
                self.publish("submitting_form", "submit_attempted", submit_attempted=True)
                if not all(matches(await self.read(), self.route).values()):
                    raise FormStop("form_changed_before_submit")
                watcher = asyncio.create_task(self.observe_refresh())
                await self.page.locator(SUBMIT).click(timeout=3000)
                await watcher
                self.publish("result_page", "visible_refresh_observed", result_page_observed=True)
                return {"state": "query_submitted", "entry_evidence": deepcopy(self.evidence)}
        except FormStop as exc:
            self.publish("stopped", exc.reason, failed_stage=self.evidence["stage"])
            return {"state": exc.state, "entry_evidence": deepcopy(self.evidence)}
        except (TimeoutError, BrowserTimeout):
            self.publish("stopped", "refresh_not_observed" if self.evidence["submit_attempted"]
                         else "form_wait_timeout", failed_stage=self.evidence["stage"])
            return {"state": "form_entry_unverified", "entry_evidence": deepcopy(self.evidence)}
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
