"""Adapter boundaries and lifecycle; only fixtures/mocks, no live requests."""
import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from airchina_probe import inspect_airchina_page
from providers import airchina
from providers.airchina_result_wait import LIVE_GUARDS, wait_for_results
from test_airchina_probe import fake_automation, ROUTE, TARGET


def snapshot():
    # Recorded public fares plus synthetic current request/guard evidence.
    path = Path(__file__).parent / "fixtures" / "airchina_expanded_economy.json"
    data = json.loads(path.read_text())
    data.update(url=TARGET, date_text="2099年1月1日", stale_visible=False)
    return data


def page_for(*snapshots):
    return SimpleNamespace(
        evaluate=AsyncMock(side_effect=list(snapshots)), wait_for_timeout=AsyncMock(),
    )


def wait(page, **kwargs):
    return asyncio.run(wait_for_results(page, ROUTE, expected_url=TARGET, **kwargs))


def test_wait_requires_two_identical_valid_projections():
    data = snapshot()
    page = page_for(data, copy.deepcopy(data))
    report = wait(page)
    assert report["state"] == "reference_fares_observed"
    assert len(report["reference_preview"]["offers"]) == 16
    assert report["wait_evidence"] == {
        "reason": "stable_visible_results", "polls": 2, "coverage_complete": False,
    }
    assert not hasattr(page, "goto")


def test_same_count_but_changed_fare_resets_stability():
    a = snapshot()
    b = copy.deepcopy(a)
    b["rows"][0]["cabins"]["economy"]["price_text"] = "¥999起"
    page = page_for(a, b, b)
    assert wait(page)["wait_evidence"]["polls"] == 3


def test_loading_resets_stability_and_does_not_reuse_old_price():
    data = snapshot()
    loading = {**data, "loading_visible": True}
    page = page_for(data, loading, data, data)
    assert wait(page)["wait_evidence"]["polls"] == 4


@pytest.mark.parametrize(("field", "state"), [
    ("verification_visible", "needs_verification"),
    ("blocked_visible", "blocked"),
    ("stale_visible", "stale_results"),
    ("notice_confirmation_visible", "needs_notice_confirmation"),
])
def test_terminal_state_after_first_sample_never_returns_old_fares(field, state):
    data = snapshot()
    page = page_for(data, {**data, field: True})
    report = wait(page)
    assert report["state"] == state
    assert report["reference_preview"]["offers"] == []
    assert page.evaluate.await_count == 2


@pytest.mark.parametrize("field", LIVE_GUARDS)
def test_missing_guard_cannot_establish_live_ready_state(field):
    data = snapshot()
    del data[field]
    report = wait(page_for(data, data), timeout_ms=500)
    assert report["state"] == "query_page_unverified"
    assert not report["reference_preview"]["offers"]


@pytest.mark.parametrize("changes", [
    {"rows": []}, {"date_text": "2099年1月2日"}, {"trip_type": "往返"},
    {"passengers_text": "2成人"}, {"header_count": 2},
])
def test_stable_but_invalid_or_empty_evidence_not_ready(changes):
    data = {**snapshot(), **changes}
    report = wait(page_for(data, data), timeout_ms=500)
    assert report["state"] == "query_page_unverified"
    assert report["wait_evidence"]["reason"] == "result_wait_budget"
    assert not report["reference_preview"]["offers"]


def test_redirect_is_terminal_not_a_new_navigation():
    data = snapshot()
    report = wait(page_for(data, {**data, "url": "https://www.airchina.com.cn/zh-CN"}))
    assert report["state"] == "unexpected_page"
    assert not report["reference_preview"]["offers"]


def test_wall_clock_bounds_hung_browser_evaluation():
    async def hung(_script):
        await asyncio.sleep(10)
    page = SimpleNamespace(evaluate=hung)
    assert wait(page, timeout_ms=100, poll_ms=50)["wait_evidence"]["reason"] == "result_wait_timeout"


def test_wait_cancellation_propagates():
    page = page_for()
    page.evaluate.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        wait(page)


def test_adapter_uses_runtime_facade_once_not_ctrip_or_generic_extraction():
    data = snapshot()
    page = page_for(data, data)
    page.url = TARGET
    page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
    page.locator = lambda _: SimpleNamespace(inner_text=AsyncMock(return_value="选择航班"))
    runtime = SimpleNamespace(throttle=AsyncMock())  # No Ctrip/browser-private methods.
    report = asyncio.run(airchina.inspect(runtime, SimpleNamespace(timeout_ms=1000), ROUTE, page))
    runtime.throttle.assert_awaited_once()
    page.goto.assert_awaited_once()
    assert report["state"] == "reference_fares_observed"
    assert not report["final_quote_supported"] and not report["quote_saved"]


@pytest.mark.parametrize("text", [
    "您的停留时间过长，航班及价格信息可能发生变动", "确认，查看下一个提示",
])
def test_stale_or_notice_page_stops_before_dom_read(tmp_path, text):
    automation, page = fake_automation(tmp_path, text=text)
    report = json.loads(asyncio.run(inspect_airchina_page(automation, ROUTE)).read_text())
    assert report["state"] in {"stale_results", "needs_notice_confirmation"}
    page.evaluate.assert_not_awaited()
    page.goto.assert_awaited_once()


def test_manual_notice_can_be_acknowledged_by_user_then_stable_result_read(tmp_path):
    automation, page = fake_automation(tmp_path)
    page.locator.return_value.inner_text.side_effect = ["确认，查看下一个提示", "选择航班"]
    page.evaluate.return_value = snapshot()
    report = json.loads(asyncio.run(inspect_airchina_page(
        automation, ROUTE, manual_wait_seconds=20,
    )).read_text())
    assert report["state"] == "reference_fares_observed"
    page.goto.assert_awaited_once()
    assert page.evaluate.await_count == 2


def test_expansion_failure_discards_previous_list_preview(tmp_path):
    automation, page = fake_automation(tmp_path, text="选择航班")
    page.evaluate.return_value = snapshot()
    failure = {"status": "needs_attention", "reason": "stale_results", "products": []}
    with patch("providers.airchina_products.expand_fare_products",
               new=AsyncMock(return_value=failure)):
        report = json.loads(asyncio.run(inspect_airchina_page(
            automation, ROUTE, flight_no="CA1549",
        )).read_text())
    assert report["state"] == "stale_results"
    assert not report["reference_preview"]["offers"]
    assert not report["product_preview"]["products"]
    assert not report["quote_saved"]


def test_expansion_exception_cannot_leave_partial_success_or_secrets(tmp_path):
    automation, page = fake_automation(tmp_path, text="选择航班")
    page.evaluate.return_value = snapshot()
    with patch("providers.airchina_products.expand_fare_products",
               new=AsyncMock(side_effect=RuntimeError("private-session-token"))):
        path = asyncio.run(inspect_airchina_page(automation, ROUTE, flight_no="CA1549"))
    report = json.loads(path.read_text())
    assert report["state"] == "probe_error"
    assert "reference_preview" not in report
    assert "private-session-token" not in path.read_text()
    automation._close_context_quietly.assert_awaited_once()


def test_production_contract_never_promotes_base_fare_to_total():
    with pytest.raises(RuntimeError, match="尚未完成税费"):
        asyncio.run(airchina.query(None, None, ROUTE, None, []))
    assert not airchina.supports_multi_city
    assert not airchina.supports_tail_discovery


@pytest.mark.parametrize("changes", [
    {"return_date": "2099-01-03"}, {"route_type": "multi_city"},
    {"passengers": 2}, {"origin": "BJS"},
])
def test_unsupported_queries_never_touch_runtime(changes):
    runtime = SimpleNamespace(throttle=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(airchina.inspect(runtime, None, replace(ROUTE, **changes), None))
    runtime.throttle.assert_not_awaited()
