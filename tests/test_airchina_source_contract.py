"""Offline migration contracts; mocks are not evidence of website support."""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from providers.airchina_direct import create_airchina_source, flyclaw_records_from_report
from providers.airchina_source_contract import plan_query

DAY = (date.today() + timedelta(days=30)).isoformat()


def plan(origin="PEK", destination="SHA", **kw):
    return plan_query(
        origin, destination, kw.pop("date", DAY),
        **dict(return_date=None, adults=1, cabin="economy", stops=0, limit=None,
               timeout=30, max_airport_pairs=4, entry_mode="form", flight_no=None) | kw,
    )


def report(route, price=600, **changes):
    offer = dict(
        flight_no="CA1549", marketing_airline="CA", origin=route.origin,
        destination=route.destination, departure_date=route.departure_date,
        arrival_date=route.departure_date, departure_time="16:30", arrival_time="18:45",
        cabin=route.cabin, reference_fare=price,
    )
    offer.update(changes)
    return {"state": "reference_fares_observed", "reference_preview": {"offers": [offer]}}


def source(handler=None, **kwargs):
    service = SimpleNamespace(inspect_airchina_experiment=AsyncMock(
        side_effect=handler or (lambda route, **kw: report(route))))
    return create_airchina_source(service, **kwargs), service


@pytest.mark.parametrize("value,expected", [
    ("上海", {"PVG", "SHA"}), ("Shanghai", {"PVG", "SHA"}), ("sha", {"SHA"}),
    ("浦东", {"PVG"}),
])
def test_city_expansion(value, expected):
    assert {r.destination for r in plan(destination=value).routes} == expected


def test_city_pair_budget_never_silently_truncates():
    assert len(plan("北京", "上海").routes) == 4
    with pytest.raises(ValueError, match="budget"):
        plan("北京", "上海", max_airport_pairs=3)


@pytest.mark.parametrize("kw", [
    {"limit": 0}, {"limit": -1}, {"limit": True}, {"limit": 1.5},
    {"timeout": 0}, {"timeout": float("nan")}, {"timeout": float("inf")}, {"timeout": True},
    {"stops": -1}, {"stops": True}, {"stops": "3"}, {"stops": 1.0},
    {"adults": True}, {"adults": 2}, {"return_date": ""}, {"return_date": DAY},
    {"cabin": "unknown"}, {"date": "2026-02-30"}, {"date": "2026-1-1"},
    {"max_airport_pairs": 0}, {"max_airport_pairs": 17}, {"max_airport_pairs": True},
])
def test_invalid_request_rejected_before_browser(kw):
    with pytest.raises(ValueError):
        plan(**kw)


@pytest.mark.parametrize("o,d", [("", "SHA"), ("not-an-airport", "SHA"), ("上海", "PVG"), ("PEK", "PEK")])
def test_invalid_or_overlapping_airports(o, d):
    with pytest.raises(ValueError):
        plan(o, d)


def test_results_mode_disallows_city_batch():
    with pytest.raises(ValueError, match="one explicit"):
        plan(destination="上海", entry_mode="results")


@pytest.mark.parametrize("requested,normalized", [
    ("economy", "economy"), ("premium", "premium_economy"),
    ("premium_economy", "premium_economy"), ("business", "business"), ("first", "first"),
])
def test_four_cabins_and_alias(requested, normalized):
    src, service = source()
    records = asyncio.run(src.query_by_route_async("PEK", "SHA", DAY, cabin=requested))
    assert service.inspect_airchina_experiment.await_args.args[0].cabin == normalized
    assert records[0]["cabin"] == normalized
    assert records[0]["cabin_class"] in {"经济舱", "超级经济舱", "公务舱", "头等舱"}
    assert records[0]["reference_only"] is True
    assert records[0]["tax_included"] is False
    assert records[0]["trip_type"] == "one_way"


@pytest.mark.parametrize("stops", [0, 1, 2, "any"])
def test_max_stops_keeps_direct_subset_without_claiming_connection_coverage(stops):
    src, _ = source()
    records = asyncio.run(src.query_by_route_async("PEK", "SHA", DAY, stops=stops))
    assert records[0]["stops"] == 0
    assert records[0]["connection_coverage"] == "unsupported"
    assert src.last_query["coverage_complete"] is False


def test_city_search_serial_global_limit_after_sort_and_original_timeout_unchanged():
    active = 0
    budgets = []

    async def handler(route, **kw):
        nonlocal active
        active += 1
        assert active == 1
        budgets.append(kw["timeout_seconds"])
        await asyncio.sleep(.005)
        active -= 1
        return report(route, 800 if route.destination == "PVG" else 500)

    src, service = source(handler, timeout=240)
    records = asyncio.run(src.query_by_route_async("PEK", "上海", DAY, limit=1, timeout=10))
    assert service.inspect_airchina_experiment.await_count == 2
    assert records[0]["destination_iata"] == "SHA"
    assert records[0]["price"] == 500
    assert src.last_query["truncated"] is True
    assert src.last_query["records_before_limit"] == 2
    assert src.last_query["airport_search_complete"] is True
    assert src.timeout == 240
    assert budgets[0] <= 10 and budgets[1] < budgets[0]


@pytest.mark.parametrize("state", ["needs_verification", "blocked", "stale_results", "query_page_unverified"])
def test_stop_after_first_blocked_pair_no_rotation_or_retry(state):
    src, service = source(lambda route, **kw: {"state": state})
    assert asyncio.run(src.query_by_route_async("北京", "上海", DAY)) == []
    assert service.inspect_airchina_experiment.await_count == 1
    assert src.last_query["stop_reason"] == state
    assert not src.last_query["airport_search_complete"]


def test_partial_batch_does_not_lose_earlier_reference_or_claim_full_coverage():
    n = 0

    def handler(route, **kw):
        nonlocal n
        n += 1
        return report(route) if n == 1 else {"state": "needs_verification"}

    src, service = source(handler)
    records = asyncio.run(src.query_by_route_async("北京", "上海", DAY))
    assert len(records) == 1 and service.inspect_airchina_experiment.await_count == 2
    assert src.last_query["state"] == "partial"
    assert records[0]["airport_search_complete"] is False


def test_timeout_cleans_up_and_does_not_start_another_pair():
    cleaned = []

    async def handler(route, **kw):
        try:
            await asyncio.sleep(10)
        finally:
            cleaned.append(True)

    src, service = source(handler)
    assert asyncio.run(src.query_by_route_async("北京", "上海", DAY, timeout=.02)) == []
    assert cleaned == [True]
    assert service.inspect_airchina_experiment.await_count == 1
    assert src.last_query["stop_reason"] == "timeout"
    assert src.last_report["state"] == "error"


def test_error_text_is_not_persisted():
    async def handler(route, **kw):
        raise RuntimeError("secret-cookie-and-sensitive-url")

    src, _ = source(handler)
    assert asyncio.run(src.query_by_route_async("PEK", "SHA", DAY)) == []
    assert "secret-cookie" not in str(src.last_query) + str(src.last_report)


def test_cancel_before_start():
    src, service = source()
    src.cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(src.query_by_route_async("PEK", "SHA", DAY))
    service.inspect_airchina_experiment.assert_not_awaited()
    assert src.last_report["state"] == "cancelled"


def test_rejected_query_clears_previous_report():
    src, _ = source()
    asyncio.run(src.query_by_route_async("PEK", "SHA", DAY))
    with pytest.raises(ValueError):
        asyncio.run(src.query_by_route_async("PEK", "SHA", DAY, limit=-1))
    assert src.last_report is None
    assert src.last_query["state"] == "invalid_request"


def test_sync_api_and_event_loop_error():
    src, _ = source()
    assert src.query_by_route("PEK", "SHA", DAY)

    async def run():
        with pytest.raises(RuntimeError, match="await"):
            src.query_by_route("PEK", "SHA", DAY)
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"reference_fare": float("nan")}, {"reference_fare": float("inf")},
    {"reference_fare": True}, {"reference_fare": -1}, {"reference_fare": "bad"},
    {"departure_time": "25:00"}, {"arrival_time": "12:00"}, {"arrival_date": "bad"},
    {"destination": "PVG"}, {"currency": "USD"}, {"cabin": "business"},
    {"flight_no": ""}, {"departure_date": "2020-01-01"},
])
def test_malformed_offer_is_not_a_valid_record(change):
    route = plan().routes[0]
    assert flyclaw_records_from_report(report(route, **change), route) == []


def test_native_failure_never_leaks_stale_preview():
    route = plan().routes[0]
    result = report(route)
    result["state"] = "needs_verification"
    assert flyclaw_records_from_report(result, route) == []


def test_selected_flight_product_report_does_not_upgrade_base_fare():
    def handler(route, **kw):
        result = report(route)
        result["state"] = "fare_products_observed"
        result["product_preview"] = {"products": [{"base_fare": 900}]}
        return result

    src, _ = source(handler, flight_no="CA1549")
    record = src.query_by_route("PEK", "SHA", DAY)[0]
    assert record["price"] == 600
    assert record["bookable_confirmed"] is False
    src.flight_no = "CA1501"
    assert src.query_by_route("PEK", "SHA", DAY) == []


def test_flight_number_without_route_has_explicit_unsupported_diagnostic():
    src, service = source()
    assert src.query_by_flight("CA1549") == []
    assert src.last_query["state"] == "unsupported"
    service.inspect_airchina_experiment.assert_not_awaited()


def test_concurrent_calls_cannot_overwrite_source_diagnostics():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(route, **kw):
            entered.set()
            await release.wait()
            return report(route)

        src, service = source(handler)
        first = asyncio.create_task(src.query_by_route_async("PEK", "SHA", DAY))
        await entered.wait()
        with pytest.raises(RuntimeError, match="already active"):
            await src.query_by_route_async("PEK", "PVG", DAY)
        release.set()
        assert await first
        assert service.inspect_airchina_experiment.await_count == 1
        assert src.last_query["planned_pairs"] == [{"origin": "PEK", "destination": "SHA"}]
    asyncio.run(run())


def test_external_cancel_is_propagated_and_lock_released():
    async def run():
        entered = asyncio.Event()

        async def handler(route, **kw):
            entered.set()
            await asyncio.sleep(10)

        src, _ = source(handler)
        task = asyncio.create_task(src.query_by_route_async("PEK", "SHA", DAY))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert src.last_query["state"] == "cancelled"
        assert not src._query_lock.locked()
    asyncio.run(run())
