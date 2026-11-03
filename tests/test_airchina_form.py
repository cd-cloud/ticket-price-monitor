"""Form admission, fail-closed submission and lifecycle. No network/browser."""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config_manager import RouteQuery
from experimental_query_service import validate_payload
from providers.airchina_form import FormEntry, FormStop, HOME_URL, matches, validate_entry
from providers import airchina


ROUTE = RouteQuery("PEK", "SHA", (date.today() + timedelta(days=10)).isoformat())


def snapshot():
    return {
        "schema": "airchina_form_v1", "url": HOME_URL, "verification": False,
        "blocked": False, "stale": False, "dialogs": 0, "one_way_count": 1,
        "trip_type": "oneWay", "return_date_count": 0,
        "origin": {"count": 1, "input_count": 1, "code": "PEK", "value": "北京首都"},
        "destination": {"count": 1, "input_count": 1, "code": "SHA", "value": "上海虹桥"},
        "departure_date": ROUTE.departure_date, "passengers": "1成人\n0儿童\n0婴儿",
        "special_passengers": "0残疾军人/伤残人民警察", "passenger_popup": 0,
        "calendars": [], "submit_count": 1, "submit_ready": True,
    }


@pytest.mark.parametrize("mode", [None, True, 1, [], {}, "auto", "FORM"])
def test_bad_entry_mode_rejected_before_browser(mode):
    with pytest.raises(ValueError):
        validate_payload({"origin": "PEK", "destination": "SHA",
                          "departure_date": ROUTE.departure_date, "entry_mode": mode})


def test_legacy_request_remains_direct_and_form_horizon_is_explicit():
    payload = {"origin": "PEK", "destination": "SHA", "departure_date": "2099-01-01"}
    assert validate_payload(payload)[2]["entry_mode"] == "direct"
    with pytest.raises(ValueError, match="365"):
        validate_payload({**payload, "entry_mode": "form"})


@pytest.mark.parametrize("change", [
    {"trip_type": "roundTrip"}, {"return_date_count": 1},
    {"origin": {"count": 1, "input_count": 1, "value": "PEK", "code": None}},
    {"destination": {"count": 1, "input_count": 1, "value": "上海浦东", "code": "PVG"}},
    {"departure_date": "2099-01-01"}, {"passengers": "1成人1儿童0婴儿"},
    {"special_passengers": "1残疾军人/伤残人民警察"}, {"passengers": None},
    {"passenger_popup": 1}, {"calendars": [{}]}, {"submit_count": 2},
    {"submit_ready": False},
])
def test_preflight_requires_all_visible_conditions(change):
    assert all(matches(snapshot(), ROUTE).values())
    assert not all(matches({**snapshot(), **change}, ROUTE).values())


def fake_entry(*, change=None, callback=None, click_error=None, manual_handler=None):
    data = {**snapshot(), **(change or {})}
    target = validate_entry(ROUTE, "form")
    submit = AsyncMock()
    async def clicked(**_):
        if click_error:
            raise click_error
        data["url"] = target
    submit.click.side_effect = clicked
    control = AsyncMock()
    control.count.return_value = 1
    page = SimpleNamespace(
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        evaluate=AsyncMock(side_effect=lambda _: deepcopy(data)), wait_for_timeout=AsyncMock(),
        locator=lambda selector: submit if "searchButton" in selector else control,
    )
    entry = FormEntry(page, ROUTE, callback=callback, manual_handler=manual_handler)
    # DOM-control mechanics are exercised separately by the offline Chrome smoke.
    entry.airport = AsyncMock()
    entry.departure_date = AsyncMock()
    entry.passengers = AsyncMock()
    return entry, page, submit, data


def test_form_submits_once_and_does_not_navigate_to_result_url():
    audit = []
    entry, page, submit, _ = fake_entry(callback=audit.append)
    result = asyncio.run(entry.run(1000))
    assert result["state"] == "query_submitted"
    assert result["entry_evidence"]["result_page_observed"]
    assert all(result["entry_evidence"]["checks"].values())
    page.goto.assert_awaited_once_with(HOME_URL, wait_until="domcontentloaded", timeout=1000)
    submit.click.assert_awaited_once()
    assert audit[-2]["submit_attempted"] and not audit[-2]["result_page_observed"]


def test_existing_homepage_skips_duplicate_navigation():
    entry, page, submit, _ = fake_entry()
    page.url = HOME_URL
    result = asyncio.run(entry.run(1000))
    assert result["state"] == "query_submitted"
    page.goto.assert_not_awaited()
    submit.click.assert_awaited_once()


def test_pre_submit_verification_resumes_same_form_without_navigation_or_duplicate_submit():
    manual = AsyncMock()

    async def cleared(_state):
        data["verification"] = False

    manual.side_effect = cleared
    entry, page, submit, data = fake_entry(change={"verification": True}, manual_handler=manual)
    result = asyncio.run(entry.run(1000))
    assert result["state"] == "query_submitted"
    assert result["entry_evidence"]["verification_cleared"]
    manual.assert_awaited_once_with("needs_verification")
    page.goto.assert_awaited_once()
    submit.click.assert_awaited_once()


def test_pre_submit_verification_not_cleared_stops_without_second_manual_attempt():
    manual = AsyncMock()
    entry, page, submit, _ = fake_entry(change={"verification": True}, manual_handler=manual)
    result = asyncio.run(entry.run(1000))
    assert result["state"] == "needs_verification"
    assert not result["entry_evidence"]["submit_attempted"]
    assert not result["entry_evidence"].get("verification_cleared")
    manual.assert_awaited_once_with("needs_verification")
    page.goto.assert_awaited_once()
    submit.click.assert_not_awaited()


@pytest.mark.parametrize(("change", "state"), [
    ({"verification": True}, "needs_verification"),
    ({"blocked": True}, "blocked"),
    ({"dialogs": 1}, "needs_notice_confirmation"),
    ({"url": "https://example.test/?secret=x"}, "unexpected_page"),
    ({"verification": None}, "form_entry_unverified"),
    ({"special_passengers": "1残疾军人/伤残人民警察"}, "form_entry_unverified"),
])
def test_unsafe_home_or_wrong_conditions_never_submit(change, state):
    entry, _, submit, _ = fake_entry(change=change)
    result = asyncio.run(entry.run(1000))
    assert result["state"] == state
    assert not result["entry_evidence"]["submit_attempted"]
    submit.click.assert_not_awaited()
    assert "secret" not in str(result)


def test_submit_timeout_is_unknown_and_never_retried():
    entry, page, submit, _ = fake_entry(click_error=TimeoutError("private-url-token"))
    result = asyncio.run(entry.run(1000))
    assert result["entry_evidence"]["reason"] == "submit_outcome_unknown"
    assert result["entry_evidence"]["submit_attempted"]
    assert not result["entry_evidence"]["result_page_observed"]
    submit.click.assert_awaited_once()
    page.goto.assert_awaited_once()
    assert "private-url-token" not in str(result)


def test_conditions_changed_by_audit_callback_block_submit():
    def record(evidence):
        if evidence["stage"] == "submitting_form":
            data["destination"]["code"] = "PVG"
    entry, _, submit, data = fake_entry(callback=record)
    result = asyncio.run(entry.run(1000))
    assert result["entry_evidence"]["reason"] == "form_changed_before_submit"
    submit.click.assert_not_awaited()


def test_persistence_error_prevents_click():
    def record(evidence):
        if evidence["stage"] == "submitting_form":
            raise OSError("private-db-path")
    entry, _, submit, _ = fake_entry(callback=record)
    with pytest.raises(OSError):
        asyncio.run(entry.run(1000))
    submit.click.assert_not_awaited()


def test_cancellation_propagates_without_second_click():
    entry, _, submit, _ = fake_entry(click_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(entry.run(1000))
    submit.click.assert_awaited_once()


def test_adapter_form_failure_does_not_fall_back_to_direct(monkeypatch):
    entry_report = {"state": "form_entry_unverified", "entry_evidence": {"reason": "submit_outcome_unknown"}}
    run = AsyncMock(return_value=entry_report)
    monkeypatch.setattr(FormEntry, "run", run)
    page = SimpleNamespace(goto=AsyncMock())
    result = asyncio.run(airchina.inspect(
        SimpleNamespace(throttle=AsyncMock()), SimpleNamespace(timeout_ms=1000), ROUTE, page, entry_mode="form",
    ))
    assert result["state"] == "form_entry_unverified"
    assert not result["quote_saved"]
    page.goto.assert_not_awaited()


def test_calendar_navigation_stops_after_twelve_flips():
    button = AsyncMock()
    button.count.return_value = 1
    button.get_attribute.return_value = "calendar-next"
    date_field = object()
    page = SimpleNamespace(
        get_by_placeholder=lambda *args, **kwargs: date_field,
        locator=lambda selector: button,
    )
    entry = FormEntry(page, ROUTE)
    entry.click = AsyncMock()
    # A stale calendar far behind the requested date must not traverse forever.
    entry.wait = AsyncMock(return_value={"calendars": [
        {"year": "2000年", "month": "1月"},
        {"year": "2000年", "month": "2月"},
    ]})
    with pytest.raises(FormStop) as stopped:
        asyncio.run(entry.departure_date())
    assert stopped.value.reason == "calendar_navigation_limit"
    assert entry.click.await_count == 13  # date field once, then 12 month flips
    assert sum(call.args == (button,) for call in entry.click.await_args_list) == 12


@pytest.mark.parametrize("step", ["filling_input", "waiting_candidates", "clicking_candidate", "confirming_selection"])
def test_airport_timeout_records_specific_step_and_sanitized_snapshot(step):
    from unittest.mock import Mock
    entry, page, submit, data = fake_entry()
    data["origin"]["diagnostics"] = {
        "input_matches": True, "input_focused": True, "input_enabled": True,
        "scoped_candidates": 0, "page_candidates": 3, "scoped_exact": 0, "page_exact": 1,
        "counts_capped": False, "private": "SECRET",
    }
    field, option = AsyncMock(), AsyncMock()
    field.count.return_value = option.count.return_value = 1
    option.filter = Mock(return_value=option)
    root = AsyncMock()
    root.count.return_value = 1
    root.locator = lambda s: field if s == "input:visible" else option
    page.locator = Mock(return_value=Mock())
    entry.airport_root = lambda _: root
    entry.airport = FormEntry.airport.__get__(entry)
    if step == "filling_input":
        field.fill.side_effect = TimeoutError("SECRET")
    elif step == "waiting_candidates":
        option.wait_for.side_effect = TimeoutError("SECRET")
    elif step == "clicking_candidate":
        option.click.side_effect = TimeoutError("SECRET")
    else:
        original_wait = entry.wait
        async def wait(predicate, reason, **kwargs):
            if reason == "airport_selection_unconfirmed":
                raise TimeoutError("SECRET")
            return await original_wait(predicate, reason, **kwargs)
        entry.wait = wait
    # Homepage trip selector still uses the normal safety check.
    control = AsyncMock()
    control.count.return_value = 1
    page.locator.side_effect = lambda s: control if 'data-id' in s else Mock()
    result = asyncio.run(entry.run(1000))
    assert result["entry_evidence"]["reason"] == f"airport_{step}_timeout"
    observation = result["entry_evidence"]["airport_observation"]
    assert observation["step"] == step
    assert observation["page_exact"] == 1 and observation["scoped_exact"] == 0
    assert "SECRET" not in str(result)
    submit.click.assert_not_awaited()
    if step == "waiting_candidates":
        option.click.assert_not_awaited()  # never widen scope to global match


def test_airport_projection_allowlist_caps_counts_and_drops_raw_text():
    entry, _, _, _ = fake_entry()
    entry.airport_side, entry.airport_step = "origin", "waiting_candidates"
    data = snapshot()
    data["origin"]["diagnostics"] = {"page_candidates": 999, "page_exact": True,
                                    "input_matches": "SECRET", "raw": "SECRET"}
    entry.record_airport(data)
    observation = entry.evidence["airport_observation"]
    assert observation["page_candidates"] == 100
    assert observation["page_exact"] is None and observation["input_matches"] is None
    assert "SECRET" not in str(entry.evidence)
