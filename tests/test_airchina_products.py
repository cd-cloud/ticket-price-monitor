import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from airchina_probe import inspect_airchina_page
from config_manager import RouteQuery
from providers.airchina_products import parse_product_fares, expand_fare_products
from test_airchina_probe import fake_automation


ROUTE = RouteQuery("PEK", "SHA", "2026-11-10")


def sample(cabin="economy"):
    data = json.loads((Path(__file__).parent / "fixtures" / f"airchina_expanded_{cabin}.json").read_text())
    # Simulated current guard for browser tests; original recording is unchanged.
    data["stale_visible"] = False
    return data


def flight(snapshot):
    return next(r for r in snapshot["rows"] if r["flight_numbers"] == ["CA1549"])


def test_real_economy_products_are_bound_and_not_discounted_or_tax_inclusive():
    result = parse_product_fares(sample(), ROUTE)
    assert result["status"] == "fare_products_observed"
    assert [p["seat_class"] for p in result["products"]] == ["K", "S", "V", "U", "Y"]
    assert [p["base_fare"] for p in result["products"]] == [600, 930, 1300, 1830, 2150]
    first = result["products"][0]
    assert first["flight_no"] == "CA1549" and first["departure_time"] == "16:30"
    assert first["product_name"] == "超值特价"
    assert first["baggage_summary"] == "20KG"
    assert first["refund_change_summary"] == "5%起"
    assert first["mileage_summary"] == "25%"
    assert first["estimated_discount"] == 30
    assert first["estimated_discount_applied"] is False
    assert first["tax_included"] is False
    assert first["total_price"] is None
    assert first["fare_product_confirmed"] is True
    assert first["fare_rules_complete"] is False
    assert first["inventory_status"] == "unknown"
    assert first["displayed_seats"] is None  # Transparent placeholder is excluded.
    assert first["bookable_confirmed"] is False
    assert "price" not in first
    assert result["quote_saved"] is False


def test_business_sample_is_separate_from_economy_and_seats_are_only_displayed():
    result = parse_product_fares(sample("business"), replace(ROUTE, cabin="business"))
    assert [p["seat_class"] for p in result["products"]] == ["R", "J"]
    assert [p["base_fare"] for p in result["products"]] == [2880, 8390]
    assert [p["displayed_seats"] for p in result["products"]] == [3, 7]
    assert all(p["inventory_status"] == "shown_remaining" and not p["inventory_confirmed"]
               for p in result["products"])
    assert not parse_product_fares(sample("business"), ROUTE)["products"]


@pytest.mark.parametrize(("field", "value"), [
    ("verification_visible", True), ("blocked_visible", True),
    ("loading_visible", True), ("loading_visible", None),
    ("notice_confirmation_visible", True), ("notice_confirmation_visible", None),
    ("date_text", "2026年11月11日"), ("price_notice", "含税总价"),
])
def test_wrong_page_or_transition_never_produces_product_fares(field, value):
    data = sample()
    data[field] = value
    assert not parse_product_fares(data, ROUTE)["products"]


@pytest.mark.parametrize("expanded", [[], ["business"], ["first"], ["economy", "business"], None])
def test_missing_ambiguous_or_stale_cabin_selection_is_rejected(expanded):
    data = sample()
    flight(data)["expanded_cabins"] = expanded
    assert not parse_product_fares(data, replace(ROUTE, cabin="business_first") if expanded == ["business"] else ROUTE)["products"]


@pytest.mark.parametrize(("field", "value"), [
    ("price_text", "¥30预估优惠"), ("price_text", "¥600起"), ("price_text", "¥0"),
    ("price_text", "¥500"), ("seat_class_text", "(J舱)"), ("seat_class_text", "(未知舱)"),
    ("name", ""), ("summaries", None),
])
def test_malformed_products_are_not_promoted(field, value):
    data = sample()
    item = flight(data)["products"][0]
    item[field] = value
    flight(data)["products"] = [item]
    result = parse_product_fares(data, ROUTE)
    assert not result["products"]
    assert result["rejected_products"] == 1


def test_visible_zero_stock_is_not_confused_with_placeholder():
    data = sample()
    flight(data)["products"][0]["seat_text"] = "剩余0张"
    result = parse_product_fares(data, ROUTE)
    assert result["products"][0]["inventory_status"] == "shown_zero"
    assert not result["products"][0]["bookable_confirmed"]


def test_corrupt_row_cannot_borrow_identity_from_good_duplicate():
    data = sample()
    invalid = copy.deepcopy(flight(data))
    invalid["arrival_airport"] = "PVG T2"
    flight(data)["products"] = []
    data["rows"].append(invalid)
    assert not parse_product_fares(data, ROUTE)["products"]


def test_conflicting_same_product_prices_are_excluded():
    data = sample()
    duplicate = copy.deepcopy(flight(data)["products"][0])
    duplicate["price_text"] = "¥601"
    flight(data)["products"].append(duplicate)
    result = parse_product_fares(data, ROUTE)
    assert len(result["products"]) == 4
    assert result["conflicting_products"] == 1


class Locator:
    def __init__(self, *, active=False):
        self.active = active
        self.click = AsyncMock()
        self.count = AsyncMock(return_value=1)
        self.get_attribute = AsyncMock(return_value="cabinContent_activeCabin__test" if active else "")
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return self

    def filter(self, **_kwargs):
        return self


def fake_page(snapshots, *, active=False):
    loc = Locator(active=active)
    return SimpleNamespace(evaluate=AsyncMock(side_effect=snapshots), locator=loc.locator), loc


def run_expand(page, **kwargs):
    return asyncio.run(expand_fare_products(page, ROUTE, flight_no="CA1549", cabin="economy", **kwargs))


def test_expand_clicks_one_cabin_never_booking_and_requires_stable_read():
    data = sample()
    loading = copy.deepcopy(data)
    loading["loading_visible"] = True
    page, loc = fake_page([data, loading, data, data])
    result = run_expand(page)
    assert result["status"] == "fare_products_observed" and len(result["products"]) == 5
    loc.click.assert_awaited_once()
    assert page.evaluate.await_count == 4
    assert all("btnWrapper" not in s for s in loc.selectors)
    assert '[class*="cabinContent_economy__"]' in loc.selectors


def test_already_expanded_cabin_not_toggled_closed():
    data = sample()
    page, loc = fake_page([data, data, data], active=True)
    assert run_expand(page)["status"] == "fare_products_observed"
    loc.click.assert_not_awaited()


@pytest.mark.parametrize("field", ["verification_visible", "loading_visible", "notice_confirmation_visible", "stale_visible"])
def test_blocking_state_does_not_click(field):
    data = sample()
    data[field] = True
    page, loc = fake_page([data])
    assert run_expand(page)["reason"] == "page_not_ready"
    loc.click.assert_not_awaited()


def test_challenge_after_expand_stops_without_retry():
    data = sample()
    challenged = copy.deepcopy(data)
    challenged["verification_visible"] = True
    page, loc = fake_page([data, challenged])
    assert run_expand(page)["status"] == "needs_attention"
    loc.click.assert_awaited_once()


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("stale_visible", True, "stale_results"),
    ("notice_confirmation_visible", True, "needs_notice_confirmation"),
    ("url", "https://www.airchina.com.cn/zh-CN", "unexpected_page"),
])
def test_page_changed_during_expansion_discards_products(field, value, reason):
    data = sample()
    changed = copy.deepcopy(data)
    changed[field] = value
    page, loc = fake_page([data, data, changed])
    result = run_expand(page)
    assert result["reason"] == reason and not result["products"]
    assert page.evaluate.await_count == 3
    loc.click.assert_awaited_once()


def test_timeout_does_not_reclick():
    data = sample()
    empty = copy.deepcopy(data)
    flight(empty)["products"] = []
    page, loc = fake_page([data, empty])
    assert run_expand(page, timeout_ms=100)["reason"] == "product_details_timeout"
    loc.click.assert_awaited_once()


def test_duplicate_dom_rows_do_not_click_first():
    page, loc = fake_page([sample()])
    loc.count.return_value = 2
    assert run_expand(page)["reason"] == "ambiguous_dom_row"
    loc.click.assert_not_awaited()


def test_only_requested_flight_is_returned():
    data = sample()
    other = data["rows"][0]
    other.update(expanded_cabins=["economy"], products=copy.deepcopy(flight(data)["products"]))
    # Other flight's starting fare is higher; its valid higher-priced products
    # must still not be returned by the selected-flight action.
    page, loc = fake_page([data, data, data])
    assert all(p["flight_no"] == "CA1549" for p in run_expand(page)["products"])


def test_probe_exposes_products_only_when_explicitly_requested(tmp_path):
    from test_airchina_probe import TARGET
    automation, page = fake_automation(tmp_path, text="选择航班")
    data = sample()
    data.update(url=TARGET, date_text="2099年1月1日")
    page.evaluate.return_value = data
    route = replace(ROUTE, departure_date="2099-01-01")
    parsed = parse_product_fares(data, route)
    with patch("providers.airchina_products.expand_fare_products", new=AsyncMock(return_value=parsed)) as expand:
        path = asyncio.run(inspect_airchina_page(automation, route, flight_no="CA1549"))
        report = json.loads(path.read_text())
        assert report["state"] == "fare_products_observed"
        assert len(report["product_preview"]["products"]) == 5
        assert not report["quote_saved"]
        expand.assert_awaited_once()


def test_probe_without_flight_never_expands(tmp_path):
    automation, page = fake_automation(tmp_path, text="选择航班")
    with patch("providers.airchina_products.expand_fare_products", new=AsyncMock()) as expand:
        asyncio.run(inspect_airchina_page(automation, replace(ROUTE, departure_date="2099-01-01")))
        expand.assert_not_awaited()


def test_probe_bad_flight_rejected_before_browser(tmp_path):
    automation, page = fake_automation(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(inspect_airchina_page(automation, replace(ROUTE, departure_date="2099-01-01"),
                                         flight_no="CA1549;book"))
    automation._new_page.assert_not_awaited()
