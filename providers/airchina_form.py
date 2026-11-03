"""Opt-in homepage form entry. One submission, no direct-URL fallback.

All writes use normal visible controls. A changed/ambiguous control fails closed;
never force a click, mutate read-only fields or replay an internal request.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import re
import json
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout

from providers.airchina_page import HOME_URL, build_search_url

FORM_SCRIPT = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
INPUT_ROOT = '[class*="input_inputContainer__"]:visible'
PASSENGER_ROOT = '[class*="select_container__"]:visible'
PANEL = '[class*="styles_calendarPanel__"]:visible'
SUBMIT = '[class*="searchCore_searchButton__"]:visible'
ENTRY_MODES = ("direct", "form", "results")


def validate_entry(route, entry_mode, *, today=None):
    if entry_mode not in ENTRY_MODES:
        raise ValueError("国航查询入口只支持 direct / form / results。")
    target = build_search_url(route, today=today)
    # Bound calendar traversal independently from the task's wall-clock limit.
    if entry_mode in {"form", "results"} and date.fromisoformat(route.departure_date) > (today or date.today()) + timedelta(days=365):
        raise ValueError("国航表单实验仅支持未来 365 天内的日期；官网可售范围仍以实际日历为准。")
    return target


def compact(value):
    return re.sub(r"\s+", "", value) if isinstance(value, str) else ""


def same_page(actual, expected):
    try:
        a, e = urlsplit(actual), urlsplit(expected)
        return a.scheme == e.scheme == "https" and a.netloc == e.netloc == "www.airchina.com.cn" and a.path == e.path
    except (TypeError, ValueError):
        return False


def matches(snapshot, route):
    """Never infer selection from typed airport text or request parameters."""
    return {
        "one_way": snapshot.get("trip_type") == "oneWay" and snapshot.get("return_date_count") == 0,
        **{name: (snapshot.get(name, {}).get("count") == 1
                  and snapshot[name].get("input_count") == 1
                  and snapshot[name].get("code") == getattr(route, name).upper()
                  and bool(snapshot[name].get("value")))
           for name in ("origin", "destination")},
        "departure_date": snapshot.get("departure_date") == route.departure_date,
        "passengers": compact(snapshot.get("passengers")) == "1成人0儿童0婴儿",
        "special_passengers": compact(snapshot.get("special_passengers")) == "0残疾军人/伤残人民警察",
        "overlays_closed": snapshot.get("passenger_popup") == 0 and snapshot.get("calendars") == [],
        "submit": snapshot.get("submit_count") == 1 and snapshot.get("submit_ready") is True,
    }


class FormStop(Exception):
    def __init__(self, reason, state="form_entry_unverified"):
        self.reason, self.state = reason, state


class FormEntry:
    def __init__(self, page, route, *, callback=None, progress=None, manual_handler=None):
        self.page, self.route = page, route
        self.target = validate_entry(route, "form")
        self.form_url = HOME_URL
        self.callback, self.progress = callback, progress
        self.manual_handler = manual_handler
        self._verification_resumed = False
        self.airport_side = None
        self.airport_step = None
        self.evidence = {
            "mode": "form", "stage": "opening_home", "reason": "not_submitted",
            "submit_attempted": False, "result_page_observed": False, "checks": {},
        }

    def publish(self, stage, reason=None, **changes):
        self.evidence.update(stage=stage, **changes)
        if reason:
            self.evidence["reason"] = reason
        if self.callback:
            self.callback(deepcopy(self.evidence))

    async def read(self, *, submitted=False):
        data = await self.page.evaluate("(" + FORM_SCRIPT + "\n)(" + json.dumps({
            "origin": self.route.origin.upper(), "destination": self.route.destination.upper(),
        }) + ")")
        if not isinstance(data, dict) or data.get("schema") != "airchina_form_v1":
            raise FormStop("invalid_form_snapshot")
        self.record_airport(data)
        # After click, only the exact expected route is accepted; matching URL
        # does not imply any price or inventory has been verified.
        on_result = same_page(data.get("url"), self.target)
        if submitted and on_result:
            return data
        if not same_page(data.get("url"), self.form_url):
            raise FormStop("unexpected_form_page", "unexpected_page")
        if (
            data.get("verification") is True
            and self.manual_handler is not None
            and not self.evidence["submit_attempted"]
            and not self._verification_resumed
        ):
            self._verification_resumed = True
            self.publish("waiting_verification", "verification")
            await self.manual_handler("needs_verification")
            data = await self.read(submitted=submitted)
            self.publish("verification_cleared", "verification_cleared", verification_cleared=True)
            return data
        for key, state in (("blocked", "blocked"), ("verification", "needs_verification"),
                           ("stale", "stale_results")):
            if data.get(key) is True:
                raise FormStop(key, state)
            if data.get(key) is not False:
                raise FormStop("form_access_unconfirmed")
        if data.get("dialogs") != 0:
            raise FormStop("unknown_home_dialog", "needs_notice_confirmation")
        return data

    def record_airport(self, data):
        if self.airport_side is None:
            return
        raw = data.get(self.airport_side, {}).get("diagnostics") or {}
        observation = {"side": self.airport_side, "step": self.airport_step}
        for key in ("input_matches", "input_focused", "input_enabled", "selected_matches", "counts_capped", "picker_ready"):
            observation[key] = raw.get(key) if type(raw.get(key)) is bool else None
        for key in ("root_count", "input_count", "scoped_candidates", "page_candidates", "scoped_exact", "page_exact"):
            value = raw.get(key)
            observation[key] = min(value, 100) if type(value) is int and value >= 0 else None
        for key in ("verification", "blocked", "stale"):
            observation[key] = data.get(key) if type(data.get(key)) is bool else None
        observation["dialog_present"] = (data["dialogs"] > 0
            if type(data.get("dialogs")) is int else None)
        if observation != self.evidence.get("airport_observation"):
            self.publish(self.evidence["stage"], airport_observation=observation)

    def airport_checkpoint(self, step):
        self.airport_step = step
        self.publish(self.evidence["stage"], airport_step=step)

    async def wait(self, predicate, reason, *, submitted=False):
        # A poll cap handles mocks/fast clocks; wall-clock bounds hung browser I/O.
        async with asyncio.timeout(10):
            for _ in range(40):
                sample = await self.read(submitted=submitted)
                if predicate(sample):
                    return sample
                await self.page.wait_for_timeout(250)
        raise FormStop(reason)

    async def click(self, locator):
        await self.read()
        if await locator.count() != 1:
            raise FormStop("ambiguous_form_control")
        await locator.click(timeout=3000)

    def airport_root(self, name):
        label = "出发机场" if name == "origin" else "到达机场"
        return self.page.locator(INPUT_ROOT).filter(has=self.page.get_by_text(label, exact=True).or_(
            self.page.get_by_placeholder(label, exact=True)))

    async def airport(self, name):
        self.airport_side = name
        self.publish("selecting_" + name)
        self.airport_checkpoint("locating_input")
        code = getattr(self.route, name).upper()
        root = self.airport_root(name)
        sample = await self.read()
        field = root.locator('input:visible')
        if await root.count() != 1 or await field.count() != 1:
            raise FormStop("ambiguous_airport_input")
        # Visible server-rendered inputs are not proof that the city data and
        # focus handlers are ready. Open once, then observe public controls.
        # Do not rewrite/retry input or broaden the candidate scope on failure.
        # Selecting origin can auto-focus/open destination. Do not click an
        # already-ready picker again (the site's click handler can toggle it).
        if sample.get(name, {}).get("diagnostics", {}).get("picker_ready") is not True:
            self.airport_checkpoint("opening_picker")
            await self.click(field)
        self.airport_checkpoint("waiting_picker")
        await self.wait(lambda s: s.get(name, {}).get("diagnostics", {}).get("picker_ready") is True,
                        "airport_waiting_picker_timeout")
        self.airport_checkpoint("filling_input")
        await field.fill(code, timeout=3000)
        self.airport_checkpoint("waiting_candidates")
        await self.read()
        # Fuzzy SHA search also returns PVG and many unrelated airports.
        # Bind the exact code inside the observed candidate row, never its index.
        option = root.locator('li[class*="CitySelector_airportItem__"]:visible').filter(
            has=self.page.locator('[class*="CitySelector_code__"]').filter(has_text=re.compile("^" + code + "$")),
        )
        # Poll the same read-only projection so challenges/restrictions during
        # asynchronous candidate loading terminate the task immediately.
        await self.wait(lambda s: s.get(name, {}).get("diagnostics", {}).get("scoped_exact") == 1,
                        "airport_waiting_candidates_timeout")
        self.airport_checkpoint("clicking_candidate")
        await self.click(option)
        self.airport_checkpoint("confirming_selection")
        await self.wait(lambda s: s.get(name, {}).get("code") == code, "airport_selection_unconfirmed")
        self.airport_checkpoint("selection_confirmed")
        await self.read()
        self.airport_side = None

    async def departure_date(self):
        self.publish("selecting_date")
        # Always use the actual calendar, including when a remembered date matches.
        await self.click(self.page.get_by_placeholder("出发日期", exact=True))
        wanted = date.fromisoformat(self.route.departure_date)
        wanted_month = wanted.year * 12 + wanted.month
        for step in range(13):
            sample = await self.wait(lambda s: len(s.get("calendars", [])) == 2, "calendar_not_ready")
            calendars = sample["calendars"]
            months = []
            for c in calendars:
                if not re.fullmatch(r"\d{4}年", c.get("year") or "") or not re.fullmatch(r"(?:[1-9]|1[0-2])月", c.get("month") or ""):
                    raise FormStop("unknown_calendar_header")
                months.append(int(c["year"][:-1]) * 12 + int(c["month"][:-1]))
            if months[1] != months[0] + 1:
                raise FormStop("ambiguous_calendar_months")
            if wanted_month in months:
                calendar = calendars[months.index(wanted_month)]
                days = [d for d in calendar["days"] if d.get("day") == str(wanted.day)]
                if len(days) != 1 or days[0].get("selectable") is not True:
                    raise FormStop("date_unavailable")
                panel = self.page.locator(PANEL).filter(has=self.page.get_by_text(f"{wanted.year}年", exact=True)).filter(
                    has=self.page.get_by_text(f"{wanted.month}月", exact=True))
                day = panel.locator('[class*="styles_calendarInnerBodyDate__"]:visible').filter(
                    has_text=re.compile("^" + str(wanted.day) + "$"))
                await self.click(day)
                await self.wait(lambda s: s.get("departure_date") == self.route.departure_date and not s.get("calendars"),
                                "date_selection_unconfirmed")
                return
            if step == 12:
                raise FormStop("calendar_navigation_limit")
            direction = "Prev" if wanted_month < months[0] else "Next"
            button = self.page.locator(f'[class*="styles_calendarHeader{direction}Btn__"]:visible')
            if await button.count() != 1 or "disabled" in (await button.get_attribute("class") or "").lower():
                raise FormStop("calendar_navigation_unavailable")
            await self.click(button)
            await self.wait(lambda s: s.get("calendars") != calendars, "calendar_did_not_change")
        raise FormStop("calendar_navigation_limit")

    async def passengers(self):
        self.publish("selecting_passengers")
        root = self.page.locator(PASSENGER_ROOT).filter(has=self.page.get_by_text("乘客类型", exact=True))
        await self.click(root.locator('[class*="select_title__"]:visible'))
        await self.wait(lambda s: s.get("passenger_popup") == 1 and len(s.get("counters", [])) == 3,
                        "passenger_controls_missing")
        # Remove infants/children before reducing adults to avoid invalid ratios.
        for label, wanted in (("婴儿", 0), ("儿童", 0), ("成人", 1)):
            for _ in range(10):
                sample = await self.read()
                counters = [c for c in sample["counters"] if c.get("label") == label]
                if len(counters) != 1 or not re.fullmatch(r"[0-9]", counters[0].get("value") or ""):
                    raise FormStop("unknown_passenger_count")
                counter = counters[0]
                value = int(counter["value"])
                if value == wanted:
                    break
                sign = "-" if value > wanted else "+"
                if counter["minus_disabled" if sign == "-" else "plus_disabled"]:
                    raise FormStop("passenger_control_disabled")
                row = root.locator('[class*="select_stepContainer__"]:visible').filter(
                    has=self.page.get_by_text(label, exact=True))
                await self.click(row.locator('[class*="stepper_stepperBtn__"]:visible').filter(
                    has_text=re.compile("^" + re.escape(sign) + "$")))
                expected = str(value + (-1 if sign == "-" else 1))
                await self.wait(lambda s: any(c.get("label") == label and c.get("value") == expected
                                            for c in s.get("counters", [])), "passenger_count_did_not_change")
            else:
                raise FormStop("passenger_adjustment_limit")
        await self.click(root.locator('[class*="select_primaryBtn__"]:visible').filter(has_text=re.compile("^确定$")))
        await self.wait(lambda s: s.get("passenger_popup") == 0 and
                        compact(s.get("passengers")) == "1成人0儿童0婴儿", "passenger_selection_unconfirmed")

    async def run(self, timeout_ms):
        try:
            async with asyncio.timeout(min(timeout_ms, 60000) / 1000):
                self.publish("opening_home")
                if same_page(getattr(self.page, "url", ""), HOME_URL):
                    await self.read()
                else:
                    response = await self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                    status = getattr(response, "status", None)
                    if status is not None and status >= 400:
                        raise FormStop("home_http_error", "blocked" if status in {401, 403, 429, 432} else "form_entry_unverified")
                await self.wait(lambda s: s.get("one_way_count") == 1 and
                                all(s.get(n, {}).get("input_count") == 1 for n in ("origin", "destination")) and
                                s.get("submit_count") == 1 and s.get("passengers") is not None,
                                "home_form_not_ready")
                if self.progress:
                    self.progress("filling_form")
                self.publish("filling_form")
                await self.click(self.page.locator('[data-id="oneWay"]:visible'))
                await self.wait(lambda s: s.get("trip_type") == "oneWay" and s.get("return_date_count") == 0,
                                "one_way_not_selected")
                await self.airport("origin")
                await self.airport("destination")
                await self.departure_date()
                await self.passengers()
                sample = await self.read()
                checks = matches(sample, self.route)
                self.publish("checking_form", checks=checks)
                if not all(checks.values()):
                    raise FormStop("form_conditions_mismatch")
                # Recheck after audit persistence; changed conditions never use
                # a previous successful check to authorize the final click.
                if not all(matches(await self.read(), self.route).values()):
                    raise FormStop("form_changed_before_submit")
                if self.progress:
                    self.progress("submitting_form")
                self.publish("submitting_form", "submit_attempted", submit_attempted=True)
                # Set attempted BEFORE click. Timeout/exception must not retry:
                # the site may already have accepted the click.
                if not all(matches(await self.read(), self.route).values()):
                    raise FormStop("form_changed_before_submit")
                await self.page.locator(SUBMIT).click(timeout=3000)
                await self.wait(lambda s: same_page(s.get("url"), self.target), "submit_outcome_unknown", submitted=True)
                self.publish("result_page", "result_page_observed", result_page_observed=True)
                return {"state": "query_submitted", "entry_evidence": deepcopy(self.evidence)}
        except FormStop as exc:
            self.publish("stopped", exc.reason, failed_stage=self.evidence["stage"])
            return {"state": exc.state, "entry_evidence": deepcopy(self.evidence)}
        except (TimeoutError, BrowserTimeout):
            # Best-effort final projection, without another action or navigation.
            # Persistence failures/cancellation must still propagate.
            if self.airport_side:
                try:
                    async with asyncio.timeout(1):
                        await self.read()
                except FormStop as exc:
                    self.publish("stopped", exc.reason, failed_stage=self.evidence["stage"])
                    return {"state": exc.state, "entry_evidence": deepcopy(self.evidence)}
                except (TimeoutError, BrowserTimeout):
                    pass
            reason = ("airport_" + self.airport_step + "_timeout" if self.airport_side
                      else "submit_outcome_unknown" if self.evidence["submit_attempted"] else "form_wait_timeout")
            self.publish("stopped", reason,
                         failed_stage=self.evidence["stage"])
            return {"state": "form_entry_unverified", "entry_evidence": deepcopy(self.evidence)}
