"""Real public DOM fixture plus deliberate corruption; no live network calls."""
import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config_manager import RouteQuery
from providers.airchina_parser import parse_reference_fares, collect_reference_fares, DOM_SCRIPT
from airchina_probe import inspect_airchina_page
from test_airchina_probe import fake_automation


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "airchina_visible_oneway.json"
ROUTE = RouteQuery("PEK", "SHA", "2026-11-10")


def fixture():
    return json.loads(FIXTURE_PATH.read_text())


def test_real_snapshot_binds_16_flights_to_economy_starting_fares():
    result = parse_reference_fares(fixture(), ROUTE)
    assert result["status"] == "reference_fares_observed"
    assert len(result["offers"]) == 16
    assert result["rejected_rows"] == 0
    cheapest = result["offers"][0]
    assert (cheapest["flight_no"], cheapest["departure_time"], cheapest["arrival_time"]) == ("CA1549", "16:30", "18:45")
    assert cheapest["reference_fare"] == 600
    assert cheapest["cabin"] == "economy"
    assert cheapest["tax_included"] is False
    assert cheapest["price_unit"] == "per_adult"
    assert cheapest["total_price"] is None
    assert cheapest["operating_airline"] is None
    assert cheapest["fare_product_confirmed"] is False
    assert cheapest["bookable_confirmed"] is False
    assert cheapest["inventory_confirmed"] is False
    assert "price" not in cheapest  # Not a PriceSnapshot input.
    assert not result["quote_saved"] and not result["parser_ready"]


@pytest.mark.parametrize(("cabin", "count", "minimum"), [
    ("first", 2, 9250), ("business", 16, 2880), ("premium_economy", 9, 1880),
    ("economy_plus", 25, 600), ("business_first", 18, 2880),
])
def test_columns_never_shift_when_other_cabins_are_missing(cabin, count, minimum):
    result = parse_reference_fares(fixture(), replace(ROUTE, cabin=cabin))
    assert len(result["offers"]) == count
    assert result["offers"][0]["reference_fare"] == minimum


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("schema", "new", "unknown_schema"),
    ("verification_visible", True, "access_unconfirmed"),
    ("verification_visible", None, "access_unconfirmed"),
    ("blocked_visible", True, "access_unconfirmed"),
    ("stale_visible", True, "stale_results"),
    ("loading_visible", True, "results_loading"),
    ("notice_confirmation_visible", True, "manual_notice_confirmation_required"),
    ("url", "https://evil.test/flight/oneway/pek-sha/2026-11-10", "url_mismatch"),
    ("url", "https://www.airchina.com.cn/flight/oneway/pek-sha/2026-11-11", "url_mismatch"),
    ("header_count", 2, "ambiguous_header"),
    ("trip_type", "往返", "ambiguous_header"),
    ("route_text", "北京大兴(PKX) 上海虹桥(SHA)", "visible_route_mismatch"),
    ("route_text", None, "visible_route_mismatch"),
    ("date_text", "2026年11月11日", "visible_date_mismatch"),
    ("date_text", "2026年02月30日", "visible_date_mismatch"),
    ("date_text", None, "visible_date_mismatch"),
    ("passengers_text", "2成人", "visible_passengers_mismatch"),
    ("passengers_text", "1成人1儿童", "visible_passengers_mismatch"),
    ("price_notice", "含税总价", "unknown_price_scope"),
    ("price_notice", None, "unknown_price_scope"),
    ("cabin_headers", {}, "unknown_cabin_columns"),
    ("rows", None, "missing_rows"),
])
def test_wrong_or_missing_page_evidence_does_not_borrow_query_conditions(field, value, reason):
    sample = fixture()
    sample[field] = value
    result = parse_reference_fares(sample, ROUTE)
    assert result["offers"] == []
    assert result["reason"] == reason


@pytest.mark.parametrize("changes", [
    {"return_date": "2026-11-12"}, {"route_type": "multi_city"}, {"passengers": 2},
    {"transfer_policy": "transfer_only"}, {"cabin": "unknown"},
])
def test_unsupported_modes_rejected(changes):
    result = parse_reference_fares(fixture(), replace(ROUTE, **changes))
    assert result["offers"] == []
    assert result["reason"] == "unsupported_request"


@pytest.mark.parametrize(("field", "value"), [
    ("flight_numbers", []), ("flight_numbers", ["CA1549", "ZH1234"]),
    ("flight_numbers", [None]), ("departure_time", "16:30+1"),
    ("arrival_time", "01:30"), ("arrival_time", "25:30"), ("duration", "4h15m"),
    ("between_text", "经停"), ("between_text", "中转"), ("between_text", None),
    ("departure_airport", "PKX"), ("arrival_airport", "PVG"),
    ("cabins", None),
])
def test_ambiguous_row_rejected_without_generic_price_fallback(field, value):
    sample = fixture()
    sample["rows"] = [sample["rows"][11]]
    sample["rows"][0][field] = value
    result = parse_reference_fares(sample, ROUTE)
    assert not result["offers"]
    assert result["rejected_rows"] == 1
    assert result["status"] == "inconclusive"


def test_calendar_prices_cannot_become_flight_fares():
    sample = fixture()
    sample["rows"] = []
    sample["calendar"] = "2026-11-10 ￥500"
    result = parse_reference_fares(sample, ROUTE)
    assert not result["offers"]
    assert not result["coverage_complete"]
    assert result["status"] == "inconclusive"


def test_seat_count_does_not_become_price():
    sample = fixture()
    sample["rows"] = [sample["rows"][11]]
    sample["rows"][0]["cabins"]["economy"]["price_text"] = None
    sample["rows"][0]["cabins"]["economy"]["cell_text"] = "3"
    assert not parse_reference_fares(sample, ROUTE)["offers"]


def test_fare_product_structure_change_not_silently_accepted():
    sample = fixture()
    sample["rows"] = [sample["rows"][11]]
    sample["rows"][0]["cabins"]["economy"]["price_text"] = "￥600"
    assert not parse_reference_fares(sample, ROUTE)["offers"]


def test_airline_filter_is_marketing_not_assumed_operating_airline():
    result = parse_reference_fares(fixture(), replace(ROUTE, preferred_airlines=["MU"]))
    assert result["offers"] == []
    assert result["status"] == "inconclusive"
    assert result["filtered_offers"] > 0


def test_duplicate_conflicting_cards_do_not_pick_lower_price():
    sample = fixture()
    sample["rows"] = [sample["rows"][11], copy.deepcopy(sample["rows"][11])]
    assert len(parse_reference_fares(sample, ROUTE)["offers"]) == 1
    sample["rows"][1]["cabins"]["economy"]["price_text"] = "￥999起"
    result = parse_reference_fares(sample, ROUTE)
    assert result["offers"] == []
    assert result["conflicting_offers"] == 1


def test_collector_executes_one_atomic_read_only_projection():
    page = AsyncMock()
    page.evaluate.return_value = fixture()
    result = asyncio.run(collect_reference_fares(page, ROUTE))
    assert len(result["offers"]) == 16
    page.evaluate.assert_awaited_once_with("(" + DOM_SCRIPT + "\n)()")


def test_probe_reports_reference_preview_without_enabling_price_storage(tmp_path):
    automation, page = fake_automation(tmp_path, text="选择航班")
    future = replace(ROUTE, departure_date="2099-01-01")
    sample = fixture()
    sample.update(url=page.url, date_text="2099年1月1日")
    # Synthetic live-state evidence, not a change to the historical fixture.
    sample.update(stale_visible=False, loading_visible=False, notice_confirmation_visible=False)
    page.evaluate.return_value = sample
    path = asyncio.run(inspect_airchina_page(automation, future))
    report = json.loads(path.read_text())
    assert report["state"] == "reference_fares_observed"
    assert len(report["reference_preview"]["offers"]) == 16
    assert report["quote_saved"] is False
    assert report["parser_ready"] is False
    assert not automation.app_config.providers["airchina"].enabled


def test_probe_challenge_never_extracts_background_fares(tmp_path):
    automation, page = fake_automation(tmp_path)
    future = replace(ROUTE, departure_date="2099-01-01")
    asyncio.run(inspect_airchina_page(automation, future))
    page.evaluate.assert_not_awaited()
