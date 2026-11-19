"""Controlled requery tests: no browser, network, profiles or site credentials."""
import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from experimental_query_service import validate_payload
from providers.airchina_form import validate_entry
from providers.airchina_requery import ResultsRequery
from providers.airchina_result_wait import LIVE_GUARDS
from test_airchina_form import ROUTE, snapshot


def baseline():
    data = json.loads((Path(__file__).parent / "fixtures/airchina_visible_oneway.json").read_text())
    d = date.fromisoformat(ROUTE.departure_date)
    data.update(url=validate_entry(ROUTE, "results"), date_text=f"{d.year}年{d.month}月{d.day}日",
                **{key: False for key in LIVE_GUARDS})
    return data


def fake(*, after=None, before=None, form_change=None, expanded=False, callback=None, click_error=None):
    initial = {**baseline(), **(before or {})}
    form = {**snapshot(), "url": initial["url"], "submit_count": int(expanded), **(form_change or {})}
    state = {"submitted": False}
    submit, toggle = AsyncMock(), AsyncMock()
    toggle.count.return_value = 1

    async def opened(**_):
        form["submit_count"] = 1
    toggle.click.side_effect = opened

    async def clicked(**_):
        if click_error:
            raise click_error
        state["submitted"] = True
    submit.click.side_effect = clicked

    async def evaluate(script):
        if "airchina_form_v1" in script:
            return deepcopy(form)
        return deepcopy({**initial, **((after or {}) if state["submitted"] else {})})

    page = SimpleNamespace(
        goto=AsyncMock(), reload=AsyncMock(), evaluate=AsyncMock(side_effect=evaluate),
        wait_for_timeout=AsyncMock(), get_by_text=lambda *a, **kw: toggle,
        locator=lambda _: submit,
    )
    return ResultsRequery(page, ROUTE, callback=callback), page, submit, toggle, form


@pytest.mark.parametrize("after,signal", [
    ({"loading_visible": True}, "loading_visible"), ({"rows": []}, "previous_rows_cleared"),
])
@pytest.mark.parametrize("expanded", [False, True])
def test_same_document_single_submit_requires_visible_refresh(after, signal, expanded):
    entry, page, submit, toggle, _ = fake(after=after, expanded=expanded)
    result = asyncio.run(entry.run(1000))
    assert result["state"] == "query_submitted"
    assert result["entry_evidence"]["refresh_signal"] == signal
    assert result["entry_evidence"]["baseline_validated"]
    submit.click.assert_awaited_once()
    assert toggle.click.await_count == int(not expanded)
    page.goto.assert_not_awaited()
    page.reload.assert_not_awaited()
    assert "offers" not in result


def test_unchanged_old_list_cannot_be_a_new_result_even_if_prices_are_valid():
    entry, _, submit, _, _ = fake()
    result = asyncio.run(entry.run(2000))
    assert result["state"] == "form_entry_unverified"
    assert result["entry_evidence"]["reason"] == "refresh_not_observed"
    assert not result["entry_evidence"]["refresh_observed"]
    submit.click.assert_awaited_once()
    assert "offers" not in result


@pytest.mark.parametrize("change,state", [
    ({"verification_visible": True}, "needs_verification"),
    ({"blocked_visible": True}, "blocked"),
    ({"notice_confirmation_visible": True}, "needs_notice_confirmation"),
    ({"stale_visible": True}, "stale_results"),
    ({"url": "https://example.test/private"}, "unexpected_page"),
    ({"loading_visible": True}, "form_entry_unverified"),
    ({"loading_visible": None}, "form_entry_unverified"),
    ({"rows": []}, "form_entry_unverified"),
    ({"route_text": "北京首都(PEK) 上海浦东(PVG)"}, "form_entry_unverified"),
    ({"date_text": "2099年1月1日"}, "form_entry_unverified"),
])
def test_invalid_baseline_never_opens_form_or_submits(change, state):
    entry, page, submit, toggle, _ = fake(before=change)
    result = asyncio.run(entry.run(1000))
    assert result["state"] == state
    submit.click.assert_not_awaited()
    toggle.click.assert_not_awaited()
    page.goto.assert_not_awaited()
    assert "private" not in str(result)


@pytest.mark.parametrize("change", [
    {"passengers": "2成人0儿童0婴儿"}, {"departure_date": "2099-01-01"},
    {"submit_count": 2}, {"special_passengers": "1残疾军人/伤残人民警察"},
])
def test_form_mismatch_does_not_reselect_or_submit(change):
    entry, _, submit, _, _ = fake(form_change=change, expanded=True)
    result = asyncio.run(entry.run(1000))
    assert result["state"] == "form_entry_unverified"
    submit.click.assert_not_awaited()


def test_callback_condition_change_prevents_click():
    def callback(evidence):
        if evidence["stage"] == "submitting_form":
            form["departure_date"] = "2099-01-01"
    entry, _, submit, _, form = fake(callback=callback)
    result = asyncio.run(entry.run(1000))
    assert result["entry_evidence"]["reason"] == "form_changed_before_submit"
    submit.click.assert_not_awaited()


@pytest.mark.parametrize("flag,state", [
    ("verification_visible", "needs_verification"), ("blocked_visible", "blocked"),
    ("notice_confirmation_visible", "needs_notice_confirmation"), ("stale_visible", "stale_results"),
])
def test_challenge_after_click_stops_without_retry(flag, state):
    entry, page, submit, _, _ = fake(after={flag: True})
    result = asyncio.run(entry.run(1000))
    assert result["state"] == state
    assert not result["entry_evidence"]["refresh_observed"]
    submit.click.assert_awaited_once()
    page.goto.assert_not_awaited()


@pytest.mark.parametrize("error", [TimeoutError("private"), asyncio.CancelledError()])
def test_uncertain_click_never_retries_and_cancels_watcher(error):
    async def run():
        entry, _, submit, _, _ = fake(click_error=error)
        if isinstance(error, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await entry.run(1000)
        else:
            result = await entry.run(1000)
            assert result["state"] == "form_entry_unverified"
            assert "private" not in str(result)
        submit.click.assert_awaited_once()
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


@pytest.mark.parametrize("mode,interaction", [("fresh", "stop"), ("reuse", "wait")])
def test_requery_admission_requires_retained_session_and_no_manual_retry(mode, interaction):
    with pytest.raises(ValueError, match="结果页续查"):
        validate_payload({"origin": ROUTE.origin, "destination": ROUTE.destination,
                          "departure_date": ROUTE.departure_date, "entry_mode": "results",
                          "session_mode": mode, "interaction_mode": interaction})
